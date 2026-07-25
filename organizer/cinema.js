/* Cinema Organizer - window logic (mirrors app.js patterns).
   Relies on app.js globals: $, api, post, esc, fmtBytes, setBar, reveal. */
"use strict";

let cResults = null;       // /api/cinema/results payload
let cPlan = null;          // /api/cinema/plan payload
let cPollTimer = null;
let cExcluded = new Set();        // source paths unticked in the plan preview
let cAppliedExclude = new Set();  // exclusions baked into the loaded plan

/* Pipeline stepper: phases 1-3 stream from the backend chain; 4-5 are the
   user's calls and rendered as static reminders of what comes next. */
const C_PIPE_LABEL = { scan: "1&nbsp;&middot;&nbsp;Scan &amp; identify",
                       supervise: "2&nbsp;&middot;&nbsp;AI supervisor",
                       duplicates: "3&nbsp;&middot;&nbsp;Duplicate check" };
function cRenderPipeSteps(d) {
  const el = $("cPipeSteps");
  if (!el) return;
  const chip = (label, state, detail) =>
    `<span class="pipe-step ${state}" title="${esc(detail || "")}">` +
    `${state === "done" ? "&#10003; " : state === "running" ? "&#9203; " :
      state === "skipped" ? "&#8722; " :
      state === "error" || state === "cancelled" ? "&#9888; " : ""}` +
    `${label}${detail ? ` <span class="pipe-detail">${esc(detail)}</span>` : ""}</span>`;
  const parts = (d.phases || []).map((p) =>
    chip(C_PIPE_LABEL[p.name] || p.name, p.state, p.detail));
  parts.push(chip("4&nbsp;&middot;&nbsp;Build plan (you)", "pending", ""));
  parts.push(chip("5&nbsp;&middot;&nbsp;Execute (you)", "pending", ""));
  el.innerHTML = parts.join('<span class="pipe-arrow">&rarr;</span>');
}

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
  cExcluded = new Set();
  cAppliedExclude = new Set();
  cSetStatus("Scanning…");
  cStopPoll();
  cPollTimer = setInterval(cPollScan, 500);
});

async function cPollScan() {
  let d;
  try { d = await api("/api/cinema/pipeline/status"); } catch (e) { return; }
  const s = d.scan || {};
  cRenderPipeSteps(d);
  if (d.phase === "supervise" && d.supervise) {
    const sup = d.supervise;
    $("cScanText").textContent = `${sup.processed} / ${sup.total} folders`;
    $("cScanFile").textContent = sup.currentFile || "";
    setBar($("cScanBar"), $("cScanPct"), sup.processed, sup.total);
    cSetStatus("AI supervisor…", undefined,
               `${sup.identified || 0} identified`);
  } else if (d.phase === "duplicates") {
    $("cScanText").textContent = "Checking duplicate quality…";
    $("cScanFile").textContent = "";
    setBar($("cScanBar"), $("cScanPct"), 1, 1);
    cSetStatus("Duplicate check…");
  } else {
    $("cScanText").textContent = `${s.processed} / ${s.total}`;
    $("cScanFile").textContent = s.currentFile || "";
    setBar($("cScanBar"), $("cScanPct"), s.processed, s.total);
    cSetStatus("Scanning…", undefined, `${s.processed}/${s.total}`);
  }
  const state = d.state === "idle" ? s.state : d.state;
  if (state === "done") {
    cStopPoll();
    setBar($("cScanBar"), $("cScanPct"), 1, 1);
    $("cBtnScanCancel").disabled = true;
    $("cBtnScan").disabled = false;
    cSetStatus("Analysis complete", undefined, `${s.total} files`);
    await cLoadResults();
  } else if (state === "cancelled") {
    cStopPoll();
    $("cBtnScan").disabled = false;
    $("cBtnScanCancel").disabled = true;
    $("cScanCancelNote").classList.remove("hidden");
    $("cScanCancelText").textContent =
      `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    cSetStatus("Scan cancelled", undefined, `${s.processed}/${s.total}`);
  } else if (state === "error") {
    cStopPoll();
    $("cBtnScan").disabled = false;
    cSetStatus("Scan error");
    alert("Scan failed: " + (d.error || s.error || "unknown"));
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
  const tvBox = $("cTvSupBox");
  if (tvBox) {
    tvBox.classList.toggle("hidden", !(r.unidentified > 0));
    if (r.unidentified > 0) cRefreshTvScope();
  }
  const audit = r.dupeAudit;
  cStatRows($("cDupBody"), [
    ["Duplicate groups", r.dupeGroups],
    ["Duplicate files (non-best)", r.dupeFiles],
    ["Best-copy audit", audit
      ? `${audit.groups} checked, ${(audit.flagged || []).length} flagged`
      : "(not run yet)"],
    ["Samples → _Samples\\", r.samples],
    ["Clutter → _Clutter\\", r.clutter],
    ["Unidentified → _Unidentified\\", r.unidentified],
  ]);
  cRenderAudit(r);
}

/* Flagged dupe groups from the phase-3 audit: the handful of keeper
   decisions that were effectively coin tosses, so the user can eyeball
   THOSE instead of distrusting every group. */
const C_AUDIT_FLAG = {
  "quality-tie": "same quality either way",
  "no-quality-signal": "no quality tags — picked by size",
  "keeper-is-disc-rip": "keeping a disc rip over a playable file",
  "size-inversion": "kept file is much smaller — tags may overstate it",
};
function cRenderAudit(r) {
  const box = $("cAuditBox");
  if (!box) return;
  const a = r.dupeAudit;
  const fl = (a && a.flagged) || [];
  if (!a || !a.groups || !fl.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("cAuditSummary").textContent =
    `${a.groups} duplicate groups checked — ${fl.length} worth a look ` +
    `(the other ${a.clean} have a clear best copy). ` +
    "Untick files in the plan preview to override any keeper choice.";
  $("cAuditList").innerHTML = fl.slice(0, 100).map((g) => {
    const why = (g.flags || [])
      .map((f) => C_AUDIT_FLAG[f] || f).join("; ");
    const files = (g.files || []).map((f) =>
      `<div class="mono small ellipsis">${f.keep ? "&#10003; keep" : "&rarr; _Duplicates"} ` +
      `${esc(f.name)} <span class="hint">(${f.quality || "?"} q, ${fmtBytes(f.size)})</span></div>`)
      .join("");
    return `<div class="plan-row"><b>${esc(g.groupId)}</b> ${esc(g.title || "")}` +
      ` <span class="hint">— ${esc(why)}</span>${files}</div>`;
  }).join("");
}

/* ------------------------------------------------ LLM TV supervisor
   For episode files whose SERIES NAME is nowhere in the path (segment-title
   rips): a local model reads each folder's filenames together and proposes
   the show; TMDB must confirm it before anything is adopted. */
(function cInjectTvSupervisor() {
  const results = $("cResults");
  if (!results) return;
  const box = document.createElement("fieldset");
  box.id = "cTvSupBox";
  box.className = "hidden";
  box.innerHTML =
    "<legend>Unidentified media &mdash; AI supervisor</legend>" +
    '<div class="hint">Reads each folder&rsquo;s filenames together and asks your ' +
    "local model to identify them &mdash; a series from its episode titles, " +
    "or films from cryptic release names. <b>TMDB must confirm</b> every guess " +
    "before it's adopted, and nothing moves: this only fills in identities " +
    "so the normal plan &rarr; preview &rarr; undo flow can file them.</div>" +
    '<div class="field-row-stacked" style="margin-top:6px">' +
    '<label for="cTvSupPath">Look in (blank = everything unidentified from ' +
    'the last scan):</label>' +
    '<div class="path-row">' +
    '<input id="cTvSupPath" type="text" spellcheck="false">' +
    '<button id="cTvSupBrowse" type="button" data-browse-target="cTvSupPath" ' +
    'title="Pick a folder">&#128193; Browse&hellip;</button></div></div>' +
    '<div id="cTvSupScope" class="hint"></div>' +
    '<div class="field-row" style="margin-top:6px">' +
    '<label for="cTvSupModel">Model:</label>' +
    '<select id="cTvSupModel" style="min-width:190px"></select>' +
    '<span class="hint">bigger = better at recognising shows from ' +
    'episode titles</span></div>' +
    '<div class="field-row" style="margin-top:6px">' +
    '<button id="cBtnTvSup">&#129504; Identify unidentified media</button>' +
    '<button id="cBtnTvSupCancel" disabled>Cancel</button>' +
    '<span id="cTvSupStatus" class="hint"></span></div>' +
    '<div id="cTvSupLog" class="exec-log hidden" style="max-height:150px"></div>';
  results.parentNode.insertBefore(box, results.nextSibling);
  $("cBtnTvSup").addEventListener("click", cStartTvSup);
  $("cTvSupPath").addEventListener("change", cRefreshTvScope);
  $("cTvSupPath").addEventListener("blur", cRefreshTvScope);
  // populate the model picker; prefer a large general model — series
  // recognition is world-knowledge recall, where size wins
  (async () => {
    let d;
    try { d = await api("/api/llm/models"); } catch (e) { return; }
    const sel = $("cTvSupModel");
    const models = d.models || [];
    if (!models.length) { sel.innerHTML = '<option value="">(auto)</option>'; return; }
    const score = (m) => {
      const b = /(\d+(?:\.\d+)?)\s*b\b/i.exec(m.replace(/[-_]/g, " "));
      let n = b ? parseFloat(b[1]) : 0;
      if (/coder|tablellm|lite|embed|uncensored/i.test(m)) n -= 8;
      return n;
    };
    const best = models.slice().sort((a, b) => score(b) - score(a))[0];
    sel.innerHTML = models.map((m) =>
      `<option value="${esc(m)}"${m === best ? " selected" : ""}>${esc(m)}</option>`
    ).join("");
  })();
  $("cBtnTvSupCancel").addEventListener("click", async () => {
    $("cBtnTvSupCancel").disabled = true;
    try { await post("/api/cinema/tv-supervise/cancel", {}); } catch (e) {}
  });
})();

/* Show exactly what the supervisor will work on: how many unidentified
   files and the folder that holds them (after an organize they live under
   _Unidentified\, not the original scan root). */
async function cRefreshTvScope() {
  const out = $("cTvSupScope");
  if (!out) return;
  const p = ($("cTvSupPath") || {}).value || "";
  let s;
  try {
    s = await api("/api/cinema/tv-supervise/scope" +
                  (p ? "?path=" + encodeURIComponent(p.trim()) : ""));
  } catch (e) { out.textContent = ""; return; }
  if (!s.count) {
    out.innerHTML = p
      ? `No unidentified files under <code>${esc(p)}</code>.`
      : "Nothing unidentified in the last scan.";
    return;
  }
  out.innerHTML =
    `Target: <b>${s.count.toLocaleString()}</b> unidentified file` +
    (s.count === 1 ? "" : "s") + ` in <b>${s.folders}</b> folder` +
    (s.folders === 1 ? "" : "s") + ` under <code>${esc(s.root)}</code>` +
    (s.samples && s.samples.length
      ? ` &middot; e.g. ${esc(s.samples.slice(0, 2).join(", "))}` : "");
}

let cTvSupTimer = null;
async function cStartTvSup() {
  $("cBtnTvSup").disabled = true;
  $("cBtnTvSupCancel").disabled = false;
  $("cTvSupLog").classList.remove("hidden");
  $("cTvSupStatus").textContent = "Starting…";
  try {
    await post("/api/cinema/tv-supervise", {
      model: ($("cTvSupModel") || {}).value || undefined,
      path: (($("cTvSupPath") || {}).value || "").trim() || undefined,
    });
  } catch (e) {
    $("cTvSupStatus").textContent = e.message;
    $("cBtnTvSup").disabled = false;
    $("cBtnTvSupCancel").disabled = true;
    return;
  }
  cTvSupTimer = setInterval(async () => {
    let s;
    try { s = await api("/api/cinema/tv-supervise/status"); } catch (e) { return; }
    $("cTvSupLog").innerHTML = (s.log || []).map(
      (l) => `<div>${esc(l)}</div>`).join("");
    $("cTvSupLog").scrollTop = $("cTvSupLog").scrollHeight;
    if (s.state === "running") {
      $("cTvSupStatus").textContent =
        `Folder ${s.processed}/${s.total} · ${s.identified} episodes identified` +
        (s.currentFile ? ` · ${s.currentFile}` : "");
      return;
    }
    clearInterval(cTvSupTimer);
    $("cBtnTvSup").disabled = false;
    $("cBtnTvSupCancel").disabled = true;
    $("cTvSupStatus").textContent = s.state === "error"
      ? (s.error || "failed")
      : `${s.state} — ${s.identified} episodes identified, ${s.rejected} still unidentified`;
    if (s.state === "done" && s.identified) await cLoadResults(true);
  }, 900);
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
    discPolicy: (document.querySelector('input[name="cDiscPolicy"]:checked') || {}).value || "keep",
    restructure: !!($("cRestructure") && $("cRestructure").checked),
    ...(cExcluded.size ? { exclude: Array.from(cExcluded) } : {}),
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
  cAppliedExclude = new Set(cExcluded);
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
  "in-library": ["tag tag-unidentified", () => "IN LIBRARY"],
  "disc-rip": ["tag tag-clutter", (e) =>
    (e.disc === "iso" ? "ISO" : e.disc === "dvd" ? "DVD RIP" : "BD RIP") +
    " → _DiscRips" + (e.onlyCopy ? " ⚠ ONLY COPY" : "")],
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
    (s.nfoFiles ? ` &middot; <b>${s.nfoFiles}</b> .nfo metadata files will be written` : "") +
    (s.inLibraryFiles ? ` &middot; <b>${s.inLibraryFiles}</b> already in library &rarr; <code>_AlreadyInLibrary\\</code>` : "") +
    (s.upgradeFiles ? ` &middot; <b>${s.upgradeFiles}</b> quality upgrades of owned titles` : "") +
    (s.discPolicy === "quarantine" && s.discQuarantined
      ? ` &middot; <b>${s.discQuarantined}</b> disc rips &rarr; <code>_DiscRips\\</code>` +
        (s.discOnlyCopy ? ` (<b>${s.discOnlyCopy}</b> are the ONLY copy — remux before deleting!)` : "")
      : (s.discUnits ? ` &middot; <b>${s.discUnits}</b> disc rips (ISO/DVD/BD) moved whole` : ""));
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
      tagHtml = `<span class="tag oktag">${e.kind === "tv" ? "TV" : "MOVIE"}</span>` +
        (e.disc ? `<span class="tag tag-va">${e.disc === "iso" ? "ISO" : e.disc === "dvd" ? "DVD RIP" : "BD RIP"}</span>` : "") +
        (e.upgrade ? '<span class="tag tag-va">UPGRADE</span>' : "");
    }
    row.innerHTML =
      `<input type="checkbox" class="cExcl" checked title="untick to leave this file where it is">` +
      `${tagHtml}${esc(e.from)} <b>&rarr;</b> <span class="to">${esc(e.to)}</span>`;
    const excl = row.querySelector && row.querySelector(".cExcl");
    if (excl) excl.dataset.path = e.from;
    frag.appendChild(row);
  });
  list.appendChild(frag);
  cUpdateExcludeBar();
}

/* ------------------------------------------- plan-preview exclusions */
function cUpdateExcludeBar() {
  const bar = $("cExcludeBar");
  if (!bar) return;
  const pending = [...cExcluded].filter((p) => !cAppliedExclude.has(p)).length
    + [...cAppliedExclude].filter((p) => !cExcluded.has(p)).length;
  const n = cExcluded.size;
  bar.classList.toggle("hidden", !n && !pending);
  $("cExcludeText").textContent = pending
    ? `${n} file(s) excluded — rebuild the plan to apply`
    : n ? `${n} file(s) excluded from this plan` : "";
  $("cBtnReplan").classList.toggle("hidden", !pending);
}
if ($("cPlanList"))
  $("cPlanList").addEventListener("change", (ev) => {
    const cb = ev.target;
    if (!cb.classList || !cb.classList.contains("cExcl")) return;
    if (cb.checked) cExcluded.delete(cb.dataset.path);
    else cExcluded.add(cb.dataset.path);
    cUpdateExcludeBar();
  });
if ($("cBtnReplan"))
  $("cBtnReplan").addEventListener("click", () => $("cToPlan").click());

/* ---------------------------------------------------------- execute */
/* True when the Organize settings no longer match the plan that's loaded.
   The backend executes the SAVED plan, so anything changed after "Build plan
   preview" (above all the target root) must force a rebuild. */
function cNormPath(p) {
  return String(p || "").trim().replace(/[\\/]+$/, "").replace(/\//g, "\\")
    .toLowerCase();
}
function cPlanIsStale() {
  if (!cPlan || !cPlan.stats) return false;
  const s = cPlan.stats;
  const radio = (n, d) =>
    (document.querySelector(`input[name="${n}"]:checked`) || {}).value || d;
  const cb = (id) => !!($(id) && $(id).checked);
  if (cNormPath($("cTargetRoot").value) !== cNormPath(s.targetRoot)) return true;
  if (radio("cAction", "move") !== s.action) return true;
  if (radio("cLayout", "plex") !== (s.layout || "genre")) return true;
  if (radio("cExpectKind", "any") !== (s.expectKind || "any")) return true;
  if (radio("cDiscPolicy", "keep") !== (s.discPolicy || "keep")) return true;
  if (cb("cSplitByKind") !== !!s.splitByKind) return true;
  if (cb("cMovieYearFolder") !== !!s.movieYearFolder) return true;
  if (cb("cWriteNfo") !== !!s.writeNfo) return true;
  // exclusions ticked/unticked after the build must force a rebuild too
  if (cExcluded.size !== cAppliedExclude.size) return true;
  for (const p of cExcluded) if (!cAppliedExclude.has(p)) return true;
  return false;
}

$("cBtnExecute").addEventListener("click", async () => {
  if (!cPlan) return;
  const s = cPlan.stats;
  // Execute replays the SAVED plan. If the settings changed after it was
  // built (most importantly the target root), executing would silently use
  // the old values -- e.g. writing to a drive the user just corrected away
  // from. Force a rebuild instead.
  if (cPlanIsStale()) {
    alert("These settings changed since the plan was built.\n\n" +
          "The saved plan still targets:\n  " + s.targetRoot +
          "\n\nClick “Build plan preview” again to apply your changes.");
    return;
  }
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
  cExcluded = new Set();
  cAppliedExclude = new Set();
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
    // the pipeline runs past the scan itself (supervisor + dupe audit), so
    // re-attach while EITHER is running — scan status alone reads "done"
    // during phases 2-3 and would abandon a live run
    const pipe = await api("/api/cinema/pipeline/status");
    const s = pipe.scan || {};
    if (pipe.state === "running" || s.state === "running") {
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
