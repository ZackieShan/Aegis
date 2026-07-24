#!/usr/bin/env python3
r"""Tests for the Music Organizer UI glue: server routing + static assets.

A STUB music module is injected into sys.modules BEFORE importing server.py,
so the routing glue can be tested without the real music core (built by
another agent in parallel). The server runs in-process on a scratch port
(OS-assigned), never opens a browser, and is always shut down before exit.

Verified here:
  - GET / serves the desktop HTML with the Music Organizer icon/window/script
  - GET /music.js (and the pre-existing static assets) serve 200
  - every /api/music/* GET delegates to music.api_get with the "/api/music/"
    prefix stripped, plus a parse_qs query dict; (status, obj) round-trips
  - every /api/music/* POST delegates to music.api_post the same way
  - unknown /api/music/foo 404s
  - non-200 statuses (409/418/404) pass through verbatim
  - a music module that fails import or init can never take down the server
    (503s, guarded music_safe_init)

No real network, no real music.py, and no production DBs are touched:
server.py only opens photos.db/cinema.db inside main(), which is never run.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------- stub music module
CALLS = []   # every stub invocation, in order


def api_get(path, qs):
    CALLS.append(("api_get", path, qs))
    if path == "scan/status":
        return 200, {"state": "idle", "total": 0, "processed": 0,
                     "currentFile": "", "error": None}
    if path == "execute/status":
        return 200, {"state": "idle", "total": 0, "processed": 0,
                     "currentFile": "", "log": [], "error": None,
                     "result": None}
    if path == "results":
        return 200, {"scannedRoot": r"C:\stub\Music", "partial": False,
                     "totalFiles": 3, "byCodec": {"FLAC": 2, "MP3": 1},
                     "topGenres": [["Rock", 2], ["Jazz", 1]],
                     "genreCoverage": 67, "identified": 2, "unidentified": 1,
                     "singles": 0, "compilations": 1, "dupeGroups": 1,
                     "dupeFiles": 1, "upgradesAvailable": 1,
                     "pendingIdentify": 2, "quickCleanEligible": True,
                     "dupeBytes": 4510, "dupesQuarantined": 0}
    if path == "plan":
        return 404, {"error": "No music plan yet."}
    if path == "config":
        return 200, {"hasAcoustidKey": True, "acoustidKeyMasked": "ac****34",
                     "hasDiscogsToken": False, "discogsTokenMasked": "",
                     "hasLastfmKey": False, "lastfmKeyMasked": "",
                     "fingerprintAvailable": True,
                     "fpcalcPath": r"C:\tools\fpcalc.exe"}
    if path == "teapot":
        # proves arbitrary non-200 statuses round-trip verbatim
        return 418, {"error": "stub teapot"}
    return 404, {"error": f"unknown music GET: {path}"}


def api_post(path, body):
    CALLS.append(("api_post", path, body))
    body = body or {}
    if path == "scan":
        return 200, {"ok": True, "root": body.get("root")}
    if path == "scan/cancel":
        return 409, {"error": "No music scan is running."}
    if path == "plan":
        if body.get("mode") == "dupes_only":
            # first-pass duplicate cleanup: dupe losers only, dest
            # <targetRoot>\_Duplicates\Gxx\<original filename>
            tr = body.get("targetRoot") or r"C:\stub\Music"
            return 200, {"stats": {"mode": "dupes_only",
                                   "action": body.get("action"),
                                   "targetRoot": tr,
                                   "totalFiles": 2, "dupeFiles": 2,
                                   "foldersToCreate": 2},
                         "entries": [
                             {"from": r"C:\stub\Music\a\dup1.mp3",
                              "to": tr + r"\_Duplicates\G01\dup1.mp3",
                              "reason": "dupe", "kind": "track",
                              "groupId": "G01"},
                             {"from": r"C:\stub\Music\b\dup2.mp3",
                              "to": tr + r"\_Duplicates\G02\dup2.mp3",
                              "reason": "dupe", "kind": "track",
                              "groupId": "G02"}],
                         "params": dict(body)}
        return 200, {"stats": {"action": body.get("action"),
                               "targetRoot": body.get("targetRoot"),
                               "totalFiles": 3, "companionFiles": 1,
                               "foldersToCreate": 2, "dupeFiles": 1,
                               "unidentifiedFiles": 1},
                     "entries": [
                         {"from": r"C:\stub\Music\hit.mp3",
                          "to": r"C:\stub\Out\Rock\General\A\2000 - X\01 - hit.mp3",
                          "reason": None, "kind": "album", "companions": []}],
                     "params": dict(body)}
    if path == "identify/resume":
        return 200, {"ok": True, "resumed": 2}
    if path == "execute":
        return 200, {"ok": True}
    if path == "execute/cancel":
        return 409, {"error": "No music execution is running."}
    if path == "undo":
        return 200, {"restored": 2, "deleted": 0, "skipped": 0, "errors": 0}
    if path == "config":
        return 200, {"hasAcoustidKey": bool(body.get("acoustidKey")),
                     "acoustidKeyMasked": "ac****34",
                     "hasDiscogsToken": False, "discogsTokenMasked": "",
                     "hasLastfmKey": False, "lastfmKeyMasked": "",
                     "fingerprintAvailable": False}
    return 404, {"error": f"unknown music POST: {path}"}


stub_music = types.ModuleType("music")
stub_music.db_init = lambda: CALLS.append(("db_init",))
stub_music.restore_state = lambda: CALLS.append(("restore_state",))
stub_music.api_get = api_get
stub_music.api_post = api_post
sys.modules["music"] = stub_music      # injected BEFORE server import

import server                          # noqa: E402

# ---------------------------------------------------------- HTTP helpers
HTTPD = None
BASEURL = None


def req(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(BASEURL + path, data=data, headers=headers,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            payload = resp.read()
            if raw:
                return resp.status, dict(resp.headers), payload
            return resp.status, json.loads(payload.decode())
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, dict(e.headers), payload
        try:
            return e.code, json.loads(payload.decode())
        except Exception:
            return e.code, {}


def port_open(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def get_calls(kind):
    return [c for c in CALLS if c[0] == kind]


# ---------------------------------------------------------- node DOM harness
# Loads the REAL music.js under node with a minimal fake DOM + in-memory
# api/post stubs (no network: fetch throws), drives one scenario, and prints
# "JSON:<checks>" for this file to register. Covers the first-pass duplicate
# filter front-end behaviour that plain HTTP routing tests cannot see.
MUSIC_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const srcPath = process.argv[2];
const scenario = process.argv[3];
const src = fs.readFileSync(srcPath, "utf8");

const out = [];
function check(name, cond, detail) {
  out.push({ name: name, pass: !!cond, detail: detail || "" });
}

/* ---------------- fake DOM ---------------- */
const allEls = [];
const byId = {};

class El {
  constructor(tag) {
    this.tagName = tag || "div";
    this.id = "";
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this._cls = new Set();
    this._text = "";
    this._html = "";
    this._closest = null;
    this._isFrag = false;
    allEls.push(this);
  }
  set className(v) {
    this._cls = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this._cls).join(" "); }
  get classList() {
    const s = this._cls;
    return {
      add: function () { for (const c of arguments) s.add(c); },
      remove: function () { for (const c of arguments) s.delete(c); },
      toggle: function (c, force) {
        const want = force === undefined ? !s.has(c) : !!force;
        if (want) s.add(c); else s.delete(c);
      },
      contains: function (c) { return s.has(c); },
    };
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const kids = this.parentNode.children;
    const i = kids.indexOf(this);
    return i >= 0 && i < kids.length - 1 ? kids[i + 1] : null;
  }
  addEventListener(t, fn) {
    (this.listeners[t] = this.listeners[t] || []).push(fn);
  }
  appendChild(c) {
    if (c && c._isFrag) {
      const kids = c.children.slice();
      c.children = [];
      for (const k of kids) this.appendChild(k);
      return c;
    }
    this.children.push(c);
    if (c) c.parentNode = this;
    return c;
  }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    if (c) c.parentNode = this;
    return c;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return this._closest; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  set innerHTML(v) {
    this._html = String(v);
    const re = /id="([^"]+)"/g;
    let m;
    while ((m = re.exec(this._html))) {
      if (!byId[m[1]]) {
        const e = new El("span");
        e.id = m[1];
        byId[m[1]] = e;
      }
    }
  }
  get innerHTML() { return this._html; }
}

function el(id, tag) {
  const e = new El(tag || "div");
  e.id = id;
  byId[id] = e;
  return e;
}
function getEl(id) {
  if (byId[id]) return byId[id];
  const e = allEls.find((x) => x.id === id);
  if (e) byId[id] = e;
  return e || null;
}

/* pre-create the index.html music-window elements music.js touches */
const fpRow = el("fpRow");
const fpBox = el("fpBox");
fpRow.parentNode = fpBox;
fpBox.children.push(fpRow);
const fp = el("mFingerprint", "input");
fp._closest = fpRow;
fpRow.children.push(fp);
fp.parentNode = fpRow;

const scanBox = el("scanBox");
const progress = el("progress");
progress.parentNode = scanBox;
scanBox.children.push(progress);
const scanBar = el("mScanBar");
scanBar.parentNode = progress;
progress.children.push(scanBar);

const bodyEl = el("body");
const mResultsEl = el("mResults");
mResultsEl.parentNode = bodyEl;
bodyEl.children.push(mResultsEl);

const IDS = [
  "mScanPath", "mScanMax", "mHash", "mTargetRoot", "mBtnScan",
  "mBtnScanCancel", "mBtnScanResume", "mScanProgressBox", "mScanCancelNote",
  "mScanCancelText", "mScanText", "mScanFile", "mScanPct", "mOrganize",
  "mToPlan", "mPlan", "mPlanStats", "mPlanList", "mBtnExecute", "mExecBox",
  "mExecText", "mExecFile", "mExecBar", "mExecPct", "mExecLog",
  "mBtnExecCancel", "mDoneWindow", "mDoneBody", "mBtnStartOver",
  "mStatusState", "mStatusPath", "mStatusCount", "mSumBody", "mCodecBody",
  "mGenreBody", "mDupBody", "mKeysMsg", "mFpBadge", "mAcoustidKey",
  "mDiscogsToken", "mLastfmKey", "mBtnSaveKeys", "mBtnClearKeys", "winMusic",
];
for (const id of IDS) if (!byId[id]) el(id);

function radio(value) { const e = new El("input"); e.value = value; return e; }
const radios = {
  'input[name="mAction"]:checked': radio("move"),
  'input[name="mDupeHandling"]:checked': radio("quarantine"),
  'input[name="mDiscStyle"]:checked': radio("subfolder"),
};

const documentStub = {
  getElementById: getEl,
  createElement: (tag) => new El(tag),
  createDocumentFragment: () => { const f = new El("frag"); f._isFrag = true; return f; },
  querySelector: (sel) => radios[sel] || null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};

/* ---------------- fake app.js globals + stub API ---------------- */
const calls = [];
const routes = { GET: {}, POST: {} };

const sandbox = {
  console: console,
  setInterval: setInterval,
  clearInterval: clearInterval,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  document: documentStub,
  alert: (m) => { calls.push(["alert", String(m)]); },
  confirm: () => true,
  fetch: async () => { throw new Error("network disabled in tests"); },
};
sandbox.window = sandbox;
sandbox.$ = getEl;
sandbox.api = async (path) => {
  calls.push(["GET", path]);
  const r = routes.GET[path];
  const v = typeof r === "function" ? r() : r;
  if (v === undefined) throw new Error("no stub for GET " + path);
  return v;
};
sandbox.post = async (path, req) => {
  calls.push(["POST", path, req]);
  const r = routes.POST[path];
  const v = typeof r === "function" ? r(req) : r;
  if (v === undefined) throw new Error("no stub for POST " + path);
  return v;
};
sandbox.esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
sandbox.fmtBytes = (n) => {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
};
sandbox.setBar = () => {};
sandbox.reveal = async () => {};

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function flush(n) { for (let i = 0; i < (n || 4); i++) await sleep(0); }
async function fire(elm, type) {
  const fns = (elm.listeners[type] || []).slice();
  for (const fn of fns) await fn({ preventDefault: () => {}, target: elm });
}
const postCalls = (p) => calls.filter((c) => c[0] === "POST" && c[1] === p);
const getCalls = (p) => calls.filter((c) => c[0] === "GET" && c[1] === p);
const hidden = (elm) => elm._cls.has("hidden");

/* ---------------- scenario data ---------------- */
const ROOT = "C:\\stub\\Music";
function resultsPayload(patch) {
  return Object.assign({
    scannedRoot: ROOT, partial: false, totalFiles: 974,
    byCodec: { FLAC: 500, MP3: 474 },
    topGenres: [["Rock", 300]], genreCoverage: 80,
    identified: 0, unidentified: 974, singles: 12, compilations: 3,
    dupeGroups: 148, dupeFiles: 326, upgradesAvailable: 20,
    pendingIdentify: 96, note: "local phases only",
    quickCleanEligible: true, dupeBytes: 4509715661, dupesQuarantined: 0,
  }, patch || {});
}
const dupePlan = {
  stats: { mode: "dupes_only", action: "move",
           targetRoot: ROOT + "\\Organized",
           totalFiles: 2, dupeFiles: 2, foldersToCreate: 2 },
  entries: [
    { from: ROOT + "\\a\\dup1.mp3",
      to: ROOT + "\\Organized\\_Duplicates\\G01\\dup1.mp3",
      reason: "dupe", kind: "track", groupId: "G01" },
    { from: ROOT + "\\b\\dup2.mp3",
      to: ROOT + "\\Organized\\_Duplicates\\G02\\dup2.mp3",
      reason: "dupe", kind: "track", groupId: "G02" },
  ],
};

routes.GET["/api/music/config"] = {
  hasAcoustidKey: false, acoustidKeyMasked: "", hasDiscogsToken: false,
  discogsTokenMasked: "", hasLastfmKey: false, lastfmKeyMasked: "",
  fingerprintAvailable: false,
};
routes.GET["/api/music/scan/status"] = {
  state: "idle", total: 0, processed: 0, currentFile: "", error: null,
};
routes.GET["/api/music/execute/status"] = {
  state: "idle", total: 0, processed: 0, currentFile: "", log: [],
  error: null, result: null,
};
routes.POST["/api/music/identify/resume"] = { ok: true, resumed: 96 };
routes.POST["/api/music/plan"] = (req) => {
  if (req && req.mode === "dupes_only") return dupePlan;
  return { stats: {}, entries: [] };
};
routes.POST["/api/music/execute"] = { ok: true };

(async () => {
  vm.runInNewContext(src, sandbox, { filename: "music.js" });
  const winMusic = getEl("winMusic");
  check("music.js loads under fake DOM without throwing", !!winMusic);
  check("quick-clean panel + both buttons injected at load",
        !!getEl("mQuickClean") && !!getEl("mBtnQuickClean")
        && !!getEl("mBtnSkipClean"));
  check("resume-identification box still injected at load",
        !!getEl("mResumeBox"));

  if (scenario === "A") {
    /* happy path: clean duplicates -> plan -> execute -> identify survivors */
    routes.GET["/api/music/results"] = resultsPayload();
    await fire(winMusic, "wm:open");
    await flush();
    check("panel renders when quickCleanEligible && dupeGroups>0",
          !hidden(getEl("mQuickClean")));
    const txt = getEl("mQuickCleanText").innerHTML;
    check("panel shows counts + reclaimable estimate",
          txt.indexOf("326") >= 0 && txt.indexOf("148") >= 0
          && txt.indexOf("4.2 GB") >= 0 && txt.indexOf("reclaimable") >= 0,
          txt);
    check("resume box stays hidden while quick-clean panel is up",
          hidden(getEl("mResumeBox")));

    await fire(getEl("mBtnQuickClean"), "click");
    await flush();
    const pc = postCalls("/api/music/plan");
    check("clean click POSTs /api/music/plan once", pc.length === 1);
    const reqBody = pc.length ? pc[0][2] : {};
    const keys = Object.keys(reqBody).sort();
    check("dupes_only plan POST body is exactly {mode,targetRoot,action}",
          keys.join(",") === "action,mode,targetRoot"
          && reqBody.mode === "dupes_only"
          && reqBody.targetRoot === ROOT + "\\Organized"
          && reqBody.action === "move",
          JSON.stringify(reqBody));
    check("plan preview visible with dupes_only stats line",
          !hidden(getEl("mPlan"))
          && getEl("mPlanStats").innerHTML.indexOf("duplicate") >= 0);
    const rows = getEl("mPlanList").children;
    check("plan preview lists from->to rows with DUPE group tags",
          rows.length === 2 && rows[0].innerHTML.indexOf("DUPE G01") >= 0
          && rows[0].innerHTML.indexOf("_Duplicates") >= 0);
    check("confirm button relabelled for quarantine",
          getEl("mBtnExecute").innerHTML.indexOf("quarantine") >= 0);

    routes.GET["/api/music/execute/status"] = {
      state: "done", total: 2, processed: 2, currentFile: "",
      log: ["moved dup1.mp3"], error: null,
      result: { moved: 2, copied: 0, skipped: 0, errors: 0,
                undoFile: "undo_log_test.json" },
    };
    routes.GET["/api/music/results"] = resultsPayload({
      quickCleanEligible: false, dupesQuarantined: 2,
    });
    await fire(getEl("mBtnExecute"), "click");
    await flush();
    await sleep(800);   // let the 500ms execute poll fire once
    check("execute POSTed through the shared machinery",
          postCalls("/api/music/execute").length === 1);
    check("done panel shows moved count + 'identify survivors' button",
          !hidden(getEl("mDoneWindow"))
          && getEl("mDoneBody").innerHTML.indexOf("Files moved") >= 0
          && !!getEl("mBtnIdentifySurvivors"));
    check("results reloaded after cleanup: quarantined note, panel gone",
          getCalls("/api/music/results").length >= 2
          && getEl("mDupBody").innerHTML
               .indexOf("hidden from identification") >= 0
          && hidden(getEl("mQuickClean")));

    await fire(getEl("mBtnIdentifySurvivors"), "click");
    await flush();
    check("'Now identify the survivors' calls identify/resume",
          postCalls("/api/music/identify/resume").length === 1);
  } else if (scenario === "B") {
    /* skip: straight to identification, no plan/execute */
    routes.GET["/api/music/results"] = resultsPayload();
    await fire(winMusic, "wm:open");
    await flush();
    check("panel renders when quickCleanEligible && dupeGroups>0",
          !hidden(getEl("mQuickClean")));
    await fire(getEl("mBtnSkipClean"), "click");
    await flush();
    check("Skip goes straight to identify/resume (no plan POST)",
          postCalls("/api/music/identify/resume").length === 1
          && postCalls("/api/music/plan").length === 0);
    check("panels swap to the identify progress view",
          hidden(getEl("mQuickClean")) && hidden(getEl("mResults"))
          && !hidden(getEl("mScanProgressBox")));
  } else if (scenario === "C") {
    /* pendingIdentify>0 but no dupes: plain Resume box (current behavior) */
    routes.GET["/api/music/results"] = resultsPayload({
      dupeGroups: 0, dupeFiles: 0, pendingIdentify: 5,
    });
    await fire(winMusic, "wm:open");
    await flush();
    check("no dupes -> quick-clean panel stays hidden",
          hidden(getEl("mQuickClean")));
    check("pendingIdentify>0 -> plain Resume identification box",
          !hidden(getEl("mResumeBox"))
          && getEl("mResumeText").textContent.indexOf("5") >= 0);
    await fire(getEl("mBtnResumeIdentify"), "click");
    await flush();
    check("Resume identification button still works",
          postCalls("/api/music/identify/resume").length === 1);
  } else if (scenario === "D") {
    /* dupes already quarantined: results-line note, no quick-clean panel */
    routes.GET["/api/music/results"] = resultsPayload({
      quickCleanEligible: false, dupesQuarantined: 312,
    });
    await fire(winMusic, "wm:open");
    await flush();
    const dup = getEl("mDupBody").innerHTML;
    check("results-line note when dupes already quarantined",
          dup.indexOf("312") >= 0
          && dup.indexOf("hidden from identification") >= 0,
          dup);
    check("quarantined -> quick-clean panel hidden, resume box shown",
          hidden(getEl("mQuickClean")) && !hidden(getEl("mResumeBox")));
  } else {
    check("known scenario", false, scenario);
  }

  console.log("JSON:" + JSON.stringify(out));
  process.exit(out.every((r) => r.pass) ? 0 : 1);
})().catch((e) => {
  out.push({ name: "harness crashed", pass: false,
             detail: String((e && e.stack) || e) });
  console.log("JSON:" + JSON.stringify(out));
  process.exit(1);
});
"""

NODE_EXE = shutil.which("node")


def run_js_scenario(scenario):
    """Run the node DOM harness for one scenario; register its checks."""
    if not NODE_EXE:
        check(f"js:{scenario}: node available on PATH", False,
              "node.exe not found")
        return
    harness_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", prefix="music_ui_harness_",
                delete=False, encoding="utf-8") as f:
            f.write(MUSIC_JS_HARNESS)
            harness_path = f.name
        p = subprocess.run(
            [NODE_EXE, harness_path, os.path.join(BASE, "music.js"), scenario],
            capture_output=True, text=True, timeout=90)
    except Exception as e:
        check(f"js:{scenario}: harness runs", False, str(e))
        return
    finally:
        if harness_path:
            try:
                os.unlink(harness_path)
            except OSError:
                pass
    jsline = next((l for l in p.stdout.splitlines()
                   if l.startswith("JSON:")), None)
    if not jsline:
        check(f"js:{scenario}: harness produced results", False,
              (p.stdout + p.stderr).strip()[:300])
        return
    try:
        items = json.loads(jsline[5:])
    except Exception as e:
        check(f"js:{scenario}: harness JSON parses", False, str(e))
        return
    for it in items:
        check(f"js:{scenario}: {it['name']}", it["pass"],
              it.get("detail", "")[:300])



# ---------------------------------------------------------- test body
def main():
    global HTTPD, BASEURL
    from http.server import ThreadingHTTPServer
    HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    HTTPD.daemon_threads = True
    port = HTTPD.server_address[1]
    BASEURL = f"http://127.0.0.1:{port}"
    threads_before = set(threading.enumerate())
    serve_t = threading.Thread(target=HTTPD.serve_forever, daemon=True)
    serve_t.start()
    try:
        print(f"in-process server on 127.0.0.1:{port}")

        print("\n== static ==")
        st, hd, body = req("GET", "/", raw=True)
        check("GET / serves HTML", st == 200 and b"Photo Organizer" in body)
        check("/ shows Music Organizer icon + window + start-menu entry",
              b"Music Organizer" in body and b"winMusic" in body
              and b'data-app="winMusic"' in body)
        check("/ loads music.js", b'src="music.js"' in body)
        check("/ default scan root is D:\\Music", b'D:\\Music' in body)
        st, hd, body = req("GET", "/music.js", raw=True)
        check("GET /music.js 200 as javascript",
              st == 200 and "javascript" in hd.get("Content-Type", ""))
        check("music.js talks to /api/music/*",
              b"/api/music/scan" in body and b"/api/music/plan" in body
              and b"/api/music/config" in body and len(body) > 3000,
              f"{len(body)} bytes")
        for asset in ("/app.js", "/wm.js", "/cinema.js", "/style.css",
                      "/98.css"):
            st, hd, body = req("GET", asset, raw=True)
            check(f"GET {asset} still 200", st == 200 and len(body) > 400)
        st, hd, body = req("GET", "/style.css", raw=True)
        check("style.css has music plan-row/badge/glyph rules",
              b"tag-va" in body and b"badge-on" in body
              and b"glyph-music" in body)

        print("\n== GET delegation ==")
        st, obj = req("GET", "/api/music/scan/status")
        check("GET scan/status delegates + 200",
              st == 200 and obj.get("state") == "idle", json.dumps(obj))
        st, obj = req("GET", "/api/music/execute/status")
        check("GET execute/status delegates + 200",
              st == 200 and obj.get("state") == "idle" and "log" in obj)
        st, obj = req("GET", "/api/music/results")
        check("GET results delegates + payload intact",
              st == 200 and obj.get("totalFiles") == 3
              and obj.get("byCodec", {}).get("FLAC") == 2
              and obj.get("genreCoverage") == 67)
        st, obj = req("GET", "/api/music/plan")
        check("GET plan passes through stub 404",
              st == 404 and "plan" in (obj.get("error") or "").lower())
        st, obj = req("GET", "/api/music/config")
        check("GET config delegates (masked keys + fp badge fields)",
              st == 200 and obj.get("hasAcoustidKey") is True
              and obj.get("fingerprintAvailable") is True)
        st, obj = req("GET", "/api/music/teapot")
        check("non-200 status round-trips verbatim (418)",
              st == 418 and obj.get("error") == "stub teapot")
        st, obj = req("GET", "/api/music/foo")
        check("unknown /api/music/foo GET 404s", st == 404)
        req("GET", "/api/music/config?x=1&x=2&y=3")
        qs_calls = [c[2] for c in get_calls("api_get") if c[1] == "config"
                    and c[2]]
        check("query string passed as parse_qs dict",
              qs_calls and qs_calls[-1] == {"x": ["1", "2"], "y": ["3"]},
              str(qs_calls[-1] if qs_calls else None))
        subs = {c[1] for c in get_calls("api_get")}
        check("GET sub-paths stripped of /api/music/ prefix",
              {"scan/status", "execute/status", "results", "plan", "config",
               "teapot", "foo"} <= subs, str(sorted(subs)))

        print("\n== POST delegation ==")
        scan_body = {"root": r"C:\stub\Music", "maxFiles": 5,
                     "hashEnabled": True, "fingerprintEnabled": False}
        st, obj = req("POST", "/api/music/scan", scan_body)
        check("POST scan delegates + body delivered intact",
              st == 200 and obj.get("ok") is True
              and get_calls("api_post")[-1][2] == scan_body)
        st, obj = req("POST", "/api/music/scan/cancel", {})
        check("POST scan/cancel passes through stub 409", st == 409)
        plan_body = {"action": "move", "targetRoot": r"C:\stub\Out",
                     "dupeHandling": "quarantine", "discStyle": "merge"}
        st, obj = req("POST", "/api/music/plan", plan_body)
        check("POST plan delegates (action/dupeHandling/discStyle)",
              st == 200 and obj["stats"]["action"] == "move"
              and obj["params"]["dupeHandling"] == "quarantine"
              and obj["params"]["discStyle"] == "merge")
        st, obj = req("POST", "/api/music/execute", {})
        check("POST execute delegates + 200", st == 200 and obj.get("ok"))
        st, obj = req("POST", "/api/music/execute/cancel", {})
        check("POST execute/cancel passes through stub 409", st == 409)
        st, obj = req("POST", "/api/music/undo", {"manifest": "m.json"})
        check("POST undo delegates + result intact",
              st == 200 and obj.get("restored") == 2)
        st, obj = req("POST", "/api/music/config", {"acoustidKey": "abc123"})
        check("POST config delegates", st == 200
              and obj.get("hasAcoustidKey") is True)
        st, obj = req("POST", "/api/music/whatever", {})
        check("unknown /api/music/whatever POST 404s", st == 404)
        post_subs = {c[1] for c in get_calls("api_post")}
        check("POST sub-paths stripped of /api/music/ prefix",
              {"scan", "scan/cancel", "plan", "execute", "execute/cancel",
               "undo", "config", "whatever"} <= post_subs,
              str(sorted(post_subs)))

        print("\n== music module can never take down the server ==")
        orig = server.music
        broken = types.ModuleType("music")

        def boom():
            raise RuntimeError("boom")

        broken.db_init = boom
        broken.restore_state = boom
        server.music = broken
        try:
            server.music_safe_init()
            check("music_safe_init swallows db_init/restore_state failure",
                  True)
        except Exception as e:
            check("music_safe_init swallows db_init/restore_state failure",
                  False, str(e))
        server.music = None
        st, obj = req("GET", "/api/music/scan/status")
        check("music=None -> GET 503 (not a crash)",
              st == 503 and "unavailable" in (obj.get("error") or "").lower())
        st, obj = req("POST", "/api/music/scan", scan_body)
        check("music=None -> POST 503 (not a crash)", st == 503)
        server.music = orig
        st, obj = req("GET", "/api/music/scan/status")
        check("routes recover once music is restored",
              st == 200 and obj.get("state") == "idle")
        n_before = len([c for c in CALLS if c[0] == "db_init"])
        server.music_safe_init()
        check("music_safe_init calls db_init + restore_state on the stub",
              len([c for c in CALLS if c[0] == "db_init"]) == n_before + 1
              and ("restore_state",) in CALLS)

        print("\n== first-pass duplicate filter: API contract ==")
        st, obj = req("GET", "/api/music/results")
        check("results carries quickCleanEligible/dupeBytes/pendingIdentify",
              st == 200 and obj.get("quickCleanEligible") is True
              and obj.get("dupeBytes") == 4510
              and obj.get("pendingIdentify") == 2
              and obj.get("dupeGroups") == 1 and obj.get("dupeFiles") == 1)
        dupe_body = {"mode": "dupes_only", "targetRoot": r"C:\stub\Music",
                     "action": "move"}
        st, obj = req("POST", "/api/music/plan", dupe_body)
        got = get_calls("api_post")[-1][2]
        check("dupes_only plan POST body is exactly {mode,targetRoot,action}",
              st == 200 and set(got.keys()) == {"mode", "targetRoot", "action"}
              and got == dupe_body, json.dumps(got))
        check("dupes_only plan -> stats.mode + dupe-loser entries to "
              "_Duplicates\\Gxx",
              obj["stats"]["mode"] == "dupes_only"
              and len(obj["entries"]) == 2
              and all(e["reason"] == "dupe" and e.get("groupId")
                      for e in obj["entries"])
              and r"\_Duplicates\G01\dup1.mp3" in obj["entries"][0]["to"],
              json.dumps(obj)[:200])
        st, obj = req("POST", "/api/music/plan",
                      {"action": "move", "targetRoot": r"C:\stub\Out",
                       "dupeHandling": "quarantine", "discStyle": "merge"})
        check("normal organize plan still works alongside dupes_only",
              st == 200 and obj["stats"].get("mode") is None
              and obj["params"]["dupeHandling"] == "quarantine")
        st, obj = req("POST", "/api/music/identify/resume", {})
        check("POST identify/resume delegates + 200",
              st == 200 and obj.get("ok") is True
              and get_calls("api_post")[-1][1] == "identify/resume")

        print("\n== music.js front-end behaviour (node DOM harness) ==")
        p = subprocess.run(
            [NODE_EXE, "--check", os.path.join(BASE, "music.js")],
            capture_output=True, text=True, timeout=60) if NODE_EXE else None
        check("node --check music.js (syntax)",
              p is not None and p.returncode == 0,
              ((p.stderr or "") if p else "node.exe not found").strip()[:200])
        for scenario in ("A", "B", "C", "D"):
            run_js_scenario(scenario)

        print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
        return 1 if FAIL else 0
    finally:
        HTTPD.shutdown()
        HTTPD.server_close()
        serve_t.join(timeout=5)
        time.sleep(0.3)
        alive = [t.name for t in threading.enumerate()
                 if t not in threads_before and t.is_alive()]
        if port_open(port):
            check("server socket released after shutdown", False)
        else:
            check("server socket released after shutdown", True)
        check("no stray threads left alive", not alive, str(alive))
        print("server stopped; stray threads:", alive or "none")


if __name__ == "__main__":
    sys.exit(main())
