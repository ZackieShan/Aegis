#!/usr/bin/env python3
r"""Tests for POST /api/reveal (Open in file location).

Unit part (in-process): reveal_path validation + injectable runner/platform.
HTTP part: a real server on port 8135 with PO_NO_REVEAL=1 so Explorer never
actually opens; verifies scanned-root and plan-target-root registration.

Fixtures only; never D:\Photos.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")
PORT = 8135
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


def unit_tests():
    print("\n== unit: reveal_path validation ==")
    sys.path.insert(0, BASE)
    import server

    tmp = tempfile.mkdtemp(prefix="reveal_unit_")
    server.DB_PATH = os.path.join(tmp, "test.db")
    server.db_init()
    server.db_add_root(FIX, "scan")

    inside_file = os.path.join(FIX, "DSC_0001.jpg")
    missing_inside = os.path.join(FIX, "definitely_not_there.jpg")
    outside = os.path.abspath(os.path.join(FIX, "..", "server.py"))

    code, payload = server.reveal_path("")
    check("empty path -> 400", code == 400 and "error" in payload, str(payload))
    code, payload = server.reveal_path(missing_inside)
    check("nonexistent (even under a root) -> 400", code == 400, str(payload))
    code, payload = server.reveal_path(outside)
    check("existing file outside scanned roots -> 400",
          code == 400 and "not under a scanned root" in payload["error"], str(payload))

    calls = []
    code, payload = server.reveal_path(inside_file, runner=calls.append)
    check("file under scanned root -> 200, runner invoked with abspath",
          code == 200 and payload.get("ok") is True
          and calls == [os.path.abspath(inside_file)], f"{code} {calls}")
    calls.clear()
    code, payload = server.reveal_path(FIX, runner=calls.append)
    check("directory under scanned root -> 200",
          code == 200 and calls == [os.path.abspath(FIX)], f"{code} {calls}")

    code, payload = server.reveal_path(inside_file, runner=calls.append,
                                       platform_ok=False)
    check("non-Windows -> 501", code == 501 and "error" in payload, str(payload))

    def boom(p):
        raise RuntimeError("explorer exploded")
    code, payload = server.reveal_path(inside_file, runner=boom)
    check("runner failure -> 500", code == 500 and "error" in payload, str(payload))

    print("\n== unit: reveal_in_explorer argv + PO_NO_REVEAL ==")
    spawned = []
    real_popen = subprocess.Popen
    server.subprocess.Popen = lambda *a, **k: spawned.append(a[0])
    try:
        server.reveal_in_explorer(inside_file)
        server.reveal_in_explorer(FIX)
    finally:
        server.subprocess.Popen = real_popen
    check("file -> explorer /select,<path>; dir -> explorer <path>",
          spawned[0][:1] == ["explorer.exe"]
          and spawned[0][1].startswith("/select,")
          and inside_file in spawned[0][1]
          and spawned[1] == ["explorer.exe", FIX], str(spawned))

    spawned.clear()
    os.environ["PO_NO_REVEAL"] = "1"
    try:
        server.reveal_in_explorer(inside_file)
    finally:
        del os.environ["PO_NO_REVEAL"]
    check("PO_NO_REVEAL=1 suppresses Explorer", spawned == [], str(spawned))


def http_tests():
    print("\n== http: /api/reveal on a live server (Explorer suppressed) ==")
    env = dict(os.environ, PO_NO_REVEAL="1")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"), "--port", str(PORT),
         "--no-browser"],
        cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_port(), "server did not start"
        req("POST", "/api/scan", {"path": FIX})
        s = poll("/api/scan/status")
        check("fixture scan done", s["state"] == "done", f"{s['state']} {s.get('error')}")

        st, r = req("POST", "/api/reveal", {})
        check("no path -> 400", st == 400, f"{st} {r}")
        st, r = req("POST", "/api/reveal", {"path": r"C:\Windows\explorer.exe"})
        check("outside scanned roots -> 400", st == 400 and "error" in r, f"{st} {r}")
        st, r = req("POST", "/api/reveal",
                    {"path": os.path.join(FIX, "DSC_0001.jpg")})
        check("fixture file under scanned root -> 200 ok",
              st == 200 and r.get("ok") is True, f"{st} {r}")
        st, r = req("POST", "/api/reveal", {"path": FIX})
        check("fixture root dir itself -> 200 ok", st == 200 and r.get("ok") is True,
              f"{st} {r}")
        st, r = req("POST", "/api/reveal",
                    {"path": os.path.join(FIX, "no_such_file.jpg")})
        check("nonexistent under root -> 400", st == 400, f"{st} {r}")

        # plan target roots become revealable too
        troot = os.path.join(FIX, "OrganizedReveal")
        st, plan = req("POST", "/api/plan", {
            "levels": ["year"], "nameTemplate": "orig", "dupeMode": "ignore",
            "action": "copy", "removeEmpty": False, "targetRoot": troot})
        check("plan built for target-root registration", st == 200, f"{st}")
        os.makedirs(troot, exist_ok=True)
        st, r = req("POST", "/api/reveal", {"path": troot})
        check("plan target root -> 200 ok (registered as target)",
              st == 200 and r.get("ok") is True, f"{st} {r}")
        os.rmdir(troot)
    finally:
        srv.terminate()
        try:
            out, _ = srv.communicate(timeout=10)
            print("--- server log tail ---")
            print("\n".join(out.splitlines()[-6:]))
        except Exception:
            srv.kill()
        print("server stopped")


def main():
    unit_tests()
    http_tests()
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("failed:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
