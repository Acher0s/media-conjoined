"""MediaMTX's auth hook: MediaMTX asks this for every publish and read attempt (authMethod: http).

  * players may publish only to their own team path, one teammate at a time: if a teammate is
    already live, the attempt is refused and recorded so the bot's panel can explain it. The same
    login reconnecting (e.g. after a drop) is allowed and replaces its old connection.
  * casters may read only the delayed feeds.
  * a team's players may read their own team's test feed (teamNN-preview), to check their setup.
  * the internal delay players publish the delayed feeds and the test feeds.
Anything else is refused. Every refusal is logged with its reason (login, path, protocol and IP,
never the password), the same one at most once per LOG_EVERY_SECONDS: OBS retries every 2 s.
"""
import hmac
import logging
import time

from . import config, db

log = logging.getLogger(__name__)

LOG_EVERY_SECONDS = 60
_last_logged: dict[tuple, list] = {}  # (user, path, action, reason) -> [logged at, repeats since]


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _check_login(conn, user: str, password: str, kind: str, team_path: str | None = None) -> str | None:
    """Why this login may not do it, or None if it may."""
    row = db.get_login(conn, user)
    if not row:
        return "unknown login"
    if not row["active"]:
        return "login revoked (removed from the roster)"
    if row["kind"] != kind:
        return f"a {row['kind']} login, this needs a {kind} login"
    if team_path is not None and row["team_path"] != team_path:
        return f"this login belongs to {row['team_path']}"
    if not _same(password, row["password"]):
        return "wrong password (reset or re-exported since?)"
    return None


def _reason(conn, live, action: str, path: str, user: str, password: str, payload: dict, now: float) -> str | None:
    """Why the attempt is refused, or None if it's allowed."""
    delayed = config.DELAYED_PATH_RE.match(path)
    preview = config.PREVIEW_PATH_RE.match(path)

    if action == "publish":
        if delayed or preview:
            if config.DELAY_PASSWORD and user == config.DELAY_USER and _same(password, config.DELAY_PASSWORD):
                return None
            return "only the delay service publishes feeds"
        if path not in config.TEAM_PATHS:
            return "not a team path"
        refused = _check_login(conn, user, password, "player", path)
        if refused:
            return refused
        current = live.publisher_login(path)
        if current and current != user:
            db.record_refusal(conn, path, user, "teammate_live", now)
            return f"teammate {current} is live"
        db.record_session(conn, payload.get("id") or "", path, user, now)
        return None

    if action == "read":
        if preview:
            return _check_login(conn, user, password, "player", preview.group(1))
        if delayed:
            return _check_login(conn, user, password, "caster")
        return "live team paths can't be read"
    return f"action {action!r} isn't allowed"


def decide(conn, live, payload: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    action = payload.get("action") or ""
    path = payload.get("path") or ""
    user = payload.get("user") or ""
    password = payload.get("password") or ""
    reason = _reason(conn, live, action, path, user, password, payload, now)
    if reason is not None:
        _log_refusal(user, path, action, reason, payload, now)
    return reason is None


def _log_refusal(user: str, path: str, action: str, reason: str, payload: dict, now: float) -> None:
    where = f"{payload.get('protocol') or '?'} from {payload.get('ip') or '?'}"
    if not user:  # browsers and players first try without a login, then ask for one: not worth a line
        log.debug("Refused %s %s without a login (%s)", action, path, where)
        return
    key = (user, path, action, reason)
    entry = _last_logged.get(key)
    if entry and now - entry[0] < LOG_EVERY_SECONDS:
        entry[1] += 1
        return
    repeats = f" (and {entry[1]} more time(s) since the last report)" if entry and entry[1] else ""
    log.info("Refused %s %s %s: %s [%s]%s", user, action, path, reason, where, repeats)
    _last_logged[key] = [now, 0]
