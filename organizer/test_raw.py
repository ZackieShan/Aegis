#!/usr/bin/env python3
r"""RAW / video / sidecar support tests.

Runs against synthetic fixtures ONLY (never D:\Photos). Starts its own
server with --no-browser and always kills it before exiting.

Covers: RAW metadata via the stdlib TIFF parser, embedded-preview aHash
(RAW+JPEG twin shares a dupe group), mvhd video dates, graceful fallbacks
for garbage .cr3/.avi, sidecar companions riding through plan/execute/undo,
orphan sidecars -> _Sidecars\, and the Explore type= filter.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
TROOT = os.path.join(FIX, "OrganizedRaw")
PORT = 8131
HOST = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def req(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=hdr, method=method)
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


def poll(path, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.3)
    raise TimeoutError(path)


def find(res, name):
    return next((p for p in res["photos"] if p["name"] == name), None)


def main():
    server = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT),
         "--no-browser"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_port(), "server did not start"

        print("\n== scan fixtures (29 files incl. RAW/video/sidecar) ==")
        st, r = req("POST", "/api/scan", {"path": FIX})
        check("scan accepted", st == 200 and r.get("ok"))
        s = poll("/api/scan/status")
        check("scan done, 29 files walked", s["state"] == "done" and s["total"] == 29,
              f"{s['state']} total={s['total']} err={s.get('error')}")

        _, res = req("GET", "/api/results")
        check("28 media rows (companion xmp not a row)", res["totalPhotos"] == 28,
              str(res["totalPhotos"]))
        bt = res.get("byType") or {}
        print("  byType:", bt)
        check("byType photo=22 raw=3 video=2 sidecar=1",
              bt.get("photo", {}).get("count") == 22
              and bt.get("raw", {}).get("count") == 3
              and bt.get("video", {}).get("count") == 2
              and bt.get("sidecar", {}).get("count") == 1, json.dumps(bt))

        print("\n== RAW metadata (stdlib TIFF parser) ==")
        nef = find(res, "RAW_NIKON.NEF")
        check("NEF row exists, media_type raw",
              nef and nef.get("media_type") == "raw", str(nef and nef.get("media_type")))
        check("NEF camera = Nikon D700", nef and nef["camera"] == "Nikon D700",
              str(nef and nef["camera"]))
        check("NEF date = 2013-06-15 12:00:00 (exif_original)",
              nef and nef["dt"] == "2013-06-15 12:00:00"
              and nef["dateSource"] == "exif_original",
              f'{nef and nef["dt"]} via {nef and nef["dateSource"]}')
        check("NEF GPS ~ Rochester",
              nef and abs((nef["lat"] or 0) - 43.1566) < 0.01
              and abs((nef["lon"] or 0) + 77.6088) < 0.01,
              f'{nef and nef["lat"]},{nef and nef["lon"]}')
        check("NEF dims from embedded preview 320x240",
              nef and nef["width"] == 320 and nef["height"] == 240,
              f'{nef and nef["width"]}x{nef and nef["height"]}')
        check("NEF aHash from embedded preview", nef and bool(nef["ahash"]))
        check("NEF companion list = [RAW_NIKON.xmp]",
              nef and nef.get("companions") == ["RAW_NIKON.xmp"],
              str(nef and nef.get("companions")))

        dng = find(res, "raw_canon.dng")
        check("DNG camera = Hasselblad L2D-20c",
              dng and dng["camera"] == "Hasselblad L2D-20c", str(dng and dng["camera"]))
        check("DNG date = 2019-11-02 09:30:00",
              dng and dng["dt"] == "2019-11-02 09:30:00", str(dng and dng["dt"]))

        cr3 = find(res, "broken.cr3")
        check("broken.cr3 graceful: raw, mtime fallback, no error",
              cr3 and cr3["media_type"] == "raw" and cr3["dateSource"] == "mtime"
              and not cr3.get("error"),
              f'{cr3 and cr3["dateSource"]} err={cr3 and cr3.get("error")}')

        print("\n== video metadata ==")
        mp4 = find(res, "vid_2014.mp4")
        check("MP4 media_type video + mvhd date 2014",
              mp4 and mp4["media_type"] == "video" and mp4["dateSource"] == "video"
              and mp4["dt"].startswith("2014-12-2"),
              f'{mp4 and mp4["dt"]} via {mp4 and mp4["dateSource"]}')
        avi = find(res, "clip.avi")
        check("garbage .avi graceful: video, mtime fallback, no error",
              avi and avi["media_type"] == "video" and avi["dateSource"] == "mtime"
              and not avi.get("error"), str(avi and avi["dateSource"]))

        print("\n== sidecars ==")
        check("RAW_NIKON.xmp NOT indexed (companion)", find(res, "RAW_NIKON.xmp") is None)
        orph = find(res, "orphan.xmp")
        check("orphan.xmp indexed as sidecar",
              orph and orph["media_type"] == "sidecar", str(orph and orph.get("media_type")))

        print("\n== RAW+JPEG twin shares dupe group ==")
        groups = res["groups"]
        master_gid = next((g for g, m in groups.items()
                           if any(x.endswith("\\master.jpg") for x in m)), None)
        check("master.jpg in a group", master_gid is not None, str(list(groups)))
        members = [os.path.basename(x) for x in groups.get(master_gid, [])]
        check("NEF grouped with master trio",
              sorted(members) == ["RAW_NIKON.NEF", "master.jpg",
                                  "master_exact_copy.jpg", "master_resized.jpg"],
              str(members))

        print("\n== RAW thumbnail via embedded preview ==")
        st, hd, body = req("GET", "/api/thumb?path=" + urllib.parse.quote(nef["path"]),
                           raw=True)
        check("NEF thumb is JPEG", st == 200 and body[:2] == b"\xff\xd8",
              f"{len(body)} bytes")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(body)) as im:
            check("NEF thumb is real preview (not gray pixel)", im.size[0] > 8,
                  f"{im.size}")
        st, hd, body = req("GET", "/api/thumb?path=" + urllib.parse.quote(cr3["path"]),
                           raw=True)
        check("broken.cr3 thumb -> gray placeholder", st == 200 and body[:2] == b"\xff\xd8")

        print("\n== plan: companions + orphan sidecar ==")
        st, plan = req("POST", "/api/plan", {
            "levels": ["camera", "year"], "nameTemplate": "orig",
            "dupeMode": "best", "action": "move", "removeEmpty": True,
            "targetRoot": TROOT})
        check("plan built", st == 200, str(plan.get("error")))
        entries = plan["entries"]
        check("plan covers all 28 rows", len(entries) == 28, str(len(entries)))
        nef_e = next((e for e in entries if e["from"].endswith("RAW_NIKON.NEF")), None)
        check("NEF entry carries 1 companion",
              nef_e and len(nef_e.get("companions") or []) == 1,
              str(nef_e and nef_e.get("companions")))
        if nef_e and nef_e.get("companions"):
            comp = nef_e["companions"][0]
            check("companion to = parent dir + parent stem + .xmp",
                  comp["from"].endswith("RAW_NIKON.xmp")
                  and os.path.dirname(comp["to"]) == os.path.dirname(nef_e["to"])
                  and os.path.basename(comp["to"]) ==
                  os.path.splitext(os.path.basename(nef_e["to"]))[0] + ".xmp",
                  comp["to"])
        orph_e = next((e for e in entries if e["from"].endswith("orphan.xmp")), None)
        check("orphan.xmp -> <root>\\_Sidecars\\orphan.xmp",
              orph_e and orph_e["to"] == os.path.join(TROOT, "_Sidecars", "orphan.xmp"),
              str(orph_e and orph_e["to"]))
        check("stats count companion files",
              plan["stats"].get("companionFiles") == 1, str(plan["stats"].get("companionFiles")))
        check("plan totalFiles = 28 rows (companions not separate entries)",
              plan["stats"]["totalFiles"] == 28, str(plan["stats"]["totalFiles"]))

        print("\n== execute (move) ==")
        st, r = req("POST", "/api/execute", {})
        check("execute accepted", st == 200 and r.get("ok"))
        s = poll("/api/execute/status")
        er = s.get("result") or {}
        check("28 moved, 0 errors", s["state"] == "done" and er.get("moved") == 28
              and er.get("errors") == 0, f"{s['state']} {er}")
        check("companion moved beside parent",
              os.path.isfile(os.path.join(os.path.dirname(nef_e["to"]), "RAW_NIKON.xmp"))
              and not os.path.isfile(os.path.join(FIX, "RAW_NIKON.xmp")))
        check("orphan sidecar in _Sidecars",
              os.path.isfile(os.path.join(TROOT, "_Sidecars", "orphan.xmp"))
              and not os.path.isfile(os.path.join(FIX, "orphan.xmp")))
        check("NEF destination exists", os.path.isfile(nef_e["to"]))

        print("\n== undo ==")
        st, u = req("POST", "/api/undo", {"manifest": er.get("undoFile")})
        check("undo ok, 29 restored (28 + companion), 0 errors",
              st == 200 and u.get("restored") == 29 and u.get("errors") == 0,
              json.dumps({k: u.get(k) for k in ("restored", "errors", "skipped")}))
        check("companion restored to fixture dir",
              os.path.isfile(os.path.join(FIX, "RAW_NIKON.xmp")))
        check("orphan restored to fixture dir",
              os.path.isfile(os.path.join(FIX, "orphan.xmp")))
        check("target tree removed", not os.path.exists(TROOT))

        print("\n== explore type= filter ==")
        _, d = req("GET", "/api/explore?type=raw")
        names = sorted(r["name"] for r in d["results"])
        check("type=raw -> 3 (NEF, DNG, broken.cr3)",
              d["count"] == 3 and names == ["RAW_NIKON.NEF", "broken.cr3", "raw_canon.dng"],
              f'{d["count"]} {names}')
        _, d = req("GET", "/api/explore?type=video")
        check("type=video -> 2", d["count"] == 2, str(d["count"]))
        _, d = req("GET", "/api/explore?type=sidecar")
        check("type=sidecar -> 1 (orphan.xmp)",
              d["count"] == 1 and d["results"][0]["name"] == "orphan.xmp", str(d["count"]))
        _, d = req("GET", "/api/explore?type=photo")
        check("type=photo -> 22", d["count"] == 22, str(d["count"]))
        _, d = req("GET", "/api/explore?type=raw&has_gps=1")
        check("type=raw + has_gps -> NEF only",
              d["count"] == 1 and d["results"][0]["name"] == "RAW_NIKON.NEF",
              str(d["count"]))
        _, d = req("GET", "/api/explore?type=video&year=2014")
        check("type=video + year=2014 -> vid_2014.mp4",
              d["count"] == 1 and d["results"][0]["name"] == "vid_2014.mp4",
              str(d["count"]))
        _, d = req("GET", "/api/explore?type=bogus")
        check("bogus type ignored -> all 28", d["count"] == 28, str(d["count"]))
        _, d = req("GET", "/api/explore?type=raw")
        check("explore rows carry mediaType",
              all(r.get("mediaType") == "raw" for r in d["results"]))

        print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
        return 1 if FAIL else 0
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
            print("\n--- server log tail ---")
            print("\n".join(out.splitlines()[-8:]))
        except Exception:
            server.kill()
        print("server stopped")


if __name__ == "__main__":
    sys.exit(main())
