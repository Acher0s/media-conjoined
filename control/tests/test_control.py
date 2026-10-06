"""Unit tests for the control service logic that doesn't need MediaMTX or ffmpeg.

Run from control/:  python -m pytest tests
"""
import asyncio
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
    assert p["team_name"] == "Jimbo" and p["browser_url"].endswith(f"/go/#team05-p1:{p['password']}")
    assert f"streamid=publish:team05:team05-p1:{p['password']}" in p["srt_url"]
    c = data["casters"][0]
    assert len(c["feeds"]) == len(config.SLOTS) and c["feeds"][0]["path"] == "s1t1-delayed"
    assert "streamid=read:s1t1-delayed:caster-01:" in c["feeds"][0]["srt_url"]


# -- auth -------------------------------------------------------------------------

class FakeLive:
    def __init__(self, publishers=None):
        self.publishers = publishers or {}
        self.teams = {path: {"login": login} for path, login in self.publishers.items()}
        self.settings = {}

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


# -- go-live page -----------------------------------------------------------------

def test_go_live_page_served_for_team_paths_only(conn):
    from aiohttp.test_utils import TestClient, TestServer
    from mediactl import pages

    async def check():
        async with TestClient(TestServer(pages.make_app(conn, FakeLive()))) as client:
            for url in ("/go/", "/go/team05"):
                r = await client.get(url)
                assert r.status == 200 and "text/html" in r.headers["Content-Type"]
            r = await client.get("/go", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/go/"
            r = await client.get("/go/")
            assert '<script src="/go/publish.js">' in await r.text() and r.headers["X-Frame-Options"] == "DENY"
            r = await client.get("/go/publish.js")
            assert r.status == 200 and "javascript" in r.headers["Content-Type"]
            assert (await client.get("/go/team99")).status == 404
            assert await (await client.get("/go/delay")).json() == {"minutes": config.DEFAULT_DELAY_MINUTES}
            db.set_delay(conn, 45, time.time())
            assert await (await client.get("/go/delay")).json() == {"minutes": 45}
            assert (await client.get("/go/s1t1-delayed")).status == 404

    asyncio.run(check())


# -- test feeds and Twitch passthrough --------------------------------------------

def test_players_watch_only_their_own_teams_test_feed(conn):
    roster.sync(conn, [team("team05", "A", ("1", "alice")), team("team06", "B", ("2", "bob"))],
                [{"discord_id": "9", "name": "anna"}])
    pw = lambda login: db.get_login(conn, login)["password"]
    read = lambda u, p, path: {"action": "read", "user": u, "password": p, "path": path}
    assert auth.decide(conn, FakeLive(), read("team05-p1", pw("team05-p1"), "team05-preview"))
    assert not auth.decide(conn, FakeLive(), read("team05-p1", pw("team05-p1"), "team06-preview"))
    assert not auth.decide(conn, FakeLive(), read("team05-p1", "wrong", "team05-preview"))
    assert not auth.decide(conn, FakeLive(), read("caster-01", pw("caster-01"), "team05-preview"))
    delay = {"action": "publish", "user": config.DELAY_USER, "password": config.DELAY_PASSWORD, "path": "team05-preview"}
    assert auth.decide(conn, FakeLive(), delay)
    assert not auth.decide(conn, FakeLive(), {**delay, "user": "team05-p1", "password": pw("team05-p1")})


def test_twitch_channel_from_name_or_link():
    from mediactl import twitch
    for value in ("FalconPlays", "@falconplays", "twitch.tv/FalconPlays", "https://www.twitch.tv/falconplays/",
                  "https://m.twitch.tv/falconplays?ref=x"):
        assert twitch.parse_channel(value) == "falconplays"
    for bad in ("", "ab", "https://youtube.com/falcon", "falcon plays", "x" * 26):
        with pytest.raises(ValueError):
            twitch.parse_channel(bad)


def test_go_live_endpoints_for_players(conn):
    import base64
    from aiohttp.test_utils import TestClient, TestServer
    from mediactl import pages
    roster.sync(conn, [team("team05", "Falcon", ("1", "alice"))], [])
    pw = db.get_login(conn, "team05-p1")["password"]
    auth_header = {"Authorization": "Basic " + base64.b64encode(f"Team05-P1:{pw}".encode()).decode()}

    async def check():
        async with TestClient(TestServer(pages.make_app(conn, FakeLive()))) as client:
            assert (await client.get("/go/me")).status == 401
            bad = {"Authorization": "Basic " + base64.b64encode(b"team05-p1:nope").decode()}
            assert (await client.get("/go/me", headers=bad)).status == 401
            me = await (await client.get("/go/me", headers=auth_header)).json()
            assert me["team"] == "team05" and me["team_name"] == "Falcon" and me["tested"] is False
            assert me["team_tested"] == []
            roster.sync(conn, [team("team05", "Falcon", ("1", "alice"), ("2", "bob"))], [])
            db.mark_verified(conn, "team05-p2", 123.0)  # bob tested: the team is covered, alice isn't tested
            me = await (await client.get("/go/me", headers=auth_header)).json()
            assert me["tested"] is False and me["team_tested"] == [{"login": "team05-p2", "name": "bob",
                                                                   "tested_at": 123.0}]
            assert me["twitch"]["channel"] is None

            r = await client.post("/go/twitch", headers=auth_header, json={"channel": "twitch.tv/FalconPlays"})
            assert (await r.json())["channel"] == "falconplays" and db.get_twitch(conn, "team05") == "falconplays"
            r = await client.post("/go/twitch", headers=auth_header, json={"channel": "not a channel!"})
            assert r.status == 400 and db.get_twitch(conn, "team05") == "falconplays"
            await client.post("/go/twitch", headers=auth_header, json={"channel": None})
            assert db.get_twitch(conn, "team05") is None

            r = await client.post("/go/preview", headers=auth_header)
            assert (await r.json())["path"] == "team05-preview"
            assert db.preview_active(conn, "team05", time.time())
            assert not db.preview_active(conn, "team05", time.time() + config.PREVIEW_MINUTES * 60 + 1)
            assert not db.preview_active(conn, "team06", time.time())
            await client.delete("/go/preview", headers=auth_header)
            assert not db.preview_active(conn, "team05", time.time())

            # At most PREVIEW_MAX test feeds at once; a team whose feed runs may keep asking
            for n in range(1, config.PREVIEW_MAX + 1):
                db.request_preview(conn, f"team{n + 5:02d}", time.time() + 60)
            assert (await client.post("/go/preview", headers=auth_header)).status == 429
            db.request_preview(conn, "team05", time.time() + 60)
            db.stop_preview(conn, "team06")
            assert (await client.post("/go/preview", headers=auth_header)).status == 200

    asyncio.run(check())


# -- stream settings checks ---------------------------------------------------------

def test_settings_checklist():
    from mediactl import settings
    good = {"video": {"codec": "h264", "width": 1920, "height": 1080, "fps": 60}, "bitrate_kbps": 6000,
            "audio": {"codec": "aac"}}
    items = settings.check(good, {"b_frames": False, "keyframe_gap": 2.0, "keyframe_gap_at_least": False})
    assert all(item["ok"] for item in items) and len(items) == 7

    # OBS defaults gone wrong: B-frames, auto keyframes (one keyframe in an 8 s segment), 10 Mbps, 1440p, no sound
    bad = {"video": {"codec": "h264", "width": 2560, "height": 1440, "fps": 60}, "bitrate_kbps": 10000}
    items = {i["key"]: i for i in settings.check(bad, {"b_frames": True, "keyframe_gap": 8.3,
                                                       "keyframe_gap_at_least": True})}
    assert [k for k, i in items.items() if not i["ok"]] == ["bframes", "keyframes", "bitrate", "resolution", "audio"]
    assert items["keyframes"]["value"] == "at least 8.3 s" and items["bitrate"]["value"] == "10.0 Mbps"
    assert all(key in settings.FIXES for key in items)
    hevc = settings.check({"video": {"codec": "hevc"}}, {})
    assert hevc[0] == {"key": "codec", "ok": False, "value": "HEVC"}


def test_refusals_are_logged_with_a_reason_once_a_minute(conn, caplog):
    import logging
    roster.sync(conn, [team("team05", "A", ("1", "alice"))], [{"discord_id": "9", "name": "anna"}])
    read = {"action": "read", "user": "caster-01", "password": "wrong", "path": "s1t2-delayed",
            "protocol": "srt", "ip": "203.0.113.7"}
    with caplog.at_level(logging.INFO, logger="mediactl.auth"):
        for n in range(3):  # OBS retrying every 2 s
            assert not auth.decide(conn, FakeLive(), read, now=1000 + 2 * n)
        assert not auth.decide(conn, FakeLive(), {**read, "user": ""}, now=1001)  # no login: debug only
        assert not auth.decide(conn, FakeLive(), read, now=1070)
    lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(lines) == 2 and "wrong" not in lines[0].split("Refused")[0]
    assert lines[0] == ("Refused caster-01 read s1t2-delayed: wrong password (reset or re-exported since?) "
                        "[srt from 203.0.113.7]")
    assert lines[1].endswith("(and 2 more time(s) since the last report)")
    assert auth._reason(conn, FakeLive(), "read", "team05", "caster-01", "x", {}, 0) == "live team paths can't be read"
    pw = db.get_login(conn, "team05-p1")["password"]
    assert auth._reason(conn, FakeLive(), "publish", "team06", "team05-p1", pw, {}, 0) == \
        "this login belongs to team05"
