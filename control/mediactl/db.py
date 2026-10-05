"""SQLite state shared by the control services.

The server writes; the delay players and the archiver only read (WAL mode allows that while
the server writes). All times are UNIX timestamps in seconds (UTC).
"""
import json
import secrets
import sqlite3
import string
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    path TEXT PRIMARY KEY,          -- team01 .. team16
    name TEXT NOT NULL              -- display name from the bot
);
CREATE TABLE IF NOT EXISTS logins (
    login TEXT PRIMARY KEY,         -- team05-p1, caster-03
    kind TEXT NOT NULL,             -- 'player' or 'caster'
    team_path TEXT,                 -- players only
    discord_id TEXT NOT NULL,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    verified_at REAL                -- first time this login was live for a while
);
CREATE TABLE IF NOT EXISTS sessions (  -- MediaMTX connection id -> who opened it
    conn_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    login TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refusals (  -- publish attempts refused because a teammate was live
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    login TEXT NOT NULL,
    reason TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL,
    team_path TEXT,                 -- NULL = slot freed
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS slot_history_slot_at ON slot_history (slot, at);
CREATE TABLE IF NOT EXISTS delay_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minutes REAL NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    manifest TEXT NOT NULL,
    at REAL NOT NULL
);
"""


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        conn.executescript(SCHEMA)
    return conn


def new_password() -> str:
    # Letters and digits only: passwords end up in SRT stream ids (':' separated) and links, and in
    # the export CSV, where a leading '-' would make spreadsheets read the cell as a formula
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))


# -- teams and logins ------------------------------------------------------------

def set_team_name(conn, path: str, name: str) -> None:
    conn.execute("INSERT INTO teams (path, name) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET name = excluded.name",
                 (path, name))


def team_names(conn) -> dict[str, str]:
    return {r["path"]: r["name"] for r in conn.execute("SELECT path, name FROM teams")}


def get_login(conn, login: str):
    return conn.execute("SELECT * FROM logins WHERE login = ?", (login,)).fetchone()


def active_logins(conn) -> list:
    return conn.execute("SELECT * FROM logins WHERE active = 1 ORDER BY login").fetchall()


def create_login(conn, login: str, kind: str, team_path: str | None, discord_id: str, name: str) -> None:
    """Create (or bring back) a login with a fresh password."""
    conn.execute(
        "INSERT INTO logins (login, kind, team_path, discord_id, name, password, active, created_at, verified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL) "
        "ON CONFLICT(login) DO UPDATE SET kind = excluded.kind, team_path = excluded.team_path, "
        "discord_id = excluded.discord_id, name = excluded.name, password = excluded.password, active = 1, "
        "created_at = excluded.created_at, verified_at = NULL",
        (login, kind, team_path, discord_id, name, new_password(), time.time()))


def rename_login_holder(conn, login: str, name: str) -> None:
    conn.execute("UPDATE logins SET name = ? WHERE login = ?", (name, login))


def deactivate_login(conn, login: str) -> None:
    conn.execute("UPDATE logins SET active = 0 WHERE login = ?", (login,))


def reset_password(conn, login: str) -> None:
    conn.execute("UPDATE logins SET password = ? WHERE login = ?", (new_password(), login))


def mark_verified(conn, login: str, at: float) -> None:
    conn.execute("UPDATE logins SET verified_at = ? WHERE login = ? AND verified_at IS NULL", (at, login))


# -- sessions and refusals -------------------------------------------------------

def record_session(conn, conn_id: str, path: str, login: str, at: float) -> None:
    conn.execute("INSERT OR REPLACE INTO sessions (conn_id, path, login, at) VALUES (?, ?, ?, ?)",
                 (conn_id, path, login, at))


def session_login(conn, conn_id: str) -> str | None:
    row = conn.execute("SELECT login FROM sessions WHERE conn_id = ?", (conn_id,)).fetchone()
    return row["login"] if row else None


def record_refusal(conn, path: str, login: str, reason: str, at: float) -> None:
    conn.execute("INSERT INTO refusals (path, login, reason, at) VALUES (?, ?, ?, ?)", (path, login, reason, at))


def last_refusal(conn, path: str):
    return conn.execute("SELECT * FROM refusals WHERE path = ? ORDER BY at DESC LIMIT 1", (path,)).fetchone()


# -- slots -----------------------------------------------------------------------

def assign_slots(conn, mapping: dict[str, str | None], at: float) -> None:
    """Record new slot assignments (None frees the slot), all with the same timestamp."""
    conn.executemany("INSERT INTO slot_history (slot, team_path, at) VALUES (?, ?, ?)",
                     [(slot, team, at) for slot, team in mapping.items()])


def assignment_at(conn, slot: str, at: float) -> str | None:
    row = conn.execute("SELECT team_path FROM slot_history WHERE slot = ? AND at <= ? ORDER BY at DESC, id DESC "
                       "LIMIT 1", (slot, at)).fetchone()
    return row["team_path"] if row else None


def assignments_between(conn, slot: str, after: float, until: float) -> list:
    """Assignment changes for a slot with after < at <= until, oldest first."""
    return conn.execute("SELECT team_path, at FROM slot_history WHERE slot = ? AND at > ? AND at <= ? "
                        "ORDER BY at, id", (slot, after, until)).fetchall()


def next_assignment_change(conn, slot: str, after: float) -> float | None:
    row = conn.execute("SELECT at FROM slot_history WHERE slot = ? AND at > ? ORDER BY at, id LIMIT 1",
                       (slot, after)).fetchone()
    return row["at"] if row else None


# -- delay -----------------------------------------------------------------------

def get_delay(conn, default_minutes: float) -> tuple[float, float | None]:
    """(minutes, changed_at). changed_at is None while the default applies."""
    row = conn.execute("SELECT minutes, at FROM delay_log ORDER BY id DESC LIMIT 1").fetchone()
    return (row["minutes"], row["at"]) if row else (default_minutes, None)


def set_delay(conn, minutes: float, at: float) -> None:
    conn.execute("INSERT INTO delay_log (minutes, at) VALUES (?, ?)", (minutes, at))


# -- matches ---------------------------------------------------------------------

def save_match(conn, match_id: str, manifest: dict, at: float) -> None:
    conn.execute("INSERT OR REPLACE INTO matches (match_id, manifest, at) VALUES (?, ?, ?)",
                 (match_id, json.dumps(manifest), at))
