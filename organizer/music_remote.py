#!/usr/bin/env python3
"""music_remote.py - Music Organizer: throttled metadata providers.

MusicBrainz (ws/2), Cover Art Archive, AcoustID (v2), Last.fm and Discogs
behind one shared per-host throttle, Retry-After back-off on 429/503, and
an SQLite response cache (mb_cache table in music.db, journal_mode=WAL).
Stdlib only; no network happens unless the default fetcher is used.

Fetcher contract (every public function takes fetcher=None):
    fetcher(url, headers=None, data=None) -> (status:int, headers:dict, body:bytes)
    - headers/body are the OUTBOUND request headers/body (data set => POST)
    - returns response status, response headers (any case), raw body bytes
    - may raise on transport failure; callers treat that as status 0
The default fetcher is urllib with the app User-Agent. Tests inject fakes.

Failure policy: providers never raise into callers. Searches return [] on
failure, single-object lookups return None. Transient failures are NOT
cached; confirmed absences (404, empty result set) are cached so scans
don't re-hit the APIs. Cache keys never contain API keys/tokens.

Return shapes (plain dicts):
  mb_search_release  -> [{mbid,title,artist,is_va,date,country,score,
                         track_count,primary_type,secondary_types,
                         release_group_mbid}]
  mb_get_release     -> {mbid,title,artist,artist_credits,is_va,date,
                         country,barcode,status,label,catno,
                         release_group_mbid,primary_type,secondary_types,
                         tracks:[{disc,track,title,artist,is_va,
                                  duration_s,recording_mbid}]}
  mb_search_recording-> [{mbid,title,artist,is_va,duration_s,score,
                         releases:[{mbid,title}]}]
  cover_art_front    -> raw image bytes | None
  acoustid_lookup    -> {results:[{acoustid,score,recordings:[{
                         recording_mbid,title,artist,duration_s,
                         release_groups:[{mbid,title,primary_type,
                                          secondary_types}]}]}]}
  lastfm_correction  -> {artist,title}
  discogs_search_release -> {id,title,year,genres,styles,labels,catno,
                             country,formats}
"""
import base64
import contextlib
import gzip
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_DB = os.path.join(BASE_DIR, "music.db")   # tests redirect to a temp dir

USER_AGENT = "MediaOrganizer/1.0 (local app)"
HTTP_TIMEOUT = 15
MAX_RETRIES = 2                 # 429/503: up to 2 retries (3 attempts total)
RETRY_AFTER_CAP = 60.0          # never sleep longer than this per back-off

MB_ROOT = "https://musicbrainz.org/ws/2"
CAA_FRONT = "https://coverartarchive.org/release/{mbid}/front"
ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DISCOGS_SEARCH = "https://api.discogs.com/database/search"

VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"   # MusicBrainz "Various Artists"

# Minimum seconds between requests, per host (provider rate limits).
HOST_INTERVALS = {
    "musicbrainz.org": 1.05,      # 1 req/s
    "api.acoustid.org": 0.34,     # 3 req/s
    "api.discogs.com": 1.05,      # 60 req/min
    "ws.audioscrobbler.com": 1.0,  # ~1 req/s
    "api.deezer.com": 0.12,       # 50 req/5s
    "itunes.apple.com": 3.1,      # ~1 req/3s
    "coverartarchive.org": 0.5,   # polite default (redirects to archive.org)
}
DEFAULT_INTERVAL = 0.5


# =================================================================== time
# Module-level wrappers so tests can substitute a fake clock (throttle and
# back-off tests then run instantly and deterministically).

def _now():
    return time.time()


def _sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


# =================================================================== throttle

_THROTTLE_LOCK = threading.Lock()
_HOST_LAST = {}                 # host -> monotonic-ish timestamp of last request


def _throttle(url):
    """Block until `url`'s host may be hit again (per-host interval)."""
    host = urllib.parse.urlparse(url).netloc.lower()
    interval = HOST_INTERVALS.get(host, DEFAULT_INTERVAL)
    while True:
        with _THROTTLE_LOCK:
            now = _now()
            wait = interval - (now - _HOST_LAST.get(host, 0.0))
            if wait <= 0:
                _HOST_LAST[host] = now
                return
        _sleep(wait)


# =================================================================== http

def _real_fetcher(url, headers=None, data=None):
    """urllib fetcher: (status, headers, body). HTTPError -> its own status
    so 429/503/404 reach the caller instead of raising."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, hdrs, body


def _resolve_fetcher(fetcher):
    return fetcher if fetcher is not None else _real_fetcher


def _retry_after(headers):
    """Retry-After response header -> float seconds (capped), or None."""
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            try:
                return max(0.0, min(float(str(v).strip()), RETRY_AFTER_CAP))
            except (TypeError, ValueError):
                return None     # HTTP-date form: fall back to default backoff
    return None


def _http(url, fetcher, headers=None, data=None):
    """Throttled request with 429/503 back-off (honors Retry-After).
    -> (status, headers, body). Raises only what the fetcher raises."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    attempt = 0
    while True:
        _throttle(url)
        status, resp_headers, body = fetcher(url, headers=hdrs, data=data)
        if status in (429, 503) and attempt < MAX_RETRIES:
            attempt += 1
            delay = _retry_after(resp_headers)
            if delay is None:
                delay = min(2.0 * attempt, RETRY_AFTER_CAP)
            _sleep(delay)
            continue
        return status, resp_headers, body


def _fetch_json(url, fetcher, headers=None, data=None):
    """-> (status:int, obj|None). status 0 = transport failure; obj None on
    unparseable body."""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        status, _, body = _http(url, fetcher, headers=hdrs, data=data)
    except Exception:
        return 0, None
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception:
        return status, None


# =================================================================== cache
# mb_cache(key TEXT PRIMARY KEY, value TEXT, fetched_at TEXT) in music.db.
# Created lazily on first use so importing this module never touches disk.

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_PATH = None

_SCHEMA_SQL = ("CREATE TABLE IF NOT EXISTS mb_cache ("
               " key TEXT PRIMARY KEY, value TEXT, fetched_at TEXT)")


def _connect():
    con = sqlite3.connect(MUSIC_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _ensure_schema():
    global _SCHEMA_READY_PATH
    with _SCHEMA_LOCK:
        if _SCHEMA_READY_PATH == MUSIC_DB:
            return
        with contextlib.closing(_connect()) as con:
            con.execute(_SCHEMA_SQL)
            con.execute("PRAGMA journal_mode=WAL")
            con.commit()
        _SCHEMA_READY_PATH = MUSIC_DB


def cache_get(key):
    """Cached JSON value for key, or None (miss/corrupt/error). Never raises."""
    try:
        _ensure_schema()
        with contextlib.closing(_connect()) as con:
            row = con.execute("SELECT value FROM mb_cache WHERE key = ?",
                              (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def cache_set(key, value):
    """Store a JSON-serializable value. Returns True on success, never raises."""
    try:
        _ensure_schema()
        payload = json.dumps(value)
        with contextlib.closing(_connect()) as con:
            con.execute("INSERT OR REPLACE INTO mb_cache (key, value, fetched_at)"
                        " VALUES (?, ?, ?)",
                        (key, payload, datetime.now().isoformat(sep=" ")))
            con.commit()
        return True
    except Exception:
        return False


# Internal wrapper so a cached None (confirmed absence) is distinguishable
# from a cache miss, and cache_get stays a plain value store for other users.
_WRAP = "__music_remote_v1__"


def _cache_lookup(key):
    raw = cache_get(key)
    if isinstance(raw, dict) and raw.get(_WRAP):
        return True, raw.get("v")
    return False, None


def _cache_store(key, value):
    cache_set(key, {_WRAP: 1, "v": value})


def _cached_call(key, produce, failure):
    """Cache-aside around produce(); produce() -> (cacheable:bool, value).
    Transient failures (cacheable False) never poison the cache."""
    hit, value = _cache_lookup(key)
    if hit:
        return value
    try:
        cacheable, value = produce()
    except Exception:
        return failure
    if cacheable:
        _cache_store(key, value)
    return value


# =================================================================== musicbrainz

def _artist_credit_string(credits):
    """MB artist-credit array -> display string, Picard-style join phrases:
    [{name:'A', joinphrase:' feat. '}, {name:'B', joinphrase:''}] ->
    'A feat. B'. 'name' falls back to artist.name when absent."""
    parts = []
    for c in credits or []:
        if not isinstance(c, dict):
            continue
        artist = c.get("artist") or {}
        name = c.get("name") or artist.get("name") or ""
        parts.append(name + (c.get("joinphrase") or ""))
    return "".join(parts).strip()


def _artist_credit_list(credits):
    """Structured artist credit: [{name, joinphrase, mbid}]."""
    out = []
    for c in credits or []:
        if not isinstance(c, dict):
            continue
        artist = c.get("artist") or {}
        out.append({"name": c.get("name") or artist.get("name"),
                    "joinphrase": c.get("joinphrase") or "",
                    "mbid": artist.get("id")})
    return out


def _is_va_credit(credits):
    """True when the credit references the MusicBrainz Various Artists entity."""
    return any(isinstance(c, dict)
               and (c.get("artist") or {}).get("id") == VA_MBID
               for c in credits or [])


def _norm_key(s):
    return " ".join(str(s or "").split()).lower()


def _mb_escape(s):
    """Strip characters that would break a quoted Lucene clause."""
    return str(s or "").replace('"', " ").strip()


def _release_summary(rel):
    rg = rel.get("release-group") or {}
    return {
        "mbid": rel.get("id"),
        "title": rel.get("title"),
        "artist": _artist_credit_string(rel.get("artist-credit")),
        "is_va": _is_va_credit(rel.get("artist-credit")),
        "date": rel.get("date"),
        "country": rel.get("country"),
        "score": rel.get("score"),
        "track_count": rel.get("track-count"),
        "primary_type": rg.get("primary-type"),
        "secondary_types": rg.get("secondary-types") or [],
        "release_group_mbid": rg.get("id"),
    }


def _parse_release(rel):
    """Full release lookup -> the dict documented in the module docstring."""
    rg = rel.get("release-group") or {}
    label = catno = None
    for li in rel.get("label-info") or []:
        if not isinstance(li, dict):
            continue
        name = (li.get("label") or {}).get("name")
        if label is None and name:
            label = name
        cn = li.get("catalog-number")
        if catno is None and cn:
            catno = cn
    tracks = []
    for medium in rel.get("media") or []:
        if not isinstance(medium, dict):
            continue
        disc = medium.get("position") or 1
        for tr in medium.get("tracks") or []:
            if not isinstance(tr, dict):
                continue
            rec = tr.get("recording") or {}
            credits = tr.get("artist-credit") or rec.get("artist-credit")
            length = tr.get("length")
            if length is None:
                length = rec.get("length")
            tracks.append({
                "disc": disc,
                "track": tr.get("position"),
                "title": tr.get("title") or rec.get("title"),
                "artist": _artist_credit_string(credits),
                "is_va": _is_va_credit(credits),
                "duration_s": length / 1000.0
                if isinstance(length, (int, float)) else None,
                "recording_mbid": rec.get("id"),
            })
    return {
        "mbid": rel.get("id"),
        "title": rel.get("title"),
        "artist": _artist_credit_string(rel.get("artist-credit")),
        "artist_credits": _artist_credit_list(rel.get("artist-credit")),
        "is_va": _is_va_credit(rel.get("artist-credit")),
        "date": rel.get("date"),
        "country": rel.get("country"),
        "barcode": rel.get("barcode"),
        "status": rel.get("status"),
        "label": label,
        "catno": catno,
        "release_group_mbid": rg.get("id"),
        "primary_type": rg.get("primary-type"),
        "secondary_types": rg.get("secondary-types") or [],
        "tracks": tracks,
    }


def mb_search_release(artist, album, fetcher=None):
    """MusicBrainz ws/2 release search -> list of summary dicts ([] on failure)."""
    if not album or not str(album).strip():
        return []
    fetcher = _resolve_fetcher(fetcher)
    key = f"mb:relsearch:{_norm_key(artist)}|{_norm_key(album)}"

    def produce():
        clauses = [f'release:"{_mb_escape(album)}"']
        if artist and str(artist).strip():
            clauses.append(f'artist:"{_mb_escape(artist)}"')
        qs = urllib.parse.urlencode({"query": " AND ".join(clauses),
                                     "fmt": "json"})
        status, obj = _fetch_json(f"{MB_ROOT}/release/?{qs}", fetcher)
        if status == 200 and isinstance(obj, dict):
            return True, [_release_summary(r) for r in obj.get("releases") or []
                          if isinstance(r, dict)]
        if status == 404:
            return True, []
        return False, []

    return _cached_call(key, produce, [])


def mb_get_release(mbid, fetcher=None):
    """Release lookup (inc=recordings+artist-credits+release-groups+labels)
    -> full plain dict, or None on failure/not-found."""
    if not mbid:
        return None
    fetcher = _resolve_fetcher(fetcher)
    key = f"mb:release:{mbid}"

    def produce():
        qs = urllib.parse.urlencode({
            "inc": "recordings artist-credits release-groups labels",
            "fmt": "json"})
        url = f"{MB_ROOT}/release/{urllib.parse.quote(str(mbid))}?{qs}"
        status, obj = _fetch_json(url, fetcher)
        if status == 200 and isinstance(obj, dict) and obj.get("id"):
            return True, _parse_release(obj)
        if status == 404:
            return True, None
        return False, None

    return _cached_call(key, produce, None)


def mb_search_recording(artist, title, fetcher=None):
    """MusicBrainz ws/2 recording search -> list of dicts ([] on failure)."""
    if not title or not str(title).strip():
        return []
    fetcher = _resolve_fetcher(fetcher)
    key = f"mb:recsearch:{_norm_key(artist)}|{_norm_key(title)}"

    def produce():
        clauses = [f'recording:"{_mb_escape(title)}"']
        if artist and str(artist).strip():
            clauses.append(f'artist:"{_mb_escape(artist)}"')
        qs = urllib.parse.urlencode({"query": " AND ".join(clauses),
                                     "fmt": "json"})
        status, obj = _fetch_json(f"{MB_ROOT}/recording/?{qs}", fetcher)
        if status == 200 and isinstance(obj, dict):
            out = []
            for rec in obj.get("recordings") or []:
                if not isinstance(rec, dict):
                    continue
                length = rec.get("length")
                out.append({
                    "mbid": rec.get("id"),
                    "title": rec.get("title"),
                    "artist": _artist_credit_string(rec.get("artist-credit")),
                    "is_va": _is_va_credit(rec.get("artist-credit")),
                    "duration_s": length / 1000.0
                    if isinstance(length, (int, float)) else None,
                    "score": rec.get("score"),
                    "releases": [{"mbid": r.get("id"), "title": r.get("title")}
                                 for r in rec.get("releases") or []
                                 if isinstance(r, dict)],
                })
            return True, out
        if status == 404:
            return True, []
        return False, []

    return _cached_call(key, produce, [])


# =================================================================== cover art

def cover_art_front(mbid, fetcher=None):
    """Cover Art Archive front image -> raw bytes, or None. Cached (base64)
    like the JSON providers; 404 is cached as a confirmed absence."""
    if not mbid:
        return None
    fetcher = _resolve_fetcher(fetcher)
    key = f"caa:front:{mbid}"
    try:
        hit, value = _cache_lookup(key)
        if hit:
            if value is None:
                return None
            return base64.b64decode(value.encode("ascii"))
        url = CAA_FRONT.format(mbid=urllib.parse.quote(str(mbid)))
        try:
            status, _, body = _http(url, fetcher)
        except Exception:
            return None
        if status == 200 and body:
            _cache_store(key, base64.b64encode(body).decode("ascii"))
            return body
        if status == 404:
            _cache_store(key, None)
            return None
        return None
    except Exception:
        return None


# =================================================================== acoustid

def _parse_acoustid_result(r):
    if not isinstance(r, dict) or not r.get("id"):
        return None
    recordings = []
    for rec in r.get("recordings") or []:
        if not isinstance(rec, dict):
            continue
        artists = ", ".join(a.get("name") for a in rec.get("artists") or []
                            if isinstance(a, dict) and a.get("name"))
        groups = []
        for g in rec.get("releasegroups") or []:
            if not isinstance(g, dict):
                continue
            groups.append({
                "mbid": g.get("id"),
                "title": g.get("title"),
                "primary_type": g.get("type"),
                "secondary_types": g.get("secondarytypes") or [],
            })
        recordings.append({
            "recording_mbid": rec.get("id"),
            "title": rec.get("title"),
            "artist": artists,
            "duration_s": rec.get("duration"),
            "release_groups": groups,
        })
    return {"acoustid": r.get("id"), "score": r.get("score"),
            "recordings": recordings}


def acoustid_lookup(fingerprint, duration_s, api_key, fetcher=None):
    """AcoustID v2 lookup -> {results:[...]} or None. POSTs a gzip-compressed
    form (Content-Encoding: gzip) with meta=recordings+releasegroups+compress.
    Needs a (free) API key; no key -> None without any request."""
    if not fingerprint or not api_key:
        return None
    try:
        dur = int(round(float(duration_s)))
    except (TypeError, ValueError):
        return None
    fetcher = _resolve_fetcher(fetcher)
    key = f"acoustid:{fingerprint}:{dur}"

    def produce():
        form = urllib.parse.urlencode({
            "client": api_key,
            "duration": str(dur),
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups compress",
        }).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Content-Encoding": "gzip"}
        status, obj = _fetch_json(ACOUSTID_URL, fetcher, headers=headers,
                                  data=gzip.compress(form))
        if status == 200 and isinstance(obj, dict) and obj.get("status") == "ok":
            results = [p for p in (_parse_acoustid_result(r)
                                   for r in obj.get("results") or []) if p]
            # confirmed no-match caches; an API 'error' status does not
            return True, ({"results": results} if results else None)
        return False, None

    return _cached_call(key, produce, None)


# =================================================================== last.fm

def lastfm_correction(artist, title, api_key, fetcher=None):
    """Last.fm track.getCorrection -> {'artist':.., 'title':..} (canonical
    spelling) or None when Last.fm has no correction. Needs an API key."""
    if not api_key or not artist or not title:
        return None
    fetcher = _resolve_fetcher(fetcher)
    key = f"lastfm:correction:{_norm_key(artist)}|{_norm_key(title)}"

    def produce():
        qs = urllib.parse.urlencode({
            "method": "track.getcorrection",
            "artist": artist, "track": title,
            "api_key": api_key, "format": "json"})
        status, obj = _fetch_json(f"{LASTFM_URL}?{qs}", fetcher)
        if status == 200 and isinstance(obj, dict) and "error" not in obj:
            track = (((obj.get("corrections") or {}).get("correction") or {})
                     .get("track") or {})
            name = track.get("name")
            art = (track.get("artist") or {}).get("name") \
                if isinstance(track.get("artist"), dict) else None
            if name or art:
                return True, {"artist": art or artist, "title": name or title}
            return True, None
        return False, None

    return _cached_call(key, produce, None)


# =================================================================== discogs

def discogs_search_release(artist, album, token, fetcher=None):
    """Discogs database/search (Authorization: Discogs token=...) -> best
    release hit normalized, or None. Needs a (free) personal access token."""
    if not token or not album or not str(album).strip():
        return None
    fetcher = _resolve_fetcher(fetcher)
    key = f"discogs:relsearch:{_norm_key(artist)}|{_norm_key(album)}"

    def produce():
        params = {"type": "release", "release_title": album}
        if artist and str(artist).strip():
            params["artist"] = artist
        qs = urllib.parse.urlencode(params)
        headers = {"Authorization": f"Discogs token={token}"}
        status, obj = _fetch_json(f"{DISCOGS_SEARCH}?{qs}", fetcher,
                                  headers=headers)
        if status == 200 and isinstance(obj, dict):
            results = [r for r in obj.get("results") or []
                       if isinstance(r, dict)]
            if not results:
                return True, None
            r = results[0]
            year = r.get("year")
            return True, {
                "id": r.get("id"),
                "title": r.get("title"),
                "year": int(year) if str(year or "").isdigit() else None,
                "genres": r.get("genre") or [],
                "styles": r.get("style") or [],
                "labels": r.get("label") or [],
                "catno": r.get("catno"),
                "country": r.get("country"),
                "formats": r.get("format") or [],
            }
        if status == 404:
            return True, None
        return False, None

    return _cached_call(key, produce, None)
