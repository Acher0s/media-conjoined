"use strict";
// The players' "go live" page (served at /go/). They choose one of three ways to stream:
//  * this browser: shares the screen and publishes it to MediaMTX over WHIP (WebRTC) as H.264 video
//    plus one audio track mixing the computer sound and, optionally, a microphone. No camera.
//  * OBS: instructions only (their SRT address already contains their login).
//  * their Twitch stream: instructions with the required delay, and connecting their channel
//    (Twitch passthrough, /go/twitch).
// For every way, "Test your setup" plays their team's test feed (teamNN-preview, over WHEP): what
// the casters would get from them, one delay later. The team comes from the login
// (team05-p1 -> team05); MediaMTX is on the same site and checks the login with the control service.

const LOGIN_RE = /^(team\d{2})-p\d+$/;
const LOGIN_KEY = "golive:login";
const MAX_BITRATE = 6_000_000;
const SOUND_CONSTRAINTS = {echoCancellation: false, noiseSuppression: false, autoGainControl: false};
const RETRY_MS = 5000;
const REFUSED = "Refused: either the login or password is wrong, or a teammate is still streaming for your team " +
                "(they have to stop first).";
const $ = (id) => document.getElementById(id);

// -- login --------------------------------------------------------------------------------------
// The export link carries it after the '#', which browsers never send to a server. It's moved to
// session storage and taken out of the address bar, which would show when sharing the entire screen.

let creds = null;      // the login being streamed with
let whipUrl = null;    // MediaMTX's WHIP endpoint for its team

// A personal link fills in the login (from the part after '#'); the universal /go/ link doesn't.
function initLogin() {
  let saved = null;
  const hash = decodeURIComponent(location.hash.slice(1));
  const i = hash.indexOf(":");
  if (i > 0) {
    saved = {user: hash.slice(0, i), pass: hash.slice(i + 1)};
    try { sessionStorage.setItem(LOGIN_KEY, JSON.stringify(saved)); } catch (e) { /* private mode */ }
    history.replaceState(null, "", location.pathname);
  } else {
    try { saved = JSON.parse(sessionStorage.getItem(LOGIN_KEY)); } catch (e) { saved = null; }
  }
  if (saved && saved.user && saved.pass) {
    $("user").value = saved.user;
    $("pass").value = saved.pass;
  }
}

function credentials() {
  return {user: $("user").value.trim().toLowerCase(), pass: $("pass").value.trim()};
}

// The login typed in, or null after explaining what's wrong in `show`
function checkedLogin(show) {
  const c = credentials();
  if (!c.user || !c.pass) {
    show("Enter your login and password first (at the top).");
    return null;
  }
  if (!LOGIN_RE.test(c.user)) {
    show("That isn't a player login: they look like team05-p1.");
    return null;
  }
  return c;
}

const LOGIN_HINTS = {
  browser: "Your login from the tournament: to stream from this browser and to watch your test feed.",
  obs: "Your login from the tournament: only to watch your test feed (OBS's address already contains it).",
  twitch: "Your login from the tournament: to connect your Twitch channel and to watch your test feed.",
};

function choose(mode) {
  for (const [name, button] of [["browser", "pickBrowser"], ["obs", "pickObs"], ["twitch", "pickTwitch"]]) {
    $(name).hidden = name !== mode;
    $(button).setAttribute("aria-pressed", String(name === mode));
  }
  // The test section follows the chosen way's main action
  document.querySelector(`.testSlot[data-mode="${mode}"]`).append($("test"));
  $("login").hidden = $("test").hidden = false;
  $("loginHint").textContent = LOGIN_HINTS[mode];
  refreshMe();
}

function goToTest() {
  $("test").scrollIntoView({behavior: "smooth", block: "start"});
}

// -- login bar and test banner: what the tournament knows about this player ---------------------------

let meTimer = null;
let loggedIn = false;
let teamLive = false;  // check more often while the team streams: settings change as players fix them

function showLoggedIn(data) {
  loggedIn = Boolean(data);
  $("loginForm").hidden = loggedIn;
  $("loginDone").hidden = !loggedIn;
  if (!data) showSettings(null);
  if (data) {
    $("whoLogin").textContent = data.login;
    $("whoMore").textContent = `(${data.name} \u00b7 ${data.team_name || data.team})`;
    $("loginError").textContent = "";
  }
  showBanner(data);
}

function escapeHtml(text) {
  const el = document.createElement("span");
  el.textContent = text;
  return el.innerHTML;
}

// -- the stream settings checklist (settings.py on the server) ----------------------------------------

const SETTING_LABELS = {
  codec: "Video codec H.264",
  bframes: "No B-frames",
  keyframes: "A keyframe every 2 s",
  bitrate: "Bitrate 8 Mbps or less",
  resolution: "1080p or lower",
  fps: "60 fps or less",
  audio: "Sound",
};

function showSettings(data) {
  const box = $("settings");
  const result = data && data.settings;
  if (!result) {
    box.hidden = true;
    return;
  }
  const ago = Math.max(0, Math.round(Date.now() / 1000 - result.checked_at));
  const whose = result.login && result.login !== data.login ? ` (${escapeHtml(result.login)}'s stream)` : "";
  const live = data.live_login ? `checked ${ago < 60 ? `${ago} s` : `${Math.round(ago / 60)} min`} ago`
                               : "from your team's last stream";
  const items = result.checks.map((item) =>
    `<li>${item.ok ? "\u2705" : "\u274c"} ${SETTING_LABELS[item.key] || item.key} ` +
    `<span class="hint">(${escapeHtml(String(item.value))})</span>` +
    (item.fix ? `<div class="fix">Fix: ${escapeHtml(item.fix)}</div>` : "") + "</li>").join("");
  box.className = result.ok ? "good" : "bad";
  box.innerHTML = `<h3>${result.ok ? "\u2705 Your stream settings are good" : "\u274c Your stream settings need fixing"}</h3>` +
    `<p class="hint">Measured on your stream${whose}, ${live}.${result.ok ? "" : " Change them, then check back here " +
    "(it updates by itself)."}</p><ul>${items}</ul>`;
  box.hidden = false;
}

function showBanner(data) {
  const banner = $("banner");
  const button = (label) => `<button class="small" data-go-test>${label}</button>`;
  const when = (t) => new Date(t * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
  const others = data ? (data.team_tested || []).filter((p) => p.login !== data.login) : [];
  if (!data) {
    banner.className = "banner";
    banner.innerHTML = "<p>Log in to see whether your team has tested. <b>Every team has to test once before the " +
                       "event:</b> at least the player who will stream your games.</p>";
  } else if (data.tested) {
    banner.className = "banner tested";
    banner.innerHTML = `<h3>\u2705 Your setup is tested</h3><p>The tournament received a working stream from you ` +
                       `(${when(data.tested_at)}), so your team is covered. Changed something since? Test again any ` +
                       `time.</p>${button("Test again \u2193")}`;
  } else if (others.length) {
    const names = others.map((p) => `<b>${escapeHtml(p.name)}</b> (${when(p.tested_at)})`).join(", ");
    banner.className = "banner tested";
    banner.innerHTML = `<h3>\u2705 Your team is covered</h3><p>${names} tested a stream for your team. That's ` +
                       "enough if they stream your games. <b>Will you stream some games yourself too?</b> Then test " +
                       `your own setup as well.</p>${button("Test my own setup \u2193")}`;
  } else {
    banner.className = "banner untested";
    banner.innerHTML = "<h3>\u26a0\ufe0f Your team hasn't tested yet</h3>" +
      "<p><b>Every team has to test once before the event:</b> the player who will stream your games (if you'll " +
      "both stream, both test). It takes a few minutes:</p>" +
      "<ol><li>Go live, the way you chose below.</li><li>Watch your test feed and check that you see and hear your " +
      "game.</li><li>This banner turns green by itself.</li></ol>" + button("Go to the test \u2193");
  }
  if (data && data.settings && !data.settings.ok) {
    banner.insertAdjacentHTML("beforeend", "<p class=\"settingsWarning\">\u274c Your stream settings need fixing: " +
                              "see the checklist in Test your setup.</p>");
  }
  const go = banner.querySelector("[data-go-test]");
  if (go) go.addEventListener("click", goToTest);
}

async function refreshMe() {
  clearTimeout(meTimer);
  meTimer = setTimeout(refreshMe, watching || teamLive ? 10000 : 30000);
  const c = credentials();
  if (!c.user || !c.pass || !LOGIN_RE.test(c.user)) {
    showLoggedIn(null);
    return;
  }
  let res, data;
  try {
    res = await fetch("/go/me", {headers: {"Authorization": basicAuth(c)}, cache: "no-store"});
    data = await res.json();
  } catch (e) {
    return;  // keep showing what we had
  }
  if (!res.ok) {
    showLoggedIn(null);
    $("loginError").textContent = data.error || "Couldn't check your login.";
    return;
  }
  try { sessionStorage.setItem(LOGIN_KEY, JSON.stringify(c)); } catch (e) { /* private mode */ }
  teamLive = Boolean(data.live_login);
  showLoggedIn(data);
  showTwitch(data.twitch);
  showSettings(data);
}

function logIn(e) {
  e.preventDefault();
  const c = checkedLogin((text) => { $("loginError").textContent = text; });
  if (c) refreshMe();
}

function changeLogin() {
  if (wantLive) return;  // stop streaming first
  $("loginForm").hidden = false;
  $("loginDone").hidden = true;
  $("user").focus();
}

// -- Twitch passthrough -----------------------------------------------------------------------------

function showTwitch(state) {
  if (!state || !state.channel) {
    $("twitchState").textContent = "Your team isn't using a Twitch stream.";
    return;
  }
  if (!$("twitchChannel").value) $("twitchChannel").value = state.channel;
  const live = state.live === true ? "live right now" : state.live === false ? "offline right now" : "checking whether it's live...";
  $("twitchState").textContent = `While your team is on a feed, the casters see twitch.tv/${state.channel} (${live}).`;
}

async function setTwitch(channel) {
  const c = checkedLogin((text) => { $("twitchState").textContent = text; });
  if (!c) return;
  let res, data;
  try {
    res = await fetch("/go/twitch", {
      method: "POST",
      headers: {"Authorization": basicAuth(c), "Content-Type": "application/json"},
      body: JSON.stringify({channel}),
    });
    data = await res.json();
  } catch (e) {
    $("twitchState").textContent = "Can't reach the stream server. Try again in a moment.";
    return;
  }
  if (!res.ok) {
    $("twitchState").textContent = data.error || "That didn't work.";
    return;
  }
  if (!channel) $("twitchChannel").value = "";
  showTwitch(data);
}

// -- test feed: the team's teamNN-preview, received over WHEP ----------------------------------------

let viewer = null;       // {pc, resource, auth}
let watching = false;
let renewTimer = null;
let viewRetry = null;
let viewTimer = null;    // the countdown to stopping by itself
let viewDeadline = 0;
const VIEW_LIMIT_MS = 3 * 60000;

async function watchPreview() {
  const c = checkedLogin((text) => { $("previewState").textContent = text; });
  if (!c) return;
  watching = true;
  $("watch").hidden = true;
  $("unwatch").hidden = false;
  viewDeadline = Date.now() + VIEW_LIMIT_MS;
  clearInterval(viewTimer);
  viewTimer = setInterval(() => {
    const left = Math.max(0, viewDeadline - Date.now());
    if (left === 0) {
      stopWatching("Stopped after 3 minutes, to keep the server free for other players. Watch again if you need to.");
      return;
    }
    $("viewLeft").textContent = `${Math.floor(left / 60000)}:${String(Math.floor(left / 1000) % 60).padStart(2, "0")}`;
  }, 1000);
  refreshMe();
  await startViewer(c);
}

async function startViewer(c) {
  clearTimeout(viewRetry);
  closeViewer();
  if (!watching) return;
  const auth = basicAuth(c);
  let path;
  try {  // starts (or keeps running) the team's test feed
    const res = await fetch("/go/preview", {method: "POST", headers: {"Authorization": auth}});
    const data = await res.json();
    if (!res.ok) {
      stopWatching(data.error || "Couldn't start your test feed.");
      return;
    }
    path = data.path;
  } catch (e) {
    return retryViewer(c, "Can't reach the stream server. Retrying...");
  }
  clearInterval(renewTimer);
  renewTimer = setInterval(() => {  // the server stops the test feed a minute after the last of these
    fetch("/go/preview", {method: "POST", headers: {"Authorization": auth}}).catch(() => {});
  }, 30000);

  const conn = new RTCPeerConnection({bundlePolicy: "max-bundle"});
  viewer = {pc: conn, resource: null, auth};
  const media = new MediaStream();
  conn.addTransceiver("video", {direction: "recvonly"});
  conn.addTransceiver("audio", {direction: "recvonly"});
  conn.addEventListener("track", (e) => {
    media.addTrack(e.track);
    $("preview").srcObject = media;
    $("preview").hidden = false;
  });
  conn.addEventListener("connectionstatechange", () => {
    if (viewer && viewer.pc === conn && ["failed", "disconnected"].includes(conn.connectionState)) {
      retryViewer(c, "The test feed dropped. Reconnecting...");
    }
  });
  try {
    await conn.setLocalDescription(await conn.createOffer());
    await iceGatheringDone(conn);
    const res = await fetch(new URL(`/${path}/whep`, location.origin), {
      method: "POST",
      headers: {"Content-Type": "application/sdp", "Authorization": auth},
      body: conn.localDescription.sdp,
    });
    if (res.status === 401 || res.status === 403) {
      stopWatching("Your login was refused for the test feed. Check it at the top.");
      return;
    }
    if (!res.ok) return retryViewer(c, `The stream server answered ${res.status}. Retrying...`);
    viewer.resource = new URL(res.headers.get("Location") || "", res.url).href;
    await conn.setRemoteDescription({type: "answer", sdp: await res.text()});
  } catch (e) {
    return retryViewer(c, "Couldn't open the test feed. Retrying...");
  }
  const delay = $("test").querySelector(".delayMin").textContent;
  $("previewState").innerHTML = `Watching your test feed (stops by itself in <b id="viewLeft">3:00</b>). Your game ` +
    `shows up about ${delay} minute(s) after you went live; until then (or when nothing is coming in) you see the ` +
    "waiting screen.";
}

function retryViewer(c, text) {
  if (!watching) return;
  $("previewState").textContent = text;
  closeViewer();
  clearTimeout(viewRetry);
  viewRetry = setTimeout(() => startViewer(c), RETRY_MS);
}

function closeViewer() {
  if (!viewer) return;
  if (viewer.resource) {
    fetch(viewer.resource, {method: "DELETE", headers: {"Authorization": viewer.auth}, keepalive: true}).catch(() => {});
  }
  viewer.pc.close();
  viewer = null;
}

function stopWatching(text = "") {
  if (watching) {  // nobody needs the test feed now: stop it on the server straight away
    const c = credentials();
    if (c.user && c.pass) {
      fetch("/go/preview", {method: "DELETE", headers: {"Authorization": basicAuth(c)}, keepalive: true}).catch(() => {});
    }
  }
  watching = false;
  clearTimeout(viewRetry);
  clearInterval(renewTimer);
  clearInterval(viewTimer);
  closeViewer();
  $("preview").srcObject = null;
  $("preview").hidden = true;
  $("watch").hidden = false;
  $("unwatch").hidden = true;
  $("previewState").textContent = text;
}

function basicAuth(c) {
  return "Basic " + btoa(String.fromCharCode(...new TextEncoder().encode(`${c.user}:${c.pass}`)));
}

// -- status -------------------------------------------------------------------------------------

function setStatus(text, kind = "") {
  $("statusText").textContent = text;
  $("status").className = kind;
  if (kind !== "live") $("stats").textContent = "";
  $("checkTest").hidden = kind !== "live" || watching;
}

function setNote(text) {
  $("note").textContent = text;
}

// -- audio: computer sound and microphone, mixed into one track ---------------------------------

const audio = {ctx: null, dest: null, sys: null, mic: null};

function mixer() {
  if (!audio.ctx) {
    audio.ctx = new AudioContext({sampleRate: 48000});
    audio.dest = audio.ctx.createMediaStreamDestination();
    // A silent source that's always connected: without any input the browser sends no audio at all,
    // and MediaMTX only records the tracks that send something in the first seconds. With it, the
    // stream always has an audio track, and sound added later (microphone, computer sound) is in it.
    const silence = audio.ctx.createConstantSource();
    silence.offset.value = 0;
    silence.connect(audio.dest);
    silence.start();
  }
  audio.ctx.resume();  // called from a click, so the browser lets it run
  return audio;
}

function sourceGain(kind) {
  const on = $(kind === "sys" ? "sysOn" : "micOn").checked;
  return on ? Number($(kind === "sys" ? "sysVol" : "micVol").value) / 100 : 0;
}

function connectSource(kind, stream) {
  disconnectSource(kind);
  const track = stream.getAudioTracks()[0];
  if (!track) return false;
  const {ctx, dest} = mixer();
  const source = ctx.createMediaStreamSource(new MediaStream([track]));
  const gain = ctx.createGain();
  gain.gain.value = sourceGain(kind);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(gain);
  gain.connect(dest);
  gain.connect(analyser);
  audio[kind] = {stream, source, gain, analyser};
  return true;
}

function disconnectSource(kind) {
  const s = audio[kind];
  if (!s) return;
  s.source.disconnect();
  s.gain.disconnect();
  if (kind === "mic") s.stream.getTracks().forEach((t) => t.stop());  // the screen's tracks stop with the screen
  audio[kind] = null;
}

function updateGains() {
  for (const kind of ["sys", "mic"]) {
    if (audio[kind]) audio[kind].gain.gain.value = sourceGain(kind);
  }
}

async function openMic(deviceId) {
  mixer();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: deviceId ? {deviceId: {exact: deviceId}} : true,
      video: false,
    });
    if (!$("micOn").checked) {  // unticked while the browser was asking
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    connectSource("mic", stream);
    await fillMicList(stream.getAudioTracks()[0].getSettings().deviceId);
    $("micControls").hidden = false;
    setNote("");
  } catch (e) {
    $("micOn").checked = false;
    $("micControls").hidden = true;
    setNote(`Couldn't open the microphone (${e.name}). Check that no other app blocks it and that the browser may use it.`);
  }
}

async function fillMicList(selected) {
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  const select = $("micDevice");
  select.replaceChildren(...devices.map((d, n) => {
    const option = new Option(d.label || `Microphone ${n + 1}`, d.deviceId);
    option.selected = d.deviceId === selected;
    return option;
  }));
}

const levels = new Float32Array(512);

function drawMeters() {
  for (const kind of ["sys", "mic"]) {
    let level = 0;
    const s = audio[kind];
    if (s) {
      s.analyser.getFloatTimeDomainData(levels);
      let sum = 0;
      for (const v of levels) sum += v * v;
      level = Math.min(1, Math.sqrt(sum / levels.length) * 4);
    }
    $(`${kind}Meter`).style.width = `${Math.round(level * 100)}%`;
  }
  requestAnimationFrame(drawMeters);
}

// -- publishing -----------------------------------------------------------------------------------

let display = null;     // the shared Balatro window or screen (video, plus sound if it's the entire screen)
let soundShare = null;  // a second share of the entire screen, only for its sound (see addComputerSound)
let pc = null;          // the current WebRTC connection
let resource = null;    // its WHIP session URL, for DELETE
let wantLive = false;
let retryTimer = null;
let statsTimer = null;

async function goLive() {
  const c = checkedLogin((text) => setStatus(text, "bad"));
  if (!c) return;
  const team = LOGIN_RE.exec(c.user);
  creds = c;
  whipUrl = new URL(`/${team[1]}/whip`, location.origin).href;
  try { sessionStorage.setItem(LOGIN_KEY, JSON.stringify(c)); } catch (e) { /* private mode */ }
  refreshMe();
  mixer();
  $("go").disabled = true;
  try {
    display = await navigator.mediaDevices.getDisplayMedia({
      // Opens on the "Window" tab: share only Balatro
      video: {displaySurface: "window", width: {max: 1920}, height: {max: 1080}, frameRate: {ideal: 60, max: 60}},
      audio: $("sysOn").checked ? SOUND_CONSTRAINTS : false,
      systemAudio: "include",
      selfBrowserSurface: "exclude",
      surfaceSwitching: "include",
    });
  } catch (e) {
    $("go").disabled = false;
    setStatus(e.name === "NotAllowedError" ? "Screen sharing was cancelled." : `Couldn't share the screen: ${e.message}`, "bad");
    return;
  }
  const video = display.getVideoTracks()[0];
  video.contentHint = "detail";  // keep text and cards sharp; drop frames rather than resolution
  video.addEventListener("ended", () => stop("You stopped sharing your screen, so the stream stopped."));
  if (!soundShare) connectSource("sys", display);
  updateSoundButton();
  wantLive = true;
  $("stop").hidden = false;
  $("user").disabled = $("pass").disabled = true;
  setStatus("Connecting...");
  await publish();
}

async function publish() {
  clearTimeout(retryTimer);
  closeConnection();
  if (!wantLive) return;
  const conn = new RTCPeerConnection({bundlePolicy: "max-bundle"});
  pc = conn;
  const videoTransceiver = conn.addTransceiver(display.getVideoTracks()[0], {direction: "sendonly"});
  const h264 = preferH264(videoTransceiver);
  conn.addTransceiver(audio.dest.stream.getAudioTracks()[0], {direction: "sendonly"});
  conn.addEventListener("connectionstatechange", () => onConnectionState(conn));

  let sdp;
  try {
    await conn.setLocalDescription(await conn.createOffer());
    await limitBitrate(videoTransceiver.sender);
    await iceGatheringDone(conn);
    sdp = conn.localDescription.sdp;
  } catch (e) {
    return fail(`Your browser couldn't set up the stream: ${e.message}`);
  }
  if (!h264) {
    if (!/a=rtpmap:\d+ H264\/90000/i.test(sdp)) {
      return fail("This browser can't send H.264 video, which the tournament needs. Use Chrome or Edge, or OBS.");
    }
    sdp = h264First(sdp);
  }

  let res;
  try {
    res = await fetch(whipUrl, {
      method: "POST",
      headers: {"Content-Type": "application/sdp", "Authorization": basicAuth(creds)},
      body: sdp,
    });
  } catch (e) {
    return retryLater("Can't reach the stream server. Retrying...");
  }
  if (conn !== pc) return;  // stopped or restarted meanwhile
  if (res.status === 401 || res.status === 403) return fail(REFUSED);
  if (!res.ok) return retryLater(`The stream server answered ${res.status}. Retrying...`);
  resource = new URL(res.headers.get("Location") || whipUrl, whipUrl).href;
  try {
    await conn.setRemoteDescription({type: "answer", sdp: await res.text()});
  } catch (e) {
    return retryLater(`The stream server's answer didn't work (${e.message}). Retrying...`);
  }
}

// A shared window has no sound and browsers can't take one program's sound, so the computer sound
// comes from a second share: the entire screen with "Share system audio". Only its sound is used
// (its picture is kept tiny, and isn't sent). Works before or while live: the mix changes in place.
async function addComputerSound() {
  mixer();
  let share;
  try {
    share = await navigator.mediaDevices.getDisplayMedia({
      video: {displaySurface: "monitor", width: {max: 320}, frameRate: {max: 1}},
      audio: SOUND_CONSTRAINTS,
      systemAudio: "include",
      selfBrowserSurface: "exclude",
    });
  } catch (e) {
    setNote(e.name === "NotAllowedError" ? "" : `Couldn't add the computer sound: ${e.message}`);
    return;
  }
  const track = share.getAudioTracks()[0];
  if (!track) {
    share.getTracks().forEach((t) => t.stop());
    setNote("That share had no sound. Pick \"Entire screen\" and tick \"Share system audio\".");
    return;
  }
  dropSoundShare();
  soundShare = share;
  connectSource("sys", share);
  track.addEventListener("ended", () => {
    if (soundShare === share) {
      dropSoundShare();
      if (display) connectSource("sys", display);
      updateSoundButton();
    }
  });
  updateSoundButton();
}

function dropSoundShare() {
  if (!soundShare) return;
  disconnectSource("sys");
  soundShare.getTracks().forEach((t) => t.stop());
  soundShare = null;
}

function updateSoundButton() {
  const missing = $("sysOn").checked && !audio.sys;
  $("addSys").hidden = !missing;
  setNote(missing && display ? "Your stream has no computer sound yet: click \"Add computer sound\"." : "");
}

function onConnectionState(conn) {
  if (conn !== pc || !wantLive) return;
  if (conn.connectionState === "connected") {
    setStatus("You're live.", "live");
    startStats();
  } else if (conn.connectionState === "failed") {
    retryLater("Connection lost. Reconnecting...");
  } else if (conn.connectionState === "disconnected") {
    setStatus("Connection unstable...", "warn");
    setTimeout(() => {
      if (conn === pc && conn.connectionState === "disconnected") retryLater("Connection lost. Reconnecting...");
    }, 4000);
  }
}

function retryLater(text) {
  if (!wantLive) return;
  setStatus(text, "warn");
  closeConnection();
  clearTimeout(retryTimer);
  retryTimer = setTimeout(publish, RETRY_MS);
}

function fail(text) {
  stop(null);
  setStatus(text, "bad");
}

function closeConnection() {
  clearInterval(statsTimer);
  if (resource) {
    fetch(resource, {method: "DELETE", headers: {"Authorization": basicAuth(creds)}, keepalive: true}).catch(() => {});
    resource = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
}

function stop(text = "Not streaming.") {
  wantLive = false;
  clearTimeout(retryTimer);
  closeConnection();
  dropSoundShare();
  disconnectSource("sys");
  if (display) {
    display.getTracks().forEach((t) => t.stop());
    display = null;
  }
  updateSoundButton();
  $("go").disabled = false;
  $("user").disabled = $("pass").disabled = false;
  $("stop").hidden = true;
  if (text !== null) setStatus(text);
}

// Only offer H.264: the delayed feeds and the archive copy the video as-is
function preferH264(transceiver) {
  if (!transceiver.setCodecPreferences || !window.RTCRtpSender || !RTCRtpSender.getCapabilities) return false;
  const codecs = RTCRtpSender.getCapabilities("video").codecs;
  const h264 = codecs.filter((c) => c.mimeType.toLowerCase() === "video/h264");
  if (!h264.length) return false;
  const score = (c) => (/packetization-mode=1/.test(c.sdpFmtpLine || "") ? 2 : 0) +
                       (/profile-level-id=42e0/.test(c.sdpFmtpLine || "") ? 1 : 0);
  h264.sort((a, b) => score(b) - score(a));
  const helpers = codecs.filter((c) => ["video/rtx", "video/red", "video/ulpfec"].includes(c.mimeType.toLowerCase()));
  try {
    transceiver.setCodecPreferences([...h264, ...helpers]);
    return true;
  } catch (e) {
    return false;
  }
}

// Fallback for browsers without setCodecPreferences: list H.264 first in the offer
function h264First(sdp) {
  const h264 = [...sdp.matchAll(/^a=rtpmap:(\d+) H264\/90000/gim)].map((m) => m[1]);
  return sdp.split("\r\n").map((line) => {
    if (!line.startsWith("m=video ")) return line;
    const parts = line.split(" ");
    const types = parts.slice(3);
    return [...parts.slice(0, 3), ...h264.filter((t) => types.includes(t)), ...types.filter((t) => !h264.includes(t))].join(" ");
  }).join("\r\n");
}

async function limitBitrate(sender) {
  try {
    const params = sender.getParameters();
    if (!params.encodings || !params.encodings.length) return;
    params.encodings[0].maxBitrate = MAX_BITRATE;
    params.encodings[0].maxFramerate = 60;
    params.degradationPreference = "maintain-resolution";
    await sender.setParameters(params);
  } catch (e) {
    console.warn("Couldn't set the bitrate", e);
  }
}

function iceGatheringDone(conn) {
  if (conn.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => { if (conn.iceGatheringState === "complete") resolve(); };
    conn.addEventListener("icegatheringstatechange", check);
    setTimeout(resolve, 2000);  // the server is publicly reachable, so local candidates are enough
  });
}

function startStats() {
  clearInterval(statsTimer);
  let last = null;
  statsTimer = setInterval(async () => {
    if (!pc) return;
    const report = await pc.getStats();
    let out = null;
    report.forEach((r) => { if (r.type === "outbound-rtp" && r.kind === "video") out = r; });
    if (!out) return;
    const codec = out.codecId && report.get(out.codecId) ? report.get(out.codecId).mimeType.split("/")[1] : "?";
    const mbps = last ? ((out.bytesSent - last.bytesSent) * 8) / ((out.timestamp - last.timestamp) * 1000) : 0;
    last = out;
    $("stats").textContent = `${out.frameWidth || "?"}x${out.frameHeight || "?"}, ${Math.round(out.framesPerSecond || 0)} fps, ` +
                             `${mbps.toFixed(1)} Mbps, ${codec}`;
  }, 2000);
}

// -- the tournament delay (players streaming to Twitch must delay their own stream at least as much) --

const FALLBACK_DELAY_MINUTES = 15;

async function showDelay() {
  let minutes = FALLBACK_DELAY_MINUTES;
  try {
    const res = await fetch("/go/delay", {cache: "no-store"});
    const data = await res.json();
    if (res.ok && Number(data.minutes) > 0) minutes = Number(data.minutes);
  } catch (e) { /* keep the fallback */ }
  const shown = Number.isInteger(minutes) ? `${minutes}` : minutes.toFixed(1).replace(/\.0$/, "");
  document.querySelectorAll(".delayMin").forEach((el) => { el.textContent = shown; });
  document.querySelectorAll(".delaySec").forEach((el) => { el.textContent = `${Math.ceil(minutes * 60)}`; });
}

// -- page wiring ----------------------------------------------------------------------------------

initLogin();
showLoggedIn(null);
updateSoundButton();
$("pickBrowser").addEventListener("click", () => choose("browser"));
$("pickObs").addEventListener("click", () => choose("obs"));
$("pickTwitch").addEventListener("click", () => choose("twitch"));
$("alsoDirect").addEventListener("click", () => { choose("browser"); $("browser").scrollIntoView({behavior: "smooth"}); });
$("twitchUse").addEventListener("click", () => setTwitch($("twitchChannel").value.trim()));
$("twitchOff").addEventListener("click", () => setTwitch(null));
$("watch").addEventListener("click", watchPreview);
$("unwatch").addEventListener("click", () => stopWatching());
$("loginForm").addEventListener("submit", logIn);
$("loginChange").addEventListener("click", changeLogin);
$("checkTest").addEventListener("click", () => { goToTest(); if (!watching) watchPreview(); $("checkTest").hidden = true; });
$("go").addEventListener("click", goLive);
$("stop").addEventListener("click", () => stop());
$("sysOn").addEventListener("change", () => { updateGains(); updateSoundButton(); });
$("addSys").addEventListener("click", addComputerSound);
$("sysVol").addEventListener("input", updateGains);
$("micVol").addEventListener("input", updateGains);
$("micOn").addEventListener("change", () => {
  if ($("micOn").checked) {
    openMic();
  } else {
    disconnectSource("mic");
    $("micControls").hidden = true;
  }
});
$("micDevice").addEventListener("change", () => openMic($("micDevice").value));
window.addEventListener("beforeunload", (e) => {
  if (wantLive) e.preventDefault();  // "Leave site?": closing the tab stops the stream
});
window.addEventListener("pagehide", () => { if (wantLive) closeConnection(); if (watching) stopWatching(); });
if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
  $("go").disabled = true;
  setStatus("This browser can't share the screen. Use Chrome or Edge on a computer, or OBS.", "bad");
}
requestAnimationFrame(drawMeters);
showDelay();
setInterval(showDelay, 60000);  // the delay can change during the event
