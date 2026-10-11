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

// ---- collapsible sidenav (persisted) ----
if ($("btnNav")) {
  $("btnNav").onclick = () => {
    const shell = document.querySelector(".shell");
    if (!shell) return;
    const collapsed = shell.classList.toggle("nav-collapsed");
    try { localStorage.setItem("snow-nav", collapsed ? "1" : "0"); } catch (e) {}
  };
  try {
    if (localStorage.getItem("snow-nav") === "1") document.querySelector(".shell")?.classList.add("nav-collapsed");
  } catch (e) {}
}

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
    const label = s.auth_mode === "basic" && s.username
      ? `${s.instance} · basic as ${s.username}`
      : s.instance;
    setStatus(s.connected, label, s.auth_mode === "basic" ? null : s.expires_in);
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
    tbody.innerHTML = `<tr><td colspan="9" class="muted">Loading…</td></tr>`;
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
    const pageOf = (page, pages) => pages ? `Page ${page} of ${pages}` : `Page ${page}`;
    const showing = (from, to, total) =>
      total != null ? `Showing ${from}–${to} of ${total}` : `Showing ${from}–${to}`;
    $("countLine").textContent = rows.length
      ? `${showing(offset + 1, offset + rows.length, data.total)} · ${pageOf(currentPage, data.pages)}`
      : `No incidents on page ${currentPage}.`;
    updatePager(!!data.has_next, data.pages);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted">No incidents found.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((r) => `
      <tr ondblclick='viewIncident(${JSON.stringify(r.sys_id)})' title="Double-click to view">
        <td><span class="num">${r.number || ""}</span>
          <span class="sysid-sub"><span class="mono">${esc(sid(r.sys_id))}</span>
            <button class="copy-btn" data-copy="${esc(sid(r.sys_id))}">Copy</button></span></td>
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
    tbody.innerHTML = `<tr><td colspan="9" class="muted">Error: ${e.message}</td></tr>`;
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

// Single delegated handler for dynamically rendered buttons (avoids inline
// onclick, which breaks on nested quotes). Covers copy + citation pills.
document.addEventListener("click", (e) => {
  const cp = e.target.closest("[data-copy]");
  if (cp) { copyText(cp.dataset.copy || ""); return; }
  const cite = e.target.closest("[data-cite]");
  if (cite) openCitedTicket(cite.dataset.cite || "");
  const go = e.target.closest("[data-goto-current]");
  if (go) dashOpenCurrent(go.dataset.gotoCurrent || "");
});

function copyText(text) {  if (!text) return;
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
  $("uCurrent").textContent = "Loading…";
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
    $("uCurrent").textContent = "Couldn't load: " + e.message;
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
function syncAuthRows() {
  const basic = ($("selAuthMode")?.value || "bearer") === "basic";
  if ($("bearerRows")) $("bearerRows").style.display = basic ? "none" : "";
}

$("btnConnect").onclick = async () => {
  const m = $("connMsg");
  m.textContent = "Connecting…"; m.className = "msg";
  try {
    const r = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({
        instance: $("inInstance").value,
        auth_mode: $("selAuthMode")?.value || "bearer",
        grant_type: $("selGrantType")?.value || "password",
        client_id: $("inClientId").value,
        client_secret: $("inClientSecret").value,
        username: $("inUser").value,
        password: $("inPass").value,
      }),
    });
    if (r.auth_mode === "basic") {
      m.textContent = `Connected · ${r.username || "user"}`;
    } else {
      m.textContent = `Connected · token ${r.expires_in}s${r.reused ? " (reused)" : ""}`;
    }
    m.className = "msg ok";
    toast("Connected");
    await refreshStatus();
    loadIncidents();
    if (typeof loadCustom === "function") { try { loadCustom(); } catch (e) {} }
  } catch (e) { m.textContent = e.message; m.className = "msg err"; toast(e.message, true); }
};

$("btnDisconnect").onclick = async () => {
  await api("/api/disconnect", { method: "POST" }).catch(() => {});
  $("connMsg").textContent = "Disconnected";
  refreshStatus();
};

if ($("selAuthMode")) {
  $("selAuthMode").addEventListener("change", syncAuthRows);
  syncAuthRows();
}

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
  if (!$("cShort").value.trim()) { m.textContent = "Short description required."; m.className = "msg err"; return; }
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
  if (!Object.keys(body).length) { m.textContent = "Change a field first."; m.className = "msg err"; return; }
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

function updatePager(hasNext, pages) {
  $("btnPrev").disabled = currentPage <= 1;
  $("btnNext").disabled = !hasNext;
  $("pageInfo").textContent = pages ? `Page ${currentPage} of ${pages}` : `Page ${currentPage}`;
}

$("btnPrev").onclick = () => { if (currentPage > 1) { currentPage--; loadIncidents(); } };
if ($("btnStateLegend")) $("btnStateLegend").onclick = (e) => {
  e.stopPropagation(); // don't trigger State column sort
  $("stateLegendModal")?.classList.remove("hidden");
};
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
loadDashboard();

// ---- custom table browser (e.g. u_snow_incident_table) ----
let customPage = 1;
let customRows = [];

function customTableName() {
  const t = ($("inCustomTable")?.value || "").trim() || "u_snow_incident_table";
  const title = $("customTitleTable");
  if (title) title.textContent = t;
  return t;
}

function pickCustomColumns(rows) {
  // Prefer human-meaningful columns first, then everything else (capped).
  const preferred = [
    "number", "u_number", "name", "u_name", "short_description",
    "u_short_description", "state", "u_state", "priority", "u_priority",
    "sys_id", "sys_created_on", "sys_updated_on",
  ];
  const allKeys = [];
  const seen = new Set();
  for (const r of rows.slice(0, 10)) {
    for (const k of Object.keys(r || {})) {
      if (!seen.has(k)) { seen.add(k); allKeys.push(k); }
    }
  }
  const cols = [];
  for (const p of preferred) {
    if (seen.has(p)) cols.push(p);
  }
  for (const k of allKeys) {
    if (cols.length >= 8) break;
    if (!cols.includes(k) && !k.startsWith("sys_") && typeof rows[0]?.[k] !== "object") cols.push(k);
  }
  if (!cols.includes("sys_id") && seen.has("sys_id")) cols.unshift("sys_id");
  return cols.slice(0, 8);
}

function customCellVal(v) {
  if (v === null || v === undefined || v === "") return "<span class='muted'>—</span>";
  if (typeof v === "object") {
    const s = v.display_value ?? v.value ?? JSON.stringify(v);
    return esc(String(s)).slice(0, 100);
  }
  return esc(String(v)).slice(0, 100);
}

async function loadCustom() {
  const head = $("customHead");
  const body = $("customBody");
  const msg = $("customMsg");
  const table = customTableName();
  if (msg) { msg.textContent = ""; msg.className = "msg"; }
  if (body) body.innerHTML = `<tr><td class="muted">Loading ${esc(table)}…</td></tr>`;
  try {
    const limit = parseInt($("selCustomLimit")?.value, 10) || 25;
    const offset = (customPage - 1) * limit;
    const q = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      search: ($("inCustomSearch")?.value || "").trim(),
      query: ($("inCustomQuery")?.value || "").trim(),
    });
    const data = await api(`/api/custom/${encodeURIComponent(table)}?` + q.toString());
    const rows = data.result || [];
    customRows = rows;
    const countLine = $("customCountLine");
    if (countLine) {
      const tot = data.total != null ? ` of ${data.total}` : "";
      const pg = data.pages ? ` of ${data.pages}` : "";
      countLine.textContent = rows.length
        ? `Showing ${offset + 1}–${offset + rows.length}${tot} · Page ${customPage}${pg} · ${table}`
        : `No records${data.total != null ? ` (${data.total} total)` : ""} in ${table} on page ${customPage}.`;
    }
    $("btnCustomPrev").disabled = customPage <= 1;
    $("btnCustomNext").disabled = !data.has_next;
    $("customPageInfo").textContent = data.pages ? `Page ${customPage} of ${data.pages}` : `Page ${customPage}`;
    if (!rows.length) {
      if (head) head.innerHTML = `<th class="muted">No columns</th>`;
      body.innerHTML = `<tr><td class="muted">No records found.</td></tr>`;
      return;
    }
    const cols = pickCustomColumns(rows);
    head.innerHTML = cols.map((c) => `<th>${esc(c)}</th>`).join("") + `<th>Actions</th>`;
    body.innerHTML = rows.map((r, i) => {
      const tds = cols.map((c) => {
        if (c === "sys_id") return `<td class="sysid-cell"><span class="mono">${esc(sid(r.sys_id))}</span></td>`;
        return `<td>${customCellVal(r[c])}</td>`;
      }).join("");
      return `<tr>${tds}<td style="white-space:nowrap"><button class="btn sm" onclick="viewCustomRow(${i})">View</button></td></tr>`;
    }).join("");
  } catch (e) {
    if (body) body.innerHTML = `<tr><td class="muted">Error: ${esc(e.message)}</td></tr>`;
    if (msg) {
      msg.textContent = e.message;
      msg.className = "msg err";
      msg.style.whiteSpace = "pre-wrap";
    }
    toast("Query failed: " + e.message, true);
  }
}

function viewCustomRow(i) {
  const r = customRows[i] || {};
  const table = customTableName();
  $("cdTitle").textContent = `${table} · ${dv(r.number) || dv(r.u_number) || dv(r.name) || dv(r.u_name) || sid(r.sys_id)}`;
  const keys = Object.keys(r).sort((a, b) => {
    if (a === "sys_id") return 1;
    if (b === "sys_id") return -1;
    return a.localeCompare(b);
  });
  const grid = keys.map((k) => {
    let v = r[k];
    if (v && typeof v === "object") v = v.display_value ?? v.value ?? JSON.stringify(v);
    const t = String(v ?? "").trim();
    if (!t) return "";
    if (k === "sys_id") {
      return `<div class="full"><span class="k">sys_id:</span> <span class="v mono">${esc(t)}</span> <button class="copy-btn" data-copy="${esc(t)}">Copy</button></div>`;
    }
    return `<div class="${t.length > 80 ? "full" : ""}"><span class="k">${esc(k)}:</span> <span class="v">${esc(t).slice(0, 500)}</span></div>`;
  }).join("");
  $("cdBody").innerHTML = `
    <div class="det-sec"><h4>Fields (${keys.length})</h4><div class="det-grid">${grid || "<span class='muted'>Empty record</span>"}</div></div>
    <details class="raw"><summary>Raw JSON</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>`;
  $("customDetailModal").classList.remove("hidden");
}

if ($("btnCustomRefresh")) {
  $("btnCustomRefresh").onclick = () => { customPage = 1; loadCustom(); };
  $("btnCustomPrev").onclick = () => { if (customPage > 1) { customPage--; loadCustom(); } };
  $("btnCustomNext").onclick = () => { customPage++; loadCustom(); };
  $("inCustomSearch")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { customPage = 1; loadCustom(); } });
  $("inCustomQuery")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { customPage = 1; loadCustom(); } });
  $("selCustomLimit")?.addEventListener("change", () => { customPage = 1; loadCustom(); });
}

// ---- Current incidents (open tickets + grounded triage/RCA/solution) ----
let currentRows = [];

function confPill(c) {
  const cls = c === "High" ? "st-6" : c === "Medium" ? "st-2" : "st-3";
  return `<span class="pill ${cls}">${esc(c || "—")}</span>`;
}

async function loadCurrent() {
  const body = $("currentBody"), msg = $("currentMsg"), line = $("currentCountLine");
  if (msg) { msg.textContent = "Loading…"; msg.className = "msg"; }
  if (body) body.innerHTML = `<tr><td colspan="7" class="muted">Loading…</td></tr>`;
  try {
    const r = await api("/api/current?limit=50");
    currentRows = r.result || [];
    if (!currentRows.length) {
      body.innerHTML = `<tr><td colspan="7" class="muted">No current tickets — run the pipeline.</td></tr>`;
    } else {
      body.innerHTML = currentRows.map((t, i) => `
        <tr ondblclick="viewCurrent(${i})" title="Double-click to view">
          <td><span class="num">${esc(t.ticket_id || "?")}</span></td>
          <td>${esc((t.summary || "").slice(0, 90))}</td>
          <td><span class="pill st-2">${esc(t.status || "")}</span></td>
          <td>${esc(t.priority || "—")}</td>
          <td>${t.triaged_at ? confPill(t.confidence) : `<span class="muted">pending</span>`}</td>
          <td class="muted small">${esc((t.triaged_at || "").slice(0, 16))}</td>
          <td><button class="btn sm" onclick="viewCurrent(${i})">View</button></td>
        </tr>`).join("");
    }
    if (line) line.textContent = `${currentRows.length} of ${r.total ?? "?"} current tickets`;
    if (msg) { msg.textContent = ""; msg.className = "msg"; }
  } catch (e) {
    if (body) body.innerHTML = `<tr><td colspan="7" class="muted">Error: ${esc(e.message)}</td></tr>`;
    if (msg) { msg.textContent = e.message; msg.className = "msg err"; }
  }
}

async function viewCurrent(i) {
  const t = currentRows[i];
  if (!t) return;
  $("curTitle").textContent = t.ticket_id || "Current ticket";
  $("curBody").innerHTML = `<p class="muted">Loading…</p>`;
  $("currentDetailModal").classList.remove("hidden");
  $("btnCurRetriage").dataset.sys = t.sys_id;
  try {
    const r = await api(`/api/current/${t.sys_id}`);
    const d = r.result;
    const det = [
      ["Status", d.status], ["Priority", d.priority], ["Severity", d.severity],
      ["Category", d.category], ["Environment", d.environment],
      ["Technology", d.technology_stack], ["Component", d.affected_component],
    ].filter(([, v]) => v).map(([k, v]) => `<div><span class="k">${esc(k)}:</span> <span class="v">${esc(v)}</span></div>`).join("");
    const cited = (d.cited || []).map((c) =>
      `<button class="cite-link" data-cite="${esc(c.ticket_id || "")}" title="Open ${esc(c.ticket_id || "")}">${esc(c.ticket_id || "?")}</button>`
    ).join("");
    $("curBody").innerHTML = `
      <div class="det-sec"><h4>${esc(d.ticket_id || "")}</h4><p class="det-desc">${esc(d.summary || "")}</p>
      <div class="det-grid">${det}</div></div>
      <div class="det-sec"><h4>Triage</h4><p class="det-desc">${esc(d.triage || "Not triaged yet.")}</p></div>
      <div class="det-sec"><h4>Root cause · ${esc(d.confidence || "?")} confidence</h4><p class="det-desc">${esc(d.rca || "—")}</p></div>
      <div class="det-sec"><h4>Recommended solution</h4><p class="det-desc">${esc(d.solution || "—")}</p></div>
      ${cited ? `<div class="det-sec"><h4>Supporting tickets (${d.cited.length})</h4><div class="cite-row">${cited}</div></div>` : ""}`;
  } catch (e) {
    $("curBody").innerHTML = `<p class="muted">Error: ${esc(e.message)}</p>`;
  }
}

async function openCitedTicket(ticketId) {
  if (!ticketId) return;
  try {
    const r = await api(`/api/custom/${customTableName()}?search=${encodeURIComponent(ticketId)}&limit=1`);
    const row = (r.result || [])[0];
    if (!row) { toast(`Ticket ${ticketId} not found`, true); return; }
    const keep = customRows;
    customRows = [row];
    viewCustomRow(0);
    customRows = keep; // modal already rendered; leave the Custom list untouched
  } catch (e) { toast(e.message, true); }
}

async function retriageCurrent(sys_id) {  const m = $("currentMsg");
  try {
    if (m) { m.textContent = "Analysing… (embed + local LLM, may take minutes)"; m.className = "msg"; }
    const r = await api(`/api/current/retriage/${sys_id}?top_k=5`, { method: "POST" });
    toast(`Re-analysed · ${r.confidence}${r.supported ? "" : " (unsupported)"}`);
    loadCurrent();
    const idx = currentRows.findIndex((t) => t.sys_id === sys_id);
    if (idx >= 0) viewCurrent(idx);
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
    toast(e.message, true);
  }
}

async function runCurrentPipeline() {
  const m = $("currentMsg");
  try {
    if (m) { m.textContent = "Pipeline running… (each ticket = embed + LLM call)"; m.className = "msg"; }
    const r = await api("/api/current/run", { method: "POST", body: JSON.stringify({ limit: 10, top_k: 5 }) });
    if (m) { m.textContent = `Fetched ${r.fetched}, upserted ${r.upserted}, triaged ${r.triaged} (${r.unsupported} unsupported) in ${r.elapsed_s}s`; m.className = "msg ok"; }
    loadCurrent();
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
    toast(e.message, true);
  }
}

if ($("btnCurrentRefresh")) {
  $("btnCurrentRefresh").onclick = loadCurrent;
  $("btnCurrentRun").onclick = () => { if (confirm("Run ingest + triage on up to 10 open tickets? (LLM calls are slow)")) runCurrentPipeline(); };
  $("btnCurRetriage").onclick = (e) => retriageCurrent(e.target.dataset.sys);
}

// ---- dashboard (landing page aggregate) ----
async function loadDashboard() {
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  try {
    const d = await api("/api/dashboard");
    const conn = $("dashConn");
    if (conn) conn.innerHTML = `<span class="dot ${d.connected ? "online" : "offline"}"></span> ${d.connected ? "Connected" : "Disconnected"}`;
    set("dashConnSub", `${d.instance || ""}${d.auth_mode ? ` · ${d.auth_mode}` : ""}${d.expires_in ? ` · ${d.expires_in}s left` : ""}`);
    set("dashOpen", d.open_total ?? "—");
    set("dashOpenSub", d.open_table || "");
    const c = d.current || {};
    set("dashTriaged", c.triaged ?? "—");
    set("dashTriagedSub", c.total != null ? `of ${c.total} current` : "");
    set("dashPending", c.total != null && c.triaged != null ? String(c.total - c.triaged) : "—");
    set("dashUnsup", c.unsupported ?? "—");
    const rdb = d.rag || {};
    set("dashRag", rdb.rows ?? "—");
    set("dashRagSub", rdb.ollama_up != null ? `ollama ${rdb.ollama_up ? "up" : "DOWN"}` : (rdb.error || ""));
    const mir = d.mirror || {};
    set("dashMirror", mir.rows ?? "—");
    set("dashMirrorSub", mir.watermark ? `wm ${String(mir.watermark).slice(0, 16)}` : (mir.error || ""));
    const up = d.uptime_s;
    set("dashUp", up != null ? `${Math.floor(up / 3600)}h ${Math.floor(up % 3600 / 60)}m` : "—");
    set("dashSnowSub", `${d.snow?.calls ?? "—"} calls · ${d.snow?.errors ?? "—"} err · tok ${d.tokens?.reused ?? "—"} reused`);
    const un = $("dashUntriaged");
    if (un) un.innerHTML = (c.untriaged || []).length
      ? c.untriaged.map((t) => `<button class="cite-link" data-goto-current="${esc(t.ticket_id || "")}" title="Open in Current">${esc(t.ticket_id || "?")}</button>`).join("")
      : `<span class="muted">All caught up — nothing waiting.</span>`;
    const rc = $("dashRecent");
    if (rc) rc.innerHTML = (c.recent || []).length
      ? c.recent.map((t) => `<button class="cite-link" data-goto-current="${esc(t.ticket_id || "")}" title="Open in Current">${esc(t.ticket_id || "?")} · ${esc(t.confidence || "?")}${t.triaged_at ? ` · ${esc(t.triaged_at)}` : ""}</button>`).join("")
      : `<span class="muted">No triaged tickets yet.</span>`;
  } catch (e) {
    set("dashConnSub", e.message);
  }
}

async function dashOpenCurrent(ticketId) {
  showView("view-current");
  await loadCurrent();
  const idx = currentRows.findIndex((t) => t.ticket_id === ticketId);
  if (idx >= 0) viewCurrent(idx);
  else toast(`Ticket ${ticketId} not in Current list`, true);
}

if ($("btnDashRefresh")) {
  $("btnDashRefresh").onclick = () => { refreshStatus(); loadDashboard(); };
  $("btnDashConnect").onclick = async () => {
    const m = $("dashConnMsg");
    try {
      if (m) { m.textContent = "Connecting…"; m.className = "msg"; }
      const st = await api("/api/status").catch(() => ({}));
      if (st.connected) {
        if (m) { m.textContent = ""; m.className = "msg"; }
        toast("Already connected");
        return;
      }
      let r, mode = "default";
      try {
        r = await api("/api/connect", { method: "POST", body: JSON.stringify({}) });
      } catch (e) {
        if (/token|grant|oauth|access_denied/i.test(e.message)) {
          r = await api("/api/connect", { method: "POST", body: JSON.stringify({ auth_mode: "basic" }) });
          mode = "basic fallback";
        } else { throw e; }
      }
      if (m) { m.textContent = ""; m.className = "msg"; }
      toast(mode === "basic fallback" ? "Connected (basic fallback)" : "Connected");
      refreshStatus(); loadDashboard();
    } catch (e) {
      if (m) { m.textContent = e.message; m.className = "msg err"; }
      toast(e.message, true);
    }
  };
  $("btnDashDisconnect").onclick = async () => {
    await api("/api/disconnect", { method: "POST" }).catch(() => {});
    toast("Disconnected");
    refreshStatus(); loadDashboard();
  };
  $("btnDashAdvanced").onclick = () =>
    document.querySelector('.nav-item[data-view="view-connect"]')?.click();
}

// ---- Early Warning (AIML model details + single-entry scoring) ----
let ewHistory = [];
const EW_FEATS = ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"];

async function loadEWModel() {
  const m = $("ewModelMsg");
  try {
    const d = await api("/api/earlywarning/model");
    const cal = d.isolation_forest.calibration || {};
    $("ewIF").innerHTML =
      `One model per service, trained only on normal minutes.<br/>`
      + `Estimators: 200 · contamination: ${d.isolation_forest.contamination}<br/>`
      + `<span class="muted">${esc(d.levels)}</span>`;
    const rf = d.random_forest;
    $("ewRF").innerHTML =
      `OOB score: <b>${rf.oob_score ?? "—"}</b> · precision: <b>${rf.train_precision ?? "—"}</b>`
      + ` · recall: <b>${rf.train_recall ?? "—"}</b><br/>`
      + `Trees: 300 · samples: ${rf.samples ?? "—"} (${rf.positives ?? "—"} pre-failure)<br/>`
      + `Horizon: ${rf.horizon_min} min · fires at p ≥ ${rf.proba_threshold}`;
    $("ewCal").innerHTML =
      `<table><thead><tr><th>Service</th><th>Watch</th><th>Warning</th><th>Critical</th></tr></thead><tbody>`
      + Object.entries(cal).map(([s, t]) =>
        `<tr><td>${esc(s)}</td><td>${t.watch?.toFixed(3)}</td><td>${t.warning?.toFixed(3)}</td><td>${t.critical?.toFixed(3)}</td>`).join("")
      + `</tbody></table>`;
    if (!$("ewF_count")?.value) ewSample("normal"); // one-click Score on first visit
    if (m) { m.textContent = ""; m.className = "msg"; }
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
  }
}

async function ewSample(kind) {
  const m = $("ewMsg");
  const svc = $("selEWService")?.value || "payments";
  try {
    if (m) { m.textContent = "Loading sample…"; m.className = "msg"; }
    const r = await api(`/api/earlywarning/sample?service=${encodeURIComponent(svc)}&kind=${kind}`);
    for (const f of EW_FEATS) $("ewF_" + f).value = r.features[f] ?? "";
    ewHistory = r.history || [];
    $("ewHistNote").textContent = `History: ${r.note || "—"}. Editing any field clears history (prediction then uses this minute only).`;
    if (m) { m.textContent = ""; m.className = "msg"; }
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
  }
}

async function ewScore() {
  const m = $("ewMsg");
  const svc = $("selEWService")?.value || "payments";
  const features = {};
  for (const f of EW_FEATS) {
    const v = parseFloat($("ewF_" + f)?.value);
    if (!isFinite(v)) {
      if (m) { m.textContent = `Fill in ${f} (number). Or load a sample.`; m.className = "msg err"; }
      return;
    }
    features[f] = v;
  }
  try {
    if (m) { m.textContent = "Scoring…"; m.className = "msg"; }
    const r = await api("/api/earlywarning/score", {
      method: "POST", body: JSON.stringify({ service: svc, features, history: ewHistory }),
    });
    const v = $("ewVerdict");
    v.textContent = r.verdict;
    v.className = "pill verdict " + (r.verdict === "FAILURE LIKELY" ? "v-bad" : r.verdict === "ANOMALOUS" ? "v-warn" : "v-ok");
    $("ewOut").innerHTML =
      `IF score: <b>${r.if_score}</b> · level: <b>${esc(r.level)}</b><br/>`
      + `Failure probability: <b>${(r.failure_proba * 100).toFixed(1)}%</b> (fires at ${(r.proba_threshold * 100).toFixed(0)}%)`
      + (r.history_degraded ? `<br/><span class="muted">Note: no prior-minute history — RF used this minute only.</span>` : "");
    $("ewProbaFill").style.width = `${Math.round(r.failure_proba * 100)}%`;
    $("ewProbaText").textContent = `${(r.failure_proba * 100).toFixed(1)}%`;
    $("ewTop").innerHTML = (r.top_features || []).length
      ? r.top_features.map((t) => `${esc(t.feature)}: z=${t.z}`).join(" · ")
      : "—";
    $("ewResult").classList.remove("hidden");
    if (m) { m.textContent = ""; m.className = "msg"; }
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
    toast(e.message, true);
  }
}

if ($("btnEWRefresh")) {
  $("btnEWRefresh").onclick = loadEWModel;
  $("btnEWSampleNormal").onclick = () => ewSample("normal");
  $("btnEWSamplePeak").onclick = () => ewSample("peak");
  $("btnEWSampleFailure").onclick = () => ewSample("failure");
  $("btnEWScore").onclick = ewScore;
  $("selEWService").onchange = () => { ewHistory = []; $("ewHistNote").textContent = ""; };
  for (const f of EW_FEATS) {
    $("ewF_" + f)?.addEventListener("input", () => {
      if (ewHistory.length) { ewHistory = []; $("ewHistNote").textContent = "History cleared by manual edit — prediction uses this minute only."; }
    });
  }
}

// ---- sidenav view switching ----
function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== id));
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === id));
  if (id === "view-sync") loadSyncStats();
  if (id === "view-obs") loadMetrics();
  if (id === "view-rag") refreshHistory();
  if (id === "view-dashboard") loadDashboard();
  if (id === "view-current") loadCurrent();
  if (id === "view-ew") loadEWModel();
}
document.querySelectorAll(".nav-item").forEach((b) =>
  b.addEventListener("click", () => showView(b.dataset.view))
);

// ---- sync view stats (local mirror + vector DB health) ----
async function loadSyncStats() {
  const m = $("syncMsg");
  if (m) { m.textContent = "Loading…"; m.className = "msg"; }
  try {
    const [s, r] = await Promise.all([api("/api/sync/stats"), api("/api/rag/stats")]);
    if ($("syncRows")) $("syncRows").textContent = s.rows;
    if ($("syncWatermark")) $("syncWatermark").textContent = s.watermark || "—";
    if ($("syncLatest")) $("syncLatest").textContent = s.latest_sync || "—";
    if ($("ragRows")) $("ragRows").textContent = r.rows;
    if ($("ragDims")) $("ragDims").textContent = r.embedding_dims ?? "—";
    if ($("ragModel")) $("ragModel").textContent = r.chat_model;
    if ($("ollamaUp")) $("ollamaUp").textContent = r.ollama_up ? "up" : "DOWN";
    if (m) { m.textContent = ""; m.className = "msg"; }
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
  }
}

if ($("btnSyncStats")) $("btnSyncStats").onclick = loadSyncStats;

// ---- Knowledge Agent chat (multi-turn thread over the vector DB, persisted) ----
let ragTranscript = []; // [{role, content}] — full conversation sent each turn
let ragThreadId = null;

function ragScroll() {
  const t = $("ragThread");
  if (t) t.scrollTop = t.scrollHeight;
}

function ragBubble(role, text) {
  const thread = $("ragThread");
  if (!thread) return null;
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  const p = document.createElement("p");
  p.textContent = text;
  div.appendChild(p);
  thread.appendChild(div);
  ragScroll();
  return div;
}

function ragSimCards(similar) {
  if (!similar || !similar.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "chat-sim";
  wrap.innerHTML = `<span class="muted small">${(similar || []).length} similar tickets</span>` +
    similar.map((t) => `
      <div class="sim-card">
        <div class="j-head"><span class="j-tag">${esc(t.ticket_id || "?")}</span>
        <span>similarity ${t.similarity}</span><span>·</span><span>${esc(t.status || "")}</span></div>
        <p><b>${esc(t.summary || "")}</b></p>
        ${t.solution ? `<p><span class="muted">Solution:</span> ${esc(t.solution).slice(0, 300)}</p>` : ""}
        ${t.rca ? `<p><span class="muted">Root cause:</span> ${esc(t.rca).slice(0, 300)}</p>` : ""}
      </div>`).join("");
  return wrap;
}

async function sendChat() {
  const box = $("inRagInput");
  const q = (box?.value || "").trim();
  const m = $("ragMsg");
  if (!q) { if (m) { m.textContent = "Ask about an incident."; m.className = "msg err"; } return; }
  const btn = $("btnRagAsk");
  const greet = document.querySelector("#ragThread .gpt-greet");
  if (greet) greet.remove();
  const sugs = $("ragSugs");
  if (sugs) sugs.style.display = "none";
  if (m) { m.textContent = ""; m.className = "msg"; }
  ragTranscript.push({ role: "user", content: q });
  ragBubble("user", q);
  if (box) { box.value = ""; box.style.height = "auto"; }
  const thinking = ragBubble("assistant", "Working… 0s");
  if (thinking) thinking.classList.add("thinking");
  const t0 = Date.now();
  const tick = setInterval(() => {
    const p = thinking?.querySelector("p");
    if (p) p.textContent = `Working… ${Math.floor((Date.now() - t0) / 1000)}s`;
  }, 1000);
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try {
    const agentic = $("chkAgent")?.checked !== false;
    const endpoint = agentic ? "/api/rag/agent" : "/api/rag/chat";
    const payload = {
      messages: ragTranscript,
      top_k: parseInt($("selRagK")?.value, 10) || 5,
      thread_id: ragThreadId,
    };
    if (agentic) payload.max_iters = 2;
    const r = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
    if (thinking) thinking.remove();
    ragTranscript.push({ role: "assistant", content: r.reply || "(empty answer)" });
    if (r.thread_id && r.thread_id !== ragThreadId) {
      ragThreadId = r.thread_id;
      refreshHistory();
    }
    const div = ragBubble("assistant", r.reply || "(empty answer)");
    if (div) {
      if (r.trace && r.trace.length) {
        const det = document.createElement("details");
        det.className = "trace";
        const iters = r.trace.length;
        det.innerHTML = `<summary>Agent trace · ${iters} iteration${iters > 1 ? "s" : ""}</summary>` +
          r.trace.map((s) => `
            <div class="trace-step"><b>iter ${s.iter}</b> · confidence ${esc(s.confidence || "?")} · retrieved ${s.retrieved ?? "?"}${s.thought ? `<br/>${esc(s.thought).slice(0, 400)}` : ""}` +
            (s.action ? `<br/><span class="muted">tool:</span> <code>${esc(s.action.tool || "?")}</code>${s.observation ? `<br/><span class="muted">result:</span> ${esc(s.observation).slice(0, 300)}` : ""}` : `<br/><span class="muted">final answer</span>`) +
            `</div>`).join("");
        div.appendChild(det);
      }
      if (r.actions && r.actions.length) {
        const acts = document.createElement("div");
        acts.className = "act-row";
        acts.innerHTML = r.actions.map((a) =>
          a.tool === "create_incident" && a.number
            ? `<span class="act-chip ok">Created ${esc(a.number)}</span>`
            : a.tool === "file_triage_note" && a.filed_on
              ? `<span class="act-chip ok">Triaged ${esc(a.filed_on)}</span>`
              : `<span class="act-chip">${esc(a.tool || "action")} done</span>`
        ).join("");
        div.appendChild(acts);
      }
      const cards = ragSimCards(r.similar);
      if (cards) div.appendChild(cards);
      ragScroll();
    }
  } catch (e) {
    if (thinking) thinking.remove();
    ragTranscript.pop(); // keep transcript clean so retry resends the same question
    if (m) { m.textContent = e.message; m.className = "msg err"; }
    toast("Chat failed: " + e.message, true);
  } finally {
    clearInterval(tick);
    if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
  }
}

function resetChat() {
  ragTranscript = [];
  ragThreadId = null;
  const thread = $("ragThread");
  if (thread) {
    thread.innerHTML = `<div class="gpt-greet">What can I help with?</div>`;
  }
  const sugs = $("ragSugs");
  if (sugs) sugs.style.display = "";
  document.querySelectorAll(".hist-item").forEach((b) => b.classList.remove("active"));
  const m = $("ragMsg");
  if (m) { m.textContent = ""; m.className = "msg"; }
}

async function refreshHistory() {
  const box = $("ragHistory");
  if (!box) return;
  try {
    const r = await api("/api/rag/threads?limit=30");
    const rows = r.threads || [];
    const cnt = $("ragHistCount");
    if (cnt) cnt.textContent = rows.length ? `(${rows.length})` : "";
    box.innerHTML = rows.length
      ? rows.map((t) => `
        <div class="hist-item${t.id === ragThreadId ? " active" : ""}" data-tid="${t.id}" title="${esc(t.title)}">
          <span class="t">${esc(t.title)}</span>
          <span class="n">${t.messages}</span>
          <button class="hist-del" data-del="${t.id}" title="Delete chat">×</button>
        </div>`).join("")
      : `<div class="hist-empty muted">No chats yet.</div>`;
    box.querySelectorAll(".hist-item").forEach((el) =>
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-del]")) return;
        loadThread(parseInt(el.dataset.tid, 10));
      })
    );
    box.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        const tid = parseInt(b.dataset.del, 10);
        if (!confirm("Delete this chat?")) return;
        try {
          await api(`/api/rag/threads/${tid}`, { method: "DELETE" });
          if (tid === ragThreadId) resetChat();
          refreshHistory();
        } catch (err) { toast("Delete failed: " + err.message, true); }
      })
    );
  } catch (e) { box.innerHTML = `<div class="hist-empty muted">History unavailable.</div>`; }
}

async function loadThread(tid) {
  const m = $("ragMsg");
  try {
    const r = await api(`/api/rag/threads/${tid}`);
    ragThreadId = r.id;
    ragTranscript = (r.messages || []).map((x) => ({ role: x.role, content: x.content }));
    const thread = $("ragThread");
    if (thread) {
      thread.innerHTML = "";
      for (const x of r.messages || []) {
        const div = ragBubble(x.role === "user" ? "user" : "assistant", x.content);
        if (x.role === "assistant" && x.similar) {
          const cards = ragSimCards(x.similar);
          if (div && cards) div.appendChild(cards);
        }
      }
      ragScroll();
    }
    const sugs = $("ragSugs");
    if (sugs) sugs.style.display = "none";
    refreshHistory();
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
  }
}

function ragAutoresize() {
  const box = $("inRagInput");
  if (!box) return;
  box.style.height = "auto";
  box.style.height = Math.min(160, box.scrollHeight) + "px";
}

if ($("btnRagAsk")) {
  $("btnRagAsk").onclick = sendChat;
  $("inRagInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  $("inRagInput")?.addEventListener("input", ragAutoresize);
}
if ($("btnRagNew")) $("btnRagNew").onclick = resetChat;
if ($("btnRagClear")) $("btnRagClear").onclick = async () => {
  if (!confirm("Delete ALL chats? This cannot be undone.")) return;
  try {
    const r = await api("/api/rag/threads", { method: "DELETE" });
    resetChat();
    refreshHistory();
    toast(`Deleted ${r.deleted ?? 0} chats`);
  } catch (e) { toast(e.message, true); }
};
document.querySelectorAll(".sug-chip").forEach((c) =>
  c.addEventListener("click", () => {
    const box = $("inRagInput");
    if (box) { box.value = c.dataset.q || ""; ragAutoresize(); box.focus(); }
    sendChat();
  })
);

// ---- observability dashboard ----
function drawChart(id, seriesList, opts = {}) {
  const cv = $(id);
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 600, H = parseInt(cv.getAttribute("height"), 10) || 150;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const pad = { l: 44, r: 8, t: 8, b: 18 };
  const max = Math.max(1, ...seriesList.flatMap((s) => s.data));
  const n = Math.max(1, ...seriesList.map((s) => s.data.length));
  const X = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, n - 1);
  const Y = (v) => H - pad.b - (v / max) * (H - pad.t - pad.b);
  ctx.strokeStyle = "rgba(148,163,184,.25)"; ctx.fillStyle = "rgba(148,163,184,.8)";
  const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  ctx.font = `${0.75 * rootPx}px Inter,sans-serif`; ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const v = (max * g) / 3, y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillText(v >= 100 ? Math.round(v) : v.toFixed(1), 4, y + 3);
  }
  for (const s of seriesList) {
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
    s.data.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
    ctx.stroke();
    if (opts.fill && s.data.length) {
      ctx.lineTo(X(s.data.length - 1), H - pad.b); ctx.lineTo(X(0), H - pad.b); ctx.closePath();
      ctx.globalAlpha = 0.12; ctx.fillStyle = s.color; ctx.fill(); ctx.globalAlpha = 1;
    }
  }
  const css = getComputedStyle(document.documentElement);
  ctx.fillStyle = css.getPropertyValue("--muted") || "#888";
  (opts.legend || []).forEach((t, i) => ctx.fillText(t, pad.l + i * 130, H - 5));
}

const ACCENT = "#38bdf8", GREEN = "#34d399", RED = "#f87171", AMBER = "#fbbf24";

function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

async function loadMetrics() {
  const m = $("mMsg");
  if (m) { m.textContent = "Loading…"; m.className = "msg"; }
  try {
    const [s, q] = await Promise.all([api("/api/metrics/summary"), api("/api/metrics/series?minutes=60")]);
    if ($("mUptime")) $("mUptime").textContent = fmtUptime(s.uptime_s);
    if ($("mReq")) $("mReq").textContent = s.requests;
    if ($("mErr")) $("mErr").textContent = `${s.errors} errors · ${s.error_pct}%`;
    if ($("mLat")) $("mLat").textContent = `${s.latency_ms.p50 ?? "—"} / ${s.latency_ms.p95 ?? "—"} ms`;
    if ($("mSnow")) $("mSnow").textContent = s.snow.calls;
    if ($("mSnowLat")) $("mSnowLat").textContent = `${s.snow.errors} err · p50 ${s.snow.p50_ms ?? "—"} / p95 ${s.snow.p95_ms ?? "—"} ms`;
    if ($("mTok")) $("mTok").textContent = `${s.tokens.minted} / ${s.tokens.reused}`;
    if ($("mRag")) $("mRag").textContent = s.rag.asks;
    if ($("mRagLat")) $("mRagLat").textContent = `${s.rag.errors} err · avg ${s.rag.avg_ms ?? "—"} ms`;
    const mir = s.mirror || {};
    if ($("mMirror")) $("mMirror").textContent = mir.rows ?? "—";
    if ($("mLag")) {
      let lag = "—";
      if (mir.watermark) {
        const mins = Math.max(0, Math.round((Date.now() - new Date(mir.watermark.replace(" ", "T") + "Z").getTime()) / 60000));
        lag = `watermark ${mir.watermark} · lag ~${mins}m`;
      }
      $("mLag").textContent = mir.error ? mir.error : lag;
    }
    const rdb = s.rag_db || {};
    if ($("mVec")) $("mVec").textContent = rdb.rows ?? "—";
    if ($("mOll")) $("mOll").textContent = `ollama ${s.ollama_up ? "up" : "DOWN"} · ${rdb.chat_model || ""}`;
    if ($("mHost")) $("mHost").textContent = `${s.host.load_1m ?? "—"} / ${s.host.mem_avail_mb ?? "—"} MB`;
    if ($("mEndpoints")) {
      const rows = Object.entries(s.endpoints || {});
      $("mEndpoints").innerHTML = rows.length
        ? rows.map(([r, e]) => `<tr><td class="mono">${r}</td><td>${e.req}</td><td>${e.err}</td><td>${e.avg_ms ?? "—"}</td><td>${e.max_ms}</td></tr>`).join("")
        : `<tr><td colspan="5" class="muted">No data yet.</td></tr>`;
    }
    drawChart("chReq", [
      { data: q.req_per_min, color: ACCENT },
      { data: q.err_per_min, color: RED },
    ], { fill: true, legend: ["— requests", "— errors"] });
    drawChart("chLat", [
      { data: q.avg_latency_ms, color: ACCENT },
      { data: q.snow_latency_ms, color: AMBER },
    ], { legend: ["— api avg", "— snow avg"] });
    drawChart("chSnow", [
      { data: q.snow_per_min, color: GREEN },
      { data: q.rag_per_min, color: AMBER },
    ], { legend: ["— snow calls", "— rag asks"] });
    if (m) { m.textContent = ""; m.className = "msg"; }
  } catch (e) {
    if (m) { m.textContent = e.message; m.className = "msg err"; }
  }
}

if ($("btnMetrics")) $("btnMetrics").onclick = loadMetrics;
setInterval(() => {
  if (!$("view-obs")?.classList.contains("hidden")) loadMetrics();
}, 15000);
