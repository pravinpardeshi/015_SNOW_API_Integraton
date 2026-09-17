const $ = (id) => document.getElementById(id);
const toast = (msg, isErr = false) => {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 3500);
};

const ICON_MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`;
const ICON_SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`;

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("snow-theme", t); } catch (e) {}
  const b = $("btnTheme");
  if (b) b.innerHTML = (t === "light" ? ICON_SUN + "Dark" : ICON_MOON + "Light");
}

$("btnTheme").onclick = () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
};

(function initTheme() {
  let t = "dark";
  try { t = localStorage.getItem("snow-theme") || "dark"; } catch (e) {}
  applyTheme(t === "light" ? "light" : "dark");
})();

function setStatus(connected, instance, expiresIn) {
  $("statusDot").className = "dot " + (connected ? "online" : "offline");
  $("statusText").textContent = connected
    ? `Connected${expiresIn ? ` (${expiresIn}s left)` : ""}`
    : "Disconnected";
  $("statusInstance").textContent = instance || "";
}

// sys_id may arrive as a plain string or a {display_value, value} object
// (single-record fetches use sysparm_display_value=all). Always normalize.
function sid(v) {
  if (v && typeof v === "object") return String(v.value || v.display_value || "");
  return String(v ?? "");
}

function prettyDetail(d) {
  if (typeof d === "string") return d;
  if (d && typeof d === "object") {
    const vals = Object.values(d);
    const inner = vals.length === 1 && vals[0] && typeof vals[0] === "object" ? vals[0] : d;
    if (inner && inner.error && inner.error.message) {
      const det = inner.error.detail && inner.error.detail !== inner.error.message
        ? ` — ${inner.error.detail}` : "";
      return `${inner.error.message}${det}`;
    }
    const s = JSON.stringify(d);
    return s.length > 500 ? s.slice(0, 500) + "…" : s;
  }
  return "Request failed";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(prettyDetail(data.detail) || `Request failed (${res.status})`);
  }
  return data;
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    setStatus(s.connected, s.instance, s.expires_in);
    return s;
  } catch (e) { setStatus(false, ""); }
}

function displayVal(v) {
  if (v === null || v === undefined || v === "") return "<span class='muted'>—</span>";
  if (typeof v === "object") return (v.display_value || v.value || JSON.stringify(v));
  return String(v).slice(0, 90);
}

async function loadIncidents() {
  const tbody = $("tbody");
    tbody.innerHTML = `<tr><td colspan="10" class="muted">Loading…</td></tr>`;
  try {
    const limit = parseInt($("selLimit").value, 10) || 25;
    const offset = (currentPage - 1) * limit;
    const q = new URLSearchParams({
      limit: limit,
      offset: offset,
      active_only: $("chkActive").checked,
      search: $("inSearch").value.trim(),
      order_by: (currentSort.dir === "desc" ? "-" : "") + currentSort.field,
    });
    const data = await api("/api/incidents?" + q.toString());
    const rows = data.result || [];
    $("countLine").textContent = rows.length
      ? `Showing ${offset + 1}–${offset + rows.length} · Page ${currentPage}`
      : `No incidents on page ${currentPage}.`;
    updatePager(!!data.has_next);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="muted">No incidents found.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((r) => `
      <tr>
        <td><span class="num">${r.number || ""}</span></td>
        <td class="sysid-cell"><span class="mono">${esc(sid(r.sys_id))}</span>
          <button class="copy-btn" onclick="copyText(${JSON.stringify(sid(r.sys_id))})">Copy</button></td>
        <td>${displayVal(r.short_description)}</td>
        <td><span class="pill">${displayVal(r.state)}</span></td>
        <td>${displayVal(r.priority)}</td>
        <td>${displayVal(r.urgency)}</td>
        <td>${displayVal(r.impact)}</td>
        <td style="white-space:nowrap">${displayVal(r.opened_at)}</td>
        <td style="white-space:nowrap">${displayVal(r.updated_at)}</td>
        <td style="white-space:nowrap">
          <button class="btn sm" onclick='viewIncident(${JSON.stringify(r.sys_id)})'>View</button>
          <button class="btn sm primary" onclick='openUpdate(${JSON.stringify(r.sys_id)}, ${JSON.stringify(r.number || "")})'>Edit</button>
          <button class="btn sm danger" onclick='deleteIncident(${JSON.stringify(r.sys_id)}, ${JSON.stringify(r.number || "")})'>Del</button>
        </td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">Error: ${e.message}</td></tr>`;
    toast("Load failed: " + e.message, true);
  }
}

let currentDetail = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ServiceNow (with sysparm_display_value=all) returns {display_value, value} objects.
function dv(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return String(v.display_value ?? v.value ?? "");
  return String(v);
}

const STATE_LABELS = { 1: "New", 2: "In Progress", 3: "On Hold", 6: "Resolved", 7: "Closed", 8: "Canceled" };
function stateLabel(v) {
  const raw = String(v?.value ?? v ?? "");
  const num = raw.match(/^(\d)/)?.[1] ?? raw;
  return { cls: `st-${num}`, text: STATE_LABELS[num] || dv(v) || "—" };
}

// Parse "YYYY-MM-DD HH:MM:SS - User (Comments|Work notes)\ntext\n\n..." journal blobs.
function parseJournal(blob) {
  const text = dv(blob).trim();
  if (!text) return [];
  const entries = [];
  const re = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (.+?) \((Comments|Work notes)\)$/;
  let cur = null;
  for (const line of text.split("\n")) {
    const m = line.match(re);
    if (m) {
      if (cur) entries.push(cur);
      cur = { when: m[1], who: m[2], kind: m[3] === "Work notes" ? "work" : "comment", text: "" };
    } else if (cur) {
      cur.text += (cur.text ? "\n" : "") + line;
    }
  }
  if (cur) entries.push(cur);
  return entries.map((e) => ({ ...e, text: e.text.trim() }));
}

function badge(label, val) {
  const v = dv(val) || "—";
  const num = (v.match(/^(\d)/) || [])[1] || "";
  return `<div class="badge p${num}"><span>${label}</span><b>${esc(v)}</b></div>`;
}

function gridRow(k, v, full) {
  const t = dv(v).trim();
  if (!t) return "";
  return `<div class="${full ? "full" : ""}"><span class="k">${k}:</span> <span class="v">${esc(t)}</span></div>`;
}

async function viewIncident(sys_id) {
  sys_id = sid(sys_id);
  if (!sys_id) { toast("Cannot view: missing sys_id", true); return; }
  $("dTitle").textContent = "Loading…";
  $("dBody").innerHTML = `<p class="muted">Loading incident…</p>`;
  $("detailModal").classList.remove("hidden");
  try {
    const data = await api(`/api/incidents/${sys_id}`);
    const r = data.result || {};
    currentDetail = { sys_id: sid(r.sys_id) || sys_id, number: dv(r.number) };
    const st = stateLabel(r.state);
    const journal = parseJournal(r.comments_and_work_notes || [dv(r.comments), dv(r.work_notes)].filter(Boolean).join("\n"));

    $("dTitle").textContent = `Incident ${dv(r.number) || sys_id}`;
    $("dBody").innerHTML = `
      <div class="det-top">
        <div>
          <div class="det-num">${esc(dv(r.number))} <span class="pill ${st.cls}">${esc(st.text)}</span></div>
          <div class="det-short">${esc(dv(r.short_description)) || "<span class='muted'>No short description</span>"}</div>
          <div class="det-sub muted">Opened ${esc(dv(r.opened_at))} · Updated ${esc(dv(r.updated_at) || r.sys_updated_on)}${dv(r.opened_by) ? ` · by ${esc(dv(r.opened_by))}` : ""}</div>
        </div>
        <div class="det-badges">
          ${badge("Priority", r.priority)}${badge("Urgency", r.urgency)}${badge("Impact", r.impact)}
        </div>
      </div>
      <div class="det-sec"><h4>Description</h4>
        <p class="det-desc">${esc(dv(r.description)) || "<span class='muted'>—</span>"}</p>
      </div>
      <div class="det-sec"><h4>Details</h4><div class="det-grid">
        ${gridRow("Assigned to", r.assigned_to)}
        ${gridRow("Assignment group", r.assignment_group)}
        ${gridRow("Caller", r.caller_id)}
        ${gridRow("Category", r.category)}${r.subcategory ? gridRow("Subcategory", r.subcategory) : ""}
        ${gridRow("Resolved by", r.resolved_by)}${gridRow("Resolved at", r.resolved_at)}
        ${gridRow("Closed by", r.closed_by)}${gridRow("Closed at", r.closed_at)}
        ${gridRow("Close code", r.close_code)}${gridRow("Close notes", r.close_notes)}
        ${gridRow("Created by", r.sys_created_by)}${gridRow("Updated by", r.sys_updated_by)}
        <div class="full"><span class="k">Sys ID:</span>
          <span class="v mono">${esc(r.sys_id || sys_id)}</span>
          <button class="copy-btn" onclick="copySysId()">Copy</button>
        </div>
      </div></div>
      <div class="det-sec"><h4>Activity (${journal.length})</h4>
        <div class="journal">${
          journal.length
            ? journal.map((e) => `
              <div class="j-entry ${e.kind}">
                <div class="j-head"><span class="j-tag">${e.kind === "work" ? "Work note" : "Comment"}</span>
                <span>${esc(e.when)}</span><span>·</span><span>${esc(e.who)}</span></div>
                <p>${esc(e.text)}</p>
              </div>`).join("")
            : `<p class="muted" style="margin:0">No comments or work notes yet. Add one from Edit.</p>`
        }</div>
      </div>
      <details class="raw"><summary>Raw JSON</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>`;
  } catch (e) {
    $("dTitle").textContent = "Incident detail";
    $("dBody").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

function copySysId() {
  const id = currentDetail?.sys_id || "";
  if (!id) return;
  copyText(id);
}

function copyText(text) {
  if (!text) return;
  (navigator.clipboard?.writeText(text) || Promise.reject())
    .then(() => toast("Copied to clipboard"))
    .catch(() => toast(text));
}

$("btnDetailEdit").onclick = () => {
  if (!currentDetail) return;
  $("detailModal").classList.add("hidden");
  openUpdate(currentDetail.sys_id, currentDetail.number);
};

$("btnDetailDelete").onclick = async () => {
  if (!currentDetail) return;
  const { sys_id, number } = currentDetail;
  if (!confirm(`Delete incident ${number || sys_id}?`)) return;
  try {
    await api(`/api/incidents/${sys_id}`, { method: "DELETE" });
    toast(`Deleted ${number || sys_id}`);
    $("detailModal").classList.add("hidden");
    loadIncidents();
  } catch (e) { toast(e.message, true); }
};

async function openUpdate(sys_id, number) {
  sys_id = sid(sys_id);
  if (!sys_id) { toast("Cannot edit: missing sys_id", true); return; }
  $("uSysId").value = sys_id;
  $("uNumber").textContent = number ? `· ${number}` : "";
  ["uPriority", "uState", "uUrgency"].forEach((id) => ($(id).value = ""));
  ["uAssignGroup", "uDesc", "uComments", "uWorkNotes"].forEach((id) => ($(id).value = ""));
  $("updateMsg").textContent = "";
  $("uCurrent").textContent = "Loading current values…";
  $("updateModal").classList.remove("hidden");
  // Pre-fetch current values so the user sees what Urgency/Impact
  // (which drive auto-calculated Priority) are currently set to.
  try {
    const data = await api(`/api/incidents/${sys_id}`);
    const r = data.result || {};
    const val = (v) => (v && typeof v === "object" ? v.display_value || v.value : v) || "—";
    $("uCurrent").textContent =
      `Current: Priority ${val(r.priority)} · Urgency ${val(r.urgency)} · Impact ${val(r.impact)} · State ${val(r.state)}`;
  } catch (e) {
    $("uCurrent").textContent = "Could not load current values: " + e.message;
  }
}

async function deleteIncident(sys_id, number) {
  sys_id = sid(sys_id);
  if (!sys_id) { toast("Cannot delete: missing sys_id", true); return; }
  if (!confirm(`Delete incident ${number || sys_id}?`)) return;
  try {
    await api(`/api/incidents/${sys_id}`, { method: "DELETE" });
    toast(`Deleted ${number || sys_id}`);
    loadIncidents();
  } catch (e) { toast(e.message, true); }
}

// ---- wire up ----
$("btnConnect").onclick = async () => {
  const m = $("connMsg");
  m.textContent = "Connecting…"; m.className = "msg";
  try {
    const r = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({
        instance: $("inInstance").value,
        client_id: $("inClientId").value,
        client_secret: $("inClientSecret").value,
      }),
    });
    m.textContent = `Connected to ${r.instance} (token valid ${r.expires_in}s)`;
    m.className = "msg ok";
    toast("Connected to ServiceNow");
    await refreshStatus();
    loadIncidents();
  } catch (e) { m.textContent = e.message; m.className = "msg err"; toast(e.message, true); }
};

$("btnDisconnect").onclick = async () => {
  await api("/api/disconnect", { method: "POST" }).catch(() => {});
  $("connMsg").textContent = "Disconnected";
  refreshStatus();
};

$("btnToggleSecret").onclick = () => {
  const i = $("inClientSecret");
  i.type = i.type === "password" ? "text" : "password";
  $("btnToggleSecret").textContent = i.type === "password" ? "Show secret" : "Hide secret";
};

$("btnRefresh").onclick = loadIncidents;
$("inSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { currentPage = 1; loadIncidents(); } });

$("btnOpenCreate").onclick = () => {
  $("createMsg").textContent = "";
  $("createModal").classList.remove("hidden");
};

$("btnCreate").onclick = async () => {
  const m = $("createMsg");
  if (!$("cShort").value.trim()) { m.textContent = "Short description is required."; m.className = "msg err"; return; }
  m.textContent = "Creating…"; m.className = "msg";
  try {
    const body = {
      short_description: $("cShort").value.trim(),
      description: $("cDesc").value.trim() || null,
      urgency: $("cUrgency").value,
      impact: $("cImpact").value,
      priority: $("cPriority").value || null,
      comments: $("cComments").value.trim() || null,
    };
    Object.keys(body).forEach((k) => body[k] == null && delete body[k]);
    const r = await api("/api/incidents", { method: "POST", body: JSON.stringify(body) });
    m.textContent = `Created ${r.result?.number} (${r.result?.sys_id})`;
    m.className = "msg ok";
    toast(`Created ${r.result?.number}`);
    $("createModal").classList.add("hidden");
    currentPage = 1;
    loadIncidents();
  } catch (e) { m.textContent = e.message; m.className = "msg err"; }
};

$("btnUpdate").onclick = async () => {
  const m = $("updateMsg");
  m.textContent = "Saving…"; m.className = "msg";
  const pick = (id) => { const v = $(id).value.trim(); return v ? v : null; };
  const body = {
    priority: pick("uPriority"),
    state: pick("uState"),
    urgency: pick("uUrgency"),
    assignment_group: pick("uAssignGroup"),
    description: pick("uDesc"),
    comments: pick("uComments"),
    work_notes: pick("uWorkNotes"),
  };
  Object.keys(body).forEach((k) => body[k] == null && delete body[k]);
  if (!Object.keys(body).length) { m.textContent = "Change at least one field."; m.className = "msg err"; return; }
  try {
    const r = await api(`/api/incidents/${$("uSysId").value}`, { method: "PATCH", body: JSON.stringify(body) });
    const stored = r.result || {};
    loadIncidents();
    // ServiceNow auto-calculates Priority from Urgency x Impact and may
    // override the requested value. Surface that instead of silently closing.
    if (body.priority && stored.priority && String(stored.priority) !== String(body.priority)) {
      m.textContent = `Saved, but ServiceNow recalculated Priority ${body.priority} → ${stored.priority} ` +
        `(Urgency ${stored.urgency} × Impact ${stored.impact}). To control Priority, set Urgency and Impact.`;
      m.className = "msg warn";
      const cur = $("uCurrent");
      if (cur) cur.textContent =
        `Current: Priority ${stored.priority} · Urgency ${stored.urgency} · Impact ${stored.impact} · State ${stored.state}`;
    } else {
      m.textContent = `Updated ${stored.number || ""}`;
      m.className = "msg ok";
      toast("Incident updated");
      $("updateModal").classList.add("hidden");
    }
  } catch (e) { m.textContent = e.message; m.className = "msg err"; }
};

document.querySelectorAll("[data-close]").forEach((b) =>
  b.addEventListener("click", (e) => e.target.closest(".modal").classList.add("hidden"))
);
document.querySelectorAll(".modal").forEach((m) =>
  m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); })
);

// ---- sortable table headers ----
let currentSort = { field: "opened_at", dir: "desc" };
let currentPage = 1;

function paintSortArrows() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    const el = th.querySelector(".sort-arrow");
    if (!el) return;
    el.textContent = th.dataset.sort === currentSort.field
      ? (currentSort.dir === "asc" ? " ▲" : " ▼") : "";
  });
}

function updatePager(hasNext) {
  $("btnPrev").disabled = currentPage <= 1;
  $("btnNext").disabled = !hasNext;
  $("pageInfo").textContent = `Page ${currentPage}`;
}

$("btnPrev").onclick = () => { if (currentPage > 1) { currentPage--; loadIncidents(); } };
$("btnNext").onclick = () => { currentPage++; loadIncidents(); };
$("selLimit").addEventListener("change", () => { currentPage = 1; loadIncidents(); });
$("chkActive").addEventListener("change", () => { currentPage = 1; loadIncidents(); });

function initSorting() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const f = th.dataset.sort;
      if (currentSort.field === f) {
        currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
      } else {
        // sensible defaults: newest first for dates, A-Z / low-to-high otherwise
        currentSort = { field: f, dir: (f === "opened_at" || f === "updated_at") ? "desc" : "asc" };
      }
      paintSortArrows();
      currentPage = 1;
      loadIncidents();
    });
  });
  paintSortArrows();
}

initSorting();
refreshStatus();
