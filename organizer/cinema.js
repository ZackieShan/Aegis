/* Cinema Organizer - window logic (mirrors app.js patterns).
   Relies on app.js globals: $, api, post, esc, fmtBytes, setBar, reveal. */
"use strict";

let cResults = null;       // /api/cinema/results payload
let cPlan = null;          // /api/cinema/plan payload
let cPollTimer = null;

function cStopPoll() { if (cPollTimer) { clearInterval(cPollTimer); cPollTimer = null; } }
function cSetStatus(state, path, count) {
  $("cStatusState").textContent = state;
  if (path !== undefined) $("cStatusPath").textContent = path;
  if (count !== undefined) $("cStatusCount").textContent = count;
}

/* ---------------------------------------------------------- scan */
$("cBtnScan").addEventListener("click", async () => {
  const body = {
    path: $("cScanPath").value.trim(),
    max: $("cScanMax").value.trim(),
    hash: $("cHash").checked,
  };
  if (!body.path) { alert("Pick a folder to scan first."); return; }
  $("cBtnScan").disabled = true;
  try {
    await post("/api/cinema/scan", body);
  } catch (e) {
    alert(e.message);
    $("cBtnScan").disabled = false;
    return;
  }
  $("cScanProgressBox").classList.remove("hidden");
  $("cScanCancelNote").classList.add("hidden");
  $("cResults").classList.add("hidden");
  $("cOrganize").classList.add("hidden");
  $("cPlan").classList.add("hidden");
  $("cExecBox").classList.add("hidden");
  $("cDoneWindow").classList.add("hidden");
  $("cBtnStartOver").classList.add("hidden");
  $("cBtnScanCancel").disabled = false;
  cSetStatus("Scanning…");
  cStopPoll();
  cPollTimer = setInterval(cPollScan, 500);
});

async function cPollScan() {
  let s;
  try { s = await api("/api/cinema/scan/status"); } catch (e) { return; }
  $("cScanText").textContent = `${s.processed} / ${s.total}`;
  $("cScanFile").textContent = s.currentFile || "";
  setBar($("cScanBar"), $("cScanPct"), s.processed, s.total);
  cSetStatus("Scanning…", undefined, `${s.processed}/${s.total}`);
  if (s.state === "done") {
    cStopPoll();
    setBar($("cScanBar"), $("cScanPct"), 1, 1);
    $("cBtnScanCancel").disabled = true;
    $("cBtnScan").disabled = false;
    cSetStatus("Scan complete", undefined, `${s.total} files`);
    await cLoadResults();
  } else if (s.state === "cancelled") {
    cStopPoll();
    $("cBtnScan").disabled = false;
    $("cBtnScanCancel").disabled = true;
    $("cScanCancelNote").classList.remove("hidden");
    $("cScanCancelText").textContent =
      `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    cSetStatus("Scan cancelled", undefined, `${s.processed}/${s.total}`);
  } else if (s.state === "error") {
    cStopPoll();
    $("cBtnScan").disabled = false;
    cSetStatus("Scan error");
    alert("Scan failed: " + (s.error || "unknown"));
  }
}

$("cBtnScanCancel").addEventListener("click", async () => {
  $("cBtnScanCancel").disabled = true;
  try { await post("/api/cinema/scan/cancel", {}); } catch (e) { /* finished */ }
});
$("cBtnScanResume").addEventListener("click", () => {
  $("cScanCancelNote").classList.add("hidden");
  $("cBtnScan").click();
});

/* ---------------------------------------------------------- results */
async function cLoadResults(quiet) {
  cResults = await api("/api/cinema/results");
  cRenderResults(cResults);
  $("cResults").classList.remove("hidden");
  $("cOrganize").classList.remove("hidden");
  cSetStatus(cResults.partial ? "Partial results (cancelled scan)" : "Ready",
             cResults.scannedRoot, cResults.totalFiles + " media files");
  if (!$("cTargetRoot").value.trim()) {
    $("cTargetRoot").value =
      (cResults.scannedRoot || "").replace(/[\\/]+$/, "") + "\\Organized";
  }
}

function cStatRows(el, rows) {
  el.innerHTML = '<table class="stat-table">' + rows.map(
    ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") + "</table>";
}

function cRenderResults(r) {
  const bk = r.byKind || {};
  cStatRows($("cSumBody"), [
    ...(r.partial ? [["⚠ Scan", "PARTIAL (cancelled)"]] : []),
    ["Total indexed files", r.totalFiles],
    ["Movies", bk.movie || 0],
    ["TV episodes / packs", bk.tv || 0],
    ["Unidentified", bk.unknown || 0],
    ["Clutter", bk.clutter || 0],
    ["Genre source", r.hasTmdbKey ? "TMDB" : "none (Unclassified)"],
  ]);
  const gRows = (r.topGenres || []).map(([g, n]) => [g, n]);
  if (!gRows.length) gRows.push([r.hasTmdbKey ? "(none found)" : "(no TMDB key)", 0]);
  cStatRows($("cGenreBody"), gRows);
  const qRows = Object.entries(r.qualityMix || {})
    .sort((a, b) => b[1] - a[1]);
  if (r.lowQuality) qRows.push(["Low quality (cam/ts) ⚠", r.lowQuality]);
  if (!qRows.length) qRows.push(["(none)", 0]);
  cStatRows($("cQualityBody"), qRows);
  cStatRows($("cDupBody"), [
    ["Duplicate groups", r.dupeGroups],
    ["Duplicate files (non-best)", r.dupeFiles],
    ["Samples → _Samples\\", r.samples],
    ["Clutter → _Clutter\\", r.clutter],
    ["Unidentified → _Unidentified\\", r.unidentified],
  ]);
}

/* ---------------------------------------------------------- plan */
$("cToPlan").addEventListener("click", async () => {
  const expectEl = document.querySelector('input[name="cExpectKind"]:checked');
  const body = {
    action: document.querySelector('input[name="cAction"]:checked').value,
    targetRoot: $("cTargetRoot").value.trim(),
    expectKind: expectEl ? expectEl.value : "any",
    layout: (document.querySelector('input[name="cLayout"]:checked') || {}).value || "plex",
    splitByKind: !!($("cSplitByKind") && $("cSplitByKind").checked),
    movieYearFolder: !!($("cMovieYearFolder") && $("cMovieYearFolder").checked),
    writeNfo: !!($("cWriteNfo") && $("cWriteNfo").checked),
  };
  $("cToPlan").disabled = true;
  cSetStatus("Computing plan…");
  try {
    cPlan = await post("/api/cinema/plan", body);
  } catch (e) {
    alert("Plan failed: " + e.message);
    $("cToPlan").disabled = false;
    cSetStatus("Ready");
    return;
  }
  $("cToPlan").disabled = false;
  cRenderPlan(cPlan);
  $("cPlan").classList.remove("hidden");
  $("cExecBox").classList.add("hidden");
  $("cDoneWindow").classList.add("hidden");
  cSetStatus("Plan ready", cPlan.stats.targetRoot, cPlan.stats.totalFiles + " files");
});

const C_TAGS = {
  dupe: ["tag", (e) => "DUPE " + (e.groupId || "")],
  sample: ["tag tag-sample", () => "SAMPLE"],
  clutter: ["tag tag-clutter", () => "CLUTTER"],
  unidentified: ["tag tag-unidentified", () => "UNIDENTIFIED"],
  "cross-movie": ["tag tag-unidentified", () => "MOVIE → _Movies"],
  "cross-tv": ["tag tag-unidentified", () => "TV → _TV"],
};

function cRenderPlan(p) {
  const s = p.stats;
  $("cPlanStats").innerHTML =
    `<b>${s.totalFiles}</b> files will be <b>${s.action === "move" ? "moved" : "copied"}</b> into ` +
    `<b>${esc(s.targetRoot)}</b><br>` +
    (s.companionFiles ? `<b>${s.companionFiles}</b> subtitle companions move along &middot; ` : "") +
    `<b>${s.foldersToCreate}</b> new folders &middot; ` +
    `<b>${s.dupeFiles}</b> dupes &rarr; <code>_Duplicates\\</code> &middot; ` +
    `<b>${s.sampleFiles}</b> samples &middot; ` +
    `<b>${s.clutterFiles}</b> clutter &middot; ` +
    `<b>${s.unidentifiedFiles}</b> unidentified` +
    (s.crossMovieFiles ? ` &middot; <b>${s.crossMovieFiles}</b> movies &rarr; <code>_Movies\\</code>` : "") +
    (s.crossTvFiles ? ` &middot; <b>${s.crossTvFiles}</b> TV &rarr; <code>_TV\\</code>` : "") +
    (s.nfoFiles ? ` &middot; <b>${s.nfoFiles}</b> .nfo metadata files will be written` : "");
  if (typeof mountPlanReview === "function")
    mountPlanReview($("cPlanStats"), "/api/cinema/plan/summary", "cinema");
  const list = $("cPlanList");
  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  p.entries.forEach((e) => {
    const row = document.createElement("div");
    const reason = e.reason;
    row.className = "plan-row" +
      (reason === "dupe" ? " dupe" : reason ? " row-" + reason : "");
    let tagHtml;
    if (reason && C_TAGS[reason]) {
      const [cls, label] = C_TAGS[reason];
      tagHtml = `<span class="${cls}">${esc(label(e))}</span>`;
    } else {
      tagHtml = `<span class="tag oktag">${e.kind === "tv" ? "TV" : "MOVIE"}</span>`;
    }
    row.innerHTML = `${tagHtml}${esc(e.from)} <b>&rarr;</b> <span class="to">${esc(e.to)}</span>`;
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

/* ---------------------------------------------------------- execute */
$("cBtnExecute").addEventListener("click", async () => {
  if (!cPlan) return;
  const s = cPlan.stats;
  if (!confirm(`Really ${s.action} ${s.totalFiles} files into\n${s.targetRoot} ?`)) return;
  $("cExecBox").classList.remove("hidden");
  $("cDoneWindow").classList.add("hidden");
  $("cBtnStartOver").classList.add("hidden");
  $("cBtnExecCancel").disabled = false;
  $("cExecLog").innerHTML = "";
  $("cExecBar").style.width = "0%";
  cSetStatus("Organizing…");
  try {
    await post("/api/cinema/execute", {});
  } catch (e) {
    alert("Execute failed to start: " + e.message);
    cSetStatus("Ready");
    return;
  }
  cStopPoll();
  cPollTimer = setInterval(cPollExecute, 500);
});

async function cPollExecute() {
  let s;
  try { s = await api("/api/cinema/execute/status"); } catch (e) { return; }
  $("cExecText").textContent = `${s.processed} / ${s.total}`;
  $("cExecFile").textContent = s.currentFile || "";
  setBar($("cExecBar"), $("cExecPct"), s.processed, s.total);
  cSetStatus("Organizing…", undefined, `${s.processed}/${s.total}`);
  const log = $("cExecLog");
  log.innerHTML = (s.log || []).map((l) => `<div>${esc(l)}</div>`).join("");
  log.scrollTop = log.scrollHeight;
  if (s.state === "done" || s.state === "cancelled") {
    cStopPoll();
    $("cBtnExecCancel").disabled = true;
    if (s.state === "done") setBar($("cExecBar"), $("cExecPct"), 1, 1);
    cSetStatus(s.state === "done" ? "Done" : "Execute cancelled", undefined, "");
    cRenderDone(s.result || {});
  } else if (s.state === "error") {
    cStopPoll();
    cSetStatus("Execute error");
    alert("Execute failed: " + (s.error || "unknown"));
  }
}

$("cBtnExecCancel").addEventListener("click", async () => {
  $("cBtnExecCancel").disabled = true;
  try { await post("/api/cinema/execute/cancel", {}); } catch (e) { /* finished */ }
});

function cRenderDone(r) {
  $("cDoneWindow").classList.remove("hidden");
  $("cBtnStartOver").classList.remove("hidden");
  $("cDoneBody").innerHTML =
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
      <button id="cBtnUndo" class="danger">&#8617; Undo last run</button>
      <button id="cBtnOpenTarget">&#128193; Open folder</button>
      <span class="hint" id="cUndoMsg"></span>
    </div>`;
  const targetRoot = (cPlan && cPlan.stats && cPlan.stats.targetRoot)
    || (r.undoCopy ? r.undoCopy.replace(/[\\/][^\\/]+$/, "") : "");
  $("cBtnOpenTarget").addEventListener("click", () => reveal(targetRoot));
  $("cBtnUndo").addEventListener("click", async () => {
    if (!confirm("Undo the last run?\nMoves are reversed; copies are deleted.")) return;
    $("cBtnUndo").disabled = true;
    try {
      const res = await post("/api/cinema/undo", { manifest: r.undoFile });
      $("cUndoMsg").textContent =
        `Restored ${res.restored}, deleted ${res.deleted}, skipped ${res.skipped}, errors ${res.errors}.`;
      cSetStatus("Undo complete");
    } catch (e) {
      $("cBtnUndo").disabled = false;
      $("cUndoMsg").textContent = "Undo failed: " + e.message;
    }
  });
}

$("cBtnStartOver").addEventListener("click", () => {
  cPlan = null;
  $("cPlan").classList.add("hidden");
  $("cExecBox").classList.add("hidden");
  $("cDoneWindow").classList.add("hidden");
  $("cBtnStartOver").classList.add("hidden");
  cSetStatus("Ready", "", "");
});

/* ---------------------------------------------------------- TMDB key */
/* The server only ever returns MASKED secrets ("6b19…c9ae"). Defense in
   depth against destroying the real key: never post a value that contains
   the mask ellipsis or exactly equals the mask we were shown — the server
   applies the same guard, so the stored secret survives either way.
   Clearing happens ONLY via the explicit Clear button. */
let cLastCfg = {};        // last config payload from GET/POST config

function cRefreshKeyStatus(cfg, savedFlash) {
  cLastCfg = cfg || {};
  const bits = [];
  if (cfg.hasApiKey) bits.push(`API key ${cfg.tmdbKeyMasked}`);
  if (cfg.hasToken) bits.push(`read token ${cfg.tmdbTokenMasked}`);
  $("cKeyMsg").textContent = bits.length
    ? `configured: ${bits.join(" \u00B7 ")}${savedFlash ? " \u2713" : ""} \u2014 type a new value to override`
    : "no TMDB credentials \u2014 genres will be Unclassified";
}

function cCleanSecretInput(raw, masked) {
  const v = raw.trim();
  if (!v) return { skip: true };                    // blank = leave unchanged
  if (v.includes("\u2026") || v === masked) {
    return { skip: true, masked: true };            // the display mask, not a secret
  }
  return { value: v };
}

$("cBtnSaveKey").addEventListener("click", async () => {
  const body = {};
  const k = cCleanSecretInput($("cTmdbKey").value, cLastCfg.tmdbKeyMasked);
  const t = cCleanSecretInput($("cTmdbToken").value, cLastCfg.tmdbTokenMasked);
  if (k.value) body.tmdbKey = k.value;
  if (t.value) body.tmdbToken = t.value;
  if (!k.value && !t.value) {
    $("cKeyMsg").textContent = (k.masked || t.masked)
      ? "that\u2019s the masked display value \u2014 the stored key is unchanged; type a NEW key to replace it, or Clear to remove"
      : "nothing to save \u2014 type a key/token first, or use Clear";
    return;
  }
  $("cBtnSaveKey").disabled = true;
  try {
    const cfg = await post("/api/cinema/config", body);
    $("cTmdbKey").value = "";
    $("cTmdbToken").value = "";
    cRefreshKeyStatus(cfg, true);
  } catch (e) {
    $("cKeyMsg").textContent = "save failed: " + e.message;
  }
  $("cBtnSaveKey").disabled = false;
});

$("cBtnClearKey").addEventListener("click", async () => {
  if (!confirm("Remove the stored TMDB API key and read token?")) return;
  try {
    const cfg = await post("/api/cinema/config", { tmdbKey: "", tmdbToken: "" });
    $("cTmdbKey").value = "";
    $("cTmdbToken").value = "";
    cRefreshKeyStatus(cfg);
  } catch (e) {
    $("cKeyMsg").textContent = "clear failed: " + e.message;
  }
});

/* ---------------------------------------------------------- window open: sync with server */
$("winCinema").addEventListener("wm:open", async () => {
  try {
    const cfg = await api("/api/cinema/config");
    cRefreshKeyStatus(cfg);
  } catch (e) { /* ignore */ }
  try {
    const s = await api("/api/cinema/scan/status");
    if (s.state === "running") {
      $("cScanProgressBox").classList.remove("hidden");
      $("cBtnScan").disabled = true;
      $("cBtnScanCancel").disabled = false;
      cSetStatus("Scanning…");
      cStopPoll();
      cPollTimer = setInterval(cPollScan, 500);
      cPollScan();
      return;
    } else if (s.state === "cancelled") {
      $("cScanProgressBox").classList.remove("hidden");
      $("cScanCancelNote").classList.remove("hidden");
      $("cScanCancelText").textContent =
        `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    }
  } catch (e) { /* server unreachable */ }
  try {
    const x = await api("/api/cinema/execute/status");
    if (x.state === "running") {
      $("cExecBox").classList.remove("hidden");
      $("cBtnExecCancel").disabled = false;
      cSetStatus("Organizing…");
      cStopPoll();
      cPollTimer = setInterval(cPollExecute, 500);
      cPollExecute();
      return;
    }
  } catch (e) { /* ignore */ }
  try { await cLoadResults(true); } catch (e) { /* no results yet */ }
});

/* ---------------------------------------------------------- UI prefs
   Per-window preferences in localStorage: last source folder, last target
   root, move-vs-copy, hash toggle. Restored on load. Browse buttons are
   auto-wired by browse.js via data-browse-target (no per-button code here).
   savePrefs/getPrefs are defined in app.js (loaded before this file). */
function collectCinemaPrefs() {
  return {
    scanPath: $("cScanPath").value.trim(),
    targetRoot: $("cTargetRoot").value.trim(),
    action: (document.querySelector('input[name="cAction"]:checked') || {}).value,
    hash: $("cHash").checked,
  };
}
function persistCinemaPrefs() { savePrefs("cinema", collectCinemaPrefs()); }

(function initCinemaPrefs() {
  const p = getPrefs()["cinema"] || {};
  if (p.scanPath) $("cScanPath").value = p.scanPath;
  if (p.targetRoot) $("cTargetRoot").value = p.targetRoot;
  if (p.action) {
    const r = document.querySelector(`input[name="cAction"][value="${p.action}"]`);
    if (r) r.checked = true;
  }
  if (p.hash != null) $("cHash").checked = !!p.hash;
  ["cScanPath", "cTargetRoot", "cHash"]
    .forEach((id) => $(id).addEventListener("change", persistCinemaPrefs));
  document.querySelectorAll('input[name="cAction"]')
    .forEach((r) => r.addEventListener("change", persistCinemaPrefs));
  $("cBtnScan").addEventListener("click", persistCinemaPrefs);
  $("cToPlan").addEventListener("click", persistCinemaPrefs);
})();
