/* AI Photo Organizer - wizard logic */
"use strict";

const $ = (id) => document.getElementById(id);

const LEVEL_LABELS = {
  camera: "Camera",
  year: "Year",
  month: "Month",
  location: "Location",
  location_month: "Location, Month",
};
const PRESETS = {
  camera_year_locmonth: ["camera", "year", "location_month"],
  year_month: ["year", "month"],
  year_locmonth: ["year", "location_month"],
  loc_year_month: ["location", "year", "month"],
  year_camera: ["year", "camera"],
};
const PRESET_EXAMPLES = {
  camera_year_locmonth: "e.g.  Nikon D700\\2013\\Rochester NY, December\\DCS001.jpg",
  year_month: "e.g.  2013\\12 December\\DCS001.jpg",
  year_locmonth: "e.g.  2013\\Rochester NY, December\\DCS001.jpg",
  loc_year_month: "e.g.  Rochester NY\\2013\\12 December\\DCS001.jpg",
  year_camera: "e.g.  2013\\Nikon D700\\DCS001.jpg",
  custom: "Pick 1-3 levels. Each level becomes one folder.",
};

let results = null;      // /api/results payload
let plan = null;         // /api/plan payload
let pollTimer = null;

/* ---------------------------------------------------------- utilities */
async function api(path, opts) {
  // Root-relative (strip leading "/") so the app works both standalone at "/"
  // and mounted under a prefix like "/organizer/" (Aegis reverse proxy).
  const r = await fetch(path.replace(/^\//, ""), opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}
function fmtBytes(n) {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function setBar(barEl, pctEl, done, total) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  barEl.style.width = pct + "%";
  if (pctEl) pctEl.textContent = pct + "%";
}
function setStatus(state, path, count) {
  $("statusState").textContent = state;
  if (path !== undefined) $("statusPath").textContent = path;
  if (count !== undefined) $("statusCount").textContent = count;
}
function setExStatus(state, path, count) {
  const el = $("exStatus");
  if (!el) return;
  el.textContent = state;
  if (path !== undefined) $("exStatusPath").textContent = path;
  if (count !== undefined) $("exStatusCount").textContent = count;
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* reveal a file/folder in Windows Explorer via the server */
async function reveal(path) {
  if (!path) return;
  try {
    await post("/api/reveal", { path });
  } catch (e) {
    alert("Open file location failed: " + e.message);
  }
}

/* ---------------------------------------------------------- wizard nav */
function showStep(n) {
  for (let i = 1; i <= 5; i++) {
    const el = $("step" + i);
    if (el) el.classList.toggle("hidden", i !== n);
  }
  document.querySelectorAll(".wtab").forEach((t) => {
    const s = Number(t.dataset.step);
    t.classList.toggle("active", s === n);
    t.classList.toggle("done", s < n);
  });
  const body = $("winWizard") && $("winWizard").querySelector(".window-body");
  if (body) body.scrollTop = 0;
}

/* ---------------------------------------------------------- step 1: scan */
$("btnScan").addEventListener("click", async () => {
  const path = $("scanPath").value.trim();
  if (!path) { alert("Enter a folder path first."); return; }
  $("btnScan").disabled = true;
  $("btnScanCancel").disabled = false;
  $("scanProgressBox").classList.remove("hidden");
  $("scanCancelNote").classList.add("hidden");
  $("toStep2").disabled = true;
  $("scanBar").style.width = "0%";
  $("scanText").textContent = "0 / 0";
  $("scanFile").textContent = "";
  setStatus("Scanning…", path, "");
  try {
    await post("/api/scan", { path, max: $("scanMax").value.trim() || undefined });
  } catch (e) {
    $("btnScan").disabled = false;
    setStatus("Scan failed to start");
    alert(e.message);
    return;
  }
  stopPoll();
  pollTimer = setInterval(pollScan, 500);
});

async function pollScan() {
  let s;
  try { s = await api("/api/scan/status"); } catch (e) { return; }
  $("scanText").textContent = `${s.processed} / ${s.total}`;
  $("scanFile").textContent = s.currentFile || "";
  setBar($("scanBar"), $("scanPct"), s.processed, s.total);
  setStatus("Scanning…", undefined, `${s.processed}/${s.total}`);
  if (s.state === "done") {
    stopPoll();
    setBar($("scanBar"), $("scanPct"), 1, 1);
    $("btnScanCancel").disabled = true;
    setStatus("Scan complete", undefined, `${s.total} files`);
    $("btnScan").disabled = false;
    await loadResults();
  } else if (s.state === "cancelled") {
    stopPoll();
    $("btnScan").disabled = false;
    $("btnScanCancel").disabled = true;
    $("scanCancelNote").classList.remove("hidden");
    $("scanCancelText").textContent =
      `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    setStatus("Scan cancelled", undefined, `${s.processed}/${s.total}`);
    try { await loadResults(true); } catch (e) { /* partial results optional */ }
  } else if (s.state === "error") {
    stopPoll();
    $("btnScan").disabled = false;
    setStatus("Scan error");
    alert("Scan failed: " + (s.error || "unknown"));
  }
}

/* ---------------------------------------------------------- step 2: results */
async function loadResults(quiet) {
  results = await api("/api/results");
  renderResults(results);
  $("toStep2").disabled = false;
  if (!quiet) showStep(2);
  setStatus(results.partial ? "Partial results (cancelled scan)" : "Ready",
            results.scannedRoot, results.totalPhotos + " media files");
  // default target root for step 3 — never clobber a folder the user (or a
  // restored pref) already chose; cinema/music behave the same way
  if (!$("targetRoot").value.trim()) {
    $("targetRoot").value = results.scannedRoot.replace(/[\\/]+$/, "") + "\\Organized";
  }
}

function statRows(el, rows) {
  el.innerHTML = '<table class="stat-table">' + rows.map(
    ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") + "</table>";
}

function renderResults(r) {
  statRows($("sumBody"), [
    ...(r.partial ? [["⚠ Scan", "PARTIAL (cancelled)"]] : []),
    ["Total media files", r.totalPhotos],
    ["Total size", fmtBytes(r.totalBytes)],
    ["Date range", (r.dateMin ? r.dateMin.slice(0, 10) : "?") + "  →  " +
                   (r.dateMax ? r.dateMax.slice(0, 10) : "?")],
    ["Unreadable files", r.errorCount],
  ]);
  const camRows = r.cameras.slice(0, 8).map(([c, n]) => [c, n]);
  if (!camRows.length) camRows.push(["(none)", 0]);
  statRows($("camBody"), camRows);
  statRows($("locBody"), [
    ["With GPS", r.gpsCount],
    ["Without GPS", r.noGpsCount],
  ]);
  const TYPE_LABELS = { photo: "Photos", raw: "RAW photos", video: "Videos",
                        sidecar: "Sidecar files" };
  const typeRows = Object.entries(r.byType || {})
    .sort((a, b) => b[1].count - a[1].count)
    .map(([t, v]) => [TYPE_LABELS[t] || t, `${v.count}  (${fmtBytes(v.bytes)})`]);
  if (!typeRows.length) typeRows.push(["(none)", 0]);
  statRows($("typeBody"), typeRows);
  const Q_LABELS = { exif: "EXIF (trusted)", exif_suspect: "EXIF rejected ⚠",
                     filename: "From filename", container: "From container",
                     mtime: "From file date", mtime_suspect: "File date (suspect) ⚠",
                     unknown: "Unknown date ⚠" };
  const qRows = Object.entries(r.byQuality || {})
    .sort((a, b) => b[1] - a[1])
    .map(([q, n]) => [Q_LABELS[q] || q, n]);
  if (!qRows.length) qRows.push(["(none)", 0]);
  statRows($("qualityBody"), qRows);
  const warn = $("dateWarn");
  if (r.dateQualityWarning > 0) {
    warn.className = "date-warn";
    warn.innerHTML = `⚠ <b>${r.dateQualityWarning}</b> files had unreliable dates — ` +
      `the best available date was used, or they will go to <code>_Unknown Date\\</code>. ` +
      `Review them in Explore with Date quality = "Needs review".`;
  } else {
    warn.className = "date-warn hidden";
    warn.innerHTML = "";
  }
  statRows($("dupBody"), [
    ["Exact dupe groups", r.exactGroups],
    ["Wasted by exact dupes", fmtBytes(r.exactWastedBytes)],
    ["Near-dupe groups", r.nearGroups],
    ["Total dupe groups", r.totalGroups],
    ["Files in dupe groups", Object.values(r.groups).reduce((a, g) => a + g.length, 0)],
  ]);
  const grid = $("thumbGrid");
  grid.innerHTML = "";
  r.thumbSample.forEach((p) => {
    const cell = document.createElement("div");
    cell.className = "thumb-cell";
    const name = p.split(/[\\/]/).pop();
    cell.innerHTML = `<img loading="lazy" src="api/thumb?path=${encodeURIComponent(p)}" alt="">
                      <div class="cap" title="${esc(p)}">${esc(name)}</div>`;
    grid.appendChild(cell);
  });
}

$("toStep2").addEventListener("click", () => { if (results) showStep(2); });
$("backTo1").addEventListener("click", () => showStep(1));
$("toStep3").addEventListener("click", () => showStep(3));

$("btnScanCancel").addEventListener("click", async () => {
  $("btnScanCancel").disabled = true;
  try { await post("/api/scan/cancel", {}); } catch (e) { /* already finished */ }
});
$("btnScanResume").addEventListener("click", () => {
  $("scanCancelNote").classList.add("hidden");
  $("btnScan").click();
});

/* ---------------------------------------------------------- step 3: organize */
function fillLevelSelect(sel, allowNone) {
  sel.innerHTML = "";
  if (allowNone) sel.add(new Option("(none)", ""));
  Object.entries(LEVEL_LABELS).forEach(([v, l]) => sel.add(new Option(l, v)));
}
fillLevelSelect($("lvl1"), false);
fillLevelSelect($("lvl2"), true);
fillLevelSelect($("lvl3"), true);
$("lvl1").value = "camera"; $("lvl2").value = "year"; $("lvl3").value = "location_month";

$("preset").addEventListener("change", () => {
  const v = $("preset").value;
  $("customLevels").style.display = v === "custom" ? "" : "none";
  $("presetExample").textContent = PRESET_EXAMPLES[v] || "";
});
$("preset").dispatchEvent(new Event("change"));

$("nameTemplate").addEventListener("change", () => {
  $("customTplRow").style.display = $("nameTemplate").value === "custom" ? "" : "none";
});

function currentLevels() {
  const v = $("preset").value;
  if (v !== "custom") return PRESETS[v];
  const lv = [$("lvl1").value, $("lvl2").value, $("lvl3").value].filter(Boolean);
  return lv.slice(0, 3);
}

$("backTo2").addEventListener("click", () => showStep(2));
$("toStep4").addEventListener("click", async () => {
  const levels = currentLevels();
  if (!levels.length) { alert("Pick at least one folder level."); return; }
  const nt = $("nameTemplate").value;
  const body = {
    levels,
    nameTemplate: nt,
    customTemplate: nt === "custom" ? $("customTpl").value : undefined,
    dupeMode: document.querySelector('input[name="dupeMode"]:checked').value,
    action: document.querySelector('input[name="action"]:checked').value,
    removeEmpty: $("removeEmpty").checked,
    targetRoot: $("targetRoot").value.trim(),
  };
  $("toStep4").disabled = true;
  setStatus("Computing plan…");
  try {
    plan = await post("/api/plan", body);
  } catch (e) {
    alert("Plan failed: " + e.message);
    $("toStep4").disabled = false;
    setStatus("Ready");
    return;
  }
  $("toStep4").disabled = false;
  renderPlan(plan);
  showStep(4);
  setStatus("Plan ready", plan.stats.targetRoot, plan.stats.totalFiles + " files");
});

/* ---------------------------------------------------------- step 4: plan */
function renderPlan(p) {
  const s = p.stats;
  $("planStats").innerHTML =
    `<b>${s.totalFiles}</b> files will be <b>${s.action === "move" ? "moved" : "copied"}</b> into ` +
    `<b>${esc(s.targetRoot)}</b><br>` +
    (s.companionFiles ? `<b>${s.companionFiles}</b> sidecar companions move along with their files &middot; ` : "") +
    `<b>${s.foldersToCreate}</b> new folders &middot; ` +
    `<b>${s.dupeFiles}</b> duplicate files in <b>${s.groupCount}</b> groups &rarr; <code>_Duplicates\\</code> &middot; ` +
    `<b>${s.collisionsResolved}</b> name collisions resolved (-2, -3&hellip;)`;
  mountPlanReview($("planStats"), "/api/plan/summary", "photo");
  const list = $("planList");
  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  p.entries.forEach((e) => {
    const row = document.createElement("div");
    row.className = "plan-row" + (e.isDupe ? " dupe" : "");
    const tag = e.isDupe
      ? `<span class="tag">DUPE ${esc(e.groupId || "")}</span>`
      : `<span class="tag oktag">FILE</span>`;
    row.innerHTML = `${tag}${esc(e.from)} <b>&rarr;</b> <span class="to">${esc(e.to)}</span>`;
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

/* LLM plan-review: an opt-in plain-English summary + anomaly flags, shown
   before Execute. Advisory only — it gates nothing. Generic across photos /
   cinema / music: each caller passes its own host element + endpoint + key.
   Defined here in app.js so cinema.js and music.js reuse it (load order). */
const _SEV_COLOR = { danger: "#a00", caution: "#a60", info: "#405060" };
function mountPlanReview(host, url, key) {
  if (!host) return;
  const boxId = "planReview-" + key;
  const btnId = "btnPlanReview-" + key;
  const outId = "planReviewOut-" + key;
  let box = document.getElementById(boxId);
  if (!box) {
    box = document.createElement("div");
    box.id = boxId;
    box.style.margin = "8px 0";
    box.innerHTML =
      `<button id="${btnId}" type="button">&#129504; Explain this plan</button>` +
      `<div id="${outId}" class="hint" style="margin-top:5px"></div>`;
    host.parentNode.insertBefore(box, host.nextSibling);
    document.getElementById(btnId)
      .addEventListener("click", () => runPlanReview(url, btnId, outId));
  }
  document.getElementById(outId).innerHTML = "";
}
async function runPlanReview(url, btnId, outId) {
  const btn = document.getElementById(btnId);
  const out = document.getElementById(outId);
  btn.disabled = true;
  out.textContent = "Reviewing…";
  try {
    const r = await post(url, {});
    let html = "<div>" + esc(r.summary || "") + "</div>";
    if (Array.isArray(r.warnings) && r.warnings.length) {
      html += '<ul style="margin:5px 0 0;padding-left:18px">' +
        r.warnings.map((w) =>
          `<li style="color:${_SEV_COLOR[w.severity] || "inherit"}">${esc(w.text)}</li>`
        ).join("") + "</ul>";
    }
    out.innerHTML = html;
  } catch (e) {
    out.textContent = "Review failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

$("backTo3").addEventListener("click", () => showStep(3));

/* ---------------------------------------------------------- step 5: execute */
$("btnExecute").addEventListener("click", async () => {
  if (!plan) return;
  const s = plan.stats;
  if (!confirm(`Really ${s.action} ${s.totalFiles} files into\n${s.targetRoot} ?`)) return;
  showStep(5);
  $("doneWindow").classList.add("hidden");
  $("btnStartOver").classList.add("hidden");
  $("btnExecCancel").disabled = false;
  $("execLog").innerHTML = "";
  $("execBar").style.width = "0%";
  setStatus("Organizing…");
  try {
    await post("/api/execute", {});
  } catch (e) {
    alert("Execute failed to start: " + e.message);
    setStatus("Ready");
    return;
  }
  stopPoll();
  pollTimer = setInterval(pollExecute, 500);
});

async function pollExecute() {
  let s;
  try { s = await api("/api/execute/status"); } catch (e) { return; }
  $("execText").textContent = `${s.processed} / ${s.total}`;
  $("execFile").textContent = s.currentFile || "";
  setBar($("execBar"), $("execPct"), s.processed, s.total);
  setStatus("Organizing…", undefined, `${s.processed}/${s.total}`);
  const log = $("execLog");
  log.innerHTML = s.log.map((l) => `<div>${esc(l)}</div>`).join("");
  log.scrollTop = log.scrollHeight;
  if (s.state === "done" || s.state === "cancelled") {
    stopPoll();
    $("btnExecCancel").disabled = true;
    if (s.state === "done") setBar($("execBar"), $("execPct"), 1, 1);
    setStatus(s.state === "done" ? "Done" : "Execute cancelled", undefined, "");
    renderDone(s.result || {});
  } else if (s.state === "error") {
    stopPoll();
    setStatus("Execute error");
    alert("Execute failed: " + (s.error || "unknown"));
  }
}

$("btnExecCancel").addEventListener("click", async () => {
  $("btnExecCancel").disabled = true;
  try { await post("/api/execute/cancel", {}); } catch (e) { /* already finished */ }
});

function renderDone(r) {
  const w = $("doneWindow");
  w.classList.remove("hidden");
  $("btnStartOver").classList.remove("hidden");
  $("doneBody").innerHTML =
    (r.cancelled ? `<p><b>&#9888; Cancelled by user</b> — everything below was completed and can be undone.</p>` : "") +
    `<table class="done-kv">
      <tr><td>Files moved</td><td><b>${r.moved || 0}</b></td></tr>
      <tr><td>Files copied</td><td><b>${r.copied || 0}</b></td></tr>
      <tr><td>Skipped (already in place)</td><td>${r.skipped || 0}</td></tr>
      <tr><td>Errors</td><td>${r.errors || 0}</td></tr>
      <tr><td>Undo manifest</td><td class="mono small">${esc(r.undoFile || "-")}</td></tr>
      ${r.undoCopy ? `<tr><td>Undo copy (target)</td><td class="mono small">${esc(r.undoCopy)}</td></tr>` : ""}
    </table>
    <div class="field-row" style="margin-top:10px">
      <button id="btnUndo" class="danger">&#8617; Undo last run</button>
      <button id="btnOpenTarget">&#128193; Open folder</button>
      <span class="hint" id="undoMsg"></span>
    </div>`;
  const targetRoot = (plan && plan.stats && plan.stats.targetRoot)
    || (r.undoCopy ? r.undoCopy.replace(/[\\/][^\\/]+$/, "") : "");
  $("btnOpenTarget").addEventListener("click", () => reveal(targetRoot));
  $("btnUndo").addEventListener("click", async () => {
    if (!confirm("Undo the last run?\nMoves are reversed; copies are deleted.")) return;
    $("btnUndo").disabled = true;
    try {
      const res = await post("/api/undo", { manifest: r.undoFile });
      $("undoMsg").textContent =
        `Restored ${res.restored}, deleted ${res.deleted}, skipped ${res.skipped}, errors ${res.errors}.`;
      setStatus("Undo complete");
    } catch (e) {
      $("btnUndo").disabled = false;
      $("undoMsg").textContent = "Undo failed: " + e.message;
    }
  });
}

$("btnStartOver").addEventListener("click", () => {
  plan = null;
  showStep(1);
  setStatus("Ready", "", "");
});

/* ---------------------------------------------------------- UI prefs
   Per-window preferences in localStorage: last source folder, last target
   root, move-vs-copy, dupe handling, preset/template. Restored on load so
   the wizard comes back the way the user left it. Browse buttons are
   auto-wired by browse.js via data-browse-target (no per-button code here). */
const PREFS_KEY = "po.uiPrefs.v1";
function getPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; }
  catch (e) { return {}; }
}
function savePrefs(app, patch) {
  const all = getPrefs();
  all[app] = Object.assign({}, all[app] || {}, patch);
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(all)); } catch (e) { /* full/blocked */ }
}

function collectPhotoPrefs() {
  return {
    scanPath: $("scanPath").value.trim(),
    targetRoot: $("targetRoot").value.trim(),
    action: (document.querySelector('input[name="action"]:checked') || {}).value,
    dupeMode: (document.querySelector('input[name="dupeMode"]:checked') || {}).value,
    removeEmpty: $("removeEmpty").checked,
    preset: $("preset").value,
    nameTemplate: $("nameTemplate").value,
  };
}
function persistPhotoPrefs() { savePrefs("photos", collectPhotoPrefs()); }

(function initPhotoPrefs() {
  const p = getPrefs()["photos"] || {};
  if (p.scanPath) $("scanPath").value = p.scanPath;
  if (p.targetRoot) $("targetRoot").value = p.targetRoot;
  if (p.action) {
    const r = document.querySelector(`input[name="action"][value="${p.action}"]`);
    if (r) r.checked = true;
  }
  if (p.dupeMode) {
    const r = document.querySelector(`input[name="dupeMode"][value="${p.dupeMode}"]`);
    if (r) r.checked = true;
  }
  if (p.removeEmpty != null) $("removeEmpty").checked = !!p.removeEmpty;
  if (p.preset && $("preset").querySelector(`option[value="${p.preset}"]`)) {
    $("preset").value = p.preset;
    $("preset").dispatchEvent(new Event("change"));
  }
  if (p.nameTemplate && $("nameTemplate").querySelector(`option[value="${p.nameTemplate}"]`)) {
    $("nameTemplate").value = p.nameTemplate;
    $("nameTemplate").dispatchEvent(new Event("change"));
  }
  ["scanPath", "targetRoot", "preset", "nameTemplate", "removeEmpty"]
    .forEach((id) => $(id).addEventListener("change", persistPhotoPrefs));
  document.querySelectorAll('input[name="action"], input[name="dupeMode"]')
    .forEach((r) => r.addEventListener("change", persistPhotoPrefs));
  $("btnScan").addEventListener("click", persistPhotoPrefs);
  $("toStep4").addEventListener("click", persistPhotoPrefs);
})();

setStatus("Ready", "", "");

/* ---------------------------------------------------------- step 6: explore */
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"];
const EX_PAGE = 200;
let exOffset = 0, exCount = 0, exLastQuery = "";

(function initExploreForm() {
  const m = $("exMonth");
  MONTH_NAMES.forEach((name, i) => m.add(new Option(name, String(i + 1))));
  const d = $("exDay");
  for (let i = 1; i <= 31; i++) d.add(new Option(String(i), String(i)));
  const hf = $("exHourFrom"), ht = $("exHourTo");
  hf.add(new Option("Any", "")); ht.add(new Option("Any", ""));
  for (let i = 0; i < 24; i++) {
    const lbl = String(i).padStart(2, "0") + ":00";
    hf.add(new Option(lbl, String(i))); ht.add(new Option(lbl, String(i)));
  }
})();

async function loadCameraList() {
  try {
    const data = await api("/api/cameras");
    const sel = $("exCamera");
    const cur = sel.value;
    sel.innerHTML = '<option value="">Any camera</option>';
    data.cameras.forEach((c) => sel.add(new Option(c, c)));
    sel.value = cur;
  } catch (e) { /* DB may be empty */ }
}

async function loadExtensionList() {
  try {
    const data = await api("/api/extensions");
    const sel = $("exExt");
    const cur = sel.value;
    sel.innerHTML = '<option value="">Any extension</option>';
    data.extensions.forEach(([ext, n]) =>
      sel.add(new Option(`${ext} (${n.toLocaleString()})`, ext)));
    sel.value = cur;
  } catch (e) { /* DB may be empty */ }
}

/* Explore lives in its own desktop window; loading data on every open. */
$("btnExplore").addEventListener("click", () => {
  if (window.WM) WM.open("winExplore");
});
$("exBack").addEventListener("click", () => {
  if (window.WM) WM.close("winExplore");
});
$("winExplore").addEventListener("wm:open", () => {
  loadCameraList();
  loadExtensionList();
  setExStatus("Explore", "photo database", "");
  if (!exLastQuery) doSearch(0);  // first open: show everything
});

function exploreQueryString(offset) {
  const q = new URLSearchParams();
  if ($("exMonth").value) q.set("month", $("exMonth").value);
  if ($("exDay").value) q.set("day", $("exDay").value);
  if ($("exYear").value.trim()) q.set("year", $("exYear").value.trim());
  if ($("exDateFrom").value.trim()) q.set("date_from", $("exDateFrom").value.trim());
  if ($("exDateTo").value.trim()) q.set("date_to", $("exDateTo").value.trim());
  if ($("exHourFrom").value !== "") q.set("hour_from", $("exHourFrom").value);
  if ($("exHourTo").value !== "") q.set("hour_to", $("exHourTo").value);
  if ($("exPlace").value.trim()) q.set("place", $("exPlace").value.trim());
  if ($("exCamera").value) q.set("camera", $("exCamera").value);
  if ($("exType").value) q.set("type", $("exType").value);
  if ($("exExt").value) q.set("ext", $("exExt").value);
  if ($("exQuality").value) q.set("quality", $("exQuality").value);
  if ($("exGps").checked) q.set("has_gps", "1");
  if ($("exDupes").checked) q.set("dupes_only", "1");
  q.set("sort", $("exSort").value);
  q.set("limit", String(EX_PAGE));
  q.set("offset", String(offset));
  return q.toString();
}

async function doSearch(offset, queryOverride) {
  exLastQuery = queryOverride || exploreQueryString(offset);
  const u = new URLSearchParams(exLastQuery);
  u.set("offset", String(offset));
  u.set("limit", String(EX_PAGE));
  $("exCount").textContent = "Searching…";
  let data;
  try {
    data = await api("/api/explore?" + u.toString());
  } catch (e) {
    $("exCount").textContent = "Search failed: " + e.message;
    return;
  }
  exOffset = data.offset; exCount = data.count;
  renderExplore(data);
}

function renderExplore(data) {
  $("exCount").textContent = data.count === 0
    ? "No files match — try widening the filters."
    : `${data.count} file${data.count === 1 ? "" : "s"} found`;
  const grid = $("exGrid");
  grid.innerHTML = "";
  data.results.forEach((r) => {
    const cell = document.createElement("div");
    cell.className = "thumb-cell";
    const when = (r.taken_at || "").slice(0, 16) || "no date";
    const cap2 = [when, r.camera, r.location].filter(Boolean).join(" · ");
    const ext = (r.ext || "").replace(/^\./, "").toUpperCase();
    const badge = ext ? `<span class="ext-badge ext-${esc(r.mediaType || "photo")}">${esc(ext)}</span>` : "";
    cell.innerHTML = `<img loading="lazy" src="${r.thumbUrl}" alt="">${badge}
                      <button class="cell-reveal" title="Open file location">&#128193;</button>
                      <div class="cap" title="${esc(r.path)}">${esc(r.name)}</div>
                      <div class="cap2">${esc(cap2)}</div>`;
    cell.title = r.path;
    cell.querySelector(".cell-reveal").addEventListener("click", (e) => {
      e.stopPropagation();
      reveal(r.path);
    });
    cell.addEventListener("click", () => openPreview(r, cap2));
    grid.appendChild(cell);
  });
  setExStatus("Explore", undefined, `${data.count} files`);
  const pager = $("exPager");
  if (data.count > EX_PAGE) {
    pager.style.display = "";
    $("exPageInfo").textContent =
      `showing ${exOffset + 1}–${Math.min(exOffset + EX_PAGE, data.count)} of ${data.count}`;
    $("exPrev").disabled = exOffset === 0;
    $("exNext").disabled = exOffset + EX_PAGE >= data.count;
  } else {
    pager.style.display = "none";
  }
}

$("btnSearch").addEventListener("click", () => doSearch(0));
$("exPrev").addEventListener("click", () => doSearch(Math.max(0, exOffset - EX_PAGE)));
$("exNext").addEventListener("click", () => doSearch(exOffset + EX_PAGE));
$("btnOnThisDay").addEventListener("click", () => {
  $("exMonth").value = ""; $("exDay").value = ""; $("exYear").value = "";
  $("exDateFrom").value = ""; $("exDateTo").value = "";
  doSearch(0, "on_this_day=1&sort=date_asc&limit=" + EX_PAGE + "&offset=0");
});

function openPreview(r, caption) {
  $("previewTitle").textContent = caption || r.name;
  $("previewImg").src = r.thumbUrl + "&size=512";
  $("previewPath").textContent = r.path;
  $("previewOverlay").classList.remove("hidden");
  setExStatus("Preview", r.path, "");
}
$("previewClose").addEventListener("click", closePreview);
$("previewReveal").addEventListener("click", () => {
  const p = $("previewPath").textContent;
  if (p) reveal(p);
});
$("previewOverlay").addEventListener("click", (e) => {
  if (e.target === $("previewOverlay")) closePreview();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePreview();
});
function closePreview() {
  $("previewOverlay").classList.add("hidden");
  $("previewImg").src = "";
}

/* --------------------------- startup: sync page with server-side job state */
(async function syncWithServer() {
  try {
    const s = await api("/api/scan/status");
    if (s.state === "running") {
      showStep(1);
      $("btnScan").disabled = true;
      $("btnScanCancel").disabled = false;
      $("scanProgressBox").classList.remove("hidden");
      $("toStep2").disabled = true;
      setStatus("Scanning…");
      stopPoll();
      pollTimer = setInterval(pollScan, 500);
      pollScan();
    } else if (s.state === "done") {
      try { await loadResults(true); } catch (e) { /* no results yet */ }
    } else if (s.state === "cancelled") {
      $("scanProgressBox").classList.remove("hidden");
      $("scanCancelNote").classList.remove("hidden");
      $("scanCancelText").textContent =
        `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
      try { await loadResults(true); } catch (e) { /* partial optional */ }
    }
  } catch (e) { /* server unreachable — leave page as-is */ }
  try {
    const x = await api("/api/execute/status");
    if (x.state === "running") {
      showStep(5);
      $("doneWindow").classList.add("hidden");
      $("btnStartOver").classList.add("hidden");
      $("btnExecCancel").disabled = false;
      setStatus("Organizing…");
      stopPoll();
      pollTimer = setInterval(pollExecute, 500);
      pollExecute();
    }
  } catch (e) { /* ignore */ }
})();
