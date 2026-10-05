"use strict";
// The players' "go live" page (served at /go/): they choose between streaming from this browser
// and OBS. In the browser it shares the screen and publishes it to MediaMTX over WHIP (WebRTC) as
// H.264 video plus one audio track that mixes the computer sound and, optionally, a microphone.
// Nothing here touches the camera. The team comes from the login (team05-p1 -> team05); MediaMTX is
// on the same site, so its WHIP endpoint is /<team path>/whip, and it checks the login with the
// control service like any publish. For OBS the page only shows instructions.

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

function choose(mode) {
  if (wantLive && mode !== "browser") return;  // stop streaming here first
  $("browser").hidden = mode !== "browser";
  $("obs").hidden = mode !== "obs";
  $("pickBrowser").setAttribute("aria-pressed", String(mode === "browser"));
  $("pickObs").setAttribute("aria-pressed", String(mode === "obs"));
}

function basicAuth(c) {
  return "Basic " + btoa(String.fromCharCode(...new TextEncoder().encode(`${c.user}:${c.pass}`)));
}

// -- status -------------------------------------------------------------------------------------

function setStatus(text, kind = "") {
  $("statusText").textContent = text;
  $("status").className = kind;
  if (kind !== "live") $("stats").textContent = "";
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
  const c = credentials();
  if (!c.user || !c.pass) {
    setStatus("Enter your login and password first.", "bad");
    return;
  }
  const team = LOGIN_RE.exec(c.user);
  if (!team) {
    setStatus("That isn't a player login: they look like team05-p1.", "bad");
    return;
  }
  creds = c;
  whipUrl = new URL(`/${team[1]}/whip`, location.origin).href;
  try { sessionStorage.setItem(LOGIN_KEY, JSON.stringify(c)); } catch (e) { /* private mode */ }
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
  $("user").disabled = $("pass").disabled = $("pickObs").disabled = true;
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
  $("user").disabled = $("pass").disabled = $("pickObs").disabled = false;
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

// -- page wiring ----------------------------------------------------------------------------------

initLogin();
updateSoundButton();
$("pickBrowser").addEventListener("click", () => choose("browser"));
$("pickObs").addEventListener("click", () => choose("obs"));
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
window.addEventListener("pagehide", () => { if (wantLive) closeConnection(); });
if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
  $("go").disabled = true;
  setStatus("This browser can't share the screen. Use Chrome or Edge on a computer, or OBS.", "bad");
}
requestAnimationFrame(drawMeters);
