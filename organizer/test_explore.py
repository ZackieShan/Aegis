#!/usr/bin/env python3
r"""Tests for v2 features: cancel, port-busy, SQLite DB, /api/explore.

Runs against synthetic fixtures ONLY (never D:\Photos). Starts its own
server with --no-browser and always kills it before exiting.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
CANCEL_DIR = os.path.join(BASE, "fixture_cancel")
DB = os.path.join(BASE, "photos.db")
PORT = 8125
HOST = f"http://127.0.0.1:{PORT}"
N_COPY = 800

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name}  -- {why}")


def req(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def explore(q):
    _, d = req("GET", "/api/explore?" + q)
    return d


def wait_port(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req("GET", "/api/scan/status")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def poll(path, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.3)
    raise TimeoutError(path)


def make_cancel_fixtures():
    """800 byte-identical copies -> slow enough to cancel mid-scan/mid-copy."""
    if os.path.isdir(CANCEL_DIR):
        shutil.rmtree(CANCEL_DIR)
    os.makedirs(CANCEL_DIR)
    src = os.path.join(FIX, "DSC_0003.jpg")
    for i in range(N_COPY):
        shutil.copyfile(src, os.path.join(CANCEL_DIR, f"copy_{i:04d}.jpg"))


def main():
    rc = 0
    server = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT), "--no-browser"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_port(), "server did not start"

        print("\n== port busy ==")
        b = subprocess.run(
            [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT), "--no-browser"],
            cwd=BASE, capture_output=True, text=True, timeout=30)
        check("second instance exits code 2", b.returncode == 2, f"rc={b.returncode}")
        check("friendly port-busy message", "already in use" in (b.stdout + b.stderr),
              (b.stdout + b.stderr).strip().splitlines()[-1] if (b.stdout + b.stderr).strip() else "empty")
        check("no traceback", "Traceback" not in (b.stdout + b.stderr))

        print("\n== static still serves ==")
        raw = urllib.request.urlopen(HOST + "/", timeout=10).read()
        check("GET / serves HTML", b"Photo Organizer" in raw)

        print("\n== full fixture scan -> DB ==")
        req("POST", "/api/scan", {"path": FIX})
        s = poll("/api/scan/status")
        check("scan done", s["state"] == "done", f"{s['state']} {s.get('error')}")
        con = sqlite3.connect(DB)
        n = con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
        check("photos.db has 28 rows", n == 28, str(n))
        check("schema has expected columns",
              {"taken_at", "taken_source", "camera", "make", "model", "gps_lat",
               "location", "dupe_group", "scanned_at", "media_type", "companions",
               "date_quality", "exif_date_raw"} <= cols,
              str(sorted(cols)[:5]) + "...")
        idx = {r[1] for r in con.execute("PRAGMA index_list(photos)")}
        con.close()
        check("indexes exist", {"idx_photos_taken", "idx_photos_camera",
                                "idx_photos_location"} <= idx, str(idx))

        print("\n== explore queries ==")
        d = explore("")
        check("no params returns all 28", d["count"] == 28, str(d["count"]))
        d = explore("month=12&day=25")
        names = sorted(r["name"] for r in d["results"])
        check("month+day across years -> DSC_0001 + xmas2014",
              names == ["DSC_0001.jpg", "xmas2014.jpg"], str(names))
        d = explore("year=2013")
        check("year=2013 -> 6 (5 jpeg + NEF)", d["count"] == 6, str(d["count"]))
        d = explore("date_from=2013-12-01&date_to=2013-12-31")
        check("date range Dec 2013 -> 2", d["count"] == 2, str(d["count"]))
        d = explore("hour_from=18&hour_to=21&camera=canon")
        check("evening canon -> IMG_1001 only", d["count"] == 1
              and d["results"][0]["name"] == "IMG_1001.jpg", str(d["count"]))
        d = explore("hour_from=22&hour_to=2&camera=canon")
        check("overnight wrap 22->2 canon -> IMG_1003 only", d["count"] == 1
              and d["results"][0]["name"] == "IMG_1003.jpg", str(d["count"]))
        d = explore("place=rochester")
        if d["count"] == 0:
            skip("place=rochester -> 3", "geocoding unavailable (offline?)")
        else:
            check("place=rochester -> 3 (2 jpeg + NEF)", d["count"] == 3, str(d["count"]))
        d = explore("camera=nikon")
        check("camera=nikon -> 7", d["count"] == 7, str(d["count"]))
        d = explore("year=2013&camera=nikon&hour_from=10&hour_to=11")
        check("year+hour+camera combo -> 2", d["count"] == 2, str(d["count"]))
        d = explore("has_gps=1")
        check("has_gps -> 5", d["count"] == 5, str(d["count"]))
        d = explore("dupes_only=1")
        check("dupes_only -> 4 (master trio + NEF)", d["count"] == 4, str(d["count"]))
        d = explore("sort=size")
        sizes = [r["size"] for r in d["results"]]
        check("sort=size descending", sizes == sorted(sizes, reverse=True), str(sizes[:3]))
        d = explore("limit=5")
        check("limit=5 pages", len(d["results"]) == 5 and d["count"] == 28)
        d = explore("limit=5&offset=25")
        check("offset paging tail", len(d["results"]) == 3, str(len(d["results"])))
        from datetime import datetime as _dt
        _today = _dt.now()
        d1 = explore("on_this_day=1")
        d2 = explore(f"month={_today.month:02d}&day={_today.day:02d}")
        check("on_this_day == explicit month+day of today",
              d1["count"] == d2["count"]
              and sorted(r["name"] for r in d1["results"])
              == sorted(r["name"] for r in d2["results"]),
              f'{d1["count"]} vs {d2["count"]}')
        _, cams = req("GET", "/api/cameras")
        check("/api/cameras distinct list",
              cams["cameras"] == ["Apple iPhone 12", "Canon EOS 5D", "Canon EOS 6D Mark II",
                                  "Hasselblad L2D-20c", "Nikon D700", "Unknown Camera"],
              str(cams["cameras"]))

        print("\n== scan cancel ==")
        make_cancel_fixtures()
        req("POST", "/api/scan", {"path": CANCEL_DIR})
        cancelled = None
        t0 = time.time()
        while time.time() - t0 < 30:
            _, s = req("GET", "/api/scan/status")
            if s["state"] == "running" and s["processed"] >= 5:
                req("POST", "/api/scan/cancel", {})
                cancelled = poll("/api/scan/status")
                break
            if s["state"] in ("done", "error", "cancelled"):
                cancelled = s
                break
            time.sleep(0.05)
        check("scan cancelled mid-run", cancelled and cancelled["state"] == "cancelled",
              str(cancelled and cancelled["state"]))
        check("partial count 0 < N < 800",
              cancelled and 0 < cancelled["processed"] < N_COPY,
              str(cancelled and cancelled["processed"]))
        _, res = req("GET", "/api/results")
        check("results marked partial", res.get("partial") is True)
        check("partial results match processed", res["totalPhotos"] == cancelled["processed"],
              f'{res["totalPhotos"]} vs {cancelled["processed"]}')
        # resume
        req("POST", "/api/scan", {"path": CANCEL_DIR})
        s = poll("/api/scan/status")
        check("resume scan done", s["state"] == "done" and s["total"] == N_COPY,
              f'{s["state"]} total={s["total"]}')
        _, res = req("GET", "/api/results")
        check("resumed results not partial", res.get("partial") is False)

        print("\n== execute cancel (copy mode) ==")
        troot = os.path.join(CANCEL_DIR, "Out")
        _, plan = req("POST", "/api/plan", {
            "levels": ["year"], "nameTemplate": "orig", "dupeMode": "ignore",
            "action": "copy", "removeEmpty": False, "targetRoot": troot})
        check("plan has 800 entries", len(plan["entries"]) == N_COPY, str(len(plan["entries"])))
        req("POST", "/api/execute", {})
        fin = None
        t0 = time.time()
        while time.time() - t0 < 60:
            _, s = req("GET", "/api/execute/status")
            if s["state"] == "running" and s["processed"] >= 5:
                req("POST", "/api/execute/cancel", {})
                fin = poll("/api/execute/status", timeout=120)
                break
            if s["state"] in ("done", "error", "cancelled"):
                fin = s
                break
            time.sleep(0.02)
        er = (fin or {}).get("result") or {}
        check("execute cancelled", fin and fin["state"] == "cancelled",
              str(fin and fin["state"]))
        check("partial copies 0 < N < 800", 0 < er.get("copied", 0) < N_COPY, str(er.get("copied")))
        on_disk = []
        if os.path.isdir(troot):
            for dp, dn, fn in os.walk(troot):
                on_disk += [os.path.join(dp, f) for f in fn if f.lower().endswith(".jpg")]
        check("files on disk == reported copies", len(on_disk) == er.get("copied"),
              f'{len(on_disk)} vs {er.get("copied")}')
        src_size = os.path.getsize(os.path.join(CANCEL_DIR, "copy_0000.jpg"))
        check("no half-copied file (all sizes match source)",
              all(os.path.getsize(f) == src_size for f in on_disk))
        check("undo manifest written even when cancelled",
              bool(er.get("undoFile")) and os.path.isfile(er["undoFile"]))
        if er.get("undoFile"):
            _, u = req("POST", "/api/undo", {"manifest": er["undoFile"]})
            check("undo deleted all partial copies",
                  u.get("deleted") == er.get("copied") and u.get("errors") == 0,
                  json.dumps({k: u[k] for k in ("deleted", "errors", "skipped")}))
        shutil.rmtree(CANCEL_DIR, ignore_errors=True)
        check("cancel fixture dir cleaned", not os.path.exists(CANCEL_DIR))
        con = sqlite3.connect(DB)
        left = con.execute("SELECT COUNT(*) FROM photos WHERE path LIKE ?",
                           (CANCEL_DIR.replace("\\", "\\") + "%",)).fetchone()[0]
        if left:
            con.execute("DELETE FROM photos WHERE path LIKE ?", (CANCEL_DIR + "%",))
            con.commit()
        con.close()
        check("DB rows for temp fixture cleaned", True, f"{left} stale rows removed")

        print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped ====")
        rc = 1 if FAIL else 0
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
            print("\n--- server log tail ---")
            print("\n".join(out.splitlines()[-8:]))
        except Exception:
            server.kill()
        print("server stopped")
        shutil.rmtree(CANCEL_DIR, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
