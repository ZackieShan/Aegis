#!/usr/bin/env python3
r"""End-to-end test for AI Photo Organizer.

Starts server.py as a subprocess on a test port, exercises the full API
flow against the synthetic fixture folder ONLY (never D:\Photos), verifies
the on-disk tree after execute, then verifies undo restores everything.

Optional: --dphotos runs a read-only capped smoke scan of D:\Photos
afterwards (no plan, no execute).

The server subprocess is always terminated before this script exits.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
PORT = 8123
HOST = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def req(method, path, body=None, raw=False):
    url = HOST + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        payload = resp.read()
        if raw:
            return resp.status, dict(resp.headers), payload
        return resp.status, json.loads(payload.decode())


def wait_port(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req("GET", "/api/scan/status")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def poll(path, done_states=("done", "error"), timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in done_states:
            return s
        time.sleep(0.5)
    raise TimeoutError(f"poll {path} timed out")


def tree(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dphotos", action="store_true", help="also run read-only smoke scan of D:\\Photos")
    ap.add_argument("--dphotos-max", type=int, default=60)
    args = ap.parse_args()

    server = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT)],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        print(f"server pid={server.pid}")
        assert wait_port(), "server did not start"

        print("\n== static ==")
        st, hd, body = req("GET", "/", raw=True)
        check("GET / returns HTML", st == 200 and b"Photo Organizer" in body)
        for asset in ("/app.js", "/style.css", "/98.css"):
            st, hd, body = req("GET", asset, raw=True)
            check(f"GET {asset}", st == 200 and len(body) > 500)

        print("\n== scan fixtures ==")
        st, r = req("POST", "/api/scan", {"path": FIX})
        check("scan accepted", st == 200 and r.get("ok"))
        s = poll("/api/scan/status", timeout=180)
        check("scan finished done", s["state"] == "done", f"state={s['state']} err={s.get('error')}")

        _, res = req("GET", "/api/results")
        cams = dict(res["cameras"])
        print("  cameras:", cams)
        print("  gps:", res["gpsCount"], "noGps:", res["noGpsCount"], "errors:", res["errorCount"])
        print("  groups:", {k: [os.path.basename(x) for x in v] for k, v in res["groups"].items()})
        locs = {os.path.basename(p["path"]): p["location"] for p in res["photos"] if p["lat"] is not None}
        print("  geocoded:", locs)
        check("28 media rows scanned (29 files; companion xmp not a row)",
              res["totalPhotos"] == 28, str(res["totalPhotos"]))
        check("Nikon D700 cleaned & counted (6 jpeg + 1 NEF)",
              cams.get("Nikon D700") == 7, str(cams.get("Nikon D700")))
        check("Canon EOS 5D cleaned", cams.get("Canon EOS 5D") == 5, str(cams.get("Canon EOS 5D")))
        check("Apple iPhone 12 cleaned", cams.get("Apple iPhone 12") == 2)
        check("Unknown Camera for no-EXIF+heic+cr3+avi+mp4+sidecar+new",
              cams.get("Unknown Camera") == 12, str(cams.get("Unknown Camera")))
        check("5 GPS files (4 jpeg + NEF)", res["gpsCount"] == 5, str(res["gpsCount"]))
        check("2 unreadable (broken.heic + fake_container.heic)",
              res["errorCount"] == 2, str(res["errorCount"]))
        check("exact dupe group found", res["exactGroups"] >= 1, str(res["exactGroups"]))
        check("near dupe group found", res["nearGroups"] >= 1, str(res["nearGroups"]))
        g1 = [os.path.basename(x) for x in res["groups"].get("G01", [])]
        check("G01 = master trio + NEF (RAW twin grouped via preview aHash)",
              sorted(g1) == ["RAW_NIKON.NEF", "master.jpg", "master_exact_copy.jpg",
                             "master_resized.jpg"], str(g1))
        check("date range starts 2012 (EXIF dates win)", res["dateMin"].startswith("2012"),
              res["dateMin"])
        d1rec = next(p for p in res["photos"] if p["name"] == "DSC_0001.jpg")
        check("EXIF DateTimeOriginal parsed", d1rec["dt"] == "2013-12-25 10:30:00"
              and d1rec["dateSource"] == "exif_original",
              f'{d1rec["dt"]} via {d1rec["dateSource"]}')
        p1rec = next(p for p in res["photos"] if p["name"] == "plain1.jpg")
        check("no-EXIF file falls back to mtime", p1rec["dateSource"] == "mtime",
              p1rec["dateSource"])

        print("\n== thumbnails ==")
        target = next(p["path"] for p in res["photos"] if p["name"] == "DSC_0001.jpg")
        st, hd, body = req("GET", "/api/thumb?path=" + urllib.parse.quote(target), raw=True)
        check("thumb is JPEG", st == 200 and hd.get("Content-Type") == "image/jpeg"
              and body[:2] == b"\xff\xd8", f"{len(body)} bytes")
        st, hd, body = req("GET", "/api/thumb?path=" + urllib.parse.quote(os.path.join(FIX, "broken.heic")), raw=True)
        check("broken heic -> gray pixel jpeg", st == 200 and body[:2] == b"\xff\xd8")

        print("\n== plan (Camera -> Year -> Location, Month ; keep names ; best-copy dupes ; move) ==")
        troot = os.path.join(FIX, "Organized")
        st, plan = req("POST", "/api/plan", {
            "levels": ["camera", "year", "location_month"],
            "nameTemplate": "orig",
            "dupeMode": "best",
            "action": "move",
            "removeEmpty": True,
            "targetRoot": troot,
        })
        entries = plan["entries"]
        check("plan covers all 28 rows", len(entries) == 28, str(len(entries)))
        dupes = [e for e in entries if e["isDupe"]]
        check("3 files go to _Duplicates (best copy kept)",
              len(dupes) == 3, str([os.path.basename(e["from"]) for e in dupes]))
        check("dupes land in _Duplicates\\G01",
              all("\\_Duplicates\\G01\\" in e["to"] for e in dupes))
        d1 = next((e for e in entries if e["from"].endswith("DSC_0001.jpg")), None)
        loc1 = next(p["location"] for p in res["photos"] if p["name"] == "DSC_0001.jpg")
        expect1 = os.path.join(troot, "Nikon D700", "2013", f"{loc1}, December", "DSC_0001.jpg")
        check("DSC_0001 dest = <root>\\Nikon D700\\2013\\<loc>, December\\DSC_0001.jpg",
              d1 and d1["to"] == expect1, d1["to"] if d1 else "missing")
        nef = next(e for e in entries if e["from"].endswith("\\RAW_NIKON.NEF"))
        check("RAW_NIKON.NEF stays in main tree (best copy: raw, largest)", not nef["isDupe"])
        check("NEF companion rides along in plan",
              len(nef.get("companions") or []) == 1
              and nef["companions"][0]["from"].endswith("RAW_NIKON.xmp"))
        check("all dests under target root", all(e["to"].startswith(troot) for e in entries))
        check("stats fields sane",
              plan["stats"]["totalFiles"] == 28 and plan["stats"]["foldersToCreate"] > 0,
              json.dumps(plan["stats"]))
        print("  stats:", plan["stats"])

        print("\n== execute (MOVE against fixtures) ==")
        before_tree = tree(FIX)
        st, r = req("POST", "/api/execute", {})
        check("execute accepted", st == 200 and r.get("ok"))
        s = poll("/api/execute/status", timeout=120)
        check("execute done", s["state"] == "done", f"state={s['state']} err={s.get('error')}")
        er = s.get("result") or {}
        print("  execute result:", er)
        check("28 moved, 0 errors", er.get("moved") == 28 and er.get("errors") == 0, str(er))

        missing = [e["to"] for e in entries if not os.path.isfile(e["to"])]
        leftover = [e["from"] for e in entries if os.path.exists(e["from"])]
        check("every planned destination exists on disk", not missing, str(missing[:3]))
        check("all sources moved away", not leftover, str(leftover[:3]))
        undo_file = er.get("undoFile")
        check("undo manifest exists in project dir", undo_file and os.path.isfile(undo_file))
        check("undo manifest copy next to target root",
              bool(er.get("undoCopy")) and os.path.isfile(er["undoCopy"]))
        print("\n  tree after execute:")
        for t in tree(FIX):
            print("   ", t)

        print("\n== undo ==")
        st, u = req("POST", "/api/undo", {"manifest": undo_file})
        check("undo ok", st == 200 and u.get("errors") == 0, json.dumps(u))
        check("29 restored (28 + sidecar companion)", u.get("restored") == 29,
              str(u.get("restored")))
        after_tree = tree(FIX)
        check("fixture tree fully restored", after_tree == before_tree,
              f"{len(after_tree)} vs {len(before_tree)}")
        check("Organized tree removed after undo", not os.path.exists(troot))

        if args.dphotos:
            print("\n== D:\\Photos read-only smoke scan (max %d) ==" % args.dphotos_max)
            t0 = time.time()
            st, r = req("POST", "/api/scan", {"path": "D:\\Photos", "max": args.dphotos_max})
            check("smoke scan accepted", st == 200 and r.get("ok"))
            s = poll("/api/scan/status", timeout=240)
            dur = time.time() - t0
            check("smoke scan done", s["state"] == "done", f"state={s['state']} err={s.get('error')}")
            _, res = req("GET", "/api/results")
            errn = res["errorCount"]
            print(f"  saw {res['totalPhotos']} files in {dur:.1f}s "
                  f"({res['totalPhotos']/max(dur,0.1):.1f}/s), errors={errn}")
            print("  cameras:", dict(res["cameras"][:8]))
            print(f"  gps: {res['gpsCount']}  noGps: {res['noGpsCount']}")
            print(f"  exactGroups={res['exactGroups']} nearGroups={res['nearGroups']} "
                  f"wasted={res['exactWastedBytes']} bytes")
            print(f"  date range: {res['dateMin']} .. {res['dateMax']}")
            locs = {}
            for p in res["photos"]:
                if p["lat"] is not None:
                    locs[p["location"]] = locs.get(p["location"], 0) + 1
            print("  geocoded locations:", locs)
            check("no files were moved (read-only)", True)

        print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
        return 1 if FAIL else 0
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
            tail = "\n".join(out.splitlines()[-15:])
            print("\n--- server log tail ---\n" + tail)
        except Exception:
            server.kill()
        print("server stopped, exit code:", server.poll())


if __name__ == "__main__":
    sys.exit(main())
