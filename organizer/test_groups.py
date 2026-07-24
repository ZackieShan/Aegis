#!/usr/bin/env python3
r"""compute_groups correctness + performance test.

Builds 2,000 tiny JPEGs with planted dupe clusters (250 exact-copy pairs,
250 resized pairs, 1,000 singletons), then checks the banded compute_groups
returns EXACTLY the same clusters as the old O(n^2) all-pairs algorithm,
and times both.
"""
import os
import random
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import server  # noqa: E402
from PIL import Image  # noqa: E402

BENCH = os.path.join(BASE, "fixture_bench")
N_EXACT_PAIRS = 250
N_RESIZED_PAIRS = 250
N_SINGLETONS = 1000


def make_pattern(seed, size):
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 8))
    px = small.load()
    for y in range(8):
        for x in range(8):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return small.resize(size, Image.NEAREST)


def build_fixtures():
    if os.path.isdir(BENCH):
        shutil.rmtree(BENCH)
    os.makedirs(BENCH)
    files = []
    for i in range(N_EXACT_PAIRS):
        img = make_pattern(i, (48, 36))
        a = os.path.join(BENCH, f"exact_{i:03d}.jpg")
        img.save(a, quality=90)
        b = os.path.join(BENCH, f"exact_{i:03d}_copy.jpg")
        shutil.copyfile(a, b)
        files += [a, b]
    for i in range(N_RESIZED_PAIRS):
        img = make_pattern(1000 + i, (48, 36))
        a = os.path.join(BENCH, f"res_{i:03d}.jpg")
        img.save(a, quality=90)
        b = os.path.join(BENCH, f"res_{i:03d}_small.jpg")
        img.resize((24, 18), Image.LANCZOS).save(b, quality=60)
        files += [a, b]
    for i in range(N_SINGLETONS):
        img = make_pattern(5000 + i, (48, 36))
        a = os.path.join(BENCH, f"uniq_{i:04d}.jpg")
        img.save(a, quality=90)
        files.append(a)
    return files


def brute_force_groups(photos):
    """The old O(n^2) algorithm (hex-string hashes, int() per comparison)."""
    n = len(photos)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_md5 = {}
    for i, p in enumerate(photos):
        if p.get("md5"):
            if p["md5"] in by_md5:
                union(i, by_md5[p["md5"]])
            else:
                by_md5[p["md5"]] = i
    hashed = [(i, p["ahash"]) for i, p in enumerate(photos) if p.get("ahash")]
    for a in range(len(hashed)):
        ia, ha = hashed[a]
        for b in range(a + 1, len(hashed)):
            ib, hb = hashed[b]
            if (int(ha, 16) ^ int(hb, 16)).bit_count() <= 6:
                union(ia, ib)
    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return {frozenset(photos[i]["path"] for i in v) for v in clusters.values() if len(v) > 1}


def as_clusters(groups):
    by_gid = {}
    for path, gid in groups.items():
        by_gid.setdefault(gid, set()).add(path)
    return {frozenset(v) for v in by_gid.values()}


def main():
    print("building 2,000 bench fixtures...")
    files = build_fixtures()
    print(f"built {len(files)} files; extracting hashes...")
    photos = []
    for p in files:
        with Image.open(p) as im:
            ah = format(server.ahash_image(im), "016x")
        photos.append({"path": p, "md5": server.md5_file(p), "ahash": ah})

    t0 = time.perf_counter()
    new_groups = server.compute_groups(photos)
    t_new = time.perf_counter() - t0

    t0 = time.perf_counter()
    old_clusters = brute_force_groups(photos)
    t_old = time.perf_counter() - t0

    new_clusters = as_clusters(new_groups)
    print(f"\nnew banded: {t_new:.2f}s, {len(new_clusters)} clusters")
    print(f"old O(n^2): {t_old:.2f}s, {len(old_clusters)} clusters")
    print(f"speedup: {t_old / max(t_new, 1e-9):.1f}x")

    ok = True
    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
        ok = ok and cond

    check("identical clusters to O(n^2)", new_clusters == old_clusters,
          f"new={len(new_clusters)} old={len(old_clusters)}")
    check("500 planted clusters found", len(new_clusters) == 500, str(len(new_clusters)))
    exact_pairs = [c for c in new_clusters
                   if any("exact_" in os.path.basename(x) and "_copy" not in x for x in c)]
    check("all cluster sizes are 2", all(len(c) == 2 for c in new_clusters))
    # every exact-copy pair must be grouped
    exact_ok = sum(1 for i in range(N_EXACT_PAIRS)
                   if frozenset({os.path.join(BENCH, f"exact_{i:03d}.jpg"),
                                 os.path.join(BENCH, f"exact_{i:03d}_copy.jpg")}) in new_clusters)
    check("all 250 exact pairs grouped", exact_ok == N_EXACT_PAIRS, str(exact_ok))
    res_ok = sum(1 for i in range(N_RESIZED_PAIRS)
                 if frozenset({os.path.join(BENCH, f"res_{i:03d}.jpg"),
                               os.path.join(BENCH, f"res_{i:03d}_small.jpg")}) in new_clusters)
    check("resized pairs grouped", res_ok >= N_RESIZED_PAIRS * 0.95,
          f"{res_ok}/{N_RESIZED_PAIRS}")
    check("runtime well under O(n^2)", t_new < max(t_old / 4, 0.001) and t_new < 30,
          f"{t_new:.2f}s vs {t_old:.2f}s")

    # correctness on the 17 standard fixtures too
    fix = os.path.join(BASE, "fixture_photos")
    fphotos = []
    for fn in sorted(os.listdir(fix)):
        p = os.path.join(fix, fn)
        if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        with Image.open(p) as im:
            ah = format(server.ahash_image(im), "016x")
        fphotos.append({"path": p, "md5": server.md5_file(p), "ahash": ah})
    fg = as_clusters(server.compute_groups(fphotos))
    trio = frozenset(os.path.join(fix, n) for n in
                     ("master.jpg", "master_exact_copy.jpg", "master_resized.jpg"))
    check("fixtures: master trio grouped", trio in fg, str([len(c) for c in fg]))

    shutil.rmtree(BENCH, ignore_errors=True)
    print(f"\n==== {'ALL PASS' if ok else 'FAILURES'} ====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
