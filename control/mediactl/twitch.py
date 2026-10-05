"""Twitch passthrough: instead of the team's recording, their feed shows their own Twitch stream.

Players streaming to Twitch must delay their stream by exactly the tournament delay (OBS's Stream
Delay), so their Twitch stream already is what the feed should show: the delay player relays it as it
comes in, without delaying it again (delay.py). Nothing of it is recorded. The delay service checks
every minute whether each channel is live (status file twitch.json) for the bot and the go-live page;
a channel seen live counts as a successful stream for the player who connected it.
"""
import asyncio
import json
import logging
import re
import time

from . import config, db

log = logging.getLogger(__name__)

CHECK_SECONDS = 60
CHANNEL_RE = re.compile(r"^[a-z0-9_]{3,25}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([a-z0-9_]{3,25})/?(?:[?#].*)?$")


def parse_channel(value: str) -> str:
    """A channel name or link (twitch.tv/name) -> the channel name. ValueError if it's neither."""
    value = (value or "").strip().lower()
    match = URL_RE.match(value)
    channel = match.group(1) if match else value.lstrip("@")
    if not CHANNEL_RE.match(channel):
        raise ValueError("That isn't a Twitch channel: use the channel name or its twitch.tv link")
    return channel


def url(channel: str) -> str:
    return f"https://www.twitch.tv/{channel}"


async def is_live(channel: str) -> bool | None:
    """True/False, or None when it couldn't be checked."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "streamlink", "--json", url(channel),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
        data = json.loads(out or b"{}")
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        log.warning("Couldn't check twitch.tv/%s: %r", channel, e)
        return None
    if data.get("streams"):
        return True
    return False if "No playable streams" in str(data.get("error", "")) else None


def status(conn) -> dict:
    """team path -> {channel, set_by, at, live, checked_at} for the teams with passthrough on. live is
    None until the channel has been checked (a check of a team's earlier channel doesn't count)."""
    try:
        checks = json.loads((config.STATUS_DIR / "twitch.json").read_text())
    except (OSError, ValueError):
        checks = {}
    result = {}
    for path, setting in db.twitch_settings(conn).items():
        check = checks.get(path, {})
        current = check.get("channel") == setting["channel"]
        result[path] = {**setting, "live": check.get("live") if current else None,
                        "checked_at": check.get("checked_at") if current else None}
    return result


async def watch(conn) -> None:
    """Keep STATUS_DIR/twitch.json up to date: team path -> {channel, live, checked_at}."""
    target = config.STATUS_DIR / "twitch.json"
    while True:
        result = {}
        for path, setting in db.twitch_settings(conn).items():
            live = await is_live(setting["channel"])
            result[path] = {"channel": setting["channel"], "live": live, "checked_at": time.time()}
            if live and setting["set_by"] != "bot":
                db.mark_verified(conn, setting["set_by"], time.time())
        config.STATUS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        tmp.replace(target)
        await asyncio.sleep(CHECK_SECONDS)
