"""Runs on node 1: cuts one MP4 per team for every match manifest that arrived with the archive copy.

Watches ARCHIVE_DIR/recordings/_manifests. For each manifest not stitched yet, and once the
recordings up to its end have been copied (or STITCH_WAIT_MINUTES have passed), it joins the
team's segments between the match's start and end without re-encoding:
  ARCHIVE_DIR/vods/<match_id>_<team path>.mp4
Gaps in the footage are noted in ARCHIVE_DIR/stitch-status.json, which the archiver reads back
for `!stream archive` and `!stream vods`.

Settings: ARCHIVE_DIR (default /archive), STITCH_WAIT_MINUTES (default 10).
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from . import recordings

log = logging.getLogger("mediactl.stitch")

ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/archive"))
WAIT_MINUTES = float(os.environ.get("STITCH_WAIT_MINUTES", "10"))
POLL_SECONDS = 30


def _gaps(segs: list[recordings.Segment], start: float, end: float) -> list[dict]:
    gaps, cursor = [], start
    for seg in segs:
        if seg.start - cursor > recordings.GAP_TOLERANCE:
            gaps.append({"from": cursor, "to": seg.start})
        cursor = max(cursor, seg.end)
    if end - cursor > recordings.GAP_TOLERANCE:
        gaps.append({"from": cursor, "to": end})
    return gaps


async def stitch_team(manifest: dict, team: str, out_dir: Path) -> dict:
    start, end = manifest["start"], manifest["end"]
    segs = recordings.overlapping(recordings.segments(ARCHIVE_DIR / "recordings" / team), start, end)
    entry = {"match_id": manifest["match_id"], "team": team, "team_name": manifest.get("names", {}).get(team),
             "set_id": manifest.get("set_id"), "match_no": manifest.get("match_no"),
             "start": start, "end": end, "gaps": _gaps(segs, start, end)}
    if not segs:
        entry["error"] = "no footage"
        return entry
    out = out_dir / f"{manifest['match_id']}_{team}.mp4"
    with tempfile.NamedTemporaryFile("w", suffix=".ffconcat", delete=False) as f:
        f.write(recordings.concat_list(segs, start, end))
        listfile = f.name
    tmp = out.with_suffix(".part.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", listfile, "-c", "copy", "-movflags", "+faststart", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    Path(listfile).unlink(missing_ok=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        entry["error"] = err.decode(errors="replace").strip()[-300:]
        return entry
    tmp.replace(out)
    entry.update(path=str(out), size_bytes=out.stat().st_size)
    return entry


def _ready(manifest: dict, now: float) -> bool:
    """Recordings up to the match's end have arrived (or we've waited long enough)."""
    if now > manifest["end"] + WAIT_MINUTES * 60:
        return True
    for team in manifest["teams"]:
        segs = recordings.segments(ARCHIVE_DIR / "recordings" / team)
        if not segs or segs[-1].start < manifest["end"]:
            return False
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    manifests_dir = ARCHIVE_DIR / "recordings" / "_manifests"
    out_dir = ARCHIVE_DIR / "vods"
    out_dir.mkdir(parents=True, exist_ok=True)
    status_file = ARCHIVE_DIR / "stitch-status.json"
    try:
        status = json.loads(status_file.read_text())
    except (OSError, ValueError):
        status = {"done": [], "vods": [], "errors": [], "last_stitched": None}
    while True:
        for path in sorted(manifests_dir.glob("*.json")) if manifests_dir.is_dir() else []:
            try:
                manifest = json.loads(path.read_text())
            except ValueError:
                continue
            match_id = manifest.get("match_id")
            if not match_id or match_id in status["done"] or not _ready(manifest, time.time()):
                continue
            entries = [await stitch_team(manifest, team, out_dir) for team in manifest["teams"]]
            status["vods"] = [v for v in status["vods"] if v.get("match_id") != match_id] + \
                             [e for e in entries if "path" in e]
            status["errors"] = ([e for e in status["errors"] if e.get("match_id") != match_id] +
                                [e for e in entries if "error" in e])[-50:]
            status["done"].append(match_id)
            status["last_stitched"] = {"match_id": match_id, "at": time.time()}
            log.info("Stitched %s: %s", match_id, ", ".join(f"{e['team']} ({e.get('error', 'ok')})" for e in entries))
            tmp = status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=1))
            tmp.replace(status_file)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
