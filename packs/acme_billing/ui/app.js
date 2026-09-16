/* Acme Billing's own front end, shipped in the pack and served at /app. It reads its branding
   (title, subtitle, accent, starter messages) from GET /channels/ag_ui and drives the
   conversation over the AG-UI SSE endpoint (POST /channels/ag_ui). Self-contained: no framework,
   no build step, no external request. A different pack ships a different one of these. */

const el = {
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  restart: document.getElementById("restart"),
  transcript: document.getElementById("transcript"),
  scroll: document.getElementById("scroll"),
  approval: document.getElementById("approval"),
  approvalTool: document.getElementById("approval-tool"),
  approvalPrompt: document.getElementById("approval-prompt"),
  formHost: document.getElementById("form-host"),
  suggestions: document.getElementById("suggestions"),
  composer: document.getElementById("composer"),
  message: document.getElementById("message"),
  send: document.getElementById("send"),
  note: document.getElementById("note"),
};

const state = {
  threadId: uuid(),
  suggestions: [],
  running: false,
  messages: {},
  tool: null,
  thinkingEl: null,
};

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

function scrollToBottom() {
  el.scroll.scrollTop = el.scroll.scrollHeight;
}

// A Claude-style "working" indicator: a bubble of pulsing dots shown while the agent runs, cleared
// the moment any real output (a message, a form, or an approval) arrives.
function showThinking() {
  removeThinking();
  const li = document.createElement("li");
  li.className = "turn agent thinking";
  li.setAttribute("aria-label", "Assistant is working");
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("span");
    dot.className = "dot";
    li.appendChild(dot);
  }
  el.transcript.appendChild(li);
  state.thinkingEl = li;
  scrollToBottom();
}

function removeThinking() {
  if (state.thinkingEl) {
    state.thinkingEl.remove();
    state.thinkingEl = null;
  }
}

function say(author, text) {
  removeThinking();
  const li = document.createElement("li");
  li.className = "turn " + (author === "customer" ? "customer" : "agent");
  li.textContent = text;
  el.transcript.appendChild(li);
  scrollToBottom();
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
  removeThinking();
  el.approvalTool.textContent = tool || "a tool";
  el.approvalPrompt.textContent = proposal || "";
  el.approval.hidden = false;
  el.approval.scrollIntoView({ block: "nearest" });
}

// Generative UI: build a form from a render_form tool call's schema, validate it, and send the
// values back as the next message. Labels come from the trusted pack, and every value goes in as
// textContent, so there is no injection from the rendered form.
function buildField(field) {
  const wrap = document.createElement("div");
  wrap.className = "fld";
  wrap.dataset.key = field.key;
  const id = "pf_" + field.key;
  const addLabel = (forId, text) => {
    const l = document.createElement("label");
    l.className = "l";
    if (forId) l.setAttribute("for", forId);
    l.textContent = text;
    if (field.required) {
      const s = document.createElement("span");
      s.className = "req";
      s.textContent = "*";
      l.appendChild(s);
    }
    wrap.appendChild(l);
  };
  if (field.type === "select") {
    addLabel(id, field.label);
    const sel = document.createElement("select");
    sel.id = id;
    for (const o of field.options || []) {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      sel.appendChild(opt);
    }
    wrap.appendChild(sel);
  } else if (field.type === "radio" || field.type === "checklist") {
    addLabel(null, field.label);
    for (const o of field.options || []) {
      const lab = document.createElement("label");
      lab.className = "opt";
      const inp = document.createElement("input");
      inp.type = field.type === "checklist" ? "checkbox" : "radio";
      inp.name = id;
      inp.value = o.value;
      const sp = document.createElement("span");
      sp.textContent = o.label;
      lab.append(inp, sp);
      wrap.appendChild(lab);
    }
  } else if (field.type === "checkbox") {
    const lab = document.createElement("label");
    lab.className = "opt";
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.id = id;
    const sp = document.createElement("span");
    sp.textContent = field.label;
    if (field.required) {
      const s = document.createElement("span");
      s.className = "req";
      s.textContent = "*";
      sp.appendChild(s);
    }
    lab.append(inp, sp);
    wrap.appendChild(lab);
  } else {
    addLabel(id, field.label);
    const inp = document.createElement("input");
    inp.type = field.type === "email" ? "email" : field.type === "date" ? "date" : "text";
    inp.id = id;
    wrap.appendChild(inp);
  }
  if (field.hint) {
    const h = document.createElement("div");
    h.className = "hint";
    h.textContent = field.hint;
    wrap.appendChild(h);
  }
  const err = document.createElement("div");
  err.className = "err";
  err.hidden = true;
  wrap.appendChild(err);
  return wrap;
}

function renderForm(schema) {
  removeThinking();
  el.approval.hidden = true;
  const host = el.formHost;
  host.innerHTML = "";
  const title = document.createElement("h3");
  title.textContent = schema.title || "Please complete this form";
  host.appendChild(title);
  if (schema.intro) {
    const intro = document.createElement("p");
    intro.className = "intro";
    intro.textContent = schema.intro;
    host.appendChild(intro);
  }
  const all = [];
  for (const section of schema.sections || []) {
    if (section.title) {
      const s = document.createElement("div");
      s.className = "sec";
      s.textContent = section.title;
      host.appendChild(s);
    }
    for (const field of section.fields || []) {
      host.appendChild(buildField(field));
      all.push(field);
    }
  }
  const byKey = Object.fromEntries(all.map((f) => [f.key, f]));
  const actions = document.createElement("div");
  actions.className = "pactions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.textContent = schema.submit_label || "Submit";
  actions.appendChild(submit);
  host.appendChild(actions);
  host.hidden = false;
  host.scrollIntoView({ block: "nearest" });

  const nodeFor = (key) => host.querySelector('.fld[data-key="' + key + '"]');
  function raw(field) {
    const n = nodeFor(field.key);
    if (!n) return field.type === "checklist" ? [] : "";
    if (field.type === "checkbox") return n.querySelector("input").checked;
    if (field.type === "checklist")
      return [...n.querySelectorAll("input:checked")].map((i) => i.value);
    if (field.type === "radio") {
      const c = n.querySelector("input:checked");
      return c ? c.value : "";
    }
    const inp = n.querySelector("input, select");
    return inp ? inp.value.trim() : "";
  }
  const visible = (f) => !f.show_if || raw(byKey[f.show_if.field]) === f.show_if.equals;
  const needed = (f) =>
    f.required || (f.required_if && raw(byKey[f.required_if.field]) === f.required_if.equals);
  function refresh() {
    for (const f of all) {
      const n = nodeFor(f.key);
      if (n) n.style.display = visible(f) ? "" : "none";
    }
  }
  host.addEventListener("input", refresh);
  host.addEventListener("change", refresh);
  refresh();

  submit.addEventListener("click", () => {
    let ok = true;
    const parts = [];
    for (const f of all) {
      const n = nodeFor(f.key);
      const err = n.querySelector(".err");
      let msg = "";
      if (visible(f)) {
        const v = raw(f);
        const empty = v === "" || v === false || (Array.isArray(v) && v.length === 0);
        if (needed(f) && empty) msg = f.type === "checkbox" ? "Please confirm." : "Required.";
        else if (f.type === "email" && v && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v))
          msg = "Enter a valid email.";
        else if (f.pattern && v && !new RegExp(f.pattern).test(v)) msg = "Doesn't match the format.";
        if (!msg && !empty) {
          const shown = Array.isArray(v) ? v.join(", ") : v === true ? "yes" : v;
          parts.push(f.label.replace(/\s*\*$/, "") + ": " + shown);
        }
      }
      err.textContent = msg;
      err.hidden = !msg;
      if (msg) ok = false;
    }
    if (!ok) return;
    host.hidden = true;
    send("Here are the details — " + parts.join("; "));
  });
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
        if (state.tool.name === "render_form") {
          // Generative UI: the agent asked us to render a form. Draw it from the schema.
          try {
            renderForm(JSON.parse(state.tool.args));
          } catch {
            note("The assistant sent a form we could not read.", true);
          }
        } else {
          let proposal = "";
          try {
            proposal = JSON.parse(state.tool.args).proposal || "";
          } catch {
            proposal = "";
          }
          showApproval(state.tool.name, proposal);
        }
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
  showThinking();
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
    removeThinking();
    note(`Could not start the run: ${err.message}`, true);
    state.running = false;
    el.send.disabled = el.message.disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    removeThinking();
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
    removeThinking();
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
  el.formHost.hidden = true;
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
  state.thinkingEl = null;
  el.approval.hidden = true;
  el.formHost.hidden = true;
  note("New conversation. Say something to begin.");
});

loadInfo();
