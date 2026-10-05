# Tournament streaming: spec

Status: **implemented, not yet deployed.** Based on "Discord Bot × Stream Integration" (Oct 3, 2026)
and the decisions made since. The original "Tournament Streaming Blueprint" is gone, so part 2 (the
media stack) is a fresh design. The open questions at the end were implemented with the defaults
noted there, all adjustable through settings.

### Implementation notes (what changed against the plan)

- **Delayed feeds:** each slot's player pipes the recorded segments through two ffmpeg processes
  (concat → MPEG-TS pipe → real-time publisher). Pacing the concat input directly runs at ~0.64× real
  time in ffmpeg. MediaMTX's `alwaysAvailable` keeps casters connected and plays the slate in gaps; it
  can't be used on regex paths, so `mediamtx.yml` lists the 16 delayed paths.
- **Archive IDs:** per match `set<ID>-m<match>`; whole-set fallback `set<ID>-all` (when no
  `!bala start` time is known for any of the set's matches).
- **Bot:** casters come from the caster role's members, which needs Discord's *Server Members Intent*
  (enabled in `bot.py`; must also be switched on in the developer portal).

Three pieces, built in this order:

1. **Bot permission rework** (`acheros-bmp-bot`): independent of streaming, done first.
2. **Media stack** (this repo): MediaMTX, the control service, delay, archive, in one Docker Compose setup.
3. **`!stream` cog** (`acheros-bmp-bot`): built against a mock of the control service first.

## Principles (unchanged from the design doc)

- **A stream never holds up a match.** Games start on schedule; a missing POV is the producer's problem.
- **Links are permanent.** One fixed path per team, one login per player, generated in one batch before
  the event. Starting a set creates nothing new, so nothing new can fail.
- **Testing happens before the event.** Every team does at least one test stream, by the player who will
  stream its games (both, if they take turns); the server records who
  succeeded. The organiser decides the timing; the bot has no deadline.
- **No player-facing commands.** Players never use the bot for streaming.
- **The delay (default 30 min) is the safety margin.** A late or dropped stream shows as a slate on the
  broadcast later, while the match carries on.

## Terminology

| Term | Meaning |
|---|---|
| stage, round, set, match | As in the bot: a **set** is one pairing (shown as `#12`), its **matches** are the individual games. The design doc's "match" is a set here. |
| team path | `team01` … `team16`: where a team's POV is published. Fixed per team for the whole event. |
| slot | `s1t1` … `s8t2`: set *n* of the current round, team 1 or 2. One POV each. |
| delayed feed | `s1t1-delayed` … `s8t2-delayed`: what casters watch. The only thing that leaves the server. |
| login | One per player and per caster. Players publish to their team path, casters read delayed feeds. |
| manager | Anyone with Discord's Administrator permission (the bot's existing `is_manager`). |
| caster | Anyone with the role set by `!stream casterrole`. |

Slots are the set's **position in the round**, not its ID; the bot always shows both, e.g. "s3 · Set #12".

---

## Part 1: bot permission rework

Goal: **anything a user isn't allowed to do is ignored silently.** No "only administrators…" replies.

1. **One permission module** (`checks.py`): `manager_only()` as a real `commands.check`. Every
   permission rule becomes a check instead of an error raised inside a command, so `!help` and the
   silent handler both see it. Removes `selection.py`'s duplicate `is_manager`.
2. **Manager-only:** every command in `bala`, `conjoined` (except `standings` and `roundstats`, see open
   question 9), `report`, `selection`, `team` (**all** of them, including `create` and `list`), `util`,
   `stream`, and `!help` itself.
3. **One global error handler** in `bot.py`: permission failures and commands sent in DMs are dropped
   without a reply. Each cog's own "Only administrators…" branch is removed. Permitted users still get the
   usual replies for bad arguments and real errors.
4. **Buttons:** a click from someone not allowed to use the button is acknowledged without any message,
   because a click with no answer at all makes Discord show "This interaction failed". The report widget's
   per-team voting buttons stay usable by that team's members.
5. **`!help`:** manager-only, lists everything (the 🔒 marker goes away, managers can run all of it).

---

## Part 2: media stack (this repo)

One Docker Compose setup on the stream container (node 2, `10.99.60.10`, reachable from the LAN through
node 2's `192.168.100.159`).

### Components

| Service | Role |
|---|---|
| `mediamtx` | Ingest and playback. Players publish to `teamNN` over **SRT** (OBS) or **WebRTC/WHIP** (browser page). Casters read `sNtM-delayed`. Records every team path to short segments on the SSD. Asks `control` about every publish/read attempt (`authMethod: http`), so logins change without reloading MediaMTX. |
| `control` | Python (aiohttp). The **only** place holding credentials and MediaMTX API access. Serves the bot's API (bearer token), answers MediaMTX auth requests, keeps state in SQLite: roster, logins, slot-assignment history, delay changes, verified publishers. |
| `delay` | One player per slot. Plays the **recorded team segments** from *now − delay*, choosing the team from the slot-assignment history at that moment, and publishes the result to `sNtM-delayed`. Missing footage → slate. Resumes from disk after a restart. |
| `archiver` | Copies segments from the SSD ring buffer to node 1's HDDs (rsync, every ~minute) and prunes SSD segments once they're copied and older than the delay plus a margin. |
| `stitch` (node 1) | Turns each match manifest into one MP4 per team (ffmpeg concat, no re-encode). Runs next to the HDD copy. |

**Why slots are virtual:** only team paths are recorded. A slot is just "which team, when", so the delay
player follows the assignment history. A slot assigned when a set starts reaches the casters exactly
*delay* later, in sync with the game, and `!stream lineup` uses the same history.

### Logins and paths

- **Player logins:** `teamNN-p1`, `teamNN-p2`, linked to the player's Discord ID (names change, logins
  don't). May publish to their own `teamNN` only. **One publisher per team path**: a teammate trying to
  start while the other is still live is refused (and the refusal is recorded so the panel can say so).
  That's how teams swap who plays between matches: one stops, the other starts.
- **Caster logins:** `caster-NN`, linked to Discord IDs. May read `*-delayed` only; a guessed live path is
  refused.
- **Passwords are stored by `control`**, because export must return the same passwords again. The DB file
  is the most sensitive thing in the setup.

### Bot-facing API (`control`)

JSON over HTTP, `Authorization: Bearer <token>`. Draft, to be firmed up before building.

| Endpoint | Used by | Purpose |
|---|---|---|
| `POST /export` | `!stream export` | Body: the full roster (`teams: [{path, name, players: [{discord_id, name}]}]`, `casters: [{discord_id, name}]`). Creates missing logins, **revokes logins of removed players/casters**, returns every login with its links (browser publish/read page, SRT URL). |
| `POST /logins/{login}/reset` | `!stream reset` | New password; returns the new links. |
| `GET /status` | panels, `overview`, `inspect` | Per team path: live or not, which login/Discord ID, since when, resolution, fps, bitrate, codecs, audio, last refused publish attempt; per login: has ever published successfully. |
| `PUT /slots` | set started, round changes, `!stream assign` | Body: `{"s1t1": "team05", "s1t2": "team11", …}` (null to free). Applied atomically, appended to the history with a timestamp. |
| `GET /lineup` | `!stream lineup` | Per slot: team on the delayed feed now, and upcoming assignments with the time they reach the feed. |
| `POST /teams/{path}/kick` | `!stream kick` | Disconnect the current publisher. |
| `GET /delay`, `POST /delay/preview`, `PUT /delay` | `!stream delay` | Read, preview ("feeds hold on slate for 15 min" / "feeds skip 14:20–14:30, which contains team05 playing"), apply (logged). Range 5–120 min. |
| `POST /matches` | match result recorded | Match manifest: `{match_id: "set12-m2", set_id, stage, round, match_no, teams: [path, path], start, end}`. Written next to the segments; reaches node 1 with the next copy, where `stitch` cuts one MP4 per team. |
| `GET /archive`, `GET /vods?team=&match=` | `!stream archive`, `!stream vods` | Copy lag, SSD ring usage, lost footage, last stitched match, HDD free space; list of archived files. |

MediaMTX's auth requests go to a separate internal endpoint that is only reachable from the MediaMTX
container, never through the bot's port.

### Ports (stream container)

| Port | What | Reachable from |
|---|---|---|
| 8890/udp | SRT publish/read | Internet (router → node 2 → DNAT to `10.99.60.10`) |
| 8889/tcp | WebRTC pages (browser publish, browser read) | Through the reverse proxy over HTTPS (`stream.acheros.be`) |
| 8189/udp | WebRTC media (ICE) | Internet (router → node 2 → DNAT); MediaMTX told the public IP |
| 9000/tcp | `control` API | The bot only (node 2 DNAT + container firewall) |
| 9997/tcp | MediaMTX API | Inside the compose network only |

Container firewall: inbound only the above, outbound blocked except while updating (same pattern as the
game server).

### Storage (to confirm against real numbers)

At ~4 Mbps per stream and 16 streams: the SSD ring buffer holds at least *delay + margin*, about
**15 GB per 30 min**; the full archive is about **29 GB per hour of event**.

---

## Part 3: the `!stream` cog (bot)

Separate cog, removable without touching the rest of the bot. Talks only to `control`, holds no MediaMTX
credentials, reads no live paths, and **never posts credentials in a channel**.

### Commands (all manager-only unless noted)

| Command | What it does |
|---|---|
| `!stream casterrole @Role` | Sets which role counts as caster. Members with it at export time get caster logins. |
| `!stream export` | Posts an **Export** button. Clicking it syncs the roster to `control` and replies with the CSV **privately, only to the manager who clicked**: per player team, browser link, login, password, SRT address; per caster login and all 16 delayed-feed links. Re-running re-exports the same logins, creates missing ones and revokes removed ones. |
| `!stream reset <@player or login>` | New password; the new link is only shown through a private button reply. |
| `!stream overview` | All teams: who has streamed successfully at least once, who's live now, current slot mapping, current delay. |
| `!stream inspect <team>` | The team's current stream: player, resolution, fps, bitrate, codecs, audio. |
| `!stream lineup` | Managers and **casters**, in `#stream-casters`. What's on each feed now (in feed time) and what's coming next. |
| `!stream assign <slot> <team>` | Manual slot assignment (normally automatic). |
| `!stream kick <team>` | Disconnect whoever is publishing on the team's path. |
| `!stream delay [minutes] [confirm]` | Show; preview; apply. |
| `!stream archive` | Archive health. |
| `!stream vods <team or set>` | Archived files for a team or set. |

### Automatic behaviour

| Trigger | What the cog does |
|---|---|
| `!stream casterrole` set | Creates `#stream-casters` (caster role + managers), or repairs its permissions. |
| **Set started** (new event from `match_channels.start_set`) | Assigns its slots (`s{position}t1/t2`) and posts the status panel in the set's channel. |
| **`!bala start`** (new event) | Records the start time of the current match of every set in progress. |
| **Match result recorded** (new event from the report flow) | Sends the match manifest (start from `!bala start` with a minute of padding, end = result time plus a minute). |
| **Set decided** (new event) | Frees its slots. |
| **Set reopened** by `!report correct` (new event) | Re-assigns its slots. |
| Every ~10 s | Refreshes the panels. Edits only when something changed; one message per set, edited in place, never re-posted (so it doesn't fight the report widget). |

**Per-match archives** need a known start, so when `manual_start` is on (hosts start games themselves)
the bot can't tell when a match began; those sets fall back to one archive per set (from set start to set
decided).

**The panel** is an embed with, per team: streaming or not, and by which player (or "a teammate tried to
start while the other is live"). No links or logins. Both teams see it.

**State** lives in the cog's own file, `data/stream_<guild>.json`: caster role, `#stream-casters` ID,
team → `teamNN` mapping (assigned once, kept for the event), panel message IDs, match start times.

**If `control` is down:** panels say "stream status unavailable" instead of showing everyone offline, and
nothing else in the bot is affected; the event listeners run separately from the set-start flow.

---

## Testing (from the design doc)

1. Simulated teams: a script starts 16 ffmpeg test-pattern publishers, one per team login.
2. Cog in a test Discord server with dummy teams (`!util filldummy`): export, slot assignment, panels,
   lineup, permissions (non-managers get no response).
3. Chaos run: randomly stop/restart publishers, start a second teammate on a live path, kill `delay`,
   restart MediaMTX. Panels and overview must follow; delayed feeds must recover.
4. Scrim night with real teams 1–2 weeks before: browser publishing, player swaps between matches, a
   caster using only the feed links and `!stream lineup`.
5. Every team's mandatory test stream (whoever will stream its games), followed via `!stream overview`.

## Open questions (implemented with these defaults)

1. **Hardware:** sizes unknown; retention is set by `LOCAL_RETAIN_MARGIN_MINUTES` / `LOCAL_RETAIN_MAX_HOURS`.
2. **Stream quality:** not enforced; `!stream inspect` shows what each player sends. Only H.264 video works.
3. **Domain:** `stream.acheros.be` (`PUBLIC_BROWSER_BASE`, `PUBLIC_SRT_HOST`).
4. **Slate:** plain dark card (`mediamtx/slate.mp4`); replace with a branded H.264 + Opus MP4.
5. **Casters' audio:** the players' stream audio only (converted to Opus).
6. **Browser publishing:** enabled (WebRTC page through the proxy, UDP 8189 forwarded).
7. **Delay default:** 30 minutes (`DEFAULT_DELAY_MINUTES`).
8. **Team → `teamNN`:** registration order on first use, kept for the event.
9. **`!conjoined standings` / `roundstats`:** still usable by everyone.
