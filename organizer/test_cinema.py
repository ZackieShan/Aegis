#!/usr/bin/env python3
r"""Tests for the Cinema Organizer feature.

Unit tests (parser, sample-size rule, mocked TMDB, genre cache) run
in-process against a temp cinema.db. HTTP tests run against a real server
on port 8137 with synthetic fixtures ONLY (never D:\Movies).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "cinema_fixtures")
CANCEL_DIR = os.path.join(BASE, "cinema_cancel_tmp")
TARGET = os.path.join(BASE, "cinema_out_tmp")
DB = os.path.join(BASE, "cinema.db")
CONFIG = os.path.join(BASE, "cinema_config.json")
PORT = 8137
HOST = f"http://127.0.0.1:{PORT}"
N_CANCEL = 400

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def req(method, path, body=None, timeout=30, expect_error=False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(HOST + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if expect_error:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}
        raise


def wait_port(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req("GET", "/api/cinema/scan/status")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def poll(path, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = req("GET", path)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.3)
    raise TimeoutError(path)


# ============================================================ unit tests

def unit_tests():
    sys.path.insert(0, BASE)
    import cinema

    print("\n== parser unit tests ==")
    CASES = [
        # (name, kind, title, year, season, episode, season_pack, is_sample)
        ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv", "movie", "The Matrix", 1999, None, None, False, False),
        ("The Matrix (1999) 1080p.mkv", "movie", "The Matrix", 1999, None, None, False, False),
        ("Inception (2010) 720p.mp4", "movie", "Inception", 2010, None, None, False, False),
        ("Alien Romulus 2024 2160p WEB-DL.mkv", "movie", "Alien Romulus", 2024, None, None, False, False),
        ("Some.Show.S01E01.1080p.WEB-DL.mkv", "tv", "Some Show", None, 1, 1, False, False),
        ("Some.Show.S01E02.1080p.WEB-DL.mkv", "tv", "Some Show", None, 1, 2, False, False),
        ("Some.Show.S01.1080p.WEB-DL.mkv", "tv", "Some Show", None, 1, None, True, False),
        ("Old.Rock.1x02.DVDRip.avi", "tv", "Old Rock", None, 1, 2, False, False),
        ("random_home_video.mkv", "unknown", None, None, None, None, False, False),
        ("Oceans Eleven.mkv", "unknown", None, None, None, None, False, False),
        ("The.Matrix.1999.1080p.BluRay.x264-GROUP.sample.mkv", "movie", "The Matrix", 1999, None, None, False, True),
        ("2012 (2009).mkv", "movie", "2012", 2009, None, None, False, False),
        ("Show Name (US) S01E01.mkv", "tv", "Show Name", None, 1, 1, False, False),
        ("Movie (1899).mkv", "unknown", None, None, None, None, False, False),
        ("Movie (2099).mkv", "unknown", None, None, None, None, False, False),
        ("My Show Season 1 Episode 2.mkv", "tv", "My Show", None, 1, 2, False, False),
    ]
    for name, kind, title, year, season, ep, pack, samp in CASES:
        p = cinema.parse_media_name(name)
        got = (p["kind"], p["title"], p["year"], p["season"], p["episode"],
               p["season_pack"], p["is_sample"])
        want = (kind, title, year, season, ep, pack, samp)
        check(f"parse {name}", got == want, f"got {got}")

    print("\n== parser: yearless / fansub / year-first / numeric ==")
    NEW = [
        # (name, kind, title, year, guess_title)
        ("1408 (720p).mp4", "unknown", None, None, "1408"),
        ("2081 (720p).avi", "unknown", None, None, "2081"),
        ("22 Jump Street (720p).mkv", "unknown", None, None, "22 Jump Street"),
        ("13 Assassins.avi", "unknown", None, None, "13 Assassins"),
        ("50-50.avi", "unknown", None, None, "50-50"),
        ("(1984) Nausicaä of the Valley of the Wind~Kaze no Tani no Nausicaä"
         " (720p Blu-ray 8bit Dual Audio) [NoobSubs] [86760B55].mkv",
         "movie", "Nausicaä of the Valley of the Wind", 1984, None),
        ("(1988) Grave of the Fireflies~Hotaru no Haka"
         " (720p Blu-ray 8bit Dual Audio) [NoobSubs] [3E904CC4].mkv",
         "movie", "Grave of the Fireflies", 1988, None),
        ("2001 - A Space Odyssey.avi", "movie", "A Space Odyssey", 2001,
         "2001 A Space Odyssey"),
        ("random_home_video.mkv", "unknown", None, None, "Random Home Video"),
        ("5-Vengeful Beauty DVDrip {BALA}.mp4", "unknown", None, None,
         "Vengeful Beauty"),
        ("Re-Animator.mp4", "unknown", None, None, "Re-Animator"),
    ]
    for name, kind, title, year, guess in NEW:
        p = cinema.parse_media_name(name)
        got = (p["kind"], p["title"], p["year"], p.get("guess_title"))
        want = (kind, title, year, guess)
        check(f"parse {name[:58]}", got == want, f"got {got}")

    q = cinema.parse_media_name(
        "(1984) Nausicaä of the Valley of the Wind~Kaze no Tani no Nausicaä"
        " (720p Blu-ray 8bit Dual Audio) [NoobSubs] [86760B55].mkv")
    check("fansub keeps 720p quality tag", "720p" in q["tags"], str(q["tags"]))

    print("\n== parser: TV absolute / anime / loose forms ==")
    TVCASES = [
        # (name, kind, title, season, episode, season_pack)
        ("Cowboy.Bebop.1x06.1080p.mkv", "tv", "Cowboy Bebop", 1, 6, False),
        ("[NoobSubs] Cowboy Bebop - 05 (1080p Blu-ray 8bit AAC) [CRC].mkv",
         "tv", "Cowboy Bebop", 1, 5, False),
        ("Cowboy Bebop - 05.mkv", "tv", "Cowboy Bebop", 1, 5, False),
        ("Show.E05.1080p.mkv", "tv", "Show", 1, 5, False),
        ("Some Show - 105.mkv", "tv", "Some Show", 1, 105, False),
        ("[Group] Show - 05v2 (1080p).mkv", "tv", "Show", 1, 5, False),
        ("Show S2 - 05.mkv", "tv", "Show", 2, 5, False),
        ("IT Crowd S03 - E05 - Friendface.mkv", "tv", "IT Crowd", 3, 5, False),
        ("Pokemon the Series XY S17E05.mkv", "tv", "Pokemon the Series XY",
         17, 5, False),
        ("Pokémon SE1 EP024 - Haunter Versus Kadabra.avi", "tv", "Pokémon",
         1, 24, False),
        ("Pokemon - 001 - Pokemon, I Choose You!.mkv", "tv", "Pokemon",
         1, 1, False),
        ("[CBM]_Cowboy_Bebop_-_Session_05_-_Ballad_of_Fallen_Angels_"
         "[720p]_[3457B12D].mkv", "tv", "Cowboy Bebop", 1, 5, False),
        ("Show Episode 52 - Legacy Of Laughter.mkv", "tv", "Show", 1, 52, False),
        ("Red.Skelton.In.Color.E11.DVDRip.XviD.avi", "tv",
         "Red Skelton In Color", 1, 11, False),
        ("Miami Mega-Jail ep01.avi", "tv", "Miami Mega-Jail", 1, 1, False),
        ("The Mighty Boosh - 101 - Killeroo.avi", "tv", "The Mighty Boosh",
         1, 101, False),
        ("Cowboy Bebop~Kauboi Bibappu - 05 (1080p).mkv", "tv",
         "Cowboy Bebop", 1, 5, False),
        ("Some.Show.S01E01E02.1080p.mkv", "tv", "Some Show", 1, 1, False),
    ]
    for name, kind, title, season, ep, pack in TVCASES:
        p = cinema.parse_media_name(name)
        got = (p["kind"], p["title"], p["season"], p["episode"],
               p["season_pack"])
        want = (kind, title, season, ep, pack)
        check(f"parse {name[:58]}", got == want, f"got {got}")

    p = cinema.parse_media_name("Some.Show.S01E01E02.1080p.mkv")
    check("multi-episode keeps full run", p.get("episodes") == [1, 2],
          str(p.get("episodes")))
    p = cinema.parse_media_name("Some.Show.S01E01.1080p.mkv")
    check("single episode run", p.get("episodes") == [1], str(p.get("episodes")))

    print("\n== parser: TV-shaped but unknowable series stays unknown ==")
    for name in ("S15E03 - The A-Team Special.mkv", "01 - Pilot.avi",
                 "Episode 52 - Legacy Of Laughter.avi",
                 "E9 The Legend Of Merle McQuoddy.avi",
                 "S03 - E05 - Friendface.mkv",
                 "01 - 14-Carrot Rabbit.avi",
                 "1012 - 24 Hour Propane People.mkv",
                 "723 - The Witches of East Arlen.mkv"):
        p = cinema.parse_media_name(name)
        check(f"no movie guess for {name[:48]}",
              p["kind"] == "unknown" and p.get("guess_title") is None,
              f"{p['kind']} guess={p.get('guess_title')!r}")

    p = cinema.parse_media_name(
        "Joel Hodgson - 8th Annual Young Comedians Show.avi")
    check("' - 8th' is not an episode number", p["kind"] == "unknown",
          f"{p['kind']} S{p['season']}E{p['episode']}")
    p = cinema.parse_media_name("24 S01E01.mkv")
    check("numeric series title still works with SxxEyy",
          p["kind"] == "tv" and p["title"] == "24"
          and (p["season"], p["episode"]) == (1, 1),
          f"{p['kind']} {p['title']!r}")
    p = cinema.parse_media_name("[Group] Show - 05v2 (1080p).mkv")
    check("v2 version tag still allowed",
          p["kind"] == "tv" and p["episode"] == 5,
          f"{p['kind']} E{p['episode']}")

    print("\n== parser: year guard keeps movies as movies ==")
    for name, title, year in (
            ("Movie - 300 (2006).mkv", "Movie - 300", 2006),
            ("Session 9 (2001).mkv", "Session 9", 2001),
            ("Star Wars Episode 1 The Phantom Menace (1999).mkv",
             "Star Wars Episode 1 The Phantom Menace", 1999),
            ("Judex (Episode 03) The Fantastic Dog Pack (1917).mpg",
             "Judex The Fantastic Dog Pack", 1917)):
        p = cinema.parse_media_name(name)
        check(f"year-guard {name[:48]}",
              p["kind"] == "movie" and p["title"] == title
              and p["year"] == year,
              f"{p['kind']} {p['title']!r} {p['year']}")
    p = cinema.parse_media_name("d-mptmol-720p.mkv")
    check("scene release tail not an episode number",
          p["kind"] == "unknown", f"{p['kind']} {p['title']!r}")

    print("\n== tv_dest scheme: Genre\\\\Sub-genre\\\\Title\\\\Season NN (no year) ==")
    rec = {"title": "Cowboy Bebop", "year": 1998, "season": 1, "episode": 5,
           "episodes": [5], "ext": ".mkv", "name": "bebop05.mkv",
           "genre": "Animation", "subgenre": "Sci-Fi & Fantasy"}
    d = cinema.tv_dest(rec, "ROOT")
    want = os.path.join("ROOT", "Animation", "Sci-Fi & Fantasy",
                        "Cowboy Bebop", "Season 01",
                        "Cowboy Bebop - S01E05.mkv")
    check("tv dest has sub-genre level", d == want, d)
    check("tv dest has no year component", "1998" not in d, d)
    rec2 = dict(rec, episodes=[1, 2], episode=1)
    d2 = cinema.tv_dest(rec2, "ROOT")
    check("multi-episode tag preserved",
          d2.endswith(os.path.join("Season 01",
                                   "Cowboy Bebop - S01E01E02.mkv")), d2)
    rec3 = dict(rec, genre=None, subgenre=None, episodes=None)
    d3 = cinema.tv_dest(rec3, "ROOT")
    check("missing genre -> Unclassified/General",
          d3 == os.path.join("ROOT", "Unclassified", "General",
                             "Cowboy Bebop", "Season 01",
                             "Cowboy Bebop - S01E05.mkv"), d3)
    rec4 = {"title": "Some Show", "season": 1, "episode": None,
            "season_pack": True, "ext": ".mkv",
            "name": "Some.Show.S01.1080p.WEB-DL.mkv",
            "genre": "Drama", "subgenre": "General"}
    d4 = cinema.tv_dest(rec4, "ROOT")
    check("season pack keeps original name under sub-genre",
          d4 == os.path.join("ROOT", "Drama", "General", "Some Show",
                             "Season 01", "Some.Show.S01.1080p.WEB-DL.mkv"),
          d4)
    mrec = {"title": "The Matrix", "year": 1999, "ext": ".mkv",
            "genre": "Action", "subgenre": "Science Fiction",
            "name": "matrix.mkv"}
    dm = cinema.movie_dest(mrec, "ROOT")
    check("movie dest keeps year scheme",
          dm == os.path.join("ROOT", "Action", "Science Fiction", "1999",
                             "The Matrix (1999)", "The Matrix (1999).mkv"),
          dm)

    print("\n== identify_tv (mocked TMDB, no network) ==")
    tv_calls = []

    def fake_tv(url):
        tv_calls.append(url)
        if "/genre/tv/list" in url:
            return {"genres": [{"id": 16, "name": "Animation"},
                               {"id": 10765, "name": "Sci-Fi & Fantasy"},
                               {"id": 10759, "name": "Action & Adventure"}]}
        if "/search/tv" in url:
            import urllib.parse as up
            q = up.parse_qs(up.urlparse(url).query).get("query", [""])[0]
            if q.lower() == "cowboy bebop":
                return {"results": [
                    {"id": 1, "name": "Cowboy Bebop",
                     "original_name": "カウボーイビバップ",
                     "first_air_date": "1998-04-03",
                     "genre_ids": [16, 10765, 10759]}]}
            if q.lower() == "the office":
                return {"results": [
                    {"id": 2, "name": "The Office",
                     "first_air_date": "2001-07-09", "genre_ids": [35]},
                    {"id": 3, "name": "The Office",
                     "first_air_date": "2005-03-24", "genre_ids": [35]}]}
            return {"results": [
                {"id": 9, "name": "Totally Different Show",
                 "first_air_date": "2010-01-01", "genre_ids": [16]}]}
        raise AssertionError("unexpected url " + url)

    r = cinema.identify_tv("Cowboy Bebop", None, "KEY",
                           fetcher=fake_tv, _maps={})
    check("bebop -> Animation/Sci-Fi & Fantasy",
          r == ("Cowboy Bebop", 1998, "Animation", "Sci-Fi & Fantasy",
                "tmdb"), str(r))
    check("tv search carries no year filter",
          all("year=" not in u for u in tv_calls), str(tv_calls))
    r = cinema.identify_tv("The Office", 2005, "KEY",
                           fetcher=fake_tv, _maps={})
    check("parsed year prefers the matching series",
          r[0] == "The Office" and r[1] == 2005, str(r))
    r = cinema.identify_tv("Random Home Video", None, "KEY",
                           fetcher=fake_tv, _maps={})
    check("similarity gate rejects unrelated tv hit",
          r == (None, None, None, None, "none"), str(r))
    n = len(tv_calls)
    r = cinema.identify_tv("Cowboy Bebop", None, "", fetcher=fake_tv,
                           _maps={})
    check("no key -> none without fetching",
          r == (None, None, None, None, "none") and len(tv_calls) == n,
          str(r))

    print("\n== diacritic fold + similarity gate ==")
    check("fold strips diacritics", cinema._fold("Nausicaä") == "Nausicaa")
    check("similarity exact after fold",
          cinema.title_similarity("Nausicaä of the Valley of the Wind",
                                  "Nausicaa of the Valley of the Wind") == 1.0)
    check("gate accepts 50-50 vs 50/50", cinema._sim_accept("50-50", "50/50"))
    check("gate accepts numeric 1408", cinema._sim_accept("1408", "1408"))
    check("gate drops release tail",
          cinema._sim_accept("One Flew Over The Cuckoo's Nest YIFY",
                             "One Flew Over the Cuckoo's Nest"))
    check("gate rejects unrelated",
          not cinema._sim_accept("Random Home Video", "The Matrix"))

    q = cinema.parse_media_name("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv")
    check("quality 1080p bluray = 3090", q["quality_score"] == 3090,
          str(q["quality_score"]))
    q = cinema.parse_media_name("Some.Show.S01E01.720p.HDTV.mkv")
    check("quality 720p hdtv = 2050", q["quality_score"] == 2050,
          str(q["quality_score"]))
    q = cinema.parse_media_name("Old.Rock.1x02.DVDRip.avi")
    check("quality dvdrip-nores = 1030", q["quality_score"] == 1030,
          str(q["quality_score"]))
    q = cinema.parse_media_name("Alien Romulus 2024 2160p WEB-DL.mkv")
    check("no false group tag from WEB-DL", not any(t.startswith("group:") for t in q["tags"]),
          str(q["tags"]))
    q = cinema.parse_media_name("Some.Movie.2020.HDCAM.mkv")
    check("cam source flagged low quality", q["low_quality"] is True)

    print("\n== clutter rules ==")
    stems = {"the.matrix.1999.1080p.bluray.x264-group"}
    check("nfo matching video stem is clutter",
          cinema.looks_like_clutter("The.Matrix.1999.1080p.BluRay.x264-GROUP.nfo", stems))
    check("screenshot is clutter", cinema.looks_like_clutter("screenshot01.jpg", set()))
    check("poster is clutter", cinema.looks_like_clutter("poster.jpg", set()))
    check("evil_screenshot.exe is clutter", cinema.looks_like_clutter("evil_screenshot.exe", set()))
    check("setup.exe NOT clutter", not cinema.looks_like_clutter("setup.exe", set()))
    check("readme.txt NOT clutter", not cinema.looks_like_clutter("readme.txt", set()))

    print("\n== sample-by-size rule (in-process scan, temp db) ==")
    tmpdir = tempfile.mkdtemp(prefix="cine_unit_")
    tmpdb = os.path.join(tmpdir, "unit.db")
    old_db, old_size = cinema.CINEMA_DB, cinema.SAMPLE_SIZE
    old_cfg_outer = cinema.CONFIG_PATH
    try:
        cinema.CINEMA_DB = tmpdb
        cinema.SAMPLE_SIZE = 1000   # shrink the 50MB rule for the test
        # hermetic: an empty config keeps this phase off the real network
        empty_cfg = os.path.join(tmpdir, "cfg_empty.json")
        with open(empty_cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        cinema.CONFIG_PATH = empty_cfg
        cinema.db_init()
        vdir = os.path.join(tmpdir, "vids")
        os.makedirs(vdir)
        with open(os.path.join(vdir, "Big.Movie.2020.1080p.mkv"), "wb") as f:
            f.write(b"X" * 5000)
        with open(os.path.join(vdir, "Big.Movie.2020.720p.mkv"), "wb") as f:
            f.write(b"Y" * 200)     # small sibling -> sample by size
        with open(os.path.join(vdir, "Alone.Movie.2021.1080p.mkv"), "wb") as f:
            f.write(b"Z" * 200)     # small but no big sibling -> not sample
        cinema.SCAN_CANCEL.clear()
        cinema.run_scan(vdir, 0, False)
        recs = {r["name"]: r for r in cinema.STATE["recs"]}
        check("small sibling marked sample",
              recs["Big.Movie.2020.720p.mkv"]["is_sample"] is True)
        check("big sibling not sample",
              recs["Big.Movie.2020.1080p.mkv"]["is_sample"] is False)
        check("lone small file not sample",
              recs["Alone.Movie.2021.1080p.mkv"]["is_sample"] is False)

        print("\n== genre cache ==")
        cinema.genre_cache_put("movie", "The Matrix", 1999, "Action", "Science Fiction", "tmdb")
        got = cinema.genre_cache_get("movie", "The Matrix", 1999)
        check("cache roundtrip", got == ("Action", "Science Fiction", "tmdb"), str(got))
        got = cinema.genre_cache_get("movie", "the  MATRIX!! ", 1999)
        check("cache key normalizes title", got == ("Action", "Science Fiction", "tmdb"), str(got))
        check("cache miss returns None", cinema.genre_cache_get("movie", "Nope", 2001) is None)

        print("\n== mocked TMDB ==")
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if "/genre/movie/list" in url:
                return {"genres": [{"id": 28, "name": "Action"},
                                   {"id": 878, "name": "Science Fiction"},
                                   {"id": 12, "name": "Adventure"}]}
            if "/search/movie" in url:
                return {"results": [
                    {"id": 1, "title": "The Matrix",
                     "release_date": "1999-03-31", "genre_ids": [28, 878]}]}
            if "/genre/tv/list" in url:
                return {"genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}]}
            if "/search/tv" in url:
                return {"results": [
                    {"id": 2, "name": "Some Show",
                     "first_air_date": "2020-01-01", "genre_ids": [10765]}]}
            raise AssertionError("unexpected url " + url)

        g = cinema.lookup_genre("movie", "The Matrix", 1999, "KEY",
                                fetcher=fake_fetch, _maps={})
        check("tmdb movie -> Action/Science Fiction/tmdb",
              g == ("Action", "Science Fiction", "tmdb"), str(g))
        g = cinema.lookup_genre("tv", "Some Show", None, "KEY",
                                fetcher=fake_fetch, _maps={})
        check("tmdb tv -> Sci-Fi & Fantasy/General/tmdb",
              g == ("Sci-Fi & Fantasy", "General", "tmdb"), str(g))

        def fake_year_mismatch(url):
            if "/genre/movie/list" in url:
                return {"genres": [{"id": 28, "name": "Action"}]}
            return {"results": [
                {"id": 9, "title": "The Matrix Regurgitated",
                 "release_date": "1970-01-01", "genre_ids": [28]},
                {"id": 1, "title": "The Matrix",
                 "release_date": "1999-03-31", "genre_ids": [28]}]}

        g = cinema.lookup_genre("movie", "The Matrix", 1999, "KEY",
                                fetcher=fake_year_mismatch, _maps={})
        check("year-mismatched hit skipped for next", g == ("Action", "General", "tmdb"), str(g))

        def fake_all_mismatch(url):
            if "/genre/movie/list" in url:
                return {"genres": [{"id": 28, "name": "Action"}]}
            return {"results": [
                {"id": 9, "title": "X", "release_date": "1970-01-01", "genre_ids": [28]}]}

        g = cinema.lookup_genre("movie", "The Matrix", 1999, "KEY",
                                fetcher=fake_all_mismatch, _maps={})
        check("all mismatched -> none", g == (None, None, "none"), str(g))

        n_before = len(calls)
        g = cinema.lookup_genre("movie", "The Matrix", 1999, "",
                                fetcher=fake_fetch, _maps={})
        check("no api key -> none without fetching",
              g == (None, None, "none") and len(calls) == n_before, str(g))

        # cached 'tmdb' row must be used by run_scan without calling the lookup
        calls.clear()
        ck = cinema.genre_cache_get("movie", "The Matrix", 1999)
        check("cached tmdb row preferred (no fetch needed)",
              ck == ("Action", "Science Fiction", "tmdb") and len(calls) == 0, str(ck))

        # full scan path: cached 'tmdb' rows + an API key present -> lookup skipped
        old_cfg = cinema.CONFIG_PATH
        real_lookup = cinema.lookup_genre
        lookup_calls = []
        def counting_lookup(*a, **kw):
            lookup_calls.append(a)
            return None, None, "none"   # pretend network is down
        try:
            cfg_tmp = os.path.join(tmpdir, "cfg.json")
            with open(cfg_tmp, "w", encoding="utf-8") as f:
                json.dump({"tmdbKey": "FAKE"}, f)
            cinema.CONFIG_PATH = cfg_tmp
            cinema.lookup_genre = counting_lookup
            # first scan: nothing cached -> lookup attempted, caches 'none'
            cinema.SCAN_CANCEL.clear()
            cinema.run_scan(vdir, 0, False)
            n_first = len(lookup_calls)
            check("uncached titles go to lookup when key present", n_first >= 2,
                  str(n_first))
            # 'none' cache rows are re-tried while a key is present
            lookup_calls.clear()
            cinema.SCAN_CANCEL.clear()
            cinema.run_scan(vdir, 0, False)
            check("'none' cache rows re-tried with key", len(lookup_calls) >= 2,
                  str(len(lookup_calls)))
            # seed 'tmdb' cache rows -> lookup must be skipped entirely
            cinema.genre_cache_put("movie", "Big Movie", 2020, "Drama", "General", "tmdb")
            cinema.genre_cache_put("movie", "Alone Movie", 2021, "Comedy", "General", "tmdb")
            lookup_calls.clear()
            cinema.SCAN_CANCEL.clear()
            cinema.run_scan(vdir, 0, False)
            check("warm tmdb cache -> zero lookup calls", len(lookup_calls) == 0,
                  str(len(lookup_calls)))
            recs = {r["name"]: r for r in cinema.STATE["recs"]}
            check("genre comes from cache",
                  recs["Big.Movie.2020.1080p.mkv"]["genre"] == "Drama"
                  and recs["Alone.Movie.2021.1080p.mkv"]["genre"] == "Comedy",
                  str(recs["Big.Movie.2020.1080p.mkv"]["genre"]))
        finally:
            cinema.CONFIG_PATH = old_cfg
            cinema.lookup_genre = real_lookup
    finally:
        cinema.CINEMA_DB = old_db
        cinema.SAMPLE_SIZE = old_size
        cinema.CONFIG_PATH = old_cfg_outer
        cinema.STATE["recs"] = []
        cinema.STATE["groups"] = {}
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n== re-identify yearless library (mocked TMDB, no network) ==")
    tmpdir2 = tempfile.mkdtemp(prefix="cine_ident_")
    old_db2, old_cfg2 = cinema.CINEMA_DB, cinema.CONFIG_PATH
    old_fetch = cinema.tmdb_fetch
    try:
        cinema.CINEMA_DB = os.path.join(tmpdir2, "ident.db")
        cfg2 = os.path.join(tmpdir2, "cfg.json")
        with open(cfg2, "w", encoding="utf-8") as f:
            json.dump({"tmdbKey": "FAKE"}, f)
        cinema.CONFIG_PATH = cfg2
        cinema.db_init()
        lib = os.path.join(tmpdir2, "lib")
        os.makedirs(lib)
        NAUS = ("(1984) Nausicaä of the Valley of the Wind~Kaze no Tani no"
                " Nausicaä (720p Blu-ray 8bit Dual Audio) [NoobSubs]"
                " [86760B55].mkv")
        NAMES = ["1408 (720p).mp4", "2081 (720p).avi",
                 "22 Jump Street (720p).mkv", "13 Assassins.avi", "50-50.avi",
                 NAUS, "2001 - A Space Odyssey.avi", "Annie Hall_hd.mkv",
                 "random_home_video.mkv", "d-mptmol-720p.mkv"]
        for n in NAMES:
            with open(os.path.join(lib, n), "wb") as f:
                f.write(b"X" * 100)

        GENRES = [{"id": 28, "name": "Action"}, {"id": 35, "name": "Comedy"},
                  {"id": 18, "name": "Drama"}, {"id": 27, "name": "Horror"},
                  {"id": 878, "name": "Science Fiction"},
                  {"id": 16, "name": "Animation"},
                  {"id": 9648, "name": "Mystery"}]
        HITS = {
            "1408": [("1408", "2007-06-22", [27, 9648])],
            "2081": [("2081", "2009-05-29", [878])],
            "22 jump street": [("22 Jump Street", "2014-06-13", [28, 35])],
            "13 assassins": [("13 Assassins", "2010-09-25", [28, 18])],
            "50-50": [("50/50", "2011-09-30", [35, 18])],
            "nausicaa of the valley of the wind":
                [("Nausicaä of the Valley of the Wind", "1984-03-11", [16])],
            "a space odyssey": [],     # year-filtered lookup finds nothing
            "2001 a space odyssey":
                [("2001: A Space Odyssey", "1968-04-02", [878])],
            "annie hall hd": [],       # trailing junk poisons the query
            "annie hall": [("Annie Hall", "1977-04-20", [35, 18])],
            "random home video":
                [("Some Random Documentary", "2001-01-01", [28])],
            "d-mptmol": [],
        }
        queries = []

        def fake_tmdb(url):
            import urllib.parse as up
            queries.append(url)
            if "/genre/movie/list" in url:
                return {"genres": GENRES}
            if "/search/movie" in url:
                q = up.parse_qs(up.urlparse(url).query)
                title = q.get("query", [""])[0]
                year = q.get("year", [None])[0]
                results = []
                for t, d, gids in HITS.get(title.lower(), []):
                    if year and abs(int(d[:4]) - int(year)) > 1:
                        continue
                    results.append({"title": t, "original_title": t,
                                    "release_date": d, "genre_ids": gids})
                return {"results": results}
            raise AssertionError("unexpected url " + url)

        cinema.tmdb_fetch = fake_tmdb
        cinema.SCAN_CANCEL.clear()
        cinema.run_scan(lib, 0, False)
        recs = {r["name"]: r for r in cinema.STATE["recs"]}

        def ident(name, title, year, genre):
            r = recs[name]
            ok = (r["kind"] == "movie" and r["title"] == title
                  and r["year"] == year and r["genre"] == genre
                  and r["genre_source"] == "tmdb")
            return ok

        check("1408 yearless -> 2007 Horror",
              ident("1408 (720p).mp4", "1408", 2007, "Horror"),
              str({k: recs["1408 (720p).mp4"].get(k)
                   for k in ("kind", "title", "year", "genre")}))
        check("2081 numeric title -> 2009",
              ident("2081 (720p).avi", "2081", 2009, "Science Fiction"),
              str(recs["2081 (720p).avi"].get("title")))
        check("22 Jump Street -> 2014",
              ident("22 Jump Street (720p).mkv", "22 Jump Street", 2014,
                    "Action"))
        check("13 Assassins -> 2010",
              ident("13 Assassins.avi", "13 Assassins", 2010, "Action"))
        check("50-50 -> 50/50 (2011)",
              ident("50-50.avi", "50/50", 2011, "Comedy"),
              str(recs["50-50.avi"].get("title")))
        check("fansub Nausicaä keeps display title + 1984",
              ident(NAUS, "Nausicaä of the Valley of the Wind", 1984,
                    "Animation"),
              str({k: recs[NAUS].get(k) for k in ("kind", "title", "year")}))
        check("2001 year-first falls back to full title + 1968",
              ident("2001 - A Space Odyssey.avi", "2001: A Space Odyssey",
                    1968, "Science Fiction"),
              str({k: recs["2001 - A Space Odyssey.avi"].get(k)
                   for k in ("kind", "title", "year")}))
        check("trailing-junk tail reduced: Annie Hall_hd -> 1977",
              ident("Annie Hall_hd.mkv", "Annie Hall", 1977, "Comedy"),
              str({k: recs["Annie Hall_hd.mkv"].get(k)
                   for k in ("kind", "title", "year")}))
        check("junk name stays unidentified",
              recs["random_home_video.mkv"]["kind"] == "unknown")
        check("scene name stays unidentified",
              recs["d-mptmol-720p.mkv"]["kind"] == "unknown")
        check("TMDB query was ASCII-folded",
              any("Nausicaa" in q for q in queries)
              and not any("%C3%A4" in q for q in queries))

        # ident/genre caches are terminal: a rescan makes zero TMDB calls
        n_q = len(queries)
        cinema.SCAN_CANCEL.clear()
        cinema.run_scan(lib, 0, False)
        check("rescan fully cached (zero TMDB calls)",
              len(queries) == n_q, f"{n_q} -> {len(queries)}")
        recs2 = {r["name"]: r for r in cinema.STATE["recs"]}
        check("identifications stable across rescan",
              recs2["1408 (720p).mp4"]["year"] == 2007
              and recs2[NAUS]["title"] == "Nausicaä of the Valley of the Wind"
              and recs2["2001 - A Space Odyssey.avi"]["year"] == 1968)
    finally:
        cinema.CINEMA_DB = old_db2
        cinema.CONFIG_PATH = old_cfg2
        cinema.tmdb_fetch = old_fetch
        cinema.STATE["recs"] = []
        cinema.STATE["groups"] = {}
        shutil.rmtree(tmpdir2, ignore_errors=True)

    print("\n== TV identification through scan (mocked TMDB, no network) ==")
    tmpdir3 = tempfile.mkdtemp(prefix="cine_tv_")
    old_db3, old_cfg3 = cinema.CINEMA_DB, cinema.CONFIG_PATH
    old_fetch3 = cinema.tmdb_fetch
    try:
        cinema.CINEMA_DB = os.path.join(tmpdir3, "tv.db")
        cfg3 = os.path.join(tmpdir3, "cfg.json")
        with open(cfg3, "w", encoding="utf-8") as f:
            json.dump({"tmdbKey": "FAKE"}, f)
        cinema.CONFIG_PATH = cfg3
        cinema.db_init()
        lib = os.path.join(tmpdir3, "lib")
        os.makedirs(lib)
        NAMES = [
            "[NoobSubs] Cowboy Bebop - 05 (1080p Blu-ray 8bit AAC) [CRC].mkv",
            "Cowboy Bebop - 06.mkv",
            "Pokémon SE1 EP024 - Haunter Versus Kadabra.avi",
            "Some.Show.S01E01E02.1080p.mkv",
            "S15E03 - The A-Team Special.mkv",   # no series -> unknown
            "random_home_video.mkv",             # junk -> unknown
        ]
        for n in NAMES:
            with open(os.path.join(lib, n), "wb") as f:
                f.write(b"X" * 100)

        TV_GENRES = [{"id": 16, "name": "Animation"},
                     {"id": 10765, "name": "Sci-Fi & Fantasy"},
                     {"id": 10759, "name": "Action & Adventure"}]
        TV_HITS = {
            "cowboy bebop": [("Cowboy Bebop", "1998-04-03", [16, 10765])],
            "pokemon": [("Pokémon", "1997-04-01", [16, 10759])],
            "some show": [("Some Show", "2020-01-01", [10765])],
        }
        queries3 = []

        def fake_tmdb3(url):
            import urllib.parse as up
            queries3.append(url)
            if "/genre/tv/list" in url:
                return {"genres": TV_GENRES}
            if "/search/tv" in url:
                q = up.parse_qs(up.urlparse(url).query)
                title = q.get("query", [""])[0]
                check("tv query has no year filter", "year" not in q, url)
                results = []
                for t, d, gids in TV_HITS.get(title.lower(), []):
                    results.append({"name": t, "original_name": t,
                                    "first_air_date": d, "genre_ids": gids})
                return {"results": results}
            raise AssertionError("unexpected url " + url)

        cinema.tmdb_fetch = fake_tmdb3
        cinema.SCAN_CANCEL.clear()
        cinema.run_scan(lib, 0, False)
        recs = {r["name"]: r for r in cinema.STATE["recs"]}

        cb = recs["[NoobSubs] Cowboy Bebop - 05 (1080p Blu-ray 8bit AAC) [CRC].mkv"]
        check("fansub bebop identified as tv",
              cb["kind"] == "tv" and cb["title"] == "Cowboy Bebop"
              and cb["season"] == 1 and cb["episode"] == 5,
              str({k: cb.get(k) for k in ("kind", "title", "season", "episode")}))
        check("bebop adopts tmdb series year + tv genres",
              cb["year"] == 1998 and cb["genre"] == "Animation"
              and cb["subgenre"] == "Sci-Fi & Fantasy"
              and cb["genre_source"] == "tmdb",
              str({k: cb.get(k) for k in ("year", "genre", "subgenre")}))
        pk = recs["Pokémon SE1 EP024 - Haunter Versus Kadabra.avi"]
        check("pokemon SE..EP.. identified as tv",
              pk["kind"] == "tv" and pk["title"] == "Pokémon"
              and pk["season"] == 1 and pk["episode"] == 24
              and pk["genre"] == "Animation",
              str({k: pk.get(k) for k in ("kind", "title", "season", "genre")}))
        check("series looked up once per title (cache shared)",
              sum(1 for q in queries3 if "Cowboy%20Bebop" in q) == 1,
          str([q for q in queries3 if "search" in q]))
        check("series-less episode file stays unknown",
              recs["S15E03 - The A-Team Special.mkv"]["kind"] == "unknown")
        check("junk name stays unknown (tv suite)",
              recs["random_home_video.mkv"]["kind"] == "unknown")

        # TV destinations: Genre\Sub-genre\Title\Season NN, never a year
        plan, err = cinema.compute_plan({"action": "move",
                                         "targetRoot": os.path.join(
                                             tmpdir3, "out")})
        check("plan built for tv suite", plan is not None, str(err))
        dests = {os.path.basename(e["from"]): e["to"]
                 for e in plan["entries"]}
        d = dests["[NoobSubs] Cowboy Bebop - 05 (1080p Blu-ray 8bit AAC) [CRC].mkv"]
        want = os.path.join(tmpdir3, "out", "Animation", "Sci-Fi & Fantasy",
                            "Cowboy Bebop", "Season 01",
                            "Cowboy Bebop - S01E05.mkv")
        check("bebop dest = Genre\\Sub-genre\\Title\\Season NN", d == want, d)
        check("bebop dest has no year folder", "1998" not in d, d)
        d = dests["Pokémon SE1 EP024 - Haunter Versus Kadabra.avi"]
        want = os.path.join(tmpdir3, "out", "Animation", "Action & Adventure",
                            "Pokémon", "Season 01", "Pokémon - S01E24.avi")
        check("pokemon dest = Genre\\Sub-genre\\Title\\Season NN",
              d == want, d)
        d = dests["Some.Show.S01E01E02.1080p.mkv"]
        check("multi-episode dest keeps S01E01E02",
              d.endswith(os.path.join("Some Show", "Season 01",
                                      "Some Show - S01E01E02.mkv")), d)
        tv_dests = [d for n, d in dests.items()
                    if recs[n]["kind"] == "tv"]
        check("no tv destination contains a year component",
              all(not re.search(r"(^|\\)(19|20)\d{2}(\\|$)", d)
                  for d in tv_dests), str(tv_dests))

        # rescan is fully cached for tv as well
        n_q3 = len(queries3)
        cinema.SCAN_CANCEL.clear()
        cinema.run_scan(lib, 0, False)
        check("tv rescan fully cached (zero TMDB calls)",
              len(queries3) == n_q3, f"{n_q3} -> {len(queries3)}")
    finally:
        cinema.CINEMA_DB = old_db3
        cinema.CONFIG_PATH = old_cfg3
        cinema.tmdb_fetch = old_fetch3
        cinema.STATE["recs"] = []
        cinema.STATE["groups"] = {}
        cinema.STATE["plan"] = None
        shutil.rmtree(tmpdir3, ignore_errors=True)


# ============================================================ HTTP tests

def start_server():
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"),
         "--port", str(PORT), "--no-browser"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def stop_server(server):
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def http_tests():
    import make_cinema_fixtures
    make_cinema_fixtures.main()

    # preserve any pre-existing cinema config
    saved_config = None
    if os.path.isfile(CONFIG):
        with open(CONFIG, "r", encoding="utf-8") as f:
            saved_config = f.read()

    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    # start from a clean cinema DB so "404 before scan" holds on re-runs
    for suffix in ("", "-wal", "-shm"):
        p = DB + suffix
        if os.path.exists(p):
            os.remove(p)

    server = start_server()
    try:
        assert wait_port(), "server did not start"

        print("\n== static ==")
        raw = urllib.request.urlopen(HOST + "/cinema.js", timeout=10).read()
        check("GET /cinema.js serves JS", b"Cinema Organizer" in raw)
        raw = urllib.request.urlopen(HOST + "/", timeout=10).read()
        check("index.html has cinema window", b"winCinema" in raw and b"Cinema Organizer 1.0" in raw)

        print("\n== cancel when idle -> 409 ==")
        code, _ = req("POST", "/api/cinema/scan/cancel", {}, expect_error=True)
        check("scan cancel idle 409", code == 409, str(code))
        code, _ = req("POST", "/api/cinema/execute/cancel", {}, expect_error=True)
        check("execute cancel idle 409", code == 409, str(code))

        print("\n== config endpoint ==")
        req("POST", "/api/cinema/config", {"tmdbKey": "", "tmdbToken": ""})
        _, cfg = req("GET", "/api/cinema/config")
        check("config hasKey False initially", cfg.get("hasKey") is False, str(cfg))
        _, cfg = req("POST", "/api/cinema/config", {"tmdbKey": "TESTKEY123"})
        check("config saved hasKey True", cfg.get("hasKey") is True, str(cfg))
        _, cfg = req("GET", "/api/cinema/config")
        check("api key masked", cfg.get("tmdbKeyMasked") == "TEST…Y123", str(cfg))
        check("raw key never exposed",
              "tmdbKey" not in cfg and "TESTKEY123" not in json.dumps(cfg),
              str(cfg))
        _, cfg = req("POST", "/api/cinema/config", {"tmdbToken": "TESTTOKEN98765"})
        check("token saved, key preserved",
              cfg.get("hasToken") is True
              and cfg.get("tmdbKeyMasked") == "TEST…Y123", str(cfg))
        check("token masked", cfg.get("tmdbTokenMasked") == "TEST…8765", str(cfg))
        req("POST", "/api/cinema/config", {"tmdbKey": ""})
        _, cfg = req("GET", "/api/cinema/config")
        check("key cleared, token keeps auth",
              cfg.get("hasKey") is True and cfg.get("hasApiKey") is False
              and cfg.get("hasToken") is True, str(cfg))
        req("POST", "/api/cinema/config", {"tmdbToken": ""})
        _, cfg = req("GET", "/api/cinema/config")
        check("config cleared", cfg.get("hasKey") is False, str(cfg))

        print("\n== results before scan -> 404 ==")
        code, _ = req("GET", "/api/cinema/results", expect_error=True)
        check("results 404 before scan", code == 404, str(code))

        print("\n== scan fixtures (hash ON) ==")
        code, r = req("POST", "/api/cinema/scan",
                      {"path": FIX, "hash": True}, expect_error=True)
        check("scan accepted", code == 200 and r.get("ok"), f"{code} {r}")
        s = poll("/api/cinema/scan/status")
        check("scan done", s["state"] == "done", f"{s['state']} {s.get('error')}")

        code, res = req("GET", "/api/cinema/results")
        check("results 200 after scan", code == 200)
        # 16 not 17: the Matrix .nfo stem-matches its video and now rides as
        # a companion (posters/nfo/subs live WITH the content) instead of
        # being indexed as a clutter row.
        check("16 rows indexed", res["totalFiles"] == 16, str(res["totalFiles"]))
        bk = res["byKind"]
        check("byKind movie=6 tv=5 unknown=2 clutter=3",
              bk.get("movie") == 6 and bk.get("tv") == 5
              and bk.get("unknown") == 2 and bk.get("clutter") == 3, str(bk))
        check("dupeGroups=2", res["dupeGroups"] == 2, str(res["dupeGroups"]))
        check("dupeFiles=3", res["dupeFiles"] == 3, str(res["dupeFiles"]))
        check("samples=1", res["samples"] == 1, str(res["samples"]))
        check("clutter=3", res["clutter"] == 3, str(res["clutter"]))
        check("unidentified=2", res["unidentified"] == 2, str(res["unidentified"]))
        check("hasTmdbKey False", res["hasTmdbKey"] is False)
        g = res["groups"]
        matrix_members = None
        ep_members = None
        for members in g.values():
            names = sorted(os.path.basename(m) for m in members)
            if any("Matrix" in n for n in names):
                matrix_members = names
            if any("S01E01" in n for n in names):
                ep_members = names
        check("matrix group has 3 members", matrix_members and len(matrix_members) == 3,
              str(matrix_members))
        check("S01E01 group has 2 members", ep_members and len(ep_members) == 2,
              str(ep_members))
        recs = {r["name"]: r for r in res["recs"]}
        check("setup.exe not indexed", "setup.exe" not in recs)
        check("readme.txt not indexed", "readme.txt" not in recs)
        check("sample parsed as movie with is_sample",
              recs["The.Matrix.1999.1080p.BluRay.x264-GROUP.sample.mkv"]["is_sample"] is True)
        check("season pack flagged",
              recs["Some.Show.S01.1080p.WEB-DL.mkv"]["season_pack"] is True)

        print("\n== plan ==")
        code, plan = req("POST", "/api/cinema/plan",
                         {"action": "move", "targetRoot": TARGET})
        check("plan 200", code == 200)
        st = plan["stats"]
        check("stats dupeFiles=3 sampleFiles=1 clutterFiles=3 unidentifiedFiles=2 companionFiles=3",
              st["dupeFiles"] == 3 and st["sampleFiles"] == 1 and st["clutterFiles"] == 3
              and st["unidentifiedFiles"] == 2 and st["companionFiles"] == 3, str(st))
        dests = {os.path.basename(e["from"]): e for e in plan["entries"]}
        e = dests["The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv"]
        want = os.path.join(TARGET, "Unclassified", "General", "1999",
                            "The Matrix (1999)", "The Matrix (1999).mkv")
        check("matrix best dest", e["to"] == want, e["to"])
        mdir = os.path.join(TARGET, "Unclassified", "General", "1999",
                            "The Matrix (1999)")
        ctos = sorted(c["to"] for c in e["companions"])
        check("matrix best has srt + nfo companions",
              ctos == sorted([os.path.join(mdir, "The Matrix (1999).srt"),
                              os.path.join(mdir, "The Matrix (1999).nfo")]),
              str(ctos))
        e = dests["Some.Show.S01E01.1080p.WEB-DL.mkv"]
        want = os.path.join(TARGET, "Unclassified", "General", "Some Show",
                            "Season 01", "Some Show - S01E01.mkv")
        check("S01E01 best dest", e["to"] == want, e["to"])
        check("S01E01 srt companion dest",
              len(e["companions"]) == 1
              and e["companions"][0]["to"] == want.replace(".mkv", ".srt"),
              str(e["companions"]))
        e = dests["Some.Show.S01.1080p.WEB-DL.mkv"]
        want = os.path.join(TARGET, "Unclassified", "General", "Some Show",
                            "Season 01", "Some.Show.S01.1080p.WEB-DL.mkv")
        check("season pack keeps original name", e["to"] == want, e["to"])
        check("sample -> _Samples",
              dests["The.Matrix.1999.1080p.BluRay.x264-GROUP.sample.mkv"]["to"]
              .startswith(os.path.join(TARGET, "_Samples")))
        # the stem-matched .nfo is a companion now (rides with the movie), so
        # it must NOT appear as its own plan entry
        check("nfo is a companion, not a plan row",
              "The.Matrix.1999.1080p.BluRay.x264-GROUP.nfo" not in dests)
        check("unknown -> _Unidentified",
              dests["random_home_video.mkv"]["to"]
              .startswith(os.path.join(TARGET, "_Unidentified")))
        check("dupe 720p matrix -> _Duplicates",
              dests["The.Matrix.1999.720p.WEB-DL.mkv"]["isDupe"] is True
              and "_Duplicates" in dests["The.Matrix.1999.720p.WEB-DL.mkv"]["to"])
        check("dupe exact-copy matrix -> _Duplicates",
              dests["The Matrix (1999) 1080p.mkv"]["isDupe"] is True)
        check("dupe HDTV S01E01 -> _Duplicates",
              dests["Some.Show.S01E01.720p.HDTV.mkv"]["isDupe"] is True)
        # best-of-group sanity: the 1080p BluRay is NOT the dupe
        check("BluRay 1080p kept as best (not dupe)",
              dests["The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv"]["isDupe"] is False)

        print("\n== execute (move) ==")
        req("POST", "/api/cinema/execute", {})
        s = poll("/api/cinema/execute/status")
        check("execute done", s["state"] == "done", f"{s['state']} {s.get('error')}")
        er = s["result"]
        check("moved = 16 plan rows", er["moved"] == 16, str(er))
        check("errors = 0", er["errors"] == 0, str(er))
        check("undo manifest exists", os.path.isfile(er["undoFile"]), er["undoFile"])
        with open(er["undoFile"], "r", encoding="utf-8") as f:
            man = json.load(f)
        check("manifest has 16 + 3 companions = 19 entries",
              len(man["entries"]) == 19, str(len(man["entries"])))
        check("manifest marks 3 companions (srt x2 + nfo)",
              sum(1 for e in man["entries"] if e.get("companion")) == 3)

        moved_to = os.path.join(TARGET, "Unclassified", "General", "1999",
                                "The Matrix (1999)", "The Matrix (1999).mkv")
        check("matrix best on disk", os.path.isfile(moved_to))
        check("matrix srt beside parent",
              os.path.isfile(moved_to.replace(".mkv", ".srt")))
        ep_to = os.path.join(TARGET, "Unclassified", "General", "Some Show",
                             "Season 01", "Some Show - S01E01.mkv")
        check("S01E01 on disk", os.path.isfile(ep_to))
        check("S01E01 srt beside parent", os.path.isfile(ep_to.replace(".mkv", ".srt")))
        check("season pack on disk with original name",
              os.path.isfile(os.path.join(TARGET, "Unclassified", "General",
                                          "Some Show", "Season 01",
                                          "Some.Show.S01.1080p.WEB-DL.mkv")))
        check("sample in _Samples", os.path.isfile(os.path.join(
            TARGET, "_Samples", "The.Matrix.1999.1080p.BluRay.x264-GROUP.sample.mkv")))
        check("nfo renamed beside the movie (not _Clutter)",
              os.path.isfile(moved_to.replace(".mkv", ".nfo")))
        check("unknown in _Unidentified",
              os.path.isfile(os.path.join(TARGET, "_Unidentified", "random_home_video.mkv")))
        check("setup.exe untouched in source",
              os.path.isfile(os.path.join(FIX, "setup.exe")))
        check("readme.txt untouched in source",
              os.path.isfile(os.path.join(FIX, "readme.txt")))
        n_left = len([f for f in os.listdir(FIX)
                      if os.path.isfile(os.path.join(FIX, f))])
        check("only setup.exe + readme.txt left in source", n_left == 2, str(n_left))

        print("\n== undo ==")
        code, ur = req("POST", "/api/cinema/undo", {"manifest": er["undoFile"]})
        check("undo 200", code == 200, str(ur))
        check("undo restored 19", ur["restored"] == 19, str(ur))
        check("undo errors 0", ur["errors"] == 0, str(ur))
        n_back = len([f for f in os.listdir(FIX)
                      if os.path.isfile(os.path.join(FIX, f))])
        check("all 21 fixture files restored", n_back == 21, str(n_back))
        check("target tree gone", not os.path.exists(TARGET))

        print("\n== restart: state survives ==")
        stop_server(server)
        server = start_server()
        assert wait_port(), "server did not restart"
        code, res = req("GET", "/api/cinema/results", expect_error=True)
        check("results 200 after restart", code == 200, str(code))
        check("16 rows after restart", res.get("totalFiles") == 16,
              str(res.get("totalFiles")))
        check("byKind survives restart",
              res["byKind"].get("movie") == 6 and res["byKind"].get("tv") == 5,
              str(res["byKind"]))

        print("\n== scan cancel mid-run ==")
        if os.path.isdir(CANCEL_DIR):
            shutil.rmtree(CANCEL_DIR)
        os.makedirs(CANCEL_DIR)
        src = os.path.join(FIX, "Inception (2010) 720p.mp4")
        for i in range(N_CANCEL):
            shutil.copyfile(src, os.path.join(CANCEL_DIR, f"copy_{i:04d}.mkv"))
        req("POST", "/api/cinema/scan", {"path": CANCEL_DIR, "hash": True})
        cancelled = None
        t0 = time.time()
        while time.time() - t0 < 120:
            _, s = req("GET", "/api/cinema/scan/status")
            if s["state"] == "running" and s["processed"] >= 3 and cancelled is None:
                req("POST", "/api/cinema/scan/cancel", {})
                cancelled = poll("/api/cinema/scan/status")
                break
            if s["state"] in ("done", "error", "cancelled"):
                cancelled = s
                break
            time.sleep(0.05)
        check("scan cancelled mid-run",
              cancelled and cancelled["state"] == "cancelled",
              str(cancelled and cancelled["state"]))
        check("0 < processed < N",
              cancelled and 0 < cancelled["processed"] < N_CANCEL,
              str(cancelled and cancelled["processed"]))
    finally:
        stop_server(server)
        shutil.rmtree(CANCEL_DIR, ignore_errors=True)
        shutil.rmtree(TARGET, ignore_errors=True)
        if saved_config is not None:
            with open(CONFIG, "w", encoding="utf-8") as f:
                f.write(saved_config)
        elif os.path.isfile(CONFIG):
            os.remove(CONFIG)


def main():
    rc = 0
    unit_tests()
    http_tests()
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
