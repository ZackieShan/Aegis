#!/usr/bin/env python3
"""Music Organizer core: tag-driven album clustering, MusicBrainz
identification, quality ranking, 5-stage dedupe, SQLite index, and
scan -> results -> plan -> execute -> undo.

Tag/tech/payload parsing lives in music_tags.py; all network access lives
in music_remote.py (MusicBrainz / Cover Art Archive / AcoustID / Discogs /
Last.fm -- throttled, cached, failure-safe). Both are imported defensively
and every call is wrapped, so the organizer stays fully usable offline:
unidentified albums keep tag-derived names and 'Unclassified' genres.

Threading model + state shapes mirror cinema.py: module STATE guarded by
LOCK, SCAN_CANCEL / EXEC_CANCEL Events, daemon worker threads, progress
dicts polled over HTTP through api_get / api_post (server.py delegates
every /api/music/* request here).

Quarantine, never delete: dupe losers go to _Duplicates\\Gxx and undo
restores every moved file byte-for-byte.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime

try:
    import music_remote
except Exception:            # remote module broken/missing -> offline mode
    music_remote = None

try:
    import music_tags
except Exception:            # tags module broken/missing -> per-file errors
    music_tags = None

try:
    import music_tagfix
except Exception:            # mutagen missing -> tag write-back unavailable
    music_tagfix = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime DB + undo logs honor ORGANIZER_DATA_DIR (set by Aegis); config with
# API keys stays in the code dir. Defaults to BASE_DIR standalone/in tests.
DATA_DIR = os.environ.get("ORGANIZER_DATA_DIR") or BASE_DIR
MUSIC_DB = os.path.join(DATA_DIR, "music.db")
CONFIG_PATH = os.path.join(BASE_DIR, "music_config.json")
UNDO_DIR = DATA_DIR

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
              ".wav", ".aiff"}
SIDECAR_EXTS = {".cue", ".log", ".lrc", ".nfo"}
ART_EXTS = {".jpg", ".jpeg", ".png"}
COMPANION_EXTS = SIDECAR_EXTS | ART_EXTS
HASH_MAX = 2 * 1024 ** 3            # skip full-file md5 for files >= 2GB
# Cap on per-track rows in the /results payload. The UI only reads summary
# stats from it; sending every row of a 90k-track library produced a 100MB+
# response that stalled the browser after an otherwise-successful scan.
RESULTS_REC_CAP = 2000
VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
VA_NAME = "Various Artists"

# fingerprinting is optional: fpcalc.exe on PATH or beside the app.
# None = autodetect each scan; "" = force-disabled; a path = explicit.
FPCALC = None

LOCK = threading.Lock()
SCAN_CANCEL = threading.Event()
EXEC_CANCEL = threading.Event()
TAGFIX_CANCEL = threading.Event()
TAG_BACKUP_DIR = os.path.join(DATA_DIR, "tag_backups")

# Scan-status shape: total/processed count FILES for the collect/tags phase;
# long non-file phases (identify works per ALBUM cluster, fingerprint per
# file, artwork per album) additionally publish phase-scoped counters
# (phase, phaseDone, phaseTotal) plus a measured-rate ETA (phaseElapsed,
# phaseRate, phaseEta, computed in scan_status) so the UI never looks stuck
# at 100% while MusicBrainz throttles along at ~1 req/s. phaseStartedAt is
# internal (monotonic clock) and never leaves the process.
STATE = {
    "scan": {"state": "idle", "total": 0, "processed": 0,
             "currentFile": "", "phase": "", "error": None,
             "phaseDone": 0, "phaseTotal": 0, "phaseStartedAt": None,
             "note": None},
    "execute": {"state": "idle", "total": 0, "processed": 0,
                "currentFile": "", "error": None, "log": [], "result": None,
                "phase": "", "phaseDone": 0, "phaseTotal": 0},
    "tagfix": {"state": "idle", "total": 0, "processed": 0, "changed": 0,
               "skipped": 0, "errors": 0, "currentFile": "", "error": None,
               "backupFile": None},
    "recs": [],             # track dicts (audio files only)
    "releases": {},         # cluster_id -> release dict
    "groups": {},           # path -> dupe group id (G01..)
    "review": {},           # path -> fuzzy-review group id (R01.., never auto)
    "scannedRoot": None,
    "plan": None,
    "lastUndo": None,
    "partialScan": False,
}


# =================================================================== helpers

def normcase_abs(p):
    return os.path.normcase(os.path.abspath(p))


def is_within(path, root):
    try:
        return os.path.commonpath([normcase_abs(path), normcase_abs(root)]) \
            == normcase_abs(root)
    except (ValueError, OSError):
        return False


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(s):
    """One safe Windows path component (never '', never ends in '.'/' ')."""
    s = _BAD_CHARS.sub(" ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return (s or "Unknown")[:80].rstrip(" .")


def md5_file(path, cancel=None):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if cancel is not None and cancel.is_set():
                return None
            h.update(chunk)
    return h.hexdigest()


def resolve_collision(dest, src):
    if normcase_abs(dest) == normcase_abs(src):
        return dest
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    for i in range(2, 1000):
        cand = f"{stem}-{i}{ext}"
        if not os.path.exists(cand):
            return cand
    raise RuntimeError(f"cannot resolve collision for {dest}")


def set_scan(**kw):
    with LOCK:
        STATE["scan"].update(kw)


def _phase_start(phase, total):
    """Begin a progress-tracked scan phase: reset counters + ETA clock."""
    set_scan(phase=phase, phaseDone=0, phaseTotal=int(total or 0),
             phaseStartedAt=time.monotonic())


def _phase_indeterminate(phase):
    """Mark a quick/indeterminate phase (no meaningful total, no ETA)."""
    set_scan(phase=phase, phaseDone=0, phaseTotal=0, phaseStartedAt=None)


def eta_seconds(done, total, elapsed):
    """Seconds remaining from measured throughput (None when unknowable).
    Linear estimate: (total-done) / (done/elapsed)."""
    if not total or total <= 0:
        return None
    done = done or 0
    if done >= total:
        return 0.0
    if not done or not elapsed or elapsed <= 0:
        return None
    return max(0.0, (total - done) * (float(elapsed) / done))


def set_exec(**kw):
    with LOCK:
        STATE["execute"].update(kw)
        if "log" in kw:
            STATE["execute"]["log"] = STATE["execute"]["log"][-500:]


def exec_log(msg):
    with LOCK:
        STATE["execute"]["log"] = (STATE["execute"]["log"] + [msg])[-500:]


def scan_status():
    with LOCK:
        d = dict(STATE["scan"])
    started = d.pop("phaseStartedAt", None)     # internal monotonic clock
    done = d.get("phaseDone") or 0
    total = d.get("phaseTotal") or 0
    elapsed = (time.monotonic() - started) \
        if started and d.get("state") == "running" else None
    d["phaseElapsed"] = round(elapsed, 2) if elapsed else None
    d["phaseRate"] = round(done / elapsed, 3) \
        if elapsed and done else None
    d["phaseEta"] = eta_seconds(done, total, elapsed)
    return d


def execute_status():
    with LOCK:
        d = dict(STATE["execute"])
        d["log"] = list(d["log"][-200:])
        return d


# =================================================================== text

def _fold(s):
    """ASCII-fold diacritics (Beyoncé -> Beyonce)."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def normalize(s):
    """Identity normalization: folded, lowercased, alphanumeric words only."""
    return re.sub(r"[^a-z0-9]+", " ", _fold(s).lower()).strip()


_FEAT_SPLIT = re.compile(r"\s*[\(\[]?\s*(?:feat\.?|featuring|ft\.?)\s+.*$", re.I)
_FEAT_ARTIST = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.I)


def strip_feat(s):
    """'Song (feat. X)' -> 'Song' (Picard-style feat. removal)."""
    return _FEAT_SPLIT.sub("", s or "").strip()


def main_artist(s):
    """'A feat. B' -> 'A' (primary artist for counting/identity)."""
    return _FEAT_ARTIST.split(s or "", maxsplit=1)[0].strip()


_STEM_COPY_LEAD = re.compile(r"^\s*copy\s+of\s+", re.I)
_STEM_COPY_TAIL = re.compile(
    r"(?:\s*[-_–—]\s*copy|\s*\(\s*(?:copy|\d{1,3})\s*\))\s*$", re.I)
_STEM_SEP = re.compile(r"[-_–—\s]+")


def normalize_stem(name):
    """'Obvious copy'-proof filename-stem key for the first-pass dupe
    filter: strips leading 'Copy of ' and trailing ' (1)'/' (2)'.../
    ' (copy)'/' - Copy' markers (repeatedly, so 'Name (1) - Copy' folds),
    folds case/diacritics, and collapses runs of spaces/underscores/dashes
    to single spaces ('Song_Name' == 'song-name' == 'SONG  name').
    Parenthesized years like ' (2021)' are KEPT (4 digits never look like
    an Explorer copy counter). Extension is NOT stripped here -- callers
    pass the bare stem or drop it themselves."""
    s = os.path.splitext(name or "")[0]
    s = _fold(s).lower()
    prev = None
    while prev != s:
        prev = s
        s = _STEM_COPY_LEAD.sub("", s)
        s = _STEM_COPY_TAIL.sub("", s)
    return _STEM_SEP.sub(" ", s).strip()


def _s(v):
    """Tag string: stripped, '' -> None."""
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _to_int(v):
    """'3/12' -> 3, '07' -> 7, junk/None -> None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.match(r"\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


# =================================================================== quality

LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "pcm", "ape", "wv", "tta"}

# Ladder: lossless 5000 > 320k 3100 > V0 3000 > 256k 2700 > V2 2600
#         > 192k 2200 > 160k 1800 > 128k 1400 > anything else.
def quality_score(codec, bitrate_kbps, vbr):
    c = (codec or "").lower()
    br = bitrate_kbps or 0
    if c in LOSSLESS_CODECS:
        return 5000
    if vbr:
        if br >= 225:
            return 3000           # ~V0
        if br >= 170:
            return 2600           # ~V2
        if br >= 130:
            return 1800
        if br > 0:
            return 1200
        return 1500
    if br >= 320:
        return 3100
    if br >= 256:
        return 2700
    if br >= 192:
        return 2200
    if br >= 160:
        return 1800
    if br >= 128:
        return 1400
    if br > 0:
        return 1000
    return 500


def quality_desc(r):
    """Human reason fragment: 'lossless flac', 'mp3 320 kbps', ..."""
    c = (r.get("codec") or "").lower()
    br = r.get("bitrate_kbps") or 0
    if c in LOSSLESS_CODECS:
        return f"lossless {c or 'audio'}"
    if r.get("vbr") and br:
        return f"{c or 'audio'} VBR ~{br} kbps"
    if br:
        return f"{c or 'audio'} {br} kbps"
    return c or "unknown codec"


def tag_completeness(r):
    """0..5: artist/albumartist, album, title, track number, year present."""
    n = 0
    if r.get("artist") or r.get("albumartist"):
        n += 1
    if r.get("album"):
        n += 1
    if r.get("title"):
        n += 1
    if r.get("trackno"):
        n += 1
    if r.get("year"):
        n += 1
    return n


def filename_sanity(r):
    """1 when the normalized filename contains the normalized title."""
    t = normalize(strip_feat(r.get("title")))
    if not t:
        return 0
    stem = os.path.splitext(r.get("name") or "")[0]
    return 1 if t in normalize(stem) else 0


def rank_key(r):
    """Best copy first: quality, embedded art, tag completeness, duration,
    filename sanity, then path for determinism."""
    return (-(r.get("quality_score") or 0),
            0 if r.get("has_art") else 1,
            -tag_completeness(r),
            -(r.get("duration_s") or 0),
            -filename_sanity(r),
            (r.get("path") or "").lower())


# =================================================================== db

TRACK_COLS = [
    "path", "filename", "dir", "ext", "size_bytes", "mtime",
    "md5", "payload_md5",
    "codec", "duration_s", "bitrate_kbps", "vbr", "samplerate", "channels",
    "artist", "albumartist", "album", "title",
    "trackno", "tracktotal", "discno", "disctotal",
    "year", "genre", "subgenre", "compilation", "has_art",
    "quality_score", "mb_recording_id", "mb_release_id",
    "acoustid", "fingerprint",
    "cluster_id", "dupe_group", "review_group",
    "keep", "keep_reason", "error", "scanned_at",
    "quarantined",
]

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  filename TEXT, dir TEXT, ext TEXT,
  size_bytes INTEGER, mtime REAL,
  md5 TEXT, payload_md5 TEXT,
  codec TEXT, duration_s REAL, bitrate_kbps INTEGER, vbr INTEGER DEFAULT 0,
  samplerate INTEGER, channels INTEGER,
  artist TEXT, albumartist TEXT, album TEXT, title TEXT,
  trackno INTEGER, tracktotal INTEGER, discno INTEGER, disctotal INTEGER,
  year INTEGER, genre TEXT, subgenre TEXT,
  compilation INTEGER DEFAULT 0, has_art INTEGER DEFAULT 0,
  quality_score INTEGER,
  mb_recording_id TEXT, mb_release_id TEXT,
  acoustid TEXT, fingerprint TEXT,
  cluster_id TEXT, dupe_group TEXT, review_group TEXT,
  keep INTEGER DEFAULT 1, keep_reason TEXT,
  error TEXT, scanned_at TEXT,
  quarantined INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS releases (
  cluster_id TEXT PRIMARY KEY,
  mb_release_id TEXT, mb_release_group_id TEXT,
  albumartist TEXT, album TEXT, year INTEGER,
  is_compilation INTEGER DEFAULT 0,
  genre TEXT, subgenre TEXT,
  label TEXT, catno TEXT, art_path TEXT,
  source TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS mmeta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def db_connect():
    con = sqlite3.connect(MUSIC_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


@contextlib.contextmanager
def _db():
    """Commit-on-success connection that ALWAYS closes (sqlite3's own
    context manager commits but never closes, which pins files on
    Windows)."""
    con = db_connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_columns(con):
    """In-place schema upgrades for pre-existing music.db files (CREATE
    TABLE IF NOT EXISTS never alters an old table)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    if cols and "quarantined" not in cols:
        con.execute("ALTER TABLE tracks ADD COLUMN"
                    " quarantined INTEGER DEFAULT 0")


def db_init():
    with _db() as con:
        con.executescript(DB_SCHEMA)
        _ensure_columns(con)
        con.execute("PRAGMA journal_mode=WAL")


def get_meta():
    try:
        with _db() as con:
            return {r[0]: r[1] for r in con.execute(
                "SELECT key, value FROM mmeta")}
    except sqlite3.Error:
        return {}


def set_meta(kv):
    with _db() as con:
        for k, v in kv.items():
            con.execute("INSERT OR REPLACE INTO mmeta (key, value)"
                        " VALUES (?, ?)", (k, str(v)))


def db_replace_state(recs, groups, review, releases, scanned_at):
    """Full-replace persist of one scan (mirrors cinema.db_replace_recs)."""
    ph = "(" + ",".join("?" * len(TRACK_COLS)) + ")"
    cols = "(" + ",".join(TRACK_COLS) + ")"
    with _db() as con:
        con.execute("DELETE FROM tracks")
        con.execute("DELETE FROM releases")
        for rec in recs:
            con.execute(
                f"INSERT OR REPLACE INTO tracks {cols} VALUES {ph}",
                (rec["path"], rec["name"], os.path.dirname(rec["path"]),
                 rec["ext"], rec.get("size"), rec.get("mtime"),
                 rec.get("md5"), rec.get("payload_md5"),
                 rec.get("codec"), rec.get("duration_s"),
                 rec.get("bitrate_kbps"), 1 if rec.get("vbr") else 0,
                 rec.get("samplerate"), rec.get("channels"),
                 rec.get("artist"), rec.get("albumartist"), rec.get("album"),
                 rec.get("title"), rec.get("trackno"), rec.get("tracktotal"),
                 rec.get("discno"), rec.get("disctotal"),
                 rec.get("year"), rec.get("genre"), rec.get("subgenre"),
                 1 if rec.get("compilation") else 0,
                 1 if rec.get("has_art") else 0,
                 rec.get("quality_score"), rec.get("mb_recording_id"),
                 rec.get("mb_release_id"), rec.get("acoustid"),
                 rec.get("fingerprint"), rec.get("cluster_id"),
                 groups.get(rec["path"]), review.get(rec["path"]),
                 1 if rec.get("keep", 1) else 0, rec.get("keep_reason"),
                 rec.get("error"), scanned_at,
                 1 if rec.get("quarantined") else 0))
        for rel in releases.values():
            con.execute(
                "INSERT OR REPLACE INTO releases (cluster_id, mb_release_id,"
                " mb_release_group_id, albumartist, album, year,"
                " is_compilation, genre, subgenre, label, catno, art_path,"
                " source, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel.get("cluster_id"), rel.get("mb_release_id"),
                 rel.get("mb_release_group_id"), rel.get("albumartist"),
                 rel.get("album"), rel.get("year"),
                 1 if rel.get("is_compilation") else 0,
                 rel.get("genre"), rel.get("subgenre"), rel.get("label"),
                 rel.get("catno"), rel.get("art_path"), rel.get("source"),
                 rel.get("confidence")))


def db_upsert_release(rel):
    """Persist ONE releases row immediately, as a cluster completes, so a
    kill/crash mid-identify keeps every finished cluster and 'Resume
    identification' never redoes that work (pending == no releases row)."""
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO releases (cluster_id, mb_release_id,"
            " mb_release_group_id, albumartist, album, year,"
            " is_compilation, genre, subgenre, label, catno, art_path,"
            " source, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rel.get("cluster_id"), rel.get("mb_release_id"),
             rel.get("mb_release_group_id"), rel.get("albumartist"),
             rel.get("album"), rel.get("year"),
             1 if rel.get("is_compilation") else 0,
             rel.get("genre"), rel.get("subgenre"), rel.get("label"),
             rel.get("catno"), rel.get("art_path"), rel.get("source"),
             rel.get("confidence")))


def db_update_paths(entries, action):
    """Keep tracks.path truthful after execute/undo."""
    with _db() as con:
        for e in entries:
            src, dst = e.get("from"), e.get("to")
            if not src:
                continue
            act = action or e.get("action")
            if act == "move":
                con.execute(
                    "UPDATE tracks SET path=?, filename=?, dir=? WHERE path=?",
                    (dst, os.path.basename(dst), os.path.dirname(dst), src))
            elif act == "restore":
                con.execute(
                    "UPDATE tracks SET path=?, filename=?, dir=? WHERE path=?",
                    (src, os.path.basename(src), os.path.dirname(src), dst))


def db_set_quarantined(paths, flag):
    """Mark/unmark track rows (by CURRENT db path) as quarantined. A
    quarantined row is a dupe loser that a dupes_only execute relocated to
    _Duplicates: identify/resume and later organize plans skip it, and undo
    of that execute clears the flag again. Rows persist (never deleted), so
    restore_state rebuilds the same picture after a restart."""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return
    with _db() as con:
        for p in paths:
            con.execute("UPDATE tracks SET quarantined=? WHERE path=?",
                        (1 if flag else 0, p))


# =================================================================== config

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(acoustid_key=None, discogs_token=None, lastfm_key=None):
    """Persist enrichment credentials. None leaves a field unchanged,
    '' clears it, any other value sets it. Values are never logged."""
    cfg = load_config()
    if acoustid_key is not None:
        cfg["acoustidKey"] = acoustid_key.strip()
    if discogs_token is not None:
        cfg["discogsToken"] = discogs_token.strip()
    if lastfm_key is not None:
        cfg["lastfmKey"] = lastfm_key.strip()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG_PATH)
    return True


def mask_secret(s):
    """'abcd1234ef567890abcd1234ef567890' -> 'abcd…7890'."""
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "…"
    return s[:4] + "…" + s[-4:]


def find_fpcalc():
    """fpcalc path when fingerprinting is available, else None."""
    if FPCALC == "":
        return None
    if FPCALC:
        return FPCALC if os.path.isfile(FPCALC) else None
    w = shutil.which("fpcalc")
    if w:
        return w
    p = os.path.join(BASE_DIR, "fpcalc.exe")
    return p if os.path.isfile(p) else None


def get_config_public():
    """Config for the UI: masked display values only, never raw secrets."""
    cfg = load_config()
    a = cfg.get("acoustidKey") or ""
    d = cfg.get("discogsToken") or ""
    l = cfg.get("lastfmKey") or ""
    fp = find_fpcalc()
    return {"hasAcoustidKey": bool(a), "acoustidKeyMasked": mask_secret(a),
            "hasDiscogsToken": bool(d), "discogsTokenMasked": mask_secret(d),
            "hasLastfmKey": bool(l), "lastfmKeyMasked": mask_secret(l),
            "fingerprintAvailable": bool(fp), "fpcalcPath": fp or ""}


# =================================================================== remote

def _safe(fn, *args):
    """Call a music_remote function; any failure (or missing module)
    becomes None so scans never die on network trouble."""
    if music_remote is None or fn is None:
        return None
    try:
        return fn(*args)
    except Exception:
        return None


def _remote(name):
    return getattr(music_remote, name, None) if music_remote else None


# =================================================================== scan

def collect_files(root, max_files):
    """All audio + companion files under root, sorted, capped at max_files
    audio files (companions never count against the cap)."""
    audio, extras = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            path = os.path.join(dirpath, fn)
            if ext in AUDIO_EXTS:
                audio.append(path)
                if max_files and len(audio) >= max_files:
                    return audio, extras
            elif ext in COMPANION_EXTS:
                extras.append(path)
    return audio, extras


def new_rec(path, st):
    return {"path": path, "name": os.path.basename(path),
            "ext": os.path.splitext(path)[1].lower(),
            "size": st.st_size, "mtime": st.st_mtime,
            "md5": None, "payload_md5": None,
            "codec": None, "duration_s": None, "bitrate_kbps": None,
            "vbr": False, "samplerate": None, "channels": None,
            "artist": None, "albumartist": None, "album": None,
            "title": None, "trackno": None, "tracktotal": None,
            "discno": None, "disctotal": None,
            "year": None, "genre": None, "subgenre": None,
            "compilation": False, "has_art": False,
            "quality_score": None, "mb_recording_id": None,
            "mb_release_id": None, "acoustid": None, "fingerprint": None,
            "cluster_id": None, "keep": 1, "keep_reason": "only copy",
            "error": None, "companions": [], "quarantined": 0}


def apply_tags(rec, tags):
    tags = tags or {}
    rec["artist"] = _s(tags.get("artist"))
    rec["albumartist"] = _s(tags.get("albumartist"))
    rec["album"] = _s(tags.get("album"))
    rec["title"] = _s(tags.get("title"))
    rec["trackno"] = _to_int(tags.get("trackno"))
    rec["tracktotal"] = _to_int(tags.get("tracktotal"))
    rec["discno"] = _to_int(tags.get("discno"))
    rec["disctotal"] = _to_int(tags.get("disctotal"))
    rec["year"] = _to_int(tags.get("year"))
    rec["compilation"] = bool(tags.get("compilation"))
    rec["has_art"] = bool(tags.get("has_art"))


def apply_tech(rec, tech):
    tech = tech or {}
    rec["codec"] = _s(tech.get("codec"))
    if rec["codec"]:
        rec["codec"] = rec["codec"].lower()
    dur = tech.get("duration_s")
    try:
        rec["duration_s"] = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        rec["duration_s"] = None
    rec["bitrate_kbps"] = _to_int(tech.get("bitrate_kbps"))
    rec["vbr"] = bool(tech.get("vbr"))
    rec["samplerate"] = _to_int(tech.get("samplerate"))
    rec["channels"] = _to_int(tech.get("channels"))


def attach_companions(recs, extras):
    """Sidecars/art matching a track basename ride with that track;
    everything else (folder.jpg, album.cue, ...) is returned per-directory
    as album-level companions."""
    stems = {}
    for rec in recs:
        key = (normcase_abs(os.path.dirname(rec["path"])),
               os.path.splitext(rec["name"])[0].lower())
        stems[key] = rec
    unmatched = {}
    for path in extras:
        key = (normcase_abs(os.path.dirname(path)),
               os.path.splitext(os.path.basename(path))[0].lower())
        rec = stems.get(key)
        if rec is not None:
            rec["companions"].append({"from": path, "keepName": False})
        else:
            unmatched.setdefault(normcase_abs(os.path.dirname(path)),
                                 []).append(path)
    return unmatched


def attach_album_companions(recs, clusters, unmatched):
    """Album-level art/sidecars ride with the cluster representative
    (kept, lowest disc/track) so folder.jpg lands in the album folder."""
    by_dir = {}
    for cid, members in clusters.items():
        if not members:
            continue
        kept = [m for m in members if m.get("keep", 1)] or members
        rep = min(kept, key=lambda m: (m.get("discno") or 1,
                                       m.get("trackno") or 9999,
                                       m["path"].lower()))
        d = normcase_abs(os.path.dirname(rep["path"]))
        by_dir.setdefault(d, rep)
    for d, paths in unmatched.items():
        rep = by_dir.get(d)
        if rep is None:
            # a directory with no audio cluster rep: attach to any track
            # in that directory (e.g. unidentified loose files)
            cands = [r for r in recs
                     if normcase_abs(os.path.dirname(r["path"])) == d]
            if not cands:
                continue
            rep = min(cands, key=lambda r: r["path"].lower())
        for p in paths:
            rep["companions"].append({"from": p, "keepName": True})


# =================================================================== cluster

_DISC_DIR = re.compile(r"^(?:disc|disk|cd)\s*\d+$", re.I)


def _cluster_dir(path):
    """Album directory of a track: a bare 'Disc N'/'CD N' subfolder belongs
    to its parent, so multi-disc albums laid out as Disc 1\\/Disc 2\\ form
    ONE cluster."""
    d = os.path.dirname(path)
    if _DISC_DIR.match(os.path.basename(d) or ""):
        d = os.path.dirname(d)
    return normcase_abs(d)


def cluster_recs(recs):
    """Album clusters keyed (directory, normalized album); loose files of
    one directory form a singles cluster. Returns {cluster_id: [recs]} and
    stamps rec['cluster_id']."""
    buckets = {}
    for rec in recs:
        d = _cluster_dir(rec["path"])
        buckets.setdefault((d, normalize(rec.get("album") or "")), []).append(rec)
    clusters = {}
    for i, key in enumerate(sorted(buckets), 1):
        cid = f"C{i:03d}"
        clusters[cid] = buckets[key]
        for rec in buckets[key]:
            rec["cluster_id"] = cid
    return clusters


def _majority(values):
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def va_heuristic(members):
    """>=4 tracks, >=3 distinct primary artists, no artist on >50% of
    tracks. feat.-artists are stripped Picard-style first, so a
    single-artist album with many '(feat. ...)' tracks is NOT a VA set."""
    total = len(members)
    if total < 4:
        return False
    mains = [normalize(main_artist(m.get("artist"))) for m in members]
    mains = [a for a in mains if a]
    if len(set(mains)) < 3:
        return False
    top = Counter(mains).most_common(1)[0][1]
    return top <= total / 2.0


# =================================================================== identify

def _mb_year(detail):
    m = re.match(r"\s*(\d{4})", str(detail.get("date") or ""))
    return int(m.group(1)) if m else None


def _mb_is_compilation(detail):
    secs = detail.get("secondary_types") or []
    if any(str(s).lower() == "compilation" for s in secs):
        return True
    return bool(detail.get("is_va"))


def _recording_fallback(members, cfg):
    """No release hit: correct spellings via Last.fm (when keyed), then a
    recording search; adopt the top recording's release when offered.
    Returns (updates:dict, compilation_mb:bool)."""
    key = (cfg.get("lastfmKey") or "").strip()
    for m in members[:3]:
        a = m.get("artist") or m.get("albumartist")
        t = m.get("title")
        if not (a and t):
            continue
        if SCAN_CANCEL.is_set():
            raise InterruptedError("cancelled")
        qa, qt = a, strip_feat(t) or t
        if key:
            corr = _safe(_remote("lastfm_correction"), qa, qt, key)
            if isinstance(corr, dict):
                qa = _s(corr.get("artist")) or qa
                qt = _s(corr.get("title")) or qt
        hits = _safe(_remote("mb_search_recording"), qa, qt) or []
        if not hits or not isinstance(hits[0], dict):
            continue
        h = hits[0]
        if h.get("mbid"):
            m["mb_recording_id"] = h["mbid"]
        updates = {"source": "recording", "confidence": 0.6}
        rels = h.get("releases") or []
        mbid = rels[0].get("mbid") if rels and isinstance(rels[0], dict) \
            else None
        if mbid:
            detail = _safe(_remote("mb_get_release"), mbid)
            if detail:
                updates.update({
                    "mb_release_id": detail.get("mbid") or mbid,
                    "mb_release_group_id": detail.get("release_group_mbid"),
                    "albumartist": _s(detail.get("artist")),
                    "album": _s(detail.get("title")),
                    "year": _mb_year(detail),
                    "label": _s(detail.get("label")),
                    "catno": _s(detail.get("catno")),
                })
                return updates, _mb_is_compilation(detail)
        return updates, False
    return None, False


def identify_cluster(cid, members, cfg):
    """One metadata lookup per album cluster: tag-first MusicBrainz release
    search, recording-search fallback (+Last.fm correction), Discogs genre
    enrichment. Falls back to tag-derived identity when offline. Never
    raises."""
    rel = {"cluster_id": cid, "mb_release_id": None,
           "mb_release_group_id": None, "albumartist": None, "album": None,
           "year": None, "is_compilation": False, "genre": None,
           "subgenre": None, "label": None, "catno": None, "art_path": None,
           "source": "none", "confidence": 0.0}
    tag_albumartist = _majority(m.get("albumartist") for m in members)
    tag_artist = _majority(m.get("artist") for m in members)
    tag_album = _majority(m.get("album") for m in members)
    tag_year = _majority(m.get("year") for m in members)
    search_artist = tag_albumartist or tag_artist
    comp_mb = False
    detail = None

    if tag_album:
        hits = _safe(_remote("mb_search_release"), search_artist, tag_album) \
            or []
        for hit in hits[:3]:
            if SCAN_CANCEL.is_set():
                raise InterruptedError("cancelled")
            if not isinstance(hit, dict):
                continue
            mbid = hit.get("mbid") or hit.get("id")
            if not mbid:
                continue
            detail = _safe(_remote("mb_get_release"), mbid)
            if not detail:
                continue
            rel.update({
                "mb_release_id": detail.get("mbid") or mbid,
                "mb_release_group_id": detail.get("release_group_mbid"),
                "albumartist": _s(detail.get("artist")),
                "album": _s(detail.get("title")),
                "year": _mb_year(detail),
                "label": _s(detail.get("label")),
                "catno": _s(detail.get("catno")),
                "source": "musicbrainz",
                "confidence": round((hit.get("score") or 90) / 100.0, 3)
                if isinstance(hit.get("score") or 90, (int, float)) else 0.9,
            })
            comp_mb = _mb_is_compilation(detail)
            # canonical per-track recording ids (match by disc/track)
            by_pos = {(t.get("disc") or 1, t.get("track")): t
                      for t in detail.get("tracks") or []
                      if isinstance(t, dict)}
            for m in members:
                t = by_pos.get((m.get("discno") or 1, m.get("trackno")))
                if t and t.get("recording_mbid"):
                    m["mb_recording_id"] = t["recording_mbid"]
            break
        if detail is None:
            try:
                updates, comp_mb = _recording_fallback(members, cfg)
            except InterruptedError:
                raise
            except Exception:
                updates, comp_mb = None, False
            if updates:
                rel.update(updates)

    # --- compilation resolution: tag > VA albumartist > MusicBrainz >
    #     track-artist heuristic ---
    comp = False
    if any(m.get("compilation") for m in members):
        comp = True
    elif normalize(tag_albumartist or "") == "various artists":
        comp = True
    elif comp_mb:
        comp = True
    elif va_heuristic(members):
        comp = True
    rel["is_compilation"] = comp

    # --- genre enrichment (sparse; Discogs genres+styles when tokened) ---
    genre = subgenre = None
    token = (cfg.get("discogsToken") or "").strip()
    if token and tag_album:
        if SCAN_CANCEL.is_set():
            raise InterruptedError("cancelled")
        dg = _safe(_remote("discogs_search_release"),
                   rel["albumartist"] or search_artist,
                   rel["album"] or tag_album, token)
        if isinstance(dg, dict):
            genres = [g for g in (dg.get("genres") or []) if g]
            styles = [s for s in (dg.get("styles") or []) if s]
            if genres:
                genre = genres[0]
                subgenre = styles[0] if styles else \
                    (genres[1] if len(genres) > 1 else "General")
    rel["genre"] = genre or "Unclassified"
    rel["subgenre"] = subgenre or "General"

    # --- canonical fields back onto the member tracks ---
    canon_aa = rel["albumartist"] or tag_albumartist or tag_artist
    canon_album = rel["album"] or tag_album
    canon_year = rel["year"] or tag_year
    for m in members:
        if canon_aa:
            m["albumartist"] = canon_aa
        if canon_album:
            m["album"] = canon_album
        m["year"] = canon_year
        m["genre"] = rel["genre"]
        m["subgenre"] = rel["subgenre"]
        m["compilation"] = comp
        m["mb_release_id"] = rel["mb_release_id"]
    return rel


# Consecutive cluster lookups that attempted MusicBrainz but produced no
# identification before the phase auto-pauses (network down would otherwise
# burn hours at ~1 req/s against a dead connection). A successful hit (or a
# no-tag singles cluster that never attempts a lookup) resets/holds the
# streak, so genuinely obscure albums don't false-trigger as easily.
NET_FAIL_LIMIT = 10

_IDENTIFIED_SOURCES = ("musicbrainz", "recording")


def _cluster_label(members):
    return _majority(m.get("album") for m in members) \
        or os.path.basename(os.path.dirname(members[0]["path"]))


def _identify_loop(items, cfg):
    """Identify each (cid, members), in order, with phase progress, cancel
    polling, per-cluster error containment + immediate persistence, and
    failure-streak auto-pause. Returns (done, cancelled, paused): done counts
    processed clusters; paused means the network looks dead and the
    remaining clusters were left pending (no releases rows) for resume."""
    done = 0
    streak = 0
    total = len(items)
    for idx, (cid, members) in enumerate(items):
        if SCAN_CANCEL.is_set():
            return done, True, False
        set_scan(currentFile=_cluster_label(members), phase="identify")
        attempted = bool(_majority(m.get("album") for m in members))
        rel = None
        try:
            rel = identify_cluster(cid, members, cfg)
        except InterruptedError:
            return done, True, False
        except Exception:
            rel = None     # one bad cluster can never stall the phase;
                           # no row written -> resume will retry it
        done += 1
        if rel is not None:
            with LOCK:
                STATE["releases"][cid] = rel
            try:
                db_upsert_release(rel)
            except Exception:
                pass
            if rel.get("source") in _IDENTIFIED_SOURCES:
                streak = 0
            elif attempted:
                streak += 1
        elif attempted:
            streak += 1
        set_scan(phaseDone=done)
        if streak > NET_FAIL_LIMIT and total - idx - 1 > 0:
            return done, False, True
    return done, False, False


def _pause_note(remaining):
    return ("network unreachable — %d clusters pending, Resume when online"
            % remaining)


# =================================================================== dedupe

def dedupe_recs(recs):
    """Union-find over the auto stages; stage 5 (fuzzy artist+title,
    duration +/-2s) fills the review bucket only and never auto-dedupes.
    Auto stages: 1 full-file md5, 2 payload md5, 3 fingerprint near-equal,
    4 tag identity, 4b obvious filename dupes (same directory + same
    normalized stem, extension ignored, NO duration guard). Returns
    (groups, review): path -> Gxx / Rxx."""
    n = len(recs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def dur_ok(a, b, tol):
        da, db = recs[a].get("duration_s"), recs[b].get("duration_s")
        if da is None or db is None:
            return True
        return abs(da - db) <= tol

    def bucket_union(keyfn, tol=None):
        by = {}
        for i, r in enumerate(recs):
            k = keyfn(r)
            if not k:
                continue
            lst = by.setdefault(k, [])
            for j in lst:
                if tol is None or dur_ok(i, j, tol):
                    union(i, j)
                    break
            lst.append(i)

    # stage 1: full-file md5 (skipped entirely for files >= 2GB at scan)
    bucket_union(lambda r: r.get("md5"))
    # stage 2: payload md5 (tag-stripped audio bytes)
    bucket_union(lambda r: r.get("payload_md5"))
    # stage 3: fingerprint near-equal (+/-1.5s); only when fpcalc ran
    bucket_union(lambda r: r.get("fingerprint"), tol=1.5)

    # stage 4: tag identity albumartist+album+disc+track+title, dur +/-2s
    def k4(r):
        aa = normalize(r.get("albumartist") or r.get("artist"))
        al = normalize(r.get("album"))
        ti = normalize(strip_feat(r.get("title")))
        tn = r.get("trackno")
        if not (aa and al and ti and tn):
            return None
        return (aa, al, r.get("discno") or 1, tn, ti)

    bucket_union(k4, tol=2.0)

    # stage 4b ("obvious dupes"): same directory + same normalized stem,
    # extension ignored, NO duration guard -- 'Song.flac' + 'Song.mp3'
    # (keep the FLAC per the quality ladder), 'Name.mp3' + 'Name (1).mp3',
    # 'Copy of Name.mp3', 'Name - Copy.mp3'. Copy-suffixed clones of the
    # SAME bytes already grouped at stage 1; this stage also catches the
    # re-encoded / retagged / rewrapped ones the user explicitly wants
    # flagged before any identification time is spent. The raw directory
    # is used (Disc-N collapsing does NOT apply), so the same stem in two
    # different folders never matches here.
    def k4b(r):
        stem = normalize_stem(r.get("name"))
        if not stem:
            return None
        return (normcase_abs(os.path.dirname(r["path"])), stem)

    bucket_union(k4b)

    # stage 5: fuzzy artist+title, dur +/-2s -> REVIEW ONLY, never auto
    def k5(r):
        a = normalize(main_artist(r.get("artist") or r.get("albumartist")))
        t = normalize(strip_feat(r.get("title")))
        return (a, t) if a and t else None

    rparent = list(range(n))

    def rfind(x):
        while rparent[x] != x:
            rparent[x] = rparent[rparent[x]]
            x = rparent[x]
        return x

    by = {}
    for i, r in enumerate(recs):
        k = k5(r)
        if not k:
            continue
        lst = by.setdefault(k, [])
        for j in lst:
            if dur_ok(i, j, 2.0) and find(i) != find(j):
                ra, rb = rfind(i), rfind(j)
                if ra != rb:
                    rparent[rb] = ra
                break
        lst.append(i)

    def assign(par, findfn, prefix, split_check=None):
        clusters = {}
        for i in range(n):
            clusters.setdefault(findfn(i), []).append(i)
        out = {}
        groups = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            if split_check is not None and not split_check(members):
                continue
            groups.append(sorted(members,
                                 key=lambda i: recs[i]["path"].lower()))
        groups.sort(key=lambda ms: recs[ms[0]]["path"].lower())
        for gi, ms in enumerate(groups, 1):
            gid = f"{prefix}{gi:02d}"
            for i in ms:
                out[recs[i]["path"]] = gid
        return out

    groups = assign(parent, find, "G")
    # review clusters must span >=2 stage-1..4 roots (already-grouped
    # pairs add nothing)
    review = assign(rparent, rfind, "R",
                    split_check=lambda ms: len({find(i) for i in ms}) >= 2)
    return groups, review


def rank_groups(recs, groups):
    """Best copy per dupe group wins keep=1 + keep_reason; losers keep=0.
    Ungrouped tracks stay 'only copy'."""
    members = {}
    for r in recs:
        gid = groups.get(r["path"])
        if gid:
            members.setdefault(gid, []).append(r)
    for gid, ms in members.items():
        ranked = sorted(ms, key=rank_key)
        winner = ranked[0]
        winner["keep"] = 1
        winner["keep_reason"] = \
            f"best quality in {gid}: {quality_desc(winner)}"
        for r in ranked[1:]:
            r["keep"] = 0
            r["keep_reason"] = \
                f"lower quality duplicate of {winner['name']}"
    for r in recs:
        if not groups.get(r["path"]):
            r["keep"] = 1
            r["keep_reason"] = "only copy"


_PROPAGATE_FIELDS = ("albumartist", "album", "year", "genre", "subgenre",
                     "compilation", "mb_release_id", "mb_recording_id")


def propagate_dupe_canonicals(recs, groups):
    """Dupe losers are never looked up (identification is keeper-only), so
    hand each loser its group winner's canonical identity: a byte/payload/
    stem duplicate IS the same recording, and results/planning stay
    consistent for it. Only non-empty winner values propagate; keep flags
    and keep_reason are never touched."""
    by_gid = {}
    for r in recs:
        gid = groups.get(r["path"])
        if gid:
            by_gid.setdefault(gid, []).append(r)
    for ms in by_gid.values():
        winners = [m for m in ms if m.get("keep", 1)]
        if not winners:
            continue
        w = winners[0]
        for m in ms:
            if m.get("keep", 1):
                continue
            for f in _PROPAGATE_FIELDS:
                v = w.get(f)
                if v is not None and v != "":
                    m[f] = v


# =================================================================== scan run

def fingerprint_file(fpcalc, path):
    """fpcalc -json -> (fingerprint, duration_s); never raises."""
    try:
        out = subprocess.run([fpcalc, "-json", path],
                             capture_output=True, timeout=60)
        data = json.loads(out.stdout.decode("utf-8", "replace"))
        return data.get("fingerprint"), data.get("duration")
    except Exception:
        return None, None


def _acoustid_recording_id(res):
    try:
        return res["results"][0]["recordings"][0]["recording_mbid"]
    except (KeyError, IndexError, TypeError):
        return None


def run_scan(root, max_files, hash_enabled, fingerprint_enabled,
             skip_identify=False):
    """Synchronous pipeline; runs on a daemon thread via start_scan.
    skip_identify runs collect -> tags -> dedupe only (no network at all):
    clusters get NO releases rows, which marks them pending for a later
    'Resume identification' pass (run_identify)."""
    scanned_at = datetime.now().isoformat(sep=" ")
    cancelled = False
    paused = False
    try:
        audio, extras = collect_files(root, max_files)
        set_scan(total=len(audio), processed=0, phase="scan")
        _phase_start("scan", len(audio))
        recs = []
        for i, path in enumerate(audio):
            if SCAN_CANCEL.is_set():
                cancelled = True
                break
            name = os.path.basename(path)
            set_scan(processed=i, currentFile=name, phase="scan",
                     phaseDone=i)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rec = new_rec(path, st)
            if music_tags is None:
                rec["error"] = "music_tags module unavailable"
            else:
                try:
                    apply_tags(rec, music_tags.read_tags(path))
                except Exception as e:
                    rec["error"] = f"tags: {type(e).__name__}: {e}"
                try:
                    apply_tech(rec, music_tags.tech_info(path))
                except Exception as e:
                    rec["error"] = rec["error"] or \
                        f"tech: {type(e).__name__}: {e}"
            rec["quality_score"] = quality_score(
                rec.get("codec"), rec.get("bitrate_kbps"), rec.get("vbr"))
            if hash_enabled:
                if rec["size"] < HASH_MAX:
                    rec["md5"] = md5_file(path, SCAN_CANCEL)
                    if rec["md5"] is None and SCAN_CANCEL.is_set():
                        recs.append(rec)
                        cancelled = True
                        break
                if music_tags is not None:
                    try:
                        rec["payload_md5"] = music_tags.payload_md5(path)
                    except Exception:
                        rec["payload_md5"] = None
            recs.append(rec)
        if SCAN_CANCEL.is_set():
            cancelled = True

        groups, review, releases, clusters = {}, {}, {}, {}
        unmatched = {}
        if not cancelled:
            unmatched = attach_companions(recs, extras)
            clusters = cluster_recs(recs)
            cfg = load_config()
            with LOCK:
                STATE["releases"] = releases   # _identify_loop fills it live
            if skip_identify:
                # fast lane: collect -> tags -> dedupe only, zero network.
                # No releases rows -> every cluster is pending for a later
                # 'Resume identification' run.
                _phase_indeterminate("")
            else:
                _phase_start("identify", len(clusters))
                done, cancelled, paused = _identify_loop(
                    sorted(clusters.items()), cfg)

            # optional fingerprint / AcoustID pass (needs fpcalc + key);
            # skipped in the fast lane: AcoustID is online identification.
            if fingerprint_enabled and not cancelled and not skip_identify:
                fpcalc = find_fpcalc()
                if fpcalc:
                    key = (cfg.get("acoustidKey") or "").strip()
                    _phase_start("fingerprint", len(recs))
                    for fi, r in enumerate(recs):
                        if SCAN_CANCEL.is_set():
                            cancelled = True
                            break
                        set_scan(currentFile=r["name"], phase="fingerprint",
                                 phaseDone=fi)
                        fp, dur = fingerprint_file(fpcalc, r["path"])
                        if fp:
                            r["fingerprint"] = fp
                        if dur and not r.get("duration_s"):
                            try:
                                r["duration_s"] = float(dur)
                            except (TypeError, ValueError):
                                pass
                        if fp and key:
                            res = _safe(_remote("acoustid_lookup"), fp,
                                        dur or r.get("duration_s"), key)
                            if isinstance(res, dict):
                                r["acoustid"] = _s(
                                    (res.get("results") or [{}])[0].get(
                                        "acoustid") if res.get("results")
                                    else None)
                                rid = _acoustid_recording_id(res)
                                if rid and not r.get("mb_recording_id"):
                                    r["mb_recording_id"] = rid
                    set_scan(phaseDone=len(recs))

            if not cancelled:
                _phase_indeterminate("dedupe")
                set_scan(currentFile="")
                groups, review = dedupe_recs(recs)
                rank_groups(recs, groups)
                attach_album_companions(recs, clusters, unmatched)

        for r in recs:      # genre fallback must hold even on cancel
            if not r.get("genre"):
                r["genre"], r["subgenre"] = "Unclassified", "General"

        with LOCK:
            STATE["recs"] = recs
            STATE["releases"] = releases
            STATE["groups"] = groups
            STATE["review"] = review
            STATE["scannedRoot"] = root
            STATE["plan"] = None
            STATE["partialScan"] = cancelled
        try:
            if cancelled:
                # NEVER let a cancelled scan replace the last COMPLETE scan on
                # disk. This used to run unconditionally, so cancelling (or
                # restarting mid-scan) silently traded a finished 92k-track
                # index for the few hundred rows collected so far. The partial
                # set still lives in STATE for this session; the DB keeps the
                # last good scan so a restart restores something useful.
                set_meta({"last_scan_cancelled_at": scanned_at,
                          "last_scan_cancelled_count": str(len(recs))})
            else:
                db_replace_state(recs, groups, review, releases, scanned_at)
                set_meta({"last_scan_root": root,
                          "last_scan_completed_at": scanned_at,
                          "last_scan_count": str(len(recs)),
                          "last_scan_partial": "0"})
        except Exception:
            pass
        if cancelled:
            set_scan(state="cancelled", processed=len(recs),
                     currentFile="", phase="", note=None)
        else:
            note = None
            if paused:
                note = _pause_note(len(pending_identify()))
            elif skip_identify:
                note = ("identification skipped — %d clusters pending, "
                        "Resume identification when online"
                        % len(pending_identify()))
            set_scan(state="done", processed=len(audio),
                     currentFile="", phase="", note=note)
    except Exception as e:
        set_scan(state="error", error=f"{type(e).__name__}: {e}", phase="")


def pending_identify():
    """cluster_id -> member recs for clusters with NO releases row yet
    (skip-identify scans, auto-paused/cancelled/killed identifies). This is
    the resume target set; rows persist per cluster, so restarts keep it."""
    with LOCK:
        recs = list(STATE["recs"])
        releases = STATE["releases"]
    out = {}
    for r in recs:
        if r.get("quarantined"):
            continue            # first-pass dupe losers stay hidden
        cid = r.get("cluster_id")
        if cid and cid not in releases:
            out.setdefault(cid, []).append(r)
    return out


def run_identify():
    """Identify-only worker: fill releases rows for pending clusters (and
    their canonical track fields) without rescanning the filesystem. Runs
    on a daemon thread via start_identify; reuses scan status + cancel."""
    cfg = load_config()
    try:
        items = sorted(pending_identify().items())
        _phase_start("identify", len(items))
        set_scan(total=len(items), processed=0, currentFile="")
        done, cancelled, paused = _identify_loop(items, cfg)
        with LOCK:
            recs = STATE["recs"]
            groups = STATE["groups"]
            review = STATE["review"]
            releases = STATE["releases"]
        for r in recs:      # genre fallback must hold even on cancel
            if not r.get("genre"):
                r["genre"], r["subgenre"] = "Unclassified", "General"
        try:
            db_replace_state(recs, groups, review, releases,
                             datetime.now().isoformat(sep=" "))
            set_meta({"last_identify_at": datetime.now().isoformat(sep=" ")})
        except Exception:
            pass
        if cancelled:
            set_scan(state="cancelled", processed=done, currentFile="",
                     phase="", note=None)
        elif paused:
            set_scan(state="done", processed=done, currentFile="", phase="",
                     note=_pause_note(len(items) - done))
        else:
            set_scan(state="done", processed=done, currentFile="", phase="",
                     note=None)
    except Exception as e:
        set_scan(state="error", error=f"{type(e).__name__}: {e}", phase="")


def start_scan(root, max_files, hash_enabled, fingerprint_enabled=False,
               skip_identify=False):
    with LOCK:
        if STATE["scan"]["state"] == "running":
            return False, "A music scan is already running."
        STATE["scan"] = {"state": "running", "total": 0, "processed": 0,
                         "currentFile": "", "phase": "", "error": None,
                         "phaseDone": 0, "phaseTotal": 0,
                         "phaseStartedAt": None, "note": None}
        STATE["partialScan"] = False
    SCAN_CANCEL.clear()
    t = threading.Thread(target=run_scan,
                         args=(root, max_files, hash_enabled,
                               fingerprint_enabled, skip_identify),
                         daemon=True)
    t.start()
    return True, None


def start_identify():
    """Launch JUST the identify phase for clusters lacking a releases row."""
    with LOCK:
        if STATE["scan"]["state"] == "running":
            return False, "A music scan is already running."
        has_recs = bool(STATE["recs"])
    if not has_recs and not ensure_state():
        return False, "No music scan results. Run a scan first."
    if not pending_identify():
        return False, "All album clusters are already identified."
    with LOCK:
        STATE["scan"] = {"state": "running", "total": 0, "processed": 0,
                         "currentFile": "", "phase": "", "error": None,
                         "phaseDone": 0, "phaseTotal": 0,
                         "phaseStartedAt": None, "note": None}
    SCAN_CANCEL.clear()
    t = threading.Thread(target=run_identify, daemon=True)
    t.start()
    return True, None


def cancel_scan():
    with LOCK:
        running = STATE["scan"]["state"] == "running"
    if not running:
        return False
    SCAN_CANCEL.set()
    return True


# =================================================================== results

def ensure_state():
    with LOCK:
        empty = not STATE["recs"]
    if empty:
        restore_state()
    with LOCK:
        return bool(STATE["recs"])


def restore_state():
    """Rebuild STATE from music.db after a restart (companions are
    re-derived on the next scan, like cinema)."""
    try:
        with _db() as con:
            rows = con.execute("SELECT * FROM tracks ORDER BY path").fetchall()
            rels = con.execute("SELECT * FROM releases").fetchall()
        if not rows:
            return False
        meta = get_meta()
    except sqlite3.Error:
        return False
    recs, groups, review, releases = [], {}, {}, {}
    for row in rows:
        rec = {"path": row["path"], "name": row["filename"],
               "ext": row["ext"], "size": row["size_bytes"],
               "mtime": row["mtime"], "md5": row["md5"],
               "payload_md5": row["payload_md5"], "codec": row["codec"],
               "duration_s": row["duration_s"],
               "bitrate_kbps": row["bitrate_kbps"], "vbr": bool(row["vbr"]),
               "samplerate": row["samplerate"], "channels": row["channels"],
               "artist": row["artist"], "albumartist": row["albumartist"],
               "album": row["album"], "title": row["title"],
               "trackno": row["trackno"], "tracktotal": row["tracktotal"],
               "discno": row["discno"], "disctotal": row["disctotal"],
               "year": row["year"], "genre": row["genre"],
               "subgenre": row["subgenre"],
               "compilation": bool(row["compilation"]),
               "has_art": bool(row["has_art"]),
               "quality_score": row["quality_score"],
               "mb_recording_id": row["mb_recording_id"],
               "mb_release_id": row["mb_release_id"],
               "acoustid": row["acoustid"], "fingerprint": row["fingerprint"],
               "cluster_id": row["cluster_id"],
               "keep": 1 if row["keep"] else 0,
               "keep_reason": row["keep_reason"], "error": row["error"],
               "quarantined": 1 if row["quarantined"] else 0,
               "companions": []}
        recs.append(rec)
        if row["dupe_group"]:
            groups[row["path"]] = row["dupe_group"]
        if row["review_group"]:
            review[row["path"]] = row["review_group"]
    for row in rels:
        releases[row["cluster_id"]] = {
            "cluster_id": row["cluster_id"],
            "mb_release_id": row["mb_release_id"],
            "mb_release_group_id": row["mb_release_group_id"],
            "albumartist": row["albumartist"], "album": row["album"],
            "year": row["year"], "is_compilation": bool(row["is_compilation"]),
            "genre": row["genre"], "subgenre": row["subgenre"],
            "label": row["label"], "catno": row["catno"],
            "art_path": row["art_path"], "source": row["source"],
            "confidence": row["confidence"]}
    with LOCK:
        STATE["recs"] = recs
        STATE["releases"] = releases
        STATE["groups"] = groups
        STATE["review"] = review
        STATE["scannedRoot"] = meta.get("last_scan_root")
        STATE["partialScan"] = meta.get("last_scan_partial") == "1"
        STATE["scan"] = {"state": "done", "total": len(recs),
                         "processed": len(recs), "currentFile": "",
                         "phase": "", "error": None,
                         "phaseDone": 0, "phaseTotal": 0,
                         "phaseStartedAt": None, "note": None}
    return True


def rec_kind(r):
    """'album' track, loose 'single', or 'unidentified' (no usable tags)."""
    if not (r.get("title") and
            (r.get("album") or r.get("artist") or r.get("albumartist"))):
        return "unidentified"
    if not r.get("album"):
        return "single"
    return "album"


def _q_bucket(r):
    s = r.get("quality_score") or 0
    if s >= 5000:
        return "Lossless"
    if s >= 2600:
        return "High"
    if s >= 1800:
        return "Medium"
    if s > 0:
        return "Low"
    return "Unknown"


def build_results():
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
        review = dict(STATE["review"])
        root = STATE["scannedRoot"]
        partial = STATE["partialScan"]
        rel_keys = set(STATE["releases"])
        note = STATE["scan"].get("note")
    by_kind = Counter(rec_kind(r) for r in recs)
    by_codec = Counter((r.get("codec") or "unknown")
                       for r in recs)
    genres = Counter(r.get("genre") for r in recs if r.get("genre"))
    identified_n = sum(1 for r in recs if rec_kind(r) != "unidentified")
    genre_ok = sum(1 for r in recs
                   if r.get("genre") and r["genre"] != "Unclassified")
    gid_members, rid_members = {}, {}
    for p, gid in groups.items():
        gid_members.setdefault(gid, []).append(p)
    for p, rid in review.items():
        rid_members.setdefault(rid, []).append(p)
    # Index by path ONCE. This loop used to rescan every rec for every dupe
    # group -- O(groups x recs), which on a 90k-track library meant hundreds
    # of millions of comparisons and results that never returned.
    by_path = {r["path"]: r for r in recs}
    upgrades = 0
    for gid, paths in gid_members.items():
        members = [by_path[p] for p in paths if p in by_path]
        if not members:
            continue
        best = max((m.get("quality_score") or 0) for m in members)
        upgrades += sum(1 for m in members
                        if not m.get("keep", 1)
                        and (m.get("quality_score") or 0) < best)
    pending_identify = len({r.get("cluster_id") for r in recs
                            if r.get("cluster_id")} - rel_keys)
    dupe_files = sum(len(m) - 1 for m in gid_members.values())
    quarantined_n = sum(1 for r in recs if r.get("quarantined"))
    dupe_bytes = sum((r.get("size") or 0) for r in recs
                     if groups.get(r["path"]) and not r.get("keep", 1))
    # Quick-clean is the "local scan done, before you spend MusicBrainz
    # lookups" moment: duplicates were found, none quarantined yet, and
    # identification is still pending for these clusters.
    quick_clean_eligible = (dupe_files > 0 and quarantined_n == 0
                            and pending_identify > 0)
    return {
        "scannedRoot": root,
        "partial": partial,
        "note": note,
        "pendingIdentify": pending_identify,
        "totalFiles": len(recs),
        "byKind": dict(by_kind),
        "byCodec": dict(by_codec),
        "identified": identified_n,
        "unidentified": by_kind.get("unidentified", 0),
        "singles": by_kind.get("single", 0),
        "compilations": sum(1 for r in recs if r.get("compilation")),
        "albums": len({r.get("cluster_id") for r in recs
                       if r.get("album")}),
        "topGenres": genres.most_common(10),
        "genreCoverage": round(100.0 * genre_ok / len(recs))
        if recs else 0,
        "qualityMix": dict(Counter(_q_bucket(r) for r in recs)),
        "dupeGroups": len(gid_members),
        "dupeFiles": dupe_files,
        "dupeBytes": dupe_bytes,
        "dupesQuarantined": quarantined_n,
        "quickCleanEligible": quick_clean_eligible,
        "upgradesAvailable": upgrades,
        "reviewGroups": len(rid_members),
        "reviewFiles": sum(len(m) for m in rid_members.values()),
        "fingerprintAvailable": bool(find_fpcalc()),
        # Per-track rows are capped: the UI renders only summary stats from
        # this payload, and serializing ~90k tracks produced a 100MB+ body
        # that stalled the browser. "recsTruncated" tells any future consumer
        # the list is partial; totalFiles remains the true count.
        "recs": [{k: r.get(k) for k in
                  ("path", "name", "artist", "albumartist", "album", "title",
                   "trackno", "discno", "year", "genre", "subgenre",
                   "compilation", "codec", "bitrate_kbps", "vbr",
                   "duration_s", "quality_score", "has_art", "cluster_id",
                   "keep", "keep_reason", "size", "error",
                   "mb_recording_id", "mb_release_id")}
                 | {"kind": rec_kind(r),
                    "dupe_group": groups.get(r["path"]),
                    "review_group": review.get(r["path"])}
                 for r in recs[:RESULTS_REC_CAP]],
        "recsTruncated": len(recs) > RESULTS_REC_CAP,
        "groups": {gid: sorted(m) for gid, m in sorted(gid_members.items())},
        "review": {rid: sorted(m) for rid, m in sorted(rid_members.items())},
    }


# =================================================================== plan

def multidisc_clusters(recs):
    """Cluster ids whose album spans >1 disc (TPOS/disctotal in tags)."""
    discs = {}
    for r in recs:
        cid = r.get("cluster_id")
        if not cid:
            continue
        d = discs.setdefault(cid, set())
        d.add(r.get("discno") or 1)
        if (r.get("disctotal") or 0) > 1:
            d.add(2)
    return {cid for cid, ds in discs.items() if len(ds) > 1}


def album_dest(r, target_root, multi_disc, disc_style, layout="genre"):
    """layout="genre": Genre\\Sub-genre\\Artist\\Year - Album\\NN - Title.ext.
    layout="artist": Artist\\Album\\NN - Title.ext (no genre levels, no year
    prefix) — the simple tree for libraries whose genre tags are a mess.
    Compilations use Various Artists + 'NN - Artist - Title.ext'; multi-disc
    albums get 'Disc N' subfolders or a 1NN filename prefix per discStyle."""
    if r.get("compilation"):
        artist_dir = VA_NAME
    else:
        artist_dir = sanitize_component(
            r.get("albumartist") or r.get("artist") or "Unknown Artist")
    album = sanitize_component(r.get("album") or "Unknown Album")
    if layout == "artist":
        album_dir = album
    else:
        album_dir = f"{r['year']} - {album}" if r.get("year") else album
    title = sanitize_component(
        r.get("title") or os.path.splitext(r["name"])[0])
    tn = r.get("trackno")
    disc = r.get("discno") or 1
    if tn and multi_disc and disc_style == "merge":
        nn = f"{disc}{tn:02d}"
    elif tn:
        nn = f"{tn:02d}"
    else:
        nn = ""
    if r.get("compilation"):
        ta = sanitize_component(r.get("artist") or "Unknown Artist")
        fname = f"{nn} - {ta} - {title}" if nn else f"{ta} - {title}"
    else:
        fname = f"{nn} - {title}" if nn else title
    fname += r["ext"]
    if layout == "artist":
        base = os.path.join(target_root, artist_dir, album_dir)
    else:
        g = sanitize_component(r.get("genre") or "Unclassified")
        sg = sanitize_component(r.get("subgenre") or "General")
        base = os.path.join(target_root, g, sg, artist_dir, album_dir)
    if multi_disc and disc_style == "subfolder":
        base = os.path.join(base, f"Disc {disc}")
    return os.path.join(base, fname)


def single_dest(r, target_root):
    """Artist\\_Singles\\Title.ext for loose tracks with no album tag."""
    artist = sanitize_component(
        r.get("artist") or r.get("albumartist") or "Unknown Artist")
    title = sanitize_component(
        r.get("title") or os.path.splitext(r["name"])[0])
    return os.path.join(target_root, artist, "_Singles", title + r["ext"])


def _norm_disc_style(v):
    v = (v or "subfolder").strip().lower()
    if v in ("subfolder", "subfolders", "disc"):
        return "subfolder"
    if v in ("merge", "prefix", "flat"):
        return "merge"
    return None


def _norm_dupe_handling(v):
    v = (v or "quarantine").strip().lower()
    if v == "quarantine":
        return "quarantine"
    if v in ("keep", "skip", "leave"):
        return "keep"
    return None


def compute_plan(params):
    ensure_state()
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
    if not recs or not root:
        return None, "No music scan results. Run a scan first."
    params = params or {}
    action = params.get("action", "move")
    if action not in ("move", "copy"):
        return None, "Invalid action."
    target_root = (params.get("targetRoot")
                   or os.path.join(root, "Organized"))
    target_root = os.path.abspath(target_root)
    if params.get("mode") == "dupes_only":
        # first-pass duplicate cleanup: quarantine losers only, no network.
        # targetRoot may be the scanned root itself (the UI falls back to
        # it), so the target!=root guard below does NOT apply here.
        return _dupes_only_plan(recs, groups, root, target_root, action)
    dupe_handling = _norm_dupe_handling(params.get("dupeHandling"))
    if dupe_handling is None:
        return None, "Invalid dupeHandling."
    disc_style = _norm_disc_style(params.get("discStyle"))
    if disc_style is None:
        return None, "Invalid discStyle."
    # layout "artist" = Artist\Album\NN - Title (no genre levels) — for
    # libraries whose genre tags are all over the place. Default stays
    # "genre" for API back-compat; the UI picks.
    layout = (params.get("layout") or "genre").strip().lower()
    if layout not in ("genre", "artist"):
        return None, "Invalid layout (use 'genre' or 'artist')."
    if normcase_abs(target_root) == normcase_abs(root):
        return None, "Target root must differ from the scanned folder."

    multi = multidisc_clusters(recs)
    entries = []
    counts = Counter()
    for r in sorted(recs, key=lambda x: x["path"].lower()):
        if r.get("quarantined"):
            continue            # first-pass dupe losers stay quarantined
        gid = groups.get(r["path"])
        kind = rec_kind(r)
        entry = {"from": r["path"], "kind": kind, "isDupe": False,
                 "groupId": gid, "reason": None,
                 "clusterId": r.get("cluster_id"),
                 "companions": [{"from": c["from"], "to": None,
                                 "keepName": bool(c.get("keepName"))}
                                for c in (r.get("companions") or [])]}
        if gid and not r.get("keep", 1):
            if dupe_handling == "keep":
                counts["keptDupe"] += 1
                continue            # keep dupes where they are (untouched)
            entry.update(to=os.path.join(target_root, "_Duplicates", gid,
                                         r["name"]),
                         isDupe=True, reason="dupe")
            counts["dupe"] += 1
        elif kind == "unidentified":
            entry.update(to=os.path.join(target_root, "_Unidentified",
                                         r["name"]),
                         reason="unidentified")
            counts["unidentified"] += 1
        elif kind == "single":
            entry["to"] = single_dest(r, target_root)
            counts["single"] += 1
        else:
            entry["to"] = album_dest(r, target_root,
                                     r.get("cluster_id") in multi,
                                     disc_style, layout=layout)
            if r.get("compilation"):
                entry["reason"] = "va"
                counts["va"] += 1
            else:
                counts["album"] += 1
        dest_dir = os.path.dirname(entry["to"])
        stem = os.path.splitext(os.path.basename(entry["to"]))[0]
        for comp in entry["companions"]:
            if comp.get("keepName"):
                comp["to"] = os.path.join(dest_dir,
                                          os.path.basename(comp["from"]))
            else:
                comp["to"] = os.path.join(
                    dest_dir, stem + os.path.splitext(comp["from"])[1])
        entries.append(entry)

    folders = {os.path.dirname(e["to"]) for e in entries}
    stats = {"totalFiles": len(entries),
             "albumFiles": counts["album"], "vaFiles": counts["va"],
             "singleFiles": counts["single"],
             "dupeFiles": counts["dupe"],
             "keptDupeFiles": counts["keptDupe"],
             "unidentifiedFiles": counts["unidentified"],
             "companionFiles": sum(len(e["companions"]) for e in entries),
             "foldersToCreate": len(folders),
             "targetRoot": target_root, "action": action,
             "dupeHandling": dupe_handling, "discStyle": disc_style,
             "layout": layout,
             "scannedRoot": root}
    plan = {"entries": entries, "stats": stats,
            "params": {"action": action, "targetRoot": target_root,
                       "dupeHandling": dupe_handling,
                       "discStyle": disc_style, "scannedRoot": root}}
    with LOCK:
        STATE["plan"] = plan
    return plan, None


# =================================================================== execute

def _backfill_art(entries, manifest, cancel):
    """Album folders with no art anywhere get a Cover Art Archive front
    image as folder.jpg (best effort; entries marked action 'art' so undo
    deletes them)."""
    with LOCK:
        releases = dict(STATE["releases"])
        recs = list(STATE["recs"])
    art_ok = {}
    for r in recs:
        cid = r.get("cluster_id")
        if not cid:
            continue
        ok = art_ok.get(cid, False)
        if r.get("has_art"):
            ok = True
        if any(str(c.get("from", "")).lower().endswith(
                tuple(ART_EXTS)) for c in (r.get("companions") or [])):
            ok = True
        art_ok[cid] = ok
    # pre-collect candidates so the artwork phase reports real progress
    # (this is a slow, throttled network pass, one fetch per album folder)
    done_dirs = set()
    candidates = []
    for e in entries:
        cid = e.get("clusterId")
        if not cid or e.get("isDupe"):
            continue
        rel = releases.get(cid) or {}
        mbid = rel.get("mb_release_id")
        d = os.path.dirname(e["to"])
        if not mbid or art_ok.get(cid) or d in done_dirs:
            continue
        done_dirs.add(d)
        candidates.append((d, mbid))
    set_exec(phase="artwork", phaseDone=0, phaseTotal=len(candidates))
    for i, (d, mbid) in enumerate(candidates):
        if cancel.is_set():
            return
        set_exec(phaseDone=i)
        data = _safe(_remote("cover_art_front"), mbid)
        if not data:
            continue
        try:
            dest = resolve_collision(os.path.join(d, "folder.jpg"),
                                     os.devnull)
            os.makedirs(d, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            manifest.append({"from": None, "to": dest, "action": "art"})
            exec_log(f"ART cover_art_front -> {dest}")
        except Exception as ex:
            exec_log(f"ERROR art {d}: {type(ex).__name__}: {ex}")
    set_exec(phaseDone=len(candidates))


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
    set_exec(total=len(entries), processed=0, phase="files",
             phaseDone=0, phaseTotal=len(entries))
    manifest = []
    moved = copied = skipped = errors = 0
    cancelled = False
    try:
        os.makedirs(target_root, exist_ok=True)
        for i, e in enumerate(entries):
            if EXEC_CANCEL.is_set():
                cancelled = True
                exec_log(f"CANCELLED by user after {i} of {len(entries)} "
                         "files")
                break
            src, dst = e["from"], e["to"]
            set_exec(processed=i, currentFile=os.path.basename(src),
                     phaseDone=i)
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
                else:
                    shutil.copy2(src, actual)
                    copied += 1
                exec_log(f"{action.upper()} {src} -> {actual}")
                manifest.append({"from": src, "to": actual, "action": action})
                for comp in e.get("companions") or []:
                    csrc, cdst = comp["from"], comp["to"]
                    try:
                        if not os.path.isfile(csrc):
                            continue
                        cactual = resolve_collision(cdst, csrc)
                        os.makedirs(os.path.dirname(cactual), exist_ok=True)
                        if action == "move":
                            shutil.move(csrc, cactual)
                        else:
                            shutil.copy2(csrc, cactual)
                        manifest.append({"from": csrc, "to": cactual,
                                         "action": action,
                                         "companion": True})
                        exec_log(f"{action.upper()} companion "
                                 f"{csrc} -> {cactual}")
                    except Exception as cx:
                        exec_log(f"ERROR companion {csrc}: "
                                 f"{type(cx).__name__}: {cx}")
            except Exception as ex:
                errors += 1
                exec_log(f"ERROR {src}: {type(ex).__name__}: {ex}")
        if not cancelled:
            _backfill_art(entries, manifest, EXEC_CANCEL)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        undo_name = f"undo_log_music_{ts}.json"
        undo_path = os.path.join(UNDO_DIR, undo_name)
        payload = {"app": "music", "version": 1,
                   "created": datetime.now().isoformat(sep=" "),
                   "action": action, "scannedRoot": scanned_root,
                   "targetRoot": target_root, "entries": manifest,
                   "stats": {"moved": moved, "copied": copied,
                             "skipped": skipped, "errors": errors,
                             "cancelled": cancelled}}
        os.makedirs(UNDO_DIR, exist_ok=True)
        tmp = undo_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, undo_path)
        undo_copy = None
        try:
            undo_copy = os.path.join(target_root, undo_name)
            shutil.copyfile(undo_path, undo_copy)
        except OSError:
            undo_copy = None
        with LOCK:
            STATE["lastUndo"] = undo_path
        if action == "move":
            try:
                db_update_paths(
                    [e for e in manifest if e.get("from")], "move")
            except Exception:
                pass
        set_exec(state="cancelled" if cancelled else "done",
                 processed=len(manifest), currentFile="", phase="",
                 result={"moved": moved, "copied": copied,
                         "skipped": skipped, "errors": errors,
                         "cancelled": cancelled,
                         "undoFile": undo_path, "undoCopy": undo_copy})
    except Exception as e:
        set_exec(state="error", error=f"{type(e).__name__}: {e}", phase="")


def start_execute():
    with LOCK:
        if STATE["execute"]["state"] == "running":
            return False, "Execution already running."
        if not STATE["plan"]:
            return False, "No plan. Build a plan preview first."
        STATE["execute"] = {"state": "running", "total": 0, "processed": 0,
                            "currentFile": "", "error": None, "log": [],
                            "result": None, "phase": "", "phaseDone": 0,
                            "phaseTotal": 0}
    EXEC_CANCEL.clear()
    t = threading.Thread(target=run_execute, daemon=True)
    t.start()
    return True, None


def cancel_execute():
    with LOCK:
        running = STATE["execute"]["state"] == "running"
    if not running:
        return False
    EXEC_CANCEL.set()
    return True


# =================================================================== undo

def run_undo(manifest_path):
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return None, f"cannot read manifest: {e}"
    restored = deleted = skipped = errors = 0
    for e in reversed(manifest.get("entries", [])):
        src, dst, act = e.get("from"), e.get("to"), e.get("action", "move")
        try:
            if act == "move":
                if not os.path.isfile(dst):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                restored += 1
            else:   # copy + art: drop the created file
                if os.path.isfile(dst):
                    os.remove(dst)
                    deleted += 1
                else:
                    skipped += 1
        except Exception:
            errors += 1
    try:
        db_update_paths([e for e in manifest.get("entries", [])
                         if e.get("from")
                         and e.get("action", "move") == "move"], "restore")
    except Exception:
        pass
    # remove now-empty dirs under the target root (bottom-up, root excluded)
    troot = manifest.get("targetRoot")
    if troot and os.path.isdir(troot):
        try:
            for fn in os.listdir(troot):
                if fn.startswith("undo_log_music_") and fn.endswith(".json"):
                    try:
                        os.remove(os.path.join(troot, fn))
                    except OSError:
                        pass
        except OSError:
            pass
        for dirpath, dirnames, filenames in os.walk(troot, topdown=False):
            if normcase_abs(dirpath) == normcase_abs(troot):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
        try:
            if not os.listdir(troot):
                os.rmdir(troot)
        except OSError:
            pass
    return {"restored": restored, "deleted": deleted, "skipped": skipped,
            "errors": errors}, None


# =================================================================== api

def _norm_path(path):
    p = (path or "").split("?")[0].strip("/")
    for pre in ("api/music/", "music/"):
        if p.startswith(pre):
            p = p[len(pre):]
    return p.strip("/")


# ============================================================== tag write-back

def set_tagfix(**kw):
    with LOCK:
        STATE["tagfix"].update(kw)


def tagfix_status():
    with LOCK:
        return dict(STATE["tagfix"])


def run_tagfix(mode="missing"):
    """Fill missing tag fields in the scanned files from the organizer's
    identified values (canonical after an online identify). Backs up every
    change to a JSON manifest first; verifies the audio payload hash is
    untouched after each write. Runs on a daemon thread."""
    try:
        with LOCK:
            recs = [dict(r) for r in STATE["recs"] if not r.get("quarantined")]
        todo = []
        for r in recs:
            if not os.path.isfile(r["path"]):
                continue
            changes = music_tagfix.plan_changes(r["path"], r, mode)
            if changes:
                todo.append((r["path"], changes))
        set_tagfix(state="running", total=len(todo), processed=0, changed=0,
                   skipped=0, errors=0, currentFile="", error=None,
                   backupFile=None)
        if not todo:
            set_tagfix(state="done")
            return
        os.makedirs(TAG_BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(TAG_BACKUP_DIR, f"tagfix_{ts}.json")
        manifest = {"version": 1, "created": datetime.now().isoformat(sep=" "),
                    "mode": mode, "entries": []}
        # write the full intended manifest BEFORE touching any file
        manifest["entries"] = [{"path": p, "changes": ch, "applied": False}
                               for p, ch in todo]
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        set_tagfix(backupFile=backup_path)
        changed = skipped = errors = 0
        for i, (path, changes) in enumerate(todo):
            if TAGFIX_CANCEL.is_set():
                break
            set_tagfix(processed=i, currentFile=os.path.basename(path))
            before = music_tags.payload_md5(path) if music_tags else None
            err = music_tagfix.apply_changes(path, changes)
            if err:
                errors += 1
                set_tagfix(errors=errors)
                continue
            after = music_tags.payload_md5(path) if music_tags else None
            if before is not None and after != before:
                # audio bytes changed — must never happen; revert immediately
                music_tagfix.revert_changes(path, changes)
                errors += 1
                set_tagfix(errors=errors)
                continue
            manifest["entries"][i]["applied"] = True
            changed += 1
            set_tagfix(changed=changed)
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        set_tagfix(state="cancelled" if TAGFIX_CANCEL.is_set() else "done",
                   processed=len(todo), currentFile="", skipped=skipped)
    except Exception as e:
        set_tagfix(state="error", error=f"{type(e).__name__}: {e}")


def start_tagfix(mode="missing"):
    if music_tagfix is None or not music_tagfix.available():
        return False, ("Tag writing needs the 'mutagen' package "
                       "(pip install mutagen).")
    with LOCK:
        for k in ("scan", "execute", "tagfix"):
            if STATE[k]["state"] == "running":
                return False, f"A {k} is already running."
    TAGFIX_CANCEL.clear()
    threading.Thread(target=run_tagfix, kwargs={"mode": mode},
                     daemon=True).start()
    return True, None


def run_tagfix_undo(backup_file=None):
    """Restore original tag values from a tagfix backup manifest (newest by
    default). Synchronous; returns (result dict, error)."""
    if music_tagfix is None or not music_tagfix.available():
        return None, "Tag writing needs the 'mutagen' package."
    if not backup_file:
        try:
            files = sorted(fn for fn in os.listdir(TAG_BACKUP_DIR)
                           if fn.startswith("tagfix_") and fn.endswith(".json"))
        except OSError:
            files = []
        if not files:
            return None, "No tag-fix backups found."
        backup_file = os.path.join(TAG_BACKUP_DIR, files[-1])
    try:
        with open(backup_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return None, f"Unreadable backup: {e}"
    restored = errors = skipped = 0
    for ent in manifest.get("entries", []):
        if not ent.get("applied"):
            skipped += 1
            continue
        if not os.path.isfile(ent["path"]):
            skipped += 1
            continue
        err = music_tagfix.revert_changes(ent["path"], ent["changes"])
        if err:
            errors += 1
        else:
            restored += 1
    return {"restored": restored, "errors": errors, "skipped": skipped,
            "backupFile": backup_file}, None


def api_get(path, qs):
    """GET /api/music/<path> -> (status, obj). qs is a parse_qs dict."""
    p = _norm_path(path)
    if p == "scan/status":
        return 200, scan_status()
    if p == "execute/status":
        return 200, execute_status()
    if p == "results":
        if not ensure_state():
            return 404, {"error": "No music scan results yet."}
        return 200, build_results()
    if p == "plan":
        with LOCK:
            plan = STATE["plan"]
        if not plan:
            return 404, {"error": "No music plan yet."}
        return 200, plan
    if p == "config":
        return 200, get_config_public()
    if p == "tagfix/status":
        return 200, tagfix_status()
    return 404, {"error": "not found"}


def api_post(path, body):
    """POST /api/music/<path> -> (status, obj)."""
    p = _norm_path(path)
    body = body or {}
    if p == "tagfix":
        mode = (body.get("mode") or "missing").strip().lower()
        if mode not in ("missing", "all"):
            return 400, {"error": "mode must be 'missing' or 'all'"}
        if not ensure_state():
            return 409, {"error": "No scan results — run a scan first."}
        ok, err = start_tagfix(mode)
        if not ok:
            return 409, {"error": err}
        return 200, {"ok": True}
    if p == "tagfix/cancel":
        with LOCK:
            running = STATE["tagfix"]["state"] == "running"
        if not running:
            return 409, {"error": "No tag fix is running."}
        TAGFIX_CANCEL.set()
        return 200, {"ok": True}
    if p == "tagfix/undo":
        res, err = run_tagfix_undo(body.get("backupFile"))
        if err:
            return 409, {"error": err}
        return 200, res
    if p == "scan":
        root = ((body.get("root") or body.get("path")) or "").strip()
        if not root:
            return 400, {"error": "root is required"}
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return 400, {"error": f"Not a directory: {root}"}
        try:
            max_files = int(body.get("maxFiles") or body.get("max") or 0)
        except (TypeError, ValueError):
            max_files = 0
        hash_enabled = bool(body.get("hashEnabled", body.get("hash")))
        fp_enabled = bool(body.get("fingerprintEnabled",
                                   body.get("fingerprint")))
        skip_identify = bool(body.get("skipIdentify",
                                      body.get("skip_identify")))
        ok, err = start_scan(root, max_files, hash_enabled, fp_enabled,
                             skip_identify)
        if not ok:
            return 409, {"error": err}
        return 200, {"ok": True, "root": root}
    if p in ("identify", "identify/resume"):
        pending_n = len(pending_identify())
        ok, err = start_identify()
        if not ok:
            return 409, {"error": err}
        return 200, {"ok": True, "pending": pending_n}
    if p == "scan/cancel":
        if not cancel_scan():
            return 409, {"error": "No music scan is running."}
        return 200, {"ok": True}
    if p == "plan":
        plan, err = compute_plan(body)
        if err:
            return 400, {"error": err}
        return 200, plan
    if p == "execute":
        ok, err = start_execute()
        if not ok:
            return 409, {"error": err}
        return 200, {"ok": True}
    if p == "execute/cancel":
        if not cancel_execute():
            return 409, {"error": "No music execution is running."}
        return 200, {"ok": True}
    if p == "undo":
        manifest = (body.get("manifest") or "").strip()
        if not manifest:
            with LOCK:
                manifest = STATE.get("lastUndo") or ""
        if not manifest:
            return 400, {"error": "No undo manifest specified."}
        if not os.path.isfile(manifest):
            return 404, {"error": f"Manifest not found: {manifest}"}
        result, err = run_undo(manifest)
        if err:
            return 400, {"error": err}
        return 200, result
    if p == "config":
        save_config(
            body.get("acoustidKey") if "acoustidKey" in body else None,
            body.get("discogsToken") if "discogsToken" in body else None,
            body.get("lastfmKey") if "lastfmKey" in body else None)
        return 200, get_config_public()
    return 404, {"error": "not found"}
