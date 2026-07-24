#!/usr/bin/env python3
r"""Settings persistence + folder-browse endpoint tests.

Part 1 (in-process, hermetic): real cinema/music modules with CONFIG_PATH
redirected into a temp dir; an in-process HTTP server exercises the true
/api/cinema/config, /api/music/config and /api/browse routes.

  - mask-overwrite regression: POSTing the masked display value
    ("REAL…7890") must NEVER overwrite the stored secret (server-side guard
    in server.py + never-prefill client contract)
  - POST {} (blank Save) leaves secrets unchanged; only an explicit ""
    (Clear button) removes them
  - GET config exposes masks only, never raw secrets
  - /api/browse: drive list, subdir listing (sorted, hidden flagged, files
    excluded), parent computation, ".." normalization, UNC/drive-relative
    rejection, 404 for missing dirs, 403 for unreadable dirs (patched
    scandir) with parent+drives still returned

Part 2 (subprocess): real server.py restarts on scratch ports prove the
config files round-trip real secrets across a restart and that a masked
POST cannot destroy them. The production cinema_config.json /
music_config.json are preserved byte-for-byte and restored afterwards.

No real network calls; no production DB writes beyond the same module
db_init the other suites already perform. Both servers are always stopped.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


import cinema   # noqa: E402
import music    # noqa: E402
import server   # noqa: E402  (installs the save_config secret guard)

TMP = tempfile.mkdtemp(prefix="po_settings_")
CINEMA_CFG = os.path.join(TMP, "cinema_config.json")
MUSIC_CFG = os.path.join(TMP, "music_config.json")
cinema.CONFIG_PATH = CINEMA_CFG
music.CONFIG_PATH = MUSIC_CFG

# ---------------------------------------------------------- HTTP helpers
HTTPD = None
BASEURL = None


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(BASEURL + path, data=data, headers=hdr,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def read_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ===================================================================
# Part 1a: config guard (in-process)
# ===================================================================
def config_tests():
    print("\n== cinema config: mask-overwrite regression ==")
    check("secret guard installed on cinema.save_config",
          getattr(cinema.save_config, "_secret_guarded", False) is True)
    check("secret guard installed on music.save_config",
          getattr(music.save_config, "_secret_guarded", False) is True)

    st, cfg = req("POST", "/api/cinema/config",
                  {"tmdbKey": "REALKEY1234567890",
                   "tmdbToken": "REALTOKEN1234567890"})
    check("save real key+token", st == 200 and cfg.get("hasKey") is True
          and cfg.get("hasApiKey") is True and cfg.get("hasToken") is True,
          f"{st} {cfg}")
    on_disk = read_cfg(CINEMA_CFG)
    check("real values on disk",
          on_disk.get("tmdbKey") == "REALKEY1234567890"
          and on_disk.get("tmdbToken") == "REALTOKEN1234567890", str(on_disk))

    st, cfg = req("GET", "/api/cinema/config")
    key_mask, tok_mask = cfg.get("tmdbKeyMasked"), cfg.get("tmdbTokenMasked")
    check("GET shows masks", key_mask == "REAL…7890"
          and tok_mask == "REAL…7890", str(cfg))
    check("GET never leaks raw secrets",
          "REALKEY1234567890" not in json.dumps(cfg)
          and "REALTOKEN1234567890" not in json.dumps(cfg))

    # THE regression: posting the mask back must not destroy the secret
    st, _ = req("POST", "/api/cinema/config", {"tmdbKey": key_mask})
    check("POST masked key -> real key unchanged on disk",
          read_cfg(CINEMA_CFG).get("tmdbKey") == "REALKEY1234567890")
    st, _ = req("POST", "/api/cinema/config", {"tmdbToken": tok_mask})
    check("POST masked token -> real token unchanged on disk",
          read_cfg(CINEMA_CFG).get("tmdbToken") == "REALTOKEN1234567890")
    st, cfg = req("GET", "/api/cinema/config")
    check("masks still describe the real values",
          cfg.get("tmdbKeyMasked") == "REAL…7890"
          and cfg.get("tmdbTokenMasked") == "REAL…7890", str(cfg))

    st, _ = req("POST", "/api/cinema/config", {"tmdbKey": "ANYT…HING"})
    check("any ellipsis value is treated as a mask, not a secret",
          read_cfg(CINEMA_CFG).get("tmdbKey") == "REALKEY1234567890")

    st, _ = req("POST", "/api/cinema/config", {})
    check("POST {} (blank Save) leaves everything unchanged",
          read_cfg(CINEMA_CFG).get("tmdbKey") == "REALKEY1234567890"
          and read_cfg(CINEMA_CFG).get("tmdbToken") == "REALTOKEN1234567890")

    st, cfg = req("POST", "/api/cinema/config", {"tmdbKey": ""})
    on_disk = read_cfg(CINEMA_CFG)
    check("explicit Clear removes only that field",
          on_disk.get("tmdbKey") == ""
          and on_disk.get("tmdbToken") == "REALTOKEN1234567890"
          and cfg.get("hasApiKey") is False and cfg.get("hasToken") is True,
          str(on_disk))

    st, cfg = req("POST", "/api/cinema/config", {"tmdbKey": "NEWKEY999"})
    check("real new value still saves through the guard",
          read_cfg(CINEMA_CFG).get("tmdbKey") == "NEWKEY999"
          and cfg.get("tmdbKeyMasked") == "NEWK…Y999", str(cfg))
    st, _ = req("POST", "/api/cinema/config", {"tmdbKey": "NEWK…Y999"})
    check("short mask echo also blocked", read_cfg(CINEMA_CFG).get("tmdbKey") == "NEWKEY999")

    st, _ = req("POST", "/api/cinema/config", {"tmdbKey": "Ab3d"})  # <= 8 chars
    check("short secret saves", read_cfg(CINEMA_CFG).get("tmdbKey") == "Ab3d")
    st, _ = req("POST", "/api/cinema/config", {"tmdbKey": "Ab…"})
    check("short-secret mask ('Ab…') blocked",
          read_cfg(CINEMA_CFG).get("tmdbKey") == "Ab3d")

    print("\n== music config: mask-overwrite regression ==")
    st, cfg = req("POST", "/api/music/config",
                  {"acoustidKey": "TESTKEY0AB",
                   "discogsToken": "DISCOGSTOKEN12345",
                   "lastfmKey": "LASTFMKEY123456"})
    check("save music keys", st == 200 and cfg.get("hasAcoustidKey") is True
          and cfg.get("hasDiscogsToken") is True
          and cfg.get("hasLastfmKey") is True, f"{st} {cfg}")
    on_disk = read_cfg(MUSIC_CFG)
    check("music values on disk",
          on_disk.get("acoustidKey") == "TESTKEY0AB"
          and on_disk.get("discogsToken") == "DISCOGSTOKEN12345"
          and on_disk.get("lastfmKey") == "LASTFMKEY123456", str(on_disk))

    st, cfg = req("GET", "/api/music/config")
    check("music GET shows masks only",
          cfg.get("acoustidKeyMasked") == "TEST…Y0AB"
          and "TESTKEY0AB" not in json.dumps(cfg), str(cfg))
    a_mask = cfg.get("acoustidKeyMasked")

    st, _ = req("POST", "/api/music/config", {"acoustidKey": a_mask})
    check("POST masked acoustid key -> real key unchanged",
          read_cfg(MUSIC_CFG).get("acoustidKey") == "TESTKEY0AB")
    st, _ = req("POST", "/api/music/config", {})
    check("music POST {} leaves everything unchanged",
          read_cfg(MUSIC_CFG).get("lastfmKey") == "LASTFMKEY123456")
    st, cfg = req("POST", "/api/music/config", {"discogsToken": ""})
    on_disk = read_cfg(MUSIC_CFG)
    check("music explicit Clear removes only that token",
          on_disk.get("discogsToken") == ""
          and on_disk.get("acoustidKey") == "TESTKEY0AB"
          and cfg.get("hasDiscogsToken") is False
          and cfg.get("hasAcoustidKey") is True, str(on_disk))


# ===================================================================
# Part 1b: /api/browse (in-process)
# ===================================================================
def browse_tests():
    print("\n== /api/browse ==")
    root = os.path.join(TMP, "browsetree")
    os.makedirs(os.path.join(root, "Alpha", "inner"), exist_ok=True)
    os.makedirs(os.path.join(root, "beta"), exist_ok=True)
    os.makedirs(os.path.join(root, ".hdir"), exist_ok=True)
    with open(os.path.join(root, "afile.txt"), "w") as f:
        f.write("x")

    st, obj = req("GET", "/api/browse?path=")
    sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    check("empty path lists drives", st == 200 and obj.get("path") == ""
          and obj.get("parent") is None and obj.get("dirs") == []
          and any(d.get("path") == sysdrive for d in obj.get("drives", [])),
          f"{st} {obj.get('drives')}")

    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(root))
    names = [d["name"] for d in obj.get("dirs", [])]
    check("lists subdirs sorted, files excluded", st == 200
          and names == [".hdir", "Alpha", "beta"], str(names))
    hid = {d["name"]: d.get("hidden") for d in obj.get("dirs", [])}
    check("hidden dir flagged", hid.get(".hdir") is True
          and hid.get("Alpha") is False, str(hid))
    check("parent computed", obj.get("parent") == os.path.dirname(root),
          str(obj.get("parent")))
    check("full child paths returned",
          any(d["path"] == os.path.join(root, "Alpha")
              for d in obj.get("dirs", [])))
    check("listing payload also carries drives",
          any(d.get("path") == sysdrive for d in obj.get("drives", [])))

    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(os.path.join(root, "beta")))
    check("leaf dir: empty dirs, parent is root",
          st == 200 and obj.get("dirs") == [] and obj.get("parent") == root,
          f"{st} {obj}")

    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(sysdrive))
    check("drive root allowed, parent is '' (drive list)",
          st == 200 and obj.get("parent") == "", f"{st} {obj.get('parent')}")

    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(root + "\\Alpha\\.."))
    check("'..' segments normalize", st == 200 and obj.get("path") == root,
          f"{st} {obj.get('path')}")

    st, obj = req("GET", "/api/browse?path=..")
    check("bare '..' rejected", st == 400 and "error" in obj, f"{st} {obj}")
    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote("\\\\server\\share"))
    check("UNC path rejected", st == 400, f"{st}")
    st, obj = req("GET", "/api/browse?path=C:relative")
    check("drive-relative path rejected", st == 400, f"{st}")
    st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(root + "\\nope"))
    check("missing dir -> 404", st == 404 and "error" in obj, f"{st}")

    # permission-denied dir: patch scandir to refuse just this root
    real_scandir = os.scandir
    norm = os.path.normcase(os.path.abspath(root))

    def fake_scandir(p):
        if os.path.normcase(os.path.abspath(p)) == norm:
            raise PermissionError(13, "Access is denied", p)
        return real_scandir(p)

    os.scandir = fake_scandir
    try:
        st, obj = req("GET", "/api/browse?path=" + urllib.parse.quote(root))
    finally:
        os.scandir = real_scandir
    check("unreadable dir -> 403 with parent+drives for navigation",
          st == 403 and "error" in obj
          and obj.get("parent") == os.path.dirname(root)
          and any(d.get("path") == sysdrive for d in obj.get("drives", [])),
          f"{st} {obj}")


# ===================================================================
# Part 2: restart round-trip (subprocess, real config files)
# ===================================================================
REAL_CINEMA_CFG = os.path.join(BASE, "cinema_config.json")
REAL_MUSIC_CFG = os.path.join(BASE, "music_config.json")
PORT_A, PORT_B = 8141, 8142


def start_server(port):
    p = subprocess.Popen([sys.executable, os.path.join(BASE, "server.py"),
                          "--port", str(port), "--no-browser"],
                         cwd=BASE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    host = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(host + "/api/cinema/config",
                                        timeout=5) as resp:
                json.loads(resp.read().decode())
            return p, host
        except Exception:
            time.sleep(0.3)
    p.terminate()
    raise RuntimeError(f"server did not start on {port}")


def stop_server(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()


def sreq(method, host, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(host + path, data=data, headers=hdr,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def restart_tests():
    print("\n== restart round-trip (real config files, restored after) ==")
    saved = {}
    for path in (REAL_CINEMA_CFG, REAL_MUSIC_CFG):
        saved[path] = (open(path, "rb").read()
                       if os.path.isfile(path) else None)
    srv = None
    try:
        srv, host = start_server(PORT_A)
        st, cfg = sreq("POST", host, "/api/cinema/config",
                       {"tmdbKey": "RESTARTKEY12345",
                        "tmdbToken": "RESTARTTOKEN12345"})
        check("server A: test creds saved", st == 200
              and cfg.get("tmdbKeyMasked") == "REST…2345", f"{st} {cfg}")
        st, cfg = sreq("POST", host, "/api/music/config",
                       {"acoustidKey": "RESTARTMUSIC123"})
        check("server A: music test key saved", st == 200
              and cfg.get("acoustidKeyMasked") == "REST…C123", f"{st} {cfg}")
        stop_server(srv)
        srv = None

        srv, host = start_server(PORT_B)
        st, cfg = sreq("GET", host, "/api/cinema/config")
        check("server B (fresh process): cinema config loaded from disk, masked",
              st == 200 and cfg.get("tmdbKeyMasked") == "REST…2345"
              and cfg.get("tmdbTokenMasked") == "REST…2345"
              and "RESTARTKEY12345" not in json.dumps(cfg), f"{st} {cfg}")
        st, cfg = sreq("GET", host, "/api/music/config")
        check("server B: music config loaded from disk, masked",
              st == 200 and cfg.get("acoustidKeyMasked") == "REST…C123",
              f"{st} {cfg}")

        before_c = open(REAL_CINEMA_CFG, "rb").read()
        before_m = open(REAL_MUSIC_CFG, "rb").read()
        sreq("POST", host, "/api/cinema/config",
             {"tmdbKey": "REST…2345", "tmdbToken": "REST…2345"})
        sreq("POST", host, "/api/music/config", {"acoustidKey": "REST…C123"})
        check("masked POSTs after restart leave real files byte-identical",
              open(REAL_CINEMA_CFG, "rb").read() == before_c
              and open(REAL_MUSIC_CFG, "rb").read() == before_m)
        stop_server(srv)
        srv = None
    finally:
        if srv is not None:
            stop_server(srv)
        for path, data in saved.items():
            if data is None:
                if os.path.isfile(path):
                    os.remove(path)
            else:
                with open(path, "wb") as f:
                    f.write(data)
    check("production configs restored byte-for-byte",
          all((open(p, "rb").read() == d) if d is not None
              else not os.path.isfile(p) for p, d in saved.items()))


def main():
    global HTTPD, BASEURL
    from http.server import ThreadingHTTPServer
    HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    HTTPD.daemon_threads = True
    BASEURL = f"http://127.0.0.1:{HTTPD.server_address[1]}"
    serve_t = threading.Thread(target=HTTPD.serve_forever, daemon=True)
    serve_t.start()
    try:
        print(f"in-process server on {BASEURL}")
        config_tests()
        browse_tests()
    finally:
        HTTPD.shutdown()
        HTTPD.server_close()
        serve_t.join(timeout=5)
    restart_tests()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
