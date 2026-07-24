#!/usr/bin/env python3
"""Read-only smoke scan of D:\\Photos (capped). Starts the server, scans
with a file cap, reports EXIF/dupe/geocode stats, terminates the server.
Never plans, moves, or copies anything."""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8124
HOST = f"http://127.0.0.1:{PORT}"
MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 40


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


server = subprocess.Popen([sys.executable, os.path.join(BASE, "server.py"),
                           "--port", str(PORT)], cwd=BASE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            req("GET", "/api/scan/status")
            break
        except Exception:
            time.sleep(0.3)
    print(f"smoke: scanning D:\\Photos (max {MAX}, READ-ONLY)")
    req("POST", "/api/scan", {"path": "D:\\Photos", "max": MAX})
    t0 = time.time()
    while True:
        s = req("GET", "/api/scan/status")
        print(f"  {s['processed']}/{s['total']} {s['currentFile'][:50]}", end="\r")
        if s["state"] in ("done", "error"):
            break
        if time.time() - t0 > 200:
            print("\n  (giving up waiting)")
            break
        time.sleep(1)
    dur = time.time() - t0
    print(f"\nscan state: {s['state']} in {dur:.1f}s  error={s.get('error')}")
    res = req("GET", "/api/results")
    n = res["totalPhotos"]
    print(f"files seen: {n}  ({n / max(dur, 0.1):.1f} files/s)")
    print(f"total size: {res['totalBytes'] / 1e6:.1f} MB")
    print(f"date range: {res['dateMin']} .. {res['dateMax']}")
    print(f"parse/open errors: {res['errorCount']} of {n}")
    for p in res["photos"]:
        if p["error"]:
            print(f"   error file: {p['name']}: {p['error']}")
    print("cameras:", dict(res["cameras"][:10]))
    print(f"gps: {res['gpsCount']}  noGps: {res['noGpsCount']}")
    locs = {}
    for p in res["photos"]:
        if p["lat"] is not None:
            locs[p["location"]] = locs.get(p["location"], 0) + 1
    print("geocoded:", locs)
    print(f"exactGroups={res['exactGroups']} nearGroups={res['nearGroups']} "
          f"wastedBytes={res['exactWastedBytes']}")
    srcs = Counter = None
    from collections import Counter as C
    print("dateSource:", dict(C(p["dateSource"] for p in res["photos"])))
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()
    print("smoke server stopped, exit:", server.poll())
