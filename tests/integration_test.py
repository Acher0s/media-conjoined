"""End-to-end test against a running stack (docker compose up) with DEFAULT_DELAY_MINUTES=0.5.

  python tests/integration_test.py
Uses docker to run fake publishers/readers on the compose network. Needs: aiohttp, docker.
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import aiohttp

API = os.environ.get("CONTROL_URL", "http://127.0.0.1:9000")
TOKEN = os.environ.get("CONTROL_API_TOKEN", "test-token-123")
NET = os.environ.get("COMPOSE_NETWORK", "media_default")
IMAGE = "bluenviron/mediamtx:1.21.1-ffmpeg"
H = {"Authorization": f"Bearer {TOKEN}"}
ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}
results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))


def docker(*args, check_rc=False):
    return subprocess.run(["docker", *args], capture_output=True, text=True, env=ENV, check=check_rc)


def publisher(name, login, password, path="team05", size="640x360"):
    docker("rm", "-f", name)
    docker("run", "-d", "--name", name, "--network", NET, "--entrypoint", "ffmpeg", IMAGE, "-hide_banner",
           "-loglevel", "error", "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30", "-f", "lavfi", "-i",
           "sine=frequency=440:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
           "-b:v", "1M", "-c:a", "aac", "-f", "mpegts",
           f"srt://mediamtx:8890?streamid=publish:{path}:{login}:{password}&pkt_size=1316")


def mtx_path(path):
    out = docker("exec", "media-mediamtx-1", "wget", "-qO-", f"http://localhost:9997/v3/paths/get/{path}").stdout
    return json.loads(out) if out.strip() else {}


async def main():
    async with aiohttp.ClientSession(headers=H) as s:
        async def call(method, path, body=None, expect=200):
            async with s.request(method, API + path, json=body) as r:
                data = await r.json(content_type=None)
                if r.status != expect:
                    print(f"   {method} {path} -> {r.status} {data}")
                return r.status, data

        st, _ = await call("GET", "/health")
        check("health", st == 200)
        async with aiohttp.ClientSession() as anon:
            async with anon.get(API + "/status", headers={"Authorization": "Bearer wrong"}) as r:
                check("wrong token refused (401)", r.status == 401)

        roster = {"teams": [{"path": "team05", "name": "Team Falcon", "players": [
            {"discord_id": "101", "name": "alice"}, {"discord_id": "102", "name": "bob"}]},
            {"path": "team06", "name": "Team Otter", "players": [{"discord_id": "201", "name": "carol"}]}],
            "casters": [{"discord_id": "901", "name": "anna"}]}
        st, exp = await call("POST", "/export", roster)
        logins = {p["login"]: p for p in exp["players"] + exp["casters"]}
        check("export creates 3 player + 1 caster logins", st == 200 and sorted(exp["created"]) ==
              ["caster-01", "team05-p1", "team05-p2", "team06-p1"], exp.get("created"))
        p1, p2, caster = logins["team05-p1"], logins["team05-p2"], logins["caster-01"]

        publisher("pub-alice", "team05-p1", p1["password"])
        await asyncio.sleep(6)
        publisher("pub-bob", "team05-p2", p2["password"])  # teammate while alice is live
        publisher("pub-wrong", "team05-p1", "badpassword", path="team06")
        await asyncio.sleep(9)
        st, status = await call("GET", "/status")
        t5 = next(t for t in status["teams"] if t["path"] == "team05")
        t6 = next(t for t in status["teams"] if t["path"] == "team06")
        check("team05 live as alice (team05-p1)", t5["live"] and t5.get("login") == "team05-p1" and
              t5.get("player_name") == "alice", t5)
        check("team05 reports resolution and bitrate", (t5.get("video") or {}).get("width") == 640 and
              (t5.get("bitrate_kbps") or 0) > 100, t5)
        check("bob refused while alice live, shown as last_refused",
              (t5.get("last_refused") or {}).get("login") == "team05-p2", t5.get("last_refused"))
        check("wrong team/password refused (team06 not live)", not t6["live"])
        verified = {l["login"]: l["verified"] for l in status["logins"]}
        check("alice verified after 10s live, bob not", verified.get("team05-p1") and not verified.get("team05-p2"),
              verified)
        check("MediaMTX status ok", status["mediamtx_ok"])

        st, insp = await call("GET", "/teams/team05")
        seg = insp.get("segment", {})
        check("inspect: probed segment (h264 640x360, 30fps, aac)",
              seg.get("video", {}).get("codec") == "h264" and seg.get("video", {}).get("width") == 640 and
              seg.get("video", {}).get("fps") == 30 and seg.get("audio", {}).get("codec") == "aac", seg)

        st, slots = await call("PUT", "/slots", {"s1t1": "team05", "s1t2": "team06"})
        check("slots assigned", slots.get("s1t1") == "team05" and slots.get("s1t2") == "team06", slots)
        st, lu = await call("GET", "/lineup")
        s1t1 = next(x for x in lu["slots"] if x["slot"] == "s1t1")
        check("lineup: s1t1 not on feed yet, Team Falcon upcoming",
              s1t1["team"] is None and s1t1["upcoming"] and s1t1["upcoming"][0]["team_name"] == "Team Falcon", s1t1)

        before = mtx_path("s1t1-delayed")
        check("delayed feed shows the slate before the delay passes (available, not online)",
              before.get("ready") and not before.get("online"), before)
        print("   waiting for the 30s delay...")
        await asyncio.sleep(40)
        after = mtx_path("s1t1-delayed")
        check("delayed feed live from team05's recording (H264 + Opus)", after.get("online") and
              after.get("tracks") == ["H264", "Opus"], {k: after.get(k) for k in ("online", "tracks", "source")})
        st, status = await call("GET", "/status")
        feed = status["feeds"].get("s1t1", {})
        check("delay player playing team05 ~30s behind", feed.get("state") == "playing" and feed.get("team") == "team05"
              and 25 <= (feed.get("behind_seconds") or 0) <= 40, feed)
        check("s1t2 (team06, never streamed) shows slate", status["feeds"].get("s1t2", {}).get("state") == "no_footage",
              status["feeds"].get("s1t2"))

        # A caster reads the delayed feed over SRT with their own login; a player can't
        probe = docker("run", "--rm", "--network", NET, "--entrypoint", "ffprobe", IMAGE, "-v", "error",
                       "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-rw_timeout", "8000000",
                       f"srt://mediamtx:8890?streamid=read:s1t1-delayed:caster-01:{caster['password']}")
        check("caster reads s1t1-delayed over SRT (h264 + opus)", "h264" in probe.stdout and "opus" in probe.stdout,
              probe.stdout + probe.stderr[-200:])
        probe = docker("run", "--rm", "--network", NET, "--entrypoint", "ffprobe", IMAGE, "-v", "error",
                       "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-rw_timeout", "5000000",
                       f"srt://mediamtx:8890?streamid=read:team05:caster-01:{caster['password']}")
        check("caster cannot read the live team path", "h264" not in probe.stdout)

        st, prev = await call("POST", "/delay/preview", {"minutes": 0.25})
        check("delay preview shorter -> skip incl. team05", prev.get("effect") == "skip" and
              any(x["team"] == "team05" for x in prev.get("skipped", [])), prev)
        st, prev = await call("POST", "/delay/preview", {"minutes": 1})
        check("delay preview longer -> hold 0.5 min", prev.get("effect") == "hold" and prev.get("hold_minutes") == 0.5,
              prev)
        st, _ = await call("PUT", "/delay", {"minutes": 0.75})
        await asyncio.sleep(4)
        st, status = await call("GET", "/status")
        check("longer delay applied -> feed holds on slate", status["delay_minutes"] == 0.75 and
              status["feeds"].get("s1t1", {}).get("state") == "holding", status["feeds"].get("s1t1"))
        st, bad = await call("PUT", "/delay", {"minutes": 500}, expect=400)
        check("out-of-range delay rejected", st == 400)

        print("   waiting for the held feed to resume...")
        await asyncio.sleep(16)
        st, status = await call("GET", "/status")
        feed = status["feeds"].get("s1t1", {})
        check("held feed resumes where it was, now ~45s behind", feed.get("state") == "playing" and
              40 <= (feed.get("behind_seconds") or 0) <= 55, feed)

        st, k = await call("POST", "/teams/team05/kick")
        check("kick disconnects alice", k.get("kicked") and k.get("login") == "team05-p1", k)
        docker("rm", "-f", "pub-alice")  # she stops for real (otherwise her ffmpeg would just reconnect)
        await asyncio.sleep(4)
        st, status = await call("GET", "/status")
        check("team05 offline after the kick", not next(t for t in status["teams"] if t["path"] == "team05")["live"])

        st, reset = await call("POST", "/logins/team05-p2/reset")
        check("reset gives bob a new password", st == 200 and reset["password"] != p2["password"], reset)
        publisher("pub-bob", "team05-p2", reset["password"], size="1280x720")  # player swap
        await asyncio.sleep(8)
        st, status = await call("GET", "/status")
        t5 = next(t for t in status["teams"] if t["path"] == "team05")
        check("player swap: team05 now live as bob at 1280x720", t5["live"] and t5.get("login") == "team05-p2" and
              (t5.get("video") or {}).get("width") == 1280, t5)

        now = time.time()
        st, m = await call("POST", "/matches", {"match_id": "set1-m1", "set_id": 1, "match_no": 1, "stage": 1,
                                                "round": 1, "teams": ["team05", "team06"],
                                                "start": now - 60, "end": now})
        check("match manifest saved", st == 200 and os.path.exists("recordings/_manifests/set1-m1.json"))
        st, arc = await call("GET", "/archive")
        check("archive status (local disk + archiver)", st == 200 and arc["local"]["free_bytes"] > 0 and
              arc.get("enabled") is False, arc)

    for name in ("pub-alice", "pub-bob", "pub-wrong"):
        docker("rm", "-f", name)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
