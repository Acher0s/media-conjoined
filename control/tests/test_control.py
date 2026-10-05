"""Unit tests for the control service logic that doesn't need MediaMTX or ffmpeg.

Run from control/:  python -m pytest tests
"""
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("DELAY_PASSWORD", "delaypw")

from mediactl import auth, config, db, recordings, roster, schedule  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "control.db")


def team(path, name, *players):
    return {"path": path, "name": name, "players": [{"discord_id": d, "name": n} for d, n in players]}


# -- roster -----------------------------------------------------------------------

def test_export_creates_stable_logins(conn):
    created, revoked = roster.sync(conn, [team("team05", "Jimbo", ("1", "alice"), ("2", "bob"))], [{"discord_id": "9", "name": "anna"}])
    assert sorted(created) == ["caster-01", "team05-p1", "team05-p2"] and revoked == []
    pw = db.get_login(conn, "team05-p1")["password"]
    # Re-export: same logins and passwords, nothing created
    created, revoked = roster.sync(conn, [team("team05", "Jimbo", ("1", "alice"), ("2", "bob"))], [{"discord_id": "9", "name": "anna"}])
    assert created == [] and revoked == []
    assert db.get_login(conn, "team05-p1")["password"] == pw


def test_removed_player_is_revoked_and_new_player_gets_free_slot(conn):
    roster.sync(conn, [team("team05", "Jimbo", ("1", "alice"), ("2", "bob"))], [])
    alice = db.get_login(conn, "team05-p1")
    created, revoked = roster.sync(conn, [team("team05", "Jimbo", ("2", "bob"), ("3", "carol"))], [])
    assert revoked == ["team05-p1"] and created == ["team05-p1"]
    carol = db.get_login(conn, "team05-p1")
    assert carol["discord_id"] == "3" and carol["password"] != alice["password"]


def test_player_moving_team_gets_new_login(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice"))], [])
    created, revoked = roster.sync(conn, [team("team06", "B", ("1", "alice"))], [])
    assert revoked == ["team05-p1"] and created == ["team06-p1"]


def test_bad_team_path_rejected(conn):
    with pytest.raises(roster.RosterError):
        roster.sync(conn, [team("team99", "X")], [])


def test_export_links(conn):
    roster.sync(conn, [team("team05", "Jimbo", ("1", "alice"))], [{"discord_id": "9", "name": "anna"}])
    data = roster.export(conn)
    p = data["players"][0]
    assert p["team_name"] == "Jimbo" and p["browser_url"].endswith("/team05/publish")
    assert f"streamid=publish:team05:team05-p1:{p['password']}" in p["srt_url"]
    c = data["casters"][0]
    assert len(c["feeds"]) == len(config.SLOTS) and c["feeds"][0]["path"] == "s1t1-delayed"
    assert "streamid=read:s1t1-delayed:caster-01:" in c["feeds"][0]["srt_url"]


# -- auth -------------------------------------------------------------------------

class FakeLive:
    def __init__(self, publishers=None):
        self.publishers = publishers or {}

    def publisher_login(self, path):
        return self.publishers.get(path)


def _pub(login, pw, path, conn_id="c1"):
    return {"action": "publish", "user": login, "password": pw, "path": path, "id": conn_id, "protocol": "srt"}


def test_player_publishes_only_to_own_team(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice")), team("team06", "B", ("2", "bob"))], [])
    pw = db.get_login(conn, "team05-p1")["password"]
    assert auth.decide(conn, FakeLive(), _pub("team05-p1", pw, "team05"))
    assert not auth.decide(conn, FakeLive(), _pub("team05-p1", pw, "team06"))
    assert not auth.decide(conn, FakeLive(), _pub("team05-p1", "wrong", "team05"))
    assert db.session_login(conn, "c1") == "team05-p1"


def test_teammate_refused_while_other_is_live_but_same_login_may_reconnect(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice"), ("2", "bob"))], [])
    pw1 = db.get_login(conn, "team05-p1")["password"]
    pw2 = db.get_login(conn, "team05-p2")["password"]
    live = FakeLive({"team05": "team05-p1"})
    assert not auth.decide(conn, live, _pub("team05-p2", pw2, "team05", "c2"))
    assert db.last_refusal(conn, "team05")["login"] == "team05-p2"
    assert auth.decide(conn, live, _pub("team05-p1", pw1, "team05", "c3"))


def test_revoked_login_refused(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice"))], [])
    pw = db.get_login(conn, "team05-p1")["password"]
    roster.sync(conn, [team("team05", "A")], [])
    assert not auth.decide(conn, FakeLive(), _pub("team05-p1", pw, "team05"))


def test_casters_read_delayed_only(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice"))], [{"discord_id": "9", "name": "anna"}])
    cpw = db.get_login(conn, "caster-01")["password"]
    ppw = db.get_login(conn, "team05-p1")["password"]
    read = lambda u, p, path: {"action": "read", "user": u, "password": p, "path": path}
    assert auth.decide(conn, FakeLive(), read("caster-01", cpw, "s3t2-delayed"))
    assert not auth.decide(conn, FakeLive(), read("caster-01", cpw, "team05"))
    assert not auth.decide(conn, FakeLive(), read("team05-p1", ppw, "s3t2-delayed"))
    assert not auth.decide(conn, FakeLive(), {"action": "publish", "user": "caster-01", "password": cpw, "path": "team05"})


def test_delay_player_publishes_delayed_feeds_only(conn):
    ok = {"action": "publish", "user": config.DELAY_USER, "password": config.DELAY_PASSWORD, "path": "s1t1-delayed"}
    assert auth.decide(conn, FakeLive(), ok)
    assert not auth.decide(conn, FakeLive(), {**ok, "password": "x"})
    assert not auth.decide(conn, FakeLive(), {**ok, "path": "team05"})


def test_other_actions_refused(conn):
    assert not auth.decide(conn, FakeLive(), {"action": "playback", "user": "x", "password": "y", "path": "team05"})


# -- schedule ---------------------------------------------------------------------

def test_lineup_answers_in_feed_time(conn):
    roster.sync(conn, [team("team05", "Falcon"), team("team06", "Otter"), team("team07", "Lynx")], [])
    t0 = 1_000_000.0
    db.assign_slots(conn, {"s1t1": "team05", "s1t2": "team06"}, t0)
    db.assign_slots(conn, {"s1t1": "team07"}, t0 + 20 * 60)  # next set, 20 min later
    lu = schedule.lineup(conn, t0 + 35 * 60, 30)  # feed time = t0 + 5 min
    s1t1 = next(s for s in lu["slots"] if s["slot"] == "s1t1")
    assert s1t1["team_name"] == "Falcon"
    assert s1t1["upcoming"][0]["team_name"] == "Lynx"
    assert s1t1["upcoming"][0]["reaches_feed_at"] == t0 + 50 * 60


def test_delay_preview(conn):
    roster.sync(conn, [team("team05", "Falcon")], [])
    now = 2_000_000.0
    db.assign_slots(conn, {"s2t1": "team05"}, now - 25 * 60)
    assert schedule.preview(conn, now, 30, 45) == {"current_minutes": 30, "new_minutes": 45, "effect": "hold",
                                                     "hold_minutes": 15}
    p = schedule.preview(conn, now, 30, 15)
    assert p["effect"] == "skip" and p["skip_from"] == now - 30 * 60 and p["skip_to"] == now - 15 * 60
    assert {"slot": "s2t1", "team": "team05", "team_name": "Falcon"} in p["skipped"]


# -- recordings -------------------------------------------------------------------

def _seg(d: Path, start: float, end: float) -> None:
    name = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(start)) + f"-{int((start % 1) * 1e6):06d}.mp4"
    f = d / name
    f.write_bytes(b"x")
    os.utime(f, (end, end))


def test_segments_and_gaps(tmp_path):
    base = 1_700_000_000.0
    for a, b in [(0, 4), (4, 8), (8, 12), (20, 24), (24, 28)]:
        _seg(tmp_path, base + a, base + b)
    segs = recordings.segments(tmp_path)
    assert [round(s.start - base) for s in segs] == [0, 4, 8, 20, 24]
    assert recordings.covering(segs, base + 5) == 1
    assert recordings.covering(segs, base + 15) is None  # in the gap
    run = recordings.contiguous_from(segs, 0)
    assert len(run) == 3  # stops at the gap
    run = recordings.contiguous_from(segs, 0, stop_at=base + 8)
    assert len(run) == 2  # stops before an assignment change
    assert len(recordings.overlapping(segs, base + 6, base + 22)) == 3


def test_concat_list_places_segments_by_their_start(tmp_path):
    base = 1_700_000_000.0
    for a, b in [(0, 3.8), (3.8, 8), (8, 12), (20, 24)]:
        _seg(tmp_path, base + a, base + b)
    segs = recordings.segments(tmp_path)
    lines = recordings.concat_list(segs, base + 1, base + 22).splitlines()
    durations = [float(x.split()[1]) for x in lines if x.startswith("duration")]
    # to the next start (minus the inpoint on the first), own length before the gap, none on the last
    assert durations == pytest.approx([2.8, 4.2, 4.0])
    assert [x for x in lines if x.startswith(("inpoint", "outpoint"))] == ["inpoint 1.000000", "outpoint 2.000000"]
