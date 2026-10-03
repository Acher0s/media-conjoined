"""Delayed feeds: one player per slot, publishing `sNtM-delayed` from the team recordings.

Feed time = now - delay. A slot shows the team assigned to it at feed time (slot history in the
DB). Because the feeds run behind live, everything a slot will play is already on disk, so a
player plays the recorded segments back at real-time speed:

  ffmpeg (concat the segments, no pacing) -> MPEG-TS pipe -> ffmpeg (-re, video copy,
  audio to Opus) -> RTSP -> MediaMTX

(Pacing the concat input directly makes ffmpeg fall behind real time at every file boundary, so
the work is split over two processes.) Each "run" covers continuous footage of one team: it ends
at a gap in the recordings, at the next slot assignment, or at the newest complete segment, and
the next run starts right where it left off. MediaMTX keeps casters connected between runs and
shows the slate whenever nothing is published (alwaysAvailable in mediamtx.yml).

Delay changes: a longer delay holds (slate) until feed time catches up with where the feed was;
a shorter one jumps forward. Videos must be H.264 (all readers can play it); other codecs show the
slate and are reported as unsupported in the status file.
"""
import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path

from . import config, db, probe, recordings

log = logging.getLogger("mediactl.delay")

POLL_SECONDS = 1.0
# Jump forward instead of continuing when the feed is this far behind where it should be
SKIP_TOLERANCE = 5.0
# A segment counts as complete once nothing has been written to it for this long
SETTLE_SECONDS = 3.0


class SlotPlayer:
    def __init__(self, slot: str, conn, probes: probe.ProbeCache, status: dict):
        self.slot = slot
        self.conn = conn
        self.probes = probes
        self.status = status
        self.position: float | None = None  # content time the feed has played up to
        self._procs: list[asyncio.subprocess.Process] = []

    def _set_status(self, state: str, **extra) -> None:
        self.status[self.slot] = {"state": state, "updated_at": time.time(), **extra}

    def _delay(self) -> float:
        return db.get_delay(self.conn, config.DEFAULT_DELAY_MINUTES)[0] * 60

    async def run(self) -> None:
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                await self._stop()
                raise
            except Exception:
                log.exception("[%s] player error", self.slot)
                await self._stop()
                await asyncio.sleep(5)

    async def _step(self) -> None:
        now = time.time()
        delay = self._delay()
        target = now - delay  # feed time
        if self.position is not None and target < self.position - 0.5:
            # The delay got longer: hold on the slate until feed time reaches where we were
            self._set_status("holding", resume_at=self.position + delay)
            await asyncio.sleep(min(POLL_SECONDS, self.position - target))
            return
        start = target if self.position is None or target > self.position + SKIP_TOLERANCE else self.position
        team = db.assignment_at(self.conn, self.slot, start)
        if team is None:
            self.position = None
            self._set_status("idle")
            await asyncio.sleep(POLL_SECONDS)
            return

        segs = recordings.segments(config.RECORDINGS_DIR / team)
        i = self._first_segment(segs, start)
        if i is None:  # no footage at feed time: slate
            self._set_status("no_footage", team=team)
            await asyncio.sleep(POLL_SECONDS)
            return

        stop_at = db.next_assignment_change(self.conn, self.slot, start)
        run = recordings.contiguous_from(segs, i, stop_at=stop_at, complete_before=now - SETTLE_SECONDS)
        if not run:
            await asyncio.sleep(POLL_SECONDS)
            return

        info = await self.probes.summary(run[0].path)
        video = (info.get("video") or {}).get("codec")
        if video != "h264":
            self._set_status("unsupported", team=team, video_codec=video)
            self.position = run[0].end  # skip it rather than retrying the same segment
            await asyncio.sleep(POLL_SECONDS)
            return

        inpoint = max(0.0, start - run[0].start)
        await self._play(team, run, inpoint, has_audio="audio" in info, delay=delay)

    @staticmethod
    def _first_segment(segs: list[recordings.Segment], t: float) -> int | None:
        """The segment containing t, or one starting just after it (continuing across a tiny gap)."""
        for i, seg in enumerate(segs):
            if seg.end > t and seg.start <= t + recordings.GAP_TOLERANCE:
                return i
            if seg.start > t + recordings.GAP_TOLERANCE:
                return None
        return None

    async def _play(self, team: str, run: list[recordings.Segment], inpoint: float, has_audio: bool,
                    delay: float) -> None:
        content_start = run[0].start + inpoint
        content_end = run[-1].end
        with tempfile.NamedTemporaryFile("w", suffix=".ffconcat", delete=False) as f:
            f.write("ffconcat version 1.0\n")
            for n, seg in enumerate(run):
                f.write(f"file '{seg.path}'\n")
                if n == 0 and inpoint > 0:
                    f.write(f"inpoint {inpoint:.3f}\n")
            listfile = f.name

        url = f"{config.MEDIAMTX_RTSP}/{self.slot}{config.DELAYED_SUFFIX}"
        url = url.replace("rtsp://", f"rtsp://{config.DELAY_USER}:{config.DELAY_PASSWORD}@", 1)
        reader_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                      "-f", "concat", "-safe", "0", "-i", listfile,
                      "-map", "0:v:0"] + (["-map", "0:a:0"] if has_audio else []) + \
                     ["-c", "copy", "-f", "mpegts", "pipe:1"]
        publisher_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                         "-re", "-f", "mpegts", "-i", "pipe:0"]
        if has_audio:
            publisher_cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        else:  # MediaMTX expects H264 + Opus on the delayed feeds: add silence
            publisher_cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0",
                              "-shortest"]
        publisher_cmd += ["-c:v", "copy", "-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                          "-f", "rtsp", "-rtsp_transport", "tcp", url]

        read_fd, write_fd = os.pipe()
        try:
            reader = await asyncio.create_subprocess_exec(*reader_cmd, stdout=write_fd)
            publisher = await asyncio.create_subprocess_exec(*publisher_cmd, stdin=read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self._procs = [reader, publisher]
        wall_start = time.time()
        log.info("[%s] playing %s from %s (%d segment(s), %.0fs)", self.slot, team,
                 time.strftime("%H:%M:%S", time.gmtime(content_start)), len(run), content_end - content_start)
        try:
            while publisher.returncode is None:
                self.position = content_start + (time.time() - wall_start)
                self._set_status("playing", team=team, content_time=self.position,
                                 behind_seconds=round(time.time() - self.position, 1))
                if abs(self._delay() - delay) > 0.5:
                    log.info("[%s] delay changed, restarting", self.slot)
                    break
                try:
                    await asyncio.wait_for(publisher.wait(), POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
            else:
                if publisher.returncode == 0:
                    self.position = content_end  # played the whole run: continue right after it
                else:
                    log.warning("[%s] publisher exited with %s", self.slot, publisher.returncode)
                    await asyncio.sleep(1)
        finally:
            await self._stop()
            Path(listfile).unlink(missing_ok=True)

    async def _stop(self) -> None:
        for proc in self._procs:
            if proc.returncode is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for proc in self._procs:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        self._procs = []


async def write_status(status: dict) -> None:
    config.STATUS_DIR.mkdir(parents=True, exist_ok=True)
    target = config.STATUS_DIR / "delay.json"
    while True:
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        tmp.replace(target)
        await asyncio.sleep(2)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DELAY_PASSWORD:
        raise SystemExit("DELAY_PASSWORD is not set")
    conn = db.connect(config.DB_PATH)
    probes = probe.ProbeCache()
    status: dict = {}
    players = [SlotPlayer(slot, conn, probes, status) for slot in config.SLOTS]
    log.info("Delay players for %d slots", len(players))
    await asyncio.gather(write_status(status), *(p.run() for p in players))


if __name__ == "__main__":
    asyncio.run(main())
