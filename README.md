# media

Streaming stack for the Conjoined tournament: players publish their POV to a fixed team path, the
Discord bot assigns teams to match slots, casters watch delayed slot feeds, and every match is
archived per team. Design and decisions: [SPEC.md](SPEC.md).

| Service | What it does |
|---|---|
| `mediamtx` | Ingest (SRT from OBS, WebRTC from the browser), delayed feeds out, records team paths in 4 s segments |
| `control` | Bot API (port 9000), MediaMTX's auth hook and the players' go-live page (port 9002): logins, slots, status, delay, match manifests |
| `delay` | One player per slot: replays the assigned team's recording *delay* minutes later into `sNtM-delayed` (or relays their Twitch stream, see below); same for each team's test feed `teamNN-preview` |
| `archiver` | Copies recordings to node 1 over SSH and prunes the SSD ring buffer |
| `stitch` (node 1) | One MP4 per team per match from the archive copy |

## Ports (stream container, node 2)

| Port | What | Open to |
|---|---|---|
| 8890/udp | SRT: players' OBS publish, casters' OBS view | Internet (router → node 2 → DNAT to the container) |
| 8189/udp | WebRTC media | Internet (router → node 2 → DNAT); set `PUBLIC_IP` |
| 8889/tcp | WebRTC pages (browser publish / view) | The reverse proxy only (`stream.acheros.be` → this port) |
| 9002/tcp | Players' go-live page | The reverse proxy only (`stream.acheros.be/go/` → this port) |
| 9000/tcp | Control API | The bot only |

## Setup (node 2: the stream container)

1. **Docker** in the container (Proxmox LXC: enable *nesting* and *keyctl*), then clone this repo.
2. **Settings:** `cp .env.example .env` and fill in:
   - `CONTROL_API_TOKEN` (`openssl rand -hex 32`): also goes in the bot's `STREAM_CONTROL_TOKEN`.
   - `DELAY_PASSWORD` (`openssl rand -hex 16`).
   - `PUBLIC_BROWSER_BASE`, `PUBLIC_SRT_HOST`, `PUBLIC_IP`: what ends up in players' and casters' links.
   - `CONTROL_BIND` / `MEDIA_HTTP_BIND`: the container's address the bot / proxy connect to.
   - **Reverse proxy:** the site (`PUBLIC_BROWSER_BASE`) goes to `MEDIA_HTTP_BIND:8889`, with HTTPS (browsers only
     share the screen on HTTPS pages), and a custom location `/go/` to `MEDIA_HTTP_BIND:9002`.
   - `HOST_RECORDINGS_DIR`, `HOST_DATA_DIR`: on the SSD.
3. **Archive copy (optional):** create an SSH key pair, put the private key in `secrets/archive_key`, the
   public key in node 1's `~/.ssh/authorized_keys` for the archive user, and set `ARCHIVE_TARGET`
   (e.g. `archive@192.168.100.158:/mnt/hdd/tourney`). Node 1 needs `rsync`.
4. **Start:** `docker compose up -d --build`.
5. **Node 2 DNAT** (Proxmox host): forward UDP 8890 and 8189 from the LAN address to the container, and
   TCP 9000 for the bot (same pattern as the game server).
6. **Firewall** (Proxmox, container): inbound only the ports above, from the sources above; outbound
   only what updates and the archive copy need (DNS, 80/443 while updating, SSH to node 1), plus
   HTTPS (443) to Twitch for Twitch passthrough (`*.twitch.tv`, `*.ttvnw.net`, `*.jtvnw.net`).

## Setup (node 1: stitcher)

Clone the repo next to the archive disk and run `HOST_ARCHIVE_DIR=/mnt/hdd/tourney docker compose -f
docker-compose.node1.yml up -d --build`. It writes `vods/` and `stitch-status.json` into that folder,
which the archiver reads back for `!stream archive` and `!stream vods`.

## Things to know

- **Delayed feeds need H.264 video.** OBS defaults to it; the go-live page always sends it.
  Other codecs show the slate and are flagged by `!stream inspect`.
- **B-frames and browsers:** browsers (WebRTC) can't play H.264 with B-frames, which OBS uses by
  default. Slot feeds copy the video, so casters should watch in **OBS over SRT**; a caster's browser
  link only works for players without B-frames (the go-live page never sends them, and its OBS
  instructions ask for "Max B-frames 0"). Test feeds re-encode, so they play in any browser.
- **The go-live page** (`/go/`, one link for everyone) lets players choose one of three ways:
  - *In this browser* (asks for their login; the team comes from it): shares the Balatro window and mixes
    the computer sound (a second share of the entire screen, sound only: browsers can't take one
    program's sound) and optionally a microphone. No camera. It reconnects by itself.
  - *With OBS*: setup instructions only (their SRT address already contains their login).
  - *Via their Twitch stream* (Twitch passthrough): they delay their Twitch stream by exactly the
    tournament delay (the page shows it, from `/go/delay`) and connect their channel; while their team is
    on a feed, the casters see their Twitch stream, relayed live (`streamlink`), not delayed again. It
    **isn't recorded**: the page and the bot's panels push them to also stream directly. Managers can set
    it too: `!stream twitch <team> <channel|off>`. Ad breaks show as a frozen picture or the slate, and
    Twitch's own lag puts them a few seconds behind their opponent.

  Each player's `browser_link` in the export is the same page with their login filled in, in the link's
  `#` part, which never reaches a server. MediaMTX's own page (`/teamNN/publish`) still works too.
- **Testing:** on the go-live page, "Test your setup" plays the team's test feed (`teamNN-preview`, only
  that team's players may watch it): exactly what a slot feed would show for them, one delay later, for
  any of the three ways. A login counts as tested (`!stream overview`) after 10 s live, or when the Twitch
  channel it connected is seen live. Set a short delay (`!stream delay`) for test sessions.
- **The slate** (`mediamtx/slate.mp4`) is a plain dark card with silence. Replace it with a branded
  one as long as it stays an MP4 with H.264 video and Opus audio.
- **One teammate at a time:** a second teammate starting while the first is live is refused, and the
  bot's panel says so. `!stream kick` disconnects a forgotten stream.

## Tests

- Unit tests: `cd control && python -m pytest tests` (needs `aiohttp`, `pytest`).
- End to end: with `DEFAULT_DELAY_MINUTES=0.5` in `.env`, `docker compose up -d --build`, then
  `python tests/integration_test.py` (needs `aiohttp` and Docker; runs fake publishers and readers).
