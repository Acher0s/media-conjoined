"""What's live right now, from MediaMTX's API, polled every few seconds.

For each team path: online or not, which login is publishing (matched through the connection id
MediaMTX sends to the auth hook, which is the same as the path's source id in the API), since
when, resolution, codecs and an inbound bitrate. A login counts as *verified* (has streamed
successfully) once it has been live for VERIFY_SECONDS.
"""
import asyncio
import logging
import time

import aiohttp

from . import config, db

log = logging.getLogger(__name__)

VERIFY_SECONDS = 10

# MediaMTX source types -> the API collection their kick endpoint lives in
KICK_COLLECTIONS = {
    "srtConn": "srtconns",
    "webRTCSession": "webrtcsessions",
    "rtspSession": "rtspsessions",
    "rtspsSession": "rtspssessions",
    "rtmpConn": "rtmpconns",
    "rtmpsConn": "rtmpsconns",
}


class MediaMTX:
    def __init__(self, session: aiohttp.ClientSession, base: str = config.MEDIAMTX_API):
        self.session = session
        self.base = base

    async def paths(self) -> list[dict]:
        async with self.session.get(f"{self.base}/v3/paths/list", params={"itemsPerPage": "1000"},
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
            resp.raise_for_status()
            return (await resp.json()).get("items", [])

    async def kick(self, source: dict) -> bool:
        collection = KICK_COLLECTIONS.get(source.get("type"))
        if not collection or not source.get("id"):
            return False
        async with self.session.post(f"{self.base}/v3/{collection}/kick/{source['id']}",
                                     timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status < 300


def _video_props(path_item: dict) -> dict:
    tracks = path_item.get("tracks2") or []
    video = next((t for t in tracks if t.get("codec") in ("H264", "H265", "VP8", "VP9", "AV1")), None)
    audio = next((t for t in tracks if t.get("codec") not in ("H264", "H265", "VP8", "VP9", "AV1")), None)
    result = {}
    if video:
        props = video.get("codecProps") or {}
        result["video"] = {"codec": video["codec"], "width": props.get("width"), "height": props.get("height")}
    if audio:
        props = audio.get("codecProps") or {}
        result["audio"] = {"codec": audio.get("codec"), "sample_rate": props.get("sampleRate"),
                           "channels": props.get("channelCount")}
    return result


class LiveState:
    def __init__(self, conn, mtx: MediaMTX):
        self.conn = conn
        self.mtx = mtx
        self.ok = False  # last poll succeeded
        self.updated_at: float | None = None
        self.teams: dict[str, dict] = {}  # team path -> live info (only while online)
        self.delayed: dict[str, dict] = {}  # delayed path -> {"publishing": bool, "readers": n}
        self._bytes: dict[str, tuple[float, int]] = {}

    def publisher_login(self, path: str) -> str | None:
        info = self.teams.get(path)
        return info.get("login") if info else None

    def source(self, path: str) -> dict | None:
        info = self.teams.get(path)
        return info.get("source") if info else None

    async def poll_once(self) -> None:
        try:
            items = await self.mtx.paths()
        except Exception as e:  # MediaMTX down or restarting: keep the last state, mark it stale
            if self.ok:
                log.warning("MediaMTX API unreachable: %r", e)
            self.ok = False
            return
        now = time.time()
        teams, delayed = {}, {}
        for item in items:
            name = item.get("name", "")
            if name in config.TEAM_PATHS:
                source = item.get("source")
                if not (item.get("online", item.get("ready")) and source):
                    self._bytes.pop(name, None)
                    continue
                login = db.session_login(self.conn, source.get("id", ""))
                since = self.teams.get(name, {}).get("since")
                if since is None or self.teams[name].get("source", {}).get("id") != source.get("id"):
                    since = now
                inbound = int(item.get("inboundBytes", item.get("bytesReceived", 0)) or 0)
                prev = self._bytes.get(name)
                bitrate = None
                if prev and now > prev[0] and inbound >= prev[1]:
                    bitrate = round((inbound - prev[1]) * 8 / (now - prev[0]) / 1000)
                elif name in self.teams:
                    bitrate = self.teams[name].get("bitrate_kbps")
                self._bytes[name] = (now, inbound)
                teams[name] = {"source": source, "login": login, "since": since, "bitrate_kbps": bitrate,
                               **_video_props(item)}
                if login and now - since >= VERIFY_SECONDS:
                    db.mark_verified(self.conn, login, now)
            elif config.DELAYED_PATH_RE.match(name):
                delayed[name] = {"publishing": bool(item.get("online") and item.get("source")),
                                 "readers": len(item.get("readers") or [])}
        self.teams, self.delayed = teams, delayed
        self.ok, self.updated_at = True, now

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(config.STATUS_POLL_SECONDS)
