"""Delayed feeds: one player per slot, publishing `sNtM-delayed` from the team recordings.

Feed time = now - delay. A slot shows the team assigned to it at feed time (slot history in the
DB). Because the feeds run behind live, everything a slot will play is already on disk, so a
player plays the recorded segments back at real-time speed:

  ffmpeg per run (concat the segments, no pacing) -> buffer -> ffmpeg (-re, video copy,
  audio to Opus) -> RTSP -> MediaMTX

(Pacing the concat input directly makes ffmpeg fall behind real time at every file boundary, so
the work is split over two processes.) A "run" is the footage of one team that's on disk and
complete; while it plays, more gets recorded, so the next run follows. One publisher plays run
after run for as long as the footage continues: each run's reader starts while the previous one is
still playing (a bounded buffer, BUFFER_BYTES, keeps it ahead) and its timestamps continue where
the previous one's ended, so the switch is seamless. Restarting the publisher per run made the
feed stall briefly at every switch. The publisher only stops at a real break: a gap in the
recordings, the next slot assignment, Twitch passthrough, or a delay change. MediaMTX keeps casters
connected meanwhile and shows the slate whenever nothing is published (alwaysAvailable in
mediamtx.yml).

Twitch passthrough: when the team on the slot at feed time has a Twitch channel set (twitch.py),
the player relays that channel live instead (streamlink -> ffmpeg, audio to Opus), because the
player's own Twitch delay already puts it at feed time. It stops when the slot changes team or the
setting changes; while the channel is offline (or showing an ad break) the slate shows.

Test feeds: a PreviewPlayer per team plays that team into `teamNN-preview` the same way, while the
team's go-live page asks for it (db.request_preview), so players can watch their own setup exactly as
the casters would get it. Test feeds are watched in the browser, which (WebRTC) can't play H.264 with
B-frames, OBS's default and most Twitch streams'; so they re-encode the video (at most 720p30,
2 Mbps, no B-frames). Slot feeds copy it: casters watching in OBS over SRT get B-frames fine.

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

from . import config, db, probe, recordings, twitch

log = logging.getLogger("mediactl.delay")

POLL_SECONDS = 1.0
# Jump forward instead of continuing when the feed is this far behind where it should be
SKIP_TOLERANCE = 5.0
# A segment counts as complete once nothing has been written to it for this long
SETTLE_SECONDS = 3.0
# How long to wait before trying an offline Twitch channel again
TWITCH_RETRY_SECONDS = 15.0
# Start this many Twitch segments (about 2 s each) behind live: a cushion against late segments
TWITCH_LIVE_EDGE = 5
# How far the next run's reader may get ahead of the publisher (~10 s of a 6 Mbps stream)
BUFFER_BYTES = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024


# Re-encoding for the test feeds: plays in any browser, cheap enough for several at a time
BROWSER_VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-bf", "0", "-g", "60",
                 "-b:v", "2M", "-maxrate", "2M", "-bufsize", "4M", "-pix_fmt", "yuv420p",
                 "-vf", "scale=-2:'min(720,ih)',fps=30"]


class SlotPlayer:
    video_codec = ["-c:v", "copy"]

    def __init__(self, slot: str, conn, probes: probe.ProbeCache, status: dict):
        self.slot = slot
        self.conn = conn
        self.probes = probes
        self.status = status
        self.position: float | None = None  # content time the feed has played up to
        self._fed_until: float | None = None  # content time read into the current publisher so far
        self._procs: list[asyncio.subprocess.Process] = []

    def _set_status(self, state: str, **extra) -> None:
        self.status[self.slot] = {"state": state, "updated_at": time.time(), **extra}

    # A slot feed follows the slot assignments (PreviewPlayer overrides these three)
    @property
    def output(self) -> str:
        return f"{self.slot}{config.DELAYED_SUFFIX}"

    def _team_at(self, at: float) -> str | None:
        return db.assignment_at(self.conn, self.slot, at)

    def _next_change(self, after: float) -> float | None:
        return db.next_assignment_change(self.conn, self.slot, after)

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
        team = self._team_at(start)
        if team is None:
            self.position = None
            self._set_status("idle")
            await asyncio.sleep(POLL_SECONDS)
            return
        channel = db.get_twitch(self.conn, team)
        if channel is not None:
            self.position = None  # back on the recording, continue from feed time
            await self._relay(team, channel)
            return

        segs = recordings.segments(config.RECORDINGS_DIR / team)
        i = self._first_segment(segs, start)
        if i is None:  # no footage at feed time: slate
            self._set_status("no_footage", team=team)
            await asyncio.sleep(POLL_SECONDS)
            return

        stop_at = self._next_change(start)
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
        """Play the team's footage from `run` on, run after run, through one publisher (see the module
        docstring), until the footage stops continuing or the delay changes."""
        session_start = run[0].start + inpoint
        publisher = await asyncio.create_subprocess_exec(*self._publisher_cmd(has_audio), stdin=asyncio.subprocess.PIPE)
        self._procs = [publisher]
        self._fed_until = session_start
        buffer: asyncio.Queue = asyncio.Queue(maxsize=BUFFER_BYTES // READ_CHUNK)
        feeder = asyncio.create_task(self._feed(team, run, session_start, has_audio, buffer))
        writer = asyncio.create_task(self._write(buffer, publisher))
        wall_start = time.time()
        log.info("[%s] playing %s from %s", self.slot, team, time.strftime("%H:%M:%S", time.gmtime(session_start)))
        try:
            while publisher.returncode is None:
                self.position = session_start + (time.time() - wall_start)
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
                    self.position = self._fed_until  # played everything: continue right after it
                else:
                    log.warning("[%s] publisher exited with %s", self.slot, publisher.returncode)
                    await asyncio.sleep(1)
        finally:
            for task in (feeder, writer):
                task.cancel()
            await asyncio.gather(feeder, writer, return_exceptions=True)
            await self._stop()

    def _publisher_cmd(self, has_audio: bool) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-re", "-f", "mpegts", "-i", "pipe:0"]
        if has_audio:
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        else:  # MediaMTX expects H264 + Opus on the delayed feeds: add silence
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        return cmd + [*self.video_codec, "-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                      "-f", "rtsp", "-rtsp_transport", "tcp", self._publish_url()]

    async def _feed(self, team: str, run: list[recordings.Segment], session_start: float, has_audio: bool,
                    buffer: asyncio.Queue) -> None:
        """Read run after run into the buffer, until the footage stops continuing."""
        start = session_start
        try:
            while run:
                await self._read_run(team, run, start, start - session_start, has_audio, buffer)
                self._fed_until = run[-1].end
                waited = 0.0
                while (following := self._next_run(team, run)) == [] and (not buffer.empty() or waited < 3):
                    await asyncio.sleep(0.5)  # recorded but not complete yet: it will be
                    waited += 0.5
                run = following
                start = run[0].start if run else 0.0
        finally:
            try:
                buffer.put_nowait(None)  # end of the stream: the publisher plays what's left, then exits
            except asyncio.QueueFull:
                pass

    def _next_run(self, team: str, previous: list[recordings.Segment]) -> list[recordings.Segment] | None:
        """The run right after `previous`; [] if it isn't complete yet, None if the footage doesn't continue
        (a gap, the slot's next team, Twitch passthrough)."""
        end = previous[-1].end
        if self._team_at(end) != team or db.get_twitch(self.conn, team) is not None:
            return None
        segs = recordings.segments(config.RECORDINGS_DIR / team)
        i = next((k for k, seg in enumerate(segs) if seg.start > previous[-1].start), None)
        if i is None:
            return []  # the next segment isn't even started yet
        if segs[i].start - end > recordings.GAP_TOLERANCE:
            return None
        return recordings.contiguous_from(segs, i, stop_at=self._next_change(end),
                                          complete_before=time.time() - SETTLE_SECONDS)

    async def _read_run(self, team: str, run: list[recordings.Segment], start: float, offset: float,
                        has_audio: bool, buffer: asyncio.Queue) -> None:
        """One run of segments as MPEG-TS into the buffer, its timestamps shifted to follow the previous runs."""
        with tempfile.NamedTemporaryFile("w", suffix=".ffconcat", delete=False) as f:
            f.write(recordings.concat_list(run, start))
            listfile = f.name
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listfile,
               "-map", "0:v:0", *(["-map", "0:a:0"] if has_audio else []), "-c", "copy",
               "-output_ts_offset", f"{offset:.6f}", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"]
        reader = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        log.debug("[%s] reading %s from %s (%d segment(s), %.0fs)", self.slot, team,
                  time.strftime("%H:%M:%S", time.gmtime(start)), len(run), run[-1].end - start)
        try:
            while chunk := await reader.stdout.read(READ_CHUNK):
                await buffer.put(chunk)
            await reader.wait()
        finally:
            if reader.returncode is None:
                reader.kill()
            # communicate() reads the pipe to its end: wait() alone also waits for the pipe to close,
            # which a full pipe nobody reads never does (the player would hang here)
            try:
                await asyncio.wait_for(reader.communicate(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            Path(listfile).unlink(missing_ok=True)

    @staticmethod
    async def _write(buffer: asyncio.Queue, publisher: asyncio.subprocess.Process) -> None:
        try:
            while (chunk := await buffer.get()) is not None:
                publisher.stdin.write(chunk)
                await publisher.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                publisher.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass

    def _publish_url(self) -> str:
        url = f"{config.MEDIAMTX_RTSP}/{self.output}"
        return url.replace("rtsp://", f"rtsp://{config.DELAY_USER}:{config.DELAY_PASSWORD}@", 1)

    async def _relay(self, team: str, channel: str) -> None:
        """Twitch passthrough: relay the team's live Twitch stream until the slot or the setting changes."""
        reader_cmd = ["streamlink", "--stdout", "--twitch-disable-ads", "--retry-open", "2",
                      "--hls-live-edge", str(TWITCH_LIVE_EDGE), twitch.url(channel), "best"]
        # Twitch arrives in bursts of a few seconds (HLS segments); -re sends it on at playback speed, or
        # browsers would show it stuttering. Starting TWITCH_LIVE_EDGE segments back gives the cushion.
        publisher_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-re", "-f", "mpegts", "-i", "pipe:0",
                         "-map", "0:v:0", "-map", "0:a:0?", *self.video_codec, "-c:a", "libopus", "-b:a", "96k",
                         "-ar", "48000", "-ac", "2", "-f", "rtsp", "-rtsp_transport", "tcp", self._publish_url()]
        read_fd, write_fd = os.pipe()
        try:
            reader = await asyncio.create_subprocess_exec(*reader_cmd, stdout=write_fd,
                                                          stderr=asyncio.subprocess.DEVNULL)
            publisher = await asyncio.create_subprocess_exec(*publisher_cmd, stdin=read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self._procs = [reader, publisher]
        log.info("[%s] relaying twitch.tv/%s for %s", self.slot, channel, team)
        started = time.time()
        try:
            while publisher.returncode is None:
                self._set_status("twitch", team=team, channel=channel, since=started)
                if self._team_at(time.time() - self._delay()) != team or \
                        db.get_twitch(self.conn, team) != channel:
                    log.info("[%s] slot or Twitch setting changed, stopping the relay", self.slot)
                    return
                try:
                    await asyncio.wait_for(publisher.wait(), POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop()
        # The stream ended or never started (channel offline): slate, then try again
        self._set_status("twitch_offline", team=team, channel=channel)
        await asyncio.sleep(TWITCH_RETRY_SECONDS)

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


class PreviewPlayer(SlotPlayer):
    """A team's test feed: plays that team (recording or Twitch passthrough) into teamNN-preview, the
    same way a slot feed would, while the team's go-live page keeps asking for it."""
    video_codec = BROWSER_VIDEO

    def __init__(self, team: str, conn, probes: probe.ProbeCache, status: dict):
        super().__init__(f"{team}{config.PREVIEW_SUFFIX}", conn, probes, status)
        self.team = team

    @property
    def output(self) -> str:
        return self.slot

    def _team_at(self, at: float) -> str | None:
        return self.team if db.preview_active(self.conn, self.team, time.time()) else None

    def _next_change(self, after: float) -> float | None:
        return None


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
    players = [SlotPlayer(slot, conn, probes, status) for slot in config.SLOTS] + \
              [PreviewPlayer(team, conn, probes, status) for team in config.TEAM_PATHS]
    log.info("Delay players for %d slots and %d test feeds", len(config.SLOTS), len(config.TEAM_PATHS))
    await asyncio.gather(write_status(status), twitch.watch(conn), *(p.run() for p in players))


if __name__ == "__main__":
    asyncio.run(main())
