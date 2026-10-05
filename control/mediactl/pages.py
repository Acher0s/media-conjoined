"""Public pages: the players' "go live" page at /go/ (also at /go/<team path>, as earlier links had it).

Served on CONTROL_PAGES_PORT, which the reverse proxy maps to /go/ on the same site as MediaMTX's
pages (so the page's WHIP requests to /<team path>/whip go to MediaMTX). Nothing here is secret
or needs a login to load: the player's login comes from the link's #fragment, which browsers never
send to a server, or is typed in, and MediaMTX checks it with the auth hook like any publish.
/go/delay tells the page the current delay, which players streaming to Twitch must match, and
/go/twitch lets a player (with their login, as HTTP Basic auth) turn Twitch passthrough on or off
for their team, /go/me tells them what the tournament knows about them (team, tested or not, Twitch)
and /go/preview starts (POST, again every 30 s while watching) or stops (DELETE) their team's test
feed (teamNN-preview); at most PREVIEW_MAX run at once.
"""
import base64
import binascii
import hmac
import logging
import time
from pathlib import Path

from aiohttp import web

from . import config, db, twitch

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
HEADERS = {
    "Cache-Control": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
}


def _player(conn, request: web.Request):
    """The active player login in the request's Basic auth header, or None."""
    header = request.headers.get("Authorization", "")
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":") if header.startswith("Basic ") \
            else ("", "", "")
    except (binascii.Error, UnicodeDecodeError):
        return None
    row = db.get_login(conn, user.strip().lower())
    if not row or not row["active"] or row["kind"] != "player" or \
            not hmac.compare_digest(password.encode(), row["password"].encode()):
        return None
    return row


def _twitch_state(conn, path: str) -> dict:
    setting = twitch.status(conn).get(path, {})
    return {"team": path, "channel": setting.get("channel"), "live": setting.get("live")}


def make_app(conn) -> web.Application:
    async def publish_page(request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path")
        if path is not None and path not in config.TEAM_PATHS:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / "publish.html", headers=HEADERS)

    async def to_page(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/go/")

    async def publish_script(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "publish.js", headers=HEADERS)

    async def delay(request: web.Request) -> web.Response:
        minutes, _ = db.get_delay(conn, config.DEFAULT_DELAY_MINUTES)
        return web.json_response({"minutes": minutes}, headers={"Cache-Control": "no-store"})

    async def twitch_get(request: web.Request) -> web.Response:
        row = _player(conn, request)
        if row is None:
            return web.json_response({"error": "Wrong login or password."}, status=401)
        return web.json_response(_twitch_state(conn, row["team_path"]), headers={"Cache-Control": "no-store"})

    async def twitch_set(request: web.Request) -> web.Response:
        row = _player(conn, request)
        if row is None:
            return web.json_response({"error": "Wrong login or password."}, status=401)
        try:
            value = (await request.json()).get("channel")
            channel = twitch.parse_channel(value) if value else None
        except (ValueError, AttributeError) as e:
            return web.json_response({"error": str(e) or "Invalid request."}, status=400)
        db.set_twitch(conn, row["team_path"], channel, row["login"], time.time())
        log.info("Twitch passthrough for %s: %s (%s)", row["team_path"], channel or "off", row["login"])
        return web.json_response(_twitch_state(conn, row["team_path"]))

    async def me(request: web.Request) -> web.Response:
        row = _player(conn, request)
        if row is None:
            return web.json_response({"error": "Wrong login or password."}, status=401)
        return web.json_response({"login": row["login"], "name": row["name"], "team": row["team_path"],
                                  "team_name": db.team_names(conn).get(row["team_path"]),
                                  "tested": row["verified_at"] is not None, "tested_at": row["verified_at"],
                                  "twitch": _twitch_state(conn, row["team_path"]),
                                  "delay_minutes": db.get_delay(conn, config.DEFAULT_DELAY_MINUTES)[0]},
                                 headers={"Cache-Control": "no-store"})

    async def preview(request: web.Request) -> web.Response:
        row = _player(conn, request)
        if row is None:
            return web.json_response({"error": "Wrong login or password."}, status=401)
        now = time.time()
        running = db.active_previews(conn, now)
        if row["team_path"] not in running and len(running) >= config.PREVIEW_MAX:
            return web.json_response({"error": "Lots of players are testing right now. Try again in a minute."},
                                     status=429)
        db.request_preview(conn, row["team_path"], now + config.PREVIEW_MINUTES * 60)
        return web.json_response({"path": row["team_path"] + config.PREVIEW_SUFFIX})

    async def preview_stop(request: web.Request) -> web.Response:
        row = _player(conn, request)
        if row is None:
            return web.json_response({"error": "Wrong login or password."}, status=401)
        db.stop_preview(conn, row["team_path"])
        return web.json_response({"stopped": True})

    app = web.Application()
    app.router.add_get("/go", to_page)
    app.router.add_get("/go/", publish_page)
    app.router.add_get("/go/publish.js", publish_script)
    app.router.add_get("/go/delay", delay)
    app.router.add_get("/go/twitch", twitch_get)
    app.router.add_post("/go/twitch", twitch_set)
    app.router.add_get("/go/me", me)
    app.router.add_post("/go/preview", preview)
    app.router.add_delete("/go/preview", preview_stop)
    app.router.add_get("/go/{path}", publish_page)
    return app
