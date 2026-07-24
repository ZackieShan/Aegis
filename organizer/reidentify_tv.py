#!/usr/bin/env python3
"""Re-identify TV rows in the production cinema.db after the TV fixes.

Row-based maintenance tool. It reads EXISTING media rows, re-parses every
filename with the current (fixed) parser, and repairs rows the old scheme
got wrong:

  * kind='unknown' rows whose names now match a TV pattern (SxxEyy / 1x02 /
    SE1 EP24 / S2 - 05 / Session 05 / Episode 52 / ep01 / .E05. / anime
    absolute " - 05")  ->  kind='tv' + year-less TMDB series identification
  * kind='movie' rows that were really TV episodes (same patterns hit)
    ->  movie-vs-TV flip to kind='tv'
  * kind='movie' rows whose episode evidence has no series in the name
    (episode-led "19 - Daddy Queerest.avi")  ->  back to kind='unknown',
    dropping the bogus yearless movie identification

READ-ONLY on media files. It updates cinema.db rows ONLY - it never plans,
executes, or moves anything. TMDB /search/tv lookups are similarity-gated,
year-less, throttled to ~4/s by cinema, and cached in ident_cache /
genre_cache, so re-runs are cheap.

Usage:
    python reidentify_tv.py [--db PATH] [--root DISPLAY_ROOT] [--limit N]
"""
import argparse
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cinema  # noqa: E402

BACKUP = os.path.join(BASE, "db_safety_backup", "cinema_prereident_tv.db")
DEFAULT_ROOT = r"F:\TV\Organized"


def backup_db(db_path):
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(BACKUP)
    src.backup(dst)
    dst.close()
    src.close()
    return BACKUP


def kind_counts(con):
    return dict(con.execute("SELECT kind, COUNT(*) FROM media"
                            " GROUP BY kind").fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=cinema.CINEMA_DB,
                    help="cinema DB to repair (default: production cinema.db)")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="display root for destination previews")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N candidate rows (debugging)")
    args = ap.parse_args()

    cinema.CINEMA_DB = args.db
    cinema.db_init()  # ensures ident_cache / genre_cache exist
    cfg = cinema.load_config()
    api_key = cfg.get("tmdbKey") or ""
    token = cfg.get("tmdbToken") or ""
    if not (api_key or token):
        print("WARNING: no TMDB credentials - series will be classified "
              "from filenames only (genre Unclassified)")

    bak = backup_db(args.db)
    print(f"backup: {bak}")

    # autocommit: each row UPDATE is its own short transaction so cinema's
    # cache helpers (ident_cache/genre_cache, separate connections) never
    # hit a long-held write lock
    con = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    before = kind_counts(con)
    print("before:", before)

    rows = con.execute(
        "SELECT id, path, filename, kind, title, year, season, episode,"
        " season_pack, genre, subgenre FROM media"
        " WHERE kind IN ('unknown', 'movie') ORDER BY filename").fetchall()
    print(f"candidate rows (unknown + movie): {len(rows)}")

    flips_tv = []        # rows flipped/confirmed to tv
    flips_unknown = []   # movie -> unknown (bogus movie dropped)
    skipped = 0
    t0 = time.time()
    tmdb_series = 0      # distinct series that needed a live TMDB call

    for i, row in enumerate(rows):
        if args.limit and i >= args.limit:
            break
        name = row["filename"]
        p = cinema.parse_media_name(name)
        if p["kind"] == "tv" and p.get("title"):
            # (re)identify the series via year-less /search/tv (cached)
            tv_key = "tv: " + p["title"]
            ic = cinema.ident_cache_get(tv_key)
            if ic is None and (api_key or token):
                tmdb_series += 1
                ct, yr, g, sg, src = cinema.identify_tv(
                    p["title"], p.get("year"), api_key, token=token)
                cinema.ident_cache_put(tv_key, ct, yr, g, sg, src)
                ic = (ct, yr, g, sg, src)
            title, year = p["title"], p.get("year")
            if ic and ic[4] == "tmdb" and ic[0]:
                title = ic[0]
                if ic[1]:
                    year = ic[1]
                genre = ic[2] or "Unclassified"
                sub = ic[3] or "General"
                gsrc = "tmdb"
                cinema.genre_cache_put("tv", title, year, genre, sub, "tmdb")
            else:
                genre, sub, gsrc = "Unclassified", "General", "none"
            con.execute(
                "UPDATE media SET kind='tv', title=?, year=?, season=?,"
                " episode=?, season_pack=?, genre=?, subgenre=?,"
                " genre_source=?, dupe_group=NULL WHERE id=?",
                (title, year, p.get("season"), p.get("episode"),
                 1 if p.get("season_pack") else 0, genre, sub, gsrc,
                 row["id"]))
            flips_tv.append({
                "name": name, "from_kind": row["kind"],
                "title": title, "year": year, "season": p.get("season"),
                "episode": p.get("episode"), "episodes": p.get("episodes"),
                "season_pack": bool(p.get("season_pack")),
                "genre": genre, "subgenre": sub, "ext": os.path.splitext(name)[1].lower(),
                "was": (row["kind"], row["title"], row["year"]),
            })
        elif p["kind"] == "unknown" and not p.get("guess_title") \
                and row["kind"] == "movie":
            # episode evidence but no series anywhere -> the old yearless
            # movie identification was bogus; drop it
            con.execute(
                "UPDATE media SET kind='unknown', title=NULL, year=NULL,"
                " season=NULL, episode=NULL, season_pack=0, genre=NULL,"
                " subgenre=NULL, genre_source=NULL, dupe_group=NULL"
                " WHERE id=?", (row["id"],))
            flips_unknown.append({"name": name, "was_title": row["title"],
                                  "was_year": row["year"]})
        else:
            skipped += 1
        if (i + 1) % 250 == 0:
            con.commit()
            print(f"  [{time.time()-t0:6.0f}s] {i+1}/{len(rows)}"
                  f"  ->tv {len(flips_tv)}  ->unknown {len(flips_unknown)}",
                  flush=True)
    con.commit()

    after = kind_counts(con)
    print(f"\ndone in {time.time()-t0:.0f}s | live TMDB series lookups:"
          f" {tmdb_series} (cached afterwards)")
    print("after: ", after)
    print(f"flipped to tv: {len(flips_tv)}"
          f" (from unknown {sum(1 for f in flips_tv if f['from_kind'] == 'unknown')},"
          f" from movie {sum(1 for f in flips_tv if f['from_kind'] == 'movie')})")
    print(f"bogus movies dropped to unknown: {len(flips_unknown)}")
    print(f"unchanged: {skipped}")

    # ---- examples: prefer Cowboy Bebop / Pokemon, one per series ----
    def rank(f):
        n = f["name"].lower()
        if "bebop" in n:
            return 0
        if "pokémon" in n or "pokemon" in n:
            return 1
        return 2
    by_series = {}
    for f in sorted(flips_tv, key=rank):
        by_series.setdefault(f["title"], f)
    examples = sorted(by_series.values(),
                      key=lambda f: (rank(f), f["title"].lower()))[:10]
    print("\nexamples (file -> destination under the new scheme):")
    for f in examples:
        rec = {"title": f["title"], "year": f["year"],
               "season": f["season"] or 1, "episode": f["episode"],
               "episodes": f["episodes"], "season_pack": f["season_pack"],
               "genre": f["genre"], "subgenre": f["subgenre"],
               "ext": f["ext"], "name": f["name"]}
        dest = cinema.tv_dest(rec, args.root)
        was = f" [was {f['was'][0]}: {f['was'][1]!r}]" \
            if f["from_kind"] == "movie" else ""
        print(f"  {f['name']}")
        print(f"    -> {dest}{was}")
    if flips_unknown:
        print("\nsample bogus-movie drops (now unknown again):")
        for f in flips_unknown[:5]:
            print(f"  {f['name']}  [was movie: {f['was_title']!r}"
                  f" ({f['was_year']})]")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
