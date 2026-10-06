"""Runs the bot API (CONTROL_API_PORT), MediaMTX's auth hook (CONTROL_AUTH_PORT) and the players'
go-live page (CONTROL_PAGES_PORT), polls MediaMTX, and checks the live streams' settings."""
import asyncio
import logging

import aiohttp
from aiohttp import web

from . import api, auth, config, db, pages, probe, settings
from .live import LiveState, MediaMTX

log = logging.getLogger("mediactl")


def make_auth_app(conn, live) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400)
        allowed = auth.decide(conn, live, payload if isinstance(payload, dict) else {})
        return web.Response(status=200 if allowed else 401)

    app = web.Application()
    app.router.add_post("/mediamtx/auth", handle)
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.API_TOKEN:
        raise SystemExit("CONTROL_API_TOKEN is not set")
    if not config.DELAY_PASSWORD:
        raise SystemExit("DELAY_PASSWORD is not set")
    conn = db.connect(config.DB_PATH)
    async with aiohttp.ClientSession() as session:
        live = LiveState(conn, MediaMTX(session))
        runners = []
        apps = ((api.make_app(conn, live), config.API_PORT), (make_auth_app(conn, live), config.AUTH_PORT),
                (pages.make_app(conn, live), config.PAGES_PORT))
        for app, port in apps:
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            runners.append(runner)
        log.info("Bot API on :%d, MediaMTX auth hook on :%d, go-live pages on :%d",
                 config.API_PORT, config.AUTH_PORT, config.PAGES_PORT)
        checker = asyncio.create_task(settings.watch(live, probe.ProbeCache()))
        try:
            await live.run()
        finally:
            checker.cancel()
            for runner in runners:
                await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
