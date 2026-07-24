#!/usr/bin/env python3
r"""Date provenance ladder + plausibility tests.

Unit-checks date_quality directly, then scans the synthetic fixtures through
a real server (port 8134) and asserts the ladder outcomes for the five
date-focused fixtures:

    IMG_20230629_220120.jpg  zeroed mtime, filename date   -> filename
    canon6d_2000.jpg         EXIF 2000 on a 2017 camera     -> exif_suspect
    nodate.jpg               zeroed mtime, no date anywhere -> unknown (NULL)
    IMG-20191102-WA0007.jpg  WhatsApp filename date         -> filename
    fake_container.heic      1980 mtime, HEIC meta Exif     -> container

Also covers the Results byQuality panel + warning, the _Unknown Date plan
routing, the Explore quality= filter, and repair_dates.py dry-run /
idempotency on a scratch database. Fixtures only; never D:\Photos.
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
DB = os.path.join(BASE, "photos.db")
PORT = 8134
HOST = f"http://127.0.0.1:{PORT}"
TROOT = os.path.join(FIX, "OrganizedDates")

sys.path.insert(0, BASE)
import date_quality as dq  # noqa: E402
import repair_dates  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
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


def poll(path, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.3)
    raise TimeoutError(path)


def db_row(name):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT taken_at, taken_source, date_quality, exif_date_raw, camera"
        " FROM photos WHERE filename = ?", (name,)).fetchone()
    con.close()
    return dict(r) if r else None


def unit_checks():
    print("\n== unit: date_quality rules ==")
    ok_lo, _ = dq.check_datetime(datetime(1989, 12, 31, 23, 59))
    ok_hi, _ = dq.check_datetime(datetime(1990, 1, 2))
    check("hard lower bound 1990", not ok_lo and ok_hi, f"{ok_lo}/{ok_hi}")
    ok_fu, why_fu = dq.check_datetime(datetime.now() + timedelta(days=30))
    check("future rejected", not ok_fu and why_fu == "future", why_fu)
    ok_f, _ = dq.check_datetime(datetime(2000, 1, 1))
    ok_f2, _ = dq.check_datetime(datetime(2000, 1, 2))
    check("factory-default day rejected, next day ok", not ok_f and ok_f2)
    ry = dq.release_year("Canon", "EOS 6D Mark II")
    ok_pre, _ = dq.check_datetime(datetime(2010, 5, 1), "Canon", "EOS 6D Mark II")
    ok_post, _ = dq.check_datetime(datetime(2020, 5, 1), "Canon", "EOS 6D Mark II")
    check("camera release-year table (6D Mk II 2017, LG VX 2006)",
          ry == 2017 and not ok_pre and ok_post
          and dq.release_year("LG VX9200") == 2006,
          f"ry={ry} pre={ok_pre} post={ok_post}")
    check("mtime rules: zeroed / overflow / 1980-exact rejected, 2021 ok",
          dq.check_mtime(0)[0] is False
          and dq.check_mtime(2 ** 31)[0] is False
          and dq.check_mtime(315550800.0)[0] is False
          and dq.check_mtime(None)[0] is False
          and dq.check_mtime(1620230400.0)[0] is True)
    check("filename patterns (IMG_ / WhatsApp / Screenshot / ISO)",
          dq.parse_filename_date("IMG_20230629_220120.jpg") == datetime(2023, 6, 29, 22, 1, 20)
          and dq.parse_filename_date("IMG-20191102-WA0007.jpg") == datetime(2019, 11, 2)
          and dq.parse_filename_date("Screenshot_20230629-220120.png") == datetime(2023, 6, 29, 22, 1, 20)
          and dq.parse_filename_date("2023-06-29_22-01-20.jpg") == datetime(2023, 6, 29, 22, 1, 20))
    check("filename invalid month rejected; plain names have no date",
          dq.parse_filename_date("IMG_20231329_220120.jpg") is None
          and dq.parse_filename_date("nodate.jpg") is None
          and dq.parse_filename_date("canon6d_2000.jpg") is None)


def server_checks():
    server = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT),
         "--no-browser"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_port(), "server did not start"

        print("\n== scan fixtures through the ladder ==")
        req("POST", "/api/scan", {"path": FIX})
        s = poll("/api/scan/status")
        check("scan done, 29 files walked", s["state"] == "done" and s["total"] == 29,
              f"{s['state']} total={s['total']} err={s.get('error')}")

        r = db_row("IMG_20230629_220120.jpg")
        check("zeroed-mtime file rescued by filename date",
              r and r["taken_at"] == "2023-06-29 22:01:20"
              and r["taken_source"] == "filename" and r["date_quality"] == "filename",
              json.dumps(r))
        r = db_row("canon6d_2000.jpg")
        check("EXIF 2000 on 2017 camera -> exif_suspect, mtime 2021 kept, raw preserved",
              r and r["taken_at"] == "2021-05-05 12:00:00"
              and r["taken_source"] == "mtime" and r["date_quality"] == "exif_suspect"
              and r["exif_date_raw"] == "2000:01:01 00:00:00"
              and r["camera"] == "Canon EOS 6D Mark II", json.dumps(r))
        r = db_row("nodate.jpg")
        check("no date anywhere -> taken_at NULL, quality unknown",
              r and r["taken_at"] is None and r["taken_source"] == "unknown"
              and r["date_quality"] == "unknown", json.dumps(r))
        r = db_row("IMG-20191102-WA0007.jpg")
        check("WhatsApp filename date",
              r and r["taken_at"] == "2019-11-02 00:00:00"
              and r["taken_source"] == "filename" and r["date_quality"] == "filename",
              json.dumps(r))
        r = db_row("fake_container.heic")
        check("1980-mtime HEIC rescued by container meta Exif",
              r and r["taken_at"] == "2021-08-15 10:00:00"
              and r["taken_source"] == "container" and r["date_quality"] == "container",
              json.dumps(r))

        print("\n== results byQuality panel ==")
        _, res = req("GET", "/api/results")
        bq = res.get("byQuality") or {}
        print("  byQuality:", bq)
        check("byQuality exact counts",
              bq == {"exif": 15, "mtime": 7, "container": 2, "filename": 2,
                     "exif_suspect": 1, "unknown": 1}, json.dumps(bq))
        check("dateQualityWarning = 2 (exif_suspect + unknown)",
              res.get("dateQualityWarning") == 2, str(res.get("dateQualityWarning")))

        print("\n== plan: _Unknown Date routing ==")
        st, plan = req("POST", "/api/plan", {
            "levels": ["camera", "year"], "nameTemplate": "orig",
            "dupeMode": "best", "action": "move", "removeEmpty": True,
            "targetRoot": TROOT})
        entries = plan["entries"]
        nd = next((e for e in entries if e["from"].endswith("nodate.jpg")), None)
        check("nodate.jpg -> <root>\\_Unknown Date\\Unknown Camera\\nodate.jpg",
              nd and nd["to"] == os.path.join(TROOT, "_Unknown Date",
                                              "Unknown Camera", "nodate.jpg"),
              str(nd and nd["to"]))
        check("stats: unknownDateFiles == 1, totalFiles == 28",
              plan["stats"].get("unknownDateFiles") == 1
              and plan["stats"].get("totalFiles") == 28, json.dumps(plan["stats"]))

        print("\n== explore quality= filter ==")
        d = explore("quality=unknown")
        check("quality=unknown -> 1 (nodate.jpg)",
              d["count"] == 1 and d["results"][0]["name"] == "nodate.jpg", str(d["count"]))
        d = explore("quality=filename")
        check("quality=filename -> 2", d["count"] == 2, str(d["count"]))
        d = explore("quality=container")
        names = sorted(r["name"] for r in d["results"])
        check("quality=container -> 2 (vid_2014.mp4 + fake_container.heic)",
              d["count"] == 2 and names == ["fake_container.heic", "vid_2014.mp4"],
              f'{d["count"]} {names}')
        d = explore("quality=suspect")
        check("quality=suspect -> 2 (exif_suspect + unknown)", d["count"] == 2,
              str(d["count"]))
        d1 = explore("quality=exif")
        d2 = explore("quality=mtime")
        d3 = explore("quality=exif_suspect")
        check("quality=exif -> 15, mtime -> 7, exif_suspect -> 1",
              d1["count"] == 15 and d2["count"] == 7 and d3["count"] == 1,
              f'{d1["count"]}/{d2["count"]}/{d3["count"]}')
        check("explore rows carry dateQuality",
              all(r.get("dateQuality") for r in explore("")["results"]))

        print("\n== execute + undo through _Unknown Date ==")
        req("POST", "/api/execute", {})
        s = poll("/api/execute/status")
        er = s.get("result") or {}
        check("execute done, 28 moved",
              s["state"] == "done" and er.get("moved") == 28 and er.get("errors") == 0,
              f"{s['state']} {er}")
        check("nodate.jpg on disk under _Unknown Date",
              os.path.isfile(os.path.join(TROOT, "_Unknown Date",
                                          "Unknown Camera", "nodate.jpg")))
        st, u = req("POST", "/api/undo", {"manifest": er.get("undoFile")})
        check("undo restores everything (28 + companion)",
              st == 200 and u.get("restored") == 29 and u.get("errors") == 0
              and os.path.isfile(os.path.join(FIX, "nodate.jpg"))
              and not os.path.exists(TROOT),
              json.dumps({k: u.get(k) for k in ("restored", "errors")}))
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
            print("\n--- server log tail ---")
            print("\n".join(out.splitlines()[-8:]))
        except Exception:
            server.kill()
        print("server stopped")


def repair_checks():
    print("\n== repair_dates.py on a scratch DB ==")
    tmp = tempfile.mkdtemp(prefix="repair_dates_")
    db = os.path.join(tmp, "scratch.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE photos (path TEXT, filename TEXT, ext TEXT, taken_at TEXT,"
        " taken_source TEXT, camera TEXT, make TEXT, model TEXT, mtime REAL,"
        " exif_date_raw TEXT, date_quality TEXT)")
    rows = [
        # zeroed mtime, rescuable via filename
        (r"D:\nonexistent\IMG_20230629_220120.jpg", "IMG_20230629_220120.jpg",
         ".jpg", "1980-01-01 00:00:00", "mtime", "Unknown Camera", None, None,
         315550800.0, None, None),
        # EXIF year 2000 on a 2017 camera, plausible mtime available
        (r"D:\nonexistent\canon6d_2000.jpg", "canon6d_2000.jpg", ".jpg",
         "2000-01-01 00:00:00", "exif_original", "Canon EOS 6D Mark II",
         "Canon", "EOS 6D Mark II", 1620230400.0, None, None),
        # overflowed mtime, nothing else available
        (r"D:\nonexistent\nodate.jpg", "nodate.jpg", ".jpg",
         "2106-02-06 00:00:00", "mtime", "Unknown Camera", None, None,
         4294967295.0, None, None),
    ]
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = repair_dates.main(["--db", db, "--dry-run"])
    out = buf.getvalue()
    con = sqlite3.connect(db)
    n_null = con.execute("SELECT COUNT(*) FROM photos WHERE taken_at = '1980-01-01 00:00:00'").fetchone()[0]
    con.close()
    check("dry-run reports 3 suspects, writes nothing",
          rc == 0 and "3 suspect rows" in out and n_null == 1, out.splitlines()[-1])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = repair_dates.main(["--db", db, "--no-backup"])
    out = buf.getvalue()
    check("repair run 1 completes, 3 repaired", rc == 0 and "repaired 3 rows" in out,
          out.splitlines()[-1])

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    got = {r["filename"]: dict(r) for r in con.execute(
        "SELECT filename, taken_at, taken_source, date_quality, exif_date_raw"
        " FROM photos")}
    con.close()
    a = got["IMG_20230629_220120.jpg"]
    check("zeroed-mtime row -> filename tier (2023-06-29 22:01:20)",
          a["taken_at"] == "2023-06-29 22:01:20" and a["taken_source"] == "filename"
          and a["date_quality"] == "filename", json.dumps(a))
    b = got["canon6d_2000.jpg"]
    check("EXIF-2000 row -> exif_suspect via mtime 2021, raw preserved",
          b["taken_at"] == "2021-05-05 12:00:00" and b["taken_source"] == "mtime"
          and b["date_quality"] == "exif_suspect"
          and b["exif_date_raw"] == "2000-01-01 00:00:00", json.dumps(b))
    c = got["nodate.jpg"]
    check("overflow row -> taken_at NULL, quality unknown",
          c["taken_at"] is None and c["taken_source"] == "unknown"
          and c["date_quality"] == "unknown", json.dumps(c))

    before = json.dumps(got, sort_keys=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = repair_dates.main(["--db", db, "--no-backup"])
    out2 = buf.getvalue()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    got2 = {r["filename"]: dict(r) for r in con.execute(
        "SELECT filename, taken_at, taken_source, date_quality, exif_date_raw"
        " FROM photos")}
    con.close()
    check("second run idempotent: 0 repairs, identical state",
          rc == 0 and "0 suspect rows" in out2
          and json.dumps(got2, sort_keys=True) == before,
          [l for l in out2.splitlines() if "suspect" in l][:1])


def main():
    unit_checks()
    server_checks()
    repair_checks()
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("failed:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
