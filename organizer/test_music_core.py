#!/usr/bin/env python3
r"""Tests for music.py - the Music Organizer core.

Fully hermetic: fake music_tags and music_remote modules are injected into
sys.modules BEFORE importing music, so no real tag parsing and no network
ever happen. Every test runs against temp dirs under _music_test_tmp\
inside the workspace; production DBs/configs are never touched and no
server is started (api_get/api_post are called in-process).

Covered: scan+cluster+identify with fake MusicBrainz data, VA/compilation
detection both directions, the 5 dedupe stages, best-copy ranking with
keep_reason, exact plan paths (album / VA / single / unidentified /
duplicates / multi-disc subfolder+merge), execute+undo byte-for-byte,
cancel mid-scan, restart restore_state, and api_get/api_post shapes.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.abspath(__file__))
TMP_ROOT = os.path.join(BASE, "_music_test_tmp")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


def _norm(s):
    return " ".join(str(s or "").lower().split())


# ----------------------------------------------------- fake music_tags ----
fake_tags = types.ModuleType("music_tags")

TAGS = {}        # basename -> partial tag dict
TECH = {}        # basename -> tech dict
SLEEP = [0.0]    # per-call sleep (cancel test)

TAG_KEYS = ("artist", "albumartist", "album", "title", "trackno",
            "tracktotal", "discno", "disctotal", "year", "genre",
            "compilation", "has_art")


def fake_read_tags(path):
    if SLEEP[0]:
        time.sleep(SLEEP[0])
    out = {k: None for k in TAG_KEYS}
    out["compilation"] = False
    out["has_art"] = False
    d = TAGS.get(os.path.basename(path))
    if d:
        out.update(d)
    return out


def fake_tech_info(path):
    d = TECH.get(os.path.basename(path))
    if d is None:
        return {"codec": "mp3", "duration_s": 180.0, "bitrate_kbps": 128,
                "vbr": False, "samplerate": 44100, "channels": 2}
    return dict(d)


def fake_payload_md5(path):
    """md5 of the file minus a fake ID3v2 first line / ID3v1 last line."""
    data = open(path, "rb").read()
    parts = data.split(b"\n")
    if parts and parts[0].startswith(b"ID3v2"):
        parts = parts[1:]
    if parts and parts[-1].startswith(b"ID3v1"):
        parts = parts[:-1]
    return hashlib.md5(b"\n".join(parts)).hexdigest()


fake_tags.read_tags = fake_read_tags
fake_tags.tech_info = fake_tech_info
fake_tags.payload_md5 = fake_payload_md5

# --------------------------------------------------- fake music_remote ----
fake_remote = types.ModuleType("music_remote")

MB_SEARCH = {}     # (norm artist, norm album) -> [summary dict]
MB_RELEASES = {}   # mbid -> detail dict
MB_REC = {}        # (norm artist, norm title) -> [recording dict]
LASTFM = {}        # (norm artist, norm title) -> correction dict
DISCOGS = {}       # norm album -> discogs dict (album-keyed fake)
CAA = {}           # mbid -> bytes
CALLS = []


def fake_mb_search_release(artist, album, fetcher=None):
    CALLS.append(("mb_search_release", artist, album))
    return list(MB_SEARCH.get((_norm(artist), _norm(album)), []))


def fake_mb_get_release(mbid, fetcher=None):
    CALLS.append(("mb_get_release", mbid))
    return MB_RELEASES.get(mbid)


def fake_mb_search_recording(artist, title, fetcher=None):
    CALLS.append(("mb_search_recording", artist, title))
    return list(MB_REC.get((_norm(artist), _norm(title)), []))


def fake_cover_art_front(mbid, fetcher=None):
    CALLS.append(("cover_art_front", mbid))
    return CAA.get(mbid)


def fake_acoustid_lookup(fingerprint, duration_s, api_key, fetcher=None):
    CALLS.append(("acoustid_lookup", fingerprint))
    return None


def fake_lastfm_correction(artist, title, api_key, fetcher=None):
    CALLS.append(("lastfm_correction", artist, title))
    return LASTFM.get((_norm(artist), _norm(title)))


def fake_discogs_search_release(artist, album, token, fetcher=None):
    CALLS.append(("discogs_search_release", artist, album))
    return DISCOGS.get(_norm(album))


_CACHE = {}


def fake_cache_get(key):
    return _CACHE.get(key)


def fake_cache_set(key, value):
    _CACHE[key] = value
    return True


fake_remote.mb_search_release = fake_mb_search_release
fake_remote.mb_get_release = fake_mb_get_release
fake_remote.mb_search_recording = fake_mb_search_recording
fake_remote.cover_art_front = fake_cover_art_front
fake_remote.acoustid_lookup = fake_acoustid_lookup
fake_remote.lastfm_correction = fake_lastfm_correction
fake_remote.discogs_search_release = fake_discogs_search_release
fake_remote.cache_get = fake_cache_get
fake_remote.cache_set = fake_cache_set

# inject fakes BEFORE importing music (the real music_remote.py exists in
# this folder; the fake must win, so no network can ever happen)
sys.modules["music_tags"] = fake_tags
sys.modules["music_remote"] = fake_remote
sys.path.insert(0, BASE)

import music  # noqa: E402


# ------------------------------------------------------------- helpers ----

def write_audio(root, relname, payload=b"AUDIO-DATA", variant=b""):
    """Write a fake audio file: ID3v2-ish first line + payload + ID3v1-ish
    last line (fake_payload_md5 strips both ends)."""
    path = os.path.join(root, relname)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = b"ID3v2:" + variant + b"\n" + payload + b"\nID3v1"
    with open(path, "wb") as f:
        f.write(data)
    return path


def write_file(root, relname, data=b"x"):
    path = os.path.join(root, relname)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def fresh(tmpdir, cfg=None):
    """Point music at a temp db/config/undo dir and reset all state."""
    music.MUSIC_DB = os.path.join(tmpdir, "music.db")
    music.CONFIG_PATH = os.path.join(tmpdir, "music_config.json")
    music.UNDO_DIR = os.path.join(tmpdir, "undo")
    music.FPCALC = ""           # force fingerprinting unavailable
    SLEEP[0] = 0.0
    if cfg is not None:
        with open(music.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    with music.LOCK:
        music.STATE["recs"] = []
        music.STATE["releases"] = {}
        music.STATE["groups"] = {}
        music.STATE["review"] = {}
        music.STATE["scannedRoot"] = None
        music.STATE["plan"] = None
        music.STATE["lastUndo"] = None
        music.STATE["partialScan"] = False
        music.STATE["scan"] = {"state": "idle", "total": 0, "processed": 0,
                               "currentFile": "", "phase": "", "error": None}
        music.STATE["execute"] = {"state": "idle", "total": 0,
                                  "processed": 0, "currentFile": "",
                                  "error": None, "log": [], "result": None}
    music.SCAN_CANCEL.clear()
    music.EXEC_CANCEL.clear()
    music.db_init()
    for d in (TAGS, TECH, MB_SEARCH, MB_RELEASES, MB_REC, LASTFM, DISCOGS,
              CAA, _CACHE):
        d.clear()
    CALLS.clear()


def scan(root, max_files=0, hash_enabled=True, fingerprint=False):
    music.SCAN_CANCEL.clear()
    music.run_scan(root, max_files, hash_enabled, fingerprint)
    return music.scan_status()


def recs_by_name():
    return {r["name"]: r for r in music.STATE["recs"]}


def snapshot(root):
    out = {}
    for dp, _dn, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def mkd():
    return tempfile.mkdtemp(dir=TMP_ROOT)


# ============================================================== unit ======

def unit_tests():
    print("\n== quality ladder ==")
    Q = music.quality_score
    check("flac lossless = 5000", Q("flac", 900, False) == 5000)
    check("wav lossless = 5000", Q("wav", 1411, False) == 5000)
    check("mp3 320 = 3100", Q("mp3", 320, False) == 3100)
    check("mp3 V0 (245k vbr) = 3000", Q("mp3", 245, True) == 3000)
    check("mp3 256 = 2700", Q("mp3", 256, False) == 2700)
    check("mp3 V2 (190k vbr) = 2600", Q("mp3", 190, True) == 2600)
    check("mp3 192 = 2200", Q("mp3", 192, False) == 2200)
    check("mp3 160 = 1800", Q("mp3", 160, False) == 1800)
    check("mp3 128 = 1400", Q("mp3", 128, False) == 1400)
    check("ladder order 320 > V0 > 256 > V2",
          Q("mp3", 320, False) > Q("mp3", 245, True)
          > Q("mp3", 256, False) > Q("mp3", 190, True))

    print("\n== feat. normalization (Picard-style) ==")
    check("strip_feat paren", music.strip_feat("Song (feat. X)") == "Song")
    check("strip_feat bare", music.strip_feat("Song feat. X") == "Song")
    check("strip_feat ft.", music.strip_feat("Song ft. X") == "Song")
    check("main_artist", music.main_artist("A feat. B") == "A")
    check("main_artist featuring", music.main_artist("A featuring B") == "A")
    check("normalize folds + lowers",
          music.normalize("Beyoncé  Knowles!") == "beyonce knowles")

    print("\n== VA heuristic ==")
    def m(artist):
        return {"artist": artist}
    check("5 tracks 5 artists -> VA",
          music.va_heuristic([m("A"), m("B"), m("C"), m("D"), m("E")]))
    check("4 tracks 3 artists, none >50% -> VA",
          music.va_heuristic([m("A"), m("A"), m("B"), m("C")]))
    check("4 tracks, one artist on 3 (>50%) -> not VA",
          not music.va_heuristic([m("A"), m("A"), m("A"), m("B")]))
    check("feat.-heavy single artist -> not VA",
          not music.va_heuristic([m("A feat. B"), m("A feat. C"),
                                  m("A feat. D"), m("A")]))
    check("3 tracks 3 artists -> not VA (<4 tracks)",
          not music.va_heuristic([m("A"), m("B"), m("C")]))

    print("\n== sanitize / collision ==")
    check("bad chars stripped",
          music.sanitize_component('AC/DC: "Live" <*?>') == "AC DC Live")
    check("empty -> Unknown", music.sanitize_component("") == "Unknown")
    tmp = mkd()
    p = write_file(tmp, "a.mp3", b"1")
    check("collision suffix", music.resolve_collision(p, p + "x")
          == os.path.join(tmp, "a-2.mp3"))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P1 ========

def p1_scan_identify():
    print("\n== P1: scan + cluster + identify (fake MusicBrainz) ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    names = ["01 - Song One.mp3", "02 - Song Two.mp3",
             "03 - Song Three.flac"]
    for i, n in enumerate(names, 1):
        write_audio(root, os.path.join("AlbumDir", n),
                    payload=b"SONG-%d" % i)
        TAGS[n] = {"artist": "ArtistA", "albumartist": "ArtistA",
                   "album": "AlbumA", "title": n[4:-4], "trackno": i,
                   "tracktotal": 3, "year": 2001}
    TECH[names[0]] = {"codec": "mp3", "duration_s": 200.0,
                      "bitrate_kbps": 320, "vbr": False,
                      "samplerate": 44100, "channels": 2}
    TECH[names[1]] = {"codec": "mp3", "duration_s": 201.0,
                      "bitrate_kbps": 128, "vbr": False,
                      "samplerate": 44100, "channels": 2}
    TECH[names[2]] = {"codec": "flac", "duration_s": 202.0,
                      "bitrate_kbps": 900, "vbr": False,
                      "samplerate": 44100, "channels": 2}
    MB_SEARCH[("artista", "albuma")] = [{
        "mbid": "rel-1", "title": "AlbumA", "artist": "ArtistA",
        "score": 95, "date": "2001-05-01", "secondary_types": [],
        "release_group_mbid": "rg-1"}]
    MB_RELEASES["rel-1"] = {
        "mbid": "rel-1", "title": "AlbumA", "artist": "ArtistA",
        "is_va": False, "date": "2001-05-01", "label": "LabelX",
        "catno": "LX-001", "release_group_mbid": "rg-1",
        "primary_type": "Album", "secondary_types": [],
        "tracks": [{"disc": 1, "track": i, "title": n[4:-4],
                    "artist": "ArtistA", "is_va": False,
                    "duration_s": 200.0 + i, "recording_mbid": f"rec-{i}"}
                   for i, n in enumerate(names, 1)]}

    st = scan(root)
    check("scan done", st["state"] == "done", st.get("error") or "")
    recs = recs_by_name()
    check("3 tracks scanned", len(recs) == 3, str(len(recs)))
    cids = {r["cluster_id"] for r in recs.values()}
    check("all 3 in one cluster", cids == {"C001"}, str(cids))
    rel = music.STATE["releases"].get("C001")
    check("release identified via musicbrainz",
          rel and rel["source"] == "musicbrainz", str(rel and rel["source"]))
    check("release label/catno/year",
          rel and rel["label"] == "LabelX" and rel["catno"] == "LX-001"
          and rel["year"] == 2001, str(rel))
    check("release group id kept",
          rel and rel["mb_release_group_id"] == "rg-1")
    check("not a compilation", rel and rel["is_compilation"] is False)
    check("per-track recording mbids from MB tracks",
          recs[names[0]]["mb_recording_id"] == "rec-1"
          and recs[names[2]]["mb_recording_id"] == "rec-3",
          str(recs[names[0]]["mb_recording_id"]))
    check("genre falls back to Unclassified without keys",
          recs[names[0]]["genre"] == "Unclassified"
          and recs[names[0]]["subgenre"] == "General",
          str(recs[names[0]]["genre"]))
    check("mb release id on tracks",
          recs[names[0]]["mb_release_id"] == "rel-1")
    check("no genre calls without discogs token",
          not any(c[0] == "discogs_search_release" for c in CALLS))

    # rescan with a Discogs token -> genre enrichment
    with open(music.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"discogsToken": "DTOK"}, f)
    DISCOGS["albuma"] = {"id": 1, "title": "AlbumA", "year": 2001,
                         "genres": ["Rock"], "styles": ["Indie"],
                         "labels": ["LabelX"], "catno": "LX-001"}
    st = scan(root)
    recs = recs_by_name()
    check("discogs genre enrichment: Rock/Indie",
          recs[names[0]]["genre"] == "Rock"
          and recs[names[0]]["subgenre"] == "Indie",
          str(recs[names[0]]["genre"]))
    rel = music.STATE["releases"].get("C001")
    check("release carries genre too",
          rel and rel["genre"] == "Rock" and rel["subgenre"] == "Indie")
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P2 ========

def p2_va_detection():
    print("\n== P2: VA/compilation detection, both directions ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")

    # (a) heuristic VA: 5 tracks, 5 distinct artists
    va_artists = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    for i, a in enumerate(va_artists, 1):
        n = f"{i:02d} - {a} - Sun {a}.mp3"
        write_audio(root, os.path.join("va", n), payload=b"VA-%d" % i)
        TAGS[n] = {"artist": a, "album": "Summer", "title": f"Sun {a}",
                   "trackno": i, "year": 2021}

    # (b) feat.-heavy single-artist album -> NOT VA
    feats = ["MainA feat. Bee", "MainA feat. Cee", "MainA feat. Dee",
             "MainA"]
    for i, a in enumerate(feats, 1):
        n = f"{i:02d} - Feat Song {i}.mp3"
        write_audio(root, os.path.join("feat", n), payload=b"FE-%d" % i)
        TAGS[n] = {"artist": a, "albumartist": "MainA", "album": "Feat Album",
                   "title": f"Feat Song {i} (feat. Guest)", "trackno": i}

    # (c) TCMP/cpil tag on one track -> compilation
    for i in range(1, 4):
        n = f"{i:02d} - Tcmp Song {i}.mp3"
        write_audio(root, os.path.join("tcmp", n), payload=b"TC-%d" % i)
        TAGS[n] = {"artist": "SoloA", "albumartist": "SoloA",
                   "album": "Tcmp Album", "title": f"Tcmp Song {i}",
                   "trackno": i, "compilation": i == 2}

    # (d) literal 'Various Artists' albumartist -> compilation
    for i in range(1, 4):
        n = f"{i:02d} - Lit Song {i}.mp3"
        write_audio(root, os.path.join("lit", n), payload=b"LI-%d" % i)
        TAGS[n] = {"artist": f"Lit Artist {i}", "albumartist":
                   "Various Artists", "album": "Lit Album",
                   "title": f"Lit Song {i}", "trackno": i}

    # (e) only 3 tracks with 3 artists -> heuristic must NOT fire
    for i, a in enumerate(["X1", "Y2", "Z3"], 1):
        n = f"{i:02d} - Few Song {i}.mp3"
        write_audio(root, os.path.join("few", n), payload=b"FW-%d" % i)
        TAGS[n] = {"artist": a, "album": "Few Album",
                   "title": f"Few Song {i}", "trackno": i}

    st = scan(root)
    check("scan done", st["state"] == "done", st.get("error") or "")

    def comp_of(d):
        rs = [r for r in music.STATE["recs"]
              if os.path.basename(os.path.dirname(r["path"])) == d]
        return {r["compilation"] for r in rs}, len(rs)

    c, n = comp_of("va")
    check("heuristic VA album flagged compilation (all tracks)",
          c == {True} and n == 5, f"{c} n={n}")
    c, n = comp_of("feat")
    check("feat.-heavy single-artist album NOT compilation",
          c == {False} and n == 4, f"{c} n={n}")
    c, n = comp_of("tcmp")
    check("TCMP tag flags whole cluster compilation", c == {True} and n == 3,
          f"{c} n={n}")
    c, n = comp_of("lit")
    check("literal Various Artists albumartist flagged", c == {True},
          str(c))
    c, n = comp_of("few")
    check("3 tracks/3 artists NOT compilation (<4 tracks)",
          c == {False} and n == 3, f"{c} n={n}")
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P3 ========

def p3_dedupe_stages():
    print("\n== P3: 5-stage dedupe outcomes ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")

    # stage 1: identical bytes across folders -> full-file md5
    write_audio(root, os.path.join("d1", "s1a.mp3"), payload=b"S1-PAYLOAD",
                variant=b"A")
    write_audio(root, os.path.join("d2", "s1b.mp3"), payload=b"S1-PAYLOAD",
                variant=b"A")
    for n in ("s1a.mp3", "s1b.mp3"):
        TAGS[n] = {"artist": "S1 Artist", "album": "S1 Album",
                   "title": "S1 Song", "trackno": 1}

    # stage 2: different ID3 wrapper, same payload -> payload md5
    write_audio(root, os.path.join("d3", "s2a.mp3"), payload=b"S2-PAYLOAD",
                variant=b"VERSION-A")
    write_audio(root, os.path.join("d4", "s2b.mp3"), payload=b"S2-PAYLOAD",
                variant=b"VERSION-B")
    for n in ("s2a.mp3", "s2b.mp3"):
        TAGS[n] = {"artist": "S2 Artist", "album": "S2 Album",
                   "title": "S2 Song", "trackno": 1}

    # stage 4: different bytes+payload, same tag identity, dur within 2s
    write_audio(root, os.path.join("d5", "s4a.mp3"), payload=b"S4-PAY-A",
                variant=b"A")
    write_audio(root, os.path.join("d6", "s4b.mp3"), payload=b"S4-PAY-B",
                variant=b"B")
    for n in ("s4a.mp3", "s4b.mp3"):
        TAGS[n] = {"artist": "S4 Artist", "albumartist": "S4 Artist",
                   "album": "S4 Album", "title": "S4 Song", "trackno": 3,
                   "discno": 1}
    TECH["s4a.mp3"] = {"codec": "mp3", "duration_s": 200.0,
                       "bitrate_kbps": 320, "vbr": False, "samplerate": 44100,
                       "channels": 2}
    TECH["s4b.mp3"] = {"codec": "mp3", "duration_s": 201.5,
                       "bitrate_kbps": 128, "vbr": False, "samplerate": 44100,
                       "channels": 2}

    # stage 5: same fuzzy artist+title but DIFFERENT albums -> review only
    write_audio(root, os.path.join("d7", "s5a.mp3"), payload=b"S5-PAY-A",
                variant=b"A")
    write_audio(root, os.path.join("d8", "s5b.mp3"), payload=b"S5-PAY-B",
                variant=b"B")
    TAGS["s5a.mp3"] = {"artist": "Fuzz Artist", "album": "Alb A",
                       "title": "Fuzz Song", "trackno": 1}
    TAGS["s5b.mp3"] = {"artist": "Fuzz Artist", "album": "Alb B",
                       "title": "Fuzz Song (feat. Someone)", "trackno": 9}
    TECH["s5a.mp3"] = {"codec": "mp3", "duration_s": 200.0,
                       "bitrate_kbps": 192, "vbr": False, "samplerate": 44100,
                       "channels": 2}
    TECH["s5b.mp3"] = {"codec": "mp3", "duration_s": 201.0,
                       "bitrate_kbps": 192, "vbr": False, "samplerate": 44100,
                       "channels": 2}

    # duration too far apart -> NOT grouped even with identical tags
    write_audio(root, os.path.join("d9", "dur1.mp3"), payload=b"DU-PAY-A",
                variant=b"A")
    write_audio(root, os.path.join("d10", "dur2.mp3"), payload=b"DU-PAY-B",
                variant=b"B")
    for n in ("dur1.mp3", "dur2.mp3"):
        TAGS[n] = {"artist": "Dur Artist", "album": "Dur Album",
                   "title": "Dur Song", "trackno": 1}
    TECH["dur1.mp3"] = {"codec": "mp3", "duration_s": 200.0,
                        "bitrate_kbps": 192, "vbr": False,
                        "samplerate": 44100, "channels": 2}
    TECH["dur2.mp3"] = {"codec": "mp3", "duration_s": 210.0,
                        "bitrate_kbps": 192, "vbr": False,
                        "samplerate": 44100, "channels": 2}

    # a loner that must never group
    write_audio(root, os.path.join("d11", "solo.mp3"), payload=b"SOLO")
    TAGS["solo.mp3"] = {"artist": "Solo Artist", "album": "Solo Album",
                        "title": "Solo Song", "trackno": 1}

    st = scan(root, hash_enabled=True)
    check("scan done", st["state"] == "done", st.get("error") or "")
    groups = music.STATE["groups"]
    review = music.STATE["review"]

    def gid(name):
        return groups.get(os.path.join(root, "d" + name[0], name[1]))

    paths = {r["name"]: r["path"] for r in music.STATE["recs"]}
    g1a, g1b = groups.get(paths["s1a.mp3"]), groups.get(paths["s1b.mp3"])
    check("stage1: byte-identical pair grouped",
          g1a and g1a == g1b, f"{g1a}/{g1b}")
    g2a, g2b = groups.get(paths["s2a.mp3"]), groups.get(paths["s2b.mp3"])
    check("stage2: retagged payload pair grouped (md5 differs)",
          g2a and g2a == g2b, f"{g2a}/{g2b}")
    check("stage2: full md5s really differ",
          recs_by_name()["s2a.mp3"]["md5"]
          != recs_by_name()["s2b.mp3"]["md5"])
    g4a, g4b = groups.get(paths["s4a.mp3"]), groups.get(paths["s4b.mp3"])
    check("stage4: tag-identity pair grouped (dur +1.5s)",
          g4a and g4a == g4b, f"{g4a}/{g4b}")
    check("three distinct dupe groups",
          len({g1a, g2a, g4a}) == 3, f"{g1a},{g2a},{g4a}")
    check("all group ids look like Gxx",
          all(g.startswith("G") for g in (g1a, g2a, g4a)))
    check("stage5: fuzzy cross-album pair NOT auto-grouped",
          paths["s5a.mp3"] not in groups and paths["s5b.mp3"] not in groups)
    r5a, r5b = review.get(paths["s5a.mp3"]), review.get(paths["s5b.mp3"])
    check("stage5: fuzzy pair lands in review bucket",
          r5a and r5a == r5b and r5a.startswith("R"), f"{r5a}/{r5b}")
    check("duration-mismatched pair not grouped",
          paths["dur1.mp3"] not in groups and paths["dur2.mp3"] not in groups)
    check("duration-mismatched pair not in review either",
          paths["dur1.mp3"] not in review and paths["dur2.mp3"] not in review)
    check("loner not grouped", paths["solo.mp3"] not in groups)
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P4 ========

def p4_best_copy():
    print("\n== P4: best-copy ranking + keep_reason ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")

    # trio of the "same song": FLAC vs 320k vs 128k
    trio = [("bc.flac", "flac", 900, b"BC-FLAC"),
            ("bc320.mp3", "mp3", 320, b"BC-320"),
            ("bc128.mp3", "mp3", 128, b"BC-128")]
    for n, codec, br, payload in trio:
        write_audio(root, os.path.join("trio", n), payload=payload)
        TAGS[n] = {"artist": "BC Artist", "albumartist": "BC Artist",
                   "album": "BC Album", "title": "BC Song", "trackno": 1,
                   "discno": 1}
        TECH[n] = {"codec": codec, "duration_s": 200.0,
                   "bitrate_kbps": br, "vbr": False, "samplerate": 44100,
                   "channels": 2}

    # pair: 320k vs 128k
    for n, br, payload in (("h320.mp3", 320, b"H-320"),
                           ("h128.mp3", 128, b"H-128")):
        write_audio(root, os.path.join("pair", n), payload=payload)
        TAGS[n] = {"artist": "H Artist", "albumartist": "H Artist",
                   "album": "H Album", "title": "H Song", "trackno": 2}
        TECH[n] = {"codec": "mp3", "duration_s": 180.0,
                   "bitrate_kbps": br, "vbr": False, "samplerate": 44100,
                   "channels": 2}

    st = scan(root, hash_enabled=True)
    check("scan done", st["state"] == "done", st.get("error") or "")
    recs = recs_by_name()

    check("FLAC wins the trio",
          recs["bc.flac"]["keep"] == 1, recs["bc.flac"]["keep_reason"])
    check("FLAC keep_reason says lossless flac",
          "lossless flac" in recs["bc.flac"]["keep_reason"],
          recs["bc.flac"]["keep_reason"])
    check("FLAC keep_reason names the group",
          "G0" in recs["bc.flac"]["keep_reason"],
          recs["bc.flac"]["keep_reason"])
    check("320k loses to FLAC", recs["bc320.mp3"]["keep"] == 0)
    check("128k loses to FLAC", recs["bc128.mp3"]["keep"] == 0)
    check("trio forms one group",
          len({music.STATE["groups"][r["path"]] for r in
               (recs["bc.flac"], recs["bc320.mp3"], recs["bc128.mp3"])}) == 1)
    check("320k wins the pair",
          recs["h320.mp3"]["keep"] == 1
          and recs["h128.mp3"]["keep"] == 0)
    check("320k keep_reason mentions 320 kbps",
          "320 kbps" in recs["h320.mp3"]["keep_reason"],
          recs["h320.mp3"]["keep_reason"])
    check("loser keep_reason points at winner",
          "bc.flac" in recs["bc128.mp3"]["keep_reason"],
          recs["bc128.mp3"]["keep_reason"])
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P5 ========

def p5_plan_paths():
    print("\n== P5: exact plan paths ==")
    tmp = mkd()
    fresh(tmp, cfg={"discogsToken": "DTOK"})
    root = os.path.join(tmp, "lib")

    # -- identified single-artist album (genre Rock/Indie via Discogs)
    alb = ["01 - Song One.mp3", "02 - Song Two.mp3", "03 - Song Three.flac"]
    for i, n in enumerate(alb, 1):
        write_audio(root, os.path.join("alb", n), payload=b"A-%d" % i)
        TAGS[n] = {"artist": "ArtistA", "albumartist": "ArtistA",
                   "album": "AlbumA", "title": n[4:-4], "trackno": i,
                   "year": 2001}
        TECH[n] = {"codec": "flac" if n.endswith(".flac") else "mp3",
                   "duration_s": 200.0, "bitrate_kbps": 900, "vbr": False,
                   "samplerate": 44100, "channels": 2}
    DISCOGS["albuma"] = {"genres": ["Rock"], "styles": ["Indie"]}

    # -- VA compilation (heuristic), genre Rock/Dance
    va = [("Alpha", "Sun One"), ("Beta", "Sun Two"), ("Gamma", "Sun Three"),
          ("Delta", "Sun Four"), ("Epsilon", "Sun Five")]
    for i, (a, t) in enumerate(va, 1):
        n = f"{i:02d} - {a} - {t}.mp3"
        write_audio(root, os.path.join("va", n), payload=b"V-%d" % i)
        TAGS[n] = {"artist": a, "album": "Summer", "title": t,
                   "trackno": i, "year": 2021}
    DISCOGS["summer"] = {"genres": ["Rock", "Dance"], "styles": []}

    # -- loose single
    write_audio(root, "Loose Song.mp3", payload=b"LOOSE")
    TAGS["Loose Song.mp3"] = {"artist": "Solo Artist", "title": "Loose Song"}

    # -- unidentified garbage file
    write_audio(root, "track01.mp3", payload=b"GARBAGE")

    # -- exact dupe pair (loser quarantined)
    write_audio(root, os.path.join("dup1", "dup.mp3"), payload=b"DUP")
    write_audio(root, os.path.join("dup2", "dup.mp3"), payload=b"DUP")
    for d in ("dup1", "dup2"):
        TAGS["dup.mp3"] = {"artist": "Dup Artist", "album": "Dup Album",
                           "title": "Dup Song", "trackno": 1}
    # both files share one basename in TAGS; same tags -> same identity

    # -- 2-disc album (laid out as Disc 1\/Disc 2 subfolders, like real rips)
    md = [("Disc 1", "01 - M1.mp3", 1, 1), ("Disc 1", "02 - M2.mp3", 1, 2),
          ("Disc 2", "01 - N1.mp3", 2, 1), ("Disc 2", "02 - N2.mp3", 2, 2)]
    for sub, n, disc, tr in md:
        write_audio(root, os.path.join("multi", sub, n),
                    payload=b"M-%d-%d" % (disc, tr))
        TAGS[n] = {"artist": "BandM", "albumartist": "BandM",
                   "album": "Multi", "title": n[4:-4], "trackno": tr,
                   "discno": disc, "disctotal": 2, "year": 2018}
    DISCOGS["multi"] = {"genres": ["Rock"], "styles": ["Indie"]}
    DISCOGS["dup album"] = {"genres": ["Rock"], "styles": []}

    st = scan(root, hash_enabled=True)
    check("scan done", st["state"] == "done", st.get("error") or "")

    target = os.path.join(tmp, "out")
    plan, err = music.compute_plan({"action": "move", "targetRoot": target,
                                    "discStyle": "subfolder"})
    check("plan built", plan is not None and err is None, str(err))
    entries = {e["from"]: e for e in plan["entries"]}

    def dest(sub, n):
        return entries[os.path.join(root, sub, n)]["to"]

    want = os.path.join(target, "Rock", "Indie", "ArtistA",
                        "2001 - AlbumA", "03 - Song Three.flac")
    check("album dest exactly Genre\\Sub-genre\\Artist\\2001 - Album\\03 - Title.ext",
          dest("alb", "03 - Song Three.flac") == want,
          dest("alb", "03 - Song Three.flac"))
    want = os.path.join(target, "Rock", "Indie", "ArtistA",
                        "2001 - AlbumA", "01 - Song One.mp3")
    check("album track 01 dest", dest("alb", "01 - Song One.mp3") == want,
          dest("alb", "01 - Song One.mp3"))

    va_entry = entries[os.path.join(root, "va", "01 - Alpha - Sun One.mp3")]
    want = os.path.join(target, "Rock", "Dance", "Various Artists",
                        "2021 - Summer", "01 - Alpha - Sun One.mp3")
    check("VA dest exactly ...\\Various Artists\\2021 - Summer\\01 - Alpha - Sun One.mp3",
          va_entry["to"] == want, va_entry["to"])
    check("VA entry carries reason 'va'", va_entry["reason"] == "va",
          str(va_entry["reason"]))
    check("VA genre picks second genre as subgenre",
          "Dance" in va_entry["to"], va_entry["to"])

    e = entries[os.path.join(root, "Loose Song.mp3")]
    want = os.path.join(target, "Solo Artist", "_Singles", "Loose Song.mp3")
    check("single dest Artist\\_Singles\\Title.ext", e["to"] == want, e["to"])
    check("single kind", e["kind"] == "single", e["kind"])

    e = entries[os.path.join(root, "track01.mp3")]
    want = os.path.join(target, "_Unidentified", "track01.mp3")
    check("unidentified dest", e["to"] == want, e["to"])
    check("unidentified reason", e["reason"] == "unidentified")

    dupes = [e for f, e in entries.items() if os.path.basename(f) == "dup.mp3"]
    losers = [e for e in dupes if e["isDupe"]]
    check("exactly one dupe loser", len(losers) == 1, str(len(losers)))
    want = os.path.join(target, "_Duplicates", losers[0]["groupId"], "dup.mp3")
    check("dupe loser -> _Duplicates\\Gxx",
          losers[0]["to"] == want
          and losers[0]["groupId"].startswith("G"), losers[0]["to"])
    check("dupe reason 'dupe'", losers[0]["reason"] == "dupe")
    winners = [e for e in dupes if not e["isDupe"]]
    want = os.path.join(target, "Rock", "General", "Dup Artist",
                        "Dup Album", "01 - Dup Song.mp3")
    check("dupe winner organized normally",
          winners[0]["to"] == want, winners[0]["to"])

    check("multi-disc subfolder style: Disc 2 dir",
          dest(os.path.join("multi", "Disc 2"), "01 - N1.mp3")
          == os.path.join(target, "Rock", "Indie", "BandM", "2018 - Multi",
                          "Disc 2", "01 - N1.mp3"),
          dest(os.path.join("multi", "Disc 2"), "01 - N1.mp3"))
    check("multi-disc disc 1 subfolder",
          dest(os.path.join("multi", "Disc 1"), "02 - M2.mp3")
          == os.path.join(target, "Rock", "Indie", "BandM", "2018 - Multi",
                          "Disc 1", "02 - M2.mp3"))

    st = plan["stats"]
    check("stats fields present",
          all(k in st for k in ("totalFiles", "companionFiles",
                                "foldersToCreate", "dupeFiles",
                                "unidentifiedFiles", "targetRoot", "action",
                                "dupeHandling", "discStyle")), str(st))
    check("stats counts",
          st["totalFiles"] == len(plan["entries"])
          and st["dupeFiles"] == 1 and st["unidentifiedFiles"] == 1
          and st["singleFiles"] == 1 and st["vaFiles"] == 5,
          str(st))

    # -- merge disc style: 1NN prefix, no Disc subfolders
    plan2, err2 = music.compute_plan({"action": "move", "targetRoot": target,
                                      "discStyle": "merge"})
    check("merge-style plan built", plan2 is not None, str(err2))
    e2 = {e["from"]: e for e in plan2["entries"]}
    got = e2[os.path.join(root, "multi", "Disc 2", "01 - N1.mp3")]["to"]
    check("merge style gives 1NN prefix (disc 2 track 1 -> 201)",
          got == os.path.join(target, "Rock", "Indie", "BandM",
                              "2018 - Multi", "201 - N1.mp3"), got)
    got = e2[os.path.join(root, "multi", "Disc 1", "02 - M2.mp3")]["to"]
    check("merge style disc 1 track 2 -> 102",
          got == os.path.join(target, "Rock", "Indie", "BandM",
                              "2018 - Multi", "102 - M2.mp3"), got)

    # -- default target root + validation errors
    plan3, err3 = music.compute_plan({"action": "move"})
    check("default targetRoot is <scanned>\\Organized",
          plan3 and plan3["stats"]["targetRoot"]
          == os.path.join(root, "Organized"),
          str(plan3 and plan3["stats"]["targetRoot"]))
    p, e = music.compute_plan({"action": "bogus"})
    check("invalid action rejected", p is None and e, str(e))
    p, e = music.compute_plan({"action": "move", "dupeHandling": "bogus"})
    check("invalid dupeHandling rejected", p is None and e, str(e))
    p, e = music.compute_plan({"action": "move", "discStyle": "bogus"})
    check("invalid discStyle rejected", p is None and e, str(e))
    p, e = music.compute_plan({"action": "move", "targetRoot": root})
    check("target == scanned root rejected", p is None and e, str(e))

    # -- dupeHandling keep: losers stay in place (excluded from plan)
    plan4, err4 = music.compute_plan({"action": "move", "targetRoot": target,
                                      "dupeHandling": "keep"})
    check("dupeHandling=keep plan built", plan4 is not None, str(err4))
    losers4 = [e for e in plan4["entries"] if e["isDupe"]]
    check("keep mode: no dupe entries in plan",
          len(losers4) == 0 and plan4["stats"]["keptDupeFiles"] == 1,
          str(plan4["stats"]))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P6 ========

def p6_execute_undo():
    print("\n== P6: execute + undo restores byte-for-byte ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")

    # album with a stem-matched .lrc and an album-level folder.jpg
    for i, t in enumerate(["Song One", "Song Two"], 1):
        n = f"{i:02d} - {t}.mp3"
        write_audio(root, os.path.join("alb", n), payload=b"S-%d" % i)
        TAGS[n] = {"artist": "ArtistE", "albumartist": "ArtistE",
                   "album": "AlbumE", "title": t, "trackno": i, "year": 2005}
    write_file(root, os.path.join("alb", "01 - Song One.lrc"),
               b"[00:01.00]lyrics\n")
    write_file(root, os.path.join("alb", "folder.jpg"), b"\xff\xd8JPEG\xff\xd9")

    # a single, an unidentified file, an exact dupe pair
    write_audio(root, "Alone.mp3", payload=b"ALONE")
    TAGS["Alone.mp3"] = {"artist": "Solo E", "title": "Alone"}
    write_audio(root, "zzz.mp3", payload=b"ZZZ")
    write_audio(root, os.path.join("k1", "kk.mp3"), payload=b"KK")
    write_audio(root, os.path.join("k2", "kk.mp3"), payload=b"KK")
    TAGS["kk.mp3"] = {"artist": "K Artist", "album": "K Album",
                      "title": "K Song", "trackno": 1}

    before = snapshot(root)
    n_files = len(before)
    check("fixture tree has 8 files", n_files == 8, str(n_files))

    st = scan(root, hash_enabled=True)
    check("scan done", st["state"] == "done", st.get("error") or "")
    check("companions attached: lrc to track 1",
          any(c["from"].endswith(".lrc")
              for r in music.STATE["recs"] if r["name"] == "01 - Song One.mp3"
              for c in r["companions"]),
          str([r["companions"] for r in music.STATE["recs"]
               if r["name"] == "01 - Song One.mp3"]))
    check("companions attached: folder.jpg album-level (keepName)",
          any(c["from"].endswith("folder.jpg") and c.get("keepName")
              for r in music.STATE["recs"] for c in r["companions"]))

    target = os.path.join(tmp, "out")
    plan, err = music.compute_plan({"action": "move", "targetRoot": target})
    check("plan built", plan is not None, str(err))
    music.EXEC_CANCEL.clear()
    music.run_execute()
    es = music.execute_status()
    check("execute done", es["state"] == "done", es.get("error") or "")
    check("no execute errors", es["result"]["errors"] == 0,
          str(es["result"]))
    check("undo manifest written", os.path.isfile(es["result"]["undoFile"]),
          es["result"]["undoFile"])

    # spot-check destinations + byte identity
    moved_to = os.path.join(target, "Unclassified", "General", "ArtistE",
                            "2005 - AlbumE", "01 - Song One.mp3")
    check("album track landed with same bytes",
          os.path.isfile(moved_to)
          and open(moved_to, "rb").read() == before[os.path.join(
              "alb", "01 - Song One.mp3")])
    check("stem-matched .lrc rode along (renamed to track stem)",
          os.path.isfile(moved_to.replace(".mp3", ".lrc")))
    check("folder.jpg kept its name in the album folder",
          os.path.isfile(os.path.join(os.path.dirname(moved_to),
                                      "folder.jpg")))
    check("single landed", os.path.isfile(os.path.join(
        target, "Solo E", "_Singles", "Alone.mp3")))
    check("unidentified landed", os.path.isfile(os.path.join(
        target, "_Unidentified", "zzz.mp3")))
    check("dupe loser quarantined", any(
        os.path.isfile(os.path.join(target, "_Duplicates", g, "kk.mp3"))
        for g in ("G01", "G02", "G03")))
    check("source tree emptied of moved audio",
          not os.path.isfile(os.path.join(root, "alb",
                                          "01 - Song One.mp3")))

    # ---- undo ----
    res, err = music.run_undo(es["result"]["undoFile"])
    check("undo ran clean", err is None and res and res["errors"] == 0,
          f"{res} {err}")
    after = snapshot(root)
    check("tree restored byte-for-byte", after == before,
          f"{len(after)} vs {len(before)}")
    check("target tree removed after undo", not os.path.exists(target))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P7 ========

def p7_cancel():
    print("\n== P7: cancel mid-scan ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    N = 300
    for i in range(N):
        n = f"t{i:04d}.mp3"
        write_audio(root, n, payload=b"T-%d" % i)
        TAGS[n] = {"artist": "Bulk", "album": "Bulk Album",
                   "title": f"Track {i}", "trackno": i + 1}
    SLEEP[0] = 0.01
    ok, err = music.start_scan(root, 0, False)
    check("scan started", ok and err is None, str(err))
    cancelled = None
    t0 = time.time()
    while time.time() - t0 < 60:
        s = music.scan_status()
        if s["state"] == "running" and s["processed"] >= 3:
            check("cancel_scan returns True while running",
                  music.cancel_scan() is True)
            break
        time.sleep(0.02)
    t0 = time.time()
    while time.time() - t0 < 60:
        s = music.scan_status()
        if s["state"] in ("done", "error", "cancelled"):
            cancelled = s
            break
        time.sleep(0.05)
    SLEEP[0] = 0.0
    check("scan ended cancelled", cancelled
          and cancelled["state"] == "cancelled",
          str(cancelled and cancelled["state"]))
    check("0 < processed < N", cancelled
          and 0 < cancelled["processed"] < N,
          str(cancelled and cancelled["processed"]))
    check("partial results flagged", music.STATE["partialScan"] is True)
    check("cancel when idle returns False", music.cancel_scan() is False)
    # partial state still persisted + restorable
    n_recs = len(music.STATE["recs"])
    check("partial recs kept", 0 < n_recs < N, str(n_recs))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P8 ========

def p8_restart_restore():
    print("\n== P8: restart restore_state ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for i in range(1, 4):
        n = f"{i:02d} - R Song {i}.mp3"
        write_audio(root, os.path.join("alb", n), payload=b"R-%d" % i)
        TAGS[n] = {"artist": "RA", "albumartist": "RA", "album": "R Album",
                   "title": f"R Song {i}", "trackno": i, "year": 1999}
    write_audio(root, os.path.join("x1", "xx.mp3"), payload=b"XX")
    write_audio(root, os.path.join("x2", "xx.mp3"), payload=b"XX")
    TAGS["xx.mp3"] = {"artist": "RX", "album": "X Album", "title": "X Song",
                      "trackno": 1}
    st = scan(root, hash_enabled=True)
    check("scan done", st["state"] == "done", st.get("error") or "")
    res_before = music.build_results()
    groups_before = dict(music.STATE["groups"])
    rels_before = set(music.STATE["releases"])

    # simulate a process restart: wipe in-memory state, reload from db
    with music.LOCK:
        music.STATE["recs"] = []
        music.STATE["releases"] = {}
        music.STATE["groups"] = {}
        music.STATE["review"] = {}
        music.STATE["scannedRoot"] = None
        music.STATE["partialScan"] = False
        music.STATE["plan"] = None
        music.STATE["scan"] = {"state": "idle", "total": 0, "processed": 0,
                               "currentFile": "", "phase": "", "error": None}
    ok = music.restore_state()
    check("restore_state True", ok is True)
    res_after = music.build_results()
    check("totalFiles survives restart",
          res_after["totalFiles"] == res_before["totalFiles"],
          f"{res_after['totalFiles']} vs {res_before['totalFiles']}")
    check("byKind survives restart", res_after["byKind"] == res_before["byKind"],
          str(res_after["byKind"]))
    check("dupe groups survive restart",
          music.STATE["groups"] == groups_before)
    check("releases survive restart",
          set(music.STATE["releases"]) == rels_before)
    check("scannedRoot survives restart",
          music.STATE["scannedRoot"] == root)
    check("scan status shows done after restore",
          music.scan_status()["state"] == "done")
    check("ensure_state True after restore", music.ensure_state() is True)

    # a plan built purely from restored state must still work
    plan, err = music.compute_plan({"action": "move",
                                    "targetRoot": os.path.join(tmp, "out")})
    check("plan works from restored state", plan is not None, str(err))
    check("restored plan has dupe entry",
          any(e["isDupe"] for e in plan["entries"]))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P9 ========

def p9_api():
    print("\n== P9: api_get / api_post shapes ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for i in range(1, 3):
        n = f"{i:02d} - Api Song {i}.mp3"
        write_audio(root, os.path.join("alb", n), payload=b"API-%d" % i)
        TAGS[n] = {"artist": "API Artist", "album": "API Album",
                   "title": f"Api Song {i}", "trackno": i, "year": 2010}
    write_audio(root, "q_low.mp3", payload=b"Q-LOW")
    write_audio(root, os.path.join("q2", "q_high.mp3"), payload=b"Q-HIGH")
    for n in ("q_low.mp3", "q_high.mp3"):
        TAGS[n] = {"artist": "Q Artist", "album": "Q Album",
                   "title": "Q Song", "trackno": 1}
    TECH["q_low.mp3"] = {"codec": "mp3", "duration_s": 200.0,
                         "bitrate_kbps": 128, "vbr": False,
                         "samplerate": 44100, "channels": 2}
    TECH["q_high.mp3"] = {"codec": "mp3", "duration_s": 200.0,
                          "bitrate_kbps": 320, "vbr": False,
                          "samplerate": 44100, "channels": 2}

    code, obj = music.api_get("results", {})
    check("results 404 before scan", code == 404 and "error" in obj,
          f"{code}")
    code, obj = music.api_get("nope", {})
    check("unknown GET 404", code == 404)
    code, obj = music.api_post("scan", {})
    check("scan without root 400", code == 400, str(code))
    code, obj = music.api_post("scan", {"root": os.path.join(tmp, "nope")})
    check("scan with bad root 400", code == 400)
    code, obj = music.api_post("plan", {"action": "move"})
    check("plan before scan 400", code == 400 and "error" in obj, str(code))
    code, obj = music.api_post("execute", {})
    check("execute before plan 409", code == 409, str(code))

    print("\n== config endpoint ==")
    code, cfg = music.api_post("config", {"acoustidKey": "ACOUSTIDKEY12345",
                                          "discogsToken": "DTOKEN98765",
                                          "lastfmKey": "LFMKEY1111"})
    check("config saved 200", code == 200)
    check("acoustid masked", cfg.get("acoustidKeyMasked") == "ACOU…2345",
          str(cfg.get("acoustidKeyMasked")))
    check("raw keys never exposed",
          "ACOUSTIDKEY12345" not in json.dumps(cfg)
          and "DTOKEN98765" not in json.dumps(cfg), json.dumps(cfg))
    check("config flags", cfg.get("hasAcoustidKey") is True
          and cfg.get("hasDiscogsToken") is True
          and cfg.get("hasLastfmKey") is True, str(cfg))
    check("fingerprintAvailable False without fpcalc",
          cfg.get("fingerprintAvailable") is False
          and cfg.get("fpcalcPath") == "", str(cfg))
    code, cfg = music.api_get("config", {})
    check("config GET matches", cfg.get("lastfmKeyMasked") == "LFMK…1111",
          str(cfg.get("lastfmKeyMasked")))
    code, cfg = music.api_post("config", {"lastfmKey": ""})
    check("empty string clears one key, others kept",
          cfg.get("hasLastfmKey") is False
          and cfg.get("hasAcoustidKey") is True, str(cfg))
    code, cfg = music.api_post("config", {"acoustidKey": "",
                                          "discogsToken": ""})
    check("all keys cleared", cfg.get("hasAcoustidKey") is False
          and cfg.get("hasDiscogsToken") is False)

    print("\n== full flow over the api ==")
    code, obj = music.api_post("scan", {"root": root, "maxFiles": 0,
                                        "hashEnabled": True,
                                        "fingerprintEnabled": True})
    check("scan accepted", code == 200 and obj.get("ok"), f"{code} {obj}")
    t0 = time.time()
    while time.time() - t0 < 30:
        code, s = music.api_get("scan/status", {})
        if s["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    check("scan done via api", s["state"] == "done", s.get("error") or "")
    check("status shape has phase",
          {"state", "total", "processed", "currentFile", "phase", "error"}
          <= set(s), str(sorted(s)))
    code, s2 = music.api_get("/api/music/scan/status", {})
    check("prefixed path also works", code == 200
          and s2["state"] == "done")

    code, res = music.api_get("results", {})
    check("results 200", code == 200)
    need = {"scannedRoot", "partial", "totalFiles", "byCodec", "topGenres",
            "genreCoverage", "identified", "unidentified", "singles",
            "compilations", "dupeGroups", "dupeFiles", "upgradesAvailable",
            "dupeBytes", "dupesQuarantined", "quickCleanEligible",
            "recs", "groups", "review"}
    check("results shape", need <= set(res),
          str(sorted(need - set(res))))
    check("results counts: 4 files, 1 dupe group, 1 upgrade",
          res["totalFiles"] == 4 and res["dupeGroups"] == 1
          and res["upgradesAvailable"] == 1, str(res["totalFiles"]))
    check("byCodec counts mp3", res["byCodec"].get("mp3") == 4,
          str(res["byCodec"]))
    check("rec entries carry kind + dupe_group",
          all("kind" in r and "dupe_group" in r for r in res["recs"]))

    code, obj = music.api_post("scan/cancel", {})
    check("scan cancel idle 409", code == 409, str(code))
    code, obj = music.api_post("execute/cancel", {})
    check("execute cancel idle 409", code == 409, str(code))

    code, plan = music.api_post("plan", {"action": "move",
                                         "dupeHandling": "quarantine",
                                         "discStyle": "subfolder"})
    check("plan 200", code == 200, f"{code} {plan.get('error')}")
    check("plan default target <root>\\Organized",
          plan["stats"]["targetRoot"] == os.path.join(root, "Organized"),
          plan["stats"]["targetRoot"])
    check("plan stats shape",
          {"totalFiles", "companionFiles", "foldersToCreate", "dupeFiles",
           "unidentifiedFiles", "targetRoot", "action"}
          <= set(plan["stats"]))
    check("plan entries have from/to/kind/reason",
          all({"from", "to", "kind", "reason"} <= set(e)
              for e in plan["entries"]))
    code, plan2 = music.api_get("plan", {})
    check("plan GET returns same plan",
          code == 200 and len(plan2["entries"]) == len(plan["entries"]))

    code, obj = music.api_post("execute", {})
    check("execute accepted", code == 200 and obj.get("ok"))
    t0 = time.time()
    while time.time() - t0 < 30:
        code, xs = music.api_get("execute/status", {})
        if xs["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    check("execute done", xs["state"] == "done", xs.get("error") or "")
    check("execute status shape",
          {"state", "total", "processed", "currentFile", "log", "error",
           "result"} <= set(xs))
    check("moved 4 files, no errors",
          xs["result"]["moved"] == 4 and xs["result"]["errors"] == 0,
          str(xs["result"]))
    check("undo file exists", os.path.isfile(xs["result"]["undoFile"]))

    code, ur = music.api_post("undo", {"manifest": xs["result"]["undoFile"]})
    check("undo 200", code == 200 and ur["restored"] == 4
          and ur["errors"] == 0, f"{code} {ur}")
    check("files back in place",
          os.path.isfile(os.path.join(root, "alb", "01 - Api Song 1.mp3")))
    code, ur = music.api_post("undo", {"manifest": os.path.join(tmp, "no.json")})
    check("undo missing manifest 404", code == 404, str(code))
    code, obj = music.api_post("unknown", {})
    check("unknown POST 404", code == 404)
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== E2E =======
# Real end-to-end against the real music_fixtures\ tree with the REAL
# music_tags.py / music_remote.py (loaded under private names because the
# fakes above hold the sys.modules slots). Two legs:
#   offline: a dead fetcher proves graceful Unclassified behaviour with zero
#            network; scan -> results -> plan -> execute -> undo byte-for-byte
#   genre'd: fake MB/Discogs functions on the real remote module prove
#            genre-first plan paths + label/catno identification

def _load_real_module(name):
    import importlib.util
    path = os.path.join(BASE, name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_real", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def e2e_real_fixtures():
    print("\n== E2E: real fixtures + real music_tags (offline leg) ==")
    real_tags = _load_real_module("music_tags")
    real_remote = _load_real_module("music_remote")
    tmp = mkd()
    fresh(tmp)                      # temp db/config/undo + reset state
    old_tags, old_remote = music.music_tags, music.music_remote
    music.music_tags = real_tags    # swap the fakes out for the real parsers
    music.music_remote = real_remote
    real_remote.MUSIC_DB = music.MUSIC_DB   # remote cache table in the temp db
    real_remote._sleep = lambda s: None     # documented fake-clock test hook

    def dead_fetcher(url, headers=None, data=None):
        return 0, {}, b""

    real_remote._real_fetcher = dead_fetcher
    try:
        src = os.path.join(BASE, "music_fixtures")
        if not os.path.isdir(src):
            import make_music_fixtures
            make_music_fixtures.main()
        fix = os.path.join(tmp, "fixtures")
        shutil.copytree(src, fix)   # execute moves files: work on a copy
        before = snapshot(fix)
        check("fixture copy has 31 files", len(before) == 31,
              str(len(before)))

        st = scan(fix, hash_enabled=True, fingerprint=False)
        check("scan done (offline, real tags)", st["state"] == "done",
              st.get("error") or "")
        recs = recs_by_name()
        check("28 audio files indexed", len(recs) == 28, str(len(recs)))
        res = music.build_results()
        check("offline genre coverage is 0%",
              res["genreCoverage"] == 0, str(res["genreCoverage"]))
        check("byKind: 22 album + 6 unidentified, 0 singles",
              res["byKind"].get("album") == 22
              and res["byKind"].get("unidentified") == 6
              and res["byKind"].get("single", 0) == 0, str(res["byKind"]))

        paths = {r["name"]: r["path"] for r in music.STATE["recs"]}
        groups = music.STATE["groups"]

        def gid_of(name, nth=0):
            return groups.get([p for k, p in paths.items()
                               if k == name][nth])

        ex = sorted(p for k, p in paths.items()
                    if k in ("03 - Stellar Winds - Aurora.mp3",
                             "Aurora (copy).mp3"))
        check("exact-byte dupes group together (stage 1)",
              len(ex) == 2 and groups.get(ex[0])
              and groups.get(ex[0]) == groups.get(ex[1]),
              f"{groups.get(ex[0])}/{groups.get(ex[1])}")
        rt = sorted(p for k, p in paths.items() if "Ocean Drift" in k)
        check("retagged dupe groups via payload md5 (stage 2)",
              len(rt) == 2 and groups.get(rt[0])
              and groups.get(rt[0]) == groups.get(rt[1]),
              f"{groups.get(rt[0])}/{groups.get(rt[1])}")
        check("retagged full md5s differ on disk",
              recs[[k for k in recs if "retagged" in k][0]]["md5"]
              != recs[[k for k in recs if k.startswith("01 - Crystal")][0]]
              ["md5"])

        flac = recs["01 - Amber Vale - Golden Hour.flac"]
        mp3 = recs["01 - Amber Vale - Golden Hour.mp3"]
        check("FLAC + 128k MP3 same group (stage 4)",
              groups.get(flac["path"]) and
              groups.get(flac["path"]) == groups.get(mp3["path"]))
        check("FLAC beats 128k MP3 with lossless keep_reason",
              flac["keep"] == 1 and mp3["keep"] == 0
              and "lossless flac" in flac["keep_reason"],
              flac["keep_reason"])

        summer = [r for r in music.STATE["recs"]
                  if "Summer Vibes 2021" in r["path"]]
        check("12-track 5-artist compilation flagged VA",
              len(summer) == 12
              and all(r["compilation"] for r in summer), str(len(summer)))
        neon = [r for r in music.STATE["recs"] if "Neon Skyline" in r["path"]]
        check("Neon Skyline (2 feat. filenames) NOT VA",
              len(neon) == 4
              and not any(r["compilation"] for r in neon))
        wand = [r for r in music.STATE["recs"] if "The Wandering" in r["path"]]
        check("multi-disc album clusters as ONE album",
              len({r["cluster_id"] for r in wand}) == 1,
              str({r["cluster_id"] for r in wand}))
        check("multi-disc detected from TPOS tags",
              wand[0]["cluster_id"] in music.multidisc_clusters(
                  music.STATE["recs"]))

        # ---- plan (offline -> Unclassified) ----
        target = os.path.join(tmp, "out")
        plan, err = music.compute_plan({"action": "move",
                                        "targetRoot": target,
                                        "discStyle": "subfolder"})
        check("plan built", plan is not None, str(err))
        entries = {e["from"]: e for e in plan["entries"]}

        def E(name, nth=0):
            return entries[[p for k, p in paths.items() if k == name][nth]]

        e = E("01 - Crystal Waves - Ocean Drift.mp3")
        want = os.path.join(target, "Unclassified", "General",
                            "Crystal Waves", "2022 - Ocean Drift - Single",
                            "01 - Ocean Drift.mp3")
        check("plan path exactly Genre\\Sub\\Artist\\Year - Album\\NN - Title.ext",
              e["to"] == want, e["to"])
        check("retag loser quarantined with original name",
              E("Crystal Waves - Ocean Drift (retagged).mp3")["isDupe"]
              and E("Crystal Waves - Ocean Drift (retagged).mp3")["to"]
              .endswith(os.path.join(
                  "_Duplicates",
                  E("Crystal Waves - Ocean Drift (retagged).mp3")["groupId"],
                  "Crystal Waves - Ocean Drift (retagged).mp3")))
        e = E("01 - Amber Vale - Golden Hour.flac")
        want = os.path.join(target, "Unclassified", "General", "Amber Vale",
                            "2023 - Golden Hour EP", "01 - Golden Hour.flac")
        check("best-copy FLAC plan path", e["to"] == want, e["to"])
        e = E("01 - Artist Alpha - Sunny Days.mp3")
        want = os.path.join(target, "Unclassified", "General",
                            "Various Artists", "2021 - Summer Vibes 2021",
                            "01 - Artist Alpha - Sunny Days.mp3")
        check("VA plan path (Various Artists folder)", e["to"] == want,
              e["to"])
        check("VA entry reason is 'va'", e["reason"] == "va")
        comp_dests = {os.path.basename(c["to"]): c["to"]
                      for c in e["companions"]}
        check("stem-matched .lrc rides with its track",
              comp_dests.get("01 - Artist Alpha - Sunny Days.lrc")
              == os.path.join(os.path.dirname(want),
                              "01 - Artist Alpha - Sunny Days.lrc"),
              str(e["companions"]))
        check("album-level folder.jpg + .cue keep names beside album",
              comp_dests.get("folder.jpg") == os.path.join(
                  os.path.dirname(want), "folder.jpg")
              and comp_dests.get("Summer Vibes 2021.cue") == os.path.join(
                  os.path.dirname(want), "Summer Vibes 2021.cue"),
              str(e["companions"]))
        e = E("01 - The Wandering - Driftwood.mp3")
        want = os.path.join(target, "Unclassified", "General",
                            "The Wandering", "2018 - Across the Sea",
                            "Disc 2", "01 - Driftwood.mp3")
        check("multi-disc subfolder plan path", e["to"] == want, e["to"])
        e = E("track01.mp3")
        check("garbage file -> _Unidentified",
              e["to"] == os.path.join(target, "_Unidentified",
                                      "track01.mp3"))
        wav = E("01 - Midnight Drive.wav")
        check("tagless WAV -> _Unidentified",
              wav["to"].startswith(os.path.join(target, "_Unidentified")),
              wav["to"])
        st = plan["stats"]
        check("plan stats: 28 files, dupes quarantined, 3 unidentified "
              "groups of companions",
              st["totalFiles"] == 28 and st["dupeFiles"] >= 3
              and st["companionFiles"] == 3, str(st))

        # ---- execute + undo byte-for-byte ----
        music.EXEC_CANCEL.clear()
        music.run_execute()
        es = music.execute_status()
        check("execute done, zero errors", es["state"] == "done"
              and es["result"]["errors"] == 0, str(es.get("result")))
        got = os.path.join(target, "Unclassified", "General",
                           "Various Artists", "2021 - Summer Vibes 2021",
                           "01 - Artist Alpha - Sunny Days.mp3")
        check("VA track on disk with sidecars",
              os.path.isfile(got)
              and os.path.isfile(got.replace(".mp3", ".lrc"))
              and os.path.isfile(os.path.join(
                  os.path.dirname(got), "folder.jpg"))
              and os.path.isfile(os.path.join(
                  os.path.dirname(got), "Summer Vibes 2021.cue")))
        res, err = music.run_undo(es["result"]["undoFile"])
        check("undo clean", err is None and res["errors"] == 0,
              f"{res} {err}")
        check("fixtures restored byte-for-byte",
              snapshot(fix) == before)
        check("target tree removed", not os.path.exists(target))

        # ---- quick-clean fields for the 'First pass: duplicates' panel ----
        # Regression: build_results() must emit quickCleanEligible/dupeBytes/
        # dupesQuarantined -- the Music UI panel is gated on quickCleanEligible
        # and previously it was never set, so the panel never appeared.
        music.run_scan(fix, 0, True, False, True)   # skip_identify: local only
        qc = music.build_results()
        check("quick-clean: local-only scan is eligible",
              qc["quickCleanEligible"] is True and qc["dupeBytes"] > 0
              and qc["dupesQuarantined"] == 0,
              f"elig={qc.get('quickCleanEligible')} "
              f"bytes={qc.get('dupeBytes')} quar={qc.get('dupesQuarantined')}")
        with music.LOCK:
            for r in music.STATE["recs"]:
                if not r.get("keep", 1) \
                        and music.STATE["groups"].get(r["path"]):
                    r["quarantined"] = 1
                    break
        qc2 = music.build_results()
        check("quick-clean: hidden once a loser is quarantined",
              qc2["quickCleanEligible"] is False
              and qc2["dupesQuarantined"] >= 1,
              f"elig={qc2.get('quickCleanEligible')} "
              f"quar={qc2.get('dupesQuarantined')}")
    finally:
        music.music_tags = old_tags
        music.music_remote = old_remote
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------- genre'd leg --------
    print("\n== E2E: genre'd plan via fake MB/Discogs (real remote module) ==")
    tmp = mkd()
    fresh(tmp, cfg={"discogsToken": "DTOK"})
    music.music_tags = real_tags
    music.music_remote = real_remote
    real_remote.MUSIC_DB = music.MUSIC_DB
    real_remote._sleep = lambda s: None
    try:
        MB = {
            ("stellar winds", "northern lights"): (
                [{"mbid": "rel-nl", "title": "Northern Lights",
                  "artist": "Stellar Winds", "score": 98,
                  "secondary_types": [], "release_group_mbid": "rg-nl"}],
                {"mbid": "rel-nl", "title": "Northern Lights",
                 "artist": "Stellar Winds", "is_va": False,
                 "date": "2020-03-01", "label": "Nightsky",
                 "catno": "NS-042", "release_group_mbid": "rg-nl",
                 "primary_type": "Album", "secondary_types": [],
                 "tracks": []}),
            ("the wandering", "across the sea"): (
                [{"mbid": "rel-sea", "title": "Across the Sea",
                  "artist": "The Wandering", "score": 97,
                  "secondary_types": [], "release_group_mbid": "rg-sea"}],
                {"mbid": "rel-sea", "title": "Across the Sea",
                 "artist": "The Wandering", "is_va": False,
                 "date": "2018-06-01", "label": "Drift Records",
                 "catno": "DR-007", "release_group_mbid": "rg-sea",
                 "primary_type": "Album", "secondary_types": [],
                 "tracks": []}),
        }
        DG = {
            "northern lights": {"genres": ["Ambient"], "styles": ["Chillout"]},
            "summer vibes 2021": {"genres": ["Dance"], "styles": ["House"]},
            "across the sea": {"genres": ["Folk"], "styles": ["Acoustic"]},
            "golden hour ep": {"genres": ["Pop"], "styles": []},
            "ocean drift - single": {"genres": ["Electronic"],
                                     "styles": ["Chill"]},
            "chillhop essentials": {"genres": ["Electronic"], "styles": []},
        }
        real_remote.mb_search_release = lambda a, al, fetcher=None: \
            list(MB.get((_norm(a), _norm(al)), [[], None])[0])
        real_remote.mb_get_release = lambda mbid, fetcher=None: \
            next((d for hits, d in MB.values() if d["mbid"] == mbid), None)
        real_remote.discogs_search_release = lambda a, al, tok, fetcher=None: \
            DG.get(_norm(al))

        fix = os.path.join(tmp, "fixtures")
        shutil.copytree(os.path.join(BASE, "music_fixtures"), fix)
        st = scan(fix, hash_enabled=True, fingerprint=False)
        check("genre'd scan done", st["state"] == "done",
              st.get("error") or "")
        recs = recs_by_name()
        aurora = recs["03 - Stellar Winds - Aurora.mp3"]
        check("MB-identified genre on tracks",
              aurora["genre"] == "Ambient" and aurora["subgenre"] == "Chillout",
              str(aurora["genre"]))
        rel = next(r for r in music.STATE["releases"].values()
                   if r.get("album") == "Northern Lights")
        check("release row: label/catno/source musicbrainz",
              rel["label"] == "Nightsky" and rel["catno"] == "NS-042"
              and rel["source"] == "musicbrainz", str(rel))
        res = music.build_results()
        check("genre coverage > 0 with enrichment",
              res["genreCoverage"] > 0, str(res["genreCoverage"]))
        check("topGenres lists Ambient",
              any(g == "Ambient" for g, _ in res["topGenres"]),
              str(res["topGenres"]))

        target = os.path.join(tmp, "out")
        plan, err = music.compute_plan({"action": "move",
                                        "targetRoot": target})
        check("genre'd plan built", plan is not None, str(err))
        paths = {r["name"]: r["path"] for r in music.STATE["recs"]}
        entries = {e["from"]: e for e in plan["entries"]}

        def E2(name, nth=0):
            return entries[[p for k, p in paths.items() if k == name][nth]]

        want = os.path.join(target, "Ambient", "Chillout", "Stellar Winds",
                            "2020 - Northern Lights", "03 - Aurora.mp3")
        check("genre'd album path", E2("Aurora (copy).mp3")["to"] == want
              or E2("03 - Stellar Winds - Aurora.mp3")["to"] == want,
              E2("03 - Stellar Winds - Aurora.mp3")["to"])
        e = E2("01 - Artist Alpha - Sunny Days.mp3")
        want = os.path.join(target, "Dance", "House", "Various Artists",
                            "2021 - Summer Vibes 2021",
                            "01 - Artist Alpha - Sunny Days.mp3")
        check("genre'd VA path", e["to"] == want, e["to"])
        e = E2("02 - The Wandering - Homeward.mp3")
        want = os.path.join(target, "Folk", "Acoustic", "The Wandering",
                            "2018 - Across the Sea", "Disc 2",
                            "02 - Homeward.mp3")
        check("genre'd multi-disc path", e["to"] == want, e["to"])
        plan2, _ = music.compute_plan({"action": "move",
                                       "targetRoot": target,
                                       "discStyle": "merge"})
        e = {x["from"]: x for x in plan2["entries"]}[
            paths["02 - The Wandering - Homeward.mp3"]]
        check("merge style on real fixtures -> 202 prefix",
              e["to"] == os.path.join(target, "Folk", "Acoustic",
                                      "The Wandering", "2018 - Across the Sea",
                                      "202 - Homeward.mp3"), e["to"])
    finally:
        music.music_tags = old_tags
        music.music_remote = old_remote
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P10 =======

def p10_phase_progress():
    print("\n== P10: phase-scoped progress counters + ETA ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for a in range(1, 5):
        for t in range(1, 3):
            n = f"0{t} - A{a} Song {t}.mp3"
            write_audio(root, os.path.join(f"alb{a}", n),
                        payload=b"P10-%d-%d" % (a, t))
            TAGS[n] = {"artist": f"Artist{a}", "albumartist": f"Artist{a}",
                       "album": f"Album {a}", "title": f"A{a} Song {t}",
                       "trackno": t, "year": 2000 + a}
    SLEEP[0] = 0.01            # slow the file phase so polls can observe it
    orig_search = fake_remote.mb_search_release
    orig_get = fake_remote.mb_get_release

    def slow_search(artist, album, fetcher=None):
        time.sleep(0.05)       # ~1 req/s MusicBrainz, in miniature
        return orig_search(artist, album)

    def slow_get(mbid, fetcher=None):
        time.sleep(0.05)
        return orig_get(mbid)

    fake_remote.mb_search_release = slow_search
    fake_remote.mb_get_release = slow_get
    samples = []
    try:
        ok, err = music.start_scan(root, 0, False)
        check("scan started", ok and err is None, str(err))
        t0 = time.time()
        while time.time() - t0 < 60:
            s = music.scan_status()
            samples.append(s)
            if s["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.005)
    finally:
        fake_remote.mb_search_release = orig_search
        fake_remote.mb_get_release = orig_get
        SLEEP[0] = 0.0
    final = samples[-1]
    check("scan done", final["state"] == "done", str(final.get("error")))
    check("status always carries phase keys",
          all({"phase", "phaseDone", "phaseTotal", "phaseEta", "phaseRate",
               "note"} <= set(s) for s in samples))
    scan_ph = [s for s in samples if s.get("phase") == "scan"]
    check("scan phase counts files (8 total)",
          any(s["phaseTotal"] == 8 for s in scan_ph),
          str([(s["phaseDone"], s["phaseTotal"]) for s in scan_ph][:5]))
    id_ph = [s for s in samples if s.get("phase") == "identify"]
    check("identify phase counts CLUSTERS (4 total), not files",
          any(s["phaseTotal"] == 4 for s in id_ph),
          str([(s["phaseDone"], s["phaseTotal"]) for s in id_ph][:8]))
    dones = sorted({s["phaseDone"] for s in id_ph})
    check("identify progress emitted as clusters complete",
          len([d for d in dones if d >= 1]) >= 2, str(dones))
    check("ETA measured from rate (a non-negative number once moving)",
          any(isinstance(s["phaseEta"], (int, float)) and s["phaseEta"] >= 0
              for s in id_ph if s["phaseDone"] >= 1),
          str([s["phaseEta"] for s in id_ph][:8]))
    check("no note on a healthy scan", final.get("note") is None)
    check("no pending clusters after a full identify",
          music.build_results()["pendingIdentify"] == 0)
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P11 =======

def p11_eta_math():
    print("\n== P11: ETA math ==")
    E = music.eta_seconds
    check("no progress -> unknown", E(0, 100, 5.0) is None)
    check("half done in 10s -> 10s left", E(50, 100, 10.0) == 10.0)
    check("quarter done in 5s -> 15s left", E(25, 100, 5.0) == 15.0)
    check("1/s for 5s, 95 left -> 95s", E(5, 100, 5.0) == 95.0)
    check("complete -> 0", E(100, 100, 10.0) == 0.0)
    check("over-complete -> 0", E(120, 100, 10.0) == 0.0)
    check("no total -> unknown", E(3, 0, 5.0) is None)
    check("no clock -> unknown", E(3, 10, 0) is None
          and E(3, 10, None) is None)


# ============================================================== P12 =======

def p12_skip_identify():
    print("\n== P12: skipIdentify = collect->tags->dedupe, zero network ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for a in (1, 2):
        for t in (1, 2):
            n = f"0{t} - S{a} Song {t}.mp3"
            write_audio(root, os.path.join(f"sk{a}", n),
                        payload=b"SK-%d-%d" % (a, t))
            TAGS[n] = {"artist": f"Skip Artist {a}",
                       "album": f"Skip Album {a}",
                       "title": f"S{a} Song {t}", "trackno": t}
    write_audio(root, "Loose.mp3", payload=b"SK-LOOSE")
    TAGS["Loose.mp3"] = {"artist": "Loose Artist", "title": "Loose"}
    write_audio(root, os.path.join("d1", "dup.mp3"), payload=b"SK-DUP")
    write_audio(root, os.path.join("d2", "dup.mp3"), payload=b"SK-DUP")
    TAGS["dup.mp3"] = {"artist": "Dup A", "album": "Dup Alb",
                       "title": "Dup Song", "trackno": 1}

    code, obj = music.api_post("scan", {"root": root, "hashEnabled": True,
                                        "fingerprintEnabled": True,
                                        "skipIdentify": True})
    check("skipIdentify scan accepted", code == 200 and obj.get("ok"),
          f"{code} {obj}")
    t0 = time.time()
    s = {}
    while time.time() - t0 < 30:
        code, s = music.api_get("scan/status", {})
        if s["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    check("skipIdentify scan done", s["state"] == "done",
          s.get("error") or "")
    check("ZERO network calls (fake remote records every call)",
          CALLS == [], str(CALLS[:4]))
    recs = recs_by_name()
    check("all 7 files scanned (2 share one basename)",
          len(music.STATE["recs"]) == 7, str(len(music.STATE["recs"])))
    check("no releases rows written (every cluster pending)",
          music.STATE["releases"] == {})
    res = music.build_results()
    check("results mark all 5 clusters pending",
          res["pendingIdentify"] == 5, str(res["pendingIdentify"]))
    check("dedupe still ran (exact dupe pair grouped)",
          len(music.STATE["groups"]) == 2)
    check("genre fallback applied without network",
          recs["Loose.mp3"]["genre"] == "Unclassified")
    check("note explains the skip + remedy",
          s.get("note") and "skipped" in s["note"].lower()
          and "Resume" in s["note"], str(s.get("note")))
    with music._db() as con:
        n = con.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    check("db has zero releases rows", n == 0, str(n))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P13 =======

def p13_resume_identify():
    print("\n== P13: Resume identification processes ONLY pending clusters ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for a in (1, 2, 3):
        for t in (1, 2):
            n = f"0{t} - R{a} Song {t}.mp3"
            write_audio(root, os.path.join(f"ra{a}", n),
                        payload=b"RS-%d-%d" % (a, t))
            TAGS[n] = {"artist": f"Res Artist {a}",
                       "albumartist": f"Res Artist {a}",
                       "album": f"Res Album {a}",
                       "title": f"R{a} Song {t}", "trackno": t}
    music.SCAN_CANCEL.clear()
    music.run_scan(root, 0, True, False, True)      # skipIdentify
    check("skip scan done", music.scan_status()["state"] == "done")
    check("3 clusters pending", len(music.pending_identify()) == 3,
          str(list(music.pending_identify())))

    # cluster 3 already has a releases row -> resume must NOT touch it
    cid_of = {}
    for r in music.STATE["recs"]:
        for a in (1, 2, 3):
            if f"ra{a}" in r["path"]:
                cid_of[a] = r["cluster_id"]
    music.db_upsert_release({"cluster_id": cid_of[3],
                             "mb_release_id": "pre-3",
                             "albumartist": "Res Artist 3",
                             "album": "Res Album 3", "year": 1993,
                             "source": "musicbrainz", "confidence": 0.9})
    with music.LOCK:
        music.STATE["releases"][cid_of[3]] = {
            "cluster_id": cid_of[3], "mb_release_id": "pre-3",
            "source": "musicbrainz"}
    check("2 clusters pending after pre-seed",
          len(music.pending_identify()) == 2)

    MB_SEARCH[("res artist 1", "res album 1")] = [{
        "mbid": "rel-r1", "title": "Res Album 1", "artist": "Res Artist 1",
        "score": 96, "date": "1991-03-04", "secondary_types": [],
        "release_group_mbid": "rg-r1"}]
    MB_RELEASES["rel-r1"] = {
        "mbid": "rel-r1", "title": "Res Album 1 (Remaster)",
        "artist": "Res Artist 1", "is_va": False, "date": "1991-03-04",
        "label": "ResLabel", "catno": "RL-1", "release_group_mbid": "rg-r1",
        "primary_type": "Album", "secondary_types": [], "tracks": []}
    MB_SEARCH[("res artist 2", "res album 2")] = []   # legit no-match

    code, obj = music.api_post("identify/resume", {})
    check("resume accepted with pending count",
          code == 200 and obj.get("ok") and obj.get("pending") == 2,
          f"{code} {obj}")
    t0 = time.time()
    s = {}
    while time.time() - t0 < 30:
        code, s = music.api_get("scan/status", {})
        if s["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    check("resume done", s["state"] == "done", s.get("error") or "")
    searched = sorted({c[2] for c in CALLS if c[0] == "mb_search_release"})
    check("ONLY pending clusters were looked up (album 3 untouched)",
          searched == ["Res Album 1", "Res Album 2"], str(searched))
    rel1 = music.STATE["releases"][cid_of[1]]
    check("pending cluster identified via MusicBrainz",
          rel1["source"] == "musicbrainz"
          and rel1["album"] == "Res Album 1 (Remaster)", str(rel1))
    check("canonical fields pushed onto that cluster's tracks",
          all(r["album"] == "Res Album 1 (Remaster)" and r["year"] == 1991
              and r["mb_release_id"] == "rel-r1"
              for r in music.STATE["recs"]
              if r["cluster_id"] == cid_of[1]))
    rel2 = music.STATE["releases"][cid_of[2]]
    check("no-match cluster still got its row (no longer pending)",
          rel2["source"] == "none", str(rel2.get("source")))
    check("pre-seeded row preserved",
          music.STATE["releases"][cid_of[3]]["mb_release_id"] == "pre-3")
    res = music.build_results()
    check("nothing pending after resume", res["pendingIdentify"] == 0)
    with music._db() as con:
        rows = {r[0]: r[1] for r in con.execute(
            "SELECT cluster_id, source FROM releases")}
    check("releases persisted per-cluster to db",
          rows.get(cid_of[1]) == "musicbrainz" and rows.get(cid_of[2]) == "none"
          and rows.get(cid_of[3]) == "musicbrainz", str(rows))
    code, obj = music.api_post("identify/resume", {})
    check("second resume 409: nothing pending", code == 409, f"{code} {obj}")
    code, obj = music.api_post("identify", {})
    check("'identify' alias also 409 when done", code == 409, f"{code} {obj}")
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P14 =======

def p14_auto_pause():
    print("\n== P14: auto-pause on consecutive network failures ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    # 15 album clusters; MusicBrainz never answers (MB_SEARCH stays empty)
    for a in range(1, 16):
        n = f"01 - F{a:02d} Song.mp3"
        write_audio(root, os.path.join(f"a{a:02d}", n), payload=b"F-%d" % a)
        TAGS[n] = {"artist": f"Fail Artist {a}", "album": f"Fail Album {a}",
                   "title": f"F{a:02d} Song", "trackno": 1}
    music.SCAN_CANCEL.clear()
    music.run_scan(root, 0, False, False)
    s = music.scan_status()
    check("scan still finishes (local phases complete)",
          s["state"] == "done", s.get("error") or "")
    check("auto-pause note names pending count + remedy",
          s.get("note") == "network unreachable — 4 clusters pending, "
                           "Resume when online", str(s.get("note")))
    n_calls = len([c for c in CALLS if c[0] == "mb_search_release"])
    check("identify stopped right after 11 consecutive failures",
          n_calls == 11, str(n_calls))
    check("11 processed clusters kept their rows",
          len(music.STATE["releases"]) == 11,
          str(len(music.STATE["releases"])))
    check("4 clusters pending for resume",
          len(music.pending_identify()) == 4)

    # network 'comes back': the remaining albums now resolve
    for a in range(12, 16):
        MB_SEARCH[(f"fail artist {a}", f"fail album {a}")] = [{
            "mbid": f"rel-f{a}", "title": f"Fail Album {a}",
            "artist": f"Fail Artist {a}", "score": 90,
            "secondary_types": [], "release_group_mbid": f"rg-f{a}"}]
        MB_RELEASES[f"rel-f{a}"] = {
            "mbid": f"rel-f{a}", "title": f"Fail Album {a}",
            "artist": f"Fail Artist {a}", "is_va": False, "date": "2001",
            "release_group_mbid": f"rg-f{a}", "primary_type": "Album",
            "secondary_types": [], "tracks": []}
    CALLS.clear()
    ok, err = music.start_identify()
    check("resume after pause starts", ok and err is None, str(err))
    t0 = time.time()
    while time.time() - t0 < 30:
        s = music.scan_status()
        if s["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    check("resume done, note cleared",
          s["state"] == "done" and s.get("note") is None, str(s.get("note")))
    check("only the 4 pending clusters were processed",
          len([c for c in CALLS if c[0] == "mb_search_release"]) == 4)
    check("nothing pending now", len(music.pending_identify()) == 0)

    # a success resets the streak: 5 fail, 1 hit, 9 fail -> NO pause
    root2 = os.path.join(tmp, "lib2")
    for a in range(1, 16):
        n = f"01 - G{a:02d} Song.mp3"
        write_audio(root2, os.path.join(f"b{a:02d}", n), payload=b"G-%d" % a)
        TAGS[n] = {"artist": f"Mix Artist {a}", "album": f"Mix Album {a}",
                   "title": f"G{a:02d} Song", "trackno": 1}
    MB_SEARCH[("mix artist 6", "mix album 6")] = [{
        "mbid": "rel-m6", "title": "Mix Album 6", "artist": "Mix Artist 6",
        "score": 90, "secondary_types": [], "release_group_mbid": "rg-m6"}]
    MB_RELEASES["rel-m6"] = {
        "mbid": "rel-m6", "title": "Mix Album 6", "artist": "Mix Artist 6",
        "is_va": False, "date": "2006", "release_group_mbid": "rg-m6",
        "primary_type": "Album", "secondary_types": [], "tracks": []}
    CALLS.clear()
    music.SCAN_CANCEL.clear()
    music.run_scan(root2, 0, False, False)
    s = music.scan_status()
    check("success resets the streak: no auto-pause",
          s["state"] == "done" and s.get("note") is None, str(s.get("note")))
    check("all 15 clusters processed",
          len(music.STATE["releases"]) == 15,
          str(len(music.STATE["releases"])))
    shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== P15 =======

def p15_robustness():
    print("\n== P15: per-cluster error containment + hard timeouts ==")
    tmp = mkd()
    fresh(tmp)
    root = os.path.join(tmp, "lib")
    for a, alb in ((1, "Boom Album"), (2, "Fine Album 2"),
                   (3, "Fine Album 3")):
        for t in (1, 2):
            n = f"0{t} - X{a} Song {t}.mp3"
            write_audio(root, os.path.join(f"x{a}", n),
                        payload=b"X-%d-%d" % (a, t))
            TAGS[n] = {"artist": f"X Artist {a}", "album": alb,
                       "title": f"X{a} Song {t}", "trackno": t}
    orig = music.identify_cluster

    def boom(cid, members, cfg):
        if music._majority(m.get("album") for m in members) == "Boom Album":
            raise RuntimeError("boom cluster")
        return orig(cid, members, cfg)

    music.identify_cluster = boom
    try:
        music.SCAN_CANCEL.clear()
        music.run_scan(root, 0, False, False)
    finally:
        music.identify_cluster = orig
    s = music.scan_status()
    check("one exploding cluster cannot stall the phase",
          s["state"] == "done", s.get("error") or "")
    check("healthy clusters still processed (2 rows)",
          len(music.STATE["releases"]) == 2,
          str(len(music.STATE["releases"])))
    check("exploded cluster left pending (no row) for resume",
          len(music.pending_identify()) == 1)
    check("later clusters were reached after the failure",
          any(c[0] == "mb_search_release" and c[2] == "Fine Album 3"
              for c in CALLS))
    shutil.rmtree(tmp, ignore_errors=True)

    # the real remote module: every HTTP call has a hard timeout <= 15s
    import inspect
    real_remote = _load_real_module("music_remote")
    check("real fetcher HTTP_TIMEOUT <= 15s",
          real_remote.HTTP_TIMEOUT <= 15, str(real_remote.HTTP_TIMEOUT))
    check("timeout actually passed to urlopen",
          "timeout=HTTP_TIMEOUT" in inspect.getsource(
              real_remote._real_fetcher))


# ============================================================== main ======

def main():
    os.makedirs(TMP_ROOT, exist_ok=True)
    rc = 0
    try:
        unit_tests()
        p1_scan_identify()
        p2_va_detection()
        p3_dedupe_stages()
        p4_best_copy()
        p5_plan_paths()
        p6_execute_undo()
        p7_cancel()
        p8_restart_restore()
        p9_api()
        p10_phase_progress()
        p11_eta_math()
        p12_skip_identify()
        p13_resume_identify()
        p14_auto_pause()
        p15_robustness()
        e2e_real_fixtures()
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
