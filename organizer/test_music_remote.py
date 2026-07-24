#!/usr/bin/env python3
r"""Tests for music_remote.py - 100% offline.

Fake fetchers return recorded JSON payloads (normal album, VA compilation,
typo'd Last.fm correction, AcoustID hit, 429-then-success); a FakeClock
replaces music_remote._now/_sleep so throttle and back-off tests run
instantly; the cache DB is redirected to a temp dir inside the workspace
and removed afterwards. No server is started; nothing touches the network
or the production databases.

Run: python test_music_remote.py
"""
import contextlib
import gzip
import json
import os
import shutil
import sqlite3
import sys
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import music_remote as mr

TMP = os.path.join(BASE, "music_remote_test_tmp")
PASS, FAIL = [], []

MA_MBID = "10adbe5e-a2c0-4bf3-8249-2b4cbf6e6ca8"   # Massive Attack (real MBID)
VA_MBID = mr.VA_MBID                                # Various Artists


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


# =================================================================== fakes

class FakeClock:
    """Deterministic time: sleep() advances the clock and records durations."""
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        before = self.t
        self.t += s
        if self.t <= before:
            # increment below float ULP at this magnitude: force progress so
            # the throttle loop converges (real clocks always advance)
            self.t = before + 1e-6


@contextlib.contextmanager
def mocked_clock():
    saved_now, saved_sleep = mr._now, mr._sleep
    saved_last = dict(mr._HOST_LAST)
    clock = FakeClock()
    mr._now, mr._sleep = clock.now, clock.sleep
    mr._HOST_LAST.clear()
    try:
        yield clock
    finally:
        mr._now, mr._sleep = saved_now, saved_sleep
        mr._HOST_LAST.clear()
        mr._HOST_LAST.update(saved_last)


@contextlib.contextmanager
def fresh_cache():
    mr._ensure_schema()
    con = sqlite3.connect(mr.MUSIC_DB, timeout=30)
    try:
        con.execute("DELETE FROM mb_cache")
        con.commit()
    finally:
        con.close()
    yield


def json_fetcher(payload, status=200, resp_headers=None, record=None):
    """Fake fetcher: fixed JSON (or raw bytes) payload; records calls."""
    def fetch(url, headers=None, data=None):
        if record is not None:
            record.append({"url": url, "headers": dict(headers or {}),
                           "data": data})
        body = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode("utf-8")
        return status, dict(resp_headers or {}), body
    return fetch


def scripted_fetcher(script, record=None):
    """Fake fetcher replaying (status, headers, payload) steps in order."""
    steps = list(script)

    def fetch(url, headers=None, data=None):
        if record is not None:
            record.append({"url": url, "headers": dict(headers or {}),
                           "data": data})
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        status, resp_headers, payload = step
        body = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode("utf-8")
        return status, dict(resp_headers), body
    return fetch


def raising_fetcher(url, headers=None, data=None):
    raise AssertionError("fetch must not be called (cache should have hit)")


# ============================================================ recorded payloads

MB_MEZZANINE = {
    "id": "f6c7c3e2-3f4a-4c4a-9b1e-1234567890ab",
    "title": "Mezzanine",
    "status": "Official",
    "quality": "normal",
    "date": "1998-04-20",
    "country": "GB",
    "barcode": "724384959220",
    "asin": None,
    "artist-credit": [
        {"name": "Massive Attack", "joinphrase": "",
         "artist": {"id": MA_MBID, "name": "Massive Attack",
                    "sort-name": "Massive Attack"}}
    ],
    "release-group": {
        "id": "a1b2c3d4-0000-4000-8000-abcdefabcdef",
        "title": "Mezzanine",
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "1998-04-20"
    },
    "label-info": [
        {"catalog-number": "WBRCD4",
         "label": {"id": "9b1e5f00-1111-4222-8333-0123456789ab",
                   "name": "Virgin"}}
    ],
    "media": [
        {"position": 1, "format": "CD", "track-count": 3,
         "tracks": [
             {"id": "t-angel", "position": 1, "number": "1",
              "title": "Angel", "length": 378000,
              "recording": {"id": "r1-mbid", "title": "Angel", "length": 378000,
                            "artist-credit": [
                                {"name": "Massive Attack", "joinphrase": "",
                                 "artist": {"id": MA_MBID,
                                            "name": "Massive Attack"}}]}},
             {"id": "t-risingson", "position": 2, "number": "2",
              "title": "Risingson", "length": 327000,
              "recording": {"id": "r2-mbid", "title": "Risingson",
                            "length": 327000,
                            "artist-credit": [
                                {"name": "Massive Attack", "joinphrase": "",
                                 "artist": {"id": MA_MBID,
                                            "name": "Massive Attack"}}]}},
             {"id": "t-teardrop", "position": 3, "number": "3",
              "title": "Teardrop", "length": 333000,
              "recording": {"id": "r3-mbid", "title": "Teardrop",
                            "length": 333000,
                            "artist-credit": [
                                {"name": "Massive Attack", "joinphrase": "",
                                 "artist": {"id": MA_MBID,
                                            "name": "Massive Attack"}}]}}
         ]}
    ]
}

MB_VA = {
    "id": "va000000-1111-4222-8333-aaaaaaaaaaaa",
    "title": "Big Beat Boutique, Vol. 2",
    "status": "Official",
    "date": "1999-06-14",
    "country": "GB",
    "barcode": "5025425000420",
    "artist-credit": [
        {"name": "Various Artists", "joinphrase": "",
         "artist": {"id": VA_MBID, "name": "Various Artists",
                    "sort-name": "Various Artists"}}
    ],
    "release-group": {
        "id": "varg0000-2222-4333-8444-bbbbbbbbbbbb",
        "title": "Big Beat Boutique, Vol. 2",
        "primary-type": "Album",
        "secondary-types": ["Compilation"],
        "first-release-date": "1999-06-07"
    },
    "label-info": [
        {"catalog-number": "BBBCD02",
         "label": {"id": "lbl00000-3333-4444-8555-cccccccccccc",
                   "name": "Boutique Records"}}
    ],
    "media": [
        {"position": 1, "format": "CD", "track-count": 3,
         "tracks": [
             {"id": "v-1", "position": 1, "number": "1",
              "title": "Block Rockin' Beats", "length": 324000,
              "artist-credit": [
                  {"name": "The Chemical Brothers", "joinphrase": "",
                   "artist": {"id": "chem0000-4444-4555-8666-dddddddddddd",
                              "name": "The Chemical Brothers"}}],
              "recording": {"id": "rec-v1", "title": "Block Rockin' Beats",
                            "length": 324000}},
             {"id": "v-2", "position": 2, "number": "2",
              "title": "U Don't Know Me", "length": 367000,
              "artist-credit": [
                  {"name": "Armand Van Helden", "joinphrase": " feat. ",
                   "artist": {"id": "avh00000-5555-4666-8777-eeeeeeeeeeee",
                              "name": "Armand Van Helden"}},
                  {"name": "Duane Harden", "joinphrase": "",
                   "artist": {"id": "dh000000-6666-4777-8888-ffffffffffff",
                              "name": "Duane Harden"}}],
              "recording": {"id": "rec-v2", "title": "U Don't Know Me",
                            "length": 367000}},
             {"id": "v-3", "position": 3, "number": "3",
              "title": "Praise You", "length": 323000,
              "artist-credit": [
                  {"name": "Fatboy Slim", "joinphrase": "",
                   "artist": {"id": "fs000000-7777-4888-8999-000000000001",
                              "name": "Fatboy Slim"}}],
              "recording": {"id": "rec-v3", "title": "Praise You",
                            "length": 323000}}
         ]}
    ]
}

MB_SEARCH_RELEASE = {
    "created": "2026-01-01T00:00:00.000Z",
    "count": 2,
    "offset": 0,
    "releases": [
        {"id": MB_MEZZANINE["id"], "score": 100, "title": "Mezzanine",
         "status": "Official", "date": "1998-04-20", "country": "GB",
         "track-count": 11,
         "artist-credit": [
             {"name": "Massive Attack", "joinphrase": "",
              "artist": {"id": MA_MBID, "name": "Massive Attack"}}],
         "release-group": {"id": "a1b2c3d4-0000-4000-8000-abcdefabcdef",
                           "primary-type": "Album", "secondary-types": []}},
        {"id": MB_VA["id"], "score": 42, "title": "Mezzanine: The Remixes",
         "date": "1999-01-01", "country": "GB", "track-count": 8,
         "artist-credit": [
             {"name": "Various Artists", "joinphrase": "",
              "artist": {"id": VA_MBID, "name": "Various Artists"}}],
         "release-group": {"id": "rg-remix", "primary-type": "Album",
                           "secondary-types": ["Compilation", "Remix"]}}
    ]
}

MB_SEARCH_RECORDING = {
    "created": "2026-01-01T00:00:00.000Z",
    "count": 1,
    "offset": 0,
    "recordings": [
        {"id": "r3-mbid", "score": 98, "title": "Teardrop", "length": 333000,
         "artist-credit": [
             {"name": "Massive Attack", "joinphrase": "",
              "artist": {"id": MA_MBID, "name": "Massive Attack"}}],
         "releases": [{"id": MB_MEZZANINE["id"], "title": "Mezzanine"}]}
    ]
}

LFM_CORRECTION = {     # query was artist=blurr&track=song 2 (typos)
    "corrections": {
        "correction": {
            "track": {
                "name": "Song 2",
                "mbid": "5a1b4b9b-1111-4000-8000-cccccccccccc",
                "url": "https://www.last.fm/music/Blur/_/Song+2",
                "artist": {"name": "Blur",
                           "mbid": "ba853d1c-dbd0-4ff4-a9d4-c4f2d3b27a4a",
                           "url": "https://www.last.fm/music/Blur"}
            },
            "@attr": {"index": "0"}
        },
        "@attr": {"artist": "blurr", "track": "song 2"}
    }
}
LFM_NO_CORRECTION = {"corrections": {}}

ACOUSTID_HIT = {
    "status": "ok",
    "results": [
        {"id": "9ff43b6c-aaaa-bbbb-cccc-ddddeeeeffff",
         "score": 0.999218,
         "recordings": [
             {"duration": 333,
              "id": "r3-mbid",
              "title": "Teardrop",
              "artists": [{"id": MA_MBID, "name": "Massive Attack"}],
              "releasegroups": [
                  {"type": "Album", "id": "a1b2c3d4-0000-4000-8000-abcdefabcdef",
                   "title": "Mezzanine", "secondarytypes": []}
              ]}
         ]}
    ]
}
ACOUSTID_EMPTY = {"status": "ok", "results": []}
ACOUSTID_ERROR = {"status": "error",
                  "error": {"code": 4, "message": "invalid fingerprint"}}

DISCOGS_SEARCH = {
    "pagination": {"page": 1, "pages": 1, "per_page": 50, "items": 1,
                   "urls": {}},
    "results": [
        {"country": "UK", "year": "1998",
         "format": ["CD", "Album"],
         "label": ["Virgin", "Circa"],
         "type": "release", "id": 76424,
         "genre": ["Electronic"],
         "style": ["Trip Hop", "Downtempo"],
         "catno": "WBRCD4", "barcode": [],
         "uri": "/release/76424-Massive-Attack-Mezzanine",
         "title": "Massive Attack - Mezzanine"}
    ]
}


# =================================================================== tests

def test_spec_constants():
    print("\n== spec constants ==")
    check("single User-Agent", mr.USER_AGENT == "MediaOrganizer/1.0 (local app)",
          mr.USER_AGENT)
    iv = mr.HOST_INTERVALS
    want = {"musicbrainz.org": 1.05, "api.acoustid.org": 0.34,
            "api.discogs.com": 1.05, "ws.audioscrobbler.com": 1.0,
            "api.deezer.com": 0.12, "itunes.apple.com": 3.1}
    check("host intervals match spec", all(iv.get(h) == v for h, v in want.items()),
          str({h: iv.get(h) for h in want}))
    check("VA MBID is the MusicBrainz entity",
          mr.VA_MBID == "89ad4ac3-39f7-470e-963a-56509c546377", mr.VA_MBID)


def test_throttle_spaces_calls():
    print("\n== throttle spaces calls (mocked clock) ==")
    with fresh_cache(), mocked_clock() as clock:
        calls = []
        f = json_fetcher(MB_SEARCH_RELEASE, record=calls)
        mr.mb_search_release("Massive Attack", "Mezzanine", fetcher=f)
        mr.mb_search_release("Massive Attack", "Blue Lines", fetcher=f)
        check("two fetches for two distinct queries", len(calls) == 2,
              str(len(calls)))
        check("second MB call waited the 1.05s interval",
              abs(sum(clock.sleeps) - 1.05) < 1e-9, str(clock.sleeps))
        n_sleeps = len(clock.sleeps)
        # a different host is not blocked by MusicBrainz's throttle slot
        d = mr.discogs_search_release("Massive Attack", "Mezzanine", "TOK",
                                      fetcher=json_fetcher(DISCOGS_SEARCH))
        check("discogs (different host) not throttled by MB",
              d is not None and len(clock.sleeps) == n_sleeps,
              str(clock.sleeps))


def test_429_then_success():
    print("\n== 429 then success (Retry-After honored) ==")
    with fresh_cache(), mocked_clock() as clock:
        calls = []
        f = scripted_fetcher([
            (429, {"Retry-After": "1"}, {"error": "rate limited"}),
            (200, {}, MB_MEZZANINE),
        ], record=calls)
        rel = mr.mb_get_release(MB_MEZZANINE["id"], fetcher=f)
        check("429 retried, then release parsed",
              isinstance(rel, dict) and rel.get("title") == "Mezzanine",
              str(rel and rel.get("title")))
        check("fetcher called exactly twice", len(calls) == 2, str(len(calls)))
        check("slept the Retry-After second",
              any(abs(s - 1.0) < 1e-9 for s in clock.sleeps), str(clock.sleeps))


def test_503_default_backoff():
    print("\n== 503 without Retry-After -> default backoff ==")
    with fresh_cache(), mocked_clock() as clock:
        calls = []
        f = scripted_fetcher([(503, {}, {}), (200, {}, MB_SEARCH_RELEASE)],
                             record=calls)
        out = mr.mb_search_release("Massive Attack", "Mezzanine", fetcher=f)
        check("503 retried and succeeded", len(calls) == 2 and len(out) == 2,
              f"{len(calls)} calls, {len(out)} results")
        check("default backoff of 2.0s applied",
              any(abs(s - 2.0) < 1e-9 for s in clock.sleeps), str(clock.sleeps))


def test_retry_exhaustion():
    print("\n== retries exhausted: max 2 retries, failure not cached ==")
    with fresh_cache(), mocked_clock():
        calls = []
        f = scripted_fetcher([(429, {"Retry-After": "1"}, {})] * 3, record=calls)
        out = mr.mb_search_release("Xtc", "Skylarking", fetcher=f)
        check("gives up after 3 attempts (2 retries)",
              len(calls) == 3 and out == [], f"{len(calls)} calls -> {out}")
        # transient failure must not poison the cache: next call refetches
        out2 = mr.mb_search_release("Xtc", "Skylarking",
                                    fetcher=json_fetcher(MB_SEARCH_RELEASE))
        check("failure not cached, next call refetches",
              len(out2) == 2, str(len(out2)))


def test_cache_hit_skips_fetch():
    print("\n== cache hit skips the network ==")
    with fresh_cache(), mocked_clock():
        calls = []
        f = json_fetcher(MB_MEZZANINE, record=calls)
        rel1 = mr.mb_get_release(MB_MEZZANINE["id"], fetcher=f)
        check("first call fetched", len(calls) == 1 and rel1 is not None)
        rel2 = mr.mb_get_release(MB_MEZZANINE["id"], fetcher=raising_fetcher)
        check("second call served from cache, no fetch", rel2 == rel1)
        # search endpoint caches too
        s1 = mr.mb_search_release("Massive Attack", "Mezzanine",
                                  fetcher=json_fetcher(MB_SEARCH_RELEASE))
        s2 = mr.mb_search_release("Massive Attack", "Mezzanine",
                                  fetcher=raising_fetcher)
        check("search cache hit skips fetch", s1 == s2 and len(s1) == 2)
        # cache keys normalize case/whitespace
        s3 = mr.mb_search_release("  massive   ATTACK ", " MEZZANINE ",
                                  fetcher=raising_fetcher)
        check("cache key normalizes case/space", s3 == s1)


def test_404_negative_cached():
    print("\n== 404: confirmed absence is cached ==")
    with fresh_cache(), mocked_clock():
        calls = []
        f = json_fetcher({"error": "Not Found"}, status=404, record=calls)
        rel = mr.mb_get_release("00000000-no-such-mbid", fetcher=f)
        check("404 -> None", rel is None and len(calls) == 1)
        rel2 = mr.mb_get_release("00000000-no-such-mbid", fetcher=raising_fetcher)
        check("404 cached (no refetch)", rel2 is None)


def test_cache_primitives_and_schema():
    print("\n== cache_get/cache_set + mb_cache schema ==")
    with fresh_cache():
        check("cache miss returns None", mr.cache_get("nope") is None)
        check("cache_set reports success",
              mr.cache_set("k1", {"a": [1, 2, {"b": "c"}]}) is True)
        check("cache roundtrip", mr.cache_get("k1") == {"a": [1, 2, {"b": "c"}]})
        mr.cache_set("k1", [1])
        check("cache overwrite", mr.cache_get("k1") == [1])
        con = sqlite3.connect(mr.MUSIC_DB, timeout=30)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(mb_cache)")]
            check("mb_cache(key,value,fetched_at)",
                  cols == ["key", "value", "fetched_at"], str(cols))
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            check("journal_mode=WAL", str(mode).lower() == "wal", str(mode))
            row = con.execute("SELECT fetched_at FROM mb_cache"
                              " WHERE key = 'k1'").fetchone()
            check("fetched_at recorded", bool(row and row[0]), str(row))
        finally:
            con.close()


def test_parse_normal_album():
    print("\n== parser: normal album (Massive Attack - Mezzanine) ==")
    with fresh_cache(), mocked_clock():
        calls = []
        rel = mr.mb_get_release(MB_MEZZANINE["id"],
                                fetcher=json_fetcher(MB_MEZZANINE,
                                                     record=calls))
        check("title/artist", rel["title"] == "Mezzanine"
              and rel["artist"] == "Massive Attack",
              f"{rel['artist']} - {rel['title']}")
        check("date/country/barcode/status",
              rel["date"] == "1998-04-20" and rel["country"] == "GB"
              and rel["barcode"] == "724384959220"
              and rel["status"] == "Official")
        check("label + catno from label-info",
              rel["label"] == "Virgin" and rel["catno"] == "WBRCD4",
              f"{rel['label']} / {rel['catno']}")
        check("release-group types",
              rel["primary_type"] == "Album" and rel["secondary_types"] == []
              and rel["release_group_mbid"]
              == "a1b2c3d4-0000-4000-8000-abcdefabcdef")
        check("not VA", rel["is_va"] is False)
        check("artist_credits structured",
              rel["artist_credits"] == [{"name": "Massive Attack",
                                         "joinphrase": "", "mbid": MA_MBID}],
              str(rel["artist_credits"]))
        t = rel["tracks"]
        check("3 tracks parsed", len(t) == 3, str(len(t)))
        check("track 1: disc/track/title/duration",
              t[0]["disc"] == 1 and t[0]["track"] == 1
              and t[0]["title"] == "Angel" and t[0]["duration_s"] == 378.0,
              str(t[0]))
        check("track artist from recording credit",
              t[2]["title"] == "Teardrop" and t[2]["artist"] == "Massive Attack"
              and t[2]["recording_mbid"] == "r3-mbid", str(t[2]))
        url = calls[0]["url"]
        check("lookup URL: ws/2 + full inc + fmt=json",
              "musicbrainz.org/ws/2/release/" in url
              and "inc=recordings+artist-credits+release-groups+labels" in url
              and "fmt=json" in url, url)
        check("User-Agent header sent",
              calls[0]["headers"].get("User-Agent") == mr.USER_AGENT,
              str(calls[0]["headers"]))


def test_parse_va_compilation():
    print("\n== parser: VA compilation + feat. join phrases ==")
    with fresh_cache(), mocked_clock():
        rel = mr.mb_get_release(MB_VA["id"], fetcher=json_fetcher(MB_VA))
        check("album artist is Various Artists", rel["artist"] == "Various Artists")
        check("VA flag via MBID", rel["is_va"] is True)
        check("Compilation secondary type",
              rel["secondary_types"] == ["Compilation"],
              str(rel["secondary_types"]))
        t = rel["tracks"]
        check("per-track artist preserved",
              t[0]["artist"] == "The Chemical Brothers", str(t[0]["artist"]))
        check("feat. join phrase rendered Picard-style",
              t[1]["artist"] == "Armand Van Helden feat. Duane Harden",
              t[1]["artist"])
        check("track durations in seconds",
              t[1]["duration_s"] == 367.0 and t[2]["duration_s"] == 323.0)


def test_artist_credit_joinphrases():
    print("\n== artist-credit join phrases (unit) ==")
    s = mr._artist_credit_string([
        {"name": "A", "joinphrase": " feat. "},
        {"name": "B", "joinphrase": " & "},
        {"name": "C", "joinphrase": ""}])
    check("chained join phrases", s == "A feat. B & C", s)
    s2 = mr._artist_credit_string([{"artist": {"name": "Only Artist"}}])
    check("falls back to artist.name", s2 == "Only Artist", s2)
    check("empty credit -> ''", mr._artist_credit_string(None) == "")
    check("VA detection", mr._is_va_credit(
        [{"artist": {"id": mr.VA_MBID}}]) is True
        and mr._is_va_credit([{"artist": {"id": MA_MBID}}]) is False)


def test_search_parsers():
    print("\n== parsers: release + recording searches ==")
    with fresh_cache(), mocked_clock():
        calls = []
        out = mr.mb_search_release("Massive Attack", "Mezzanine",
                                   fetcher=json_fetcher(MB_SEARCH_RELEASE,
                                                        record=calls))
        check("2 hits", len(out) == 2, str(len(out)))
        check("hit 1 summary",
              out[0]["mbid"] == MB_MEZZANINE["id"]
              and out[0]["title"] == "Mezzanine"
              and out[0]["artist"] == "Massive Attack"
              and out[0]["score"] == 100 and out[0]["is_va"] is False
              and out[0]["track_count"] == 11)
        check("hit 2 flagged VA compilation+remix",
              out[1]["is_va"] is True
              and out[1]["secondary_types"] == ["Compilation", "Remix"],
              str(out[1]["secondary_types"]))
        check("search URL is a quoted Lucene query",
              "ws/2/release/?" in calls[0]["url"] and "query=" in calls[0]["url"]
              and "fmt=json" in calls[0]["url"], calls[0]["url"])
        recs = mr.mb_search_recording("Massive Attack", "Teardrop",
                                      fetcher=json_fetcher(MB_SEARCH_RECORDING))
        check("recording search hit",
              len(recs) == 1 and recs[0]["mbid"] == "r3-mbid"
              and recs[0]["duration_s"] == 333.0
              and recs[0]["releases"][0]["title"] == "Mezzanine", str(recs))
        check("blank inputs short-circuit (no fetch)",
              mr.mb_search_release("", "", fetcher=raising_fetcher) == []
              and mr.mb_search_recording("A", "", fetcher=raising_fetcher) == []
              and mr.mb_get_release(None, fetcher=raising_fetcher) is None)


def test_lastfm_correction():
    print("\n== Last.fm track.getCorrection ==")
    with fresh_cache(), mocked_clock():
        calls = []
        corr = mr.lastfm_correction("blurr", "song 2", "KEY",
                                    fetcher=json_fetcher(LFM_CORRECTION,
                                                         record=calls))
        check("typo corrected to Blur / Song 2",
              corr == {"artist": "Blur", "title": "Song 2"}, str(corr))
        check("URL uses track.getcorrection + json",
              "method=track.getcorrection" in calls[0]["url"]
              and "format=json" in calls[0]["url"], calls[0]["url"])
        none = mr.lastfm_correction("obscure artist", "obscure track", "KEY",
                                    fetcher=json_fetcher(LFM_NO_CORRECTION))
        check("no correction -> None", none is None)
        check("missing key short-circuits",
              mr.lastfm_correction("a", "b", "", fetcher=raising_fetcher) is None)
        check("correction cached",
              mr.lastfm_correction("blurr", "song 2", "KEY",
                                   fetcher=raising_fetcher) == corr)


def test_acoustid_lookup():
    print("\n== AcoustID v2 lookup (gzipped POST) ==")
    with fresh_cache(), mocked_clock():
        captured = []
        fp = "AQADtEmiRJmk0ZZnf5AMc1-P7kOeR0eq40eDH8dx9MfxIMdxXMdxHMeL4zrO4TyO"
        out = mr.acoustid_lookup(fp, 333.4, "TESTKEY",
                                 fetcher=json_fetcher(ACOUSTID_HIT,
                                                      record=captured))
        check("hit normalized",
              out and out["results"][0]["acoustid"]
              == "9ff43b6c-aaaa-bbbb-cccc-ddddeeeeffff"
              and abs(out["results"][0]["score"] - 0.999218) < 1e-9, str(out))
        rec = out["results"][0]["recordings"][0]
        check("recording + release-group parsed",
              rec["recording_mbid"] == "r3-mbid" and rec["title"] == "Teardrop"
              and rec["artist"] == "Massive Attack" and rec["duration_s"] == 333
              and rec["release_groups"][0]["primary_type"] == "Album",
              str(rec))
        call = captured[0]
        check("POST to /v2/lookup",
              call["url"] == "https://api.acoustid.org/v2/lookup", call["url"])
        check("Content-Encoding: gzip",
              call["headers"].get("Content-Encoding") == "gzip"
              and call["headers"].get("Content-Type")
              == "application/x-www-form-urlencoded",
              str(call["headers"]))
        check("User-Agent on POST too",
              call["headers"].get("User-Agent") == mr.USER_AGENT)
        raw = gzip.decompress(call["data"]).decode("utf-8")
        fields = urllib.parse.parse_qs(raw)
        check("body gunzips to the form fields",
              fields.get("client") == ["TESTKEY"]
              and fields.get("duration") == ["333"]
              and fields.get("fingerprint") == [fp]
              and fields.get("meta") == ["recordings releasegroups compress"],
              raw[:120])
        check("meta=recordings+releasegroups+compress on the wire",
              "meta=recordings+releasegroups+compress" in raw)
        check("API key never in the cache key",
              all("TESTKEY" not in k for k in _cache_keys()))
        # no-match / error / short-circuit paths
        check("empty results -> None",
              mr.acoustid_lookup("fp-nomatch", 100, "K",
                                 fetcher=json_fetcher(ACOUSTID_EMPTY)) is None)
        check("API error status -> None",
              mr.acoustid_lookup("fp-bad", 100, "K",
                                 fetcher=json_fetcher(ACOUSTID_ERROR)) is None)
        check("missing key short-circuits",
              mr.acoustid_lookup("fp", 1, "", fetcher=raising_fetcher) is None)
        check("result cached",
              mr.acoustid_lookup(fp, 333, "TESTKEY",
                                 fetcher=raising_fetcher) == out)


def _cache_keys():
    con = sqlite3.connect(mr.MUSIC_DB, timeout=30)
    try:
        return [r[0] for r in con.execute("SELECT key FROM mb_cache")]
    finally:
        con.close()


def test_discogs():
    print("\n== Discogs database/search (token auth) ==")
    with fresh_cache(), mocked_clock():
        calls = []
        out = mr.discogs_search_release("Massive Attack", "Mezzanine",
                                        "TESTTOKEN",
                                        fetcher=json_fetcher(DISCOGS_SEARCH,
                                                             record=calls))
        check("top hit normalized",
              out and out["id"] == 76424
              and out["title"] == "Massive Attack - Mezzanine"
              and out["year"] == 1998 and out["genres"] == ["Electronic"]
              and out["styles"] == ["Trip Hop", "Downtempo"]
              and out["catno"] == "WBRCD4", str(out))
        check("Authorization header carries the token",
              calls[0]["headers"].get("Authorization")
              == "Discogs token=TESTTOKEN", str(calls[0]["headers"]))
        check("search params in URL",
              "type=release" in calls[0]["url"]
              and "release_title=Mezzanine" in calls[0]["url"], calls[0]["url"])
        check("token never in the cache key",
              all("TESTTOKEN" not in k for k in _cache_keys()))
        check("no token -> None without fetch",
              mr.discogs_search_release("A", "B", "",
                                        fetcher=raising_fetcher) is None)
        empty = dict(DISCOGS_SEARCH, results=[])
        check("empty results -> None",
              mr.discogs_search_release("A", "Nope", "TOK",
                                        fetcher=json_fetcher(empty)) is None)


def test_cover_art_front():
    print("\n== Cover Art Archive front ==")
    with fresh_cache(), mocked_clock():
        jpeg = b"\xff\xd8\xff\xe0" + b"JFIFDATA" * 16
        calls = []
        f = json_fetcher(jpeg, resp_headers={"Content-Type": "image/jpeg"},
                         record=calls)
        blob = mr.cover_art_front("mbid-art", fetcher=f)
        check("image bytes returned verbatim", blob == jpeg,
              f"{len(blob or b'')} bytes")
        check("URL shape /release/{mbid}/front",
              calls[0]["url"].endswith("/release/mbid-art/front"),
              calls[0]["url"])
        blob2 = mr.cover_art_front("mbid-art", fetcher=raising_fetcher)
        check("cached bytes, no refetch", blob2 == jpeg)
        f404 = json_fetcher(b"", status=404)
        check("404 -> None", mr.cover_art_front("mbid-noart", fetcher=f404) is None)
        check("404 cached (no refetch)",
              mr.cover_art_front("mbid-noart", fetcher=raising_fetcher) is None)
        check("blank mbid short-circuits",
              mr.cover_art_front(None, fetcher=raising_fetcher) is None)


def test_failures_never_raise():
    print("\n== failures never raise into callers ==")
    with fresh_cache(), mocked_clock():
        def boom(url, headers=None, data=None):
            raise OSError("network down")
        checks = [
            ("mb_get_release", mr.mb_get_release("x-mbid", fetcher=boom), None),
            ("mb_search_release", mr.mb_search_release("a", "b", fetcher=boom), []),
            ("mb_search_recording", mr.mb_search_recording("a", "b", fetcher=boom), []),
            ("cover_art_front", mr.cover_art_front("x-mbid", fetcher=boom), None),
            ("acoustid_lookup", mr.acoustid_lookup("fp-x", 1, "k", fetcher=boom), None),
            ("lastfm_correction", mr.lastfm_correction("a", "b", "k", fetcher=boom), None),
            ("discogs_search_release",
             mr.discogs_search_release("a", "b", "t", fetcher=boom), None),
        ]
        for name, got, want in checks:
            check(f"{name} -> {want!r} on transport failure", got == want,
                  repr(got))
        check("malformed JSON -> None",
              mr.mb_get_release("weird-mbid",
                                fetcher=json_fetcher(b"{not json")) is None)
        check("malformed search JSON -> []",
              mr.mb_search_release("a", "malformed",
                                   fetcher=json_fetcher(b"\x00\x01")) == [])


# =================================================================== main

def main():
    original_db = mr.MUSIC_DB
    preexisting = {s for s in ("", "-wal", "-shm")
                   if os.path.exists(os.path.join(BASE, "music.db" + s))}
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    mr.MUSIC_DB = os.path.join(TMP, "music.db")
    try:
        test_spec_constants()
        test_throttle_spaces_calls()
        test_429_then_success()
        test_503_default_backoff()
        test_retry_exhaustion()
        test_cache_hit_skips_fetch()
        test_404_negative_cached()
        test_cache_primitives_and_schema()
        test_parse_normal_album()
        test_parse_va_compilation()
        test_artist_credit_joinphrases()
        test_search_parsers()
        test_lastfm_correction()
        test_acoustid_lookup()
        test_discogs()
        test_cover_art_front()
        test_failures_never_raise()
    finally:
        mr.MUSIC_DB = original_db
        mr._SCHEMA_READY_PATH = None
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n== hygiene: no music.db leaked into project root ==")
    leaked = []
    for s in ("", "-wal", "-shm"):
        p = os.path.join(BASE, "music.db" + s)
        if os.path.exists(p) and s not in preexisting:
            leaked.append(p)
            try:
                os.remove(p)     # our own test artifact, safe to remove
            except OSError:
                pass
    check("project root stays free of music.db artifacts", not leaked,
          str(leaked))

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
