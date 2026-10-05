"""Public pages: the players' "go live" page at /go/ (also at /go/<team path>, as earlier links had it).

Served on CONTROL_PAGES_PORT, which the reverse proxy maps to /go/ on the same site as MediaMTX's
pages (so the page's WHIP requests to /<team path>/whip go to MediaMTX). Nothing here is secret
or needs a login to load: the player's login comes from the link's #fragment, which browsers never
send to a server, or is typed in, and MediaMTX checks it with the auth hook like any publish.
"""
from pathlib import Path

from aiohttp import web

from . import config

STATIC = Path(__file__).parent / "static"
HEADERS = {
    "Cache-Control": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
}


def make_app() -> web.Application:
    async def publish_page(request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path")
        if path is not None and path not in config.TEAM_PATHS:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / "publish.html", headers=HEADERS)

    async def to_page(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/go/")

    async def publish_script(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "publish.js", headers=HEADERS)

    app = web.Application()
    app.router.add_get("/go", to_page)
    app.router.add_get("/go/", publish_page)
    app.router.add_get("/go/publish.js", publish_script)
    app.router.add_get("/go/{path}", publish_page)
    return app
