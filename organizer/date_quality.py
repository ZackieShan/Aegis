"""Date provenance ladder + plausibility engine.

Not all metadata is created equally. This module decides how much a stored
taken_at can be trusted and, when the top source is suspect, walks down a
ladder of increasingly weak sources:

    1. EXIF DateTimeOriginal          (checked against bounds/defaults/model)
    2. filename-embedded datetime     (IMG_20230629_220120, WhatsApp, ...)
    3. container creation time        (HEIC meta Exif / mvhd via tiff_exif)
    4. filesystem mtime               (bounds/factory-default checked)
    5. nothing plausible              -> taken_at NULL, quality 'unknown'

date_quality values (stored per row):
    exif           plausible EXIF date used
    exif_suspect   an EXIF date existed but was rejected; a lower tier
                   supplied taken_at (exif_date_raw keeps the original)
    filename       date came from the filename
    container      date came from the file container (HEIC meta / video mvhd)
    mtime          plausible mtime used
    mtime_suspect  mtime used but matches a known factory/reset default
    unknown        nothing plausible anywhere; taken_at is NULL
"""
import os
import re
from datetime import datetime, timedelta

MIN_DATE = datetime(1990, 1, 1)
FUTURE_SLACK = timedelta(days=2)

# known factory / reset-default days (year, month, day)
FACTORY_DAYS = {(1970, 1, 1), (1980, 1, 1), (2000, 1, 1), (2001, 1, 1),
                (2004, 1, 1)}

# mtime values that scream "zeroed/overflowed clock"
SUSPECT_MTIME_EXACT = {315550800.0}  # 1980-01-01 00:00 in US/Eastern

# camera-model release years (longest keys first; matched as lowercase
# substrings against "make model camera")
_MODEL_YEARS = [
    ("eos 6d mark ii", 2017), ("iphone 4s", 2011), ("galaxy s23", 2023),
    ("ilce-7rm2", 2015), ("ilce-7rm3", 2017), ("ilce-7rm4", 2019),
    ("ilce-7rm5", 2022), ("ilce-6000", 2014), ("nikon d3000", 2009),
    ("nikon d7000", 2010), ("nikon d700", 2008), ("nikon d40", 2006),
    ("rebel t5i", 2013), ("x-pro3", 2019), ("d-lux", 2014),
    ("eos 5d", 2005), ("lg vx", 2006), ("iphone", 2007),
    ("galaxy s", 2010), ("q3", 2023),
]


def release_year(*texts):
    """Release year for the first model key found in the given strings."""
    blob = " ".join(t or "" for t in texts).lower()
    for key, year in _MODEL_YEARS:
        if key in blob:
            return year
    return None


def _now():
    return datetime.now()


def check_datetime(dt, *model_texts):
    """Return (ok, reason). Hard bounds + factory defaults + (optional)
    camera-model release-year check."""
    if dt is None:
        return False, "none"
    if dt < MIN_DATE:
        return False, "out-of-bounds"
    if dt > _now() + FUTURE_SLACK:
        return False, "future"
    if (dt.year, dt.month, dt.day) in FACTORY_DAYS:
        return False, "factory-default"
    ry = release_year(*model_texts)
    if ry and dt.year < ry:
        return False, f"pre-dates model release ({ry})"
    return True, None


def check_mtime(mtime):
    """Return (ok, reason) for a filesystem mtime. The local-naive datetime
    is checked with bounds/factory rules; the raw epoch value is checked for
    zeroed/overflow patterns."""
    if mtime is None:
        return False, "none"
    try:
        m = float(mtime)
    except (TypeError, ValueError):
        return False, "not-a-number"
    if m <= 0:
        return False, "zeroed"
    if m >= 2 ** 31:
        return False, "overflow"
    if m in SUSPECT_MTIME_EXACT:
        return False, "factory-default"
    dt = datetime.fromtimestamp(m)
    ok, why = check_datetime(dt)  # no model check for mtime
    return ok, why


# ---------------- filename dates ----------------

def _valid(y, m, d, hh=0, mm=0, ss=0):
    try:
        y, m, d, hh, mm, ss = int(y), int(m), int(d), int(hh), int(mm), int(ss)
        if not (1998 <= y <= _now().year):
            return None
        return datetime(y, m, d, hh, mm, ss)
    except (ValueError, TypeError):
        return None


# ordered most-specific first; each entry (regex, group layout)
_NAME_PATTERNS = [
    # IMG_20230629_220120 / VID_... / PXL_... / MVIMG_... / bare YYYYMMDD_HHMMSS
    (re.compile(r"(?:^|IMG_|VID_|PXL_|MVIMG_|BURST|IMG)"
                r"(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
     "ymdhms"),
    # Screenshot_20230629-220120
    (re.compile(r"Screenshot[_-](\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
     "ymdhms"),
    # 2023-06-29_22-01-20 / 2023-06-29 22.01.20
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\s](\d{2})[-.:](\d{2})[-.:](\d{2})"),
     "ymdhms"),
    # WhatsApp: IMG-20191102-WA0007 / VID-...-WA0007
    (re.compile(r"(?:^|/)(?:IMG|VID)-(\d{4})(\d{2})(\d{2})-WA\d+", re.I),
     "ymd"),
    # plain YYYY-MM-DD prefix
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?=[^\d]|$)"), "ymd"),
]


def parse_filename_date(name):
    """Conservative filename-embedded datetime, or None. The first pattern
    that matches wins; if it matches but fails validation the name is
    rejected outright (no weaker pattern gets a second guess)."""
    stem = os.path.splitext(os.path.basename(name))[0]
    for rx, layout in _NAME_PATTERNS:
        m = rx.search(stem)
        if not m:
            continue
        g = m.groups()
        if layout == "ymdhms":
            return _valid(g[0], g[1], g[2], g[3], g[4], g[5])
        return _valid(g[0], g[1], g[2])
    return None


# ---------------- the ladder ----------------

QUALITY_SUSPECT = ("exif_suspect", "mtime_suspect", "unknown")
HEIC_EXTS = {".heic", ".heif"}


def apply_date_ladder(rec, path, st, heic_reader=None):
    """Finalize rec['dt'], rec['dateSource'], rec['date_quality'] and
    rec['exif_date_raw'] from whatever _fill_* extracted. Mutates rec.

    rec['_exif_dt']   : datetime parsed from EXIF (or None)
    rec['exif_date_raw']: original EXIF date string (or None)
    rec['dt']/rec['dateSource'] as preliminarily set by _fill_* (EXIF or
    video-mvhd 'video' or None).
    """
    exif_dt = rec.pop("_exif_dt", None)
    model_texts = (rec.get("make"), rec.get("model"), rec.get("camera"))
    exif_suspect = False

    # tier 1: EXIF
    if exif_dt is not None:
        ok, why = check_datetime(exif_dt, *model_texts)
        if ok:
            rec["dt"] = exif_dt.isoformat(sep=" ")
            rec["date_quality"] = "exif"
            return rec
        exif_suspect = True  # keep exif_date_raw for forensics; fall through
        rec["dt"] = None

    # tier 2: filename
    fn_dt = parse_filename_date(rec["name"])
    if fn_dt is not None:
        rec["dt"] = fn_dt.isoformat(sep=" ")
        rec["dateSource"] = "filename"
        rec["date_quality"] = "exif_suspect" if exif_suspect else "filename"
        return rec

    # tier 3: container (video mvhd already extracted by _fill_video; HEIC
    # meta Exif/mvhd read here on demand)
    if rec["dateSource"] == "video" and rec["dt"]:
        try:
            cdt = datetime.fromisoformat(rec["dt"])
        except ValueError:
            cdt = None
        ok, _ = check_datetime(cdt, *model_texts)
        if ok:
            rec["date_quality"] = "exif_suspect" if exif_suspect else "container"
            return rec
        rec["dt"] = None  # implausible container date: keep walking
    if heic_reader and rec["ext"] in HEIC_EXTS:
        try:
            cdt = heic_reader(path)
        except Exception:
            cdt = None
        ok, _ = check_datetime(cdt, *model_texts)
        if cdt is not None and ok:
            rec["dt"] = cdt.isoformat(sep=" ")
            rec["dateSource"] = "container"
            rec["date_quality"] = "exif_suspect" if exif_suspect else "container"
            return rec

    # tier 4: mtime. Factory-default-but-in-bounds values are kept (they
    # MIGHT be real) but flagged 'mtime_suspect'; zeroed/overflow/out-of-
    # bounds values are unusable.
    m = getattr(st, "st_mtime", None)
    try:
        m = float(m) if m is not None else None
    except (TypeError, ValueError):
        m = None
    if m is not None and 0 < m < 2 ** 31 and m not in SUSPECT_MTIME_EXACT:
        mdt = datetime.fromtimestamp(m)
        if MIN_DATE <= mdt <= _now() + FUTURE_SLACK:
            rec["dt"] = mdt.isoformat(sep=" ")
            rec["dateSource"] = "mtime"
            if exif_suspect:
                rec["date_quality"] = "exif_suspect"
            elif (mdt.year, mdt.month, mdt.day) in FACTORY_DAYS:
                rec["date_quality"] = "mtime_suspect"
            else:
                rec["date_quality"] = "mtime"
            return rec

    # tier 5: nothing usable
    rec["dt"] = None
    rec["dateSource"] = "unknown"
    rec["date_quality"] = "unknown"
    return rec


# ---------------- repair helpers (operate on stored DB rows) ----------------

def row_is_suspect(taken_at, taken_source, make, model, camera, mtime):
    """True when a stored row's date fails plausibility rules."""
    if not taken_at:
        return False  # NULL = already 'unknown', a terminal state
    try:
        dt = datetime.fromisoformat(taken_at)
    except (ValueError, TypeError):
        return True
    if (taken_source or "").startswith("exif"):
        ok, _ = check_datetime(dt, make, model, camera)
        return not ok
    if taken_source in ("video", "container", "filename"):
        ok, _ = check_datetime(dt, make, model, camera)
        return not ok
    # mtime-sourced (or legacy): check the stored mtime too
    ok_dt, _ = check_datetime(dt)
    ok_mt, _ = check_mtime(mtime)
    return not (ok_dt and ok_mt)
