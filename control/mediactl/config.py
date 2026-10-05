"""Settings, all from environment variables (see .env.example at the repo root)."""
import os
import re
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


# -- bot API ---------------------------------------------------------------------
API_TOKEN = _env("CONTROL_API_TOKEN")
API_PORT = _int("CONTROL_API_PORT", 9000)
# MediaMTX's auth hook; only reachable inside the compose network
AUTH_PORT = _int("CONTROL_AUTH_PORT", 9001)
# Public pages (the players' go-live page), behind the reverse proxy at /go/
PAGES_PORT = _int("CONTROL_PAGES_PORT", 9002)

# -- storage ---------------------------------------------------------------------
DB_PATH = Path(_env("DB_PATH", "/data/control.db"))
RECORDINGS_DIR = Path(_env("RECORDINGS_DIR", "/recordings"))
MANIFESTS_DIR = RECORDINGS_DIR / "_manifests"
STATUS_DIR = Path(_env("STATUS_DIR", "/data/status"))  # archiver -> server status files
SLATE_FILE = Path(_env("SLATE_FILE", "/slate/slate.mp4"))

# -- MediaMTX --------------------------------------------------------------------
MEDIAMTX_API = _env("MEDIAMTX_API", "http://mediamtx:9997").rstrip("/")
MEDIAMTX_RTSP = _env("MEDIAMTX_RTSP", "rtsp://mediamtx:8554").rstrip("/")
# Internal account the delay players publish the delayed feeds with
DELAY_USER = _env("DELAY_USER", "delay")
DELAY_PASSWORD = _env("DELAY_PASSWORD")
STATUS_POLL_SECONDS = _float("STATUS_POLL_SECONDS", 2)

# -- event shape -----------------------------------------------------------------
TEAM_COUNT = _int("TEAM_COUNT", 16)
SET_COUNT = _int("SET_COUNT", 8)  # sets played at once -> slots s1t1 .. s8t2
TEAM_PATHS = [f"team{n:02d}" for n in range(1, TEAM_COUNT + 1)]
SLOTS = [f"s{s}t{t}" for s in range(1, SET_COUNT + 1) for t in (1, 2)]
DELAYED_SUFFIX = "-delayed"

TEAM_PATH_RE = re.compile(r"^team(\d{2})$")
DELAYED_PATH_RE = re.compile(r"^(s\d+t[12])-delayed$")
# Each team's test feed: what a slot would show for that team, watched only by its own players
PREVIEW_SUFFIX = "-preview"
PREVIEW_PATH_RE = re.compile(r"^(team\d{2})-preview$")
# How long a test feed keeps running after the page last asked for it (the page asks every 30 s
# while someone watches, and stops watching by itself after a few minutes)
PREVIEW_MINUTES = _float("PREVIEW_MINUTES", 1)
# At most this many test feeds at once: each one costs ~0.2-0.5 CPU core and ~2 Mbps upload per viewer
PREVIEW_MAX = _int("PREVIEW_MAX", 8)

# -- delay -----------------------------------------------------------------------
DEFAULT_DELAY_MINUTES = _float("DEFAULT_DELAY_MINUTES", 30)
# The bot only offers 5-120; the service also accepts shorter delays for testing
MIN_DELAY_MINUTES = _float("MIN_DELAY_MINUTES", 0.1)
MAX_DELAY_MINUTES = _float("MAX_DELAY_MINUTES", 120)

# -- public links (what players and casters get in the export) --------------------
BROWSER_BASE = _env("PUBLIC_BROWSER_BASE", "https://stream.acheros.be").rstrip("/")
SRT_HOST = _env("PUBLIC_SRT_HOST", "stream.acheros.be")
SRT_PORT = _int("PUBLIC_SRT_PORT", 8890)
# Optional SRT encryption passphrase (10-79 characters), shared by publishers and readers
SRT_PASSPHRASE = _env("SRT_PASSPHRASE")

# -- archive (archiver) ----------------------------------------------------------
# rsync destination on node 1, e.g. "archive@192.168.100.158:/mnt/hdd/tourney". Empty: no copying.
ARCHIVE_TARGET = _env("ARCHIVE_TARGET")
ARCHIVE_SSH_KEY = _env("ARCHIVE_SSH_KEY", "/secrets/archive_key")
ARCHIVE_SSH_PORT = _int("ARCHIVE_SSH_PORT", 22)
ARCHIVE_INTERVAL_SECONDS = _int("ARCHIVE_INTERVAL_SECONDS", 60)
# Keep segments on the SSD for the delay plus this margin (and until they've been copied)
LOCAL_RETAIN_MARGIN_MINUTES = _float("LOCAL_RETAIN_MARGIN_MINUTES", 15)
# Safety net when nothing is being copied: never keep more than this many hours locally
LOCAL_RETAIN_MAX_HOURS = _float("LOCAL_RETAIN_MAX_HOURS", 48)
