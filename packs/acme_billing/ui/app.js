/* Acme Billing's own front end, shipped in the pack and served at /app. It reads its branding
   (title, subtitle, accent, starter messages) from GET /channels/ag_ui and drives the
   conversation over the AG-UI SSE endpoint (POST /channels/ag_ui). Self-contained: no framework,
   no build step, no external request. A different pack ships a different one of these. */

const el = {
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  restart: document.getElementById("restart"),
  transcript: document.getElementById("transcript"),
  approval: document.getElementById("approval"),
  approvalTool: document.getElementById("approval-tool"),
  approvalPrompt: document.getElementById("approval-prompt"),
  suggestions: document.getElementById("suggestions"),
  composer: document.getElementById("composer"),
  message: document.getElementById("message"),
  send: document.getElementById("send"),
  note: document.getElementById("note"),
};

const state = { threadId: uuid(), suggestions: [], running: false, messages: {}, tool: null };

function uuid() {
  try {
    return crypto.randomUUID();
  } catch {
    return "id-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
  }
}

function note(text, warn) {
  el.note.textContent = text || "";
  el.note.classList.toggle("warn", Boolean(warn));
}

function say(author, text) {
  const li = document.createElement("li");
  li.className = "turn " + (author === "customer" ? "customer" : "agent");
  li.textContent = text;
  el.transcript.appendChild(li);
  li.scrollIntoView({ block: "end" });
}

function renderSuggestions() {
  el.suggestions.innerHTML = "";
  for (const suggestion of state.suggestions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = suggestion;
    button.addEventListener("click", () => send(suggestion));
    el.suggestions.appendChild(button);
  }
  el.suggestions.hidden = state.suggestions.length === 0;
}

function showApproval(tool, proposal) {
  el.approvalTool.textContent = tool || "a tool";
  el.approvalPrompt.textContent = proposal || "";
  el.approval.hidden = false;
}

async function loadInfo() {
  try {
    const resp = await fetch("/channels/ag_ui", { headers: { accept: "application/json" } });
    if (!resp.ok) throw new Error(`info ${resp.status}`);
    const info = await resp.json();
    const ui = info.ui || {};
    if (ui.title) el.title.textContent = ui.title;
    el.subtitle.textContent = ui.subtitle || `provider: ${info.provider || "?"}`;
    // The accent is a hex colour, validated in the pack manifest, so setting it as a CSS variable
    // is safe.
    if (ui.accent) document.documentElement.style.setProperty("--accent", ui.accent);
    // Prefer the pack's own suggestions; fall back to the deployment's.
    state.suggestions = (ui.suggestions && ui.suggestions.length ? ui.suggestions : info.suggestions) || [];
    renderSuggestions();
    note("Say something to begin.");
  } catch (err) {
    note(`Could not reach the service: ${err.message}`, true);
  }
}

function handleEvent(event) {
  switch (event.type) {
    case "RUN_STARTED":
      if (event.threadId) state.threadId = event.threadId;
      break;
    case "TEXT_MESSAGE_START":
      state.messages[event.messageId] = "";
      break;
    case "TEXT_MESSAGE_CONTENT":
      state.messages[event.messageId] = (state.messages[event.messageId] || "") + (event.delta || "");
      break;
    case "TEXT_MESSAGE_END":
      say("agent", state.messages[event.messageId] || "");
      delete state.messages[event.messageId];
      break;
    case "TOOL_CALL_START":
      state.tool = { name: event.toolCallName, args: "" };
      break;
    case "TOOL_CALL_ARGS":
      if (state.tool) state.tool.args += event.delta || "";
      break;
    case "TOOL_CALL_END":
      if (state.tool) {
        let proposal = "";
        try {
          proposal = JSON.parse(state.tool.args).proposal || "";
        } catch {
          proposal = "";
        }
        showApproval(state.tool.name, proposal);
        state.tool = null;
      }
      break;
    case "STATE_SNAPSHOT": {
      const awaiting = (event.snapshot || {}).awaiting;
      if (awaiting && awaiting.kind === "handoff") note("This conversation is now with a person.");
      break;
    }
    case "RUN_ERROR":
      note(event.message || "The run failed.", true);
      break;
    default:
      break;
  }
}

async function runTurn(text) {
  state.running = true;
  el.send.disabled = true;
  el.message.disabled = true;
  state.messages = {};
  state.tool = null;
  let resp;
  try {
    resp = await fetch("/channels/ag_ui", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify({
        threadId: state.threadId,
        runId: "run_" + uuid(),
        messages: [{ id: uuid(), role: "user", content: text }],
      }),
    });
  } catch (err) {
    note(`Could not start the run: ${err.message}`, true);
    state.running = false;
    el.send.disabled = el.message.disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    note(`Run failed (${resp.status}).`, true);
    state.running = false;
    el.send.disabled = el.message.disabled = false;
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, boundary).split("\n").find((l) => l.startsWith("data:"));
        buffer = buffer.slice(boundary + 2);
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line.slice(5).trim()));
        } catch {
          /* skip a malformed frame */
        }
      }
    }
  } finally {
    state.running = false;
    el.send.disabled = el.message.disabled = false;
  }
}

async function send(text) {
  if (state.running) return;
  const message = (text ?? el.message.value).trim();
  if (!message) return;
  el.message.value = "";
  el.approval.hidden = true;
  say("customer", message);
  await runTurn(message);
}

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

el.restart.addEventListener("click", () => {
  state.threadId = uuid();
  el.transcript.innerHTML = "";
  el.approval.hidden = true;
  note("New conversation. Say something to begin.");
});

loadInfo();
