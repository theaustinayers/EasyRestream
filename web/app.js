"use strict";
const container = document.getElementById("outputs");
const errorBox = document.getElementById("error");
const cards = new Map();
let pending = false;

async function request(path, body) {
  const response = await fetch(path, body ? {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Relay-Control": "1"},
    body: JSON.stringify(body)
  } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function draw(data) {
  for (const output of data.outputs) {
    const card = cards.get(output.name);
    card.state.textContent = output.state;
    card.state.dataset.state = output.state;
    card.info.textContent = `${output.configured ? "Destination saved" : "Not configured"} | Retries: ${output.retries}`;
    card.details.textContent = output.error;
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
    <label>Full destination URL including stream key
      <input type="password" autocomplete="off" spellcheck="false" placeholder="rtmps://host/app/STREAM_KEY">
    </label>
    <div class="buttons"><button class="save">Save URL</button><button class="clear">Clear URL</button>
      <button class="start">Start</button><button class="stop">Stop</button></div>
    <pre></pre>`;
  element.querySelector("h2").textContent = title;
  const card = {state: element.querySelector(".state"), info: element.querySelector(".info"),
    input: element.querySelector("input"), details: element.querySelector("pre"),
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
    draw(await request("/api/status"));
  } catch (error) {
    errorBox.textContent = `Dashboard unavailable: ${error.message}`;
    for (const element of container.querySelectorAll("button")) element.disabled = true;
  }
}
async function poll() {
  if (!pending) await refresh();
  setTimeout(poll, 2000);
}
poll();
