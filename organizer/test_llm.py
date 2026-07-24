#!/usr/bin/env python3
r"""Offline tests for the LLM filename cracker (llm_assist.py) and the
re-identification CLI (llm_reidentify.py).

No real network anywhere: a fake Ollama and a fake TMDB are injected via
each function's fetcher parameter. The database is a temp-dir copy of the
cinema schema (production cinema.db is never touched).
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cinema  # noqa: E402
import llm_assist  # noqa: E402
import llm_reidentify  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------- fakes

def make_llm(reply, model="qwen2.5-14b-aclarc"):
    """Fake LLM fetcher covering BOTH dialects: OpenAI/llama-swap (/models,
    /chat/completions) and native Ollama (/api/tags, /api/generate). `reply`
    is the model's text output (str) or an Exception instance to raise."""
    def fetch(url, payload=None, timeout=None):
        if url.endswith("/models"):
            return {"data": [{"id": model}]}
        if url.endswith("/api/tags"):
            return {"models": [{"name": model}]}
        if url.endswith(("/api/generate", "/chat/completions")):
            if isinstance(reply, Exception):
                raise reply
            if url.endswith("/chat/completions"):
                return {"choices": [{"message": {"content": reply}}]}
            return {"response": reply}
        raise AssertionError("unexpected url " + url)
    return fetch


def _prompt_of(payload):
    """The user prompt from either an Ollama (/api/generate, 'prompt') or an
    OpenAI (/chat/completions, messages) request payload."""
    if not isinstance(payload, dict):
        return ""
    if "prompt" in payload:
        return payload.get("prompt", "")
    for m in payload.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "user":
            return m.get("content", "")
    return ""


TMDB_GENRES = {"genres": [{"id": 28, "name": "Action"},
                          {"id": 12, "name": "Adventure"},
                          {"id": 35, "name": "Comedy"}]}


def make_tmdb(hits):
    """Fake TMDB fetcher returning canned search hits (genre list served
    for /genre/ urls)."""
    def fetch(url):
        if "/genre/" in url:
            return TMDB_GENRES
        return {"results": hits}
    return fetch


HIT_TURNER = {"title": "Turner & Hooch", "original_title": "Turner & Hooch",
              "release_date": "1989-07-28", "genre_ids": [35, 28]}
HIT_BEAST = {"title": "Beauty and the Beast",
             "original_title": "Beauty and the Beast",
             "release_date": "1991-11-22", "genre_ids": [16, 10751]}


# ---------------------------------------------------------------- suites

def test_extract_json():
    print("\n== llm_assist._extract_json ==")
    e = llm_assist._extract_json
    check("plain json", e('{"title": "Rush Hour", "year": 1998}')
          == {"title": "Rush Hour", "year": 1998})
    fenced = '```json\n{"title": "Dumbo", "year": 1941}\n```'
    check("markdown fenced", e(fenced) == {"title": "Dumbo", "year": 1941})
    prose = 'Sure! Here is the identification:\n{"title": "Cosmopolis"}\nHope that helps.'
    check("prose wrapped", e(prose) == {"title": "Cosmopolis"})
    check("no json -> None", e("I have no idea what this file is.") is None)
    check("empty -> None", e("") is None and e(None) is None)
    # first brace opens junk, second one is the real object
    weird = '{broken...\nthen {"title": "Fantasia", "year": 1941}'
    check("skips unparseable brace", e(weird)
          == {"title": "Fantasia", "year": 1941})
    check("object inside array recovered", e('[{"title": "X"}]')
          == {"title": "X"})


def test_crack_filename():
    print("\n== llm_assist.crack_filename ==")
    good = make_llm('{"title": "Turner & Hooch", "year": 1989,'
                       ' "kind": "movie", "confidence": 0.85}')
    r = llm_assist.crack_filename("daa-turner.and.hooch-1080p.mkv",
                                  fetcher=good)
    check("valid reply cracked", r is not None and r["title"] == "Turner & Hooch"
          and r["year"] == 1989 and r["kind"] == "movie"
          and abs(r["confidence"] - 0.85) < 1e-9, repr(r))

    low = make_llm('{"title": "Something", "year": 2000,'
                      ' "kind": "movie", "confidence": 0.2}')
    check("below confidence gate -> None",
          llm_assist.crack_filename("x.mkv", fetcher=low) is None)

    declined = make_llm('{"title": null, "year": null, "kind": "movie",'
                           ' "confidence": 0.0}')
    check("title null (declined) -> None",
          llm_assist.crack_filename("japhson-faff.mkv", fetcher=declined)
          is None)

    garbage = make_llm("I cannot answer in JSON, sorry!")
    check("malformed reply -> None",
          llm_assist.crack_filename("bfdtmdc.mkv", fetcher=garbage) is None)

    check("fetcher raising -> None",
          llm_assist.crack_filename("x.mkv",
                                    fetcher=make_llm(OSError("down")))
          is None)

    stryr = make_llm('{"title": "Dumbo", "year": "1941", "kind": "movie",'
                        ' "confidence": 0.8}')
    r = llm_assist.crack_filename("lchd-dumbo.mkv", fetcher=stryr)
    check("string year coerced", r and r["year"] == 1941, repr(r))

    badyr = make_llm('{"title": "Old", "year": 1400, "kind": "movie",'
                        ' "confidence": 0.8}')
    r = llm_assist.crack_filename("x-old.mkv", fetcher=badyr)
    check("out-of-range year dropped", r and r["year"] is None, repr(r))

    badkind = make_llm('{"title": "X", "kind": "documentary",'
                          ' "confidence": 0.9}')
    r = llm_assist.crack_filename("x.mkv", hint="tv", fetcher=badkind)
    check("bad kind coerced to hint", r and r["kind"] == "tv", repr(r))

    over = make_llm('{"title": "X", "confidence": 42}')
    r = llm_assist.crack_filename("x.mkv", fetcher=over)
    check("confidence clamped", r and r["confidence"] == 1.0, repr(r))

    # regression: dotted scene stems must reach the LLM intact --
    # splitext would eat ".hooch-1080p" as if it were an extension
    seen = {}

    def capture(url, payload=None, timeout=None):
        seen.clear()
        seen.update(payload or {})
        reply = ('{"title": "Turner & Hooch", "year": 1989,'
                 ' "kind": "movie", "confidence": 0.85}')
        if url.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": reply}}]}
        return {"response": reply}
    llm_assist.crack_filename("daa-turner.and.hooch-1080p", fetcher=capture)
    check("dotted stem not truncated",
          '"daa-turner.and.hooch-1080p"' in _prompt_of(seen),
          _prompt_of(seen)[-90:])
    llm_assist.crack_filename("lchd-batb-720p.mkv", fetcher=capture)
    check("real extension still stripped",
          '"lchd-batb-720p"' in _prompt_of(seen)
          and ".mkv" not in _prompt_of(seen).rsplit("Now identify", 1)[-1],
          _prompt_of(seen)[-90:])


def test_availability():
    print("\n== llm_assist.available / list_models ==")
    cfg = dict(llm_assist.DEFAULTS)
    check("available when model present",
          llm_assist.available(cfg, fetcher=make_llm("{}")) is True)
    other = make_llm("{}", model="some-other-model:latest")
    check("unavailable when model missing",
          llm_assist.available(cfg, fetcher=other) is False)

    def down(url, payload=None, timeout=None):
        raise OSError("connection refused")
    check("unavailable when server down",
          llm_assist.available(cfg, fetcher=down) is False)
    check("list_models parses names",
          llm_assist.list_models(cfg, fetcher=make_llm("{}"))
          == ["qwen2.5-14b-aclarc"])
    check("list_models [] when down",
          llm_assist.list_models(cfg, fetcher=down) == [])


def test_verify():
    print("\n== llm_reidentify.verify_with_tmdb (fake TMDB) ==")
    gmaps = {"movie": {28: "Action", 12: "Adventure", 35: "Comedy",
                       16: "Animation", 10751: "Family"}}
    crack = {"title": "Turner and Hooch", "year": 1989, "kind": "movie",
             "confidence": 0.85}
    r = llm_reidentify.verify_with_tmdb(crack, "k", make_tmdb([HIT_TURNER]),
                                        gmaps)
    check("tmdb confirms similar title", r is not None
          and r["title"] == "Turner & Hooch" and r["year"] == 1989
          and r["genre"] == "Comedy" and r["subgenre"] == "Action", repr(r))

    crack2 = {"title": "Beauty and the Beast", "year": 1989, "kind": "movie",
              "confidence": 0.7}
    r = llm_reidentify.verify_with_tmdb(crack2, "k", make_tmdb([HIT_BEAST]),
                                        gmaps)
    check("year mismatch >1 rejected", r is None, repr(r))

    crack3 = {"title": "Beauty and the Beast", "year": None, "kind": "movie",
              "confidence": 0.7}
    r = llm_reidentify.verify_with_tmdb(crack3, "k", make_tmdb([HIT_BEAST]),
                                        gmaps)
    check("yearless guess adopts tmdb year", r is not None
          and r["year"] == 1991, repr(r))

    crack4 = {"title": "Zzyzx the Obliterator", "year": 2001,
              "kind": "movie", "confidence": 0.8}
    r = llm_reidentify.verify_with_tmdb(crack4, "k", make_tmdb([HIT_TURNER]),
                                        gmaps)
    check("hallucinated title rejected", r is None, repr(r))

    r = llm_reidentify.verify_with_tmdb(crack, "k", make_tmdb([]), gmaps)
    check("no hits rejected", r is None)

    # self-consistent hallucination: LLM title and TMDB hit agree, but the
    # actual filename says "fantasia" -- must be rejected
    fox = {"title": "Fantastic Mr. Fox", "original_title": "Fantastic Mr. Fox",
           "release_date": "2009-10-23", "genre_ids": [16]}
    crack5 = {"title": "Fantastic Mr. Fox", "year": 2009, "kind": "movie",
              "confidence": 0.8}
    r = llm_reidentify.verify_with_tmdb(crack5, "k", make_tmdb([fox]), gmaps,
                                        file_guess="Besthd-Fantasia")
    check("filename gate blocks hallucination", r is None, repr(r))

    # remake disambiguation: both hits pass; the one closest to the
    # filename (word order may differ) must win
    lw60 = {"title": "The Lost World", "original_title": "The Lost World",
            "release_date": "1960-07-13", "genre_ids": [12]}
    lw97 = {"title": "The Lost World: Jurassic Park",
            "original_title": "The Lost World: Jurassic Park",
            "release_date": "1997-05-23", "genre_ids": [28, 12]}
    crack6 = {"title": "The Lost World", "year": None, "kind": "movie",
              "confidence": 0.9}
    r = llm_reidentify.verify_with_tmdb(
        crack6, "k", make_tmdb([lw60, lw97]), gmaps,
        file_guess="Jurassic Park II - The Lost World")
    check("closest-to-filename hit wins", r is not None and r["year"] == 1997,
          repr(r))
    check("word overlap is order-free",
          llm_reidentify._word_overlap(
              "Jurassic Park II - The Lost World",
              "The Lost World: Jurassic Park") == 1.0)

    # gate-3 filename match rules
    fm = llm_reidentify._filename_match
    check("acronym: batb ~ Beauty And The Beast",
          fm("lchd batb", "Beauty and the Beast") is True)
    check("prefix: poca ~ Pocahontas", fm("pfa poca", "Pocahontas") is True)
    check("squashed: rushhour ~ Rush Hour",
          fm("rushhour shk", "Rush Hour") is True)
    check("overlap: dumbo", fm("lchd dumbo", "Dumbo") is True)
    check("hallucination still blocked",
          fm("besthd fantasia", "Fantastic Mr. Fox") is False)
    check("unrelated blocked", fm("japhson faff", "Turner & Hooch") is False)
    check("anchor keeps group prefix + all words",
          llm_reidentify.anchor_text("lchd-dumbo.mkv") == "lchd dumbo"
          and llm_reidentify.anchor_text("daa-turner.and.hooch-1080p.mkv")
          == "daa turner and hooch",
          repr(llm_reidentify.anchor_text("daa-turner.and.hooch-1080p.mkv")))

    def boom(url):
        raise OSError("tmdb down")
    check("tmdb error -> None (no raise)",
          llm_reidentify.verify_with_tmdb(crack, "k", boom, gmaps) is None)


def test_process_one():
    print("\n== llm_reidentify.process_one ==")
    gmaps = {"movie": {28: "Action", 35: "Comedy"}}
    llm = make_llm('{"title": "Turner & Hooch", "year": 1989,'
                      ' "kind": "movie", "confidence": 0.85}')
    tmdb = make_tmdb([HIT_TURNER])
    cand = {"id": None, "path": r"F:\x\daa-turner.and.hooch-1080p.mkv",
            "filename": "daa-turner.and.hooch-1080p.mkv", "dir": r"F:\x"}
    out = llm_reidentify.process_one(cand, "movie", dict(llm_assist.DEFAULTS),
                                     "k", tmdb, gmaps, llm_fetcher=llm)
    check("cracked+verified", out["outcome"] == llm_reidentify.OUT_CRACKED
          and out["verified"]["title"] == "Turner & Hooch", repr(out))

    llm_no = make_llm('{"title": null, "year": null, "kind": "movie",'
                         ' "confidence": 0.0}')
    out = llm_reidentify.process_one(cand, "movie", dict(llm_assist.DEFAULTS),
                                     "k", tmdb, gmaps, llm_fetcher=llm_no)
    check("llm-no-answer", out["outcome"] == llm_reidentify.OUT_NOANSWER
          and out["verified"] is None)

    llm_bad = make_llm('{"title": "Hooch the Dog Detective",'
                          ' "year": 1989, "kind": "movie",'
                          ' "confidence": 0.9}')
    out = llm_reidentify.process_one(cand, "movie", dict(llm_assist.DEFAULTS),
                                     "k", make_tmdb([HIT_TURNER]), gmaps,
                                     llm_fetcher=llm_bad)
    check("llm-guess-rejected",
          out["outcome"] == llm_reidentify.OUT_REJECTED
          and out["verified"] is None and out["crack"] is not None)


def test_db_writeback():
    print("\n== write-back into a temp cinema.db ==")
    tmpdir = tempfile.mkdtemp(prefix="llm_unit_")
    old_db = cinema.CINEMA_DB
    try:
        cinema.CINEMA_DB = os.path.join(tmpdir, "cinema.db")
        cinema.db_init()
        con = sqlite3.connect(cinema.CINEMA_DB, timeout=30)
        # unknown row as a scan would have written it
        con.execute("INSERT INTO media (path, filename, dir, ext, kind)"
                    " VALUES (?,?,?,?, 'unknown')",
                    (r"F:\Movies\Organized\_Unidentified\lchd-dumbo.mkv",
                     "lchd-dumbo.mkv",
                     r"F:\Movies\Organized\_Unidentified", ".mkv"))
        con.commit()
        res = {"kind": "movie", "title": "Dumbo", "year": 1941,
               "genre": "Animation", "subgenre": "Family"}
        cand = {"id": 1, "path": r"F:\Movies\Organized\_Unidentified\lchd-dumbo.mkv",
                "filename": "lchd-dumbo.mkv"}
        llm_reidentify.apply_result(con, cand, res)
        row = con.execute("SELECT kind, title, year, genre, subgenre,"
                          " genre_source FROM media WHERE id=1").fetchone()
        check("media row updated", row == ("movie", "Dumbo", 1941,
                                           "Animation", "Family",
                                           "llm+tmdb"), repr(row))
        ic = cinema.ident_cache_get(cinema.parse_media_name("lchd-dumbo.mkv")
                                    .get("guess_title") or "lchd-dumbo")
        check("ident_cache written with source 'tmdb' (sticks on rescan)",
              ic is not None and ic[0] == "Dumbo" and ic[1] == 1941
              and ic[4] == "tmdb", repr(ic))
        gc = cinema.genre_cache_get("movie", "Dumbo", 1941)
        check("genre_cache written", gc == ("Animation", "Family", "tmdb"),
              repr(gc))

        # folder-mode file with no media row -> minimal row inserted
        cand2 = {"id": None,
                 "path": r"F:\Movies\Organized\_Unidentified\bfdtmdc.mkv",
                 "filename": "bfdtmdc.mkv"}
        llm_reidentify.apply_result(con, cand2, res)
        row2 = con.execute("SELECT kind, title, genre_source FROM media"
                           " WHERE path=?", (cand2["path"],)).fetchone()
        check("missing row inserted", row2 == ("movie", "Dumbo", "llm+tmdb"),
              repr(row2))
        con.close()
    finally:
        cinema.CINEMA_DB = old_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_guess_key():
    print("\n== guess_key_for matches the scanner's guess ==")
    k = llm_reidentify.guess_key_for("lchd-batb-720p.mkv")
    scan_guess = cinema.parse_media_name("lchd-batb-720p.mkv")["guess_title"]
    check("guess key == scanner guess_title", k == scan_guess,
          f"{k!r} vs {scan_guess!r}")
    k2 = llm_reidentify.guess_key_for("s4a-the.brass.teapot.hdrip.xvid-s4a.avi")
    check("s4a guess key", k2 == cinema.parse_media_name(
        "s4a-the.brass.teapot.hdrip.xvid-s4a.avi")["guess_title"], k2)


def test_dialects():
    """Both wire dialects: OpenAI/llama-swap and native Ollama route to the
    right endpoint with the right payload shape and parse back correctly."""
    print("\n== llm_assist dual-dialect wire ==")
    reply = ('{"title": "Rush Hour", "year": 1998, "kind": "movie",'
             ' "confidence": 0.9}')
    seen = {}

    def cap(url, payload=None, timeout=None):
        seen["url"], seen["payload"] = url, payload
        if url.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": reply}}]}
        return {"response": reply}

    oa = {"api": "openai", "endpoint": "http://x/v1", "model": "m",
          "timeoutMs": 120000, "minConfidence": 0.5}
    r = llm_assist.crack_filename("rushhour720p-shk", cfg=oa, fetcher=cap)
    check("openai -> /chat/completions + messages",
          seen["url"].endswith("/chat/completions")
          and "messages" in (seen["payload"] or {}))
    check("openai reply cracked", r and r["title"] == "Rush Hour", repr(r))
    check("openai available", llm_assist.available(
        oa, fetcher=make_llm("{}", model="m")) is True)

    ol = {"api": "ollama", "endpoint": "http://x:11434", "model": "m",
          "timeoutMs": 120000, "minConfidence": 0.5}
    r = llm_assist.crack_filename("rushhour720p-shk", cfg=ol, fetcher=cap)
    check("ollama -> /api/generate + prompt",
          seen["url"].endswith("/api/generate")
          and "prompt" in (seen["payload"] or {}))
    check("ollama reply cracked", r and r["title"] == "Rush Hour", repr(r))
    check("ollama available", llm_assist.available(
        ol, fetcher=make_llm("{}", model="m")) is True)

    # <think>…</think> reasoning leakage is stripped before JSON extraction
    think = make_llm('<think>hmm this looks like Rush Hour</think>\n' + reply)
    r = llm_assist.crack_filename("rushhour720p-shk", cfg=oa, fetcher=think)
    check("openai strips think block", r and r["title"] == "Rush Hour", repr(r))


def main():
    test_extract_json()
    test_crack_filename()
    test_availability()
    test_dialects()
    test_verify()
    test_process_one()
    test_db_writeback()
    test_guess_key()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
