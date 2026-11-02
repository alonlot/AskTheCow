"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { chats: [], current: null, busy: false, user: "" };

// ------------------------------------------------------------------ markdown
function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  // s is already escaped
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function renderMarkdown(src) {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(esc(c.trim())));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push("<table><thead><tr>" + head.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(inline(esc(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""))));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((x) => `<li>${x}</li>`).join("") + `</${tag}>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s+|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    out.push(`<p>${inline(esc(para.join("\n"))).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("\n");
}

// ----------------------------------------------------------------------- api
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.user) headers["X-User"] = state.user;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok && !opts.raw) throw new Error(`${res.status} ${await res.text()}`);
  return opts.raw ? res : res.json();
}

// ---------------------------------------------------------------- rendering
function fmtTime(t) { return new Date(t * 1000).toLocaleString(); }

function renderChats() {
  const ul = $("#chat-list");
  ul.innerHTML = "";
  for (const c of state.chats) {
    const li = document.createElement("li");
    if (state.current && c.id === state.current.id) li.className = "active";
    li.innerHTML = `<span class="title"></span><button class="act" title="Rename">&#9998;</button><button class="act" title="Delete">&#10005;</button>`;
    li.querySelector(".title").textContent = c.title;
    li.onclick = () => openChat(c.id);
    const [ren, del] = li.querySelectorAll(".act");
    ren.onclick = async (e) => {
      e.stopPropagation();
      const t = prompt("Chat name", c.title);
      if (t && t.trim()) { await api(`/api/chats/${c.id}`, { method: "PATCH", body: JSON.stringify({ title: t.trim() }) }); await loadChats(); }
    };
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${c.title}"?`)) return;
      await api(`/api/chats/${c.id}`, { method: "DELETE" });
      if (state.current && state.current.id === c.id) { state.current = null; renderMessages([]); }
      await loadChats();
    };
    ul.appendChild(li);
  }
}

function stepsHtml(calls) {
  if (!calls || !calls.length) return "";
  const items = calls.map((c) => `<li><code>${esc(c.name)}</code> ${esc(JSON.stringify(c.input || {})).slice(0, 200)}${c.error ? " (error)" : ""}</li>`).join("");
  return `<details class="steps"><summary>${calls.length} step${calls.length > 1 ? "s" : ""} in the code</summary><ol>${items}</ol></details>`;
}

function messageEl(m) {
  const div = document.createElement("div");
  div.className = `msg ${m.role}` + (m.meta && m.meta.error ? " error" : "");
  if (m.role === "user") {
    div.innerHTML = `<div class="bubble"></div>`;
    div.querySelector(".bubble").textContent = m.content;
  } else {
    const sha = m.commit_sha ? ` · answered on commit <code>${esc(m.commit_sha.slice(0, 10))}</code>` : "";
    div.innerHTML = `${stepsHtml(m.meta && m.meta.tool_calls)}<div class="bubble md">${renderMarkdown(m.content)}</div><div class="meta">${fmtTime(m.created_at)}${sha}</div>`;
  }
  return div;
}

function renderMessages(msgs) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!msgs.length) {
    box.innerHTML = `<div class="empty"><h2>Ask about the firmware</h2><p>Questions are answered from the latest code on the tracked branch.</p></div>`;
    return;
  }
  for (const m of msgs) box.appendChild(messageEl(m));
  box.scrollTop = box.scrollHeight;
}

// -------------------------------------------------------------------- flows
async function loadChats() {
  state.chats = await api("/api/chats");
  renderChats();
}

async function openChat(id) {
  const chat = await api(`/api/chats/${id}`);
  state.current = chat;
  renderChats();
  renderMessages(chat.messages);
  $("#sidebar").classList.remove("open");
  location.hash = id;
}

async function newChat() {
  const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({}) });
  await loadChats();
  await openChat(chat.id);
  $("#input").focus();
}

async function send(text) {
  if (state.busy || !text.trim()) return;
  if (!state.current) await newChat();
  state.busy = true;
  $("#send").disabled = true;
  const box = $("#messages");
  if (box.querySelector(".empty")) box.innerHTML = "";

  const live = document.createElement("div");
  live.className = "msg assistant";
  live.innerHTML = `<details class="steps" open><summary>Working...</summary><ol></ol></details><div class="bubble md"><span class="progress">Starting...</span></div>`;
  const stepsList = live.querySelector("ol");
  const summary = live.querySelector("summary");
  const bubble = live.querySelector(".bubble");
  let draft = "";
  let userShown = false;

  try {
    const res = await api(`/api/chats/${state.current.id}/messages`, { method: "POST", body: JSON.stringify({ content: text }), raw: true });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let finished = false;
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "user_message" && !userShown) {
          box.appendChild(messageEl(ev.message));
          box.appendChild(live);
          userShown = true;
        } else if (ev.type === "status") {
          bubble.innerHTML = `<span class="progress">${esc(ev.text)}...</span>`;
        } else if (ev.type === "turn") {
          draft = "";
        } else if (ev.type === "tool") {
          const li = document.createElement("li");
          li.innerHTML = `<code>${esc(ev.name)}</code> ${esc(JSON.stringify(ev.input || {})).slice(0, 200)}`;
          stepsList.appendChild(li);
          summary.textContent = `Working... ${stepsList.children.length} steps`;
          bubble.innerHTML = `<span class="progress">Looking at the code...</span>`;
        } else if (ev.type === "text") {
          draft += ev.text;
          bubble.innerHTML = renderMarkdown(draft);
        } else if (ev.type === "done" || ev.type === "error") {
          live.replaceWith(messageEl(ev.message));
          finished = true;
        }
        box.scrollTop = box.scrollHeight;
      }
    }
  } catch (err) {
    bubble.innerHTML = `<span class="progress">Error: ${esc(String(err))}</span>`;
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    loadChats();
  }
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const k = s.knowledge || {};
    let text = `Branch ${s.branch}`;
    if (k.head) text += ` @ ${k.head.slice(0, 10)}`;
    text += s.brief ? ` · brief at ${s.brief.commit_sha.slice(0, 10)}` : " · no brief yet";
    text += ` · ${s.commit_notes} commits learned`;
    if (k.phase && k.phase !== "idle") {
      text += ` · ${k.phase}`;
      if (k.phase === "digesting") text += ` (${k.commits_done}/${k.commits_todo})`;
    }
    if (k.last_error) text += ` · last sync error: ${k.last_error}`;
    $("#status-text").textContent = text;
  } catch (e) {
    $("#status-text").textContent = "Server not reachable";
  }
}

async function showBrief() {
  const res = await api("/api/brief", { raw: true });
  $("#brief-body").innerHTML = res.ok ? renderMarkdown((await res.json()).content) : "<p>No brief yet. Calibration has not finished.</p>";
  $("#brief-dialog").showModal();
}

async function ensureUser() {
  // With server-side Basic auth, the server knows the user; the name here is only a label.
  try { state.user = localStorage.getItem("askthecow-user") || ""; } catch (e) { state.user = ""; }
  if (state.user) return;
  const dlg = $("#name-dialog");
  dlg.showModal();
  await new Promise((resolve) => dlg.addEventListener("close", resolve, { once: true }));
  state.user = $("#name-input").value.trim() || "anonymous";
  try { localStorage.setItem("askthecow-user", state.user); } catch (e) { /* private mode */ }
}

async function init() {
  await ensureUser();
  $("#who").textContent = `Signed in as ${state.user}`;
  $("#new-chat").onclick = newChat;
  $("#show-brief").onclick = showBrief;
  $("#close-brief").onclick = () => $("#brief-dialog").close();
  $("#toggle-side").onclick = () => $("#sidebar").classList.toggle("open");
  $("#composer").onsubmit = (e) => { e.preventDefault(); const v = $("#input").value; $("#input").value = ""; send(v); };
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  await loadChats();
  const id = location.hash.slice(1);
  if (id && state.chats.some((c) => c.id === id)) await openChat(id);
  refreshStatus();
  setInterval(refreshStatus, 10000);
}

init();
