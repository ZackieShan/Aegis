#!/usr/bin/env python3
r"""Restart-recovery + zero-file-scan no-clobber tests.

Flow: scan fixtures on server A -> kill A -> start server B (fresh process)
-> status/results/plan must work from photos.db alone -> execute+undo on
fixtures -> a zero-file scan must NOT wipe the in-memory results.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
EMPTY = os.path.join(BASE, "fixture_empty")
DB = os.path.join(BASE, "photos.db")
PORT = 8128
HOST = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def start_server():
    p = subprocess.Popen([sys.executable, os.path.join(BASE, "server.py"),
                          "--port", str(PORT), "--no-browser"],
                         cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    for _ in range(60):
        try:
            req("GET", "/api/scan/status")
            return p
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not start")


def stop_server(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()


def poll(path, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.3)
    raise TimeoutError(path)


def main():
    srv = start_server()
    try:
        print("== server A: scan fixtures ==")
        req("POST", "/api/scan", {"path": FIX})
        s = poll("/api/scan/status")
        check("scan done", s["state"] == "done" and s["total"] == 29, str(s))
        con = sqlite3.connect(DB)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        con.close()
        check("meta records scan", meta.get("last_scan_root") == FIX
              and meta.get("last_scan_count") == "28"
              and meta.get("last_scan_partial") == "0", str(meta))
        stop_server(srv)

        print("== server B: cold start from DB ==")
        srv = start_server()
        _, s = req("GET", "/api/scan/status")
        check("status shows done from restored state",
              s["state"] == "done" and s["total"] == 28 and s["processed"] == 28, str(s))
        st, res = req("GET", "/api/results")
        check("results 200 after restart", st == 200 and res["totalPhotos"] == 28,
              f"{st} {res.get('totalPhotos')}")
        cams = dict(res["cameras"])
        check("restored cameras correct",
              cams.get("Nikon D700") == 7 and cams.get("Canon EOS 5D") == 5, str(cams))
        check("restored dupe groups (incl. NEF via preview aHash)",
              sorted(res["groups"].keys()) == ["G01"]
              and len(res["groups"]["G01"]) == 4, str(list(res["groups"])))
        check("not marked partial", res.get("partial") is False)
        st, r = req("POST", "/api/execute", {})
        check("execute without plan refused", st == 400, f"{st} {r}")

        print("== zero-file scan must not clobber ==")
        os.makedirs(EMPTY, exist_ok=True)
        req("POST", "/api/scan", {"path": EMPTY})
        s = poll("/api/scan/status")
        check("zero-file scan completes done/0", s["state"] == "done" and s["total"] == 0, str(s))
        st, res = req("GET", "/api/results")
        check("results still 200 with 28 media rows (kept)",
              st == 200 and res["totalPhotos"] == 28, f"{st}")
        check("scannedRoot still fixture root", res.get("scannedRoot") == FIX,
              str(res.get("scannedRoot")))
        shutil.rmtree(EMPTY, ignore_errors=True)

        print("== plan/execute/undo on DB-rebuilt state ==")
        troot = os.path.join(FIX, "Organized")
        st, plan = req("POST", "/api/plan", {
            "levels": ["camera", "year", "location_month"], "nameTemplate": "orig",
            "dupeMode": "best", "action": "move", "removeEmpty": True,
            "targetRoot": troot})
        check("plan works from restored recs", st == 200 and len(plan["entries"]) == 28,
              f"{st} {len(plan.get('entries', []))}")
        loc1 = next(p["location"] for p in res["photos"] if p["name"] == "DSC_0001.jpg")
        d1 = next(e for e in plan["entries"] if e["from"].endswith("DSC_0001.jpg"))
        check("plan dest uses restored fields",
              d1["to"] == os.path.join(troot, "Nikon D700", "2013",
                                       f"{loc1}, December", "DSC_0001.jpg"), d1["to"])
        req("POST", "/api/execute", {})
        s = poll("/api/execute/status")
        er = s.get("result") or {}
        check("execute done on restored state",
              s["state"] == "done" and er.get("moved") == 28 and er.get("errors") == 0,
              f"{s['state']} {er}")
        missing = [e["to"] for e in plan["entries"] if not os.path.isfile(e["to"])]
        check("all destinations on disk", not missing, str(missing[:2]))
        nef_e = next((e for e in plan["entries"] if e["from"].endswith("RAW_NIKON.NEF")), None)
        check("companion xmp moved beside NEF after restart-restore",
              nef_e and os.path.isfile(os.path.join(os.path.dirname(nef_e["to"]),
                                                    "RAW_NIKON.xmp")))
        st, u = req("POST", "/api/undo", {"manifest": er.get("undoFile")})
        check("undo restores everything (28 + companion)",
              st == 200 and u.get("restored") == 29 and u.get("errors") == 0
              and not os.path.exists(troot), json.dumps({k: u[k] for k in ("restored", "errors")}))

        print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
        return 1 if FAIL else 0
    finally:
        stop_server(srv)
        shutil.rmtree(EMPTY, ignore_errors=True)
        print("server stopped")


if __name__ == "__main__":
    sys.exit(main())
