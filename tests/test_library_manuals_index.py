"""Offline library: the PDF-manual index (SQLite FTS5 over PDF pages).

Real PDFs are generated with PyMuPDF and "read out of a ZIM" through stubs for
the three zimdump helpers, so indexing, search and page passages run for real
without kiwix tools or a library on disk.
"""
import pytest

from services.library import config, manuals

fitz = pytest.importorskip("fitz")


def _pdf(pages, title=""):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=11)
    if title:
        doc.set_metadata({"title": title})
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library folder with one fake ZIM holding three PDFs."""
    monkeypatch.setattr(config, "_settings", lambda: {
        "library_dir": str(tmp_path), "kiwix_public_url": "https://box.example.ts.net:8443",
    })
    zim_dir = tmp_path / "zim"
    zim_dir.mkdir()
    zim = zim_dir / "fieldmanuals_en_2026-01.zim"
    zim.write_bytes(b"not a real zim")
    entries = {
        "docs/ARN12086_ATP 3-50x21 FINAL WEB 2.pdf": _pdf([
            "ATP 3-50.21\nSurvival\nSEPTEMBER 2018",
            "Water procurement. Purify water by boiling for one minute or with iodine tablets.",
            "Shelters. A snow cave or a debris hut conserves body heat. Build the shelter before dark.",
        ], title="Chapter 1"),
        "docs/first aid.pdf": _pdf([
            "First Aid",
            "Control bleeding. Apply a tourniquet two to three inches above the wound and note the time.",
        ], title="First Aid"),
        "docs/scan.pdf": _pdf(["", ""], title="Scanned thing"),
        "docs/broken.pdf": b"<html>not a pdf</html>",
    }
    monkeypatch.setattr(manuals, "zim_pdf_count", lambda path: len(entries))
    monkeypatch.setattr(manuals, "zim_pdf_entries", lambda path: sorted(entries))
    monkeypatch.setattr(manuals, "read_zim_entry", lambda path, entry: entries[entry])

    # Worker processes would not see the stubs above; run the jobs inline.
    monkeypatch.setattr(manuals, "_extract_all",
                        lambda jobs, workers, pool_factory=None: (manuals._extract_job(job) for job in jobs))
    return {"zim": zim, "entries": entries, "dir": tmp_path}


def test_index_search_and_passage(library):
    summary = manuals.index_zim(library["zim"])
    assert summary["book"] == "fieldmanuals_en"
    assert summary["pdfs"] == 4 and summary["indexed"] == 3 and summary["failed"] == 1

    hits = manuals.search("purify water with iodine tablets")
    assert hits, "expected the survival manual page"
    top = hits[0]
    assert top["kind"] == "manual" and top["page"] == 2
    assert top["doc_title"] == "ATP 3-50.21 — Survival — SEPTEMBER 2018"
    assert top["title"].endswith("— page 2")
    assert "iodine" in top["snippet"].lower()
    assert top["url"] == ("https://box.example.ts.net:8443/content/fieldmanuals_en/"
                          "docs/ARN12086_ATP%203-50x21%20FINAL%20WEB%202.pdf#page=2")
    assert manuals.is_manual_url(top["url"])

    passage = manuals.passage(top["url"], budget=2000)
    assert passage["success"]
    assert "[page 2 of 3 — the matching page]" in passage["content"]
    assert "Purify water by boiling" in passage["content"]
    assert "[page 3 of 3]" in passage["content"]          # the following page rides along

    tourniquet = manuals.search("how do I apply a tourniquet")
    assert tourniquet and tourniquet[0]["doc_title"] == "First Aid"


def test_search_does_not_pass_off_weak_matches(library):
    manuals.index_zim(library["zim"])
    # "water" is in the index, the made-up word is not: a page with only the
    # common word must not come back as an answer.
    assert manuals.search("zzqqxx water") == []
    assert manuals.search("pacman keyring update") == []
    assert manuals.search("the and of") == []
    assert manuals.search("") == []


def test_stats_and_resume(library, monkeypatch):
    manuals.index_zim(library["zim"])
    stats = manuals.stats()
    assert stats["built"] and stats["documents"] == 4
    collection = stats["collections"][0]
    assert collection["book"] == "fieldmanuals_en" and collection["complete"]
    assert collection["scanned_documents"] == 1            # the two blank pages

    # Unchanged ZIM: nothing is read again.
    def fail(*args, **kwargs):
        raise AssertionError("an unchanged, complete collection must not be re-read")

    monkeypatch.setattr(manuals, "zim_pdf_entries", fail)
    again = manuals.index_zim(library["zim"])
    assert again["skipped"] == 4 and again["indexed"] == 0


def test_changed_zim_is_reindexed_and_removed_zim_is_pruned(library, monkeypatch):
    manuals.index_all()
    assert manuals.search("tourniquet")
    library["zim"].write_bytes(b"a newer edition of the same collection")      # size changes
    library["entries"].pop("docs/first aid.pdf")
    results = manuals.index_all()
    assert results[0]["indexed"] == 2
    assert manuals.search("tourniquet") == []

    library["zim"].unlink()
    manuals.index_all()
    assert manuals.stats()["documents"] == 0


def test_downloads_in_progress_are_left_alone(library):
    (library["dir"] / "zim" / "fieldmanuals_en_2026-01.zim.aria2").write_bytes(b"")
    assert manuals.index_all() == []


def test_missing_index_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_settings", lambda: {"library_dir": str(tmp_path)})
    assert manuals.search("anything") == []
    assert manuals.stats()["built"] is False
    out = manuals.passage("https://h/content/book/doc.pdf#page=2")
    assert not out["success"] and "not been built" in out["error"]


@pytest.mark.parametrize("name,expected", [
    ("ARN12086 ATP 3-50x21 FINAL WEB 2", "ATP 3-50.21"),
    ("ARN30234-ATP 3-21.50-000-WEB-3", "ATP 3-21.50"),
    ("NOCASE-TC 21-3-000-WEB-0", "TC 21-3"),
    ("tc3 97x61", "TC 3-97.61"),
    ("ARN19927 TC 3-22x9 C3 FINAL WEB", "TC 3-22.9"),
    ("tbmed577", "TB MED 577"),
    ("ARN16284 AR638-8 CC FINAL", "AR 638-8"),
    ("fm21-76-1", "FM 21-76-1"),
    ("STP 21-1-SMCT", "STP 21-1-SMCT"),
    ("Knots (6)", ""),
    ("ARN6745 DA FORM 7636 final", ""),
    ("sfhandbook-pt1", ""),
])
def test_publication_number_from_file_name(name, expected):
    assert manuals.pub_designation(name) == expected


def test_doc_title_prefers_real_titles_over_tool_junk():
    path = "x/ARN12086_ATP 3-50x21 FINAL WEB 2.pdf"
    first = "ATP 3-50.21\nSurvival\nSEPTEMBER 2018\nDISTRIBUTION RESTRICTION: Approved for public release"
    assert manuals.doc_title("Chapter 1", first, path) == "ATP 3-50.21 — Survival — SEPTEMBER 2018"
    assert manuals.doc_title("Microsoft Word - cover.doc", first, path) == "ATP 3-50.21 — Survival — SEPTEMBER 2018"
    assert manuals.doc_title("Ranger Handbook", "", "x/ARN3039_TC 3-21x76 FINAL WEB.pdf") == "TC 3-21.76 — Ranger Handbook"
    assert manuals.doc_title("First Aid", "", "irp.fas.org/doddir/milmed/first.pdf") == "First Aid"
    assert manuals.doc_title("", "", "files/Knots (7).pdf") == "Knots (7)"
    boiler = "This publication is available at the Army Publishing Directorate site\nTC 21-3\nSoldier's Handbook"
    assert manuals.doc_title("", boiler, "x/NOCASE-TC_21-3-000-WEB-0.pdf") == "TC 21-3 — Soldier's Handbook"


def test_a_pdf_that_crashes_the_reader_does_not_take_the_batch_with_it():
    """A malformed PDF can segfault the PDF library; the pool then fails every
    job in flight. Those are retried one process each: the culprit is recorded
    as unreadable, every other document is still indexed, order is kept."""
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    jobs = [("lib.zim", f"doc{i}.pdf", False) for i in range(30)]
    poison = "doc13.pdf"
    pools = []

    class FakePool:
        def __init__(self, workers):
            self.workers, self.broken = workers, False
            pools.append(self)

        def submit(self, fn, job):
            future = Future()
            if self.broken:
                future.set_exception(BrokenProcessPool("pool is broken"))
            elif job[1] == poison:
                self.broken = True                       # the worker died; the rest of this pool fails
                future.set_exception(BrokenProcessPool("a worker died"))
            else:
                future.set_result({"entry": job[1], "pages": ["text"], "title": job[1], "bytes": 1,
                                   "ocr_pages": 0, "error": ""})
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            pass

    results = list(manuals._extract_all(jobs, workers=2, pool_factory=FakePool))
    assert [r["entry"] for r in results] == [job[1] for job in jobs]
    bad = [r for r in results if r["error"]]
    assert [r["entry"] for r in bad] == [poison] and "crashed the PDF reader" in bad[0]["error"]
    assert all(r["pages"] == ["text"] for r in results if r["entry"] != poison)
    assert any(pool.workers == 1 for pool in pools)      # the retries ran in isolation


@pytest.mark.parametrize("title,paperwork", [
    ("GENERAL ORDERS — DISTINGUISHED UNIT CITATION", True),
    ("AR 638-8 — Casualty and Mortuary Affairs", True),
    ("DA PAM 750-30 — M16A1 Rifle", True),
    ("PISTOL SCORECARD (ARN18245 A7814 final re)", True),
    ("TC 3-22.9 — Rifle and Carbine — MAY 2016", False),
    ("ATP 3-50.21 — Survival — SEPTEMBER 2018", False),
    ("First Aid", False),
    ("Deadfalls and snares; a book of instruction for trappers", False),
])
def test_paperwork_is_ranked_behind_manuals(title, paperwork):
    assert manuals._is_paperwork(title) is paperwork


def test_sourced_collections_outrank_anonymous_ones_and_duplicates_collapse(tmp_path, monkeypatch):
    """The same leaflet sits in two survival packs, and an undated one says
    "two drops" where the current EPA sheet says eight. Search must return
    each document once and read the sourced collection first."""
    monkeypatch.setattr(config, "_settings", lambda: {"library_dir": str(tmp_path), "kiwix_public_url": "https://lib"})
    conn = manuals.connect()
    try:
        def add(doc_id, book, title, text):
            conn.execute("INSERT INTO docs (id, book, path, title, pages, text_pages) VALUES (?,?,?,?,1,1)",
                         (doc_id, book, f"{doc_id}.pdf", title))
            conn.execute("INSERT INTO pages (rowid, text, title) VALUES (?,?,?)",
                         (doc_id * manuals._PAGE_FACTOR + 1, text, title))

        leaflet = "Purifying water: add two drops of bleach per gallon. Bleach bleach drops gallon water."
        add(1, "zimgit-water_en", "Purifying Water During an Emergency", leaflet)
        add(2, "zimgit-post-disaster_en", "Purifying Water During an Emergency", leaflet)
        add(3, "aegis-manuals_en_all", "Emergency Disinfection of Drinking Water (EPA 2017)",
            "Add 8 drops of 6% bleach to each gallon of water; double it if the water is cloudy.")
        add(4, "armypubs_en_all", "GENERAL ORDERS — water bleach drops gallon", "bleach drops gallon water " * 5)
        conn.commit()
    finally:
        conn.close()

    hits = manuals.search("bleach drops gallon water")
    titles = [hit["doc_title"] for hit in hits]
    assert titles[0] == "Emergency Disinfection of Drinking Water (EPA 2017)"
    assert titles.count("Purifying Water During an Emergency") == 1          # once, not per collection
    assert titles[-1].startswith("GENERAL ORDERS")                           # paperwork last
    assert [manuals.collection_tier(b) for b in ("aegis-manuals_en_all", "armypubs_en_all", "zimgit-water_en", "")] == [0, 1, 2, 2]


def test_catalogued_titles_replace_junk_ones_without_touching_the_text(library, monkeypatch):
    manuals.index_zim(library["zim"])
    before = manuals.search("purify water iodine")[0]
    assert before["doc_title"].startswith("ATP 3-50.21")
    path = "docs/ARN12086_ATP 3-50x21 FINAL WEB 2.pdf"
    assert manuals.apply_titles("fieldmanuals_en", {path: "Survival, ATP 3-50.21 (US Army, 2018)", "missing.pdf": "x"}) == 1
    after = manuals.search("purify water iodine")[0]
    assert after["doc_title"] == "Survival, ATP 3-50.21 (US Army, 2018)" and after["page"] == before["page"]
    assert manuals.search("army 2018")[0]["doc_title"].startswith("Survival")       # the new title is searchable
    assert manuals.apply_titles("fieldmanuals_en", {path: "Survival, ATP 3-50.21 (US Army, 2018)"}) == 0
    assert manuals.apply_titles("fieldmanuals_en", {}) == 0


def test_a_collections_own_catalogue_is_applied_after_indexing(library, monkeypatch):
    import json as _json
    library["entries"]["titles.json"] = _json.dumps({"docs/first aid.pdf": "First Aid, TC 4-02.1 (US Army, 2016)"}).encode()
    monkeypatch.setattr(manuals, "zim_pdf_entries", lambda path: sorted(e for e in library["entries"] if e.endswith(".pdf")))
    manuals.index_zim(library["zim"])
    assert manuals.search("tourniquet")[0]["doc_title"] == "First Aid, TC 4-02.1 (US Army, 2016)"
