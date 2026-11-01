"use strict";
const container = document.getElementById("outputs");
const errorBox = document.getElementById("error");
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

const PLATFORMS = [
  ["youtube", "YouTube", "rtmp://a.rtmp.youtube.com/live2"],
  ["facebook", "Facebook", "rtmps://live-api-s.facebook.com:443/rtmp"],
  ["x", "X", ""]
];
const groups = new Map();
const rows = new Map();
let saved = new Map();
let rowOrder = null;

function health(state) {
  if (state === "forwarding") return ["green", "Healthy: forwarding"];
  if (state === "connecting") return ["yellow", "Connecting"];
  if (state === "stalled") return ["yellow", "Warning: no data for over 5 seconds"];
  if (state === "stopped") return ["off", "Stopped"];
  if (state === "waiting") return ["red", "No OBS input"];
  return ["red", state === "retrying" ? "Failed: retrying" : "Error"];
}

function showState(stateElement, lightElement, state) {
  const [color, text] = health(state);
  stateElement.textContent = state;
  stateElement.dataset.state = state;
  lightElement.dataset.health = color;
  lightElement.title = text;
  lightElement.setAttribute("aria-label", text);
}

// Splits rtmp(s)://host/app/KEY into server and key; the server must keep at least one path segment.
function splitFullUrl(value) {
  const match = value.trim().match(/^(rtmps?:\/\/[^\/?#\s]+\/[^?#\s]*[^\/?#\s])\/([^\/?#\s]+(?:\?[^#\s]*)?)$/i);
  return match ? [match[1], match[2]] : null;
}

function maskKey(key) {
  return key ? "\u2022".repeat(Math.min(Math.max(key.length, 8), 24)) : "";
}

async function loadDestinations() {
  const data = await request("/api/destinations");
  saved = new Map(data.destinations.map(item => [item.id, item]));
  for (const [id, row] of rows) {
    const item = saved.get(id);
    if (item) row.key.textContent = row.revealed ? item.key : maskKey(item.key);
  }
}

function openEditor(group, item) {
  group.editing = item ? item.id : null;
  group.title.textContent = `${item ? "Edit" : "Add"} ${group.label} destination`;
  group.name.value = item ? item.name : "";
  group.server.value = item ? item.server : group.defaultServer;
  group.key.value = item ? item.key : "";
  group.key.type = "password";
  group.reveal.textContent = "Show key";
  group.message.textContent = "";
  group.form.hidden = false;
  group.add.disabled = true;
  (item ? group.name : group.server).focus();
}

function closeEditor(group) {
  group.form.hidden = true;
  group.editing = null;
  group.key.value = "";
  group.add.disabled = false;
}

function trySplit(group) {
  const parts = splitFullUrl(group.server.value);
  if (!parts || group.key.value) return;
  [group.server.value, group.key.value] = parts;
  group.message.textContent = "Full URL detected: split into server URL and stream key.";
}

for (const [platform, label, defaultServer] of PLATFORMS) {
  const element = document.createElement("article");
  element.className = "platform";
  element.innerHTML = `<div class="heading"><h2></h2><button type="button" class="add">Add destination</button></div>
    <p class="info count"></p>
    <div class="rows"></div>
    <form class="editor" hidden>
      <h3></h3>
      <label>Name<input class="name" maxlength="64" placeholder="e.g. Main channel"></label>
      <label>Server URL<input class="server" spellcheck="false" autocomplete="off"></label>
      <p class="info">Paste the full stream URL here to split it into server URL and stream key automatically.</p>
      <div class="keyrow"><label>Stream key<input class="key" type="password" spellcheck="false" autocomplete="off" maxlength="512"></label>
        <button type="button" class="reveal">Show key</button></div>
      <div class="buttons"><button type="submit" class="start">Save destination</button><button type="button" class="cancel">Cancel</button></div>
      <p class="message" role="status"></p>
    </form>`;
  element.querySelector("h2").textContent = label;
  const group = {platform, label, defaultServer, editing: null,
    add: element.querySelector(".add"), count: element.querySelector(".count"),
    rows: element.querySelector(".rows"), form: element.querySelector("form"),
    title: element.querySelector("h3"), name: element.querySelector(".name"),
    server: element.querySelector(".server"), key: element.querySelector(".key"),
    reveal: element.querySelector(".reveal"), message: element.querySelector(".message")};
  group.server.placeholder = defaultServer || "rtmps://host/app (from X Media Studio)";
  group.add.onclick = () => openEditor(group, null);
  group.form.querySelector(".cancel").onclick = () => closeEditor(group);
  group.server.addEventListener("paste", () => setTimeout(() => trySplit(group)));
  group.server.addEventListener("change", () => trySplit(group));
  group.reveal.onclick = () => {
    const show = group.key.type === "password";
    group.key.type = show ? "text" : "password";
    group.reveal.textContent = show ? "Hide key" : "Show key";
  };
  group.form.onsubmit = async event => {
    event.preventDefault();
    if (pending) return;
    trySplit(group);
    if (!group.server.value.trim() || !group.key.value.trim()) {
      group.message.textContent = "Enter both the server URL and the stream key.";
      return;
    }
    pending = true;
    group.message.textContent = "Saving...";
    try {
      await request("/api/destination", {id: group.editing || "", platform, name: group.name.value,
        server: group.server.value, key: group.key.value});
      closeEditor(group);
      await loadDestinations();
      errorBox.textContent = "";
    } catch (error) {
      group.message.textContent = error.message;
    } finally {
      pending = false;
      await refresh();
    }
  };
  groups.set(platform, group);
  container.append(element);
}

function createRow(output) {
  const element = document.createElement("div");
  element.className = "destination";
  element.innerHTML = `<div class="heading"><div class="title"><span class="light" role="img"></span><strong class="label"></strong></div>
      <span class="state"></span></div>
    <p class="info target"></p>
    <div class="keyline"><span class="info">Stream key:</span> <code class="key"></code><button type="button" class="reveal-key">Show key</button></div>
    <div class="stats"></div>
    <div class="buttons"><button type="button" class="start">Start</button><button type="button" class="stop">Stop</button>
      <button type="button" class="edit">Edit</button><button type="button" class="delete">Delete</button></div>
    <pre></pre>`;
  const row = {element, revealed: false, light: element.querySelector(".light"),
    label: element.querySelector(".label"), state: element.querySelector(".state"),
    target: element.querySelector(".target"), key: element.querySelector(".key"),
    revealKey: element.querySelector(".reveal-key"), stats: element.querySelector(".stats"),
    start: element.querySelector(".start"), stop: element.querySelector(".stop"),
    edit: element.querySelector(".edit"), remove: element.querySelector(".delete"),
    details: element.querySelector("pre")};
  const id = output.id;
  row.start.onclick = () => act("/api/action", {id, action: "start"});
  row.stop.onclick = () => act("/api/action", {id, action: "stop"});
  row.edit.onclick = () => {
    const item = saved.get(id);
    if (item) openEditor(groups.get(item.platform), item);
  };
  row.remove.onclick = () => {
    const item = saved.get(id);
    if (item && confirm(`Delete destination "${item.name}"?`)) act("/api/destination/delete", {id});
  };
  row.revealKey.onclick = () => {
    row.revealed = !row.revealed;
    row.revealKey.textContent = row.revealed ? "Hide key" : "Show key";
    const item = saved.get(id);
    row.key.textContent = item ? (row.revealed ? item.key : maskKey(item.key)) : "";
  };
  return row;
}

function draw(data) {
  telemetry.textContent = `Telemetry live | Updated ${new Date().toLocaleTimeString()} | Rates averaged over approximately 5 seconds`;
  showState(inbound.querySelector(".state"), inbound.querySelector(".light"), data.inbound.state);
  inbound.querySelector("pre").textContent = data.inbound.error;
  drawStats(inbound.querySelector(".stats"), data.inbound.stats);
  const order = data.outputs.map(output => output.id).join(",");
  if (order !== rowOrder) {
    for (const [id, row] of rows) {
      if (!data.outputs.some(output => output.id === id)) { row.element.remove(); rows.delete(id); }
    }
    for (const output of data.outputs) {
      if (!rows.has(output.id)) rows.set(output.id, createRow(output));
      groups.get(output.platform).rows.append(rows.get(output.id).element);
    }
    rowOrder = order;
    loadDestinations().catch(error => { errorBox.textContent = error.message; });
  }
  for (const group of groups.values()) {
    const count = data.outputs.filter(output => output.platform === group.platform).length;
    group.count.textContent = count ? `${count} saved destination${count === 1 ? "" : "s"}` : "No saved destinations. Use Add destination.";
  }
  for (const output of data.outputs) {
    const row = rows.get(output.id);
    showState(row.state, row.light, output.state);
    row.label.textContent = output.label;
    row.target.textContent = `Server: ${output.server} | Retries: ${output.retries}`;
    if (!saved.has(output.id)) row.key.textContent = "";
    row.details.textContent = output.error;
    drawStats(row.stats, output.stats);
    const active = !["stopped", "error"].includes(output.state);
    const editing = groups.get(output.platform).editing === output.id;
    row.start.disabled = pending || active || editing;
    row.stop.disabled = pending || !active;
    row.edit.disabled = pending || active;
    row.remove.disabled = pending || active || editing;
  }
}

async function act(path, body) {
  pending = true;
  for (const element of container.querySelectorAll(".destination button:not(.reveal-key)")) element.disabled = true;
  try {
    await request(path, body);
    errorBox.textContent = "";
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    pending = false;
    await refresh();
  }
}

async function refresh() {
  try {
    const data = await request("/api/status");
    draw(data);
    if (!settingsReady) await loadSettings(data.username);
  } catch (error) {
    errorBox.textContent = `Dashboard unavailable: ${error.message}`;
    telemetry.textContent = "Telemetry unavailable; displayed values are stale.";
    for (const element of container.querySelectorAll(".destination button")) element.disabled = true;
  }
}

async function poll() {
  if (!pending && !settingsSaving) await refresh();
  setTimeout(poll, 1000);
}
poll();
