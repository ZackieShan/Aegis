#!/usr/bin/env python3
"""AI Photo Organizer - Phase 1.

Local single-user web app on the Python standard library only.
Serves a Win98-themed UI and JSON APIs for scanning a photo folder,
previewing an organization plan, executing moves/copies, and undoing.

Run:  python server.py [--host 127.0.0.1] [--port 7100]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

import date_quality
import cinema
try:
    import music
except Exception as _music_err:
    # A broken/absent music module must never take down photos/cinema.
    music = None
    sys.stderr.write(f"[music] import failed (music disabled): {_music_err}\n")
import tiff_exif
try:
    import llm_review
    import llm_vision
except Exception as _llm_err:
    # LLM assist is optional; its absence must never break core organizing.
    llm_review = None
    llm_vision = None
    sys.stderr.write(f"[llm] import failed (assist disabled): {_llm_err}\n")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime data (DBs, thumbnails, geocode cache, undo logs) can be relocated
# out of the code dir via ORGANIZER_DATA_DIR (Aegis points this at
# data/organizer/); defaults to BASE_DIR for standalone use and tests.
DATA_DIR = os.environ.get("ORGANIZER_DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
THUMB_DIR = os.path.join(DATA_DIR, ".thumbs")
GEOCODE_CACHE_PATH = os.path.join(DATA_DIR, "geocode_cache.json")
DB_PATH = os.path.join(DATA_DIR, "photos.db")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp",
              ".psd"}
HEIC_EXTS = {".heic", ".heif"}
RAW_EXTS = {".nef", ".dng", ".cr2", ".cr3", ".arw", ".rwl", ".rw2", ".orf",
            ".pef", ".srw", ".raf"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg",
              ".mts", ".m2ts", ".3gp"}
SIDECAR_EXTS = {".xmp", ".modd", ".thm", ".aae"}
# ISO-BMFF containers whose creation time lives in moov/mvhd
BMFF_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
ALL_EXTS = IMAGE_EXTS | HEIC_EXTS | RAW_EXTS | VIDEO_EXTS | SIDECAR_EXTS

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
USER_AGENT = "AI-Photo-Organizer/1.0 (local desktop app; educational)"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "GUAM": "GU", "AMERICAN SAMOA": "AS",
    "NORTHERN MARIANA ISLANDS": "MP", "U.S. VIRGIN ISLANDS": "VI",
}

MAKE_MAP = {
    "NIKON CORPORATION": "Nikon", "NIKON": "Nikon",
    "CANON": "Canon", "CANON INC.": "Canon",
    "APPLE": "Apple", "SONY": "Sony", "SONY CORPORATION": "Sony",
    "FUJIFILM": "Fujifilm", "FUJI PHOTO FILM CO., LTD.": "Fujifilm",
    "OLYMPUS": "Olympus", "OLYMPUS CORPORATION": "Olympus",
    "OLYMPUS IMAGING CORP.": "Olympus", "OLYMPUS OPTICAL CO.,LTD": "Olympus",
    "PANASONIC": "Panasonic", "LEICA": "Leica", "SAMSUNG": "Samsung",
    "SAMSUNG TECHWIN": "Samsung", "HUAWEI": "Huawei", "XIAOMI": "Xiaomi",
    "GOOGLE": "Google", "EASTMAN KODAK COMPANY": "Kodak", "KODAK": "Kodak",
    "PENTAX": "Pentax", "RICOH": "Ricoh", "RICOH IMAGING COMPANY, LTD.": "Ricoh",
    "MOTOROLA": "Motorola", "LG ELECTRONICS": "LG", "HTC": "HTC",
    "ONEPLUS": "OnePlus", "DJI": "DJI", "GOPRO": "GoPro",
    "HEWLETT-PACKARD": "HP", "HP": "HP", "EPSON": "Epson", "CASIO": "Casio",
    "CASIO COMPUTER CO.,LTD.": "Casio", "MINOLTA": "Minolta",
    "KONICA MINOLTA": "Konica Minolta", "HASSELBLAD": "Hasselblad",
    "SIGMA": "Sigma", "MICROSOFT": "Microsoft", "NINTENDO": "Nintendo",
}

NAME_TEMPLATES = {
    "orig": "{orig}",
    "date": "{date}_{time}",
    "date_camera": "{date}_{camera}_{orig}",
    "location_seq": "{location}_{seq}",
}

LEVEL_TYPES = ["camera", "year", "month", "location", "location_month"]

PRESETS = {
    "camera_year_locmonth": ["camera", "year", "location_month"],
    "year_month": ["year", "month"],
    "year_locmonth": ["year", "location_month"],
    "loc_year_month": ["location", "year", "month"],
    "year_camera": ["year", "camera"],
}

LOCK = threading.Lock()
SCAN_CANCEL = threading.Event()
EXEC_CANCEL = threading.Event()
VISION_CANCEL = threading.Event()
STATE = {
    "scan": {"state": "idle", "total": 0, "processed": 0, "currentFile": "", "error": None},
    "execute": {"state": "idle", "total": 0, "processed": 0, "currentFile": "", "error": None, "log": []},
    "vision": {"state": "idle", "total": 0, "processed": 0, "tagged": 0, "currentFile": "", "error": None},
    "photos": [],
    "groups": {},          # path -> groupId
    "scannedRoot": None,
    "plan": None,
    "lastUndo": None,
    "partialScan": False,
    "geocodeDisabled": False,
}

_GEO_LAST_REQ = [0.0]
_GRAY_JPEG = None


# ---------------------------------------------------------------- helpers

def normcase_abs(p):
    return os.path.normcase(os.path.abspath(p))


def is_within(path, root):
    """True if path is root or inside root (case-insensitive, absolute)."""
    try:
        return os.path.commonpath([normcase_abs(path), normcase_abs(root)]) == normcase_abs(root)
    except (ValueError, OSError):
        return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ahash_image(im):
    """Manual average hash: 8x8 grayscale, compare to mean -> 64-bit int."""
    try:
        im.draft("L", (8, 8))
    except Exception:
        pass
    g = im.convert("L").resize((8, 8), Image.LANCZOS)
    data = g.tobytes()  # 64 raw grayscale bytes, row-major
    mean = sum(data) / len(data)
    bits = 0
    for p in data:
        bits = (bits << 1) | (1 if p >= mean else 0)
    return bits


def hamming_hex(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def parse_exif_date(s):
    if not s:
        return None
    if isinstance(s, bytes):
        try:
            s = s.decode("ascii", "ignore")
        except Exception:
            return None
    s = str(s).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def clean_camera(make, model):
    make = (str(make).strip() if make else "")
    model = (str(model).strip() if model else "")
    if not make and not model:
        return "Unknown Camera"
    cm = MAKE_MAP.get(make.upper(), make.title() if make else "")
    mtoks = model.split()
    make_toks = make.upper().split()
    while mtoks and make_toks and mtoks[0].upper() == make_toks[0]:
        mtoks.pop(0)
        make_toks.pop(0)
    model_clean = " ".join(mtoks)
    if cm and model_clean:
        return f"{cm} {model_clean}"
    return cm or model_clean or "Unknown Camera"


def gps_to_dec(vals, ref):
    try:
        d, m, s = float(vals[0]), float(vals[1]), float(vals[2])
    except Exception:
        return None
    dec = d + m / 60.0 + s / 3600.0
    if str(ref).upper() in ("S", "W"):
        dec = -dec
    return round(dec, 6)


def reverse_geocode(lat, lon, cancel=None):
    """Short place name like 'Rochester NY' via Nominatim; cached on disk."""
    key = f"{lat:.3f},{lon:.3f}"
    cache = load_json(GEOCODE_CACHE_PATH, {})
    if key in cache:
        return cache[key]
    if STATE["geocodeDisabled"]:
        return "Unknown Location"
    # Nominatim usage policy: max 1 req/sec (sleep in slices so Cancel stays snappy)
    wait = 1.05 - (time.time() - _GEO_LAST_REQ[0])
    while wait > 0:
        if cancel is not None and cancel.is_set():
            return "Unknown Location"
        slice_ = min(0.15, wait)
        time.sleep(slice_)
        wait -= slice_
    name = "Unknown Location"
    try:
        req = urllib.request.Request(
            NOMINATIM_URL.format(lat=lat, lon=lon),
            headers={"User-Agent": USER_AGENT})
        _GEO_LAST_REQ[0] = time.time()
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        addr = data.get("address", {}) or {}
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("hamlet") or addr.get("municipality") or addr.get("county"))
        if city and city.startswith("City of "):
            city = city[len("City of "):]
        cc = (addr.get("country_code") or "").upper()
        if cc == "US":
            st = addr.get("state", "")
            ab = US_STATES.get(st.upper(), st)
            if city and ab:
                name = f"{city} {ab}"
            elif city:
                name = city
            elif ab:
                name = ab
        else:
            if city and cc:
                name = f"{city} {cc}"
            elif city:
                name = city
            elif cc:
                name = cc
    except Exception:
        name = "Unknown Location"
    cache[key] = name
    try:
        save_json(GEOCODE_CACHE_PATH, cache)
    except OSError:
        pass
    return name


ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACES = re.compile(r"\s+")


def sanitize_component(s, cap=60):
    s = ILLEGAL.sub("", str(s))
    s = SPACES.sub(" ", s).strip().strip(".")
    s = s.strip()
    if not s:
        s = "Unknown"
    return s[:cap].rstrip(" .")


def sanitize_filename(s, cap=80):
    s = ILLEGAL.sub("", str(s))
    s = SPACES.sub(" ", s).strip().strip(".")
    if not s:
        s = "photo"
    return s[:cap].rstrip(" .")


# ---------------------------------------------------------------- database

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  filename TEXT,
  dir TEXT,
  ext TEXT,
  size_bytes INTEGER,
  mtime REAL,
  taken_at TEXT,
  taken_source TEXT,
  camera TEXT,
  make TEXT,
  model TEXT,
  gps_lat REAL,
  gps_lon REAL,
  location TEXT,
  md5 TEXT,
  ahash TEXT,
  width INTEGER,
  height INTEGER,
  dupe_group TEXT,
  error TEXT,
  media_type TEXT,
  companions TEXT,
  date_quality TEXT,
  exif_date_raw TEXT,
  scanned_at TEXT,
  vl_caption TEXT,
  vl_tags TEXT,
  vl_scene TEXT,
  vl_kind TEXT,
  vl_quality INTEGER,
  vl_tagged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_camera ON photos(camera);
CREATE INDEX IF NOT EXISTS idx_photos_location ON photos(location);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS roots (
  path TEXT PRIMARY KEY,
  kind TEXT,
  added_at TEXT
);
"""

DB_COLS = ("path filename dir ext size_bytes mtime taken_at taken_source camera make model "
           "gps_lat gps_lon location md5 ahash width height dupe_group error "
           "media_type companions date_quality exif_date_raw scanned_at").split()


def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def db_init():
    with db_connect() as con:
        con.executescript(DB_SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")
        # upgrade path: add columns if an older photos.db lacks them
        cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
        if "dupe_group" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN dupe_group TEXT")
        if "error" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN error TEXT")
        if "media_type" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN media_type TEXT")
        if "companions" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN companions TEXT")
        if "date_quality" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN date_quality TEXT")
        if "exif_date_raw" not in cols:
            con.execute("ALTER TABLE photos ADD COLUMN exif_date_raw TEXT")
        # Phase-4 vision tagging (advisory metadata; never affects file ops)
        for _vc, _vt in (("vl_caption", "TEXT"), ("vl_tags", "TEXT"),
                         ("vl_scene", "TEXT"), ("vl_kind", "TEXT"),
                         ("vl_quality", "INTEGER"), ("vl_tagged_at", "TEXT")):
            if _vc not in cols:
                con.execute(f"ALTER TABLE photos ADD COLUMN {_vc} {_vt}")


def rec_to_row(rec, dupe_group=None, scanned_at=None):
    return {
        "path": rec["path"], "filename": rec["name"],
        "dir": os.path.dirname(rec["path"]), "ext": rec["ext"],
        "size_bytes": rec["size"], "mtime": rec["mtime"],
        "taken_at": rec["dt"], "taken_source": rec["dateSource"],
        "camera": rec["camera"], "make": rec.get("make"), "model": rec.get("model"),
        "gps_lat": rec["lat"], "gps_lon": rec["lon"], "location": rec["location"],
        "md5": rec["md5"], "ahash": rec["ahash"],
        "width": rec["width"], "height": rec["height"],
        "dupe_group": dupe_group, "error": rec.get("error"),
        "media_type": rec.get("media_type") or "photo",
        "companions": (json.dumps(rec["companions"])
                       if rec.get("companions") else None),
        "date_quality": rec.get("date_quality"),
        "exif_date_raw": rec.get("exif_date_raw"),
        "scanned_at": scanned_at or datetime.now().isoformat(sep=" "),
    }


def row_to_rec(row):
    try:
        companions = json.loads(row["companions"]) if row["companions"] else []
        if not isinstance(companions, list):
            companions = []
    except (ValueError, TypeError):
        companions = []
    return {"path": row["path"], "name": row["filename"], "size": row["size_bytes"],
            "mtime": row["mtime"], "width": row["width"], "height": row["height"],
            "dt": row["taken_at"], "dateSource": row["taken_source"] or "mtime",
            "camera": row["camera"] or "Unknown Camera", "make": row["make"],
            "model": row["model"], "lat": row["gps_lat"], "lon": row["gps_lon"],
            "location": row["location"] or "Unknown Location", "md5": row["md5"],
            "ahash": row["ahash"], "exif": row["taken_source"] in ("exif", "exif_original"),
            "error": row["error"], "ext": row["ext"],
            "media_type": row["media_type"] or "photo",
            "companions": companions,
            "date_quality": row["date_quality"],
            "exif_date_raw": row["exif_date_raw"]}


def db_cache_lookup(paths):
    """path -> rec for rows whose size+mtime still match the file on disk."""
    out = {}
    if not paths:
        return out
    with db_connect() as con:
        for i in range(0, len(paths), 400):
            chunk = paths[i:i + 400]
            q = ",".join("?" * len(chunk))
            for row in con.execute(f"SELECT * FROM photos WHERE path IN ({q})", chunk):
                out[row["path"]] = row
    fresh = {}
    for p, row in out.items():
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size == row["size_bytes"] and abs(st.st_mtime - (row["mtime"] or 0)) < 1e-6:
            rec = row_to_rec(row)
            rec["_db_mtime"] = row["mtime"]
            fresh[p] = rec
    return fresh


def db_upsert_recs(recs, groups=None, scanned_at=None):
    """Batch upsert scan records in a single transaction."""
    if not recs:
        return
    groups = groups or {}
    cols = ",".join(DB_COLS)
    marks = ",".join("?" * len(DB_COLS))
    # never overwrite dupe_group via upsert; db_set_dupe_groups owns it
    updates = ",".join(f"{c}=excluded.{c}" for c in DB_COLS if c not in ("path", "dupe_group"))
    sql = f"INSERT INTO photos ({cols}) VALUES ({marks}) ON CONFLICT(path) DO UPDATE SET {updates}"
    rows = []
    for rec in recs:
        r = rec_to_row(rec, groups.get(rec["path"]), scanned_at)
        rows.append([r[c] for c in DB_COLS])
    with db_connect() as con:
        con.executemany(sql, rows)


def db_prune_missing(root, seen_paths):
    """After a COMPLETE scan of root, drop rows under root that no longer exist."""
    prefix = os.path.abspath(root) + os.sep
    with db_connect() as con:
        doomed = [r[0] for r in con.execute(
            "SELECT path FROM photos WHERE instr(path, ?) = 1", (prefix,))
            if r[0] not in seen_paths]
        for i in range(0, len(doomed), 400):
            chunk = doomed[i:i + 400]
            con.execute(f"DELETE FROM photos WHERE path IN ({','.join('?' * len(chunk))})", chunk)
    return len(doomed)


def db_set_dupe_groups(root, groups):
    """Refresh dupe_group flags for rows under root (complete scans only)."""
    prefix = os.path.abspath(root) + os.sep
    with db_connect() as con:
        con.execute("UPDATE photos SET dupe_group=NULL WHERE instr(path, ?) = 1", (prefix,))
        con.executemany("UPDATE photos SET dupe_group=? WHERE path=?",
                        [(gid, p) for p, gid in groups.items()])


def db_set_meta(mapping):
    with db_connect() as con:
        con.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(mapping.items()))


def db_get_meta():
    try:
        with db_connect() as con:
            return {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}


RESTORE_CAP = 200_000


def _infer_root_from_rows(rows):
    """Best-effort scanned-root guess for DBs written before the meta table:
    the common path of the majority drive's photo directories."""
    if not rows:
        return None
    drives = Counter(os.path.splitdrive(r["path"])[0].upper() for r in rows)
    drive = drives.most_common(1)[0][0]
    dirs = [os.path.dirname(r["path"]) for r in rows
            if os.path.splitdrive(r["path"])[0].upper() == drive]
    try:
        common = os.path.commonpath(dirs)
        return common or None
    except (ValueError, OSError):
        return None


def restore_state_from_db():
    """Rebuild in-memory state from photos.db (survive process restarts)."""
    if not os.path.isfile(DB_PATH):
        return False
    try:
        with db_connect() as con:
            total = con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            if not total:
                return False
            rows = con.execute("SELECT * FROM photos ORDER BY path LIMIT ?",
                               (RESTORE_CAP,)).fetchall()
        meta = db_get_meta()
    except sqlite3.Error:
        return False
    photos = [row_to_rec(r) for r in rows]
    groups = {r["path"]: r["dupe_group"] for r in rows if r["dupe_group"]}
    root = meta.get("last_scan_root") or _infer_root_from_rows(rows)
    with LOCK:
        STATE["photos"] = photos
        STATE["groups"] = groups
        STATE["scannedRoot"] = root
        STATE["partialScan"] = meta.get("last_scan_partial") == "1"
        STATE["scan"] = {"state": "done", "total": len(photos),
                         "processed": len(photos), "currentFile": "", "error": None}
    note = f" (capped at {RESTORE_CAP} of {total})" if total > len(rows) else ""
    print(f"[startup] restored {len(rows)} photos from photos.db "
          f"(last scan: {meta.get('last_scan_completed_at', '?')}){note}", flush=True)
    return True


def ensure_state():
    """Guarantee STATE is populated if the DB has data (restart recovery)."""
    with LOCK:
        empty = not STATE["photos"]
    if empty:
        restore_state_from_db()
    with LOCK:
        return bool(STATE["photos"])


def db_apply_manifest(entries, action):
    """Keep the DB truthful after execute/undo path changes."""
    with db_connect() as con:
        for e in entries:
            src, dst = e["from"], e["to"]
            act = action or e.get("action")
            if act == "move":
                con.execute("UPDATE photos SET path=?, filename=?, dir=? WHERE path=?",
                            (dst, os.path.basename(dst), os.path.dirname(dst), src))
            elif act == "copy":
                con.execute(
                    "INSERT OR IGNORE INTO photos (path, filename, dir, ext, size_bytes, mtime,"
                    " taken_at, taken_source, camera, make, model, gps_lat, gps_lon, location,"
                    " md5, ahash, width, height, dupe_group, media_type, companions,"
                    " date_quality, exif_date_raw, scanned_at)"
                    " SELECT ?, ?, ?, ext, size_bytes, mtime, taken_at, taken_source, camera,"
                    " make, model, gps_lat, gps_lon, location, md5, ahash, width, height,"
                    " dupe_group, media_type, companions, date_quality, exif_date_raw, ?"
                    " FROM photos WHERE path=?",
                    (dst, os.path.basename(dst), os.path.dirname(dst),
                     datetime.now().isoformat(sep=" "), src))
            elif act == "delete":  # undo of a copy
                con.execute("DELETE FROM photos WHERE path=?", (dst,))
            elif act == "restore":  # undo of a move: dst -> src
                con.execute("UPDATE photos SET path=?, filename=?, dir=? WHERE path=?",
                            (src, os.path.basename(src), os.path.dirname(src), dst))


def escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def explore_query(qs):
    def p(name):
        return (qs.get(name) or [""])[0].strip()

    where, args = [], []
    month, day = p("month"), p("day")
    if p("on_this_day") == "1":
        today = datetime.now()
        month, day = f"{today.month:02d}", f"{today.day:02d}"
    if month.isdigit():
        where.append("strftime('%m', taken_at) = ?")
        args.append(month.zfill(2))
    if day.isdigit():
        where.append("strftime('%d', taken_at) = ?")
        args.append(day.zfill(2))
    if p("year").isdigit():
        where.append("strftime('%Y', taken_at) = ?")
        args.append(p("year"))
    if p("year_from").isdigit():
        where.append("strftime('%Y', taken_at) >= ?")
        args.append(p("year_from"))
    if p("year_to").isdigit():
        where.append("strftime('%Y', taken_at) <= ?")
        args.append(p("year_to"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p("date_from")):
        where.append("date(taken_at) >= ?")
        args.append(p("date_from"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p("date_to")):
        where.append("date(taken_at) <= ?")
        args.append(p("date_to"))
    hf, ht = p("hour_from"), p("hour_to")
    if hf.isdigit() and ht.isdigit():
        h = "CAST(strftime('%H', taken_at) AS INTEGER)"
        a, b = max(0, min(23, int(hf))), max(0, min(23, int(ht)))
        if a <= b:
            where.append(f"{h} BETWEEN ? AND ?")
        else:  # overnight wrap, e.g. 22 -> 2
            where.append(f"({h} >= ? OR {h} <= ?)")
        args += [a, b]
    if p("place"):
        where.append("location LIKE ? ESCAPE '\\'")
        args.append("%" + escape_like(p("place")) + "%")
    if p("camera"):
        where.append("camera LIKE ? ESCAPE '\\'")
        args.append("%" + escape_like(p("camera")) + "%")
    if p("has_gps") == "1":
        where.append("gps_lat IS NOT NULL")
    if p("dupes_only") == "1":
        where.append("dupe_group IS NOT NULL")
    if p("type") in ("photo", "raw", "video", "sidecar"):
        where.append("media_type = ?")
        args.append(p("type"))
    if p("ext"):
        where.append("ext = ?")
        args.append(p("ext").lower())
    # date-quality filter; COALESCE maps rows written before the column existed
    _qcol = ("COALESCE(date_quality, CASE WHEN taken_source LIKE 'exif%' THEN 'exif' "
             "WHEN taken_source IN ('video','container') THEN 'container' "
             "WHEN taken_source = 'unknown' OR taken_at IS NULL THEN 'unknown' "
             "WHEN taken_source = 'filename' THEN 'filename' ELSE 'mtime' END)")
    if p("quality") == "suspect":
        where.append(f"{_qcol} IN ('exif_suspect','mtime_suspect','unknown')")
    elif p("quality") in ("exif", "exif_suspect", "filename", "container",
                          "mtime", "mtime_suspect", "unknown"):
        where.append(f"{_qcol} = ?")
        args.append(p("quality"))

    sort = {"date_desc": "taken_at IS NULL, taken_at DESC",
            "size": "size_bytes DESC"}.get(p("sort"), "taken_at IS NULL, taken_at")
    try:
        limit = max(1, min(1000, int(p("limit") or 500)))
    except ValueError:
        limit = 500
    try:
        offset = max(0, int(p("offset") or 0))
    except ValueError:
        offset = 0

    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with db_connect() as con:
        count = con.execute(f"SELECT COUNT(*) FROM photos{wsql}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT path, filename, taken_at, camera, location, size_bytes, dupe_group,"
            f" media_type, date_quality, taken_source, ext"
            f" FROM photos{wsql} ORDER BY {sort}, path LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
    results = [{"path": r["path"], "name": r["filename"], "taken_at": r["taken_at"],
                "camera": r["camera"], "location": r["location"], "size": r["size_bytes"],
                "dupeGroup": r["dupe_group"], "mediaType": r["media_type"] or "photo",
                "ext": r["ext"] or "",
                "dateQuality": quality_of({"date_quality": r["date_quality"],
                                           "dateSource": r["taken_source"],
                                           "dt": r["taken_at"]}),
                "thumbUrl": "api/thumb?path=" + urllib.parse.quote(r["path"])}
               for r in rows]
    return {"count": count, "limit": limit, "offset": offset, "results": results}


# ---------------------------------------------------------------- scanning

def extract_photo(path, st):
    rec = {"path": path, "name": os.path.basename(path), "size": st.st_size,
           "mtime": st.st_mtime, "width": None, "height": None, "dt": None,
           "dateSource": "mtime", "camera": "Unknown Camera", "make": None,
           "model": None, "lat": None,
           "lon": None, "location": "Unknown Location", "md5": None,
           "ahash": None, "exif": False, "error": None,
           "ext": os.path.splitext(path)[1].lower(),
           "media_type": "photo", "companions": [],
           "date_quality": None, "exif_date_raw": None, "_exif_dt": None}
    try:
        rec["md5"] = md5_file(path)
    except Exception as e:
        rec["error"] = f"md5 failed: {e}"
    if rec["ext"] in VIDEO_EXTS:
        _fill_video(rec, path)
    elif rec["ext"] in RAW_EXTS:
        _fill_raw(rec, path)
    else:
        _fill_image(rec, path)
    return date_quality.apply_date_ladder(
        rec, path, st, heic_reader=tiff_exif.heic_creation_date)


def extract_sidecar(path, st):
    """Standalone (orphan) sidecar record: indexed so it can be organized,
    but it carries no image metadata of its own."""
    return {"path": path, "name": os.path.basename(path), "size": st.st_size,
            "mtime": st.st_mtime, "width": None, "height": None,
            "dt": datetime.fromtimestamp(st.st_mtime).isoformat(sep=" "),
            "dateSource": "mtime", "camera": "Unknown Camera", "make": None,
            "model": None, "lat": None, "lon": None, "location": "Unknown Location",
            "md5": None, "ahash": None, "exif": False, "error": None,
            "ext": os.path.splitext(path)[1].lower(),
            "media_type": "sidecar", "companions": [],
            "date_quality": "mtime", "exif_date_raw": None}


def _apply_exif(rec, exif):
    rec["exif"] = True
    raw_make = exif.get(0x010F)
    raw_model = exif.get(0x0110)
    rec["make"] = str(raw_make).strip() if raw_make else None
    rec["model"] = str(raw_model).strip() if raw_model else None
    rec["camera"] = clean_camera(raw_make, raw_model)
    exif_ifd = {}
    try:
        exif_ifd = exif.get_ifd(0x8769)
    except Exception:
        pass
    d = parse_exif_date(exif_ifd.get(0x9003)) if exif_ifd else None
    if d:
        rec["_exif_dt"] = d
        rec["exif_date_raw"] = str(exif_ifd.get(0x9003)).strip()
        rec["dateSource"] = "exif_original"
    else:
        d = parse_exif_date(exif.get(0x0132))
        if d:
            rec["_exif_dt"] = d
            rec["exif_date_raw"] = str(exif.get(0x0132)).strip()
            rec["dateSource"] = "exif"
    try:
        gps = exif.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            rec["lat"] = gps_to_dec(gps[2], gps.get(1, "N"))
            rec["lon"] = gps_to_dec(gps[4], gps.get(3, "E"))
    except Exception:
        pass


def _fill_image(rec, path):
    try:
        with Image.open(path) as im:
            rec["width"], rec["height"] = im.size
            try:
                exif = im.getexif()
            except Exception:
                exif = None
            if exif:
                _apply_exif(rec, exif)
            rec["ahash"] = format(ahash_image(im), "016x")
    except Exception as e:
        rec["error"] = ("HEIC/HEIF not readable by Pillow"
                        if rec["ext"] in HEIC_EXTS else f"open failed: {type(e).__name__}")


def _fill_video(rec, path):
    rec["media_type"] = "video"
    if rec["ext"] not in BMFF_EXTS:
        return  # no standard creation-time atom; mtime fallback applies
    try:
        mv = tiff_exif.parse_mvhd(path)
    except Exception:
        mv = None
    if mv is not None:
        rec["dt"] = mv.isoformat(sep=" ")
        rec["dateSource"] = "video"


def _fill_from_preview_bytes(rec, data):
    """Read dimensions + aHash from an embedded JPEG preview (Pillow)."""
    import io
    try:
        with Image.open(io.BytesIO(data)) as im:
            rec["width"], rec["height"] = im.size
            rec["ahash"] = format(ahash_image(im), "016x")
    except Exception:
        pass


def _fill_raw(rec, path):
    """Header-only metadata for RAW files (stdlib TIFF parser), plus aHash
    and dimensions from the embedded JPEG preview when one exists - so a
    RAW + JPEG pair of the same shot shares a perceptual dupe group."""
    rec["media_type"] = "raw"
    ext = rec["ext"]
    preview_bytes = None
    try:
        if ext == ".cr3":
            return  # ISO-BMFF RAW: no TIFF header; mtime fallback
        if ext == ".raf":
            rng = tiff_exif.raf_jpeg_range(path)
            if rng:
                preview_bytes = tiff_exif.extract_preview(path, *rng)
                if preview_bytes:
                    try:  # the embedded JPEG carries the real EXIF
                        import io
                        with Image.open(io.BytesIO(preview_bytes)) as pim:
                            pexif = pim.getexif()
                            if pexif:
                                _apply_exif(rec, pexif)
                    except Exception:
                        pass
        else:
            parsed = tiff_exif.parse_tiff_exif(path)
            if parsed:
                if parsed.get("make") or parsed.get("model"):
                    rec["exif"] = True
                    rec["make"] = parsed.get("make")
                    rec["model"] = parsed.get("model")
                    rec["camera"] = clean_camera(parsed.get("make"),
                                                 parsed.get("model"))
                d = (parse_exif_date(parsed.get("dto"))
                     or parse_exif_date(parsed.get("dt_base")))
                if d:
                    rec["_exif_dt"] = d
                    rec["exif_date_raw"] = parsed.get("dto") or parsed.get("dt_base")
                    rec["dateSource"] = ("exif_original" if parsed.get("dto")
                                         else "exif")
                g = parsed.get("gps")
                if g:
                    rec["lat"], rec["lon"] = g["lat"], g["lon"]
                rng = parsed.get("preview")
                if rng:
                    preview_bytes = tiff_exif.extract_preview(path, *rng)
    except Exception as e:
        rec["error"] = f"raw parse failed: {type(e).__name__}"
    if preview_bytes:
        _fill_from_preview_bytes(rec, preview_bytes)


def set_scan(**kw):
    with LOCK:
        STATE["scan"].update(kw)


# ---- vision tagging (advisory VL captions/tags into photos.db) ----------
# Only raster images Pillow can open are attempted; RAW/HEIC/video are skipped.
VL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
                 ".tif", ".tiff"}


def set_vision(**kw):
    with LOCK:
        STATE["vision"].update(kw)


def vision_status():
    with LOCK:
        return dict(STATE["vision"])


def run_vision(redo=False, limit=0):
    """Caption/tag scanned photos with the VL model into the vl_* columns.
    Advisory only — never touches files. Runs on a daemon thread."""
    try:
        clause = "" if redo else " AND vl_tagged_at IS NULL"
        with db_connect() as con:
            rows = con.execute(
                "SELECT path FROM photos WHERE error IS NULL" + clause +
                " ORDER BY taken_at DESC").fetchall()
        paths = [r["path"] for r in rows
                 if os.path.splitext(r["path"])[1].lower() in VL_IMAGE_EXTS
                 and os.path.isfile(r["path"])]
        if limit and limit > 0:
            paths = paths[:limit]
        set_vision(state="running", total=len(paths), processed=0, tagged=0,
                   currentFile="", error=None)
        tagged = 0
        for i, path in enumerate(paths):
            if VISION_CANCEL.is_set():
                set_vision(state="cancelled", processed=i, currentFile="")
                return
            set_vision(processed=i, currentFile=os.path.basename(path))
            try:
                res = llm_vision.tag_photo(path)
            except Exception:
                res = None
            if res:
                try:
                    with db_connect() as con:
                        con.execute(
                            "UPDATE photos SET vl_caption=?, vl_tags=?, "
                            "vl_scene=?, vl_kind=?, vl_quality=?, vl_tagged_at=? "
                            "WHERE path=?",
                            (res["caption"], json.dumps(res["tags"]),
                             res["scene"], res["kind"], res["quality"],
                             datetime.now().isoformat(sep=" "), path))
                    tagged += 1
                    set_vision(tagged=tagged)
                except Exception:
                    pass
        set_vision(state="done", processed=len(paths), currentFile="",
                   tagged=tagged)
    except Exception as e:
        set_vision(state="error", error=f"{type(e).__name__}: {e}")


def start_vision(redo=False, limit=0):
    if llm_vision is None:
        return False, "LLM vision unavailable."
    with LOCK:
        if STATE["vision"]["state"] == "running":
            return False, "Vision tagging is already running."
    VISION_CANCEL.clear()
    threading.Thread(target=run_vision,
                     kwargs={"redo": redo, "limit": limit},
                     daemon=True).start()
    return True, None


def collect_files(root, max_files, exclude_root=None):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune: hidden caches, previous output tree
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d.lower() in (".thumbs", "$recycle.bin", "system volume information"):
                continue
            if exclude_root and is_within(full, exclude_root):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in ALL_EXTS:
                out.append(os.path.join(dirpath, fn))
                if max_files and len(out) >= max_files:
                    return out
    return out


def run_scan(root, max_files):
    root = os.path.abspath(root)
    cancelled = False
    try:
        prev_target = (STATE.get("plan") or {}).get("params", {}).get("targetRoot")
        exclude = prev_target if prev_target and is_within(prev_target, root) else None
        files = collect_files(root, max_files, exclude)
        files.sort()
        set_scan(total=len(files))
        # sidecar companions: a .xmp/.modd/.thm/.aae whose stem matches a media
        # file in the same folder rides along with that file instead of
        # getting its own row
        media_stems = set()
        for f in files:
            if os.path.splitext(f)[1].lower() not in SIDECAR_EXTS:
                media_stems.add((os.path.normcase(os.path.dirname(f)),
                                 os.path.splitext(os.path.basename(f))[0].lower()))
        cached = db_cache_lookup(files)
        photos = []
        companions_found = []  # (sidecar_path, parent_path)
        scanned_at = datetime.now().isoformat(sep=" ")
        for i, path in enumerate(files):
            if SCAN_CANCEL.is_set():
                cancelled = True
                break
            set_scan(processed=i, currentFile=os.path.basename(path))
            try:
                st = os.stat(path)
            except OSError:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in SIDECAR_EXTS:
                key = (os.path.normcase(os.path.dirname(path)),
                       os.path.splitext(os.path.basename(path))[0].lower())
                if key in media_stems:
                    companions_found.append((path, None))  # parent resolved below
                else:
                    photos.append(extract_sidecar(path, st))
                continue
            rec = cached.get(path)
            if rec is None:
                rec = extract_photo(path, st)
                if rec.get("lat") is not None and rec.get("lon") is not None:
                    rec["location"] = reverse_geocode(rec["lat"], rec["lon"], SCAN_CANCEL)
            photos.append(rec)
            # keep memory bounded on huge libraries: flush to DB every 500 files
            if len(photos) % 500 == 0:
                try:
                    db_upsert_recs(photos[-500:], scanned_at=scanned_at)
                except Exception:
                    pass
        # attach companions to their parent media records (fresh each scan so
        # deleted sidecars do not linger via the cache)
        for rec in photos:
            rec["companions"] = []
        if companions_found:
            by_key = {}
            for rec in photos:
                by_key[(os.path.normcase(os.path.dirname(rec["path"])),
                        os.path.splitext(rec["name"])[0].lower())] = rec
            for sc_path, _ in companions_found:
                key = (os.path.normcase(os.path.dirname(sc_path)),
                       os.path.splitext(os.path.basename(sc_path))[0].lower())
                parent = by_key.get(key)
                if parent is not None:
                    parent["companions"].append(os.path.basename(sc_path))
            for rec in photos:
                if rec["companions"]:
                    rec["companions"].sort()
        # duplicate grouping over whatever we have (full or partial)
        groups = compute_groups(photos)
        with LOCK:
            prev_root = STATE.get("scannedRoot")
            same_root = bool(prev_root) and normcase_abs(root) == normcase_abs(prev_root)
            # A scan that found nothing (or was cancelled before the first
            # file) must NOT wipe the results of a previous good scan unless
            # it is a re-scan of that very same root (files really deleted).
            if photos or (same_root and not cancelled):
                STATE["photos"] = photos
                STATE["groups"] = groups
                STATE["scannedRoot"] = root
                STATE["plan"] = None
                STATE["partialScan"] = cancelled
                kept_previous = False
            else:
                kept_previous = True
        if kept_previous:
            sys.stderr.write(f"[scan] 0 files collected in {root} - "
                             f"previous results kept in memory\n")
        try:
            db_upsert_recs(photos, groups if not cancelled else None, scanned_at)
            if photos or same_root:
                db_set_meta({
                    "last_scan_root": root,
                    "last_scan_completed_at": datetime.now().isoformat(sep=" "),
                    "last_scan_count": str(len(photos)),
                    "last_scan_partial": "1" if cancelled else "0",
                })
            if not cancelled and not max_files:
                db_set_dupe_groups(root, groups)
                db_prune_missing(root, set(files))
        except Exception:
            pass  # DB issues never fail the scan itself
        if cancelled:
            set_scan(state="cancelled", processed=len(photos), currentFile="", error=None)
        else:
            set_scan(state="done", processed=len(files), currentFile="", error=None)
    except Exception as e:
        set_scan(state="error", error=f"{type(e).__name__}: {e}")


# 64-bit aHash split into 7 bands: two hashes at hamming distance <= 6 must
# agree on at least one whole band (pigeonhole: 7 differing bits would be
# needed to touch all 7 bands). Comparing only pairs that share a band
# bucket is therefore EXACT vs the old O(n^2) all-pairs scan.
AHASH_BANDS = [(0, 10), (10, 10), (20, 10), (30, 10), (40, 8), (48, 8), (56, 8)]


def compute_groups(photos):
    """Union-find over exact md5 matches and aHash hamming <= 6.

    Returns {path: groupId} for members of groups with size > 1.
    """
    n = len(photos)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_md5 = {}
    for i, p in enumerate(photos):
        if p.get("md5"):
            if p["md5"] in by_md5:
                union(i, by_md5[p["md5"]])
            else:
                by_md5[p["md5"]] = i

    hashed = []
    for i, p in enumerate(photos):
        if p.get("ahash"):
            try:
                hashed.append((i, int(p["ahash"], 16)))  # hex->int once, not per pair
            except (ValueError, TypeError):
                pass
    for shift, bits in AHASH_BANDS:
        mask = (1 << bits) - 1
        buckets = {}
        for i, h in hashed:
            buckets.setdefault((h >> shift) & mask, []).append((i, h))
        for members in buckets.values():
            m = len(members)
            if m < 2:
                continue
            for a in range(m):
                ia, ha = members[a]
                for b in range(a + 1, m):
                    ib, hb = members[b]
                    if (ha ^ hb).bit_count() <= 6:
                        union(ia, ib)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    multi = [sorted(v, key=lambda i: photos[i]["path"]) for v in clusters.values() if len(v) > 1]
    multi.sort(key=lambda v: photos[v[0]]["path"])
    groups = {}
    for gi, members in enumerate(multi, 1):
        gid = f"G{gi:02d}"
        for i in members:
            groups[photos[i]["path"]] = gid
    return groups


def quality_of(p):
    """date_quality with legacy fallback for rows predating the column."""
    q = p.get("date_quality")
    if q:
        return q
    src = p.get("dateSource") or ""
    if src.startswith("exif"):
        return "exif"
    if src in ("video", "container"):
        return "container"
    if src == "unknown" or not p.get("dt"):
        return "unknown"
    if src == "filename":
        return "filename"
    return "mtime"


def build_results():
    with LOCK:
        photos = [dict(p) for p in STATE["photos"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
        partial = STATE["partialScan"]
    total = len(photos)
    total_bytes = sum(p["size"] for p in photos)
    dts = sorted(p["dt"] for p in photos if p.get("dt"))
    cams = Counter(p["camera"] for p in photos)
    gps_yes = sum(1 for p in photos if p.get("lat") is not None)
    errors = sum(1 for p in photos if p.get("error"))
    by_type = {}
    for p in photos:
        mt = p.get("media_type") or "photo"
        b = by_type.setdefault(mt, {"count": 0, "bytes": 0})
        b["count"] += 1
        b["bytes"] += p["size"]
    by_quality = Counter()
    for p in photos:
        by_quality[quality_of(p)] += 1
    suspect_dates = sum(by_quality[q] for q in date_quality.QUALITY_SUSPECT)

    # exact-duplicate clumps (same md5)
    by_md5 = {}
    for p in photos:
        if p.get("md5"):
            by_md5.setdefault(p["md5"], []).append(p)
    exact_groups = [v for v in by_md5.values() if len(v) > 1]
    wasted = sum(sum(f["size"] for f in sorted(v, key=lambda x: x["path"])[1:]) for v in exact_groups)

    # group composition
    gid_members = {}
    for path, gid in groups.items():
        gid_members.setdefault(gid, []).append(path)
    near_groups = 0
    for gid, members in gid_members.items():
        md5s = {p["md5"] for p in photos if p["path"] in members}
        if len(md5s) > 1:
            near_groups += 1

    return {
        "scannedRoot": root,
        "partial": partial,
        "totalPhotos": total,
        "totalBytes": total_bytes,
        "dateMin": dts[0] if dts else None,
        "dateMax": dts[-1] if dts else None,
        "cameras": cams.most_common(12),
        "byType": by_type,
        "byQuality": dict(by_quality),
        "dateQualityWarning": suspect_dates,
        "gpsCount": gps_yes,
        "noGpsCount": total - gps_yes,
        "errorCount": errors,
        "exactGroups": len(exact_groups),
        "exactWastedBytes": wasted,
        "totalGroups": len(gid_members),
        "nearGroups": near_groups,
        "groups": {gid: sorted(m) for gid, m in sorted(gid_members.items())},
        "photos": photos,
        "thumbSample": [p["path"] for p in photos[:30]],
    }


# ---------------------------------------------------------------- planning

def level_component(level, rec, dt):
    if level == "camera":
        return rec["camera"]
    if level == "year":
        return f"{dt.year:04d}"
    if level == "month":
        return f"{dt.month:02d} {MONTHS[dt.month - 1]}"
    if level == "location":
        return rec["location"]
    if level == "location_month":
        return f"{rec['location']}, {MONTHS[dt.month - 1]}"
    return "Unknown"


TOKEN_RE = re.compile(r"\{(date|time|camera|location|year|month|seq|orig)\}")


def apply_template(tpl, rec, dt, seq):
    stem = os.path.splitext(rec["name"])[0]
    cam = re.sub(r"\s+", "", rec["camera"])
    loc = re.sub(r"[\s,]+", "", rec["location"])
    vals = {
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H-%M-%S"),
        "camera": cam or "UnknownCamera",
        "location": loc or "UnknownLocation",
        "year": f"{dt.year:04d}",
        "month": f"{dt.month:02d}",
        "seq": f"{(seq or 1):04d}",
        "orig": stem,
    }
    return TOKEN_RE.sub(lambda m: vals[m.group(1)], tpl)


def compute_plan(params):
    ensure_state()  # rebuild from DB after a restart if needed
    with LOCK:
        photos = [dict(p) for p in STATE["photos"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
    if not photos or not root:
        return None, "No scan results. Run a scan first."

    levels = [l for l in (params.get("levels") or []) if l in LEVEL_TYPES][:3]
    if not levels:
        return None, "At least one folder level is required."
    nt = params.get("nameTemplate", "orig")
    tpl = params.get("customTemplate") if nt == "custom" else NAME_TEMPLATES.get(nt, "{orig}")
    if not tpl or not TOKEN_RE.search(tpl):
        return None, "Filename template must contain at least one {token}."
    dupe_mode = params.get("dupeMode", "best")
    if dupe_mode not in ("separate", "best", "ignore"):
        return None, "Invalid duplicate mode."
    action = params.get("action", "move")
    if action not in ("move", "copy"):
        return None, "Invalid action."
    target_root = params.get("targetRoot") or os.path.join(root, "Organized")
    target_root = os.path.abspath(target_root)
    if normcase_abs(target_root) == normcase_abs(root):
        return None, "Target root must differ from the scanned folder."

    # best copy per group: highest resolution, tiebreak largest file
    best_of = {}
    gid_members = {}
    for p in photos:
        gid = groups.get(p["path"])
        if gid:
            gid_members.setdefault(gid, []).append(p)
    for gid, members in gid_members.items():
        ranked = sorted(members, key=lambda p: (-(p.get("width") or 0) * (p.get("height") or 0),
                                                -p["size"], p["path"]))
        best_of[gid] = ranked[0]["path"]

    photos_sorted = sorted(photos, key=lambda p: p["path"])
    normal, dupes = [], []
    for p in photos_sorted:
        gid = groups.get(p["path"])
        if gid and dupe_mode == "separate":
            dupes.append((p, gid))
        elif gid and dupe_mode == "best" and p["path"] != best_of.get(gid):
            dupes.append((p, gid))
        else:
            normal.append(p)

    entries = []
    # --- orphan sidecars -> _Sidecars\ (flat, original filename)
    # --- files with no trustworthy date -> _Unknown Date\<Camera>\
    # --- normal-tree files
    staged = []
    unknown_date = 0
    for p in normal:
        if (p.get("media_type") or "photo") == "sidecar":
            entries.append({"from": p["path"],
                            "to": os.path.join(target_root, "_Sidecars", p["name"]),
                            "isDupe": False, "groupId": None, "mediaType": "sidecar"})
            continue
        if not p.get("dt") or (p.get("date_quality") or "") == "unknown":
            unknown_date += 1
            entries.append({"from": p["path"],
                            "to": os.path.join(target_root, "_Unknown Date",
                                               sanitize_component(p["camera"] or "Unknown Camera"),
                                               p["name"]),
                            "isDupe": False, "groupId": groups.get(p["path"]),
                            "mediaType": p.get("media_type") or "photo"})
            continue
        dt = datetime.fromisoformat(p["dt"])
        comps = [sanitize_component(level_component(l, p, dt)) for l in levels]
        dest_dir = os.path.join(target_root, *comps)
        staged.append((p, dt, dest_dir))
    # sequences per destination folder (only when template uses {seq})
    seq_of = {}
    if "{seq}" in tpl:
        by_dir = {}
        for p, dt, dest_dir in staged:
            by_dir.setdefault(dest_dir, []).append((p, dt))
        for dest_dir, items in by_dir.items():
            items.sort(key=lambda t: (t[1], t[0]["name"]))
            for i, (p, dt) in enumerate(items, 1):
                seq_of[p["path"]] = i
    for p, dt, dest_dir in staged:
        gid = groups.get(p["path"])
        stem = sanitize_filename(apply_template(tpl, p, dt, seq_of.get(p["path"])))
        fname = stem + os.path.splitext(p["name"])[1]  # keep original extension/case
        entries.append({"from": p["path"], "to": os.path.join(dest_dir, fname),
                        "isDupe": False, "groupId": gid,
                        "mediaType": p.get("media_type") or "photo"})
    # --- duplicates -> _Duplicates\<group>\ (always keep original filename)
    for p, gid in dupes:
        dest_dir = os.path.join(target_root, "_Duplicates", gid)
        entries.append({"from": p["path"], "to": os.path.join(dest_dir, p["name"]),
                        "isDupe": True, "groupId": gid,
                        "mediaType": p.get("media_type") or "photo"})

    # --- collision resolution (against plan itself and existing disk files)
    used = set()
    collisions = 0
    for e in entries:
        base, ext = os.path.splitext(e["to"])
        cand = e["to"]
        n = 2
        while os.path.normcase(cand) in used or (
                os.path.lexists(cand)
                and normcase_abs(cand) != normcase_abs(e["from"])):
            cand = f"{base}-{n}{ext}"
            n += 1
        if cand != e["to"]:
            collisions += 1
            e["to"] = cand
        used.add(os.path.normcase(cand))

    # --- sidecar companions follow their parent's final target name
    rec_by_path = {p["path"]: p for p in photos}
    companion_files = 0
    for e in entries:
        comps = (rec_by_path.get(e["from"]) or {}).get("companions") or []
        if not comps:
            continue
        parent_stem = os.path.splitext(os.path.basename(e["to"]))[0]
        clist = []
        for name in comps:
            cfrom = os.path.join(os.path.dirname(e["from"]), name)
            cto = os.path.join(os.path.dirname(e["to"]),
                               parent_stem + os.path.splitext(name)[1])
            base, cext = os.path.splitext(cto)
            cand, n = cto, 2
            while os.path.normcase(cand) in used or (
                    os.path.lexists(cand)
                    and normcase_abs(cand) != normcase_abs(cfrom)):
                cand = f"{base}-{n}{cext}"
                n += 1
            used.add(os.path.normcase(cand))
            clist.append({"from": cfrom, "to": cand})
            companion_files += 1
        e["companions"] = clist

    entries.sort(key=lambda e: e["to"])
    folders = sorted({os.path.dirname(e["to"]) for e in entries})
    new_folders = [f for f in folders if not os.path.isdir(f)]
    plan = {
        "params": {"levels": levels, "nameTemplate": nt, "customTemplate": params.get("customTemplate"),
                   "dupeMode": dupe_mode, "action": action,
                   "removeEmpty": bool(params.get("removeEmpty")), "targetRoot": target_root,
                   "scannedRoot": root},
        "entries": entries,
        "stats": {
            "totalFiles": len(entries),
            "companionFiles": companion_files,
            "unknownDateFiles": unknown_date,
            "action": action,
            "dupeFiles": len(dupes),
            "groupCount": len(gid_members),
            "foldersToCreate": len(new_folders),
            "collisionsResolved": collisions,
            "targetRoot": target_root,
            "folderList": new_folders[:500],
        },
    }
    with LOCK:
        STATE["plan"] = plan
    return plan, None


# ---------------------------------------------------------------- execute

def set_exec(**kw):
    with LOCK:
        STATE["execute"].update(kw)


def exec_log(line):
    with LOCK:
        STATE["execute"]["log"].append(line)
        if len(STATE["execute"]["log"]) > 600:
            STATE["execute"]["log"] = STATE["execute"]["log"][-600:]


def resolve_collision(dest, src):
    if not os.path.lexists(dest):
        return dest
    if normcase_abs(dest) == normcase_abs(src):
        return dest
    base, ext = os.path.splitext(dest)
    n = 2
    while os.path.lexists(f"{base}-{n}{ext}"):
        n += 1
    return f"{base}-{n}{ext}"


def remove_empty_dirs(root, exclude):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if normcase_abs(dirpath) == normcase_abs(root):
            continue
        if exclude and is_within(dirpath, exclude):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                exec_log(f"rmdir empty: {dirpath}")
        except OSError:
            pass


def run_execute():
    with LOCK:
        plan = STATE["plan"]
    if not plan:
        set_exec(state="error", error="No plan. Build a plan preview first.")
        return
    params = plan["params"]
    entries = plan["entries"]
    action = params["action"]
    target_root = params["targetRoot"]
    scanned_root = params["scannedRoot"]
    set_exec(total=len(entries), processed=0)
    manifest = []
    moved = copied = skipped = errors = 0
    cancelled = False
    try:
        os.makedirs(target_root, exist_ok=True)
        for i, e in enumerate(entries):
            if EXEC_CANCEL.is_set():
                cancelled = True
                exec_log(f"CANCELLED by user after {i} of {len(entries)} files")
                break
            src, dst = e["from"], e["to"]
            set_exec(processed=i, currentFile=os.path.basename(src))
            try:
                if not is_within(src, scanned_root):
                    raise ValueError("source outside scanned root - refused")
                if normcase_abs(src) == normcase_abs(dst):
                    skipped += 1
                    exec_log(f"SKIP (already in place): {src}")
                    continue
                if not os.path.isfile(src):
                    raise FileNotFoundError("source missing")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                actual = resolve_collision(dst, src)
                if action == "move":
                    shutil.move(src, actual)
                    moved += 1
                    exec_log(f"MOVE {src} -> {actual}")
                else:
                    shutil.copy2(src, actual)
                    copied += 1
                    exec_log(f"COPY {src} -> {actual}")
                manifest.append({"from": src, "to": actual, "action": action})
                # sidecar companions ride along, keeping the parent's final stem
                for comp in e.get("companions") or []:
                    csrc = comp["from"]
                    try:
                        if not os.path.isfile(csrc):
                            continue
                        cdst = os.path.join(
                            os.path.dirname(actual),
                            os.path.splitext(os.path.basename(actual))[0]
                            + os.path.splitext(csrc)[1])
                        cactual = resolve_collision(cdst, csrc)
                        os.makedirs(os.path.dirname(cactual), exist_ok=True)
                        if action == "move":
                            shutil.move(csrc, cactual)
                        else:
                            shutil.copy2(csrc, cactual)
                        manifest.append({"from": csrc, "to": cactual,
                                         "action": action, "companion": True})
                        exec_log(f"{'MOVE' if action == 'move' else 'COPY'} "
                                 f"companion {csrc} -> {cactual}")
                    except Exception as cx:
                        exec_log(f"ERROR companion {csrc}: "
                                 f"{type(cx).__name__}: {cx}")
            except Exception as ex:
                errors += 1
                exec_log(f"ERROR {src}: {type(ex).__name__}: {ex}")
        if action == "move" and params.get("removeEmpty") and not cancelled:
            remove_empty_dirs(scanned_root, target_root)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        undo_name = f"undo_log_{ts}.json"
        undo_path = os.path.join(DATA_DIR, undo_name)
        payload = {"version": 1, "created": datetime.now().isoformat(sep=" "),
                   "action": action, "scannedRoot": scanned_root,
                   "targetRoot": target_root, "entries": manifest,
                   "stats": {"moved": moved, "copied": copied, "skipped": skipped, "errors": errors,
                             "cancelled": cancelled}}
        save_json(undo_path, payload)
        undo_copy = None
        try:
            undo_copy = os.path.join(target_root, undo_name)
            shutil.copyfile(undo_path, undo_copy)
        except OSError:
            undo_copy = None
        with LOCK:
            STATE["lastUndo"] = undo_path
        exec_log(f"{'CANCELLED: ' if cancelled else 'DONE: '}{moved} moved, "
                 f"{copied} copied, {skipped} skipped, {errors} errors")
        exec_log(f"undo manifest: {undo_path}" + (f" (copy: {undo_copy})" if undo_copy else ""))
        try:
            db_apply_manifest(manifest, action)
        except Exception:
            pass  # DB sync failure never fails the run
        with LOCK:
            STATE["execute"]["result"] = {"moved": moved, "copied": copied, "skipped": skipped,
                                          "errors": errors, "cancelled": cancelled,
                                          "undoFile": undo_path,
                                          "undoCopy": undo_copy}
        set_exec(state="cancelled" if cancelled else "done",
                 processed=len(manifest) + skipped + errors, currentFile="", error=None)
    except Exception as e:
        set_exec(state="error", error=f"{type(e).__name__}: {e}")


def run_undo(manifest_path):
    payload = load_json(manifest_path, None)
    if not payload or "entries" not in payload:
        return None, "Manifest not found or invalid."
    target_root = payload.get("targetRoot")
    restored = deleted = skipped = errors = 0
    lines = []
    db_ops = []
    for e in reversed(payload["entries"]):
        src, dst, action = e["from"], e["to"], e.get("action", "move")
        try:
            if target_root and not is_within(dst, target_root):
                raise ValueError("entry target outside target root - refused")
            if action == "move":
                if not os.path.isfile(dst):
                    skipped += 1
                    lines.append(f"SKIP missing: {dst}")
                    continue
                if os.path.lexists(src):
                    raise FileExistsError(f"original path occupied: {src}")
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                restored += 1
                lines.append(f"RESTORE {dst} -> {src}")
                db_ops.append({"from": src, "to": dst, "action": "restore"})
            else:  # copies are deleted on undo
                if os.path.isfile(dst):
                    os.remove(dst)
                    deleted += 1
                    lines.append(f"DELETE copy {dst}")
                    db_ops.append({"from": src, "to": dst, "action": "delete"})
                else:
                    skipped += 1
                    lines.append(f"SKIP missing copy: {dst}")
        except Exception as ex:
            errors += 1
            lines.append(f"ERROR {dst}: {type(ex).__name__}: {ex}")
    try:
        db_apply_manifest(db_ops, None)
    except Exception:
        pass
    # remove the undo-manifest copy the run placed in the target root,
    # so undo fully restores the pre-run state
    if target_root and os.path.isdir(target_root):
        try:
            for fn in os.listdir(target_root):
                if re.fullmatch(r"undo_log_\d{8}_\d{6}\.json", fn):
                    fp = os.path.join(target_root, fn)
                    if os.path.isfile(fp):
                        os.remove(fp)
                        lines.append(f"DELETE manifest copy {fp}")
        except OSError:
            pass
    # clean up emptied folders in the target tree
    removed_dirs = 0
    if target_root and os.path.isdir(target_root):
        for dirpath, dirnames, filenames in os.walk(target_root, topdown=False):
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
                    removed_dirs += 1
            except OSError:
                pass
    return {"restored": restored, "deleted": deleted, "skipped": skipped,
            "errors": errors, "removedDirs": removed_dirs,
            "log": lines[-100:]}, None


# ---------------------------------------------------------------- thumbnails

def gray_jpeg():
    global _GRAY_JPEG
    if _GRAY_JPEG is None:
        import io
        buf = io.BytesIO()
        Image.new("RGB", (1, 1), (192, 192, 192)).save(buf, "JPEG")
        _GRAY_JPEG = buf.getvalue()
    return _GRAY_JPEG


def _raw_preview_bytes(path, ext):
    """Embedded JPEG preview bytes for a RAW file, or None."""
    try:
        if ext == ".cr3":
            return None
        if ext == ".raf":
            rng = tiff_exif.raf_jpeg_range(path)
        else:
            parsed = tiff_exif.parse_tiff_exif(path)
            rng = parsed.get("preview") if parsed else None
        if rng:
            return tiff_exif.extract_preview(path, *rng)
    except Exception:
        pass
    return None


def make_thumb(path, size=128):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    size = max(16, min(1024, int(size or 128)))
    key = hashlib.md5(f"{path}|{mtime}|{size}".encode("utf-8", "ignore")).hexdigest()
    cached = os.path.join(THUMB_DIR, key + ".jpg")
    if os.path.isfile(cached):
        return cached
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        try:
            data = _raw_preview_bytes(path, ext)
            if not data:
                return None
            import io
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                im.thumbnail((size, size), Image.LANCZOS)
                im.save(cached, "JPEG", quality=80)
            return cached
        except Exception:
            return None
    try:
        with Image.open(path) as im:
            try:
                im.draft("RGB", (size, size))
            except Exception:
                pass
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            im.save(cached, "JPEG", quality=80)
        return cached
    except Exception:
        return None


# ---------------------------------------------------------------- reveal in Explorer

def db_add_root(path, kind):
    """Remember a scanned/target root for /api/reveal validation."""
    try:
        with db_connect() as con:
            con.execute("INSERT OR REPLACE INTO roots (path, kind, added_at)"
                        " VALUES (?, ?, ?)",
                        (os.path.abspath(path), kind,
                         datetime.now().isoformat(sep=" ")))
    except sqlite3.Error:
        pass  # best-effort; reveal validation falls back to STATE root


def reveal_in_explorer(path):
    """Fire-and-forget reveal of a file/folder in Windows Explorer.

    Set PO_NO_REVEAL=1 in the environment to make this a no-op (tests).
    """
    if os.environ.get("PO_NO_REVEAL") == "1":
        return
    if os.path.isdir(path):
        subprocess.Popen(["explorer.exe", path])
    else:
        subprocess.Popen(["explorer.exe", "/select," + path])


def reveal_allowed(path):
    """True when path is inside a registered scan/target root (or the
    currently scanned root)."""
    roots = []
    try:
        with db_connect() as con:
            roots = [r[0] for r in con.execute("SELECT path FROM roots")]
    except sqlite3.Error:
        pass
    with LOCK:
        cur = STATE.get("scannedRoot")
    if cur:
        roots.append(cur)
    return any(is_within(path, r) for r in roots)


def reveal_path(p, runner=None, platform_ok=None):
    """Validate + reveal a path in the OS file manager.

    Returns (http_status, payload). `runner` and `platform_ok` are
    injectable so tests can stub Explorer and the platform check.
    """
    if platform_ok is None:
        platform_ok = (os.name == "nt")
    if not platform_ok:
        return 501, {"error": "Open in file location is only supported on Windows."}
    p = (p or "").strip()
    if not p:
        return 400, {"error": "path is required"}
    p = os.path.abspath(p)
    if not os.path.exists(p):
        return 400, {"error": f"Path does not exist: {p}"}
    if not reveal_allowed(p):
        return 400, {"error": "Path is not under a scanned root."}
    try:
        (runner or reveal_in_explorer)(p)
    except Exception as e:
        return 500, {"error": f"reveal failed: {type(e).__name__}: {e}"}
    return 200, {"ok": True}


# ---------------------------------------------------------------- browse
# Folder-picker backend for browse.js (window.BrowseDialog). Directories
# only; drive roots are allowed; ".." segments are normalized away; UNC /
# device / drive-relative paths are rejected defensively. Never lists files.

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]*$")


def browse_drives():
    """Accessible drive roots, e.g. [{"name": "C:\\", "path": "C:\\"}, ...].

    Uses GetLogicalDrives via ctypes on Windows; falls back to probing A-Z
    with os.path.isdir (also the non-Windows dev path). Drives that do not
    answer (empty optical drive, disconnected USB) are skipped gracefully.
    """
    roots = []
    if os.name == "nt":
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    roots.append(chr(ord("A") + i) + ":\\")
        except Exception:
            roots = []
    if not roots:
        for i in range(26):
            root = chr(ord("A") + i) + ":\\"
            if os.path.isdir(root):
                roots.append(root)
    out = []
    for root in roots:
        try:
            if os.path.isdir(root):
                out.append({"name": root, "path": root})
        except OSError:
            continue
    return out


def _dir_is_hidden(path, name):
    """Best-effort hidden flag: dot-names anywhere, FILE_ATTRIBUTE_HIDDEN
    on Windows. Failures just mean 'not hidden'."""
    if name.startswith("."):
        return True
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1:
                return bool(attrs & 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass
    return False


def browse_listing(raw_path):
    """GET /api/browse?path=... -> (status, obj).

    Empty path -> drive list. Otherwise -> subdirectories of the normalized
    path, sorted case-insensitively, hidden dirs flagged, parent computed
    ("" at a drive root means 'back to the drive list'). A directory that
    cannot be read comes back as 403 with path/parent/drives filled in so
    the picker can still navigate away; single unreadable entries inside a
    readable directory are skipped per-entry.
    """
    raw = (raw_path or "").strip()
    drives = browse_drives()
    if not raw:
        return 200, {"path": "", "parent": None, "dirs": [], "drives": drives}
    # Defensive: no UNC ("\\server\share", "//s/s"), device ("\\?\C:") or
    # drive-relative ("C:foo") paths — they resolve unpredictably.
    if raw.startswith(("\\\\", "//")):
        return 400, {"error": "UNC paths are not supported.",
                     "path": "", "parent": None, "dirs": [], "drives": drives}
    if re.match(r"^[A-Za-z]:($|[^\\/])", raw):
        return 400, {"error": "Use an absolute path like C:\\folder.",
                     "path": "", "parent": None, "dirs": [], "drives": drives}
    p = os.path.normpath(raw)          # resolves embedded ".." and "."
    if p.startswith("\\\\"):
        return 400, {"error": "UNC paths are not supported.",
                     "path": "", "parent": None, "dirs": [], "drives": drives}
    if not os.path.isabs(p) and not _DRIVE_RE.match(p):
        return 400, {"error": "Path must be absolute.",
                     "path": "", "parent": None, "dirs": [], "drives": drives}
    if _DRIVE_RE.match(p):
        p = p.rstrip("\\/") + "\\"     # normpath("C:\\") stays "C:\\"
    if not os.path.isdir(p):
        return 404, {"error": f"Not a directory: {p}",
                     "path": p, "parent": None, "dirs": [], "drives": drives}
    parent = "" if _DRIVE_RE.match(p) else (os.path.dirname(p.rstrip("\\/")) or "")
    dirs = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue  # per-dir errors never sink the whole listing
                full = os.path.join(p, entry.name)
                dirs.append({"name": entry.name, "path": full,
                             "hidden": _dir_is_hidden(full, entry.name)})
    except PermissionError:
        return 403, {"error": f"Access denied: {p}", "path": p,
                     "parent": parent, "dirs": [], "drives": drives}
    except OSError as e:
        return 400, {"error": f"Cannot list {p}: {e}", "path": p,
                     "parent": parent, "dirs": [], "drives": drives}
    dirs.sort(key=lambda d: d["name"].lower())
    return 200, {"path": p, "parent": parent, "dirs": dirs, "drives": drives}


# --------------------------------------------- config secret mask guard
# Diagnosis for "I have to re-enter my API keys every time": GET config
# deliberately returns MASKED secrets ("6b19…c9ae"), and the server-side
# save used to accept ANY posted value verbatim. A client that ever posts
# the mask back (form prefilled with the masked display value, copy-paste,
# password-manager autofill) REPLACES the real secret with its own mask —
# verified live: POST {"tmdbKey": "TEST…SK99"} overwrote the stored key.
#
# The fix is installed here at the composition root so cinema.py / music.py
# themselves stay untouched: any value containing the mask ellipsis (every
# mask_secret() output contains "…", real keys are ASCII) or exactly equal
# to the current mask of the stored secret is demoted to "leave unchanged".
# "" still clears (the explicit Clear button); real new values still save.

_MASK_ELLIPSIS = "\u2026"


def _install_secret_guard(mod, fields, kwmap):
    """Wrap mod.save_config so a masked value can never overwrite a secret.

    fields: config keys in positional order, e.g. ["tmdbKey", "tmdbToken"].
    kwmap:  python kwarg name -> config key, e.g. {"tmdb_key": "tmdbKey"}.
    Safe no-op for stubs/older modules lacking save/load/mask callables.
    """
    if mod is None:
        return False
    save = getattr(mod, "save_config", None)
    load = getattr(mod, "load_config", None)
    mask = getattr(mod, "mask_secret", None)
    if not (callable(save) and callable(load) and callable(mask)):
        return False
    if getattr(save, "_secret_guarded", False):
        return True

    def guarded(*args, **kwargs):
        args = list(args)
        try:
            current = load() or {}
        except Exception:
            current = {}

        def clean(key, v):
            if v is None:
                return None                    # leave unchanged
            s = str(v).strip()
            if not s:
                return v                       # "" = explicit clear
            if _MASK_ELLIPSIS in s:
                return None                    # a mask, not a secret
            if s == mask(current.get(key) or ""):
                return None                    # exact mask of stored secret
            return v

        for i, v in enumerate(args):
            if i < len(fields):
                args[i] = clean(fields[i], v)
        for kw in list(kwargs):
            if kw in kwmap:
                kwargs[kw] = clean(kwmap[kw], kwargs[kw])
        return save(*args, **kwargs)

    guarded._secret_guarded = True
    mod.save_config = guarded
    return True


_install_secret_guard(cinema, ["tmdbKey", "tmdbToken"],
                      {"tmdb_key": "tmdbKey", "tmdb_token": "tmdbToken"})
_install_secret_guard(music, ["acoustidKey", "discogsToken", "lastfmKey"],
                      {"acoustid_key": "acoustidKey",
                       "discogs_token": "discogsToken",
                       "lastfm_key": "lastfmKey"})


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoOrganizer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    # -- low-level responders
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- routing
    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            path = u.path
            if path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if path in ("/app.js", "/wm.js", "/cinema.js", "/music.js",
                        "/browse.js", "/singleapp.js", "/theme.js",
                        "/style.css", "/98.css", "/theme-aegis.css",
                        "/ms_sans_serif.woff2", "/ms_sans_serif_bold.woff2"):
                ctype = {"/app.js": "application/javascript; charset=utf-8",
                         "/wm.js": "application/javascript; charset=utf-8",
                         "/cinema.js": "application/javascript; charset=utf-8",
                         "/music.js": "application/javascript; charset=utf-8",
                         "/browse.js": "application/javascript; charset=utf-8",
                         "/singleapp.js": "application/javascript; charset=utf-8",
                         "/theme.js": "application/javascript; charset=utf-8",
                         "/style.css": "text/css; charset=utf-8",
                         "/98.css": "text/css; charset=utf-8",
                         "/theme-aegis.css": "text/css; charset=utf-8",
                         "/ms_sans_serif.woff2": "font/woff2",
                         "/ms_sans_serif_bold.woff2": "font/woff2"}[path]
                return self._static(path.lstrip("/"), ctype)
            if path == "/api/scan/status":
                with LOCK:
                    return self._json(dict(STATE["scan"]))
            if path == "/api/execute/status":
                with LOCK:
                    d = dict(STATE["execute"])
                    d["log"] = list(d["log"][-200:])
                    return self._json(d)
            if path == "/api/vision/status":
                return self._json(vision_status())
            if path == "/api/results":
                if not ensure_state():
                    return self._error(404, "No scan results yet.")
                return self._json(build_results())
            if path == "/api/plan":
                with LOCK:
                    plan = STATE["plan"]
                if not plan:
                    return self._error(404, "No plan yet.")
                return self._json(plan)
            if path == "/api/explore":
                qs = urllib.parse.parse_qs(u.query)
                return self._json(explore_query(qs))
            if path == "/api/cameras":
                with db_connect() as con:
                    cams = [r[0] for r in con.execute(
                        "SELECT DISTINCT camera FROM photos WHERE camera IS NOT NULL"
                        " ORDER BY camera")]
                return self._json({"cameras": cams})
            if path == "/api/extensions":
                with db_connect() as con:
                    exts = [(r[0], r[1]) for r in con.execute(
                        "SELECT ext, COUNT(*) FROM photos WHERE ext IS NOT NULL AND ext != ''"
                        " GROUP BY ext ORDER BY COUNT(*) DESC, ext")]
                return self._json({"extensions": exts})
            # ---- shared folder browse (browse.js picker backend) ----
            if path == "/api/browse":
                qs = urllib.parse.parse_qs(u.query)
                raw = (qs.get("path") or [""])[0]
                code, obj = browse_listing(raw)
                return self._json(obj, code)
            # ---- cinema organizer ----
            if path == "/api/cinema/scan/status":
                return self._json(cinema.scan_status())
            if path == "/api/cinema/execute/status":
                return self._json(cinema.execute_status())
            if path == "/api/cinema/results":
                if not cinema.ensure_state():
                    return self._error(404, "No cinema scan results yet.")
                return self._json(cinema.build_results())
            if path == "/api/cinema/plan":
                with cinema.LOCK:
                    plan = cinema.STATE["plan"]
                if not plan:
                    return self._error(404, "No cinema plan yet.")
                return self._json(plan)
            if path == "/api/cinema/config":
                return self._json(cinema.get_config_public())
            # ---- music organizer (delegated to music.api_get) ----
            # Contract: music.api_get(sub, qs) -> (status:int, obj) where sub
            # is the request path minus the "/api/music/" prefix, e.g.
            # "scan/status", "execute/status", "results", "plan", "config".
            # Unknown sub-paths come back as (404, {...}) from music itself.
            if path.startswith("/api/music/"):
                if music is None:
                    return self._error(503, "Music module unavailable.")
                qs = urllib.parse.parse_qs(u.query)
                code, obj = music.api_get(path[len("/api/music/"):], qs)
                return self._json(obj, code)
            if path == "/api/thumb":
                qs = urllib.parse.parse_qs(u.query)
                p = (qs.get("path") or [""])[0]
                try:
                    size = int((qs.get("size") or ["128"])[0])
                except ValueError:
                    size = 128
                return self._thumb(p, size)
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            body = self._body()
            if u.path == "/api/scan":
                return self._api_scan(body)
            if u.path == "/api/scan/cancel":
                with LOCK:
                    running = STATE["scan"]["state"] == "running"
                if not running:
                    return self._error(409, "No scan is running.")
                SCAN_CANCEL.set()
                return self._json({"ok": True})
            if u.path == "/api/plan":
                return self._api_plan(body)
            if u.path == "/api/plan/summary":
                with LOCK:
                    plan = STATE["plan"]
                return self._plan_summary(plan, "photo")
            if u.path == "/api/cinema/plan/summary":
                return self._plan_summary(cinema.STATE.get("plan"), "movie/TV")
            if u.path == "/api/music/plan/summary":
                plan = music.STATE.get("plan") if music else None
                return self._plan_summary(plan, "music")
            if u.path == "/api/execute":
                return self._api_execute(body)
            if u.path == "/api/execute/cancel":
                with LOCK:
                    running = STATE["execute"]["state"] == "running"
                if not running:
                    return self._error(409, "No execution is running.")
                EXEC_CANCEL.set()
                return self._json({"ok": True})
            if u.path == "/api/undo":
                return self._api_undo(body)
            if u.path == "/api/vision/tag":
                body = body or {}
                try:
                    limit = int(body.get("limit") or 0)
                except (TypeError, ValueError):
                    limit = 0
                ok, err = start_vision(bool(body.get("redo")), limit)
                if not ok:
                    return self._error(409, err)
                return self._json({"ok": True})
            if u.path == "/api/vision/cancel":
                with LOCK:
                    running = STATE["vision"]["state"] == "running"
                if not running:
                    return self._error(409, "No vision tagging is running.")
                VISION_CANCEL.set()
                return self._json({"ok": True})
            # ---- cinema organizer ----
            if u.path == "/api/cinema/scan":
                return self._api_cinema_scan(body)
            if u.path == "/api/cinema/scan/cancel":
                if not cinema.cancel_scan():
                    return self._error(409, "No cinema scan is running.")
                return self._json({"ok": True})
            if u.path == "/api/cinema/plan":
                return self._api_cinema_plan(body)
            if u.path == "/api/cinema/execute":
                ok, err = cinema.start_execute()
                if not ok:
                    return self._error(409, err)
                return self._json({"ok": True})
            if u.path == "/api/cinema/execute/cancel":
                if not cinema.cancel_execute():
                    return self._error(409, "No cinema execution is running.")
                return self._json({"ok": True})
            if u.path == "/api/cinema/undo":
                return self._api_cinema_undo(body)
            if u.path == "/api/cinema/config":
                body = body or {}
                cinema.save_config(
                    body.get("tmdbKey") if "tmdbKey" in body else None,
                    body.get("tmdbToken") if "tmdbToken" in body else None)
                return self._json(cinema.get_config_public())
            # ---- music organizer (delegated to music.api_post) ----
            # Contract: music.api_post(sub, body) -> (status:int, obj) where
            # sub is the request path minus the "/api/music/" prefix, e.g.
            # "scan", "scan/cancel", "plan", "execute", "execute/cancel",
            # "undo", "config". Unknown sub-paths come back as (404, {...}).
            if u.path.startswith("/api/music/"):
                if music is None:
                    return self._error(503, "Music module unavailable.")
                code, obj = music.api_post(u.path[len("/api/music/"):], body)
                return self._json(obj, code)
            if u.path == "/api/reveal":
                code, payload = reveal_path((body or {}).get("path"))
                return self._json(payload, code)
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _plan_summary(self, plan, domain):
        """LLM plain-English review of the current plan (advisory only)."""
        if llm_review is None:
            return self._error(503, "LLM assist unavailable.")
        if not plan or not plan.get("entries"):
            return self._error(409, "No plan to summarize — build a plan first.")
        try:
            out = llm_review.summarize_plan(
                plan.get("entries"), plan.get("stats") or {}, domain)
        except Exception as e:
            return self._error(500, f"{type(e).__name__}: {e}")
        return self._json(out)

    # -- endpoints
    def _static(self, name, ctype):
        full = os.path.join(BASE_DIR, name)
        if not os.path.isfile(full):
            return self._error(404, "not found")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _thumb(self, p, size=128):
        if not p or not os.path.isfile(p):
            return self._send(200, gray_jpeg(), "image/jpeg")
        cached = make_thumb(p, size)
        if not cached:
            return self._send(200, gray_jpeg(), "image/jpeg")
        with open(cached, "rb") as f:
            self._send(200, f.read(), "image/jpeg")

    def _api_scan(self, body):
        with LOCK:
            if STATE["scan"]["state"] == "running":
                return self._error(409, "A scan is already running.")
        root = (body.get("path") or "").strip()
        if not root:
            return self._error(400, "path is required")
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return self._error(400, f"Not a directory: {root}")
        db_add_root(root, "scan")
        try:
            max_files = int(body.get("max") or 0)
        except (TypeError, ValueError):
            max_files = 0
        SCAN_CANCEL.clear()
        with LOCK:
            STATE["scan"] = {"state": "running", "total": 0, "processed": 0,
                             "currentFile": "", "error": None}
            STATE["partialScan"] = False
        t = threading.Thread(target=run_scan, args=(root, max_files), daemon=True)
        t.start()
        return self._json({"ok": True, "root": root})

    def _api_plan(self, body):
        plan, err = compute_plan(body or {})
        if err:
            return self._error(400, err)
        tr = (plan.get("params") or {}).get("targetRoot")
        if tr:
            db_add_root(tr, "target")
        return self._json(plan)

    def _api_execute(self, body):
        with LOCK:
            if STATE["execute"]["state"] == "running":
                return self._error(409, "Execution already running.")
            if not STATE["plan"]:
                return self._error(400, "No plan. Build a plan preview first.")
            STATE["execute"] = {"state": "running", "total": 0, "processed": 0,
                                "currentFile": "", "error": None, "log": [], "result": None}
        EXEC_CANCEL.clear()
        t = threading.Thread(target=run_execute, daemon=True)
        t.start()
        return self._json({"ok": True})

    def _api_undo(self, body):
        manifest = (body.get("manifest") or "").strip()
        if not manifest:
            with LOCK:
                manifest = STATE.get("lastUndo") or ""
        if not manifest:
            return self._error(400, "No undo manifest specified.")
        if not os.path.isfile(manifest):
            return self._error(404, f"Manifest not found: {manifest}")
        result, err = run_undo(manifest)
        if err:
            return self._error(400, err)
        return self._json(result)

    # -- cinema organizer endpoints
    def _api_cinema_scan(self, body):
        root = ((body or {}).get("path") or "").strip()
        if not root:
            return self._error(400, "path is required")
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return self._error(400, f"Not a directory: {root}")
        try:
            max_files = int(body.get("max") or 0)
        except (TypeError, ValueError):
            max_files = 0
        hash_enabled = bool(body.get("hash"))
        ok, err = cinema.start_scan(root, max_files, hash_enabled)
        if not ok:
            return self._error(409, err)
        db_add_root(root, "scan")
        return self._json({"ok": True, "root": root})

    def _api_cinema_plan(self, body):
        plan, err = cinema.compute_plan(body or {})
        if err:
            return self._error(400, err)
        tr = (plan.get("params") or {}).get("targetRoot")
        if tr:
            db_add_root(tr, "target")
        return self._json(plan)

    def _api_cinema_undo(self, body):
        manifest = ((body or {}).get("manifest") or "").strip()
        if not manifest:
            with cinema.LOCK:
                manifest = cinema.STATE.get("lastUndo") or ""
        if not manifest:
            return self._error(400, "No undo manifest specified.")
        if not os.path.isfile(manifest):
            return self._error(404, f"Manifest not found: {manifest}")
        result, err = cinema.run_undo(manifest)
        if err:
            return self._error(400, err)
        return self._json(result)


def music_safe_init():
    """Init the music module at startup. A music-module failure must never
    take down photos/cinema, so every error is swallowed to stderr."""
    if music is None:
        return
    try:
        music.db_init()
        music.restore_state()
    except Exception as e:
        sys.stderr.write(f"[music] init failed (server continuing): {e}\n")


def port_in_use(host, port):
    """True if something is accepting TCP connections on host:port.

    Needed because HTTPServer sets SO_REUSEADDR, which on Windows lets a
    second instance bind the same port instead of failing with OSError.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="AI Photo Organizer (Phase 1)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7100)
    ap.add_argument("--no-browser", action="store_true",
                    help="do not auto-open the default browser on startup")
    args = ap.parse_args()
    os.makedirs(THUMB_DIR, exist_ok=True)
    db_init()
    if port_in_use(args.host, args.port):
        print("")
        print(f"Port {args.port} is already in use — the organizer may already be running.")
        print(f"Open http://localhost:{args.port}/ in your browser, or pick another port:")
        print(f"    python server.py --port {args.port + 1}")
        sys.exit(2)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        print("")
        print(f"Port {args.port} is already in use — the organizer may already be running.")
        print(f"Open http://localhost:{args.port}/ in your browser, or pick another port:")
        print(f"    python server.py --port {args.port + 1}")
        sys.exit(2)
    server.daemon_threads = True
    restore_state_from_db()  # pick up where a previous run left off
    cinema.db_init()
    cinema.restore_state()
    music_safe_init()
    url = f"http://{args.host}:{args.port}/"
    print("=" * 60)
    print("  AI PHOTO ORGANIZER 1.0")
    print(f"  Serving at:  {url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60, flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
