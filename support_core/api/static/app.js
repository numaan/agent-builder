/* The demo client for the web chat channel (DESIGN.md section 12).

   Not a product interface: enough to hold a conversation and watch it work, and deliberately
   explicit about the two things the design is really demonstrating.

   1. The conversation is identified by a *session key*, never by this connection. The key is
      kept in localStorage, sent in the socket's opening `hello` frame, and reused after a
      reload, a dropped network or a server restart - which is how a conversation suspended
      waiting for the customer (DESIGN.md 7.2) resumes on a different connection.

      In a frame and *never* in the URL, in both directions. The key is the whole of this
      channel's access control, so a query string is two holes at once: it is written verbatim
      into every access log between the browser and the app, and a link someone was sent could
      seat their conversation on a key its sender already holds. The security review of
      2026-09-07 demonstrated the second one - it read a customer's own message back from a
      second connection - so this page ignores a `session` parameter and strips it from the
      address bar rather than honouring it (review finding W9, and its unfixed half).
   2. A confirmation is not an ordinary message. When the run is waiting on a `confirm` node the
      page says so loudly, names the action, and shows the exact proposal the approval is bound
      to. Nothing moves until the customer answers.

   Everything shown here is text the server already committed. Nothing is streamed from a model
   before the turn that produced it was written down. */

"use strict";

const SESSION_STORAGE_KEY = "support-core.web-chat.session";

const el = {
  transcript: document.getElementById("transcript"),
  approval: document.getElementById("approval"),
  approvalTool: document.getElementById("approval-tool-name"),
  approvalPrompt: document.getElementById("approval-prompt"),
  suggestions: document.getElementById("suggestions"),
  composer: document.getElementById("composer"),
  message: document.getElementById("message"),
  send: document.getElementById("send"),
  note: document.getElementById("note"),
  status: document.getElementById("run-status"),
  dot: document.getElementById("link-dot"),
  pack: document.getElementById("pack-name"),
  provider: document.getElementById("provider-tag"),
  sessionKey: document.getElementById("session-key"),
  hint: document.getElementById("hint"),
  restart: document.getElementById("restart"),
};

const state = {
  socket: null,
  session: null,
  lastAgentMessage: "",
  suggestions: [],
  backoff: 250,
  closing: false,
  fatal: false,
};

function discardSessionInUrl() {
  /* A `session` parameter is not a way to resume a conversation, it is a way to be handed
     someone else's. Drop it before anything reads it, and take it out of the address bar so it
     stops travelling in history, bookmarks and referrers. */
  const params = new URLSearchParams(window.location.search);
  if (!params.has("session")) {
    return;
  }
  params.delete("session");
  const query = params.toString();
  const clean = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
  try {
    window.history.replaceState(null, "", clean);
  } catch (error) {
    /* nothing to do: some embeddings forbid it, and the key is ignored either way */
  }
}

function readSession() {
  /* localStorage only. See the note at the top of this file on why not the URL. */
  try {
    return window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch (error) {
    return null; /* private window, or storage blocked: the server assigns one instead */
  }
}

function rememberSession(session) {
  state.session = session;
  el.sessionKey.textContent = session;
  try {
    window.localStorage.setItem(SESSION_STORAGE_KEY, session);
  } catch (error) {
    /* nothing to do: the key lives for this connection only */
  }
}

function socketUrl() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/channels/web_chat/ws`;
}

/* Three authors, not two. `human` is a reply typed by a person at the desk, which phase 6 keeps
   distinguishable from the model's words on purpose; rendering it as the customer's own message
   put a support agent on the wrong side of the transcript (review finding W11). Only an `agent`
   message becomes `lastAgentMessage`, so the approval panel keeps quoting the proposal the
   approval is actually bound to even after a human has said something. */
function authorClass(author) {
  if (author === "agent") {
    return "agent";
  }
  return author === "customer" ? "customer" : "human";
}

function say(author, text, pending) {
  const line = document.createElement("li");
  line.className = `turn ${authorClass(author)}${pending ? " pending" : ""}`;
  line.textContent = text;
  el.transcript.appendChild(line);
  line.scrollIntoView({ block: "nearest" });
  if (author === "agent") {
    state.lastAgentMessage = text;
  }
}

function note(text, warn) {
  el.note.textContent = text || "";
  el.note.className = warn ? "note warn" : "note";
}

function setStatus(status) {
  el.status.textContent = status;
  const stuck = status === "waiting_human";
  el.hint.textContent = stuck
    ? "this conversation is now with a person; start a new one to keep exploring"
    : "";
}

function renderApproval(awaiting) {
  const confirming = Boolean(awaiting) && awaiting.kind === "confirm";
  el.approval.hidden = !confirming;
  if (confirming) {
    el.approvalTool.textContent = awaiting.tool || "an action";
    el.approvalPrompt.textContent = state.lastAgentMessage;
  }
}

function renderSuggestions() {
  el.suggestions.textContent = "";
  el.suggestions.hidden = state.suggestions.length === 0;
  for (const suggestion of state.suggestions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = suggestion;
    button.addEventListener("click", () => send(suggestion));
    el.suggestions.appendChild(button);
  }
}

function renderHistory(history) {
  el.transcript.textContent = "";
  state.lastAgentMessage = "";
  for (const entry of history || []) {
    say(entry.author, entry.text, entry.pending);
  }
}

function send(text) {
  const message = (text === undefined ? el.message.value : text).trim();
  if (!message) {
    return;
  }
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    note("not connected yet - the message was not sent", true);
    return;
  }
  say("customer", message, false);
  state.socket.send(JSON.stringify({ type: "message", text: message }));
  el.message.value = "";
  note("thinking...");
}

function onEvent(event) {
  switch (event.type) {
    case "ready":
      rememberSession(event.session);
      el.pack.textContent = event.pack;
      el.provider.textContent = `provider: ${event.provider}`;
      state.suggestions = event.suggestions || [];
      renderSuggestions();
      renderHistory(event.history);
      setStatus(event.status);
      renderApproval(event.awaiting);
      note(
        event.history && event.history.length
          ? "reconnected to this conversation"
          : "connected - say something to begin",
      );
      break;
    case "message":
      say(event.author, event.text, false);
      break;
    case "turn":
      /* Broadcast to every connection on this conversation, not only to the one that sent the
         message (review finding W3), so a second tab raises the approval panel too. */
      setStatus(event.status);
      renderApproval(event.awaiting);
      note("");
      break;
    case "queued":
      /* Per-caller, so it is its own frame rather than a field on `turn`. */
      note("another turn is running on this conversation; yours is queued and will run in order");
      break;
    case "error":
      note(event.detail, true);
      if (event.fatal) {
        /* The server has said this connection is over and why. Reconnecting with the same bad
           key reproduces it for ever, five seconds apart (review finding W15). */
        state.fatal = true;
        state.closing = true;
      }
      break;
    default:
      break;
  }
}

function connect() {
  el.dot.dataset.state = "connecting";
  const socket = new WebSocket(socketUrl());
  state.socket = socket;

  socket.addEventListener("open", () => {
    el.dot.dataset.state = "open";
    state.backoff = 250;
    /* The opening frame names the conversation. Omitting the key asks the server for a new one,
       which is what a first visit sends. */
    socket.send(
      JSON.stringify(state.session ? { type: "hello", session: state.session } : { type: "hello" }),
    );
  });
  socket.addEventListener("message", (frame) => {
    let event;
    try {
      event = JSON.parse(frame.data);
    } catch (error) {
      return;
    }
    onEvent(event);
  });
  socket.addEventListener("close", () => {
    el.dot.dataset.state = "closed";
    if (state.fatal) {
      setStatus("disconnected");
      return;
    }
    if (state.closing) {
      return;
    }
    setStatus("disconnected");
    note("connection lost - reconnecting to the same conversation");
    window.setTimeout(connect, state.backoff);
    state.backoff = Math.min(state.backoff * 2, 5000);
  });
}

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

el.restart.addEventListener("click", () => {
  try {
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch (error) {
    /* nothing to do */
  }
  state.session = null;
  state.closing = true;
  state.fatal = false;
  if (state.socket) {
    state.socket.close();
  }
  el.transcript.textContent = "";
  el.approval.hidden = true;
  window.setTimeout(() => {
    state.closing = false;
    connect();
  }, 50);
});

discardSessionInUrl();
state.session = readSession();
if (state.session) {
  el.sessionKey.textContent = state.session;
}
connect();
