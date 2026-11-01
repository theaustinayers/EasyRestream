"use strict";
const container = document.getElementById("outputs");
const errorBox = document.getElementById("error");
const cards = new Map();
let pending = false;
let authorization = null;
let settingsReady = false;
let settingsSaving = false;
const inbound = document.getElementById("inbound");
const telemetry = document.getElementById("telemetry");

function number(value, suffix, digits = 2) {
  return value === null || value === undefined ? "Measuring..." : `${value.toFixed(digits)} ${suffix}`;
}

function drawStats(element, stats) {
  const values = [
    ["Payload bandwidth", number(stats.bandwidth_mbps, "Mbps")],
    ["Encoded bitrate", number(stats.bitrate_mbps, "Mbps")],
    ["Delivery FPS", number(stats.fps, "fps", 1)],
    ["Media FPS", number(stats.media_fps, "fps", 1)],
    ["Payload transferred", number(stats.total_bytes / 1e9, "GB", 3)],
    ["Last data", stats.sample_age_seconds === null ? "No data yet" : `${stats.sample_age_seconds.toFixed(1)}s ago`]
  ];
  element.replaceChildren();
  for (const [label, value] of values) {
    const metric = document.createElement("div");
    const title = document.createElement("span");
    const detail = document.createElement("strong");
    title.textContent = label;
    detail.textContent = value;
    metric.append(title, detail);
    element.append(metric);
  }
}

async function request(path, body) {
  const headers = authorization ? {Authorization: authorization} : {};
  const options = {headers};
  if (body) {
    Object.assign(headers, {"Content-Type": "application/json", "X-Relay-Control": "1"});
    Object.assign(options, {method: "POST", body: JSON.stringify(body)});
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function basicAuthorization(username, password) {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  return "Basic " + btoa(String.fromCharCode(...bytes));
}

async function loadSettings(username) {
  const data = await request("/api/ingest");
  document.getElementById("account-username").value = username;
  document.getElementById("ingest-server").value = data.server;
  document.getElementById("ingest-key").value = data.stream_key;
  document.getElementById("ingest-url").value = data.url;
  document.getElementById("ingest-note").textContent = data.configured
    ? "Use OBS server URL and stream key in their separate OBS fields."
    : "Optional: replace YOUR_SERVER_IP with your server's public IP/hostname so the full URL is correct. The stream key can be changed either way.";
  settingsReady = true;
}

document.getElementById("reveal-ingest").onclick = () => {
  const show = document.getElementById("ingest-key").type === "password";
  for (const id of ["ingest-key", "ingest-url"]) document.getElementById(id).type = show ? "text" : "password";
  document.getElementById("reveal-ingest").textContent = show ? "Hide key and URL" : "Show key and URL";
};

document.getElementById("save-account").onclick = async () => {
  if (settingsSaving) return;
  const button = document.getElementById("save-account");
  const message = document.getElementById("account-message");
  const username = document.getElementById("account-username").value.trim();
  const current = document.getElementById("account-current-password").value;
  const password = document.getElementById("account-password").value;
  if (password && (password.length < 8 || password.length > 256)) {
    message.textContent = "Password must be 8-256 characters.";
    return;
  }
  if (password !== document.getElementById("account-confirm-password").value) {
    message.textContent = "New passwords do not match.";
    return;
  }
  settingsSaving = true;
  button.disabled = true;
  try {
    await request("/api/account", {username, password, current_password: current});
    authorization = basicAuthorization(username, password || current);
    for (const id of ["account-current-password", "account-password", "account-confirm-password"]) {
      document.getElementById(id).value = "";
    }
    message.textContent = "Account saved. This page uses the new login immediately; on your next visit, sign in with the new credentials.";
    errorBox.textContent = "";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    settingsSaving = false;
    button.disabled = false;
  }
};

document.getElementById("save-ingest").onclick = async () => {
  if (settingsSaving) return;
  const button = document.getElementById("save-ingest");
  const message = document.getElementById("ingest-message");
  const key = document.getElementById("new-ingest-key").value;
  const currentPassword = document.getElementById("ingest-password").value;
  if (key && !/^[A-Za-z0-9_-]{1,128}$/.test(key)) {
    message.textContent = "Stream key must be 1-128 letters, digits, underscores or hyphens.";
    errorBox.textContent = message.textContent;
    return;
  }
  if (!currentPassword) {
    message.textContent = "Enter your current dashboard password to save ingest settings.";
    errorBox.textContent = message.textContent;
    return;
  }
  settingsSaving = true;
  button.disabled = true;
  message.textContent = "Saving ingest settings...";
  errorBox.textContent = "";
  try {
    await request("/api/ingest", {
      server: document.getElementById("ingest-server").value.trim(),
      stream_key: key,
      current_password: currentPassword
    });
    document.getElementById("new-ingest-key").value = "";
    document.getElementById("ingest-password").value = "";
    await loadSettings(document.getElementById("account-username").value);
    message.textContent = "Ingest settings saved. Use the displayed server URL and key in OBS.";
  } catch (error) {
    message.textContent = error.message;
    errorBox.textContent = error.message;
  } finally {
    settingsSaving = false;
    button.disabled = false;
  }
};

function draw(data) {
  telemetry.textContent = `Telemetry live | Updated ${new Date().toLocaleTimeString()} | Rates averaged over approximately 5 seconds`;
  inbound.querySelector(".state").textContent = data.inbound.state;
  inbound.querySelector(".state").dataset.state = data.inbound.state;
  inbound.querySelector("pre").textContent = data.inbound.error;
  drawStats(inbound.querySelector(".stats"), data.inbound.stats);
  for (const output of data.outputs) {
    const card = cards.get(output.name);
    card.state.textContent = output.state;
    card.state.dataset.state = output.state;
    card.info.textContent = `${output.configured ? "Destination saved" : "Not configured"} | Retries: ${output.retries}`;
    card.details.textContent = output.error;
    drawStats(card.stats, output.stats);
    const active = !["stopped", "error"].includes(output.state);
    card.start.disabled = pending || active || !output.configured;
    card.stop.disabled = pending || !active;
    card.save.disabled = pending || active;
    card.clear.disabled = pending || active || !output.configured;
    card.input.disabled = pending || active;
  }
}

async function act(name, action, url) {
  pending = true;
  for (const element of container.querySelectorAll("button")) element.disabled = true;
  try {
    await request("/api/action", {name, action, ...(url !== undefined ? {url} : {})});
    if (action === "configure") cards.get(name).input.value = "";
    errorBox.textContent = "";
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    pending = false;
    await refresh();
  }
}

for (const [name, title] of [["youtube", "YouTube"], ["facebook", "Facebook"], ["x", "X"]]) {
  const element = document.createElement("article");
  element.innerHTML = `<div class="heading"><h2></h2><span class="state">Loading</span></div>
    <p class="info"></p>
    <div class="stats"></div>
    <label>Full destination URL including stream key
      <input type="password" autocomplete="off" spellcheck="false" placeholder="rtmps://host/app/STREAM_KEY">
    </label>
    <div class="buttons"><button class="save">Save URL</button><button class="clear">Clear URL</button>
      <button class="start">Start</button><button class="stop">Stop</button></div>
    <pre></pre>`;
  element.querySelector("h2").textContent = title;
  const card = {state: element.querySelector(".state"), info: element.querySelector(".info"),
    input: element.querySelector("input"), details: element.querySelector("pre"),
    stats: element.querySelector(".stats"),
    save: element.querySelector(".save"), clear: element.querySelector(".clear"),
    start: element.querySelector(".start"), stop: element.querySelector(".stop")};
  card.save.onclick = () => {
    const url = card.input.value.trim();
    if (!url) { errorBox.textContent = "Enter a URL, or use Clear URL to remove the saved destination."; return; }
    act(name, "configure", url);
  };
  card.clear.onclick = () => act(name, "configure", "");
  card.start.onclick = () => act(name, "start");
  card.stop.onclick = () => act(name, "stop");
  cards.set(name, card);
  container.append(element);
}

async function refresh() {
  try {
    const data = await request("/api/status");
    draw(data);
    if (!settingsReady) await loadSettings(data.username);
  } catch (error) {
    errorBox.textContent = `Dashboard unavailable: ${error.message}`;
    telemetry.textContent = "Telemetry unavailable; displayed values are stale.";
    for (const element of container.querySelectorAll("button")) element.disabled = true;
  }
}
async function poll() {
  if (!pending && !settingsSaving) await refresh();
  setTimeout(poll, 1000);
}
poll();
