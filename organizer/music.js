/* Music Organizer - window logic (mirrors cinema.js patterns).
   Relies on app.js globals: $, api, post, esc, fmtBytes, setBar, reveal.

   Backend contract (music.py, served under /api/music/ by server.py):
     GET  scan/status    -> {state,total,processed,currentFile,error,phase?,
                             phaseDone?,phaseTotal?,phaseElapsed?,phaseRate?,
                             phaseEta?,note?}   (phase-scoped counters + ETA
                             for long non-file phases: identify = albums,
                             fingerprint = files)
     GET  execute/status -> {state,total,processed,currentFile,log,error,
                             result,phase?,phaseDone?,phaseTotal?}
     GET  results        -> {scannedRoot,partial,totalFiles,byCodec:{codec:n},
                             topGenres:[[name,n]...],genreCoverage,identified,
                             unidentified,singles,compilations,dupeGroups,
                             dupeFiles,upgradesAvailable,pendingIdentify,note,
                             quickCleanEligible?,dupeBytes?,dupesQuarantined?}
                             (quickCleanEligible=true when a scan finished the
                             LOCAL phases only and identification is pending;
                             dupeBytes = dupe-loser bytes for the reclaimable
                             estimate; dupesQuarantined = dupe losers already
                             moved aside by a first-pass cleanup)
     GET  plan           -> {stats:{action,targetRoot,totalFiles,companionFiles,
                             foldersToCreate,dupeFiles,unidentifiedFiles},
                             entries:[{from,to,reason,kind,groupId?,companions?}],
                             params:{...}}
     GET  config         -> {hasAcoustidKey,acoustidKeyMasked,hasDiscogsToken,
                             discogsTokenMasked,hasLastfmKey,lastfmKeyMasked,
                             fingerprintAvailable,fpcalcPath?}
     POST scan           {root,maxFiles,hashEnabled,fingerprintEnabled,
                          skipIdentify}
     POST scan/cancel    {}
     POST identify/resume {}  -> identifies ONLY clusters missing a releases
                          row (skipped / paused / killed identifies); reuses
                          scan/status + scan/cancel for progress
     POST plan           {action:"move"|"copy", targetRoot,
                          dupeHandling:"quarantine"|"keep",
                          discStyle:"subfolder"|"merge"}
                       OR {mode:"dupes_only", targetRoot, action:"move"|"copy"}
                          -> FIRST-PASS duplicate cleanup: plan entries ONLY
                          for dupe losers, dest <targetRoot>\_Duplicates\Gxx\
                          <original filename> (default targetRoot = scanned
                          root); stats.mode:"dupes_only". Executed through the
                          SAME execute/undo machinery; quarantined tracks are
                          flagged in the DB so identify/resume and later
                          organize plans skip them.
     POST execute        {}
     POST execute/cancel {}
     POST undo           {manifest}
     POST config         {acoustidKey?,discogsToken?,lastfmKey?} ("" clears)

   Extra UI (injected at load so index.html stays untouched):
   skip-identify checkbox, phase progress bar + ETA, auto-pause note,
   Resume identification box, and the "First pass: duplicates" quick-clean
   panel (plan {mode:"dupes_only"} -> preview -> execute -> identify the
   survivors). Browse buttons live in index.html as
   <button data-browse-target="..."> and are auto-wired by browse.js.
*/
"use strict";

let mResults = null;       // /api/music/results payload
let mPlan = null;          // /api/music/plan payload
let mPollTimer = null;
let mCleanedCount = 0;     // dupe losers quarantined by a dupes_only run this session

function mStopPoll() { if (mPollTimer) { clearInterval(mPollTimer); mPollTimer = null; } }
function mSetStatus(state, path, count) {
  $("mStatusState").textContent = state;
  if (path !== undefined) $("mStatusPath").textContent = path;
  if (count !== undefined) $("mStatusCount").textContent = count;
}
function mNum(v) { return v == null ? 0 : v; }

/* ------------------------------------------------ injected controls
   Added at load so index.html stays untouched: skip-identify checkbox,
   phase progress bar + ETA, scan note line, the Resume identification box,
   and the "First pass: duplicates" quick-clean panel on the results screen.
   (Browse buttons are native index.html markup with data-browse-target,
   auto-wired by browse.js — do NOT inject duplicates here.) */
(function mInjectExtras() {
  // 'Skip online identification (fast)' under the fingerprint row
  const fp = $("mFingerprint");
  if (fp) {
    const fpRow = fp.closest(".field-row");
    const skipRow = document.createElement("div");
    skipRow.className = "field-row";
    skipRow.innerHTML =
      '<label><input type="checkbox" id="mSkipIdentify"> ' +
      "Skip online identification (fast &mdash; no MusicBrainz/AcoustID; " +
      "albums stay unidentified, resume later)</label>";
    fpRow.parentNode.insertBefore(skipRow, fpRow.nextSibling);
  }

  // second progress bar for phase-scoped work (identify / fingerprint)
  const scanBar = $("mScanBar");
  if (scanBar) {
    const wrap = document.createElement("div");
    wrap.id = "mPhaseWrap";
    wrap.className = "hidden";
    wrap.innerHTML =
      '<div class="progress"><div class="progress-fill" id="mPhaseBar"></div></div>' +
      '<div class="progress-text"><span id="mPhaseText"></span>' +
      '<span id="mPhasePct"></span></div>';
    const mainProgress = scanBar.parentNode;
    mainProgress.parentNode.insertBefore(wrap, mainProgress.nextSibling);
    const note = document.createElement("div");
    note.id = "mScanNote";
    note.className = "hint hidden";
    wrap.parentNode.insertBefore(note, wrap.nextSibling);
  }

  // 'Resume identification' box on the results screen
  const results = $("mResults");
  if (results) {
    const box = document.createElement("fieldset");
    box.id = "mResumeBox";
    box.className = "hidden";
    box.innerHTML =
      "<legend>Online identification</legend>" +
      '<div id="mResumeText" class="hint"></div>' +
      '<div class="field-row" style="margin-top:6px">' +
      '<button id="mBtnResumeIdentify">&#128269; Resume identification</button>' +
      '<span class="hint">runs only the MusicBrainz identify phase ' +
      "(throttled &asymp;1 req/s)</span></div>";
    results.parentNode.insertBefore(box, results.nextSibling);
    $("mBtnResumeIdentify").addEventListener("click", () => {
      mStartIdentify($("mBtnResumeIdentify"));
    });

    // 'First pass: duplicates' quick-clean panel (before the resume box)
    const quick = document.createElement("fieldset");
    quick.id = "mQuickClean";
    quick.className = "hidden";
    quick.innerHTML =
      "<legend>First pass: duplicates</legend>" +
      '<div id="mQuickCleanText"></div>' +
      '<div class="hint" style="margin-top:2px">Found by the local phases ' +
      "only &mdash; no network. Quarantining the losers now means " +
      "MusicBrainz identification runs only on the keepers.</div>" +
      '<div class="field-row" style="margin-top:6px">' +
      '<button id="mBtnQuickClean">&#129529; Clean duplicates now ' +
      "(fast, undoable)</button>" +
      '<button id="mBtnSkipClean">Skip &mdash; go straight to ' +
      "identification</button></div>";
    results.parentNode.insertBefore(quick, results.nextSibling);
    $("mBtnQuickClean").addEventListener("click", mQuickCleanPlan);
    $("mBtnSkipClean").addEventListener("click", () => {
      mStartIdentify($("mBtnSkipClean"));
    });

    // 'Fix missing tags' panel (after the resume box): writes identified
    // values into files' EMPTY tag fields only; backed up + undoable.
    const tagbox = document.createElement("fieldset");
    tagbox.id = "mTagfixBox";
    tagbox.className = "hidden";
    tagbox.innerHTML =
      "<legend>Tags</legend>" +
      '<div class="hint">Fill <b>missing</b> tag fields (artist, album, ' +
      "year, genre, track #) in the files themselves from identified " +
      "values. Existing tags are never changed; every write is backed up " +
      "and undoable; audio bytes are verified untouched.</div>" +
      '<div class="field-row" style="margin-top:6px">' +
      '<button id="mBtnTagfix">&#127991; Fix missing tags</button>' +
      '<button id="mBtnTagfixUndo">Undo last tag fix</button>' +
      '<span id="mTagfixStatus" class="hint"></span></div>';
    box.parentNode.insertBefore(tagbox, box.nextSibling);
    $("mBtnTagfix").addEventListener("click", mStartTagfix);
    $("mBtnTagfixUndo").addEventListener("click", mUndoTagfix);
  }
})();

/* -------------------------------------------------- tag write-back */
let mTagfixTimer = null;
async function mStartTagfix() {
  $("mBtnTagfix").disabled = true;
  $("mTagfixStatus").textContent = "Starting…";
  try {
    await post("/api/music/tagfix", { mode: "missing" });
  } catch (e) {
    $("mTagfixStatus").textContent = e.message;
    $("mBtnTagfix").disabled = false;
    return;
  }
  mTagfixTimer = setInterval(async () => {
    let s;
    try { s = await api("/api/music/tagfix/status"); } catch (e) { return; }
    if (s.state === "running") {
      $("mTagfixStatus").textContent =
        `Writing… ${s.processed}/${s.total} (${s.changed} changed)`;
      return;
    }
    clearInterval(mTagfixTimer);
    $("mBtnTagfix").disabled = false;
    $("mTagfixStatus").textContent = s.state === "done"
      ? `Done — filled tags in ${s.changed} file(s)` +
        (s.errors ? `, ${s.errors} error(s)` : "") +
        (s.changed ? " (backup saved)" : "")
      : (s.error || s.state);
  }, 700);
}
async function mUndoTagfix() {
  $("mBtnTagfixUndo").disabled = true;
  try {
    const r = await post("/api/music/tagfix/undo", {});
    $("mTagfixStatus").textContent =
      `Undo: ${r.restored} restored` + (r.errors ? `, ${r.errors} errors` : "");
  } catch (e) {
    $("mTagfixStatus").textContent = e.message;
  } finally {
    $("mBtnTagfixUndo").disabled = false;
  }
}

/* Start (or resume) the MusicBrainz identify phase. Used by the Resume
   box, the quick-clean Skip button, and the post-cleanup 'identify the
   survivors' button. The backend identifies keepers only: dupe losers are
   skipped even before quarantine. */
async function mStartIdentify(btn) {
  if (btn) btn.disabled = true;
  try {
    await post("/api/music/identify/resume", {});
  } catch (e) {
    alert(e.message);
    if (btn) btn.disabled = false;
    return;
  }
  const box = $("mResumeBox");
  if (box) box.classList.add("hidden");
  const quick = $("mQuickClean");
  if (quick) quick.classList.add("hidden");
  const tb = $("mTagfixBox");
  if (tb) tb.classList.add("hidden");
  $("mScanProgressBox").classList.remove("hidden");
  $("mScanCancelNote").classList.add("hidden");
  $("mResults").classList.add("hidden");
  $("mOrganize").classList.add("hidden");
  $("mPlan").classList.add("hidden");
  $("mBtnScan").disabled = true;
  $("mBtnScanCancel").disabled = false;
  mSetStatus("Identifying albums…");
  mStopPoll();
  mPollTimer = setInterval(mPollScan, 500);
  mPollScan();
}

/* 'Clean duplicates now': build a dupes_only plan (dupe losers only) and
   show it in the normal plan preview for confirmation. */
async function mQuickCleanPlan() {
  const body = {
    mode: "dupes_only",
    targetRoot: $("mTargetRoot").value.trim()
      || (mResults && mResults.scannedRoot) || "",
    action: document.querySelector('input[name="mAction"]:checked').value,
  };
  $("mBtnQuickClean").disabled = true;
  mSetStatus("Computing duplicate-cleanup plan…");
  try {
    mPlan = await post("/api/music/plan", body);
  } catch (e) {
    alert("Plan failed: " + e.message);
    $("mBtnQuickClean").disabled = false;
    mSetStatus("Ready");
    return;
  }
  $("mBtnQuickClean").disabled = false;
  mRenderPlan(mPlan);
  $("mPlan").classList.remove("hidden");
  $("mExecBox").classList.add("hidden");
  $("mDoneWindow").classList.add("hidden");
  $("mBtnStartOver").classList.add("hidden");
  mSetStatus("Cleanup plan ready", mPlan.stats.targetRoot,
             mPlan.stats.totalFiles + " duplicate files");
}

const M_PHASE_LABEL = {
  scan: "Scanning files",
  identify: "Identifying albums",
  fingerprint: "Fingerprinting files",
  dedupe: "Deduplicating",
};

function mFmtEta(sec) {
  if (sec == null || !Number.isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function mRenderPhase(s) {
  const wrap = $("mPhaseWrap");
  if (!wrap) return;
  const phase = s.phase || "";
  const pTot = s.phaseTotal || 0;
  const pDone = s.phaseDone || 0;
  if (s.state === "running" && phase && phase !== "scan" && pTot > 0) {
    wrap.classList.remove("hidden");
    const label = M_PHASE_LABEL[phase] || ("Working: " + phase);
    const eta = s.phaseEta != null
      ? ` (~${mFmtEta(s.phaseEta)} remaining)` : "";
    $("mPhaseText").textContent =
      `${label}: ${pDone.toLocaleString()} / ${pTot.toLocaleString()}${eta}`;
    setBar($("mPhaseBar"), $("mPhasePct"), pDone, pTot);
  } else {
    wrap.classList.add("hidden");
  }
  const note = $("mScanNote");
  if (note) {
    note.classList.toggle("hidden", !s.note);
    if (s.note) note.textContent = s.note;
  }
}

/* ---------------------------------------------------------- scan */
$("mBtnScan").addEventListener("click", async () => {
  let maxFiles = 0;
  const rawMax = $("mScanMax").value.trim();
  if (rawMax) {
    maxFiles = parseInt(rawMax, 10);
    if (!Number.isFinite(maxFiles) || maxFiles < 0) maxFiles = 0;
  }
  const body = {
    root: $("mScanPath").value.trim(),
    maxFiles,
    hashEnabled: $("mHash").checked,
    fingerprintEnabled: $("mFingerprint").checked && !$("mFingerprint").disabled,
    skipIdentify: !!($("mSkipIdentify") && $("mSkipIdentify").checked),
  };
  if (!body.root) { alert("Pick a folder to scan first."); return; }
  $("mBtnScan").disabled = true;
  try {
    await post("/api/music/scan", body);
  } catch (e) {
    alert(e.message);
    $("mBtnScan").disabled = false;
    return;
  }
  $("mScanProgressBox").classList.remove("hidden");
  $("mScanCancelNote").classList.add("hidden");
  $("mResults").classList.add("hidden");
  $("mOrganize").classList.add("hidden");
  $("mPlan").classList.add("hidden");
  $("mExecBox").classList.add("hidden");
  $("mDoneWindow").classList.add("hidden");
  $("mBtnStartOver").classList.add("hidden");
  const phaseWrap0 = $("mPhaseWrap");
  if (phaseWrap0) phaseWrap0.classList.add("hidden");
  const note0 = $("mScanNote");
  if (note0) note0.classList.add("hidden");
  const resumeBox0 = $("mResumeBox");
  if (resumeBox0) resumeBox0.classList.add("hidden");
  const quickBox0 = $("mQuickClean");
  if (quickBox0) quickBox0.classList.add("hidden");
  const tagBox0 = $("mTagfixBox");
  if (tagBox0) tagBox0.classList.add("hidden");
  mCleanedCount = 0;
  $("mBtnScanCancel").disabled = false;
  mSetStatus("Scanning…");
  mStopPoll();
  mPollTimer = setInterval(mPollScan, 500);
});

async function mPollScan() {
  let s;
  try { s = await api("/api/music/scan/status"); } catch (e) { return; }
  $("mScanText").textContent = `${s.processed} / ${s.total}`;
  $("mScanFile").textContent =
    (s.phase ? s.phase + ": " : "") + (s.currentFile || "");
  setBar($("mScanBar"), $("mScanPct"), s.processed, s.total);
  mRenderPhase(s);
  mSetStatus("Scanning…", undefined, `${s.processed}/${s.total}`);
  if (s.state === "done") {
    mStopPoll();
    setBar($("mScanBar"), $("mScanPct"), 1, 1);
    mRenderPhase(s);
    $("mBtnScanCancel").disabled = true;
    $("mBtnScan").disabled = false;
    const rbtn = $("mBtnResumeIdentify");
    if (rbtn) rbtn.disabled = false;
    mSetStatus(s.note ? "Scan complete (identification pending)"
                      : "Scan complete", undefined, `${s.total} files`);
    await mLoadResults();
  } else if (s.state === "cancelled") {
    mStopPoll();
    $("mBtnScan").disabled = false;
    $("mBtnScanCancel").disabled = true;
    $("mScanCancelNote").classList.remove("hidden");
    $("mScanCancelText").textContent =
      `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    mSetStatus("Scan cancelled", undefined, `${s.processed}/${s.total}`);
  } else if (s.state === "error") {
    mStopPoll();
    $("mBtnScan").disabled = false;
    mSetStatus("Scan error");
    alert("Scan failed: " + (s.error || "unknown"));
  }
}

$("mBtnScanCancel").addEventListener("click", async () => {
  $("mBtnScanCancel").disabled = true;
  try { await post("/api/music/scan/cancel", {}); } catch (e) { /* finished */ }
});
$("mBtnScanResume").addEventListener("click", () => {
  $("mScanCancelNote").classList.add("hidden");
  $("mBtnScan").click();
});

/* ---------------------------------------------------------- results */
async function mLoadResults(quiet) {
  mResults = await api("/api/music/results");
  mRenderResults(mResults);
  $("mResults").classList.remove("hidden");
  $("mOrganize").classList.remove("hidden");
  mSetStatus(mResults.partial ? "Partial results (cancelled scan)" : "Ready",
             mResults.scannedRoot, mResults.totalFiles + " audio files");
  if (!$("mTargetRoot").value.trim()) {
    $("mTargetRoot").value =
      (mResults.scannedRoot || "").replace(/[\\/]+$/, "") + "\\Organized";
  }
}

function mStatRows(el, rows) {
  el.innerHTML = '<table class="stat-table">' + rows.map(
    ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") + "</table>";
}

function mRenderResults(r) {
  mStatRows($("mSumBody"), [
    ...(r.partial ? [["⚠ Scan", "PARTIAL (cancelled)"]] : []),
    ["Total audio files", mNum(r.totalFiles)],
    ["Identified tracks", mNum(r.identified)],
    ["Compilations (Various Artists)", mNum(r.compilations)],
    ["Loose singles", mNum(r.singles)],
    ["Unidentified", mNum(r.unidentified)],
  ]);
  const cRows = Object.entries(r.byCodec || {})
    .sort((a, b) => b[1] - a[1])
    .map(([c, n]) => [c.toUpperCase(), n]);
  if (!cRows.length) cRows.push(["(none)", 0]);
  mStatRows($("mCodecBody"), cRows);
  const gRows = (r.topGenres || []).map(([g, n]) => [g, n]);
  if (r.genreCoverage != null) {
    gRows.unshift(["Genre coverage",
      typeof r.genreCoverage === "number" ? r.genreCoverage + "%" : r.genreCoverage]);
  }
  if (!gRows.length) gRows.push(["(none found)", 0]);
  mStatRows($("mGenreBody"), gRows);
  const quar = mNum(r.dupesQuarantined) || mCleanedCount;
  mStatRows($("mDupBody"), [
    ["Duplicate groups", mNum(r.dupeGroups)],
    ["Duplicate files (non-best)", mNum(r.dupeFiles)],
    ["Upgrades available (better copy exists)", mNum(r.upgradesAvailable)],
    ["Unidentified → _Unidentified\\", mNum(r.unidentified)],
    ...(quar ? [["Duplicates quarantined",
                 quar + " — hidden from identification"]] : []),
  ]);
  // 'First pass: duplicates' quick-clean panel: only when the scan stopped
  // after the LOCAL phases and there is something to clean.
  let quickShown = false;
  const quickBox = $("mQuickClean");
  if (quickBox) {
    const groups = mNum(r.dupeGroups);
    if (r.quickCleanEligible && groups > 0 && !quar) {
      const files = mNum(r.dupeFiles);
      $("mQuickCleanText").innerHTML =
        `<b>${files.toLocaleString()}</b> duplicate files in ` +
        `<b>${groups.toLocaleString()}</b> groups` +
        (r.dupeBytes ? ` &mdash; est. <b>${esc(fmtBytes(r.dupeBytes))}</b> reclaimable` : "");
      quickBox.classList.remove("hidden");
      quickShown = true;
    } else {
      quickBox.classList.add("hidden");
    }
  }
  const box = $("mResumeBox");
  if (box) {
    const pend = r.pendingIdentify || 0;
    if (pend > 0 && !quickShown) {
      box.classList.remove("hidden");
      $("mResumeText").textContent =
        (r.note ? r.note + " — " : "") +
        `${pend.toLocaleString()} album cluster${pend === 1 ? "" : "s"} ` +
        "still unidentified (no releases row). Resume when you're online " +
        "to fetch canonical names, years, genres and cover art.";
    } else {
      box.classList.add("hidden");
    }
  }
  // Tags panel: whenever results are on screen (works best after identify,
  // when fields are MusicBrainz-canonical, but tag/filename-derived values
  // help too — it only ever fills EMPTY fields)
  const tagBox = $("mTagfixBox");
  if (tagBox) tagBox.classList.remove("hidden");
}

/* ---------------------------------------------------------- plan */
$("mToPlan").addEventListener("click", async () => {
  const body = {
    action: document.querySelector('input[name="mAction"]:checked').value,
    targetRoot: $("mTargetRoot").value.trim(),
    dupeHandling: document.querySelector('input[name="mDupeHandling"]:checked').value,
    discStyle: document.querySelector('input[name="mDiscStyle"]:checked').value,
    layout: (document.querySelector('input[name="mLayout"]:checked') || {}).value || "artist",
  };
  $("mToPlan").disabled = true;
  mSetStatus("Computing plan…");
  try {
    mPlan = await post("/api/music/plan", body);
  } catch (e) {
    alert("Plan failed: " + e.message);
    $("mToPlan").disabled = false;
    mSetStatus("Ready");
    return;
  }
  $("mToPlan").disabled = false;
  mRenderPlan(mPlan);
  $("mPlan").classList.remove("hidden");
  $("mExecBox").classList.add("hidden");
  $("mDoneWindow").classList.add("hidden");
  mSetStatus("Plan ready", mPlan.stats.targetRoot, mPlan.stats.totalFiles + " files");
});

const M_TAGS = {
  dupe: ["tag", (e) => "DUPE " + (e.groupId || "")],
  va: ["tag tag-va", () => "VA"],
  unidentified: ["tag tag-unidentified", () => "UNIDENTIFIED"],
};

function mRenderPlan(p) {
  const s = p.stats;
  if (s.mode === "dupes_only") {
    const groups = new Set(p.entries.map((e) => e.groupId).filter(Boolean)).size;
    $("mPlanStats").innerHTML =
      `<b>${s.totalFiles}</b> duplicate files in <b>${groups}</b> groups will be ` +
      `<b>${s.action === "move" ? "moved" : "copied"}</b> to ` +
      `<code>_Duplicates\\Gxx\\</code> under <b>${esc(s.targetRoot)}</b><br>` +
      "Best copies stay in place &middot; quarantined files are hidden from " +
      "identification and later organize plans &middot; fully undoable.";
  } else {
    $("mPlanStats").innerHTML =
      `<b>${s.totalFiles}</b> files will be <b>${s.action === "move" ? "moved" : "copied"}</b> into ` +
      `<b>${esc(s.targetRoot)}</b><br>` +
      (s.companionFiles ? `<b>${s.companionFiles}</b> companions (art/cue/log/lrc/nfo) ride along &middot; ` : "") +
      `<b>${s.foldersToCreate}</b> new folders &middot; ` +
      `<b>${s.dupeFiles}</b> dupes &rarr; <code>_Duplicates\\</code> &middot; ` +
      `<b>${s.unidentifiedFiles}</b> unidentified`;
  }
  $("mBtnExecute").innerHTML = s.mode === "dupes_only"
    ? "&#10004; Looks good &mdash; quarantine these duplicates"
    : "&#10004; Looks good &mdash; organize my music";
  if (typeof mountPlanReview === "function")
    mountPlanReview($("mPlanStats"), "/api/music/plan/summary", "music");
  const list = $("mPlanList");
  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  p.entries.forEach((e) => {
    const row = document.createElement("div");
    const reason = e.reason;
    row.className = "plan-row" +
      (reason === "dupe" ? " dupe" : reason ? " row-" + reason : "");
    let tagHtml;
    if (reason && M_TAGS[reason]) {
      const [cls, label] = M_TAGS[reason];
      tagHtml = `<span class="${cls}">${esc(label(e))}</span>`;
    } else {
      tagHtml = `<span class="tag oktag">${esc((e.kind || "track").toUpperCase())}</span>`;
    }
    const comp = (e.companions && e.companions.length)
      ? ` <span class="hint">+${e.companions.length}</span>` : "";
    row.innerHTML = `${tagHtml}${esc(e.from)} <b>&rarr;</b> <span class="to">${esc(e.to)}</span>${comp}`;
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

/* ---------------------------------------------------------- execute */
$("mBtnExecute").addEventListener("click", async () => {
  if (!mPlan) return;
  const s = mPlan.stats;
  if (!confirm(`Really ${s.action} ${s.totalFiles} files into\n${s.targetRoot} ?`)) return;
  $("mExecBox").classList.remove("hidden");
  $("mDoneWindow").classList.add("hidden");
  $("mBtnStartOver").classList.add("hidden");
  $("mBtnExecCancel").disabled = false;
  $("mExecLog").innerHTML = "";
  $("mExecBar").style.width = "0%";
  mSetStatus("Organizing…");
  try {
    await post("/api/music/execute", {});
  } catch (e) {
    alert("Execute failed to start: " + e.message);
    mSetStatus("Ready");
    return;
  }
  mStopPoll();
  mPollTimer = setInterval(mPollExecute, 500);
});

async function mPollExecute() {
  let s;
  try { s = await api("/api/music/execute/status"); } catch (e) { return; }
  $("mExecText").textContent = `${s.processed} / ${s.total}`;
  $("mExecFile").textContent =
    s.phase === "artwork" && s.phaseTotal > 0
      ? `Fetching artwork: ${s.phaseDone || 0} / ${s.phaseTotal}`
      : (s.currentFile || "");
  setBar($("mExecBar"), $("mExecPct"), s.processed, s.total);
  mSetStatus("Organizing…", undefined, `${s.processed}/${s.total}`);
  const log = $("mExecLog");
  log.innerHTML = (s.log || []).map((l) => `<div>${esc(l)}</div>`).join("");
  log.scrollTop = log.scrollHeight;
  if (s.state === "done" || s.state === "cancelled") {
    mStopPoll();
    $("mBtnExecCancel").disabled = true;
    if (s.state === "done") setBar($("mExecBar"), $("mExecPct"), 1, 1);
    mSetStatus(s.state === "done" ? "Done" : "Execute cancelled", undefined, "");
    mRenderDone(s.result || {});
    if (mPlan && mPlan.stats && mPlan.stats.mode === "dupes_only") {
      // first-pass cleanup finished: refresh results so counts, the
      // 'quarantined' note and the resume box reflect the keepers-only DB
      mCleanedCount =
        ((s.result && (s.result.moved || 0) + (s.result.copied || 0)))
        || mCleanedCount;
      try { await mLoadResults(true); } catch (e) { /* keep done panel */ }
      mSetStatus(s.state === "done" ? "Duplicates quarantined"
                                    : "Execute cancelled",
                 undefined, mCleanedCount ? mCleanedCount + " quarantined" : "");
    }
  } else if (s.state === "error") {
    mStopPoll();
    mSetStatus("Execute error");
    alert("Execute failed: " + (s.error || "unknown"));
  }
}

$("mBtnExecCancel").addEventListener("click", async () => {
  $("mBtnExecCancel").disabled = true;
  try { await post("/api/music/execute/cancel", {}); } catch (e) { /* finished */ }
});

function mRenderDone(r) {
  const dupesOnly = mPlan && mPlan.stats && mPlan.stats.mode === "dupes_only";
  $("mDoneWindow").classList.remove("hidden");
  $("mBtnStartOver").classList.remove("hidden");
  $("mDoneBody").innerHTML =
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
      <button id="mBtnUndo" class="danger">&#8617; Undo last run</button>
      <button id="mBtnOpenTarget">&#128193; Open folder</button>
      ${dupesOnly ? '<button id="mBtnIdentifySurvivors">&#128269; Now identify the survivors</button>' : ""}
      <span class="hint" id="mUndoMsg"></span>
    </div>`;
  const targetRoot = (mPlan && mPlan.stats && mPlan.stats.targetRoot)
    || (r.undoCopy ? r.undoCopy.replace(/[\\/][^\\/]+$/, "") : "");
  $("mBtnOpenTarget").addEventListener("click", () => reveal(targetRoot));
  if (dupesOnly) {
    $("mBtnIdentifySurvivors").addEventListener("click", () => {
      $("mDoneWindow").classList.add("hidden");
      mStartIdentify($("mBtnIdentifySurvivors"));
    });
  }
  $("mBtnUndo").addEventListener("click", async () => {
    if (!confirm("Undo the last run?\nMoves are reversed; copies are deleted.")) return;
    $("mBtnUndo").disabled = true;
    try {
      const res = await post("/api/music/undo", { manifest: r.undoFile });
      $("mUndoMsg").textContent =
        `Restored ${res.restored}, deleted ${res.deleted}, skipped ${res.skipped}, errors ${res.errors}.`;
      mSetStatus("Undo complete");
    } catch (e) {
      $("mBtnUndo").disabled = false;
      $("mUndoMsg").textContent = "Undo failed: " + e.message;
    }
  });
}

$("mBtnStartOver").addEventListener("click", () => {
  mPlan = null;
  $("mPlan").classList.add("hidden");
  $("mExecBox").classList.add("hidden");
  $("mDoneWindow").classList.add("hidden");
  $("mBtnStartOver").classList.add("hidden");
  mSetStatus("Ready", "", "");
});

/* ---------------------------------------------------------- API keys + fingerprint badge */
function mRefreshKeyStatus(cfg, savedFlash) {
  const bits = [];
  if (cfg.hasAcoustidKey) bits.push(`AcoustID ${cfg.acoustidKeyMasked}`);
  if (cfg.hasDiscogsToken) bits.push(`Discogs ${cfg.discogsTokenMasked}`);
  if (cfg.hasLastfmKey) bits.push(`Last.fm ${cfg.lastfmKeyMasked}`);
  $("mKeysMsg").textContent = bits.length
    ? `configured: ${bits.join(" · ")}${savedFlash ? " ✓" : ""} — type a new value to override`
    : "no enrichment keys — MusicBrainz needs none";
  const avail = !!cfg.fingerprintAvailable;
  const badge = $("mFpBadge");
  badge.textContent = avail
    ? "fingerprinting available" + (cfg.fpcalcPath ? ` (${cfg.fpcalcPath})` : " (fpcalc found)")
    : "fingerprinting disabled (fpcalc.exe not found)";
  badge.className = "badge " + (avail ? "badge-on" : "badge-off");
  $("mFingerprint").disabled = !avail;
  if (!avail) $("mFingerprint").checked = false;
}

$("mBtnSaveKeys").addEventListener("click", async () => {
  const body = {};
  const a = $("mAcoustidKey").value.trim();
  const d = $("mDiscogsToken").value.trim();
  const l = $("mLastfmKey").value.trim();
  if (a) body.acoustidKey = a;
  if (d) body.discogsToken = d;
  if (l) body.lastfmKey = l;
  if (!a && !d && !l) {
    $("mKeysMsg").textContent = "nothing to save — type a key first, or use Clear";
    return;
  }
  $("mBtnSaveKeys").disabled = true;
  try {
    const cfg = await post("/api/music/config", body);
    $("mAcoustidKey").value = "";
    $("mDiscogsToken").value = "";
    $("mLastfmKey").value = "";
    mRefreshKeyStatus(cfg, true);
  } catch (e) {
    $("mKeysMsg").textContent = "save failed: " + e.message;
  }
  $("mBtnSaveKeys").disabled = false;
});

$("mBtnClearKeys").addEventListener("click", async () => {
  if (!confirm("Remove the stored AcoustID / Discogs / Last.fm keys?")) return;
  try {
    const cfg = await post("/api/music/config",
      { acoustidKey: "", discogsToken: "", lastfmKey: "" });
    $("mAcoustidKey").value = "";
    $("mDiscogsToken").value = "";
    $("mLastfmKey").value = "";
    mRefreshKeyStatus(cfg);
  } catch (e) {
    $("mKeysMsg").textContent = "clear failed: " + e.message;
  }
});

/* ---------------------------------------------------------- window open: sync with server */
/* Config load must RETRY: when this window is mounted as an Aegis tool the
   iframe can come up before the organizer subprocess is accepting requests,
   and a single swallowed failure left the panel showing blank API keys and
   "fingerprinting disabled" forever -- even though both were live server-side. */
async function mLoadConfig(tries = 6, delayMs = 700) {
  for (let i = 0; i < tries; i++) {
    try {
      mRefreshKeyStatus(await api("/api/music/config"));
      return true;
    } catch (e) {
      if (i === tries - 1) return false;
      await new Promise((r) => setTimeout(r, delayMs * (i + 1)));
    }
  }
}

$("winMusic").addEventListener("wm:open", async () => {
  mLoadConfig();   // fire-and-forget; retries until the subprocess answers
  try {
    const s = await api("/api/music/scan/status");
    if (s.state === "running") {
      $("mScanProgressBox").classList.remove("hidden");
      $("mBtnScan").disabled = true;
      $("mBtnScanCancel").disabled = false;
      mSetStatus("Scanning…");
      mStopPoll();
      mPollTimer = setInterval(mPollScan, 500);
      mPollScan();
      return;
    } else if (s.state === "cancelled") {
      $("mScanProgressBox").classList.remove("hidden");
      $("mScanCancelNote").classList.remove("hidden");
      $("mScanCancelText").textContent =
        `Scan cancelled — ${s.processed} of ${s.total} files processed.`;
    }
  } catch (e) { /* server unreachable */ }
  try {
    const x = await api("/api/music/execute/status");
    if (x.state === "running") {
      $("mExecBox").classList.remove("hidden");
      $("mBtnExecCancel").disabled = false;
      mSetStatus("Organizing…");
      mStopPoll();
      mPollTimer = setInterval(mPollExecute, 500);
      mPollExecute();
      return;
    }
  } catch (e) { /* ignore */ }
  try { await mLoadResults(true); } catch (e) { /* no results yet */ }
});
