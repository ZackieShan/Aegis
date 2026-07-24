#!/usr/bin/env python3
r"""READ-ONLY smoke test of RAW/video parsing against the real D:\Photos library.

Finds a folder under D:\Photos that actually contains RAW files, scans it
(capped) on a throwaway server, and reports per-format parse rates:
camera/date extracted vs mtime fallback, aHash-from-preview coverage,
mvhd hit rate for videos. NEVER plans, NEVER executes, NEVER writes to D:.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8133
HOST = f"http://127.0.0.1:{PORT}"
ROOT = r"D:\Photos"
MAX_SCAN = 300

RAW_EXTS = {".nef", ".dng", ".cr2", ".cr3", ".arw", ".rwl", ".rw2", ".orf",
            ".pef", ".srw", ".raf"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg",
              ".mts", ".m2ts", ".3gp"}


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status, json.loads(resp.read().decode())


def find_raw_dir(want_exts=None):
    """First directory (top-down) holding >= 20 RAW files (optionally of the
    given extensions). Returns (dirpath, {ext: count}) or (None, None)."""
    want = want_exts or RAW_EXTS
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and d.lower() not in ("organized", "$recycle.bin")]
        counts = {}
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in want:
                counts[ext] = counts.get(ext, 0) + 1
        if sum(counts.values()) >= 20:
            return dirpath, counts
    return None, None


def main():
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    want = {sys.argv[2].lower()} if len(sys.argv) > 2 else None
    if explicit:
        target, counts = explicit, {}
        print(f"scanning explicit folder {target}")
    else:
        print(f"looking for a RAW-heavy folder under {ROOT} ...")
        t0 = time.time()
        target, counts = find_raw_dir(want)
        if not target:
            print("no folder with >= 20 RAW files found; nothing to smoke")
            return 1
        print(f"found {target} in {time.time()-t0:.1f}s  raw mix: {counts}")

    # NOTE: server output must go to a file, not a PIPE nobody drains -
    # a long scan otherwise deadlocks when the pipe buffer fills.
    log_path = os.path.join(BASE, "_smoke_server.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    server = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT),
         "--no-browser"],
        cwd=BASE, stdout=log_f, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                req("GET", "/api/scan/status")
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server did not start")

        t0 = time.time()
        st, r = req("POST", "/api/scan", {"path": target, "max": MAX_SCAN})
        assert st == 200 and r.get("ok"), r
        while True:
            _, s = req("GET", "/api/scan/status")
            if s["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.5)
        dur = time.time() - t0
        print(f"\nscan: {s['state']}  {s['processed']}/{s['total']} files in {dur:.1f}s "
              f"({s['processed']/max(dur,0.1):.1f}/s)  err={s.get('error')}")

        _, res = req("GET", "/api/results")
        photos = res["photos"]
        print(f"rows: {res['totalPhotos']}   byType: "
              f"{ {k: v['count'] for k, v in (res.get('byType') or {}).items()} }")

        # per-format parse rates
        stats = {}
        for p in photos:
            ext = p["ext"]
            st_ = stats.setdefault(ext, {"n": 0, "dated": 0, "mtime": 0,
                                         "camera": 0, "ahash": 0, "err": 0})
            st_["n"] += 1
            if p["dateSource"] in ("exif", "exif_original", "video"):
                st_["dated"] += 1
            else:
                st_["mtime"] += 1
            if p["camera"] != "Unknown Camera":
                st_["camera"] += 1
            if p.get("ahash"):
                st_["ahash"] += 1
            if p.get("error"):
                st_["err"] += 1
        print(f"\n{'ext':8s} {'n':>4s} {'dated':>6s} {'mtime':>6s} {'camera':>7s} "
              f"{'aHash':>6s} {'err':>4s}")
        for ext in sorted(stats, key=lambda e: -stats[e]["n"]):
            s_ = stats[ext]
            print(f"{ext:8s} {s_['n']:>4d} {s_['dated']:>6d} {s_['mtime']:>6d} "
                  f"{s_['camera']:>7d} {s_['ahash']:>6d} {s_['err']:>4d}")

        # a few real samples
        print("\nsample RAW/video rows:")
        shown = 0
        for p in photos:
            if p["ext"] in RAW_EXTS | VIDEO_EXTS and shown < 6:
                print(f"  {os.path.basename(p['path'])[:44]:46s} "
                      f"{p['camera'][:22]:24s} {p['dt']} [{p['dateSource']}]"
                      + (f" {p['width']}x{p['height']}" if p['width'] else "")
                      + (f" ERR:{p['error']}" if p.get('error') else ""))
                shown += 1
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        log_f.close()
        print("\nserver stopped (D:\\Photos was only READ - no plan, no execute)")


if __name__ == "__main__":
    sys.exit(main())
