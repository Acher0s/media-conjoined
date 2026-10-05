"""MediaMTX's auth hook: MediaMTX asks this for every publish and read attempt (authMethod: http).

  * players may publish only to their own team path, one teammate at a time: if a teammate is
    already live, the attempt is refused and recorded so the bot's panel can explain it. The same
    login reconnecting (e.g. after a drop) is allowed and replaces its old connection.
  * casters may read only the delayed feeds.
  * a team's players may read their own team's test feed (teamNN-preview), to check their setup.
  * the internal delay players publish the delayed feeds and the test feeds.
Anything else is refused.
"""
import hmac
import logging
import time

from . import config, db

log = logging.getLogger(__name__)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def decide(conn, live, payload: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    action = payload.get("action")
    path = payload.get("path") or ""
    user = payload.get("user") or ""
    password = payload.get("password") or ""
    delayed = config.DELAYED_PATH_RE.match(path)
    preview = config.PREVIEW_PATH_RE.match(path)

    if action == "publish":
        if delayed or preview:
            return bool(config.DELAY_PASSWORD) and user == config.DELAY_USER and _same(password, config.DELAY_PASSWORD)
        if path not in config.TEAM_PATHS:
            return False
        row = db.get_login(conn, user)
        if not row or not row["active"] or row["kind"] != "player" or row["team_path"] != path:
            return False
        if not _same(password, row["password"]):
            return False
        current = live.publisher_login(path)
        if current and current != user:
            db.record_refusal(conn, path, user, "teammate_live", now)
            log.info("Refused %s publishing on %s: %s is live", user, path, current)
            return False
        db.record_session(conn, payload.get("id") or "", path, user, now)
        return True

    if action == "read":
        if preview:
            row = db.get_login(conn, user)
            return bool(row and row["active"] and row["kind"] == "player" and row["team_path"] == preview.group(1)
                        and _same(password, row["password"]))
        if not delayed:
            return False  # nobody reads live team paths from outside
        row = db.get_login(conn, user)
        return bool(row and row["active"] and row["kind"] == "caster" and _same(password, row["password"]))

    return False
