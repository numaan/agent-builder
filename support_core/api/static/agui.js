/* An AG-UI client for the support-core demo. Speaks the AG-UI protocol over SSE
   against POST /channels/ag_ui. (See the server module support_core/channels/ag_ui.py
   for the protocol reference; this served file carries no external URL, so the demo
   works on a laptop with no route to the internet.)

   A run is one turn: POST the latest user message, then read a text/event-stream of
   AG-UI events (RUN_STARTED, TEXT_MESSAGE_*, TOOL_CALL_* for the approval gate,
   STATE_SNAPSHOT, RUN_FINISHED). The conversation is carried by `threadId`, which is
   the web chat session key - kept in the body, never the URL - and persisted so a
   reload continues the same conversation. No framework, no build step, no network
   beyond this service. */

const el = {
  dot: document.getElementById("link-dot"),
  pack: document.getElementById("pack-name"),
  provider: document.getElementById("provider-tag"),
  status: document.getElementById("run-status"),
  restart: document.getElementById("restart"),
  transcript: document.getElementById("transcript"),
  approval: document.getElementById("approval"),
  approvalTool: document.getElementById("approval-tool-name"),
  approvalPrompt: document.getElementById("approval-prompt"),
  suggestions: document.getElementById("suggestions"),
  composer: document.getElementById("composer"),
  message: document.getElementById("message"),
  send: document.getElementById("send"),
  note: document.getElementById("note"),
  session: document.getElementById("session-key"),
};

const STORAGE_KEY = "support-core.agui.thread";
const state = { threadId: loadThread(), suggestions: [], running: false };

function loadThread() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

function saveThread(id) {
  state.threadId = id;
  el.session.textContent = id || "-";
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private window or blocked storage: the thread lives for this page load only. */
  }
}

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

function setStatus(status) {
  el.status.textContent = status || "idle";
}

function say(author, text) {
  const li = document.createElement("li");
  li.className = "turn " + (author === "customer" ? "customer" : author === "human" ? "human" : "agent");
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

function hideApproval() {
  el.approval.hidden = true;
}

function setBusy(busy) {
  state.running = busy;
  el.send.disabled = busy;
  el.message.disabled = busy;
  el.dot.dataset.state = busy ? "connecting" : "open";
}

async function loadInfo() {
  try {
    const resp = await fetch("/channels/ag_ui", { headers: { accept: "application/json" } });
    if (!resp.ok) throw new Error(`info ${resp.status}`);
    const info = await resp.json();
    el.pack.textContent = info.pack || "support-core";
    el.provider.textContent = `provider: ${info.provider || "?"}`;
    state.suggestions = info.suggestions || [];
    renderSuggestions();
    el.dot.dataset.state = "open";
    el.session.textContent = state.threadId || "-";
    note(state.threadId ? "continuing this thread - say something" : "connected - say something to begin");
  } catch (err) {
    el.dot.dataset.state = "closed";
    note(`could not reach the service: ${err.message}`, true);
  }
}

function handleEvent(event) {
  switch (event.type) {
    case "RUN_STARTED":
      if (event.threadId) saveThread(event.threadId);
      setStatus("running");
      break;
    case "TEXT_MESSAGE_START":
      state.messages[event.messageId] = "";
      break;
    case "TEXT_MESSAGE_CONTENT":
      state.messages[event.messageId] = (state.messages[event.messageId] || "") + (event.delta || "");
      break;
    case "TEXT_MESSAGE_END": {
      const text = state.messages[event.messageId] || "";
      delete state.messages[event.messageId];
      say("agent", text);
      break;
    }
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
      const snapshot = event.snapshot || {};
      setStatus(snapshot.status || "idle");
      const awaiting = snapshot.awaiting;
      if (awaiting && awaiting.kind === "handoff") {
        note("this conversation is now with a person at the desk");
      }
      break;
    }
    case "RUN_FINISHED":
      note("");
      break;
    case "RUN_ERROR":
      note(event.message || "the run failed", true);
      break;
    default:
      break;
  }
}

async function runTurn(text) {
  setBusy(true);
  setStatus("running");
  state.messages = {};
  state.tool = null;
  const body = {
    threadId: state.threadId || undefined,
    runId: "run_" + uuid(),
    messages: [{ id: uuid(), role: "user", content: text }],
  };
  let resp;
  try {
    resp = await fetch("/channels/ag_ui", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    note(`could not start the run: ${err.message}`, true);
    setBusy(false);
    return;
  }
  if (!resp.ok || !resp.body) {
    let detail = `run failed (${resp.status})`;
    try {
      detail = (await resp.json()).error || detail;
    } catch {
      /* not JSON */
    }
    note(detail, true);
    setBusy(false);
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
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line.slice(5).trim()));
        } catch {
          /* a malformed frame is skipped rather than breaking the stream. */
        }
      }
    }
  } finally {
    setBusy(false);
  }
}

async function send(text) {
  if (state.running) return;
  const message = (text ?? el.message.value).trim();
  if (!message) return;
  el.message.value = "";
  hideApproval();
  say("customer", message);
  await runTurn(message);
}

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

el.restart.addEventListener("click", () => {
  saveThread(null);
  el.transcript.innerHTML = "";
  hideApproval();
  saveThread(uuid());
  note("new conversation - say something to begin");
});

// A fresh thread id if none was stored, so the first run continues a known thread.
if (!state.threadId) saveThread(uuid());
loadInfo();
