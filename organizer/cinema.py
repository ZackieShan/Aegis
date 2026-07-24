#!/usr/bin/env python3
"""Cinema Organizer core: filename intelligence, quality ranking, TMDB
genres, SQLite index, and scan -> dedupe -> plan -> execute -> undo.

No ffprobe available: everything about quality comes from filename tags.
TMDB is optional - with no API key every title is genre 'Unclassified' and
the organizer stays fully usable offline.

Threading model + state shapes mirror the photo side (server.py): a module
CSTATE guarded by CLOCK, cancel Events, progress dicts polled over HTTP.
"""
import difflib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime DB + undo logs honor ORGANIZER_DATA_DIR (set by Aegis); config with
# API keys stays in the code dir. Defaults to BASE_DIR standalone/in tests.
DATA_DIR = os.environ.get("ORGANIZER_DATA_DIR") or BASE_DIR
CINEMA_DB = os.path.join(DATA_DIR, "cinema.db")
CONFIG_PATH = os.path.join(BASE_DIR, "cinema_config.json")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m2ts", ".ts",
              ".mpg", ".mpeg", ".m4v"}
SUB_EXTS = {".srt", ".sub", ".idx", ".ass", ".ssa"}
CLUTTER_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".txt", ".url", ".exe"}
CLUTTER_WORDS = ("screenshot", "thumb", "poster", "sample")
SAMPLE_SIZE = 50 * 1024 * 1024      # < 50MB next to a full file = sample
HASH_MAX = 2 * 1024 ** 3            # skip hashing files >= 2GB
TMDB_INTERVAL = 0.26                # ~4 requests/second

LOCK = threading.Lock()
SCAN_CANCEL = threading.Event()
EXEC_CANCEL = threading.Event()
STATE = {
    "scan": {"state": "idle", "total": 0, "processed": 0,
             "currentFile": "", "error": None},
    "execute": {"state": "idle", "total": 0, "processed": 0,
                "currentFile": "", "error": None, "log": [], "result": None},
    "recs": [],
    "groups": {},          # path -> groupId
    "scannedRoot": None,
    "plan": None,
    "lastUndo": None,
    "partialScan": False,
}


# =================================================================== parser

_TV_SE_RES = [
    re.compile(r"[sS](\d{1,2})[eE](\d{1,3}((?:[eE]\d{1,3})+)?)"),  # S01E02 / s01e02 / S01E01E02
    re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)"),           # 1x02
    re.compile(r"[sS]eason[ _](\d{1,2})[ _][eE]pisode[ _](\d{1,3})", re.I),
    re.compile(r"[sS][eE]\s*(\d{1,2})\s*[eE][pP]\s*(\d{1,3})(?!\d)"),  # SE1 EP024
    re.compile(r"[sS](\d{1,2})\s*-\s*[eE]?(\d{1,3})(?!\d)"),   # S2 - 05 / S03 - E05
]
# absolute episode numbering (no season given -> season 1)
_TV_ABS_RES = [
    re.compile(r"(?<![a-zA-Z0-9])[eE][pP][\s._]*(\d{1,3})(?![\dPpIi])"),  # ep01
    re.compile(r"(?<![a-zA-Z0-9])[eE](\d{1,3})(?![\dPpIi])"),             # .E05.
]
# looser absolute forms - only tried when the name carries no usable year,
# so "Session 9 (2001)", "Star Wars Episode 1 (1999)" and
# "Movie - 300 (2006)" stay movies
_TV_ABS_RES_YEAR_GUARDED = [
    re.compile(r"[sS]ession[\s._]*(\d{1,3})(?!\d)"),                      # Session 05
    re.compile(r"[eE]pisode[\s._]*(\d{1,3})(?!\d)"),                      # Episode 52
    # " - 05" / " - 105" / " - 05v2"; a trailing letter other than a vN
    # version tag vetoes it (" - 8th", " - 720p" are not episodes)
    re.compile(r"\s+-\s*(\d{1,3})(?:v\d+)?(?![\dA-Za-z])"),
]
_SEASON_ONLY = re.compile(r"[sS](\d{1,2})(?![eE\d])")
# episode-led names with no series anywhere ("01 - Pilot"): still TV-shaped,
# but the series is unknowable from the filename -> never movie-guess these
_TV_LEAD_NO_TITLE = re.compile(r"^\d{1,3}\s+-\s+\S")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SAMPLE_WORD = re.compile(r"(?<![a-z])sample(?![a-z])", re.I)

_RES_RULES = [
    ("2160p", re.compile(r"(?<!\d)(2160p|4k|uhd)(?![\dp])", re.I)),
    ("1080p", re.compile(r"(?<!\d)1080[pi](?!\d)", re.I)),
    ("720p", re.compile(r"(?<!\d)720p(?!\d)", re.I)),
    ("480p", re.compile(r"(?<!\d)(576p|480p)(?!\d)", re.I)),
]
_SRC_RULES = [
    ("bluray", re.compile(r"(?<![a-z])(blu[ -]?ray|bdrip|brrip|remux)(?![a-z])", re.I)),
    ("web-dl", re.compile(r"(?<![a-z])(web[ -]?dl|webdl)(?![a-z])", re.I)),
    ("webrip", re.compile(r"(?<![a-z])webrip(?![a-z])", re.I)),
    ("hdtv", re.compile(r"(?<![a-z])hdtv(?![a-z])", re.I)),
    ("dvdrip", re.compile(r"(?<![a-z])(dvdrip|dvd)(?![a-z])", re.I)),
    ("cam", re.compile(r"(?<![a-z])(hd)?cam(?![a-z])|(?<![a-z])(hd)?ts(?![a-z])", re.I)),
]
_CODEC = re.compile(r"(?<![a-z0-9])(x264|x265|h264|h265|hevc|avc|xvid|divx)(?![a-z0-9])", re.I)
_AUDIO = re.compile(r"(?<![a-z0-9])(aac|dts|atmos|ac3|flac|mp3|dd5\.1|5\.1)(?![a-z0-9])", re.I)
_MISC = re.compile(r"(?<![a-z0-9])(hdr|dv|proper|repack|extended|unrated|remastered|imax)(?![a-z0-9])", re.I)
_GROUP = re.compile(r"-\s*([A-Za-z0-9]+)$")
_GROUP_SKIP = {"dl", "rip", "ray", "dlmux", "hd"}   # tail of WEB-DL etc.

RES_SCORE = {"2160p": 4000, "1080p": 3000, "720p": 2000, "480p": 1000}
SRC_BONUS = {"bluray": 90, "web-dl": 90, "webrip": 70, "hdtv": 50,
             "dvdrip": 30, "cam": 1}


def _clean_title(s):
    """Human-readable title from the raw name fragment."""
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)     # bracketed junk
    s = re.sub(r"[\(\[][^\)\]]*$", " ", s)          # unclosed bracket remnant
    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    if s and (s == s.upper() or s == s.lower()):
        s = s.title()
    return s


def normalize_title(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _fold(s):
    """ASCII-fold diacritics for search queries (Nausicaä -> Nausicaa).
    Display titles keep the original letters; only TMDB queries fold."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def title_similarity(a, b):
    """0..1 similarity of two titles after diacritic fold + normalization."""
    na = normalize_title(_fold(a))
    nb = normalize_title(_fold(b))
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _sim_accept(guess, hit_title):
    """Reasonable-match gate for adopting a TMDB hit for a yearless guess."""
    g = normalize_title(_fold(guess))
    h = normalize_title(_fold(hit_title))
    if not g or not h:
        return False
    if g == h or title_similarity(g, h) >= 0.85:
        return True
    # one fully contains the other (release-group tail, dropped subtitle)
    short, long = (h, g) if len(h) <= len(g) else (g, h)
    if len(short) >= 5 and re.search(
            r"(?<![a-z0-9])" + re.escape(short) + r"(?![a-z0-9])", long):
        return True
    return False


_LEAD_YEAR_PAREN = re.compile(r"^\(((?:19|20)\d{2})\)\s*(.*)$")   # fansub (1984) Title~Jiten
_YEAR_FIRST = re.compile(r"^((?:19|20)\d{2})\s*[-–—]\s*(.+)$")    # 2001 - A Space Odyssey
_INDEX_PREFIX = re.compile(r"^\d{1,2}-(?=[A-Za-z])")               # 5-Vengeful Beauty


def _strip_tokens(s):
    """Remove bracket/brace groups and quality/codec/audio/misc tokens."""
    s = re.sub(r"\{[^}]*\}", " ", s)                # {BALA}-style braces
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\[][^\)\]]*$", " ", s)
    for _, rx in _RES_RULES:
        s = rx.sub(" ", s)
    for _, rx in _SRC_RULES:
        s = rx.sub(" ", s)
    s = _CODEC.sub(" ", s)
    s = _AUDIO.sub(" ", s)
    s = _MISC.sub(" ", s)
    g = _GROUP.search(s)
    if g and re.search(r"[A-Za-z]", g.group(1)) \
            and g.group(1).lower() not in _GROUP_SKIP \
            and len(g.group(1)) >= 3 and len(s[:g.start()].strip()) >= 4:
        s = s[:g.start()]    # scene tail -YIFY; keep 50-50, Re-Animator, WALL-E
    return s


def _guess_title(stem):
    """Best-effort movie title from a yearless filename, '' to give up.

    Handles dual titles joined by '~' (text before wins), N- index
    prefixes, braces/parens/brackets and embedded quality tags. A fully
    numeric remainder is kept -- the number IS the title (1408, 2081)."""
    s = stem
    if "~" in s:
        pick = ""
        for part in s.split("~"):
            if _clean_title(_strip_tokens(part)):
                pick = part
                break
        if not pick:
            return ""
        s = pick
    s = _INDEX_PREFIX.sub("", s)
    return _clean_title(_strip_tokens(s))


def _year_ok(y):
    return 1900 <= y <= datetime.now().year + 1


def _clean_series_title(s):
    """Series title from the fragment before an episode tag.

    Reuses the movie-side fansub cleanup (_strip_tokens) so trailing
    quality parens/brackets, [Group] prefixes and CRC tags drop out, and
    '~' alt-title splits keep the first usable title."""
    if "~" in s:
        for part in s.split("~"):
            c = _clean_title(_strip_tokens(part))
            if c:
                return c
        return ""
    return _clean_title(_strip_tokens(s))


def parse_media_name(name):
    """Parse a video filename into a metadata dict.

    kind 'movie' needs a valid year (1900..now+1); kind 'tv' needs an
    episode pattern (or season-only -> season_pack). Anything else is
    kind 'unknown'.
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    rec = {"kind": "unknown", "title": None, "year": None,
           "season": None, "episode": None, "season_pack": False,
           "guess_title": None,
           "is_sample": bool(_SAMPLE_WORD.search(stem)),
           "quality_score": 500, "low_quality": False, "tags": []}

    # --- tags / quality (independent of kind) ---
    res = source = None
    for label, rx in _RES_RULES:
        if rx.search(stem):
            res = label
            break
    for label, rx in _SRC_RULES:
        if rx.search(stem):
            source = label
            break
    codec = _CODEC.search(stem)
    audio = _AUDIO.search(stem)
    misc = _MISC.findall(stem)
    group = _GROUP.search(stem)
    tags = [t for t in [res, source,
                        codec.group(1).lower() if codec else None,
                        audio.group(1).lower() if audio else None]
            if t]
    tags += [m.lower() for m in misc]
    if group and group.group(1).lower() not in _GROUP_SKIP:
        tags.append("group:" + group.group(1))
    base = RES_SCORE.get(res) or (1000 if source == "dvdrip" else 500)
    rec["quality_score"] = base + SRC_BONUS.get(source, 0)
    rec["low_quality"] = (source == "cam")
    rec["tags"] = tags

    # --- TV first (episode patterns beat movie-year parsing) ---
    m, season, eps = None, None, None
    abs_match = False
    for rx in _TV_SE_RES:
        m = rx.search(stem)
        if m:
            season = int(m.group(1))
            eps = [int(x) for x in re.findall(r"\d{1,3}", m.group(2))]
            break
    if m is None:
        for rx in _TV_ABS_RES:
            m = rx.search(stem)
            if m:
                season, eps = 1, [int(m.group(1))]
                abs_match = True
                break
    if m is None and not any(_year_ok(int(y.group(1)))
                             for y in _YEAR.finditer(stem)):
        for rx in _TV_ABS_RES_YEAR_GUARDED:
            m = rx.search(stem)
            if m:
                season, eps = 1, [int(m.group(1))]
                abs_match = True
                break
    if m:
        title = _clean_series_title(stem[:m.start()])
        if abs_match and title and not re.search(r"[A-Za-z]", title):
            # absolute numbering needs a real series name: "01 - 14-Carrot
            # Rabbit" or "1012 - 24 Hour Propane People" is episode-led
            # junk, not a show called "01"/"1012"
            title = None
        if title:
            rec.update(kind="tv", title=title, season=season,
                       episode=eps[0], episodes=eps)
            ym = _YEAR.search(stem[:m.start()])
            if ym and _year_ok(int(ym.group(1))):
                rec["year"] = int(ym.group(1))
        # an episode pattern without a usable series title is still TV -
        # never hand it to movie identification
        return rec

    # --- episode-led names ("01 - Pilot"): TV shape, unknowable series ---
    if _TV_LEAD_NO_TITLE.search(stem):
        return rec

    # --- season pack: S01 with no episode ---
    sm = _SEASON_ONLY.search(stem)
    if sm:
        title = _clean_series_title(stem[:sm.start()])
        if title:
            rec.update(kind="tv", title=title, season=int(sm.group(1)),
                       season_pack=True)
            return rec

    # --- fansub: leading (YYYY), dual titles "Eng~Jpn", [Group] [CRC] ---
    lm = _LEAD_YEAR_PAREN.match(stem)
    if lm and _year_ok(int(lm.group(1))):
        for part in lm.group(2).split("~"):
            title = _clean_title(_strip_tokens(part))
            if title:
                rec.update(kind="movie", title=title, year=int(lm.group(1)))
                return rec

    # --- year-first: "2001 - A Space Odyssey" (year may be part of the
    # title, so keep a digits+title guess for a no-year TMDB fallback) ---
    yf = _YEAR_FIRST.match(stem)
    if yf and _year_ok(int(yf.group(1))):
        title = _clean_title(_strip_tokens(yf.group(2)))
        if title:
            rec.update(kind="movie", title=title, year=int(yf.group(1)))
            rec["guess_title"] = _fold(f"{yf.group(1)} {title}").strip()
            return rec

    # --- movie: first year with a non-empty title before it wins ---
    for ym in _YEAR.finditer(stem):
        y = int(ym.group(1))
        if not _year_ok(y):
            continue
        title = _clean_title(stem[:ym.start()])
        if title:
            rec.update(kind="movie", title=title, year=y)
            return rec

    # --- yearless: keep a cleaned title guess for TMDB identification ---
    guess = _guess_title(stem)
    if guess:
        rec["guess_title"] = guess
    return rec


def looks_like_clutter(name, video_stems):
    """Non-video junk that belongs to a release: matches a video basename
    in the same folder, or contains screenshot/thumb/poster/sample."""
    stem = os.path.splitext(os.path.basename(name))[0]
    if stem.lower() in video_stems:
        return True
    low = stem.lower()
    return any(w in low for w in CLUTTER_WORDS)


# =================================================================== tmdb

def tmdb_fetch(url):
    """HTTP GET -> parsed JSON. Module-level so tests can stub it."""
    req = urllib.request.Request(url, headers={"User-Agent": "PhotoOrganizer/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def make_tmdb_fetcher(token):
    """Fetcher authenticating with a TMDB read token (Bearer header).
    The token itself is never logged or embedded in URLs."""
    def fetch(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "PhotoOrganizer/1.0",
            "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    return fetch


def _resolve_fetcher(fetcher, token):
    if fetcher is not None:
        return fetcher
    if token:
        return make_tmdb_fetcher(token)
    return tmdb_fetch


def _key_qs(api_key):
    """api_key query fragment; empty when Bearer auth carries the request."""
    return f"api_key={urllib.parse.quote(api_key)}&" if api_key else ""


_THROTTLE = [0.0]


def _throttled(url, fetcher, cancel=None):
    wait = TMDB_INTERVAL - (time.time() - _THROTTLE[0])
    while wait > 0:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("cancelled")
        time.sleep(min(wait, 0.05))
        wait = TMDB_INTERVAL - (time.time() - _THROTTLE[0])
    _THROTTLE[0] = time.time()
    return fetcher(url)


def _genre_map(kind, api_key, fetcher, cancel):
    url = (f"https://api.themoviedb.org/3/genre/{'movie' if kind == 'movie' else 'tv'}"
           f"/list?{_key_qs(api_key)}language=en-US")
    data = _throttled(url, fetcher, cancel)
    return {g["id"]: g["name"] for g in data.get("genres", [])}


def lookup_genre(kind, title, year, api_key, token=None, fetcher=None,
                 cancel=None, _maps={}):
    """TMDB search -> (genre, subgenre, source). source 'tmdb' or 'none'.
    Top match must be within 1 year of the parsed year (when known);
    tries up to 3 results. Auth: Bearer token preferred, api_key query
    param as fallback. Diacritics fold for the query only."""
    if not title or not (api_key or token):
        return None, None, "none"
    fetcher = _resolve_fetcher(fetcher, token)
    try:
        if kind not in _maps:
            _maps[kind] = _genre_map(kind, api_key, fetcher, cancel)
        gmap = _maps[kind]
        endpoint = "movie" if kind == "movie" else "tv"
        url = (f"https://api.themoviedb.org/3/search/{endpoint}"
               f"?{_key_qs(api_key)}query={urllib.parse.quote(_fold(title))}"
               f"&include_adult=false")
        if year and kind == "movie":
            url += f"&year={year}"
        data = _throttled(url, fetcher, cancel)
        date_key = "release_date" if kind == "movie" else "first_air_date"
        for hit in (data.get("results") or [])[:3]:
            if year:
                hy = (hit.get(date_key) or "")[:4]
                if hy.isdigit() and abs(int(hy) - year) > 1:
                    continue
            names = [gmap.get(gid) for gid in hit.get("genre_ids", [])]
            names = [n for n in names if n]
            genre = names[0] if names else "Unclassified"
            sub = names[1] if len(names) > 1 else "General"
            return genre, sub, "tmdb"
    except InterruptedError:
        raise
    except Exception:
        pass
    return None, None, "none"


def identify_yearless(guess, api_key, token=None, fetcher=None, cancel=None,
                      _maps={}):
    """Yearless title guess -> (title, year, genre, subgenre, source).

    Searches TMDB without a year and adopts the first reasonable match's
    canonical title + release year + genres. The normalized-title
    similarity gate keeps junk names unidentified. If no hit is accepted,
    the query is retried with trailing words dropped (release tails like
    'hd', 'YIFY', 'cd1' otherwise poison the search)."""
    if not guess or not (api_key or token):
        return None, None, None, None, "none"
    fetcher = _resolve_fetcher(fetcher, token)
    try:
        if "movie" not in _maps:
            _maps["movie"] = _genre_map("movie", api_key, fetcher, cancel)
        gmap = _maps["movie"]
        words = _fold(guess).split()
        queries = []
        while words and len(queries) < 4:
            queries.append(" ".join(words))
            if len(words) == 1:
                break
            words = words[:-1]
        for q in queries:
            url = ("https://api.themoviedb.org/3/search/movie"
                   f"?{_key_qs(api_key)}query={urllib.parse.quote(q)}"
                   "&include_adult=false")
            data = _throttled(url, fetcher, cancel)
            for hit in (data.get("results") or [])[:5]:
                names = [hit.get("title") or "",
                         hit.get("original_title") or ""]
                if not any(_sim_accept(q, n) for n in names):
                    continue
                hy = (hit.get("release_date") or "")[:4]
                if not hy.isdigit():
                    continue
                gnames = [gmap.get(gid) for gid in hit.get("genre_ids", [])]
                gnames = [n for n in gnames if n]
                genre = gnames[0] if gnames else "Unclassified"
                sub = gnames[1] if len(gnames) > 1 else "General"
                return (hit.get("title") or guess, int(hy), genre, sub, "tmdb")
    except InterruptedError:
        raise
    except Exception:
        pass
    return None, None, None, None, "none"


def identify_tv(guess, year, api_key, token=None, fetcher=None, cancel=None,
                _maps={}):
    """Series title guess -> (title, year, genre, subgenre, source).

    The TV twin of identify_yearless: searches TMDB /search/tv WITHOUT a
    year filter (years are meaningless for episode files), applies the
    same normalized-title similarity gate, and adopts the canonical
    series title + first-air year + tv genres (Animation etc.). When the
    filename carried a year it is only used to prefer one gated hit over
    another (The Office US vs UK), never to filter. If no hit is
    accepted, the query is retried with trailing words dropped."""
    if not guess or not (api_key or token):
        return None, None, None, None, "none"
    fetcher = _resolve_fetcher(fetcher, token)
    try:
        if "tv" not in _maps:
            _maps["tv"] = _genre_map("tv", api_key, fetcher, cancel)
        gmap = _maps["tv"]
        words = _fold(guess).split()
        queries = []
        while words and len(queries) < 4:
            queries.append(" ".join(words))
            if len(words) == 1:
                break
            words = words[:-1]
        for q in queries:
            url = ("https://api.themoviedb.org/3/search/tv"
                   f"?{_key_qs(api_key)}query={urllib.parse.quote(q)}"
                   "&include_adult=false")
            data = _throttled(url, fetcher, cancel)
            gated = []
            for hit in (data.get("results") or [])[:5]:
                names = [hit.get("name") or "",
                         hit.get("original_name") or ""]
                if not any(_sim_accept(q, n) for n in names):
                    continue
                hy = (hit.get("first_air_date") or "")[:4]
                gated.append((hit, int(hy) if hy.isdigit() else None))
            pick = None
            if year:
                for hit, hy in gated:
                    if hy and abs(hy - year) <= 1:
                        pick = (hit, hy)
                        break
            if pick is None and gated:
                pick = gated[0]
            if pick:
                hit, hy = pick
                gnames = [gmap.get(gid) for gid in hit.get("genre_ids", [])]
                gnames = [n for n in gnames if n]
                genre = gnames[0] if gnames else "Unclassified"
                sub = gnames[1] if len(gnames) > 1 else "General"
                return (hit.get("name") or guess, hy, genre, sub, "tmdb")
    except InterruptedError:
        raise
    except Exception:
        pass
    return None, None, None, None, "none"


# =================================================================== db

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  filename TEXT, dir TEXT, ext TEXT,
  kind TEXT, title TEXT, year INTEGER, season INTEGER, episode INTEGER,
  season_pack INTEGER DEFAULT 0,
  quality_score INTEGER, tags TEXT, low_quality INTEGER DEFAULT 0,
  is_sample INTEGER DEFAULT 0,
  size_bytes INTEGER, mtime REAL, md5 TEXT,
  genre TEXT, subgenre TEXT, genre_source TEXT,
  dupe_group TEXT, error TEXT, scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS genre_cache (
  key TEXT PRIMARY KEY,
  genre TEXT, subgenre TEXT, source TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ident_cache (
  key TEXT PRIMARY KEY,
  title TEXT, year INTEGER, genre TEXT, subgenre TEXT, source TEXT,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS cmeta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def db_connect():
    con = sqlite3.connect(CINEMA_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


import contextlib


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


def db_init():
    with _db() as con:
        con.executescript(DB_SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")


def get_meta():
    try:
        with _db() as con:
            return {r[0]: r[1] for r in con.execute("SELECT key, value FROM cmeta")}
    except sqlite3.Error:
        return {}


def set_meta(kv):
    with _db() as con:
        for k, v in kv.items():
            con.execute("INSERT OR REPLACE INTO cmeta (key, value) VALUES (?, ?)",
                        (k, str(v)))


def cache_key(kind, title, year):
    return f"{kind}|{normalize_title(title)}|{year or ''}"


def genre_cache_get(kind, title, year):
    with _db() as con:
        r = con.execute("SELECT genre, subgenre, source FROM genre_cache"
                        " WHERE key = ?", (cache_key(kind, title, year),)).fetchone()
    return (r["genre"], r["subgenre"], r["source"]) if r else None


def genre_cache_put(kind, title, year, genre, subgenre, source):
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO genre_cache"
                    " (key, genre, subgenre, source, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (cache_key(kind, title, year), genre, subgenre, source,
                     datetime.now().isoformat(sep=" ")))


def ident_cache_get(guess):
    """Cached yearless identification -> (title, year, genre, subgenre,
    source) or None."""
    with _db() as con:
        r = con.execute("SELECT title, year, genre, subgenre, source"
                        " FROM ident_cache WHERE key = ?",
                        (normalize_title(guess),)).fetchone()
    return (r["title"], r["year"], r["genre"], r["subgenre"], r["source"]) \
        if r else None


def ident_cache_put(guess, title, year, genre, subgenre, source):
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO ident_cache"
                    " (key, title, year, genre, subgenre, source, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (normalize_title(guess), title, year, genre, subgenre,
                     source, datetime.now().isoformat(sep=" ")))


def db_replace_recs(recs, groups, scanned_at):
    with _db() as con:
        con.execute("DELETE FROM media")
        for rec in recs:
            con.execute(
                "INSERT OR REPLACE INTO media (path, filename, dir, ext, kind, title,"
                " year, season, episode, season_pack, quality_score, tags,"
                " low_quality, is_sample, size_bytes, mtime, md5, genre, subgenre,"
                " genre_source, dupe_group, error, scanned_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["path"], rec["name"], os.path.dirname(rec["path"]), rec["ext"],
                 rec["kind"], rec.get("title"), rec.get("year"), rec.get("season"),
                 rec.get("episode"), 1 if rec.get("season_pack") else 0,
                 rec.get("quality_score"), json.dumps(rec.get("tags") or []),
                 1 if rec.get("low_quality") else 0,
                 1 if rec.get("is_sample") else 0,
                 rec.get("size"), rec.get("mtime"), rec.get("md5"),
                 rec.get("genre"), rec.get("subgenre"), rec.get("genre_source"),
                 groups.get(rec["path"]), rec.get("error"), scanned_at))


def db_update_paths(entries, action):
    """Keep media.path truthful after execute/undo."""
    with _db() as con:
        for e in entries:
            src, dst = e["from"], e["to"]
            act = action or e.get("action")
            if act == "move":
                con.execute("UPDATE media SET path=?, filename=?, dir=? WHERE path=?",
                            (dst, os.path.basename(dst), os.path.dirname(dst), src))
            elif act == "restore":
                con.execute("UPDATE media SET path=?, filename=?, dir=? WHERE path=?",
                            (src, os.path.basename(src), os.path.dirname(src), dst))


# =================================================================== config

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(tmdb_key=None, tmdb_token=None):
    """Persist TMDB credentials. None leaves a field unchanged, '' clears
    it, any other value sets it. Values are never logged."""
    cfg = load_config()
    if tmdb_key is not None:
        cfg["tmdbKey"] = tmdb_key.strip()
    if tmdb_token is not None:
        cfg["tmdbToken"] = tmdb_token.strip()
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


def get_config_public():
    """Config for the UI: masked display values only, never raw secrets."""
    cfg = load_config()
    key = cfg.get("tmdbKey") or ""
    tok = cfg.get("tmdbToken") or ""
    return {"hasKey": bool(key or tok), "hasApiKey": bool(key),
            "hasToken": bool(tok), "tmdbKeyMasked": mask_secret(key),
            "tmdbTokenMasked": mask_secret(tok)}


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
    s = _BAD_CHARS.sub(" ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return (s or "Unknown")[:80].rstrip(" .")


def md5_file(path, cancel=None):
    import hashlib
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
        return dict(STATE["scan"])


def execute_status():
    with LOCK:
        d = dict(STATE["execute"])
        d["log"] = list(d["log"][-200:])
        return d


# =================================================================== scan

def collect_files(root, max_files):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in VIDEO_EXTS or ext in SUB_EXTS or ext in CLUTTER_EXTS:
                out.append(os.path.join(dirpath, fn))
                if max_files and len(out) >= max_files:
                    return out
    return out


def _group_recs(recs):
    """Union-find over (parsed identity) and (md5 when present).
    Movies key: title+year; TV: series+SxxEyy. Samples/clutter/unknown and
    season packs never group."""
    keys = {}
    for i, rec in enumerate(recs):
        if rec["kind"] == "movie":
            keys[i] = ("m", normalize_title(rec["title"]), rec["year"])
        elif rec["kind"] == "tv" and not rec.get("season_pack") \
                and rec.get("episode") is not None:
            keys[i] = ("t", normalize_title(rec["title"]),
                       rec["season"], rec["episode"])
    parent = list(range(len(recs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_key, by_md5 = {}, {}
    for i, k in keys.items():
        if k in by_key:
            union(i, by_key[k])
        else:
            by_key[k] = i
        m = recs[i].get("md5")
        if m:
            if m in by_md5:
                union(i, by_md5[m])
            else:
                by_md5[m] = i

    clusters = {}
    for i in keys:
        clusters.setdefault(find(i), []).append(i)
    groups = {}
    gi = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        gi += 1
        gid = f"G{gi:02d}"
        for i in members:
            groups[recs[i]["path"]] = gid
    return groups


def run_scan(root, max_files, hash_enabled):
    scanned_at = datetime.now().isoformat(sep=" ")
    cancelled = False
    try:
        files = collect_files(root, max_files)
        set_scan(total=len(files), processed=0)
        recs = []
        videos = []  # indices into recs that are videos (not clutter)
        for i, path in enumerate(files):
            if SCAN_CANCEL.is_set():
                cancelled = True
                break
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            set_scan(processed=i, currentFile=name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            base = {"path": path, "name": name, "ext": ext,
                    "size": st.st_size, "mtime": st.st_mtime, "md5": None,
                    "error": None}
            if ext in VIDEO_EXTS:
                rec = {**base, **parse_media_name(name)}
                if hash_enabled and st.st_size < HASH_MAX:
                    rec["md5"] = md5_file(path, SCAN_CANCEL)
                videos.append(len(recs))
                recs.append(rec)
            elif ext in SUB_EXTS:
                continue  # subtitles attach to their video below
            else:
                recs.append({**base, "kind": "_clutter_candidate"})

        # companion + clutter matching need per-dir video indexes.
        # Companions are dicts: {"from": path, "suffix": ".en.srt"} renames to
        # the video's destination stem + suffix (so language tags survive:
        # "Movie.en.srt" -> "Title (Year).en.srt"), while
        # {"from": path, "keepName": True} keeps its own filename beside the
        # video (poster.jpg / fanart.jpg / movie.nfo — what Plex looks for).
        stems_by_dir = {}          # dir -> {video stem.lower()}
        vids_by_dir = {}           # dir -> [rec index]
        stem_index = {}            # (dir, stem.lower()) -> rec index
        subs = []
        for i in videos:
            r = recs[i]
            d = os.path.dirname(r["path"])
            s = os.path.splitext(r["name"])[0].lower()
            stems_by_dir.setdefault(d, set()).add(s)
            vids_by_dir.setdefault(d, []).append(i)
            stem_index[(d, s)] = i
            r["companions"] = []
        for path in files:
            if os.path.splitext(path)[1].lower() in SUB_EXTS:
                subs.append(path)

        def attach_by_stem(path):
            """Attach Movie.srt / Movie.en.srt / Movie-poster.jpg / Movie.nfo
            to the video 'Movie' in the same folder. Longest matching video
            stem wins; the remainder (language tag, '-poster', ...) is kept
            through the rename. True when attached."""
            d = os.path.dirname(path)
            stem, ext = os.path.splitext(os.path.basename(path))
            low = stem.lower()
            best = None
            for vstem in stems_by_dir.get(d, ()):
                if low == vstem or (low.startswith(vstem)
                                    and low[len(vstem)] in "._-"):
                    if best is None or len(vstem) > len(best):
                        best = vstem
            if best is None:
                return False
            recs[stem_index[(d, best)]]["companions"].append(
                {"from": path, "suffix": stem[len(best):] + ext})
            return True

        for sp in subs:
            attach_by_stem(sp)     # unmatched subs stay where they are

        # finalize clutter candidates: artwork/NFO that belongs to a video
        # rides WITH it instead of being quarantined to _Clutter
        FOLDER_ART = {"poster", "folder", "cover", "fanart", "banner",
                      "backdrop", "clearlogo", "landscape", "thumb",
                      "movie", "tvshow", "season"}
        KEEPABLE = {".jpg", ".jpeg", ".png", ".nfo"}
        final = []
        for rec in recs:
            if rec["kind"] != "_clutter_candidate":
                final.append(rec)
                continue
            d = os.path.dirname(rec["path"])
            stem = os.path.splitext(rec["name"])[0].lower()
            if rec["ext"] in KEEPABLE:
                if attach_by_stem(rec["path"]):
                    continue                    # stem-matched its video
                if stem in FOLDER_ART and len(vids_by_dir.get(d, [])) == 1:
                    # folder-level art/metadata in a single-video folder
                    # belongs to that video; keep its own (Plex-meaningful)
                    # name at the destination
                    recs[vids_by_dir[d][0]]["companions"].append(
                        {"from": rec["path"], "keepName": True})
                    continue
            stems = stems_by_dir.get(d, set())
            if looks_like_clutter(rec["name"], stems):
                rec["kind"] = "clutter"
                final.append(rec)
            # non-matching .txt/.exe etc. are ignored entirely
        recs = final

        # yearless identification via TMDB (cached in ident_cache; needs
        # credentials; keeps the similarity gate between junk and movies)
        cfg = load_config()
        api_key = cfg.get("tmdbKey") or ""
        tmdb_token = cfg.get("tmdbToken") or ""
        if api_key or tmdb_token:
            for r in recs:
                if r["kind"] != "unknown" or not r.get("guess_title"):
                    continue
                if SCAN_CANCEL.is_set():
                    cancelled = True
                    break
                guess = r["guess_title"]
                ic = ident_cache_get(guess)
                if ic:
                    # ident rows are terminal (tmdb or none) - re-run only
                    # after clearing ident_cache
                    if ic[4] == "tmdb" and ic[0] and ic[1]:
                        r.update(kind="movie", title=ic[0], year=ic[1])
                    continue
                set_scan(currentFile=f"identify: {guess}")
                try:
                    ct, yr, g, sg, src = identify_yearless(
                        guess, api_key, token=tmdb_token, cancel=SCAN_CANCEL)
                except InterruptedError:
                    cancelled = True
                    break
                ident_cache_put(guess, ct, yr, g, sg, src)
                if src == "tmdb" and ct and yr:
                    genre_cache_put("movie", ct, yr, g or "Unclassified",
                                    sg or "General", "tmdb")
                    r.update(kind="movie", title=ct, year=yr)

        # sample-by-size: small video beside a big one of the same movie
        videos = [i for i, r in enumerate(recs) if r["kind"] in ("movie", "tv")]
        by_identity = {}
        for i in videos:
            r = recs[i]
            ident = (os.path.dirname(r["path"]),
                     normalize_title(r.get("title")), r.get("year"),
                     r.get("season"), r.get("episode"))
            by_identity.setdefault(ident, []).append(i)
        for members in by_identity.values():
            if len(members) < 2:
                continue
            biggest = max(recs[i]["size"] for i in members)
            if biggest >= SAMPLE_SIZE:
                for i in members:
                    if recs[i]["size"] < SAMPLE_SIZE:
                        recs[i]["is_sample"] = True

        groupable = [r for r in recs if r["kind"] in ("movie", "tv")
                     and not r.get("is_sample")]
        groups = _group_recs(groupable)

        # genre enrichment (cache -> TMDB -> Unclassified)
        for r in recs:
            if r["kind"] not in ("movie", "tv"):
                continue
            # a year-first alias identified before short-circuits the lookup
            if r["kind"] == "movie" and r.get("guess_title"):
                ic = ident_cache_get(r["guess_title"])
                if ic and ic[4] == "tmdb" and ic[0] and ic[1]:
                    r["title"], r["year"] = ic[0], ic[1]
            ck = genre_cache_get(r["kind"], r["title"], r.get("year"))
            if ck and (ck[2] == "tmdb" or not (api_key or tmdb_token)):
                r["genre"], r["subgenre"], r["genre_source"] = ck
                continue
            # TV series identification: year-less /search/tv + similarity
            # gate, adopting the canonical series title/year/tv genres.
            # Cached in ident_cache under a "tv: " key; 'none' is terminal.
            if r["kind"] == "tv" and (api_key or tmdb_token):
                if SCAN_CANCEL.is_set():
                    cancelled = True
                    break
                tv_key = "tv: " + (r["title"] or "")
                ic = ident_cache_get(tv_key)
                if ic is None:
                    set_scan(currentFile=f"identify-tv: {r['title']}")
                    try:
                        ct, yr, g2, sg2, src2 = identify_tv(
                            r["title"], r.get("year"), api_key,
                            token=tmdb_token, cancel=SCAN_CANCEL)
                    except InterruptedError:
                        cancelled = True
                        break
                    ident_cache_put(tv_key, ct, yr, g2, sg2, src2)
                    ic = (ct, yr, g2, sg2, src2)
                if ic[4] == "tmdb" and ic[0]:
                    r["title"] = ic[0]
                    if ic[1]:
                        r["year"] = ic[1]
                    g = ic[2] or "Unclassified"
                    sg = ic[3] or "General"
                    genre_cache_put("tv", r["title"], r.get("year"),
                                    g, sg, "tmdb")
                    r["genre"], r["subgenre"], r["genre_source"] = g, sg, "tmdb"
                else:
                    genre_cache_put("tv", r["title"], r.get("year"),
                                    "Unclassified", "General", "none")
                    r["genre"], r["subgenre"], r["genre_source"] = \
                        "Unclassified", "General", "none"
                continue
            if SCAN_CANCEL.is_set():
                cancelled = True
                break
            set_scan(currentFile=f"genre: {r['title']}")
            try:
                g, sg, src = lookup_genre(r["kind"], r["title"], r.get("year"),
                                          api_key, token=tmdb_token,
                                          cancel=SCAN_CANCEL)
            except InterruptedError:
                cancelled = True
                break
            if not g and r["kind"] == "movie" and r.get("guess_title"):
                # year-first names ("2001 - A Space Odyssey") may need the
                # full digits+title query without the year filter; a cached
                # 'none' is terminal (the with-year lookup above already
                # had its chance)
                if ident_cache_get(r["guess_title"]):
                    g, sg, src = None, None, "none"
                else:
                    set_scan(currentFile=f"identify: {r['guess_title']}")
                    try:
                        ct, yr, g2, sg2, src2 = identify_yearless(
                            r["guess_title"], api_key, token=tmdb_token,
                            cancel=SCAN_CANCEL)
                    except InterruptedError:
                        cancelled = True
                        break
                    ident_cache_put(r["guess_title"], ct, yr, g2, sg2, src2)
                    if src2 == "tmdb" and ct and yr:
                        r["title"], r["year"] = ct, yr
                        g, sg, src = g2 or "Unclassified", sg2 or "General", "tmdb"
            if not g:
                g, sg, src = "Unclassified", "General", "none"
            genre_cache_put(r["kind"], r["title"], r.get("year"), g, sg, src)
            r["genre"], r["subgenre"], r["genre_source"] = g, sg, src
        for r in recs:
            if r["kind"] in ("movie", "tv") and not r.get("genre"):
                r["genre"], r["subgenre"], r["genre_source"] = \
                    "Unclassified", "General", "none"

        with LOCK:
            STATE["recs"] = recs
            STATE["groups"] = groups
            STATE["scannedRoot"] = root
            STATE["plan"] = None
            STATE["partialScan"] = cancelled
        try:
            if cancelled:
                # db_replace_recs does DELETE FROM media -- never let a
                # cancelled scan wipe the last COMPLETE scan off disk in
                # exchange for the handful of rows collected so far. The
                # partial set stays in STATE for this session only.
                set_meta({"last_scan_cancelled_at": scanned_at,
                          "last_scan_cancelled_count": str(len(recs))})
            else:
                db_replace_recs(recs, groups, scanned_at)
                set_meta({"last_scan_root": root,
                          "last_scan_completed_at": scanned_at,
                          "last_scan_count": str(len(recs)),
                          "last_scan_partial": "0"})
        except Exception:
            pass
        if cancelled:
            set_scan(state="cancelled", processed=len(recs), currentFile="")
        else:
            set_scan(state="done", processed=len(files), currentFile="")
    except Exception as e:
        set_scan(state="error", error=f"{type(e).__name__}: {e}")


def start_scan(root, max_files, hash_enabled):
    with LOCK:
        if STATE["scan"]["state"] == "running":
            return False, "A cinema scan is already running."
        STATE["scan"] = {"state": "running", "total": 0, "processed": 0,
                         "currentFile": "", "error": None}
        STATE["partialScan"] = False
    SCAN_CANCEL.clear()
    t = threading.Thread(target=run_scan, args=(root, max_files, hash_enabled),
                         daemon=True)
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
    try:
        with _db() as con:
            rows = con.execute("SELECT * FROM media ORDER BY path").fetchall()
        if not rows:
            return False
        meta = get_meta()
    except sqlite3.Error:
        return False
    recs = []
    groups = {}
    for row in rows:
        rec = {"path": row["path"], "name": row["filename"], "ext": row["ext"],
               "kind": row["kind"], "title": row["title"], "year": row["year"],
               "season": row["season"], "episode": row["episode"],
               "season_pack": bool(row["season_pack"]),
               "quality_score": row["quality_score"],
               "tags": json.loads(row["tags"] or "[]"),
               "low_quality": bool(row["low_quality"]),
               "is_sample": bool(row["is_sample"]),
               "size": row["size_bytes"], "mtime": row["mtime"],
               "md5": row["md5"], "genre": row["genre"],
               "subgenre": row["subgenre"], "genre_source": row["genre_source"],
               "error": row["error"], "companions": []}
        recs.append(rec)
        if row["dupe_group"]:
            groups[row["path"]] = row["dupe_group"]
    with LOCK:
        STATE["recs"] = recs
        STATE["groups"] = groups
        STATE["scannedRoot"] = meta.get("last_scan_root")
        STATE["partialScan"] = meta.get("last_scan_partial") == "1"
        STATE["scan"] = {"state": "done", "total": len(recs),
                         "processed": len(recs), "currentFile": "", "error": None}
    return True


def build_results():
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
        partial = STATE["partialScan"]
    by_kind = Counter(r["kind"] for r in recs)
    genres = Counter(r.get("genre") for r in recs
                     if r["kind"] in ("movie", "tv") and r.get("genre"))
    def q_bucket(r):
        s = r.get("quality_score") or 500
        if s >= 4000:
            return "2160p/4K"
        if s >= 3000:
            return "1080p"
        if s >= 2000:
            return "720p"
        if s >= 1000:
            return "SD"
        return "unknown"
    qmix = Counter(q_bucket(r) for r in recs if r["kind"] in ("movie", "tv"))
    gid_members = {}
    for p, gid in groups.items():
        gid_members.setdefault(gid, []).append(p)
    return {
        "scannedRoot": root,
        "partial": partial,
        "totalFiles": len(recs),
        "byKind": dict(by_kind),
        "topGenres": genres.most_common(10),
        "qualityMix": dict(qmix),
        "lowQuality": sum(1 for r in recs if r.get("low_quality")),
        "dupeGroups": len(gid_members),
        "dupeFiles": sum(len(m) - 1 for m in gid_members.values()),
        "samples": sum(1 for r in recs if r.get("is_sample")),
        "clutter": by_kind.get("clutter", 0),
        "unidentified": by_kind.get("unknown", 0),
        "hasTmdbKey": bool((load_config().get("tmdbKey") or "")
                           or (load_config().get("tmdbToken") or "")),
        "recs": [{k: r.get(k) for k in
                  ("path", "name", "kind", "title", "year", "season",
                   "episode", "season_pack", "quality_score", "tags",
                   "is_sample", "low_quality", "genre", "subgenre",
                   "genre_source", "size")}
                 for r in recs],
        "groups": {gid: sorted(m) for gid, m in sorted(gid_members.items())},
    }


# =================================================================== plan

def movie_dest(rec, target_root, split=False, year_folder=True,
               layout="genre"):
    """Movie scheme. layout="plex" gives the flat structure Plex expects:
    Movies\\Title (Year)\\Title (Year).ext (no genre levels). layout="genre"
    keeps the Genre\\Sub-genre tree; there, split=True roots it at Movies\\
    and year_folder=False drops the extra YYYY level."""
    folder = f"{sanitize_component(rec['title'])} ({rec['year']})"
    name = folder + rec["ext"]
    if layout == "plex":
        return os.path.join(target_root, "Movies", folder, name)
    g = sanitize_component(rec.get("genre") or "Unclassified")
    sg = sanitize_component(rec.get("subgenre") or "General")
    parts = [target_root]
    if split:
        parts.append("Movies")
    parts += [g, sg]
    if year_folder:
        parts.append(f"{rec['year']:04d}")
    parts += [folder, name]
    return os.path.join(*parts)


def tv_dest(rec, target_root, split=False, layout="genre"):
    r"""TV scheme. layout="plex": TV\Show\Season 01\Show - S01E01.ext (no
    genre levels). layout="genre": [TV\]Genre\Sub-genre\Show\Season NN\...
    (split=True adds the TV\ root).

    Season 0 becomes Specials\ (the Plex convention for extras). Years never
    appear in TV paths (a series spans years; episodes must not scatter
    across year folders). Multi-episode tags keep their full run (S01E01E02).
    Season packs keep the original filename."""
    t = sanitize_component(rec["title"])
    season = rec["season"]
    season_dir = "Specials" if season == 0 else f"Season {season:02d}"
    if layout == "plex":
        base = [target_root, "TV", t, season_dir]
    else:
        g = sanitize_component(rec.get("genre") or "Unclassified")
        sg = sanitize_component(rec.get("subgenre") or "General")
        base = [target_root] + (["TV"] if split else []) \
            + [g, sg, t, season_dir]
    if rec.get("season_pack") or rec.get("episode") is None:
        return os.path.join(*(base + [rec["name"]]))
    eps = rec.get("episodes") or [rec["episode"]]
    tag = f"S{season:02d}" + "".join(f"E{e:02d}" for e in eps)
    ep = f"{t} - {tag}{rec['ext']}"
    return os.path.join(*(base + [ep]))


def compute_plan(params):
    ensure_state()
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
    if not recs or not root:
        return None, "No cinema scan results. Run a scan first."
    action = (params or {}).get("action", "move")
    if action not in ("move", "copy"):
        return None, "Invalid action."
    target_root = (params or {}).get("targetRoot") or os.path.join(root, "Organized")
    target_root = os.path.abspath(target_root)
    if normcase_abs(target_root) == normcase_abs(root):
        return None, "Target root must differ from the scanned folder."

    # best copy per group: quality_score desc, then size desc
    best_of = {}
    gid_members = {}
    for r in recs:
        gid = groups.get(r["path"])
        if gid:
            gid_members.setdefault(gid, []).append(r)
    for gid, members in gid_members.items():
        ranked = sorted(members, key=lambda r: (-(r.get("quality_score") or 0),
                                                -(r.get("size") or 0), r["path"]))
        best_of[gid] = ranked[0]["path"]

    # What this library is supposed to hold. Anything of the other kind is
    # quarantined to _Movies\ / _TV\ instead of being filed into the wrong
    # tree, so it can be relocated to its real home. "any" = mixed library,
    # file movies and TV side by side (previous behaviour).
    expect_kind = ((params or {}).get("expectKind") or "any").strip().lower()
    if expect_kind not in ("any", "movie", "tv"):
        return None, "Invalid expectKind (use 'any', 'movie' or 'tv')."
    # Layout: splitByKind roots movies under Movies\ and episodes under TV\ so
    # a jumbled library actually separates, instead of interleaving both kinds
    # inside shared Genre\Sub-genre folders. movieYearFolder keeps the extra
    # YYYY level under the movie genre (off = Movies\Genre\Sub\Title (Year)\).
    split_by_kind = bool((params or {}).get("splitByKind"))
    movie_year_folder = bool((params or {}).get("movieYearFolder", True))
    # writeNfo: generate Kodi/Plex-readable .nfo metadata sidecars for
    # identified movies/episodes that don't already ship one (plus one
    # tvshow.nfo per series). Created files are undo-logged (action "nfo" ->
    # undo deletes them). Documentaries / concert films / music videos are
    # movie-kind records and get movie NFOs like any other film.
    write_nfo = bool((params or {}).get("writeNfo"))
    # layout "plex" = the flat structure Plex's scanners expect:
    #   Movies\Title (Year)\Title (Year).ext
    #   TV\Show\Season 01\Show - S01E01.ext   (season 0 -> Specials\)
    # layout "genre" = the Genre\Sub-genre tree (splitByKind/movieYearFolder
    # apply there). Default stays "genre" for API back-compat; the UI sends
    # "plex" by default.
    layout = ((params or {}).get("layout") or "genre").strip().lower()
    if layout not in ("plex", "genre"):
        return None, "Invalid layout (use 'plex' or 'genre')."

    def _x(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    def _movie_nfo(r):
        lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
                 "<movie>", f"  <title>{_x(r['title'])}</title>"]
        if r.get("year"):
            lines.append(f"  <year>{r['year']}</year>")
        for gname in (r.get("genre"), r.get("subgenre")):
            if gname and gname not in ("Unclassified", "General"):
                lines.append(f"  <genre>{_x(gname)}</genre>")
        lines.append("</movie>")
        return "\n".join(lines) + "\n"

    def _episode_nfo(r):
        blocks = []
        for ep in (r.get("episodes") or [r.get("episode")]):
            if ep is None:
                continue
            blocks += ["<episodedetails>",
                       f"  <showtitle>{_x(r['title'])}</showtitle>",
                       f"  <season>{r['season']}</season>",
                       f"  <episode>{ep}</episode>",
                       "</episodedetails>"]
        if not blocks:
            return None
        return ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
                + "\n".join(blocks) + "\n")

    def _tvshow_nfo(r):
        lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
                 "<tvshow>", f"  <title>{_x(r['title'])}</title>"]
        for gname in (r.get("genre"), r.get("subgenre")):
            if gname and gname not in ("Unclassified", "General"):
                lines.append(f"  <genre>{_x(gname)}</genre>")
        lines.append("</tvshow>")
        return "\n".join(lines) + "\n"

    nfo_count = 0
    tvshow_done = set()          # series dirs that already got a tvshow.nfo
    entries = []
    counts = {"dupe": 0, "sample": 0, "clutter": 0, "unidentified": 0,
              "crossMovie": 0, "crossTv": 0}
    for r in sorted(recs, key=lambda x: x["path"]):
        gid = groups.get(r["path"])
        entry = {"from": r["path"], "kind": r["kind"],
                 "isDupe": False, "groupId": gid, "reason": None,
                 "companions": [{"from": c["from"], "to": None,
                                 "suffix": c.get("suffix"),
                                 "keepName": bool(c.get("keepName"))}
                                for c in (r.get("companions") or [])]}
        if r["kind"] == "clutter":
            entry.update(to=os.path.join(target_root, "_Clutter", r["name"]),
                         reason="clutter")
            counts["clutter"] += 1
        elif r.get("is_sample"):
            entry.update(to=os.path.join(target_root, "_Samples", r["name"]),
                         reason="sample")
            counts["sample"] += 1
        elif gid and r["path"] != best_of.get(gid):
            entry.update(to=os.path.join(target_root, "_Duplicates", gid, r["name"]),
                         isDupe=True, reason="dupe")
            counts["dupe"] += 1
        elif expect_kind == "tv" and r["kind"] == "movie":
            # A movie turned up in a TV library: don't file it into the TV
            # tree -- quarantine it so it can be moved to the real movie
            # library (same idea as _Duplicates / _Unidentified).
            entry.update(to=os.path.join(target_root, "_Movies", r["name"]),
                         reason="cross-movie")
            counts["crossMovie"] += 1
        elif expect_kind == "movie" and r["kind"] == "tv":
            entry.update(to=os.path.join(target_root, "_TV", r["name"]),
                         reason="cross-tv")
            counts["crossTv"] += 1
        elif r["kind"] == "movie":
            entry["to"] = movie_dest(r, target_root, split=split_by_kind,
                                     year_folder=movie_year_folder,
                                     layout=layout)
        elif r["kind"] == "tv":
            entry["to"] = tv_dest(r, target_root, split=split_by_kind,
                                  layout=layout)
        else:
            entry.update(to=os.path.join(target_root, "_Unidentified", r["name"]),
                         reason="unidentified")
            counts["unidentified"] += 1
        # generated NFO sidecars for identified, normally-filed movies/eps
        if write_nfo and entry["reason"] is None \
                and r["kind"] in ("movie", "tv") and r.get("title"):
            has_nfo = any(str(c.get("from") or "").lower().endswith(".nfo")
                          for c in entry["companions"])
            if not has_nfo:
                if r["kind"] == "movie":
                    content = _movie_nfo(r)
                elif r.get("season") is not None:
                    content = _episode_nfo(r)
                else:
                    content = None
                if content:
                    entry["companions"].append(
                        {"from": None, "to": None, "suffix": ".nfo",
                         "keepName": False, "generate": "nfo",
                         "content": content})
                    nfo_count += 1
            if r["kind"] == "tv":
                sdir = os.path.dirname(os.path.dirname(entry["to"]))
                if sdir not in tvshow_done:
                    tvshow_done.add(sdir)
                    entry["companions"].append(
                        {"from": None, "to": None, "keepName": False,
                         "generate": "nfo", "content": _tvshow_nfo(r),
                         "absTo": os.path.join(sdir, "tvshow.nfo")})
                    nfo_count += 1
        dest_dir = os.path.dirname(entry["to"])
        stem = os.path.splitext(os.path.basename(entry["to"]))[0]
        for comp in entry["companions"]:
            if comp.get("absTo"):
                comp["to"] = comp["absTo"]
            elif comp.get("keepName"):
                # poster.jpg / fanart.jpg / movie.nfo keep their own names
                # beside the video -- exactly what Plex scans for
                comp["to"] = os.path.join(dest_dir,
                                          os.path.basename(comp["from"]))
            else:
                # rename to the video's destination stem, preserving the
                # matched remainder ("Movie.en.srt" -> "<stem>.en.srt")
                sfx = comp.get("suffix") or os.path.splitext(comp["from"])[1]
                comp["to"] = os.path.join(dest_dir, stem + sfx)
        entries.append(entry)

    folders = {os.path.dirname(e["to"]) for e in entries}
    stats = {"totalFiles": len(entries),
             "dupeFiles": counts["dupe"], "sampleFiles": counts["sample"],
             "clutterFiles": counts["clutter"],
             "unidentifiedFiles": counts["unidentified"],
             "crossMovieFiles": counts["crossMovie"],
             "crossTvFiles": counts["crossTv"],
             "nfoFiles": nfo_count,
             "writeNfo": write_nfo,
             "expectKind": expect_kind,
             "splitByKind": split_by_kind,
             "movieYearFolder": movie_year_folder,
             "layout": layout,
             "companionFiles": sum(len(e["companions"]) for e in entries),
             "foldersToCreate": len(folders),
             "targetRoot": target_root, "action": action,
             "scannedRoot": root}
    plan = {"entries": entries, "stats": stats,
            "params": {"action": action, "targetRoot": target_root,
                       "scannedRoot": root, "expectKind": expect_kind}}
    with LOCK:
        STATE["plan"] = plan
    return plan, None


# =================================================================== execute

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
                    import shutil as _sh
                    _sh.move(src, actual)
                    moved += 1
                else:
                    import shutil as _sh
                    _sh.copy2(src, actual)
                    copied += 1
                exec_log(f"{action.upper()} {src} -> {actual}")
                manifest.append({"from": src, "to": actual, "action": action})
                for comp in e.get("companions") or []:
                    if comp.get("generate") == "nfo":
                        try:
                            cdst = comp["to"]
                            if cdst and not os.path.exists(cdst):
                                os.makedirs(os.path.dirname(cdst),
                                            exist_ok=True)
                                with open(cdst, "w", encoding="utf-8") as nf:
                                    nf.write(comp.get("content") or "")
                                manifest.append({"from": None, "to": cdst,
                                                 "action": "nfo",
                                                 "companion": True})
                                exec_log(f"NFO {cdst}")
                        except Exception as cx:
                            exec_log(f"ERROR nfo {comp.get('to')}: "
                                     f"{type(cx).__name__}: {cx}")
                        continue
                    csrc, cdst = comp["from"], comp["to"]
                    try:
                        if not os.path.isfile(csrc):
                            continue
                        cactual = resolve_collision(cdst, csrc)
                        os.makedirs(os.path.dirname(cactual), exist_ok=True)
                        if action == "move":
                            import shutil as _sh
                            _sh.move(csrc, cactual)
                        else:
                            import shutil as _sh
                            _sh.copy2(csrc, cactual)
                        manifest.append({"from": csrc, "to": cactual,
                                         "action": action, "companion": True})
                        exec_log(f"{action.upper()} companion {csrc} -> {cactual}")
                    except Exception as cx:
                        exec_log(f"ERROR companion {csrc}: "
                                 f"{type(cx).__name__}: {cx}")
            except Exception as ex:
                errors += 1
                exec_log(f"ERROR {src}: {type(ex).__name__}: {ex}")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        undo_name = f"undo_log_cinema_{ts}.json"
        undo_path = os.path.join(DATA_DIR, undo_name)
        payload = {"app": "cinema", "version": 1,
                   "created": datetime.now().isoformat(sep=" "),
                   "action": action, "scannedRoot": scanned_root,
                   "targetRoot": target_root, "entries": manifest,
                   "stats": {"moved": moved, "copied": copied,
                             "skipped": skipped, "errors": errors,
                             "cancelled": cancelled}}
        tmp = undo_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, undo_path)
        undo_copy = None
        try:
            import shutil as _sh
            undo_copy = os.path.join(target_root, undo_name)
            _sh.copyfile(undo_path, undo_copy)
        except OSError:
            undo_copy = None
        with LOCK:
            STATE["lastUndo"] = undo_path
        if action == "move":
            try:
                db_update_paths(manifest, "move")
            except Exception:
                pass
        set_exec(state="cancelled" if cancelled else "done",
                 processed=len(manifest), currentFile="",
                 result={"moved": moved, "copied": copied, "skipped": skipped,
                         "errors": errors, "cancelled": cancelled,
                         "undoFile": undo_path, "undoCopy": undo_copy})
    except Exception as e:
        set_exec(state="error", error=f"{type(e).__name__}: {e}")


def start_execute():
    with LOCK:
        if STATE["execute"]["state"] == "running":
            return False, "Execution already running."
        if not STATE["plan"]:
            return False, "No plan. Build a plan preview first."
        STATE["execute"] = {"state": "running", "total": 0, "processed": 0,
                            "currentFile": "", "error": None, "log": [],
                            "result": None}
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


def run_undo(manifest_path):
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return None, f"cannot read manifest: {e}"
    restored = deleted = skipped = errors = 0
    for e in reversed(manifest.get("entries", [])):
        src, dst, act = e["from"], e["to"], e.get("action", "move")
        try:
            if act == "move":
                if not os.path.isfile(dst):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(src), exist_ok=True)
                import shutil as _sh
                _sh.move(dst, src)
                restored += 1
            else:
                if os.path.isfile(dst):
                    os.remove(dst)
                    deleted += 1
                else:
                    skipped += 1
        except Exception:
            errors += 1
    try:
        db_update_paths([e for e in manifest.get("entries", [])
                         if e.get("action", "move") == "move"], "restore")
    except Exception:
        pass
    # remove now-empty dirs under the target root (bottom-up, root excluded)
    troot = manifest.get("targetRoot")
    if troot and os.path.isdir(troot):
        # drop undo-manifest copies so the tree can empty out completely
        try:
            for fn in os.listdir(troot):
                if fn.startswith("undo_log_cinema_") and fn.endswith(".json"):
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
