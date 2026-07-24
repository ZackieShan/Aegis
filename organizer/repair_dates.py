#!/usr/bin/env python3
r"""Repair suspect taken_at dates in photos.db (run with the server STOPPED).

Walks rows whose stored date fails the plausibility rules in date_quality
(out-of-bounds, factory default, camera-model release-year conflict, zeroed
or overflowed mtime) and re-runs the provenance ladder using only stored
columns plus, when needed, a READ-ONLY header read of the file on disk:

    filename date -> container (HEIC meta / video mvhd) -> stored mtime
    -> taken_at NULL with quality 'unknown'

Also backfills date_quality for rows written before the column existed.

Idempotent: rows repaired once are plausible afterwards (or terminally
'unknown'), so a second run changes nothing.

Usage:
    python repair_dates.py                 # repairs .\photos.db
    python repair_dates.py --db other.db   # repairs another database
    python repair_dates.py --dry-run       # report only, no writes
"""
import argparse
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import date_quality as dq  # noqa: E402
import tiff_exif  # noqa: E402

HEIC_EXTS = {".heic", ".heif"}
BMFF_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
BATCH = 500


def backup(db_path):
    """Checkpoint WAL then copy the single-file DB next to the original."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    con.close()
    dst = db_path + ".bak"
    shutil.copy2(db_path, dst)
    return dst


def repair_row(row):
    """Return (updates_dict, tier) for a suspect row, or None if it is not
    actually suspect. updates_dict keys: taken_at, taken_source,
    date_quality, exif_date_raw."""
    taken_at = row["taken_at"]
    src = row["taken_source"] or ""
    exif_origin = src.startswith("exif")
    if not dq.row_is_suspect(taken_at, src, row["make"], row["model"],
                             row["camera"], row["mtime"]):
        return None

    def outcome(dt, source, tier):
        q = "exif_suspect" if exif_origin and tier != "exif" else tier
        return ({"taken_at": dt.isoformat(sep=" ") if dt else None,
                 "taken_source": source,
                 "date_quality": q,
                 "exif_date_raw": taken_at if exif_origin else row["exif_date_raw"]},
                q)

    # 1) filename
    fn_dt = dq.parse_filename_date(row["filename"] or "")
    if fn_dt is not None:
        return outcome(fn_dt, "filename", "filename")

    # 2) container (best-effort, READ-ONLY; file may not exist)
    ext = (row["ext"] or "").lower()
    path = row["path"]
    if ext in HEIC_EXTS and os.path.isfile(path):
        try:
            cdt = tiff_exif.heic_creation_date(path)
        except Exception:
            cdt = None
        ok, _ = dq.check_datetime(cdt, row["make"], row["model"], row["camera"])
        if cdt is not None and ok:
            return outcome(cdt, "container", "container")
    elif ext in BMFF_EXTS and os.path.isfile(path):
        try:
            cdt = tiff_exif.parse_mvhd(path)
        except Exception:
            cdt = None
        ok, _ = dq.check_datetime(cdt, row["make"], row["model"], row["camera"])
        if cdt is not None and ok:
            return outcome(cdt, "video", "container")

    # 3) stored mtime (kept even for factory-default-but-in-bounds values,
    #    flagged; zeroed/overflow/out-of-bounds is unusable)
    m = row["mtime"]
    try:
        m = float(m) if m is not None else None
    except (TypeError, ValueError):
        m = None
    if m is not None and 0 < m < 2 ** 31 and m not in dq.SUSPECT_MTIME_EXACT:
        mdt = datetime.fromtimestamp(m)
        if dq.MIN_DATE <= mdt <= datetime.now() + dq.FUTURE_SLACK:
            tier = ("mtime_suspect"
                    if (mdt.year, mdt.month, mdt.day) in dq.FACTORY_DAYS
                    else "mtime")
            return outcome(mdt, "mtime", tier)

    # 4) nothing usable
    return ({"taken_at": None, "taken_source": "unknown",
             "date_quality": "unknown",
             "exif_date_raw": taken_at if exif_origin else row["exif_date_raw"]},
            "unknown")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(BASE, "photos.db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak copy (scratch DBs in tests)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"database not found: {args.db}")
        return 1

    if not args.dry_run and not args.no_backup:
        bak = backup(args.db)
        print(f"backup written: {bak}")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
    # be robust against pre-ladder schemas
    for col in ("date_quality", "exif_date_raw"):
        if col not in cols:
            con.execute(f"ALTER TABLE photos ADD COLUMN {col} TEXT")
            con.commit()

    # phase 0: backfill date_quality from taken_source where missing
    n_backfill = con.execute(
        "UPDATE photos SET date_quality = CASE "
        "WHEN taken_source LIKE 'exif%' THEN 'exif' "
        "WHEN taken_source IN ('video','container') THEN 'container' "
        "WHEN taken_source = 'unknown' OR taken_at IS NULL THEN 'unknown' "
        "WHEN taken_source = 'filename' THEN 'filename' ELSE 'mtime' END "
        "WHERE date_quality IS NULL").rowcount
    con.commit()
    print(f"phase 0: backfilled date_quality on {n_backfill} legacy rows")

    # phase 1: find suspect rows
    rows = con.execute(
        "SELECT path, filename, ext, taken_at, taken_source, camera, make, model,"
        " mtime, exif_date_raw FROM photos").fetchall()
    suspect = []
    for row in rows:
        rep = repair_row(row)
        if rep is not None:
            suspect.append((row, rep))
    print(f"phase 1: {len(suspect)} suspect rows (of {len(rows)})")

    before_years = Counter()
    after_years = Counter()
    tiers = Counter()
    modes = Counter()
    for row, (updates, tier) in suspect:
        try:
            by = datetime.fromisoformat(row["taken_at"]).year if row["taken_at"] else "null"
        except ValueError:
            by = "unparseable"
        ay = updates["taken_at"][:4] if updates["taken_at"] else "unknown"
        before_years[str(by)] += 1
        after_years[str(ay)] += 1
        tiers[tier] += 1
        modes[str(by)] += 1

    print("\nbefore -> after year histogram (affected rows):")
    for y in sorted(before_years):
        print(f"  {y}: {before_years[y]} rows")
    print("repaired to tiers:", dict(tiers))
    print("after years:", dict(sorted(after_years.items())))

    if args.dry_run:
        print("\n--dry-run: no changes written")
        con.close()
        return 0

    # phase 2: apply in batches
    done = 0
    for row, (updates, tier) in suspect:
        con.execute(
            "UPDATE photos SET taken_at=?, taken_source=?, date_quality=?,"
            " exif_date_raw=? WHERE path=?",
            (updates["taken_at"], updates["taken_source"],
             updates["date_quality"], updates["exif_date_raw"], row["path"]))
        done += 1
        if done % BATCH == 0:
            con.commit()
            print(f"  ... committed {done}/{len(suspect)}")
    con.commit()
    con.close()
    print(f"\nrepaired {done} rows in {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
