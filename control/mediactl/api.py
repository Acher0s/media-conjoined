"""The HTTP API the Discord bot uses. Every request needs `Authorization: Bearer <CONTROL_API_TOKEN>`."""
import hmac
import json
import logging
import re
import shutil
import time

from aiohttp import web

from . import config, db, probe, recordings, roster, schedule, twitch

log = logging.getLogger(__name__)

MATCH_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def bearer_auth(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not config.API_TOKEN or not hmac.compare_digest(token.encode(), config.API_TOKEN.encode()):
        return _error(401, "Unauthorized")
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (roster.RosterError, ValueError) as e:
        return _error(400, str(e))
    except Exception:
        log.exception("Error handling %s %s", request.method, request.path)
        return _error(500, "Internal error")


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Invalid JSON body")
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _read_status(name: str) -> dict:
    try:
        return json.loads((config.STATUS_DIR / f"{name}.json").read_text())
    except (OSError, ValueError):
        return {}


def _delay(conn) -> tuple[float, float | None]:
    return db.get_delay(conn, config.DEFAULT_DELAY_MINUTES)


def _team_path(value: str) -> str:
    if value not in config.TEAM_PATHS:
        raise ValueError(f"Unknown team {value!r} (expected team01..team{config.TEAM_COUNT:02d})")
    return value


def make_app(conn, live) -> web.Application:
    routes = web.RouteTableDef()
    probes = probe.ProbeCache()

    @routes.get("/health")
    async def health(request):
        return web.json_response({"ok": True, "mediamtx": live.ok})

    # -- logins ------------------------------------------------------------------

    @routes.post("/export")
    async def export(request):
        body = await _json(request)
        teams, casters = body.get("teams", []), body.get("casters", [])
        if not isinstance(teams, list) or not isinstance(casters, list):
            raise ValueError("'teams' and 'casters' must be lists")
        created, revoked = roster.sync(conn, teams, casters)
        log.info("Export: %d login(s) created, %d revoked", len(created), len(revoked))
        return web.json_response({**roster.export(conn), "created": created, "revoked": revoked})

    @routes.post("/logins/{login}/reset")
    async def reset(request):
        login = request.match_info["login"]
        row = db.get_login(conn, login)
        if row is None or not row["active"]:
            return _error(404, f"No active login {login!r}")
        db.reset_password(conn, login)
        log.info("Password reset for %s", login)
        return web.json_response(roster.describe(db.get_login(conn, login), db.team_names(conn)))

    # -- status ------------------------------------------------------------------

    @routes.get("/status")
    async def status(request):
        names = db.team_names(conn)
        logins = db.active_logins(conn)
        by_login = {r["login"]: r for r in logins}
        passthrough = twitch.status(conn)
        teams = []
        for path in config.TEAM_PATHS:
            info = live.teams.get(path)
            entry = {"path": path, "name": names.get(path), "live": info is not None}
            if info:
                row = by_login.get(info.get("login") or "")
                entry.update(login=info.get("login"), discord_id=row["discord_id"] if row else None,
                             player_name=row["name"] if row else None, since=info["since"],
                             bitrate_kbps=info.get("bitrate_kbps"), video=info.get("video"), audio=info.get("audio"))
            refused = db.last_refusal(conn, path)
            if refused is not None:
                row = by_login.get(refused["login"])
                entry["last_refused"] = {"login": refused["login"], "at": refused["at"],
                                         "discord_id": row["discord_id"] if row else None,
                                         "name": row["name"] if row else None}
            if path in passthrough:
                entry["twitch"] = passthrough[path]
            teams.append(entry)
        minutes, _ = _delay(conn)
        now = time.time()
        return web.json_response({
            "mediamtx_ok": live.ok,
            "updated_at": live.updated_at,
            "teams": teams,
            "logins": [{"login": r["login"], "kind": r["kind"], "team_path": r["team_path"],
                        "discord_id": r["discord_id"], "name": r["name"], "verified": r["verified_at"] is not None,
                        "verified_at": r["verified_at"]} for r in logins],
            "slots": {slot: db.assignment_at(conn, slot, now) for slot in config.SLOTS},
            "delay_minutes": minutes,
            "feeds": _read_status("delay"),
        })

    @routes.get("/teams/{path}")
    async def inspect(request):
        path = _team_path(request.match_info["path"])
        info = live.teams.get(path)
        result = {"path": path, "name": db.team_names(conn).get(path), "live": info is not None}
        if path in (passthrough := twitch.status(conn)):
            result["twitch"] = passthrough[path]
        if info:
            row = db.get_login(conn, info.get("login") or "")
            result.update(login=info.get("login"), player_name=row["name"] if row else None,
                          discord_id=row["discord_id"] if row else None, since=info["since"],
                          bitrate_kbps=info.get("bitrate_kbps"), video=info.get("video"), audio=info.get("audio"))
            segs = recordings.segments(config.RECORDINGS_DIR / path)
            if len(segs) >= 2:  # the newest one is still being written
                result["segment"] = await probes.summary(segs[-2].path)
        return web.json_response(result)

    @routes.post("/teams/{path}/kick")
    async def kick(request):
        path = _team_path(request.match_info["path"])
        source = live.source(path)
        if source is None:
            return _error(409, f"Nobody is publishing on {path}")
        login = live.publisher_login(path)
        if not await live.mtx.kick(source):
            return _error(502, "MediaMTX refused the kick")
        log.info("Kicked %s from %s", login, path)
        return web.json_response({"kicked": True, "path": path, "login": login})

    @routes.put("/teams/{path}/twitch")
    async def set_twitch(request):
        """Twitch passthrough on ({"channel": name or twitch.tv link}) or off ({"channel": null})."""
        path = _team_path(request.match_info["path"])
        value = (await _json(request)).get("channel")
        channel = twitch.parse_channel(value) if value else None
        db.set_twitch(conn, path, channel, "bot", time.time())
        log.info("Twitch passthrough for %s: %s (bot)", path, channel or "off")
        return web.json_response({"path": path, "channel": channel})

    # -- slots -------------------------------------------------------------------

    @routes.get("/slots")
    async def get_slots(request):
        now = time.time()
        return web.json_response({slot: db.assignment_at(conn, slot, now) for slot in config.SLOTS})

    @routes.put("/slots")
    async def put_slots(request):
        body = await _json(request)
        mapping = {}
        for slot, team in body.items():
            if slot not in config.SLOTS:
                raise ValueError(f"Unknown slot {slot!r}")
            mapping[slot] = None if team is None else _team_path(team)
        now = time.time()
        db.assign_slots(conn, mapping, now)
        log.info("Slots assigned: %s", mapping)
        return web.json_response({slot: db.assignment_at(conn, slot, now) for slot in config.SLOTS})

    @routes.get("/lineup")
    async def get_lineup(request):
        minutes, _ = _delay(conn)
        return web.json_response(schedule.lineup(conn, time.time(), minutes))

    # -- delay -------------------------------------------------------------------

    def _minutes(body: dict) -> float:
        try:
            minutes = float(body.get("minutes"))
        except (TypeError, ValueError):
            raise ValueError("'minutes' must be a number")
        if not config.MIN_DELAY_MINUTES <= minutes <= config.MAX_DELAY_MINUTES:
            raise ValueError(f"Delay must be between {config.MIN_DELAY_MINUTES:g} and {config.MAX_DELAY_MINUTES:g} minutes")
        return minutes

    @routes.get("/delay")
    async def get_delay(request):
        minutes, changed_at = _delay(conn)
        return web.json_response({"minutes": minutes, "changed_at": changed_at})

    @routes.post("/delay/preview")
    async def preview_delay(request):
        minutes = _minutes(await _json(request))
        current, _ = _delay(conn)
        return web.json_response(schedule.preview(conn, time.time(), current, minutes))

    @routes.put("/delay")
    async def put_delay(request):
        minutes = _minutes(await _json(request))
        current, _ = _delay(conn)
        now = time.time()
        effect = schedule.preview(conn, now, current, minutes)
        db.set_delay(conn, minutes, now)
        log.info("Delay changed from %g to %g minutes", current, minutes)
        return web.json_response({"minutes": minutes, "changed_at": now, "effect": effect})

    # -- matches and archive -----------------------------------------------------

    @routes.post("/matches")
    async def post_match(request):
        body = await _json(request)
        match_id = str(body.get("match_id", ""))
        if not MATCH_ID_RE.match(match_id):
            raise ValueError("'match_id' must be 1-80 letters, digits, '.', '_' or '-'")
        teams = body.get("teams")
        if not isinstance(teams, list) or not teams:
            raise ValueError("'teams' must be a non-empty list of team paths")
        for team in teams:
            _team_path(team)
        try:
            start, end = float(body["start"]), float(body["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("'start' and 'end' must be UNIX timestamps")
        if end <= start:
            raise ValueError("'end' must be after 'start'")
        manifest = {**body, "match_id": match_id, "start": start, "end": end, "teams": teams,
                    "names": {t: db.team_names(conn).get(t) for t in teams}}
        now = time.time()
        db.save_match(conn, match_id, manifest, now)
        config.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.MANIFESTS_DIR / f".{match_id}.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(config.MANIFESTS_DIR / f"{match_id}.json")
        log.info("Match manifest %s written (%s, %.0fs)", match_id, ", ".join(teams), end - start)
        return web.json_response({"saved": True, "match_id": match_id})

    @routes.get("/archive")
    async def archive(request):
        usage = shutil.disk_usage(config.RECORDINGS_DIR)
        return web.json_response({
            "local": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
            **_read_status("archiver"),
        })

    @routes.get("/vods")
    async def vods(request):
        team, match = request.query.get("team"), request.query.get("match")
        files = _read_status("archiver").get("remote", {}).get("vods", [])
        if team:
            files = [f for f in files if f.get("team") == team or f.get("team_name") == team]
        if match:
            files = [f for f in files if str(f.get("match_id", "")).startswith(match)]
        return web.json_response({"vods": files})

    app = web.Application(middlewares=[bearer_auth], client_max_size=1024 * 1024)
    app.add_routes(routes)
    return app
