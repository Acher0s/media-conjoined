"""Turning the bot's roster into logins, and logins into the links players and casters get."""
import time
from urllib.parse import quote

from . import config, db


class RosterError(ValueError):
    """The roster sent by the bot is invalid. The message is returned to the bot."""


def _free_index(taken: set[int]) -> int:
    n = 1
    while n in taken:
        n += 1
    return n


def sync(conn, teams: list[dict], casters: list[dict]) -> tuple[list[str], list[str]]:
    """Make the logins match the roster. Returns (created, revoked) login names.

    teams:   [{"path": "team05", "name": "Jimbo Squad", "players": [{"discord_id": "123", "name": "alice"}]}]
    casters: [{"discord_id": "456", "name": "anna"}]

    A player keeps their login (and password) as long as they stay on the same team. A player who
    leaves (or moves team) loses theirs; a new player gets a new login with a fresh password.
    """
    seen_paths = set()
    for team in teams:
        path = team.get("path")
        if path not in config.TEAM_PATHS:
            raise RosterError(f"Unknown team path {path!r} (expected team01..team{config.TEAM_COUNT:02d})")
        if path in seen_paths:
            raise RosterError(f"Team path {path} appears twice")
        seen_paths.add(path)

    created, revoked = [], []
    conn.execute("BEGIN")
    try:
        current = db.active_logins(conn)
        # Players: keyed by (team path, discord id)
        wanted_players = {(t["path"], str(p["discord_id"])): (t, p) for t in teams for p in t.get("players", [])}
        for team in teams:
            db.set_team_name(conn, team["path"], str(team.get("name") or team["path"]))
        have_players = {(r["team_path"], r["discord_id"]): r for r in current if r["kind"] == "player"}
        for key, row in have_players.items():
            if key not in wanted_players:
                db.deactivate_login(conn, row["login"])
                revoked.append(row["login"])
        for key, (team, player) in wanted_players.items():
            row = have_players.get(key)
            if row is not None:
                db.rename_login_holder(conn, row["login"], str(player.get("name") or key[1]))
                continue
            path = team["path"]
            taken = {int(r["login"].rsplit("-p", 1)[1])
                     for r in db.active_logins(conn) if r["kind"] == "player" and r["team_path"] == path}
            login = f"{path}-p{_free_index(taken)}"
            db.create_login(conn, login, "player", path, key[1], str(player.get("name") or key[1]))
            created.append(login)

        # Casters: keyed by discord id
        wanted_casters = {str(c["discord_id"]): c for c in casters}
        have_casters = {r["discord_id"]: r for r in current if r["kind"] == "caster"}
        for discord_id, row in have_casters.items():
            if discord_id not in wanted_casters:
                db.deactivate_login(conn, row["login"])
                revoked.append(row["login"])
        for discord_id, caster in wanted_casters.items():
            row = have_casters.get(discord_id)
            if row is not None:
                db.rename_login_holder(conn, row["login"], str(caster.get("name") or discord_id))
                continue
            taken = {int(r["login"].rsplit("-", 1)[1]) for r in db.active_logins(conn) if r["kind"] == "caster"}
            login = f"caster-{_free_index(taken):02d}"
            db.create_login(conn, login, "caster", None, discord_id, str(caster.get("name") or discord_id))
            created.append(login)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return created, revoked


# -- links -----------------------------------------------------------------------

def _srt_url(mode: str, path: str, login: str, password: str) -> str:
    url = f"srt://{config.SRT_HOST}:{config.SRT_PORT}?streamid={mode}:{path}:{login}:{password}"
    if config.SRT_PASSPHRASE:
        url += f"&passphrase={quote(config.SRT_PASSPHRASE)}"
    return url


def player_links(row) -> dict:
    path = row["team_path"]
    return {
        # The go-live page (pages.py), with the login filled in: the part after '#' never leaves the
        # player's browser. The same page without it (/go/) works for everyone.
        "browser_url": f"{config.BROWSER_BASE}/go/#{quote(row['login'], safe='')}:"
                       f"{quote(row['password'], safe='')}",
        "srt_url": _srt_url("publish", path, row["login"], row["password"]),
    }


def caster_feeds(row) -> list[dict]:
    return [{
        "slot": slot,
        "path": slot + config.DELAYED_SUFFIX,
        "browser_url": f"{config.BROWSER_BASE}/{slot}{config.DELAYED_SUFFIX}",
        "srt_url": _srt_url("read", slot + config.DELAYED_SUFFIX, row["login"], row["password"]),
    } for slot in config.SLOTS]


def describe(row, teams: dict[str, str]) -> dict:
    """Everything the export (and a reset) returns for one login. Contains the password."""
    entry = {"login": row["login"], "kind": row["kind"], "discord_id": row["discord_id"], "name": row["name"],
             "password": row["password"], "verified": row["verified_at"] is not None}
    if row["kind"] == "player":
        entry.update(team_path=row["team_path"], team_name=teams.get(row["team_path"], row["team_path"]),
                     **player_links(row))
    else:
        entry["feeds"] = caster_feeds(row)
    return entry


def export(conn) -> dict:
    teams = db.team_names(conn)
    rows = db.active_logins(conn)
    return {
        "generated_at": time.time(),
        "players": [describe(r, teams) for r in rows if r["kind"] == "player"],
        "casters": [describe(r, teams) for r in rows if r["kind"] == "caster"],
    }
