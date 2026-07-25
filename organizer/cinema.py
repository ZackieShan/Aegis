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
TVSUP_CANCEL = threading.Event()
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
    "tvsupervise": {"state": "idle", "total": 0, "processed": 0,
                    "identified": 0, "rejected": 0, "currentFile": "",
                    "error": None, "root": "", "log": []},
    # one-click chain: scan -> supervisor -> dupe regroup + quality audit.
    # phases: [{name, state: pending|running|done|skipped|error, detail}]
    "pipeline": {"state": "idle", "phase": "", "phases": [], "error": None},
    "dupeAudit": None,     # audit_dupe_groups() result (survives via meta)
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


try:
    import llm_tv
except Exception:            # LLM assist optional
    llm_tv = None

try:
    import llm_reidentify     # movie verification (3-gate TMDB check)
except Exception:
    llm_reidentify = None

try:
    import llm_assist         # local-LLM reachability probe for the pipeline
except Exception:
    llm_assist = None


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


_FOLDER_INDEX_PREFIX = re.compile(
    r"^\s*(0\d{1,2}|\d{1,3})\s*(?:([-._)\]:]+)\s*|\s+)(?=\S)")


def strip_index_prefix(s):
    """Drop a leading track/disc/episode index from a name meant to become a
    FOLDER ("01 - Greatest Hits" -> "Greatest Hits", "03. The Wall" ->
    "The Wall").

    Deliberately conservative: plenty of real titles start with a number, so
    the digits must LOOK like an index — either zero-padded ("01", "007") or
    followed by a separator ("01 - ", "03."). A bare "22 Jump Street",
    "13 Assassins", "12 Monkeys", "300" or "1917" is left alone, and the
    remainder must still contain a word.
    """
    s = str(s or "")
    m = _FOLDER_INDEX_PREFIX.match(s)
    if not m:
        return s
    digits, sep = m.group(1), m.group(2)
    zero_padded = len(digits) > 1 and digits[0] == "0"
    if not (zero_padded or sep):
        return s                        # bare "22 Jump Street" -> keep
    rest = s[m.end():].strip()
    if len(rest) < 3 or not re.search(r"[A-Za-z]{2}", rest):
        return s                        # "50-50" -> keep
    return rest


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


# "030 - Title", "12. Title", "007_Title": an episode number leads the name
# but the SERIES name isn't in the file at all — it lives in the folder tree.
_EPISODE_LED_RE = re.compile(r"^\s*(\d{1,3})\s*[-._)\]]\s*\S")
_SEASON_DIR_RE = re.compile(
    r"^(season[\s._-]*\d+|s\d{1,2}|specials?|disc[\s._-]*\d+|cd\d+)$", re.I)


def _series_from_dir(dirname):
    """A series title from a folder name, or None when the folder doesn't
    look like a real title. Requires at least two alphabetic words: release
    folders squashed into one run-on token ("036mysticmanorflopgoes...")
    would otherwise become junk series names."""
    cleaned = _clean_title(_strip_tokens(strip_index_prefix(dirname)))
    if not cleaned:
        return None
    words = [w for w in re.split(r"\s+", cleaned) if w]
    alpha_words = [w for w in words if re.search(r"[A-Za-z]", w)]
    if len(alpha_words) < 2 or len(cleaned) < 4:
        return None
    if len(max(alpha_words, key=len)) > 24:
        return None                 # run-on token: not a real title
    return cleaned


def infer_tv_from_folder(rec, scan_root):
    """Promote an unidentified, episode-led file to TV using the folder tree
    for the series name (Show\\Season 01\\03 - Title.mkv). Absolute numbering
    lands in season 1. Returns True when the rec was updated."""
    if rec.get("kind") != "unknown" or rec.get("season") is not None:
        return False
    stem = os.path.splitext(rec["name"])[0]
    m = _EPISODE_LED_RE.match(stem)
    if not m:
        return False
    ep = int(m.group(1))
    if ep <= 0:
        return False
    d = os.path.dirname(rec["path"])
    root_n = normcase_abs(scan_root)
    season = None
    while d and normcase_abs(d) != root_n and len(d) > 3:
        base = os.path.basename(d)
        sm = _SEASON_DIR_RE.match(base.strip())
        if sm:
            num = re.search(r"\d+", base)
            if num and season is None:
                season = int(num.group(0))
            d = os.path.dirname(d)
            continue
        title = _series_from_dir(base)
        if title:
            rec["kind"] = "tv"
            rec["title"] = title
            rec["season"] = season if season is not None else 1
            rec["episode"] = ep
            rec["episodes"] = [ep]
            rec["tags"] = (rec.get("tags") or []) + ["folder-series"]
            return True
        break        # nearest meaningful folder didn't look like a title
    return False


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
-- Persistent multi-root identity index: every COMPLETED scan refreshes its
-- own root's rows, so an inbox scan can ask "do I already own this movie /
-- episode?" against the main library's last scan.
CREATE TABLE IF NOT EXISTS library_index (
  path TEXT PRIMARY KEY,
  root TEXT,
  kind TEXT,
  title_norm TEXT,
  year INTEGER,
  season INTEGER,
  episode INTEGER,
  md5 TEXT,
  quality_score INTEGER,
  size_bytes INTEGER,
  indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_clibidx_root ON library_index(root);
CREATE INDEX IF NOT EXISTS idx_clibidx_md5 ON library_index(md5);
CREATE INDEX IF NOT EXISTS idx_clibidx_ident ON library_index(title_norm);
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


def _ident_key(rec):
    """Identity key for cross-root matching: movies by title+year, episodes
    by series+season+episode. None for unidentified / season packs."""
    t = normalize_title(rec.get("title") or "")
    if not t:
        return None
    if rec.get("kind") == "movie" and rec.get("year"):
        return ("movie", t, rec["year"])
    if rec.get("kind") == "tv" and rec.get("season") is not None \
            and rec.get("episode") is not None:
        return ("tv", t, rec["season"], rec["episode"])
    return None


def db_update_library_index(recs, root, scanned_at):
    """Refresh the persistent identity index for ONE root, leaving other
    roots' rows untouched."""
    prefix = normcase_abs(root) + os.sep
    with _db() as con:
        for (p,) in con.execute("SELECT path FROM library_index").fetchall():
            if normcase_abs(p).startswith(prefix):
                con.execute("DELETE FROM library_index WHERE path=?", (p,))
        for r in recs:
            if r.get("kind") not in ("movie", "tv"):
                continue
            con.execute(
                "INSERT OR REPLACE INTO library_index"
                " (path, root, kind, title_norm, year, season, episode,"
                "  md5, quality_score, size_bytes, indexed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["path"], root, r["kind"],
                 normalize_title(r.get("title") or ""), r.get("year"),
                 r.get("season"), r.get("episode"), r.get("md5"),
                 r.get("quality_score"), r.get("size"), scanned_at))


def find_in_library(recs, root):
    """{rec path -> (library path, verdict)} for scanned files already owned
    under a DIFFERENT indexed root. verdict "dupe" = same bytes or same
    identity at equal-or-lower quality; "upgrade" = same identity but this
    copy scores better than the library's."""
    prefix = normcase_abs(root) + os.sep
    by_md5, by_ident = {}, {}
    try:
        with _db() as con:
            for row in con.execute("SELECT * FROM library_index"):
                if normcase_abs(row["path"]).startswith(prefix):
                    continue
                if row["md5"]:
                    by_md5.setdefault(row["md5"], row["path"])
                k = _ident_key(dict(row, kind=row["kind"],
                                    title=row["title_norm"]))
                if k:
                    prev = by_ident.get(k)
                    if prev is None or (row["quality_score"] or 0) > prev[1]:
                        by_ident[k] = (row["path"], row["quality_score"] or 0)
    except Exception:
        return {}
    out = {}
    alive = {}
    def _isfile(p):
        if p not in alive:
            alive[p] = os.path.isfile(p)
        return alive[p]
    for r in recs:
        md5_hit = by_md5.get(r.get("md5"))
        if md5_hit and _isfile(md5_hit):
            out[r["path"]] = (md5_hit, "dupe")
            continue
        k = _ident_key(r)
        hit = by_ident.get(k) if k else None
        if hit and _isfile(hit[0]):
            if (r.get("quality_score") or 0) > hit[1]:
                out[r["path"]] = (hit[0], "upgrade")
            else:
                out[r["path"]] = (hit[0], "dupe")
    return out


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


def save_config(tmdb_key=None, tmdb_token=None, omdb_key=None,
                tvdb_key=None, tvdb_pin=None):
    """Persist API credentials. None leaves a field unchanged, '' clears
    it, any other value sets it. Values are never logged."""
    cfg = load_config()
    if tmdb_key is not None:
        cfg["tmdbKey"] = tmdb_key.strip()
    if tmdb_token is not None:
        cfg["tmdbToken"] = tmdb_token.strip()
    if omdb_key is not None:
        cfg["omdbKey"] = omdb_key.strip()
    if tvdb_key is not None:
        cfg["tvdbKey"] = tvdb_key.strip()
    if tvdb_pin is not None:
        cfg["tvdbPin"] = tvdb_pin.strip()
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
    omdb = cfg.get("omdbKey") or ""
    tvdb = cfg.get("tvdbKey") or ""
    return {"hasKey": bool(key or tok), "hasApiKey": bool(key),
            "hasToken": bool(tok), "tmdbKeyMasked": mask_secret(key),
            "tmdbTokenMasked": mask_secret(tok),
            "hasOmdbKey": bool(omdb), "omdbKeyMasked": mask_secret(omdb),
            "hasTvdbKey": bool(tvdb), "tvdbKeyMasked": mask_secret(tvdb),
            "hasTvdbPin": bool(cfg.get("tvdbPin"))}


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

DISC_DIR_NAMES = {"video_ts", "bdmv"}


def collect_files(root, max_files):
    """(files, discs). A folder containing VIDEO_TS\\ or BDMV\\ is ONE title
    (a DVD/Blu-ray rip): its whole subtree is pruned from per-file collection
    and returned as an atomic disc unit instead — otherwise the rip's
    .m2ts/.vob internals would be scanned as unidentifiable videos and the
    structure torn apart. Standalone .iso images are disc units too."""
    out, discs = [], []
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        lower = {d.lower(): d for d in dirnames}
        hit = next((lower[k] for k in ("video_ts", "bdmv") if k in lower),
                   None)
        if hit is not None:
            fmt = "dvd" if hit.lower() == "video_ts" else "bluray"
            if normcase_abs(dirpath) == normcase_abs(root_abs):
                # the scan root itself IS the rip: the unit is the disc dir
                discs.append({"path": os.path.join(dirpath, hit),
                              "format": fmt})
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in DISC_DIR_NAMES]
            else:
                discs.append({"path": dirpath, "format": fmt})
                dirnames[:] = []      # never descend into a rip
                continue              # its top-level files belong to it too
        dirnames.sort()
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".iso":
                discs.append({"path": os.path.join(dirpath, fn),
                              "format": "iso"})
                continue
            if ext in VIDEO_EXTS or ext in SUB_EXTS or ext in CLUTTER_EXTS:
                out.append(os.path.join(dirpath, fn))
                if max_files and len(out) >= max_files:
                    return out, discs
    return out, discs


def _dir_size(path):
    total = 0
    for dp, _dn, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


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
        files, discs = collect_files(root, max_files)
        set_scan(total=len(files) + len(discs), processed=0)
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
                # "Show\Season 01\03 - Title.mkv": the episode number leads
                # the filename and the series lives in the folder tree
                infer_tv_from_folder(rec, root)
                if hash_enabled and st.st_size < HASH_MAX:
                    rec["md5"] = md5_file(path, SCAN_CANCEL)
                videos.append(len(recs))
                recs.append(rec)
            elif ext in SUB_EXTS:
                continue  # subtitles attach to their video below
            else:
                recs.append({**base, "kind": "_clutter_candidate"})

        # disc-rip units (VIDEO_TS / BDMV folders, .iso images): ONE record
        # per title, moved whole at execute. Identity parses from the rip's
        # folder (or iso file) name; joins dedupe/identify like any movie.
        for d in discs:
            if SCAN_CANCEL.is_set():
                cancelled = True
                break
            p = d["path"]
            name = os.path.basename(p)
            is_iso = d["format"] == "iso"
            set_scan(currentFile=name)
            try:
                if is_iso:
                    st = os.stat(p)
                    size, mtime = st.st_size, st.st_mtime
                else:
                    size = _dir_size(p)
                    mtime = os.path.getmtime(p)
            except OSError:
                continue
            # for a VIDEO_TS/BDMV unit at the scan root, the parent folder
            # carries the title; otherwise the unit folder itself does
            if not is_iso and name.lower() in DISC_DIR_NAMES:
                src_name = os.path.basename(os.path.dirname(p)) or name
            else:
                src_name = name
            parsed = parse_media_name(src_name if is_iso
                                      else src_name + ".mkv")
            rec = {"path": p, "name": name,
                   "ext": ".iso" if is_iso else "",
                   "size": size, "mtime": mtime, "md5": None, "error": None,
                   **parsed}
            rec["is_disc"] = True
            rec["disc_format"] = d["format"]
            rec["tags"] = (rec.get("tags") or []) + ["disc:" + d["format"]]
            if rec["kind"] in ("movie", "tv"):
                # quality floor when the rip's name carries no res tags:
                # a Blu-ray outranks web 1080p sources, a DVD sits at SD
                floor = {"bluray": 3095, "dvd": 1060}.get(d["format"], 0)
                rec["quality_score"] = max(rec.get("quality_score") or 0,
                                           floor)
            recs.append(rec)

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
            # a fresh rec set invalidates the previous run's audit — the
            # pipeline's phase 3 recomputes it moments later. The meta copy
            # is only cleared on a COMPLETE scan (a cancelled one keeps the
            # DB's last complete rows, which the stored audit still matches)
            STATE["dupeAudit"] = None
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
                db_update_library_index(recs, root, scanned_at)
                set_meta({"last_scan_root": root,
                          "last_scan_completed_at": scanned_at,
                          "last_scan_count": str(len(recs)),
                          "last_scan_partial": "0",
                          "dupe_audit": "null"})
        except Exception:
            pass
        if cancelled:
            set_scan(state="cancelled", processed=len(recs), currentFile="")
        else:
            set_scan(state="done", processed=len(files), currentFile="")
    except Exception as e:
        set_scan(state="error", error=f"{type(e).__name__}: {e}")


def _title_words(s):
    return set(w for w in normalize_title(s).split() if len(w) > 3)


def tmdb_episode_titles(series_title, api_key, token=None, fetcher=None,
                        cancel=None, max_seasons=3):
    """Episode names for a series (first few seasons). [] when unavailable."""
    fetcher = _resolve_fetcher(fetcher, token)
    try:
        url = ("https://api.themoviedb.org/3/search/tv"
               f"?{_key_qs(api_key)}query="
               f"{urllib.parse.quote(_fold(series_title))}")
        data = _throttled(url, fetcher) or {}
        hits = data.get("results") or []
        if not hits:
            return []
        tv_id = hits[0].get("id")
        if not tv_id:
            return []
        names = []
        nseasons = min(max_seasons,
                       max(1, int(hits[0].get("number_of_seasons") or 1)))
        for season in range(1, nseasons + 1):
            if cancel is not None and cancel.is_set():
                break
            surl = (f"https://api.themoviedb.org/3/tv/{tv_id}/season/{season}"
                    f"?{_key_qs(api_key)}")
            sdata = _throttled(surl, fetcher) or {}
            for ep in sdata.get("episodes") or []:
                nm = ep.get("name")
                if nm:
                    names.append(nm)
        return names
    except Exception:
        return []


def episode_titles_match(filenames, tmdb_titles, min_hits=1):
    """True when at least `min_hits` episode titles from the filenames also
    appear in the series' TMDB episode list.

    This is the check that separates "that show exists" from "that show
    actually has these episodes" — the difference between accepting and
    rejecting a confident hallucination.
    """
    if not tmdb_titles:
        return None                 # can't verify (no data) -> caller decides
    tmdb_sets = [_title_words(t) for t in tmdb_titles if t]
    tmdb_sets = [s for s in tmdb_sets if s]
    if not tmdb_sets:
        return None
    hits = 0
    for fn in filenames:
        stem = os.path.splitext(os.path.basename(fn))[0]
        stem = re.sub(r"^\s*\d{1,3}\s*[-._)\]]\s*", "", stem)
        stem = _strip_tokens(stem)
        # segment-title files list several titles separated by commas
        for part in re.split(r"[,;]| - ", stem):
            words = _title_words(part)
            if len(words) < 2:
                continue
            for t in tmdb_sets:
                overlap = words & t
                if len(overlap) >= 2 or (words and words <= t):
                    hits += 1
                    break
            if hits >= min_hits:
                return True
    return hits >= min_hits


# ---------------------------------------------- second-source verification
# TMDB is the canonical database, but a TMDB miss shouldn't doom a correct
# identification: TVmaze (keyless) and TheTVDB (legacy key) provide second
# episode lists for the cross-check, and OMDb (IMDb data) a second movie
# database. All are total-failure-tolerant — any problem returns empty and
# the caller behaves exactly as before the source existed.

def _get_json(url, headers=None, timeout=20):
    """Tolerant GET -> parsed JSON dict; {} on ANY failure. Never raises."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "AegisOrganizer/1.0 (personal media tool)",
            **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")) or {}
    except Exception:
        return {}


def tvmaze_episode_titles(series_title, cancel=None):
    """Episode names from TVmaze (keyless, CC BY-SA, ~20 req/10s).
    [] when the show isn't found or the found show's name doesn't pass the
    similarity gate (a fuzzy TVmaze match for a DIFFERENT show must not
    become verification evidence)."""
    if not series_title or (cancel is not None and cancel.is_set()):
        return []
    q = urllib.parse.quote(_fold(series_title))
    data = _get_json("https://api.tvmaze.com/singlesearch/shows?q=" + q
                     + "&embed=episodes")
    if not _sim_accept(series_title, data.get("name") or ""):
        return []
    eps = (data.get("_embedded") or {}).get("episodes") or []
    return [e.get("name") for e in eps if e.get("name")]


_TVDB_TOKEN = {"token": None, "born": 0.0}


def _tvdb_login(api_key):
    """Bearer token for TheTVDB's legacy (v3) API, cached ~20h (tokens live
    24h). '' when login fails or the v3 API is finally retired."""
    now = time.time()
    if _TVDB_TOKEN["token"] and now - _TVDB_TOKEN["born"] < 20 * 3600:
        return _TVDB_TOKEN["token"]
    try:
        req = urllib.request.Request(
            "https://api.thetvdb.com/login",
            data=json.dumps({"apikey": api_key}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok = (json.loads(resp.read().decode()) or {}).get("token") or ""
    except Exception:
        tok = ""
    if tok:
        _TVDB_TOKEN.update(token=tok, born=now)
    return tok


def tvdb_episode_titles(series_title, api_key, cancel=None, max_pages=3):
    """Episode names from TheTVDB legacy v3 API. [] when unavailable."""
    if not series_title or not api_key:
        return []
    if cancel is not None and cancel.is_set():
        return []
    tok = _tvdb_login(api_key)
    if not tok:
        return []
    hdr = {"Authorization": "Bearer " + tok}
    q = urllib.parse.quote(_fold(series_title))
    data = _get_json("https://api.thetvdb.com/search/series?name=" + q, hdr)
    sid = None
    for h in (data.get("data") or [])[:5]:
        if _sim_accept(series_title, h.get("seriesName") or ""):
            sid = h.get("id")
            break
    if not sid:
        return []
    names = []
    for page in range(1, max_pages + 1):
        if cancel is not None and cancel.is_set():
            break
        data = _get_json(f"https://api.thetvdb.com/series/{sid}/episodes"
                         f"?page={page}", hdr)
        for ep in data.get("data") or []:
            nm = ep.get("episodeName")
            if nm:
                names.append(nm)
        if not (data.get("links") or {}).get("next"):
            break
    return names


def verify_episode_titles(filenames, series_title, api_key, token,
                          tvdb_key=None, cancel=None):
    """Cross-check proposed episodes against every available episode-list
    source. Returns (verdict, source): True as soon as ANY source's episode
    list matches the filenames; False when at least one source HAD data and
    none matched; None when no source had data. Extra sources reduce both
    false accepts (TMDB-exists-but-wrong-show) and false REJECTS (right
    show, sparse TMDB episode data)."""
    verdict, via = None, ""
    # the alt sources return FULL episode lists (hundreds of titles), so a
    # single 2-word fluke match must not confirm a wrong series: multi-file
    # folders need two independent filename<->episode hits
    min_hits = 1 if len(filenames) < 2 else 2
    for name, titles in (
            ("tmdb", lambda: tmdb_episode_titles(series_title, api_key,
                                                 token, cancel=cancel)),
            ("tvmaze", lambda: tvmaze_episode_titles(series_title,
                                                     cancel=cancel)),
            ("tvdb", lambda: tvdb_episode_titles(series_title, tvdb_key,
                                                 cancel=cancel))):
        if name == "tvdb" and not tvdb_key:
            continue
        v = episode_titles_match(filenames, titles(), min_hits=min_hits)
        if v is True:
            return True, name
        if v is False and verdict is None:
            verdict, via = False, name
    return verdict, via


def omdb_verify(crack, omdb_key, file_guess=None):
    """OMDb (IMDb data) as a SECOND movie database when TMDB can't confirm
    the LLM's guess. Applies the same three gates as verify_with_tmdb:
    title similarity, year within 1, and the filename anchor that blocks
    self-consistent hallucinations. Returns verify_with_tmdb's shape or
    None."""
    if not omdb_key or llm_reidentify is None:
        return None
    title, year = crack.get("title"), crack.get("year")
    if not title:
        return None
    anchor = file_guess or title
    queries = [(title, year), (title, None)] if year else [(title, None)]
    for q, qy in queries:
        url = ("https://www.omdbapi.com/?apikey="
               + urllib.parse.quote(omdb_key)
               + "&t=" + urllib.parse.quote(_fold(q))
               + (f"&y={qy}" if qy else ""))
        data = _get_json(url)
        if (data.get("Response") or "").lower() != "true":
            continue
        if (data.get("Type") or "movie") not in ("movie", "series"):
            continue
        canon = data.get("Title") or ""
        hy = (data.get("Year") or "")[:4]
        if not canon or not hy.isdigit():
            continue
        if not _sim_accept(title, canon):
            continue
        if year and abs(int(hy) - year) > 1:
            continue
        if not llm_reidentify._filename_match(anchor, canon):
            continue
        genres = [g.strip() for g in (data.get("Genre") or "").split(",")
                  if g.strip() and g.strip() != "N/A"]
        return {"kind": "tv" if data.get("Type") == "series" else "movie",
                "title": canon, "year": int(hy),
                "genre": genres[0] if genres else "Unclassified",
                "subgenre": genres[1] if len(genres) > 1 else "General"}
    return None


def unidentified_scope(path=None):
    """What the supervisor would work on: how many unidentified files, and
    the deepest folder that contains them all (so the UI can show a concrete
    target instead of leaving the user guessing). `path` narrows the set."""
    with LOCK:
        recs = [r for r in STATE["recs"] if r.get("kind") == "unknown"
                and not r.get("is_disc")]
        scanned = STATE.get("scannedRoot")
    if path:
        recs = [r for r in recs if is_within(r["path"], path)]
    paths = [r["path"] for r in recs]
    root = ""
    if paths:
        try:
            root = os.path.commonpath([os.path.dirname(p) for p in paths])
        except ValueError:              # different drives
            root = ""
    folders = sorted({os.path.dirname(p) for p in paths})
    return {"count": len(paths), "root": root or (scanned or ""),
            "scannedRoot": scanned or "", "folders": len(folders),
            "samples": [os.path.basename(p) for p in paths[:5]]}


def set_tvsup(**kw):
    with LOCK:
        STATE["tvsupervise"].update(kw)


def tvsupervise_status():
    with LOCK:
        d = dict(STATE["tvsupervise"])
        d["log"] = list(d["log"][-200:])
        return d


def _tvsup_log(msg):
    with LOCK:
        STATE["tvsupervise"]["log"].append(msg)


def run_tv_supervise(min_confidence=None, model=None, path=None):
    """LLM supervisor for EVERYTHING left unidentified (TV and films).

    Groups them by folder (shared context is what makes a series guess
    possible), asks the local model to name the series and map each file to
    a season/episode, then VERIFIES the proposed series against TMDB and
    adopts only TMDB's canonical title. Files whose series can't be
    confirmed stay unidentified. Updates the DB + in-memory recs; it never
    touches a file — organizing still goes through plan -> preview -> undo.
    """
    try:
        if llm_tv is None:
            set_tvsup(state="error", error="LLM assist unavailable.")
            return
        cfg = load_config()
        api_key = (cfg.get("tmdbKey") or "").strip()
        token = (cfg.get("tmdbToken") or "").strip()
        if not (api_key or token):
            set_tvsup(state="error",
                      error="A TMDB key or read token is required — the LLM's "
                            "series guess is only adopted after TMDB confirms it.")
            return
        thr = llm_tv.MIN_CONFIDENCE if min_confidence is None             else float(min_confidence)
        gmaps = {}      # kind -> {genre_id: name}, filled lazily by the verifier
        with LOCK:
            recs = [r for r in STATE["recs"] if r.get("kind") == "unknown"
                    and not r.get("is_disc")]
        if path:
            # scope to one folder (e.g. a previous run's _Unidentified\)
            path = os.path.abspath(os.path.expanduser(str(path).strip()))
            recs = [r for r in recs if is_within(r["path"], path)]
            if not recs:
                set_tvsup(state="error",
                          error=f"No unidentified files under {path}")
                return
        by_dir = {}
        for r in recs:
            by_dir.setdefault(os.path.dirname(r["path"]), []).append(r)
        folders = sorted(by_dir)
        scope = unidentified_scope(path)
        set_tvsup(state="running", total=len(folders), processed=0,
                  identified=0, rejected=0, currentFile="", error=None,
                  root=scope["root"], log=[])
        identified = rejected = 0
        for i, d in enumerate(folders):
            if TVSUP_CANCEL.is_set():
                set_tvsup(state="cancelled", processed=i)
                return
            members = by_dir[d]
            folder = os.path.basename(d) or d
            set_tvsup(processed=i, currentFile=folder)
            names = [m["name"] for m in members]
            try:
                out = llm_tv.supervise_folder(folder, names, model=model)
            except Exception as e:
                out = None
                _tvsup_log(f"ERROR {folder}: {type(e).__name__}: {e}")
            if not out or out["confidence"] < thr:
                rejected += len(members)
                set_tvsup(rejected=rejected)
                _tvsup_log(f"no answer: {folder}")
                continue

            hits = 0
            if out["kind"] == "movie":
                # Films: reuse the movie verifier's three gates (TMDB title
                # similarity, year within 1, and a FILENAME anchor that stops
                # self-consistent hallucinations). TMDB supplies the title.
                for m in members:
                    if TVSUP_CANCEL.is_set():
                        break
                    proposed = out["movies"].get(m["name"])
                    if not proposed:
                        continue
                    crack = {"title": proposed[0], "year": proposed[1],
                             "kind": "movie", "confidence": out["confidence"]}
                    try:
                        v = llm_reidentify.verify_with_tmdb(
                            crack, api_key, _resolve_fetcher(None, token),
                            gmaps, file_guess=m.get("guess_title") or m["name"])
                        vsrc = "llm+tmdb"
                    except Exception as e:
                        v = None
                        vsrc = ""
                        _tvsup_log(f"ERROR verify {m['name']}: "
                                   f"{type(e).__name__}: {e}")
                    if not v and cfg.get("omdbKey"):
                        # second database: a real film TMDB doesn't know
                        # (or titles it too differently) can still be
                        # confirmed by OMDb/IMDb under the same gates
                        v = omdb_verify(crack, cfg.get("omdbKey"),
                                        file_guess=m.get("guess_title")
                                        or m["name"])
                        if v and v.get("kind") != "movie":
                            # OMDb matched a SERIES — this branch writes
                            # movie records (no season/episode), so a TV
                            # hit must not be adopted here
                            _tvsup_log(f"{m['name']}: OMDb matched a TV "
                                       "series — not adopted as a film")
                            v = None
                        vsrc = "llm+omdb"
                        if v:
                            _tvsup_log(f"{m['name']}: confirmed by OMDb "
                                       "(TMDB had no match)")
                    if not v:
                        continue
                    m["kind"] = "movie"
                    m["title"] = v.get("title")
                    m["year"] = v.get("year")
                    m["genre"] = v.get("genre") or m.get("genre")
                    m["subgenre"] = v.get("subgenre") or m.get("subgenre")
                    m["genre_source"] = vsrc
                    m["tags"] = (m.get("tags") or []) + ["llm-movie"]
                    hits += 1
                if hits:
                    _tvsup_log(f"{folder} -> {hits}/{len(members)} film(s) "
                               "confirmed by TMDB")
                else:
                    _tvsup_log(f"TMDB confirmed no films in {folder}")
            else:
                # TV: series must exist on TMDB *and* the episode titles in
                # the filenames must appear in that series' episode list.
                title, year, genre, subgenre, source = identify_tv(
                    out["series"], None, api_key, token, cancel=TVSUP_CANCEL)
                if not title:
                    rejected += len(members)
                    set_tvsup(rejected=rejected)
                    _tvsup_log(f"TMDB rejected \"{out['series']}\" for {folder}")
                    continue
                verdict, via = verify_episode_titles(
                    names, title, api_key, token,
                    tvdb_key=cfg.get("tvdbKey"), cancel=TVSUP_CANCEL)
                if verdict is False:
                    rejected += len(members)
                    set_tvsup(rejected=rejected)
                    _tvsup_log(f"episode titles don't match \"{title}\" "
                               f"for {folder} ({via}) — not adopted")
                    continue
                if verdict is None:
                    _tvsup_log(f"note: could not cross-check episode titles "
                               f"for \"{title}\" ({folder})")
                elif via != "tmdb":
                    _tvsup_log(f"episode titles confirmed by {via} "
                               f"for \"{title}\"")
                for m in members:
                    se, ep = out["episodes"].get(m["name"], (None, None))
                    if ep is None:
                        ep = llm_tv.fallback_episode(m["name"])
                        se = se or out.get("season") or 1
                    if ep is None:
                        continue
                    m["kind"] = "tv"
                    m["title"] = title
                    m["season"] = se if se is not None else 1
                    m["episode"] = ep
                    m["episodes"] = [ep]
                    m["genre"] = genre or m.get("genre")
                    m["subgenre"] = subgenre or m.get("subgenre")
                    m["genre_source"] = source or "llm+tmdb"
                    m["tags"] = (m.get("tags") or []) + ["llm-tv"]
                    hits += 1
                _tvsup_log(f"{folder} -> {title} "
                           f"({hits}/{len(members)} episodes)")

            identified += hits
            rejected += len(members) - hits
            set_tvsup(identified=identified, rejected=rejected)
        # regroup FIRST: files the supervisor just identified must join
        # dupe groups now, or they escape best-copy selection until the
        # next full rescan. Then persist recs + fresh groups together, and
        # refresh the audit so its group ids match the new numbering.
        groups = _regroup_recs()
        try:
            with LOCK:
                all_recs = list(STATE["recs"])
            db_replace_recs(all_recs, groups,
                            datetime.now().isoformat(sep=" "))
        except Exception as e:
            _tvsup_log(f"WARN db write failed: {type(e).__name__}: {e}")
        audit_dupe_groups()
        set_tvsup(state="done", processed=len(folders), currentFile="",
                  identified=identified, rejected=rejected)
    except Exception as e:
        set_tvsup(state="error", error=f"{type(e).__name__}: {e}")


def start_tv_supervise(min_confidence=None, model=None, path=None):
    if llm_tv is None:
        return False, "LLM assist unavailable."
    with LOCK:
        for k in ("scan", "execute", "tvsupervise", "pipeline"):
            if STATE[k]["state"] == "running":
                return False, f"A {k} is already running."
    TVSUP_CANCEL.clear()
    threading.Thread(target=run_tv_supervise,
                     kwargs={"min_confidence": min_confidence,
                             "model": model, "path": path},
                     daemon=True).start()
    return True, None


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


# =================================================================== pipeline
# One Scan click runs the whole unattended part of the flow:
#   phase 1  scan + parse + TMDB identify + grouping   (run_scan)
#   phase 2  AI supervisor corrects the Unidentified   (run_tv_supervise)
#   phase 3  dupe regroup + best-copy quality audit    (audit_dupe_groups)
# then it STOPS. Building the plan (structure, exclusions) and executing it
# stay user-triggered — the pipeline never moves a file.

def set_pipe(**kw):
    with LOCK:
        STATE["pipeline"].update(kw)


def _pipe_phase(name, state, detail=""):
    with LOCK:
        for ph in STATE["pipeline"]["phases"]:
            if ph["name"] == name:
                ph["state"] = state
                if detail:
                    ph["detail"] = detail
        if state == "running":
            STATE["pipeline"]["phase"] = name


def pipeline_status():
    """Composite status for the UI stepper: the pipeline's phase list plus
    the live sub-statuses it is built from (poll one endpoint, not three)."""
    with LOCK:
        d = dict(STATE["pipeline"])
        d["phases"] = [dict(p) for p in d["phases"]]
        audit = STATE["dupeAudit"]
    d["scan"] = scan_status()
    d["supervise"] = tvsupervise_status()
    d["audit"] = ({"groups": audit["groups"], "flagged": len(audit["flagged"]),
                   "clean": audit["clean"]} if audit else None)
    return d


def _regroup_recs():
    """Recompute dupe groups from the CURRENT recs and swap them into
    STATE. Returns the new groups dict."""
    with LOCK:
        groupable = [r for r in STATE["recs"]
                     if r["kind"] in ("movie", "tv")
                     and not r.get("is_sample")]
    groups = _group_recs(groupable)
    with LOCK:
        STATE["groups"] = groups
    return groups


def dupe_rank_key(r, disc_policy="keep"):
    """THE best-copy ordering (first = keeper): playable beats disc rip
    under discPolicy=quarantine, then quality score, then size. compute_plan
    and the phase-3 audit must rank identically or the audit lies."""
    return (1 if (disc_policy == "quarantine" and r.get("is_disc")) else 0,
            -(r.get("quality_score") or 0),
            -(r.get("size") or 0), r["path"])


def audit_dupe_groups(disc_policy="keep"):
    """Phase-3 supervisor: deterministic audit of every dupe group's keeper
    choice. Never moves anything — it verifies each decision has a real
    quality signal behind it and flags the groups where the pick is
    effectively a coin toss (or worth a human look), so the user reviews a
    handful of groups instead of trusting hundreds blind."""
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
    members = {}
    for r in recs:
        gid = groups.get(r["path"])
        if gid:
            members.setdefault(gid, []).append(r)
    flagged = []
    for gid, mems in sorted(members.items()):
        ranked = sorted(mems, key=lambda m: dupe_rank_key(m, disc_policy))
        keeper, losers = ranked[0], ranked[1:]
        if not losers:
            continue
        flags = []
        kq = keeper.get("quality_score") or 0
        lq0 = losers[0].get("quality_score") or 0
        # scores under 1000 mean NO resolution tag anywhere in the name
        # (RES_SCORE starts at 1000; the parser's base is 500) — the
        # "best copy" was effectively picked by file size alone
        if kq < 1000 and all((l.get("quality_score") or 0) < 1000
                             for l in losers):
            flags.append("no-quality-signal")
        elif kq == lq0:
            szk, szl = keeper.get("size") or 0, losers[0].get("size") or 0
            if szl and abs(szk - szl) <= 0.1 * max(szk, szl):
                flags.append("quality-tie")     # same score, ~same size
        if keeper.get("is_disc") and any(not l.get("is_disc")
                                         for l in losers):
            flags.append("keeper-is-disc-rip")  # a playable copy loses
        big = max((l.get("size") or 0) for l in losers)
        if kq > lq0 and big and (keeper.get("size") or 0) * 2 < big:
            flags.append("size-inversion")      # name tags may overstate
        if flags:
            flagged.append({
                "groupId": gid, "flags": flags,
                "title": keeper.get("title") or keeper.get("name"),
                "keeper": keeper["path"],
                "files": [{"path": m["path"], "name": m["name"],
                           "quality": m.get("quality_score") or 0,
                           "size": m.get("size") or 0,
                           "disc": bool(m.get("is_disc")),
                           "keep": m["path"] == keeper["path"]}
                          for m in ranked]})
    audit = {"checkedAt": datetime.now().isoformat(sep=" "),
             "groups": len(members), "flagged": flagged,
             "clean": len(members) - len(flagged),
             "discPolicy": disc_policy}
    with LOCK:
        STATE["dupeAudit"] = audit
    try:
        set_meta({"dupe_audit": json.dumps(audit)})
    except Exception:
        pass
    return audit


def run_pipeline(root, max_files, hash_enabled, supervise=True,
                 model=None, min_confidence=None, disc_policy="keep"):
    try:
        _pipe_phase("scan", "running")
        run_scan(root, max_files, hash_enabled)
        st = scan_status()
        if st["state"] != "done":
            _pipe_phase("scan", st["state"])
            set_pipe(state=st["state"], phase="", error=st.get("error"))
            return
        with LOCK:
            total = len(STATE["recs"])
            unident = sum(1 for r in STATE["recs"]
                          if r.get("kind") == "unknown"
                          and not r.get("is_disc"))
        _pipe_phase("scan", "done",
                    f"{total} files, {unident} unidentified")

        cfg = load_config()
        has_tmdb = bool((cfg.get("tmdbKey") or "").strip()
                        or (cfg.get("tmdbToken") or "").strip())
        llm_ok = False
        if supervise and unident and llm_tv is not None and has_tmdb \
                and llm_assist is not None:
            try:
                llm_ok = llm_assist.available()
            except Exception:
                llm_ok = False
        if llm_ok:
            _pipe_phase("supervise", "running")
            run_tv_supervise(min_confidence=min_confidence, model=model)
            sup = tvsupervise_status()
            if sup["state"] == "cancelled" or TVSUP_CANCEL.is_set():
                _pipe_phase("supervise", "cancelled")
                set_pipe(state="cancelled", phase="")
                return
            if sup["state"] == "error":
                # a supervisor failure shouldn't strand the chain — note it
                # and still audit what the scan DID identify
                _pipe_phase("supervise", "error", sup.get("error") or "")
            else:
                _pipe_phase("supervise", "done",
                            f"{sup.get('identified', 0)} identified, "
                            f"{sup.get('rejected', 0)} left for review")
        else:
            why = ("no unidentified files" if not unident else
                   "disabled" if not supervise else
                   "LLM assist unavailable" if llm_tv is None
                   or llm_assist is None else
                   "needs a TMDB key" if not has_tmdb else
                   "local model not reachable")
            _pipe_phase("supervise", "skipped", why)

        if SCAN_CANCEL.is_set() or TVSUP_CANCEL.is_set():
            _pipe_phase("duplicates", "cancelled")
            set_pipe(state="cancelled", phase="")
            return
        _pipe_phase("duplicates", "running")
        _regroup_recs()
        audit = audit_dupe_groups(disc_policy)
        try:
            with LOCK:
                all_recs = list(STATE["recs"])
                groups = dict(STATE["groups"])
            db_replace_recs(all_recs, groups,
                            datetime.now().isoformat(sep=" "))
        except Exception:
            pass
        _pipe_phase("duplicates", "done",
                    f"{audit['groups']} groups checked, "
                    f"{len(audit['flagged'])} flagged for review")
        set_pipe(state="done", phase="")
    except Exception as e:
        set_pipe(state="error", phase="", error=f"{type(e).__name__}: {e}")


def start_pipeline(root, max_files, hash_enabled, supervise=True,
                   model=None, min_confidence=None, disc_policy="keep"):
    with LOCK:
        for k in ("scan", "execute", "tvsupervise", "pipeline"):
            if STATE[k]["state"] == "running":
                return False, f"A {k} is already running."
        STATE["scan"] = {"state": "running", "total": 0, "processed": 0,
                         "currentFile": "", "error": None}
        STATE["partialScan"] = False
        STATE["pipeline"] = {
            "state": "running", "phase": "scan", "error": None,
            "phases": [
                {"name": "scan", "state": "running", "detail": ""},
                {"name": "supervise", "state": "pending", "detail": ""},
                {"name": "duplicates", "state": "pending", "detail": ""}]}
    SCAN_CANCEL.clear()
    TVSUP_CANCEL.clear()
    threading.Thread(target=run_pipeline,
                     args=(root, max_files, hash_enabled),
                     kwargs={"supervise": supervise, "model": model,
                             "min_confidence": min_confidence,
                             "disc_policy": disc_policy},
                     daemon=True).start()
    return True, None


def cancel_pipeline():
    with LOCK:
        running = (STATE["pipeline"]["state"] == "running"
                   or STATE["scan"]["state"] == "running"
                   or STATE["tvsupervise"]["state"] == "running")
    if not running:
        return False
    SCAN_CANCEL.set()
    TVSUP_CANCEL.set()
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
        for t in rec["tags"]:
            if isinstance(t, str) and t.startswith("disc:"):
                rec["is_disc"] = True
                rec["disc_format"] = t.split(":", 1)[1]
                break
        recs.append(rec)
        if row["dupe_group"]:
            groups[row["path"]] = row["dupe_group"]
    audit = None
    try:
        audit = json.loads(meta.get("dupe_audit") or "null")
    except Exception:
        audit = None
    with LOCK:
        STATE["recs"] = recs
        STATE["groups"] = groups
        STATE["scannedRoot"] = meta.get("last_scan_root")
        STATE["partialScan"] = meta.get("last_scan_partial") == "1"
        STATE["dupeAudit"] = audit
        STATE["scan"] = {"state": "done", "total": len(recs),
                         "processed": len(recs), "currentFile": "", "error": None}
    return True


def build_results():
    with LOCK:
        recs = [dict(r) for r in STATE["recs"]]
        groups = dict(STATE["groups"])
        root = STATE["scannedRoot"]
        partial = STATE["partialScan"]
        dupe_audit = STATE.get("dupeAudit")
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
        "dupeAudit": dupe_audit,
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
    folder = (f"{sanitize_component(strip_index_prefix(rec['title']))}"
              f" ({rec['year']})")
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
    t = sanitize_component(strip_index_prefix(rec["title"]))
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


def _rel_dir(path, scanned_root):
    """The file's folder relative to the scanned root ("" when it sits at the
    root). Quarantine buckets keep this sub-path so a reviewed file stays
    with its original release folder -- that folder is often the only clue to
    what the file is -- and identically-named files from different folders
    stop colliding.
    """
    try:
        rel = os.path.relpath(os.path.dirname(os.path.abspath(path)),
                              os.path.abspath(scanned_root))
    except ValueError:              # different drive
        return ""
    if rel in (".", os.curdir) or rel.startswith(".."):
        return ""
    return rel


def compute_plan(params):
    ensure_state()
    with LOCK:
        for k in ("scan", "tvsupervise", "pipeline"):
            if STATE[k]["state"] == "running":
                return None, (f"A {k} is still running — a plan built now "
                              "would use half-updated results. Wait for it "
                              "to finish (or cancel it) first.")
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
    # restructure: re-file an ALREADY-organized library into a different
    # layout from the indexed identities — no rescan needed (execute/undo
    # keep DB paths truthful). Sources and destinations share one root, so
    # the usual target!=scanned-root guard doesn't apply.
    restructure = bool((params or {}).get("restructure"))
    if restructure:
        outside = [r["path"] for r in recs
                   if not is_within(r["path"], target_root)]
        if outside:
            return None, (
                f"{len(outside)} indexed file(s) live outside {target_root}. "
                "Restructure re-files a library in place — set the target to "
                "the folder that already holds them, e.g. "
                f"{os.path.dirname(outside[0])}")
    elif normcase_abs(target_root) == normcase_abs(root):
        return None, "Target root must differ from the scanned folder."
    rel_root = target_root if restructure else root

    # best copy per group: quality_score desc, then size desc
    # discPolicy "keep" (default) files rips into the library (Kodi-friendly);
    # "quarantine" routes every rip to _DiscRips\ for review/removal (Plex
    # can't play them) — playable files then always beat rips in dedupe, and
    # rips that are the ONLY copy of their title are flagged so a film isn't
    # lost without a remux. Parsed here because best-copy ranking needs it.
    disc_policy = ((params or {}).get("discPolicy") or "keep").strip().lower()
    if disc_policy not in ("keep", "quarantine"):
        return None, "Invalid discPolicy (use 'keep' or 'quarantine')."

    # per-file exclusions from the plan preview: excluded files are left
    # untouched and drop out of planning BEFORE best-copy ranking, so
    # excluding a keeper promotes the next-best copy instead of
    # quarantining every remaining member of its group.
    exclude = {normcase_abs(p)
               for p in ((params or {}).get("exclude") or []) if p}
    excluded = 0
    if exclude:
        kept_recs = []
        for r in recs:
            if normcase_abs(r["path"]) in exclude:
                excluded += 1
            else:
                kept_recs.append(r)
        recs = kept_recs

    best_of = {}
    gid_members = {}
    for r in recs:
        gid = groups.get(r["path"])
        if gid:
            gid_members.setdefault(gid, []).append(r)
    for gid, members in gid_members.items():
        # under discPolicy=quarantine a playable file ALWAYS beats a disc rip
        # (a rip's higher quality score is useless if you can't play it) —
        # dupe_rank_key is shared with the phase-3 audit
        ranked = sorted(members,
                        key=lambda r: dupe_rank_key(r, disc_policy))
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
    # cross-root check against other indexed roots (the main library):
    # owned copies route to _AlreadyInLibrary\; better-quality copies file
    # normally but are tagged as upgrades in the preview
    in_library = {} if restructure else find_in_library(recs, root)
    entries = []
    counts = {"dupe": 0, "sample": 0, "clutter": 0, "unidentified": 0,
              "crossMovie": 0, "crossTv": 0, "inLibrary": 0, "upgrade": 0,
              "disc": 0, "discQuarantine": 0, "discOnlyCopy": 0}
    for r in sorted(recs, key=lambda x: x["path"]):
        gid = groups.get(r["path"])
        # quarantine buckets keep the file's original sub-folder
        rel_dir = _rel_dir(r["path"], rel_root)
        entry = {"from": r["path"], "kind": r["kind"],
                 "isDupe": False, "groupId": gid, "reason": None,
                 "companions": [{"from": c["from"], "to": None,
                                 "suffix": c.get("suffix"),
                                 "keepName": bool(c.get("keepName"))}
                                for c in (r.get("companions") or [])]}
        if disc_policy == "quarantine" and r.get("is_disc"):
            # one review bucket for every rip; flag rips that are the ONLY
            # copy of their title (no playable twin in this scan, nothing in
            # the library index) — deleting those loses the film, remux first
            gid_m = gid_members.get(gid, []) if gid else []
            has_playable = any(not m.get("is_disc") for m in gid_m
                               if m["path"] != r["path"]) \
                or r["path"] in in_library
            entry.update(to=os.path.join(target_root, "_DiscRips",
                                         rel_dir, r["name"]),
                         reason="disc-rip")
            if not has_playable:
                entry["onlyCopy"] = True
                counts["discOnlyCopy"] += 1
            counts["discQuarantine"] += 1
        elif r["kind"] == "clutter":
            entry.update(to=os.path.join(target_root, "_Clutter",
                                         rel_dir, r["name"]),
                         reason="clutter")
            counts["clutter"] += 1
        elif r.get("is_sample"):
            entry.update(to=os.path.join(target_root, "_Samples",
                                         rel_dir, r["name"]),
                         reason="sample")
            counts["sample"] += 1
        elif gid and r["path"] != best_of.get(gid):
            entry.update(to=os.path.join(target_root, "_Duplicates", gid,
                                         rel_dir, r["name"]),
                         isDupe=True, reason="dupe")
            counts["dupe"] += 1
        elif r["path"] in in_library and in_library[r["path"]][1] == "dupe":
            # same bytes, or same movie/episode at equal-or-lower quality,
            # already lives in another indexed root (the main library)
            entry.update(to=os.path.join(target_root, "_AlreadyInLibrary",
                                         rel_dir, r["name"]),
                         reason="in-library",
                         libraryCopy=in_library[r["path"]][0])
            counts["inLibrary"] += 1
        elif expect_kind == "tv" and r["kind"] == "movie":
            # A movie turned up in a TV library: don't file it into the TV
            # tree -- quarantine it so it can be moved to the real movie
            # library (same idea as _Duplicates / _Unidentified).
            entry.update(to=os.path.join(target_root, "_Movies",
                                         rel_dir, r["name"]),
                         reason="cross-movie")
            counts["crossMovie"] += 1
        elif expect_kind == "movie" and r["kind"] == "tv":
            entry.update(to=os.path.join(target_root, "_TV",
                                         rel_dir, r["name"]),
                         reason="cross-tv")
            counts["crossTv"] += 1
        elif r["kind"] == "movie":
            dest = movie_dest(r, target_root, split=split_by_kind,
                              year_folder=movie_year_folder, layout=layout)
            if r.get("is_disc") and not r["ext"]:
                # folder rip: move the unit as a directory. A unit named
                # VIDEO_TS/BDMV (rip at scan root) keeps that name inside
                # the movie folder; otherwise the unit folder BECOMES the
                # movie folder ("Title (Year)\VIDEO_TS\..." either way).
                movie_dir = os.path.dirname(dest)
                if r["name"].lower() in DISC_DIR_NAMES:
                    entry["to"] = os.path.join(movie_dir, r["name"])
                else:
                    entry["to"] = movie_dir
            else:
                entry["to"] = dest
        elif r["kind"] == "tv":
            entry["to"] = tv_dest(r, target_root, split=split_by_kind,
                                  layout=layout)
        else:
            entry.update(to=os.path.join(target_root, "_Unidentified",
                                         rel_dir, r["name"]),
                         reason="unidentified")
            counts["unidentified"] += 1
        # better copy of something already owned: file it normally, but tag
        # it so the preview shows it's an upgrade (the old copy stays put —
        # a later library scan's dedupe will catch the loser)
        if entry["reason"] is None and r["path"] in in_library \
                and in_library[r["path"]][1] == "upgrade":
            entry["upgrade"] = True
            entry["libraryCopy"] = in_library[r["path"]][0]
            counts["upgrade"] += 1
        if r.get("is_disc"):
            entry["disc"] = r.get("disc_format")
            entry["isDir"] = not r["ext"]
            counts["disc"] += 1
        # generated NFO sidecars for identified, normally-filed movies/eps
        # (skipped for disc units — their metadata lives inside the rip)
        if write_nfo and entry["reason"] is None and not r.get("is_disc") \
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

    unchanged = 0
    if restructure:
        kept = []
        for e in entries:
            if normcase_abs(e["from"]) == normcase_abs(e["to"]):
                unchanged += 1
            else:
                kept.append(e)
        entries = kept

    folders = {os.path.dirname(e["to"]) for e in entries}
    stats = {"totalFiles": len(entries),
             "excludedFiles": excluded,
             "unchangedFiles": unchanged, "restructure": restructure,
             "dupeFiles": counts["dupe"], "sampleFiles": counts["sample"],
             "clutterFiles": counts["clutter"],
             "unidentifiedFiles": counts["unidentified"],
             "crossMovieFiles": counts["crossMovie"],
             "crossTvFiles": counts["crossTv"],
             "nfoFiles": nfo_count,
             "writeNfo": write_nfo,
             "inLibraryFiles": counts["inLibrary"],
             "upgradeFiles": counts["upgrade"],
             "discUnits": counts["disc"],
             "discQuarantined": counts["discQuarantine"],
             "discOnlyCopy": counts["discOnlyCopy"],
             "discPolicy": disc_policy,
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
                       "scannedRoot": target_root if restructure else root,
                       "expectKind": expect_kind,
                       "restructure": restructure}}
    with LOCK:
        STATE["plan"] = plan
    return plan, None


# =================================================================== execute

def _preflight_roots(scanned_root, target_root):
    """Friendly up-front checks so a disconnected/mistyped drive fails with a
    clear message instead of a mid-run WinError deep in makedirs."""
    if not os.path.isdir(scanned_root):
        return (f"Scanned folder is no longer available: {scanned_root} — "
                "was a drive disconnected? Rescan, then rebuild the plan.")
    drive = os.path.splitdrive(os.path.abspath(target_root))[0]
    if drive and not os.path.exists(drive + os.sep):
        return (f"Target drive {drive}\\ is not available — connect it or "
                "pick a different target folder, then rebuild the plan.")
    try:
        os.makedirs(target_root, exist_ok=True)
    except OSError as e:
        return (f"Cannot create the target folder {target_root}: "
                f"{type(e).__name__}: {e}")
    return None


def _apply_manifest_to_state(manifest):
    """Mirror an executed move-manifest onto the IN-MEMORY recs so a second
    plan in the same session (e.g. a restructure right after an organize)
    sees where files actually are — the DB is already updated by
    db_update_paths, but STATE keeps the pre-move paths otherwise."""
    moves = {normcase_abs(e["from"]): e["to"] for e in manifest
             if e.get("action") == "move" and e.get("from") and e.get("to")}
    if not moves:
        return
    with LOCK:
        for r in STATE["recs"]:
            dst = moves.get(normcase_abs(r["path"]))
            if dst:
                r["path"] = dst
                r["name"] = os.path.basename(dst)
        groups = STATE.get("groups") or {}
        if groups:
            STATE["groups"] = {moves.get(normcase_abs(p), p): g
                               for p, g in groups.items()}


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
    err = _preflight_roots(scanned_root, target_root)
    if err:
        set_exec(state="error", error=err)
        return
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
                src_is_dir = os.path.isdir(src)
                if not os.path.isfile(src) and not src_is_dir:
                    raise FileNotFoundError("source missing")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                actual = resolve_collision(dst, src)
                import shutil as _sh
                if src_is_dir:
                    # disc-rip unit (VIDEO_TS/BDMV): one atomic tree op
                    if action == "move":
                        _sh.move(src, actual)
                        moved += 1
                    else:
                        _sh.copytree(src, actual)
                        copied += 1
                elif action == "move":
                    _sh.move(src, actual)
                    moved += 1
                else:
                    _sh.copy2(src, actual)
                    copied += 1
                exec_log(f"{action.upper()} {src} -> {actual}")
                manifest.append({"from": src, "to": actual, "action": action,
                                 **({"dir": True} if src_is_dir else {})})
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
                _apply_manifest_to_state(manifest)
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
        # the pipeline's supervisor/audit mutate recs+groups+DB — an execute
        # interleaved with them would replay a plan built from half-updated
        # state
        for k in ("scan", "tvsupervise", "pipeline"):
            if STATE[k]["state"] == "running":
                return False, f"A {k} is still running — wait for it to finish."
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
                if not os.path.exists(dst):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(src), exist_ok=True)
                import shutil as _sh
                _sh.move(dst, src)
                restored += 1
            else:
                if e.get("dir") and os.path.isdir(dst):
                    # undo of a copied disc-rip unit: remove the copy we made
                    import shutil as _sh
                    _sh.rmtree(dst)
                    deleted += 1
                elif os.path.isfile(dst):
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
