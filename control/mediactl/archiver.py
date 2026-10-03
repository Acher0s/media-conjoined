"""Copies recordings (and match manifests) to node 1 and prunes the SSD ring buffer.

Every ARCHIVE_INTERVAL_SECONDS:
  1. rsync RECORDINGS_DIR to ARCHIVE_TARGET over SSH (skipped when ARCHIVE_TARGET is empty);
  2. ask node 1 for its free space and the stitcher's status file (which lists the VODs);
  3. delete local segments older than delay + LOCAL_RETAIN_MARGIN_MINUTES that have been copied,
     or older than LOCAL_RETAIN_MAX_HOURS when nothing is copied (safety net);
  4. write the archive status for the control server (`!stream archive` / `!stream vods`).

Segments are pruned only after a copy that started after they were last written succeeded, so
nothing is deleted before it reached node 1. Footage that never made it is counted as lost.
"""
import asyncio
import json
import logging
import os
import shlex
import shutil
import time

from . import config, db, recordings

log = logging.getLogger("mediactl.archiver")

# ssh refuses a private key readable by others, which is how a mounted key often looks (e.g. on
# Docker Desktop, or copied without chmod), so the archiver uses a private copy of it
PRIVATE_KEY = "/tmp/archive_key"


def _prepare_key() -> None:
    shutil.copyfile(config.ARCHIVE_SSH_KEY, PRIVATE_KEY)
    os.chmod(PRIVATE_KEY, 0o600)


def _ssh_base() -> list[str]:
    known_hosts = config.STATUS_DIR.parent / "known_hosts"
    return ["ssh", "-i", PRIVATE_KEY, "-p", str(config.ARCHIVE_SSH_PORT), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=10"]


def _split_target() -> tuple[str, str]:
    """'user@host:/dir' -> ('user@host', '/dir')"""
    host, _, directory = config.ARCHIVE_TARGET.partition(":")
    return host, directory.rstrip("/") or "."


async def _run(*cmd: str, timeout: float = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timed out"
    return proc.returncode, out.decode(errors="replace")


async def copy_once() -> tuple[bool, str]:
    host, directory = _split_target()
    code, out = await _run("rsync", "-a", "--partial", "--exclude", ".*",
                           "-e", " ".join(shlex.quote(p) for p in _ssh_base()),
                           f"{config.RECORDINGS_DIR}/", f"{host}:{directory}/recordings/")
    return code == 0, out.strip()[-500:]


async def remote_info() -> dict:
    host, directory = _split_target()
    script = (f"df -B1 --output=size,avail {shlex.quote(directory)} | tail -n 1; "
              f"cat {shlex.quote(directory)}/stitch-status.json 2>/dev/null || echo '{{}}'")
    code, out = await _run(*_ssh_base(), host, script, timeout=30)
    if code != 0:
        return {"error": out.strip()[-300:]}
    first, _, rest = out.partition("\n")
    info = {}
    try:
        size, avail = (int(x) for x in first.split())
        info.update(total_bytes=size, free_bytes=avail)
    except ValueError:
        pass
    try:
        stitched = json.loads(rest or "{}")
        info.update(last_stitched=stitched.get("last_stitched"), vods=stitched.get("vods", []),
                    stitch_errors=stitched.get("errors", []))
    except ValueError:
        pass
    return info


def prune(conn, copied_before: float | None, now: float) -> tuple[int, int]:
    """Delete local segments that are no longer needed. Returns (deleted, lost)."""
    delay = db.get_delay(conn, config.DEFAULT_DELAY_MINUTES)[0] * 60
    keep_after = now - delay - config.LOCAL_RETAIN_MARGIN_MINUTES * 60
    hard_limit = now - config.LOCAL_RETAIN_MAX_HOURS * 3600
    deleted = lost = 0
    for path in config.TEAM_PATHS:
        for seg in recordings.segments(config.RECORDINGS_DIR / path):
            if seg.end >= keep_after:
                break
            copied = copied_before is not None and seg.end < copied_before
            if copied or seg.end < hard_limit:
                try:
                    seg.path.unlink()
                    deleted += 1
                    if not copied and config.ARCHIVE_TARGET:
                        lost += 1
                except FileNotFoundError:
                    pass
    return deleted, lost


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect(config.DB_PATH, readonly=True)
    config.STATUS_DIR.mkdir(parents=True, exist_ok=True)
    status = {"enabled": bool(config.ARCHIVE_TARGET), "lost_segments": 0}
    status_file = config.STATUS_DIR / "archiver.json"
    copied_before = None
    if not config.ARCHIVE_TARGET:
        log.warning("ARCHIVE_TARGET is empty: recordings stay local (pruned after %gh)", config.LOCAL_RETAIN_MAX_HOURS)
    else:
        _prepare_key()
    while True:
        started = time.time()
        if config.ARCHIVE_TARGET:
            ok, out = await copy_once()
            if ok:
                copied_before = started
                status.update(last_copy_ok=started, copy_seconds=round(time.time() - started, 1), last_error=None)
            else:
                log.warning("Copy to node 1 failed: %s", out)
                status.update(last_error=out, last_error_at=time.time())
            status["copy_lag_seconds"] = round(time.time() - copied_before) if copied_before else None
            status["remote"] = await remote_info()
        deleted, lost = prune(conn, copied_before, time.time())
        status["lost_segments"] += lost
        status.update(updated_at=time.time(), pruned_last_cycle=deleted)
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        tmp.replace(status_file)
        await asyncio.sleep(max(1.0, config.ARCHIVE_INTERVAL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    asyncio.run(main())
