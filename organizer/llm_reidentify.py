#!/usr/bin/env python3
"""LLM-assisted re-identification for the Cinema Organizer.

Reads kind='unknown' rows from cinema.db (or video files in --folder DIR),
asks the local Ollama LLM (llm_assist.py) to crack each cryptic filename,
and accepts the answer ONLY when TMDB confirms a similar title. Confirmed
identifications are written back:

  * media row: kind/title/year/genre/subgenre, genre_source='llm+tmdb'
  * ident_cache (source 'tmdb') + genre_cache (source 'tmdb') so a future
    cinema.run_scan adopts the identification and it sticks

Media files are never touched (F:\\ reads are directory listings only).
cinema.py is imported read-only for its parser/TMDB/cache helpers.

Usage:
  python llm_reidentify.py [--limit N] [--offset N] [--dry-run]
                           [--folder DIR] [--hint movie|tv|auto]
                           [--model NAME] [--db PATH]
"""
import argparse
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cinema  # noqa: E402  (read-only use: parser, TMDB helpers, caches)
import llm_assist  # noqa: E402

BACKUP_DIR = os.path.join(BASE, "db_safety_backup")

OUT_CRACKED = "cracked+verified"
OUT_REJECTED = "llm-guess-rejected"
OUT_NOANSWER = "llm-no-answer"


# =================================================================== tmdb

def tmdb_search(kind, title, year, api_key, fetcher):
    """TMDB search hits for the LLM guess (throttled, token/key auth via
    the injected fetcher). Returns [] on any failure."""
    endpoint = "movie" if kind == "movie" else "tv"
    url = (f"https://api.themoviedb.org/3/search/{endpoint}"
           f"?{cinema._key_qs(api_key)}query={urllib.parse.quote(cinema._fold(title))}"
           "&include_adult=false")
    if year and kind == "movie":
        url += f"&year={year}"
    try:
        data = cinema._throttled(url, fetcher)
    except Exception:
        return []
    return (data.get("results") or []) if isinstance(data, dict) else []


_STOPWORDS = {"the", "a", "an", "of", "and", "to", "in", "s"}


def anchor_text(filename):
    """Gate-3 anchor: the filename's own words -- quality/codec/bracket
    noise removed but release-group prefixes KEPT (the parser's guess_title
    is unusable here: it strips trailing words as 'group', reducing
    'lchd-dumbo' to 'Lchd')."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    s = re.sub(r"\{[^}]*\}", " ", stem)
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    for _, rx in cinema._RES_RULES:
        s = rx.sub(" ", s)
    for _, rx in cinema._SRC_RULES:
        s = rx.sub(" ", s)
    s = cinema._CODEC.sub(" ", s)
    s = cinema._AUDIO.sub(" ", s)
    s = cinema._MISC.sub(" ", s)
    s = re.sub(r"[._\-]+", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _word_overlap(anchor, title):
    """Fraction of title's content words present in the anchor's word set.
    Word-order free: "Jurassic Park II - The Lost World" fully covers
    "The Lost World: Jurassic Park"."""
    aw = set(cinema.normalize_title(cinema._fold(anchor)).split())
    tw = [w for w in cinema.normalize_title(cinema._fold(title)).split()
          if w not in _STOPWORDS]
    if not tw:
        return 0.0
    return sum(1 for w in tw if w in aw) / len(tw)


def _squashed(s):
    return cinema.normalize_title(cinema._fold(s)).replace(" ", "")


def _word_f1(anchor, title):
    """F1 over content-word sets: how much of the hit the filename covers
    AND how much of the filename the hit explains. Ranks
    'The Lost World: Jurassic Park' above 'The Lost World' for a file
    named 'Jurassic Park II - The Lost World'."""
    aw = {w for w in cinema.normalize_title(cinema._fold(anchor)).split()
          if w not in _STOPWORDS}
    tw = {w for w in cinema.normalize_title(cinema._fold(title)).split()
          if w not in _STOPWORDS}
    if not aw or not tw:
        return 0.0
    inter = len(aw & tw)
    return 2 * inter / (len(aw) + len(tw))


def _acronym_match(anchor, title):
    """An anchor code word spells the title's initials in order:
    'batb' ~ Beauty And The Beast (stopwords included, release codes do
    count them)."""
    aw = set(cinema.normalize_title(cinema._fold(anchor)).split())
    initials = "".join(w[0] for w in
                       cinema.normalize_title(cinema._fold(title)).split())
    return any(len(w) >= 3 and w == initials for w in aw)


def _prefix_match(anchor, title):
    """An anchor code word (>=4 chars) abbreviates a title word by prefix,
    or vice versa: 'poca' ~ Pocahontas. ('fantasia' vs 'fantastic'
    correctly fails: neither prefixes the other.)"""
    aw = {w for w in cinema.normalize_title(cinema._fold(anchor)).split()
          if len(w) >= 4}
    tw = {w for w in cinema.normalize_title(cinema._fold(title)).split()
          if w not in _STOPWORDS}
    for a in aw:
        for t in tw:
            if len(a) >= 4 and (t.startswith(a) or
                                (len(t) >= 4 and a.startswith(t))):
                return True
    return False


def _filename_match(anchor, title):
    """Gate 3: TMDB hit must agree with the actual filename, not just with
    the LLM. Accepts plain containment/similarity, word-order-free overlap,
    squashed matches ('rushhour' ~ 'Rush Hour'), scene acronyms
    ('batb' ~ Beauty And The Beast) and prefix abbreviations
    ('poca' ~ Pocahontas) -- while still blocking self-consistent
    hallucinations ('fantasia' -/-> 'Fantastic Mr. Fox')."""
    if cinema._sim_accept(anchor, title) or _word_overlap(anchor, title) >= 0.6:
        return True
    sa, st = _squashed(anchor), _squashed(title)
    if len(st) >= 5 and st in sa:
        return True
    return _acronym_match(anchor, title) or _prefix_match(anchor, title)


def verify_with_tmdb(crack, api_key, fetcher, gmaps, file_guess=None):
    """LLM guess -> TMDB-confirmed identification or None.

    Three gates, all required:
      1. hit title passes cinema's similarity gate against the LLM title
      2. hit year within 1 of the LLM year (when the LLM gave one)
      3. hit title passes the same gate against the FILENAME-derived guess
         (file_guess) -- stops self-consistent hallucinations like
         "fantasia" -> "Fantastic Mr. Fox", where the LLM title and the
         TMDB hit agree with each other but not with the actual file
    Among hits passing all gates the one whose title is closest to the
    filename wins (disambiguates remakes/same-title releases). Adopted
    title/year/genres always come from TMDB, never from the LLM. When the
    LLM's own kind finds nothing, the other endpoint is tried as a
    fallback (documentaries are often misclassified movie<->tv)."""
    kind0 = crack.get("kind") if crack.get("kind") in ("movie", "tv") else "movie"
    title, year = crack.get("title"), crack.get("year")
    if not title:
        return None
    anchor = file_guess or title
    best = None
    for kind in (kind0, "tv" if kind0 == "movie" else "movie"):
        # candidate queries: the LLM title (year-filtered first), then the
        # filename-derived guess -- a yearless LLM title like "The Lost
        # World" otherwise surfaces the wrong remake in TMDB's top 5
        queries = [(title, year)]
        if year and kind == "movie":
            queries.append((title, None))
        if file_guess and cinema.normalize_title(file_guess) \
                != cinema.normalize_title(title):
            queries.append((file_guess, None))
        seen_ids, hits = set(), []
        for q, qy in queries:
            for h in tmdb_search(kind, q, qy, api_key, fetcher)[:5]:
                hid = h.get("id")
                if hid is None:
                    hid = (h.get("title") or h.get("name"),
                           h.get("release_date") or h.get("first_air_date"))
                if hid not in seen_ids:
                    seen_ids.add(hid)
                    hits.append(h)
        date_key = "release_date" if kind == "movie" else "first_air_date"
        title_keys = ("title", "name") if kind == "movie" else ("name", "title")
        orig_keys = ("original_title", "original_name") if kind == "movie" \
            else ("original_name", "original_title")
        for hit in hits:
            names = [hit.get(title_keys[0]) or hit.get(title_keys[1]) or "",
                     hit.get(orig_keys[0]) or hit.get(orig_keys[1]) or ""]
            if not any(cinema._sim_accept(title, n) for n in names):
                continue
            hy = (hit.get(date_key) or "")[:4]
            if not hy.isdigit():
                continue
            if year and abs(int(hy) - year) > 1:
                continue
            canon = hit.get(title_keys[0]) or hit.get(title_keys[1]) or title
            if not any(_filename_match(anchor, n) for n in [canon] + names):
                continue
            sim = max(_word_f1(anchor, n) for n in [canon] + names if n)
            if year and int(hy) == year:
                sim += 0.05          # prefer exact-year match on ties
            if best is None or sim > best[0]:
                best = (sim, kind, canon, int(hy), hit)
    if best is None:
        return None
    _, kind, canon, hy, hit = best
    gmap = gmaps.get(kind) or {}
    gnames = [gmap.get(gid) for gid in hit.get("genre_ids", [])]
    gnames = [n for n in gnames if n]
    return {"kind": kind, "title": canon, "year": hy,
            "genre": gnames[0] if gnames else "Unclassified",
            "subgenre": gnames[1] if len(gnames) > 1 else "General"}


# =================================================================== write

def _parse_safe(filename):
    """cinema.parse_media_name with a minimal local fallback (the cinema
    module is owned by another component; a transient import/parser hiccup
    must not crash a batch run)."""
    try:
        return cinema.parse_media_name(filename)
    except Exception:
        import re
        stem = os.path.splitext(filename)[0]
        tv = re.search(r"[sS]\d{1,2}[eE]\d{1,3}", stem)
        yr = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
        kind = "tv" if tv else ("movie" if yr else "unknown")
        return {"kind": kind, "guess_title": None}


def guess_key_for(filename):
    """The ident_cache key a future scan would compute for this file, so the
    identification sticks (run_scan re-derives guess_title on rescan)."""
    rec = _parse_safe(filename)
    return rec.get("guess_title") or os.path.splitext(filename)[0]


def apply_result(con, cand, res):
    """Persist one verified identification. media row updated (or inserted
    for --folder files never scanned); ident_cache gets source 'tmdb' --
    cinema.run_scan only adopts cached idents with source 'tmdb', anything
    else is treated as terminal-but-unidentified."""
    now = datetime.now().isoformat(sep=" ")
    row = None
    if cand.get("id") is not None:
        row = con.execute("SELECT id FROM media WHERE id=?",
                          (cand["id"],)).fetchone()
    if row is None and cand.get("path"):
        row = con.execute("SELECT id FROM media WHERE path=?",
                          (cand["path"],)).fetchone()
    if row is not None:
        con.execute("UPDATE media SET kind=?, title=?, year=?, genre=?,"
                    " subgenre=?, genre_source='llm+tmdb' WHERE id=?",
                    (res["kind"], res["title"], res["year"], res["genre"],
                     res["subgenre"], row[0]))
    else:
        con.execute("INSERT OR IGNORE INTO media"
                    " (path, filename, dir, ext, kind, title, year, genre,"
                    "  subgenre, genre_source, scanned_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cand["path"], cand["filename"],
                     os.path.dirname(cand["path"]),
                     os.path.splitext(cand["filename"])[1].lower(),
                     res["kind"], res["title"], res["year"], res["genre"],
                     res["subgenre"], "llm+tmdb", now))
    con.commit()
    # caches make it stick across future rescans (adopted as source 'tmdb')
    cinema.ident_cache_put(guess_key_for(cand["filename"]), res["title"],
                           res["year"], res["genre"], res["subgenre"], "tmdb")
    cinema.genre_cache_put(res["kind"], res["title"], res["year"],
                           res["genre"], res["subgenre"], "tmdb")


# =================================================================== candidates

def db_candidates(con, limit=None, offset=0):
    sql = ("SELECT id, path, filename, dir FROM media WHERE kind='unknown'"
           " ORDER BY filename")
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return [{"id": r[0], "path": r[1], "filename": r[2], "dir": r[3]}
            for r in con.execute(sql)]


def folder_candidates(folder, limit=None, offset=0):
    """Unidentified video files in one directory (listing only; files are
    never opened or modified)."""
    out = []
    for fn in sorted(os.listdir(folder)):
        p = os.path.join(folder, fn)
        if not os.path.isfile(p):
            continue
        if os.path.splitext(fn)[1].lower() not in cinema.VIDEO_EXTS:
            continue
        if _parse_safe(fn)["kind"] != "unknown":
            continue
        out.append({"id": None, "path": p, "filename": fn,
                    "dir": folder})
    return out[offset:offset + limit] if limit else out[offset:]


def pick_hint(cand, mode):
    if mode in ("movie", "tv"):
        return mode
    parts = (cand.get("dir") or "").replace("/", "\\").lower().split("\\")
    return "tv" if "tv" in parts else "movie"


# =================================================================== core

def process_one(cand, hint, llm_cfg, api_key, tmdb_fetcher, gmaps,
                llm_fetcher=None):
    """One candidate -> outcome dict. No writes here."""
    crack = llm_assist.crack_filename(cand["filename"], hint, cfg=llm_cfg,
                                      fetcher=llm_fetcher)
    if not crack:
        return {"file": cand["filename"], "outcome": OUT_NOANSWER,
                "crack": None, "verified": None}
    file_guess = anchor_text(cand["filename"])
    res = verify_with_tmdb(crack, api_key, tmdb_fetcher, gmaps,
                           file_guess=file_guess)
    if not res:
        return {"file": cand["filename"], "outcome": OUT_REJECTED,
                "crack": crack, "verified": None}
    return {"file": cand["filename"], "outcome": OUT_CRACKED,
            "crack": crack, "verified": res}


def backup_db():
    """WAL-safe snapshot of cinema.db before any write."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "cinema_pre_llm.db")
    src = sqlite3.connect(cinema.CINEMA_DB, timeout=30)
    tgt = sqlite3.connect(dst)
    src.backup(tgt)
    tgt.close()
    src.close()
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="crack + verify but write nothing")
    ap.add_argument("--folder", help="scan a directory for unidentified "
                                     "videos instead of cinema.db rows")
    ap.add_argument("--hint", choices=["movie", "tv", "auto"],
                    default="auto")
    ap.add_argument("--model", help="override llm_config.json model")
    ap.add_argument("--db", help="override cinema.db path (tests)")
    ap.add_argument("--report", help="write per-file outcomes to this JSON "
                                   "file (survives later db changes)")
    args = ap.parse_args(argv)

    if args.db:
        cinema.CINEMA_DB = os.path.abspath(args.db)
    cinema.db_init()

    llm_cfg = llm_assist.load_config()
    if args.model:
        llm_cfg["model"] = args.model
    if not llm_assist.available(llm_cfg):
        print(f"LLM not available at {llm_cfg['endpoint']} "
              f"(model {llm_cfg['model']}); aborting.")
        return 2
    print(f"LLM: {llm_cfg['model']} @ {llm_cfg['endpoint']}")

    tcfg = cinema.load_config()
    api_key, token = tcfg.get("tmdbKey") or "", tcfg.get("tmdbToken") or ""
    if not (api_key or token):
        print("No TMDB credentials in cinema_config.json; aborting.")
        return 2
    tmdb_fetcher = cinema.make_tmdb_fetcher(token) if token \
        else cinema.tmdb_fetch
    gmaps = {}  # kind -> {genre_id: name}, fetched once up front
    _ensure_gmaps("movie", gmaps, api_key, tmdb_fetcher)
    _ensure_gmaps("tv", gmaps, api_key, tmdb_fetcher)

    con = sqlite3.connect(cinema.CINEMA_DB, timeout=30)
    try:
        if args.folder:
            cands = folder_candidates(args.folder, args.limit, args.offset)
            print(f"folder {args.folder}: {len(cands)} unidentified videos")
        else:
            cands = db_candidates(con, args.limit, args.offset)
            print(f"cinema.db: {len(cands)} unknown rows selected")
        if not cands:
            return 0
        if not args.dry_run:
            print("db backup:", backup_db())

        counts = {OUT_CRACKED: 0, OUT_REJECTED: 0, OUT_NOANSWER: 0}
        outcomes = []
        for i, cand in enumerate(cands, 1):
            hint = pick_hint(cand, args.hint)
            out = process_one(cand, hint, llm_cfg, api_key, tmdb_fetcher,
                              gmaps)
            counts[out["outcome"]] += 1
            line = f"[{i:2}/{len(cands)}] {out['file']}  -> {out['outcome']}"
            if out["crack"]:
                c = out["crack"]
                line += (f"\n      llm: {c['title']} ({c['year']}) "
                         f"{c['kind']} conf={c['confidence']:.2f}")
            if out["verified"]:
                v = out["verified"]
                line += (f"\n      VERIFIED: {v['title']} ({v['year']}) "
                         f"{v['kind']} [{v['genre']}/{v['subgenre']}]")
                if not args.dry_run:
                    apply_result(con, cand, v)
                    line += "  [written]"
            elif out["crack"]:
                line += "\n      tmdb: no confirming hit - rejected"
            print(line, flush=True)
            outcomes.append(out)

        if args.report:
            import json as _json
            with open(args.report, "w", encoding="utf-8") as f:
                _json.dump(outcomes, f, indent=1)
            print("report:", args.report)

        print(f"\nsummary: {counts[OUT_CRACKED]} cracked+verified, "
              f"{counts[OUT_REJECTED]} llm-guess-rejected, "
              f"{counts[OUT_NOANSWER]} llm-no-answer"
              + ("  (dry-run: nothing written)" if args.dry_run else ""))
        return 0
    finally:
        con.close()


def _ensure_gmaps(kind, gmaps, api_key, fetcher):
    if kind not in gmaps:
        try:
            gmaps[kind] = cinema._genre_map(kind, api_key, fetcher, None)
        except Exception:
            gmaps[kind] = {}


if __name__ == "__main__":
    sys.exit(main())
