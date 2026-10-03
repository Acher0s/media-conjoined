"""ffprobe helpers: what's inside a recorded segment."""
import asyncio
import json
from pathlib import Path


async def probe(path: Path) -> dict | None:
    """Streams of a media file, or None if ffprobe can't read it."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def summarize(info: dict | None) -> dict:
    """Resolution, frame rate, codecs, bitrate and audio of a probed segment."""
    if not info:
        return {}
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    result = {}
    if video:
        fps = None
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
        if "/" in rate:
            num, den = rate.split("/", 1)
            if den not in ("", "0"):
                fps = round(int(num) / int(den), 2)
        result["video"] = {"codec": video.get("codec_name"), "width": video.get("width"),
                           "height": video.get("height"), "fps": fps}
    if audio:
        result["audio"] = {"codec": audio.get("codec_name"), "sample_rate": int(audio.get("sample_rate") or 0),
                           "channels": audio.get("channels")}
    fmt = info.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0)
        size = int(fmt.get("size") or 0)
        if duration > 0:
            result["bitrate_kbps"] = round(size * 8 / duration / 1000)
    except ValueError:
        pass
    return result


class ProbeCache:
    """Probes each segment file once (segments never change after they're complete)."""

    def __init__(self, size: int = 512):
        self._cache: dict[str, dict] = {}
        self._size = size

    async def summary(self, path: Path) -> dict:
        key = str(path)
        if key not in self._cache:
            if len(self._cache) >= self._size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = summarize(await probe(path))
        return self._cache[key]
