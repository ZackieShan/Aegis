#!/usr/bin/env python3
"""Re-identify the real cinema library after the parser/TMDB fixes.

Runs the app's own cinema.run_scan against F:\Movies (READ-ONLY on media
files; no plan/execute/move). Hashing is off for speed; md5 values from the
pre-scan DB are carried forward afterwards for unchanged (path,size) rows.
"""
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cinema  # noqa: E402

ROOT = r"F:\Movies"
BACKUP = os.path.join(BASE, "db_safety_backup", "cinema_prereident.db")


def snapshot():
    src = sqlite3.connect(cinema.CINEMA_DB, timeout=30)
    dst = sqlite3.connect(BACKUP)
    src.backup(dst)
    dst.close()
    # old md5s keyed by (path,size) to carry forward after the rescan
    old = {}
    for p, s, m in src.execute(
            "SELECT path, size_bytes, md5 FROM media WHERE md5 IS NOT NULL"):
        old[(p, s)] = m
    counts = dict(src.execute("SELECT kind, COUNT(*) FROM media"
                              " GROUP BY kind").fetchall())
    src.close()
    return old, counts


def main():
    cinema.db_init()  # adds ident_cache if missing
    old_md5, before = snapshot()
    print("before:", before, "| md5 rows saved:", len(old_md5), flush=True)

    cinema.SCAN_CANCEL.clear()
    t0 = time.time()
    done = {"done": False}

    import threading
    def run():
        cinema.run_scan(ROOT, 0, False)
        done["done"] = True

    th = threading.Thread(target=run, daemon=True)
    th.start()
    while not done["done"]:
        time.sleep(5)
        s = cinema.scan_status()
        print(f"  [{time.time()-t0:6.0f}s] {s['state']} "
              f"{s['processed']}/{s['total']} {s['currentFile'][:70]}",
              flush=True)
    th.join()
    s = cinema.scan_status()
    if s["state"] != "done":
        print("SCAN FAILED:", s["state"], s.get("error"))
        return 1
    print(f"scan done in {time.time()-t0:.0f}s", flush=True)

    # carry md5s forward for unchanged files (path + size match)
    con = sqlite3.connect(cinema.CINEMA_DB, timeout=30)
    cur = con.execute("SELECT path, size_bytes FROM media"
                      " WHERE md5 IS NULL")
    updates = [(old_md5[(p, sz)], p)
               for p, sz in cur.fetchall() if (p, sz) in old_md5]
    con.executemany("UPDATE media SET md5=? WHERE path=?", updates)
    con.commit()
    print("md5 carried forward:", len(updates), flush=True)

    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM media"
                             " GROUP BY kind").fetchall())
    print("after:", kinds, flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
