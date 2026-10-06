"""Whether a team's stream has settings that work everywhere, checked on its newest recorded segment.

Feeds pass the players' video through unchanged, so their encoder settings reach the casters as-is:
  * H.264: the delayed feeds only play H.264;
  * no B-frames: browsers (WebRTC) can't play them, so casters couldn't watch in a browser;
  * a keyframe at least every MAX_KEYFRAME_SECONDS: after any lost packet a viewer only recovers at the
    next keyframe (a frozen or black picture until then), and recordings are cut at keyframes;
  * at most MAX_BITRATE_KBPS, MAX_HEIGHT and MAX_FPS: casters watch several feeds at once;
  * sound.
Nothing is enforced: the go-live page shows the checklist with how to fix each item, and the bot
flags streams that fail (!stream inspect, overview, panels). Twitch passthrough isn't checked.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from . import config, probe, recordings

log = logging.getLogger(__name__)

CHECK_SECONDS = 15
MAX_BITRATE_KBPS = 8000
MAX_KEYFRAME_SECONDS = 2.5  # players set 2 s; a little room for rounding
MAX_HEIGHT = 1080
MAX_FPS = 61  # 60, with room for 59.94/60.0x rates

FIXES = {
    "codec": "Settings > Output > Video Encoder: pick an H.264 encoder (x264, NVIDIA NVENC H.264, AMD H.264 or "
             "QuickSync H.264), not HEVC or AV1.",
    "bframes": "Encoder settings: set B-Frames (Max B-frames) to 0. For x264, type bframes=0 under x264 Options.",
    "keyframes": "Encoder settings: set Keyframe Interval to 2 s (not 0 / auto).",
    "bitrate": "Encoder settings: set Bitrate to 6000 Kbps (at most 8000).",
    "resolution": "Settings > Video: set Output (Scaled) Resolution to 1920x1080 or lower.",
    "fps": "Settings > Video: set FPS to 60 or lower.",
    "audio": "No sound is coming through: unmute Desktop Audio (or your Application Audio Capture sources) in "
             "OBS's Audio Mixer. On the go-live page, click Add computer sound.",
}


async def video_timing(path: Path) -> dict:
    """B-frames and the longest gap between keyframes in a segment."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return {}
    packets = [p for p in data.get("packets", []) if p.get("pts_time") not in (None, "N/A")]
    pts = [float(p["pts_time"]) for p in packets]
    # B-frames: frames arrive out of display order (what browsers can't play; the encoder's own
    # has_b_frames flag can be set without any)
    has_b = any(b < a for a, b in zip(pts, pts[1:]))
    keys = [float(p["pts_time"]) for p in packets if "K" in (p.get("flags") or "")]
    span = (max(pts) - min(pts)) if pts else 0.0
    if len(keys) >= 2:
        gap, at_least = max(b - a for a, b in zip(keys, keys[1:])), False
    else:  # one keyframe (the one the segment starts on): the gap is at least the segment's length
        gap, at_least = span, True
    return {"b_frames": has_b, "keyframe_gap": round(gap, 2), "keyframe_gap_at_least": at_least}


def check(summary: dict, timing: dict) -> list[dict]:
    """The checklist: [{key, ok, value}] (a failed item's fix is in FIXES)."""
    video = summary.get("video") or {}
    items = []

    def add(key, ok, value):
        items.append({"key": key, "ok": bool(ok), "value": value})

    codec = (video.get("codec") or "?").lower()
    add("codec", codec == "h264", codec.upper())
    if "b_frames" in timing:
        add("bframes", not timing["b_frames"], "yes" if timing["b_frames"] else "none")
    if "keyframe_gap" in timing:
        gap = timing["keyframe_gap"]
        add("keyframes", gap <= MAX_KEYFRAME_SECONDS, f"{'at least ' if timing['keyframe_gap_at_least'] else ''}{gap:g} s")
    if summary.get("bitrate_kbps"):
        add("bitrate", summary["bitrate_kbps"] <= MAX_BITRATE_KBPS, f"{summary['bitrate_kbps'] / 1000:.1f} Mbps")
    if video.get("height"):
        add("resolution", video["height"] <= MAX_HEIGHT, f"{video.get('width')}x{video['height']}")
    if video.get("fps"):
        add("fps", video["fps"] <= MAX_FPS, f"{video['fps']:g} fps")
    add("audio", bool(summary.get("audio")), (summary.get("audio") or {}).get("codec", "none"))
    return items


async def watch(live, probes: probe.ProbeCache) -> None:
    """Check every live team's newest complete segment every CHECK_SECONDS, into live.settings
    (team path -> {checked_at, login, ok, checks}). A team's last result stays after it stops."""
    checked: dict[str, str] = {}  # team path -> segment checked last
    while True:
        for path, info in list(live.teams.items()):
            segs = recordings.segments(config.RECORDINGS_DIR / path)
            if len(segs) < 2 or checked.get(path) == str(segs[-2].path):
                continue  # the newest segment is still being written; the one before it is complete
            segment = segs[-2].path
            try:
                items = check(await probes.summary(segment), await video_timing(segment))
            except Exception:
                log.exception("Couldn't check the stream settings of %s", path)
                continue
            checked[path] = str(segment)
            live.settings[path] = {"checked_at": time.time(), "login": info.get("login"),
                                   "ok": all(item["ok"] for item in items), "checks": items}
        await asyncio.sleep(CHECK_SECONDS)
