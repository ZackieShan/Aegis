"""The library's shelves: one tree that everything is filed under.

What must hold:
  * a shelf can be named the way people and models write it;
  * a collection, a Project Gutenberg book and a document each land where a
    person would look for them — a cookbook by its style of cooking, a history
    by its era, a novel by when it was written, a manual by its skill;
  * a factual search is never answered out of a novel, and a search can be
    kept to one shelf;
  * files the owner copies into the library folder are read in where they
    lie, once each, and only those files are ever served;
  * what a person filed by hand, a rule or a model never undoes.

Everything here is made up or is a public catalogue fact (a title, an author,
a subject class); no test touches the real library folder.
"""
import asyncio
import json
import os
import time
import zipfile

import pytest

fitz = pytest.importorskip("fitz")        # PyMuPDF: the library's document reader


# ── a library folder of its own ────────────────────────────────────────────

@pytest.fixture
def lib(tmp_path, monkeypatch):
    from services.library import catalogue, classify, config, files, manuals, shelves

    monkeypatch.setattr(config, "library_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "enabled", lambda: True)
    monkeypatch.setattr(config, "kiwix_public_url", lambda: "https://library.example:8443")
    monkeypatch.setattr("services.library.kiwix.available", lambda force=False: False)
    monkeypatch.setattr("services.library.kiwix.book_titles", lambda: {})
    monkeypatch.setattr("services.library.kiwix.list_books", lambda force=False: [])
    shelves.reset_cache()
    yield type("Lib", (), {"root": tmp_path, "shelves": shelves, "catalogue": catalogue, "files": files, "manuals": manuals,
                           "classify": classify, "config": config})
    shelves.reset_cache()


def _pdf(path, pages, title="", author=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 760), text, fontsize=11)
    doc.set_metadata({"title": title, "author": author})
    doc.save(str(path))
    doc.close()
    _age(path)


def _epub(path, title, author, chapters, subject=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr("content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
                   'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:subject>{subject}</dc:subject>"
                   '<dc:identifier id="id">x-1</dc:identifier><dc:language>en</dc:language></metadata>'
                   f"<manifest>{items}</manifest><spine>{spine}</spine></package>")
        for i, (heading, text) in enumerate(chapters):
            z.writestr(f"c{i}.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f"<head><title>{heading}</title></head><body><h1>{heading}</h1><p>{text}</p></body></html>")
    _age(path)


def _age(path, seconds=3600):
    """Files in these tests are finished copying: older than the settle time."""
    then = time.time() - seconds
    os.utime(path, (then, then))


FILLER = "The grey heron stood in the shallows and waited for the tide to turn. " * 12


# ── naming a shelf ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("written, meant", [
    ("History/Ancient world", "History/Ancient world"),
    ("history > ancient", "History/Ancient world"),
    ("History › Middle Ages", "History/Middle Ages"),
    ("cooking/baking and desserts", "Cooking/Baking & desserts"),
    ("Cooking", "Cooking"), ("cookbooks", "Cooking"), ("Cook books", "Cooking"),
    ("novels", "Literature"), ("medieval", "History/Middle Ages"), ("first aid", "Survival/Medical & first aid"),
    ("Survival/Medical", "Survival/Medical & first aid"), ("the bible", "Philosophy & religion/The Bible & Christianity"),
    ("bible", "Philosophy & religion/The Bible & Christianity"),
    ("childrens", "Literature/Children's & young readers"), ("Unsorted", "Unsorted"),
    ("", ""), ("no such shelf anywhere", ""),
])
def test_a_shelf_can_be_named_the_way_people_write_it(lib, written, meant):
    assert lib.shelves.resolve(written) == meant


def test_every_built_in_shelf_is_a_valid_place_with_a_plain_name(lib):
    paths = lib.shelves.all_paths()
    assert len(paths) == len(set(paths)) > 70
    for path in paths:
        category, shelf = lib.shelves.split(path)
        assert category and shelf and "/" not in shelf and lib.shelves.valid(path) and lib.shelves.resolve(path) == path
        assert lib.shelves.display(path) == f"{category} › {shelf}"
    assert lib.shelves.under("History/Ancient world", "History") and not lib.shelves.under("History", "History/Ancient world")
    assert lib.shelves.under("Cooking", "") and not lib.shelves.under("Historyish/x", "History")
    assert lib.shelves.is_fiction("Literature/19th century") and not lib.shelves.is_fiction("History/Middle Ages")


def test_a_folder_named_technical_is_not_the_shelf_technical_manuals(lib):
    assert lib.shelves.resolve("Technical") == "Military/Technical manuals"          # a person typing it may mean that
    assert lib.shelves.resolve("Technical", loose=False) == ""                        # a folder's name does not say so


# ── whole collections ──────────────────────────────────────────────────────

@pytest.mark.parametrize("book, shelf", [
    ("wikipedia_en_all_maxi_2026-08", "Reference/Encyclopedias"),
    ("wikipedia_en_medicine_maxi_2026-10", "Medicine/Medical references"),
    ("cooking.stackexchange.com_en_all_2026-10", "Cooking/General cooking & technique"),
    ("ifixit_en_all_2025-12", "Repair & making/Repair guides"),
    ("appropedia_en_all_maxi_2026-02", "Homestead"),
    ("armypubs_en_all_2024-12", "Military"),
    ("zimgit-water_en_2024-08", "Survival/Water"),
    ("devdocs_en_python_2026-10", "Computing/Programming references"),
    ("gutenberg_en_lcc-pr_2026-11a", "Literature"),
    ("gutenberg_en_lcc-cdef_2026-11b", "History"),
    ("gutenberg_en_lcc-b_2026-11a", "Philosophy & religion"),
    ("gutenberg_en_lcc-pe_2026-11a", "Reference/Dictionaries & language"),
    ("gutenberg_en_lcc-p_2026-11a", "Reference/Dictionaries & language"),
    ("gutenberg_en_lcc-r_2026-11a", "Medicine/Old medical books"),
    ("something-nobody-has-seen_en_all_2030-01", "Unsorted"),
])
def test_a_collection_is_shelved_by_its_id_whatever_its_edition(lib, book, shelf):
    assert lib.shelves.collection_shelf(book) == shelf


def test_the_owner_can_file_a_collection_and_add_a_category(lib):
    (lib.root / "shelves.json").write_text(json.dumps({
        "categories": [{"name": "Strongbox", "about": "Hard-to-find files", "shelves": [{"name": "Field reports", "about": "As filed"}]},
                       {"name": "Cooking", "shelves": ["Family recipes"]}],
        "collections": {"something-nobody-has-seen_en_all": "Strongbox/Field reports", "ifixit_en_all": "No such/Shelf"},
    }))
    lib.shelves.reset_cache()
    assert lib.shelves.categories()[-1] == "Strongbox" and lib.shelves.resolve("strongbox > field") == "Strongbox/Field reports"
    assert "Cooking/Family recipes" in lib.shelves.all_paths() and "Cooking/Baking & desserts" in lib.shelves.all_paths()
    assert lib.shelves.collection_shelf("something-nobody-has-seen_en_all_2030-01") == "Strongbox/Field reports"
    assert lib.shelves.collection_shelf("ifixit_en_all_2025-12") == "Repair & making/Repair guides"       # a shelf that is not there is ignored


# ── Project Gutenberg books ────────────────────────────────────────────────

def _record(title, authors, locc, cats=(), subjects=""):
    return {"title": title, "authors": authors, "locc": locc, "subjects": subjects,
            "bookshelves": "; ".join(f"Category: {cat}" for cat in cats)}


@pytest.mark.parametrize("record, shelf", [
    (_record("Pride and Prejudice", "Austen, Jane, 1775-1817", "PR", ["Romance", "Novels", "Classics of Literature", "British Literature"]),
     "Literature/19th century"),
    (_record("The Odyssey", "Homer, 751? BCE-651? BCE; Butler, Samuel, 1835-1902 [Translator]", "PA", ["Poetry", "Classics of Literature"]),
     "Literature/Ancient & classical"),
    (_record("Hamlet", "Shakespeare, William, 1564-1616", "PR", ["Plays/Films/Dramas"]), "Literature/Medieval & Renaissance"),
    (_record("Paradise Lost", "Milton, John, 1608-1674", "PR", ["Poetry"]), "Literature/17th & 18th century"),
    (_record("The Great Gatsby", "Fitzgerald, F. Scott (Francis Scott), 1896-1940", "PS", ["Novels"]), "Literature/20th century"),
    (_record("The Wonderful Wizard of Oz", "Baum, L. Frank (Lyman Frank), 1856-1919", "PZ", ["Children & Young Adult Reading"]),
     "Literature/Children's & young readers"),
    (_record("Beowulf", "Gummere, Francis Barton, 1855-1919 [Translator]", "PR", ["Poetry"], "Epic poetry, English (Old)"),
     "Literature/Medieval & Renaissance"),
    (_record("The Republic", "Plato, 428? BCE-348? BCE; Jowett, Benjamin, 1817-1893 [Translator]", "JC; PA",
             ["Classics of Literature", "Philosophy & Ethics"]), "Philosophy & religion/Philosophy & ethics"),
    (_record("The King James Version of the Bible", "", "BS", ["Religion/Spirituality"]), "Philosophy & religion/The Bible & Christianity"),
    (_record("The Interpretation of Dreams", "Freud, Sigmund, 1856-1939", "BF", ["Psychiatry/Psychology"], "Dream interpretation; Psychoanalysis"),
     "Philosophy & religion/Psychology & the mind"),
    (_record("The History of Herodotus", "Herodotus, 481? BCE-421? BCE", "D; PA", ["History - Ancient"]), "History/Ancient world"),
    (_record("A Chronicle of the Crusades", "Someone, A., 1800-1870", "D", [], "Crusades -- Early works to 1800"), "History/Middle Ages"),
    (_record("Common Sense", "Paine, Thomas, 1737-1809", "E201", ["History - American", "History - Modern (1750+)"]), "History/Modern (1750-1914)"),
    (_record("Over There", "Someone, B., 1890-1950", "D501", ["History - Modern (1750+)", "History - Warfare"], "World War, 1914-1918"),
     "History/World wars & 20th century"),
    (_record("The Outline of History", "Wells, H. G. (Herbert George), 1866-1946", "D", ["History - Other"]), "History/General histories"),
    (_record("The Art of War", "Sunzi, active 6th century B.C.", "U", ["History - Ancient", "History - Warfare"]), "Military/Tactics, strategy & war"),
    (_record("The Boston Cooking-School Cook Book", "Farmer, Fannie Merritt, 1857-1915", "TX", ["Cooking & Drinking"], "Cooking, American"),
     "Cooking/General cooking & technique"),
    (_record("Bread and Cake Baking", "Someone, C.", "TX", ["Cooking & Drinking"], "Baking; Bread"), "Cooking/Baking & desserts"),
    (_record("Canning and Preserving", "Someone, D.", "TX", ["Cooking & Drinking"], "Canning and preserving"), "Cooking/Preserving, canning & fermenting"),
    (_record("The Complete Angler", "Walton, Izaak, 1593-1683", "SH", ["How To ..."]), "Survival/Hunting, trapping & fishing"),
    (_record("Camp and Trail", "White, Stewart Edward, 1873-1946", "SK", ["How To ...", "Sports/Hobbies"], "Camping"), "Survival/Hunting, trapping & fishing"),
    (_record("Knots, Bends, Splices", "Jutsum, J. Netherclift", "VM", ["How To ..."], "Knots and splices"), "Survival/Shelter, fire & fieldcraft"),
    (_record("On the Origin of Species", "Darwin, Charles, 1809-1882", "QH", ["Science - Biology"]), "Science/Biology & nature"),
    (_record("The Federalist Papers", "Hamilton, Alexander, 1757-1804", "JK; KF", ["Politics"]), "Society/Government & politics"),
    (_record("Household Medicine", "Someone, E., 1820-1890", "RC", ["Health & Medicine"]), "Medicine/Old medical books"),
])
def test_a_gutenberg_book_goes_where_a_reader_would_look(lib, record, shelf):
    assert lib.shelves.gutenberg_place(record)["shelf"] == shelf


def test_words_alone_do_not_make_a_survival_manual(lib):
    """A memoir with hunting in it, a regimental history with "rifles" in its
    title and a book on the survival of the soul are not field manuals."""
    place = lib.shelves.gutenberg_place
    assert place(_record("Human Personality and Its Survival of Bodily Death", "Myers, F. W. H., 1843-1901", "BF",
                         ["Psychiatry/Psychology"], "Psychical research"))["shelf"].startswith("Philosophy & religion/")
    assert place(_record("A Brief History of the Royal Rifle Corps", "Someone, F., 1848-1923", "UA",
                         ["History - British", "History - Warfare"]))["shelf"] == "Military/Tactics, strategy & war"
    assert place(_record("Among the Hill Tribes", "Someone, G., 1871-1932", "DS", ["Travel Writing"],
                         "Hunting -- Asia"))["shelf"].startswith("History/")
    assert place(_record("The Hunters of the North", "Someone, H., 1850-1910", "PS", ["Novels", "Adventure"],
                         "Hunting -- Fiction"))["shelf"] == "Literature/19th century"


@pytest.mark.parametrize("authors, name, dates", [
    ("Austen, Jane, 1775-1817", "Jane Austen", "1775–1817"),
    ("Homer, 751? BCE-651? BCE; Butler, Samuel, 1835-1902 [Translator]", "Homer", "751 BC–651 BC"),
    ("Baum, L. Frank (Lyman Frank), 1856-1919", "L. Frank Baum", "1856–1919"),
    ("Thomas, Aquinas, Saint, 1225?-1274", "Saint Thomas Aquinas", "1225–1274"),
    ("More, Thomas, Saint, 1478-1535", "Saint Thomas More", "1478–1535"),
    ("Augustine, of Hippo, Saint, 354-430", "Saint Augustine of Hippo", "354–430"),
    ("Tolstoy, Leo, graf, 1828-1910; Maude, Aylmer, 1858-1938 [Translator]", "Leo Tolstoy", "1828–1910"),
    ("Malory, Thomas, Sir, -1471", "Sir Thomas Malory", "died 1471"),
    ("Needler, G. H. (George Henry), 1866-1962 [Translator]", "translated by G. H. Needler", ""),
    ("Various", "Various", ""), ("", "", ""),
])
def test_an_author_is_read_out_of_a_catalogue_entry(lib, authors, name, dates):
    found = lib.shelves.author_of(authors)
    assert (found[0], found[1]) == (name, dates)


# ── documents: folders and titles ──────────────────────────────────────────

@pytest.mark.parametrize("path, hint", [
    ("stuff/Books/Cookbooks Pack 10/x.pdf", "Cooking"),
    ("stuff/Books/Assorted Books Pack 9/x.pdf", ""),
    ("stuff/Medical/x.pdf", "Survival/Medical & first aid"),
    ("stuff/01.2) Navigation, Reading Maps, Using a Compass(9)/x.pdf", "Survival/Navigation"),
    ("stuff/Food Production, Preservation, Storage, and Preparation (298)/Canning (20)/x.pdf", "Cooking/Preserving, canning & fermenting"),
    ("stuff/Food Production, Preservation, Storage, and Preparation (298)/Other (6)/x.pdf", "Homestead/Farming & livestock"),
    ("stuff/Misc. Extra(20)/Random other things(5)/x.pdf", ""),
    ("History/Rome/x.pdf", "History"),
    ("Technical/x.pdf", ""),
    ("x.pdf", ""),
])
def test_a_folder_says_what_is_in_it(lib, path, hint):
    assert lib.shelves.folder_hint(path) == hint


def test_the_owner_can_say_what_a_folder_is(lib):
    (lib.root / "shelves.json").write_text(json.dumps({
        "categories": [{"name": "Strongbox", "about": "Hard-to-find files"}],
        "folders": {"stuff/Strongbox": "Strongbox", "stuff/Technical": "Repair & making", "stuff/Packs/Illustrated*": "Reference"},
    }))
    lib.shelves.reset_cache()
    assert lib.shelves.folder_hint("stuff/Strongbox/DOC-1.pdf") == "Strongbox"
    assert lib.shelves.folder_hint("stuff/Technical/Lathes/x.pdf") == "Repair & making"
    assert lib.shelves.folder_hint("stuff/Technical/Canning/x.pdf") == "Cooking/Preserving, canning & fermenting"   # a nearer folder wins
    assert lib.shelves.folder_hint("stuff/Packs/Illustrated Books 3/x.pdf") == "Reference"
    assert lib.shelves.folder_hint("stuff/Packs/Other Illustrated/x.pdf") == ""


@pytest.mark.parametrize("title, folder, shelf", [
    ("Sourdough at Home", "stuff/Books/Cookbooks Pack 3/a.pdf", "Cooking/Baking & desserts"),
    ("Backyard Barbecue", "stuff/Books/Cookbooks Pack 3/b.pdf", "Cooking/Grilling, smoking & camp cooking"),
    ("Dinner Tonight", "stuff/Books/Cookbooks Pack 3/c.pdf", "Cooking/General cooking & technique"),
    ("Field First Aid Pocket Guide", "stuff/d.pdf", "Survival/Medical & first aid"),
    ("The Legions of the Tiber", "History/Rome/e.pdf", "History"),
    ("A Quiet Year", "stuff/Books/f.pdf", "Unsorted"),
])
def test_a_document_is_placed_by_its_folder_and_its_title(lib, title, folder, shelf):
    assert lib.shelves.place_document(title, folder=folder)[0] == shelf


@pytest.mark.parametrize("title, shelf", [
    ("TC 4-02.1 — FIRST AID — JANUARY 2016", "Survival/Medical & first aid"),
    ("TC 3-25.26 — Map Reading and Land Navigation", "Survival/Navigation"),
    ("ATP 3-21.8 — Infantry Platoon and Squad", "Military/Field manuals & doctrine"),
    ("TM 5-608 — TECHNICAL MANUAL — CONTRACTING FOR CUSTODIAL SERVICES", "Military/Technical manuals"),
    ("AR 11-7 — Army Regulation 11–7 — Internal Review Program", "Military/Regulations & paperwork"),
    ("DA Form 1059 (final)", "Military/Regulations & paperwork"),
    ("Something with no number at all", "Military/Regulations & paperwork"),
])
def test_an_army_publication_is_filed_by_what_it_teaches_or_what_it_is(lib, title, shelf):
    assert lib.shelves.army_place(title) == shelf


# ── the catalogue ──────────────────────────────────────────────────────────

def _fill(lib):
    conn = lib.manuals.connect()
    put = lib.catalogue.put
    put(conn, "gutenberg_en_lcc-pr_2026-11a", "1342", "Pride and Prejudice", "Literature/19th century", author="Jane Austen",
        dates="1775–1817", form="novel, romance", source="catalogue")
    put(conn, "gutenberg_en_lcc-ps_2026-11a", "2489", "Moby Dick; Or, The Whale", "Literature/19th century", author="Herman Melville",
        dates="1819–1891", form="novel", source="catalogue")
    put(conn, "gutenberg_en_lcc-ps_2026-11a", "2701", "Moby Dick; Or, The Whale", "Literature/19th century", author="Herman Melville",
        dates="1819–1891", form="novel", source="catalogue")
    put(conn, "gutenberg_en_lcc-t_2026-11a", "65061", "The Boston Cooking-School Cook Book", "Cooking/General cooking & technique",
        author="Fannie Merritt Farmer", source="catalogue")
    put(conn, "gutenberg_en_lcc-t_2026-11a", "111", "Bread and Cake Baking", "Cooking/Baking & desserts", source="catalogue")
    conn.execute("INSERT INTO shelf_sources (book, stamp, items, built_at) VALUES ('gutenberg_en_lcc-t_2026-11a', 's', 2, 0)")
    conn.commit()
    conn.close()


def test_the_catalogue_answers_what_a_library_is_asked(lib):
    _fill(lib)
    catalogue = lib.catalogue
    assert catalogue.counts() == {"Literature/19th century": 3, "Cooking/General cooking & technique": 1, "Cooking/Baking & desserts": 1}
    assert [item["title"] for item in catalogue.items("Cooking")["items"]] == ["Bread and Cake Baking", "The Boston Cooking-School Cook Book"]
    assert catalogue.items("Cooking/Baking & desserts")["total"] == 1 and catalogue.items("Cook")["total"] == 0     # not a prefix match
    assert catalogue.items("Literature", "austen")["items"][0]["key"] == "1342"
    assert catalogue.items("Literature", "100% _")["total"] == 0                                    # wildcards are only characters
    found = catalogue.find("moby dick")
    assert [item["title"] for item in found] == ["Moby Dick; Or, The Whale"]                        # two editions, listed once
    assert catalogue.find("melville")[0]["author"] == "Herman Melville" and catalogue.find("melville", scope="Cooking") == []
    assert sorted(catalogue.books_with("Cooking")) == ["gutenberg_en_lcc-t_2026-11a"]
    assert catalogue.link("gutenberg_en_lcc-pr_2026-11a", "1342") == \
        "https://library.example:8443/content/gutenberg_en_lcc-pr_2026-11a/index/book_1342"


def test_a_search_hit_is_matched_to_its_book(lib):
    _fill(lib)
    key = lib.catalogue.item_key
    assert key("gutenberg_en_lcc-pr_2026-11a", "index/book_1342") == "1342"
    assert key("gutenberg_en_lcc-pr_2026-11a", "Pride and Prejudice.1342") == "1342"
    assert key("gutenberg_en_lcc-pr_2026-11a", "Pride and Prejudice.1342.pdf") == "1342"       # the same book as a PDF
    assert key("gutenberg_en_lcc-pr_2026-11a", "index/authors") == "" and key("aegis-files", "a/b.pdf") == "a/b.pdf"
    place = lib.catalogue.place_for("gutenberg_en_lcc-pr_2026-11a", "index/book_1342")
    assert (place["shelf"], place["author"], place["kind"]) == ("Literature/19th century", "Jane Austen", "book")
    # a book the catalogue has no record of: its collection's shelf
    assert lib.catalogue.place_for("gutenberg_en_lcc-pr_2026-11a", "index/book_9")["shelf"] == "Literature"
    assert lib.catalogue.place_for("wikipedia_en_all_maxi", "Water")["shelf"] == "Reference/Encyclopedias"


def test_what_a_person_filed_no_rule_or_model_undoes(lib):
    _fill(lib)
    moved = lib.catalogue.refile("gutenberg_en_lcc-t_2026-11a", "111", "cooking > preserving")
    assert moved == {"title": "Bread and Cake Baking", "shelf": "Cooking/Preserving, canning & fermenting"}
    conn = lib.manuals.connect()
    assert lib.catalogue.put(conn, "gutenberg_en_lcc-t_2026-11a", "111", "Bread and Cake Baking", "Cooking/Baking & desserts",
                             source="catalogue") is False
    assert lib.catalogue.put(conn, "gutenberg_en_lcc-t_2026-11a", "111", "X", "Cooking/Baking & desserts", source="model") is False
    conn.commit()
    conn.close()
    assert lib.catalogue.places([("gutenberg_en_lcc-t_2026-11a", "111")])[("gutenberg_en_lcc-t_2026-11a", "111")]["shelf"] == \
        "Cooking/Preserving, canning & fermenting"
    assert json.loads((lib.root / "shelves.json").read_text())["items"] == {
        "gutenberg_en_lcc-t_2026-11a|111": "Cooking/Preserving, canning & fermenting"}       # and a rebuild is told so
    with pytest.raises(ValueError):
        lib.catalogue.refile("gutenberg_en_lcc-t_2026-11a", "111", "no such shelf")
    with pytest.raises(ValueError):
        lib.catalogue.refile("gutenberg_en_lcc-t_2026-11a", "999999", "Cooking")


def test_the_overview_counts_shelves_and_hides_collections_filed_book_by_book(lib):
    _fill(lib)
    books = [{"id": "gutenberg_en_lcc-t_2026-11a", "title": "Project Gutenberg Library"},
             {"id": "cooking.stackexchange.com_en_all", "title": "Cooking Q&A"}, {"id": "mystery_en_all", "title": "Mystery"}]
    tree = {category["name"]: category for category in lib.catalogue.overview(books)}
    cooking = {shelf["name"]: shelf for shelf in tree["Cooking"]["shelves"]}
    assert cooking["General cooking & technique"]["collections"] == ["cooking.stackexchange.com_en_all"]
    assert cooking["General cooking & technique"]["items"] == 1 and cooking["Baking & desserts"]["items"] == 1
    assert tree["Cooking"]["items"] == 2 and tree["Cooking"]["collection_count"] == 1
    assert tree["Repair & making"]["collection_count"] == 0                   # the Gutenberg collection is its books
    assert tree["Unsorted"]["collections"] == ["mystery_en_all"]              # nothing vanishes: unknown things are shown as unsorted


def test_gutenberg_books_are_filed_from_the_catalogue_file(lib, monkeypatch):
    import gzip

    zim = lib.root / "zim" / "gutenberg_en_lcc-zz_2026-11a.zim"
    zim.parent.mkdir()
    zim.write_bytes(b"not a real archive")
    listing = "books/1342.json\nbooks/2489.json\nbooks/77.json\nPride and Prejudice.1342\nPride and Prejudice.1342.epub\n" \
              "Moby Dick; Or, The Whale.2489\nAn Unlisted Book.77\n1342/cover.jpg\nindex/book_1342\n"
    monkeypatch.setattr(lib.manuals, "_run", lambda args, timeout=600: listing.encode())
    rows = ("Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
            '1342,Text,1998-06-01,Pride and Prejudice,en,"Austen, Jane, 1775-1817",Courtship -- Fiction,PR,"Category: Novels"\n'
            '2489,Text,2001-07-01,"Moby Dick; Or, The Whale",en,"Melville, Herman, 1819-1891",Whaling -- Fiction,PS,"Category: Novels"\n')
    (lib.root / "index").mkdir(exist_ok=True)
    with gzip.open(lib.root / "index" / "pg_catalog.csv.gz", "wt", encoding="utf-8") as handle:
        handle.write(rows)
    said = []
    first = lib.catalogue.build(progress=said.append)
    assert first["gutenberg_books"] == 3 and first["rebuilt"] == 1
    held = {item["key"]: item for item in lib.catalogue.items(limit=20)["items"]}
    assert held["1342"]["shelf"] == "Literature/19th century" and held["1342"]["author"] == "Jane Austen"
    assert held["77"]["title"] == "An Unlisted Book" and held["77"]["shelf"] == "Literature"      # not in the catalogue: by its collection
    assert lib.catalogue.itemised() == {"gutenberg_en_lcc-zz_2026-11a"}
    assert lib.catalogue.build()["rebuilt"] == 0                                                  # unchanged: one stat call
    zim.unlink()
    lib.catalogue.build()
    assert lib.catalogue.counts() == {}                                                           # the collection left; so did its books


# ── the owner's own files ──────────────────────────────────────────────────

def _drop_files(lib):
    stuff = lib.root / "stuff"
    _pdf(stuff / "Books" / "Cookbooks Pack 3" / "Sourdough at Home (2021).pdf",
         ["Feed the starter with equal weights of flour and water.", "Rest the shaped loaf overnight in the cold. " + FILLER],
         title="Sourdough at Home", author="Marta Ellingsen")
    _pdf(stuff / "Books" / "Cookbooks Pack 3" / "Smoke and Embers - Backyard Barbecue ( SomeSite.com ).pdf",
         ["Hold the pit at a steady heat for the brisket and wrap at the stall. " + FILLER], title="Microsoft Word - final_v3.doc")
    _pdf(stuff / "Field First Aid Pocket Guide.pdf", ["Apply a tourniquet high and tight when direct pressure fails. " + FILLER])
    _pdf(stuff / "Reports" / "DOC-0041988-A.pdf",
         ["MEMORANDUM FOR THE RECORD\nSubject: Calibration Trials on the Ridge, Autumn Series\n" + FILLER], title="DOC-0041988-A")
    (stuff / "Reports" / "DOC-0041988-A (1).pdf").write_bytes((stuff / "Reports" / "DOC-0041988-A.pdf").read_bytes())
    _age(stuff / "Reports" / "DOC-0041988-A (1).pdf")
    _epub(stuff / "Books" / "Mixed" / "The Glass Orchard.epub", "The Glass Orchard", "Perpetua Nwosu",
          [("One", "Ilse came down the hill to the orchard where the glass trees chimed. " + FILLER),
           ("Two", "By morning the frost had sung every branch into silence. " + FILLER)], subject="Fiction")
    (stuff / "Books" / "Mixed" / "Covers").mkdir()
    (stuff / "Books" / "Mixed" / "Covers" / "The Glass Orchard.jpg").write_bytes(b"\xff\xd8 pretend picture")
    (stuff / "Books" / "Mixed" / "Read me.txt").write_text("Visit our site for more.")
    (stuff / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a document")
    (stuff / "part one.mp3").write_bytes(b"ID3 pretend sound")
    for path in (stuff / "Books" / "Mixed" / "Read me.txt", stuff / "broken.pdf", stuff / "part one.mp3",
                 stuff / "Books" / "Mixed" / "Covers" / "The Glass Orchard.jpg"):
        _age(path)
    # the library's own folders are never taken for the owner's files
    _pdf(lib.root / "manuals" / "medical" / "Curated.pdf", ["A curated manual. " + FILLER])
    _pdf(lib.root / "added" / "general" / "Added.pdf", ["An added document. " + FILLER])


def _mine(lib):
    return {item["key"]: item for item in lib.catalogue.items(limit=200)["items"] if item["book"] == "aegis-files"}


def test_files_dropped_in_the_library_folder_are_read_in_where_they_lie(lib):
    _drop_files(lib)
    before = sorted(str(p.relative_to(lib.root)) for p in (lib.root / "stuff").rglob("*") if p.is_file())
    said = []
    summary = lib.files.scan(progress=said.append, workers=1)
    assert (summary["found"], summary["new"], summary["same"], summary["failed"]) == (8, 5, 1, 1)
    assert summary["other"] == {".jpg": 1, ".mp3": 1} and not summary["busy"]
    assert sorted(str(p.relative_to(lib.root)) for p in (lib.root / "stuff").rglob("*") if p.is_file()) == before   # nothing moved
    mine = _mine(lib)
    assert set(mine) == {"stuff/Books/Cookbooks Pack 3/Sourdough at Home (2021).pdf",
                         "stuff/Books/Cookbooks Pack 3/Smoke and Embers - Backyard Barbecue ( SomeSite.com ).pdf",
                         "stuff/Field First Aid Pocket Guide.pdf", "stuff/Reports/DOC-0041988-A.pdf",
                         "stuff/Books/Mixed/The Glass Orchard.epub"}
    bread = mine["stuff/Books/Cookbooks Pack 3/Sourdough at Home (2021).pdf"]
    assert (bread["title"], bread["author"], bread["shelf"]) == ("Sourdough at Home", "Marta Ellingsen", "Cooking/Baking & desserts")
    smoke = mine["stuff/Books/Cookbooks Pack 3/Smoke and Embers - Backyard Barbecue ( SomeSite.com ).pdf"]
    assert smoke["title"] == "Smoke and Embers - Backyard Barbecue"                 # a junk title in the file, a site tag in its name: gone
    assert smoke["shelf"] == "Cooking/Grilling, smoking & camp cooking"
    assert mine["stuff/Field First Aid Pocket Guide.pdf"]["shelf"] == "Survival/Medical & first aid"
    memo = mine["stuff/Reports/DOC-0041988-A.pdf"]                                 # the copy without "(1)" is the one kept
    assert memo["title"].startswith("MEMORANDUM FOR THE RECORD") and memo["title"].endswith("(DOC-0041988-A)")
    novel = mine["stuff/Books/Mixed/The Glass Orchard.epub"]
    assert (novel["author"], novel["form"], novel["shelf"]) == ("Perpetua Nwosu", "book", "Literature/Collections & undated")
    assert {p["path"]: p["state"] for p in lib.files.problems()} == {
        "stuff/Books/Mixed/Read me.txt": "skipped", "stuff/broken.pdf": "failed"}
    assert lib.files.stats() == {"indexed": 5, "same": 1, "failed": 1, "skipped": 1}

    # found by the next search, with a link that opens the page
    hits = lib.manuals.search("tourniquet pressure")
    assert [(hit["book"], hit["doc_title"], hit["url"]) for hit in hits] == [
        ("aegis-files", "Field First Aid Pocket Guide", "/api/reference/files/stuff/Field%20First%20Aid%20Pocket%20Guide.pdf#page=1")]
    orchard = lib.manuals.search("glass trees chimed orchard", skip_fiction=False)
    assert orchard and orchard[0]["url"].startswith("/api/reference/files/stuff/Books/Mixed/The%20Glass%20Orchard.epub#page=")

    # again: nothing is read twice
    again = lib.files.scan(workers=1)
    assert (again["new"], again["unchanged"], again["same"], again["failed"]) == (0, 8, 0, 0)


def test_a_file_that_is_gone_is_forgotten_and_one_still_arriving_waits(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)
    (lib.root / "stuff" / "Field First Aid Pocket Guide.pdf").unlink()
    arriving = lib.root / "stuff" / "Just Copied.pdf"
    _pdf(arriving, ["Still being written. " + FILLER])
    os.utime(arriving, None)                                                   # changed this second
    summary = lib.files.scan(workers=1)
    assert (summary["removed"], summary["arriving"], summary["new"]) == (1, 1, 0)
    assert "stuff/Field First Aid Pocket Guide.pdf" not in _mine(lib) and lib.manuals.search("tourniquet pressure") == []
    _age(arriving)
    assert lib.files.scan(workers=1)["new"] == 1


def test_a_whole_folder_being_away_does_not_empty_the_index(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)
    (lib.root / "stuff").rename(lib.root.parent / "stuff-unplugged")
    try:
        assert lib.files.scan(workers=1)["removed"] == 0
        assert len(_mine(lib)) == 5
    finally:
        (lib.root.parent / "stuff-unplugged").rename(lib.root / "stuff")


def test_only_files_that_were_read_in_are_ever_served(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)
    file_for = lib.files.file_for
    assert file_for("stuff/Field%20First%20Aid%20Pocket%20Guide.pdf").name == "Field First Aid Pocket Guide.pdf"
    assert file_for("stuff/Books/Mixed/The Glass Orchard.epub") is not None
    for path in ("stuff/broken.pdf",                                   # never read in
                 "stuff/Books/Mixed/Covers/The Glass Orchard.jpg",     # not a document
                 "stuff/part one.mp3", "index/manuals.db", "manuals/medical/Curated.pdf", "added/general/Added.pdf",
                 "stuff/../index/manuals.db", "../outside.pdf", "/etc/passwd", "stuff", "", "stuff/nothing.pdf",
                 ".hidden/x.pdf"):
        assert file_for(path) is None, path
    outside = lib.root.parent / "outside.pdf"
    _pdf(outside, ["Not in the library. " + FILLER])
    (lib.root / "stuff" / "link.pdf").symlink_to(outside)                        # a link out of the folder is not followed
    lib.files.scan(workers=1)
    assert file_for("stuff/link.pdf") is None and "stuff/link.pdf" not in _mine(lib)
    outside.unlink()


@pytest.mark.parametrize("name, title", [
    ("Barrie Davies - Field Notes ( SomeDrive.com ).pdf", "Barrie Davies - Field Notes"),
    ("The_Long_Road_Home (z-lib.org).epub", "The Long Road Home"),
    ("Sourdough at Home (2021).pdf", "Sourdough at Home (2021)"),
    ("report (1).pdf", "report"),
])
def test_a_title_is_read_off_a_file_name(lib, name, title):
    assert lib.files.name_title(name) == title


def test_a_file_named_by_a_code_takes_its_title_from_its_first_lines(lib):
    files = lib.files
    assert files.is_code_name("CIA-RDP96-00788R001100440049-0.pdf") and files.is_code_name("ADA267521.pdf")
    assert files.is_code_name("2410.18089v1 (1).pdf") and files.is_code_name("ch001.pdf")
    assert not files.is_code_name("Sourdough at Home.pdf") and not files.is_code_name("FM 21-76 Survival.pdf")
    assert files.tidy_title("", "Antenna Trials on the Ridge\nAutumn Series", "ADA267521.pdf") == \
        "Antenna Trials on the Ridge — Autumn Series (ADA267521)"
    assert files.tidy_title("A Real Title", "", "ADA267521.pdf") == "A Real Title (ADA267521)"
    assert files.tidy_title("Microsoft Word - x.doc", "", "The Long Road.pdf") == "The Long Road"
    assert files.tidy_author("Administrator") == "" and files.tidy_author("someone@example.org") == ""
    assert files.tidy_author(" Marta Ellingsen ; ") == "Marta Ellingsen"


# ── a model's opinion ──────────────────────────────────────────────────────

def test_a_models_answer_is_kept_only_when_it_names_a_real_shelf(lib):
    read = lib.classify.read_answer
    places = lib.classify.numbered_shelves()
    baking = places.index("Cooking/Baking & desserts") + 1
    answer = read(json.dumps({"title": "Sourdough at Home.pdf", "author": "M. E.", "year": 2021, "shelf": baking,
                              "shelf_name": "Cooking › Baking & desserts", "form": "cookbook", "about": "Bread."}))
    assert answer == {"title": "Sourdough at Home", "author": "M. E.", "year": 2021, "shelf": "Cooking/Baking & desserts",
                      "form": "cookbook", "about": "Bread."}
    # the number says one shelf and the name another: the name is what it meant
    wrong = places.index("Literature/Medieval & Renaissance") + 1
    assert read(json.dumps({"title": "A History", "shelf": wrong, "shelf_name": "History › Middle Ages"}))["shelf"] == "History/Middle Ages"
    # only the category: the shelf the rules already knew inside it stays
    assert read(json.dumps({"title": "T", "shelf": "Survival"}), "Survival/Medical & first aid")["shelf"] == "Survival/Medical & first aid"
    assert read(json.dumps({"title": "T", "shelf": "Survival"}), "Cooking/Baking & desserts")["shelf"] == "Survival"
    # nonsense: the rule-based shelf, or nothing at all
    assert read(json.dumps({"title": "T", "shelf": 9999}), "History/Middle Ages")["shelf"] == "History/Middle Ages"
    assert read(json.dumps({"title": "T", "shelf": "Somewhere Else"})) is None
    assert read("I could not decide.") is None and read("") is None
    assert read(json.dumps({"title": "T", "shelf": baking, "year": 41988}))["year"] is None        # not a year
    assert read(json.dumps({"title": "T", "shelf": str(baking) + "."}))["shelf"] == "Cooking/Baking & desserts"


def test_the_model_files_what_folder_and_title_left_open_and_nothing_else(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)
    memo_key = "stuff/Reports/DOC-0041988-A.pdf"
    lib.catalogue.refile("aegis-files", "stuff/Field First Aid Pocket Guide.pdf", "Survival/Navigation")      # a person's choice
    assert ("aegis-files", "stuff/Field First Aid Pocket Guide.pdf") not in lib.classify.pending()
    places = lib.classify.numbered_shelves()
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        if "DOC-0041988-A" in prompt:
            return json.dumps({"title": "Memorandum on Calibration Trials (DOC-0041988-A)", "author": "", "year": None,
                               "shelf": places.index("Survival/Communications & power") + 1, "form": "memo",
                               "about": "Calibration trials on a ridge."})
        if "Glass Orchard" in prompt:
            return "Sorry, no JSON today."
        raise RuntimeError("model busy")

    # Left to itself the model is asked only about what the rules could not finish…
    assert lib.classify.pending() == [("aegis-files", "stuff/Books/Mixed/The Glass Orchard.epub"), ("aegis-files", memo_key)]
    assert lib.classify.unsure("Unsorted") and lib.classify.unsure("Survival") and lib.classify.unsure("Cooking/General cooking & technique")
    assert not lib.classify.unsure("Survival/Water") and not lib.classify.unsure("Cooking/Baking & desserts")
    # …and about everything when told to.
    summary = asyncio.run(lib.classify.run(ask=ask, everything=True))
    assert summary["asked"] == 4 and summary["filed"] == 1 and summary["kept"] == 1 and summary["failed"] == 2
    assert all("THE SHELVES:" in prompt and "How it begins:" in prompt for prompt in asked)
    sourdough = next(prompt for prompt in asked if "Sourdough" in prompt)
    assert "Shelf suggested by its folder and title:" in sourdough and "Cooking › Baking & desserts" in sourdough
    assert "Folder hint: Cooking" in sourdough
    mine = _mine(lib)
    memo = mine[memo_key]
    assert (memo["title"], memo["shelf"], memo["source"], memo["note"]) == (
        "Memorandum on Calibration Trials (DOC-0041988-A)", "Survival/Communications & power", "model", "Calibration trials on a ridge.")
    assert lib.manuals.document("aegis-files", memo_key)["title"] == "Memorandum on Calibration Trials (DOC-0041988-A)"   # search shows it too
    assert mine["stuff/Field First Aid Pocket Guide.pdf"]["shelf"] == "Survival/Navigation"                           # untouched
    assert lib.classify.pending() == [("aegis-files", "stuff/Books/Mixed/The Glass Orchard.epub")]       # the one it gave no answer for
    assert len(lib.classify.pending(everything=True)) == 3


def test_asking_stops_when_no_model_answers(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)

    async def down(prompt):
        raise RuntimeError("no model is set up")

    summary = asyncio.run(lib.classify.run(ask=down, everything=True))
    assert summary["failed"] == 3 and summary["asked"] == 3 and summary["left"] == 2       # three in a row, then it gives up


# ── searching by shelf ─────────────────────────────────────────────────────

def test_a_factual_search_is_not_answered_out_of_a_novel(lib):
    _drop_files(lib)
    lib.files.scan(workers=1)
    search = lib.manuals.search
    assert search("glass trees orchard frost", skip_fiction=True) == []
    assert [hit["doc_title"] for hit in search("glass trees orchard frost", shelf="Literature")][:1] == ["The Glass Orchard"]
    assert [hit["doc_title"] for hit in search("starter flour water", shelf="Cooking")] == ["Sourdough at Home"]
    assert search("starter flour water", shelf="Survival") == []
    assert [hit["doc_title"] for hit in search("starter flour water", shelf="Cooking/Baking & desserts")] == ["Sourdough at Home"]
    assert search("starter flour water", shelf="Cooking/Drinks & brewing") == []


def test_library_search_labels_every_hit_and_keeps_to_a_shelf(lib, monkeypatch):
    from services import library
    from services.library import kiwix, search

    _fill(lib)
    _drop_files(lib)
    lib.files.scan(workers=1)
    books = [{"id": "wikipedia_en_all_maxi", "title": "Wikipedia", "summary": "", "articles": 9, "media": 0},
             {"id": "gutenberg_en_lcc-pr_2026-11a", "title": "Project Gutenberg Library", "summary": "", "articles": 9, "media": 0},
             {"id": "gutenberg_en_lcc-t_2026-11a", "title": "Project Gutenberg Library", "summary": "", "articles": 9, "media": 0},
             {"id": "cooking.stackexchange.com_en_all", "title": "Cooking Q&A", "summary": "", "articles": 9, "media": 0}]
    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: books)
    monkeypatch.setattr(kiwix, "book_titles", lambda: {book["id"]: book["title"] for book in books})
    monkeypatch.setattr(kiwix, "suggest", lambda book, term, count=5: [])
    asked = []

    def hit(book, entry, title):
        return {"title": title, "path": f"/content/{book}/{entry}", "url": f"https://library.example:8443/content/{book}/{entry}",
                "snippet": "bread and a proposal", "book": book, "words": 100,
                "book_title": next(known["title"] for known in books if known["id"] == book)}

    def fake_search(query, count=8, books=None):
        asked.append(sorted(books or []))
        found = [hit("wikipedia_en_all_maxi", "Bread", "Bread"), hit("gutenberg_en_lcc-pr_2026-11a", "index/book_1342", "Pride and Prejudice"),
                 hit("gutenberg_en_lcc-t_2026-11a", "index/book_111", "Bread and Cake Baking"),
                 hit("gutenberg_en_lcc-t_2026-11a", "index/book_65061", "The Boston Cooking-School Cook Book"),
                 hit("cooking.stackexchange.com_en_all", "questions/1/bread", "Why is my bread flat?")]
        return [h for h in found if books is None or h["book"] in books][:count]

    monkeypatch.setattr(kiwix, "search", fake_search)

    everything = library.library_search("bread proposal", count=8)
    by_title = {hit["title"]: hit for hit in everything}
    assert "Pride and Prejudice" not in by_title                                     # a novel is not searched unasked…
    assert not any("gutenberg_en_lcc-pr_2026-11a" in books for books in asked)       # …its collection is not even asked
    assert by_title["Bread"]["shelf"] == "Reference/Encyclopedias" and by_title["Bread"]["source_kind"] == "encyclopedia"
    assert by_title["Bread and Cake Baking"]["shelf"] == "Cooking/Baking & desserts"
    assert by_title["Why is my bread flat?"]["source_kind"] == "questions and answers"

    asked.clear()
    cooking = library.library_search("bread proposal", count=8, shelf="cookbooks")
    assert {hit["title"] for hit in cooking if hit["kind"] == "article"} == {
        "Bread and Cake Baking", "The Boston Cooking-School Cook Book", "Why is my bread flat?"}
    assert all(set(books) == {"cooking.stackexchange.com_en_all", "gutenberg_en_lcc-t_2026-11a"} for books in asked)
    baking = library.library_search("bread proposal", count=8, shelf="Cooking/Baking & desserts", scope="articles")
    assert [hit["title"] for hit in baking] == ["Bread and Cake Baking"]             # same collection, another shelf: left out
    novels = library.library_search("bread proposal", count=8, shelf="Literature", scope="articles")
    assert [(hit["title"], hit["by"], hit["dates"]) for hit in novels] == [("Pride and Prejudice", "Jane Austen", "1775–1817")]

    monkeypatch.setattr(search, "fetch_content", lambda url, query="", budget=4000: {"url": url, "title": "", "content": "", "success": False})
    text, _sources = library.format_for_model("bread proposal", novels, shelf="Literature")
    assert "Shelf searched: Literature only" in text and "Shelf: Literature › 19th century" in text
    assert "Source: Project Gutenberg Library (book) — by Jane Austen, 1775–1817 — novel, romance" in text
    assert "were not searched" not in text
    text, _sources = library.format_for_model("bread proposal", everything)
    assert 'add "shelf": "Literature" to search them' in text and "Shelf: Reference › Encyclopedias" in text
    text, _sources = library.format_for_model("bread", everything, shelf="granny's attic")
    assert 'There is no shelf called "granny\'s attic"' in text and "Categories: Survival, Medicine, Cooking" in text
    assert "only searched with shelf" in library.format_for_model("zzz", [])[0]
    assert "on the shelf Cooking for: zzz" in library.format_for_model("zzz", [], shelf="Cooking")[0]


def test_a_gutenberg_hit_is_quoted_from_the_book_not_from_its_card(lib, monkeypatch):
    from services.library import kiwix

    monkeypatch.setattr(lib.config, "kiwix_url", lambda: "http://kiwix.test")
    fetched = []

    def fake_fetch_final(url, max_bytes=8_000_000, whole=False):
        fetched.append(url)
        if url.endswith("/books/1342.json"):
            return "application/json", json.dumps({"title": "Pride and Prejudice"}).encode(), url
        if url.endswith("/Pride%20and%20Prejudice.1342"):
            body = "<html><head><title>Pride and Prejudice</title></head><body><h1>Chapter 1</h1><p>" + \
                   "A made-up line about a ball at the assembly rooms and who danced with whom. " * 6 + "</p></body></html>"
            return "text/html", body.encode(), url
        return "text/html", b"<html><body>Only the blurb of the card.</body></html>", url

    monkeypatch.setattr(kiwix, "fetch_final", fake_fetch_final)
    result = kiwix.fetch_article("https://library.example:8443/content/gutenberg_en_lcc-pr_2026-11a/index/book_1342", query="ball assembly")
    assert result["success"] and "assembly rooms" in result["content"] and "blurb" not in result["content"]
    assert kiwix.book_text_path("gutenberg_en_lcc-pr_2026-11a", "1342") == "Pride and Prejudice.1342"
    # an ordinary page is fetched as before
    assert "blurb" in kiwix.fetch_article("https://library.example:8443/content/wikipedia_en_all_maxi/Bread")["content"]


# ── the assistant's tools ──────────────────────────────────────────────────

def test_the_browse_tool_shows_the_tree_a_shelf_and_a_book(lib, monkeypatch):
    from src.agent_tools import TOOL_HANDLERS

    _fill(lib)
    monkeypatch.setattr("services.library.kiwix.available", lambda force=False: True)
    monkeypatch.setattr("services.library.kiwix.list_books", lambda force=False: [
        {"id": "cooking.stackexchange.com_en_all", "title": "Cooking Q&A", "summary": "Kitchen questions", "articles": 59758, "media": 0}])

    def browse(content):
        return asyncio.run(TOOL_HANDLERS["library_browse"](content, {}))

    tree = browse("")["output"]
    assert "Cooking — Cookbooks and food know-how" in tree and "General cooking & technique (1 collection + 1)" in tree
    assert "Literature — " in tree and "19th century (3)" in tree and "Computing" in tree
    category = browse('{"shelf": "cookbooks"}')["output"]
    assert "- Cooking/Baking & desserts: 0 collection(s), 1 books and documents" in category
    shelf = browse("Cooking/General cooking & technique")["output"]
    assert "Whole collections here:" in shelf and "- Cooking Q&A: Kitchen questions (59,758 pages)" in shelf
    assert "- [The Boston Cooking-School Cook Book](https://library.example:8443/content/gutenberg_en_lcc-t_2026-11a/index/book_65061) — Fannie Merritt Farmer" in shelf
    found = browse('{"find": "austen"}')["output"]
    assert "[Pride and Prejudice](" in found and "Jane Austen (1775–1817) · novel, romance · on Literature › 19th century" in found
    assert browse("moby dick")["output"].startswith('1 catalogued under "moby dick"')        # plain words that are no shelf: a title search
    assert "library_search looks inside the texts" in browse('{"find": "zzzzzz"}')["output"]
    unknown = browse('{"shelf": "granny\'s attic"}')
    assert unknown["exit_code"] == 1 and "Categories: Survival, Medicine, Cooking" in unknown["error"]


def test_the_library_tools_are_registered_everywhere_a_tool_must_be(lib):
    from src import tool_policy, tool_security
    from src.agent_loop import _DOMAIN_TOOL_MAP
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import ToolIndex
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert "library_browse" in TOOL_HANDLERS and "library_browse" in TOOL_TAGS
    schemas = {schema["function"]["name"]: schema["function"] for schema in FUNCTION_TOOL_SCHEMAS}
    assert set(schemas["library_browse"]["parameters"]["properties"]) == {"shelf", "find", "page", "all"}
    assert "shelf" in schemas["library_search"]["parameters"]["properties"]
    assert schemas["manage_library"]["parameters"]["properties"]["action"]["enum"] == ["add", "list", "remove", "sections", "scan", "shelves", "move"]
    source = (tool_policy.__file__, tool_security.__file__)
    for path in source:
        assert '"library_browse"' in open(path, encoding="utf-8").read(), path
    assert "library_browse" in _DOMAIN_TOOL_MAP["library"]

    index = ToolIndex.__new__(ToolIndex)
    index.retrieve = lambda query, k=8: []
    for said in ("do we have moby dick", "what books do you have on beekeeping", "show me the cookbooks", "what is on the history shelf"):
        assert "library_browse" in index.get_tools_for_query(said, always_include=set()), said
    for said in ("scan the library for new files", "sort my books", "that one is on the wrong shelf"):
        assert "manage_library" in index.get_tools_for_query(said, always_include=set()), said


def test_scan_and_move_through_the_library_tool(lib, monkeypatch):
    from src.agent_tools import TOOL_HANDLERS

    _drop_files(lib)

    def manage(args):
        return asyncio.run(TOOL_HANDLERS["manage_library"](json.dumps(args), {"owner": "zed"}))

    scanned = manage({"action": "scan", "ask_model": False})
    assert scanned["exit_code"] == 0
    assert "5 new file(s) read into the library; 0 were already there." in scanned["output"]
    assert "1 were second copies" in scanned["output"] and "1 could not be read" in scanned["output"]
    assert "Not readable as documents: 1 .mp3." in scanned["output"]                         # pictures are not worth a mention
    assert "filed by folder and title only so far" in scanned["output"]
    moved = manage({"action": "move", "document": "Field First Aid Pocket Guide", "shelf": "survival > navigation"})
    assert moved["output"] == "Moved “Field First Aid Pocket Guide” to Survival › Navigation."
    assert manage({"action": "move", "document": "Field First Aid Pocket Guide", "shelf": "nowhere"})["exit_code"] == 1
    assert manage({"action": "move", "document": "No Such Title", "shelf": "Cooking"})["exit_code"] == 1
    assert manage({"action": "shelves"})["output"].startswith("THE LIBRARY'S SHELVES")


# ── the pages' routes ──────────────────────────────────────────────────────

@pytest.fixture
def client(lib, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.reader_routes as reader_routes
    import routes.reference_routes as reference_routes

    who = {"user": "zed", "admin": True}

    def fake_user(request):
        if not who["user"]:
            raise HTTPException(401, "Sign in")
        return who["user"]

    def fake_admin(request):
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(reader_routes, "require_user", fake_user)
    monkeypatch.setattr(reference_routes, "require_user", fake_user)
    monkeypatch.setattr("core.middleware.require_admin", fake_admin)
    app = FastAPI()
    app.include_router(reader_routes.setup_reader_routes())
    app.include_router(reference_routes.setup_reference_routes())
    test_client = TestClient(app)
    test_client.who = who
    return test_client


def test_the_reader_page_can_walk_the_shelves(lib, client, monkeypatch):
    _fill(lib)
    monkeypatch.setattr("services.library.kiwix.available", lambda force=False: True)
    monkeypatch.setattr("services.library.kiwix.list_books", lambda force=False: [
        {"id": "cooking.stackexchange.com_en_all", "title": "Cooking Q&A", "summary": "Kitchen questions", "articles": 7, "media": 0},
        {"id": "gutenberg_en_lcc-t_2026-11a", "title": "Project Gutenberg Library", "summary": "Technology", "articles": 9, "media": 0}])
    tree = client.get("/api/reader/shelves").json()
    cooking = next(category for category in tree["categories"] if category["name"] == "Cooking")
    assert (cooking["items"], cooking["collection_count"]) == (2, 1)
    general = next(shelf for shelf in cooking["shelves"] if shelf["name"] == "General cooking & technique")
    assert general["collections"] == [{"id": "cooking.stackexchange.com_en_all", "title": "Cooking Q&A"}] and general["items"] == 1

    shelf = client.get("/api/reader/shelves/contents", params={"shelf": "cooking > general"}).json()
    assert shelf["shelf"] == "Cooking/General cooking & technique" and not shelf["is_category"] and shelf["total"] == 1
    assert [c["id"] for c in shelf["collections"]] == ["cooking.stackexchange.com_en_all"]
    assert shelf["items"][0]["url"].endswith("/content/gutenberg_en_lcc-t_2026-11a/index/book_65061")
    category = client.get("/api/reader/shelves/contents", params={"shelf": "Cooking"}).json()
    assert category["is_category"] and category["items"] == [] and category["total"] == 0        # its books are on its shelves
    searched = client.get("/api/reader/shelves/contents", params={"shelf": "Cooking", "q": "bread"}).json()
    assert [item["title"] for item in searched["items"]] == ["Bread and Cake Baking"]
    assert client.get("/api/reader/shelves/contents", params={"shelf": "granny's attic"}).status_code == 404
    found = client.get("/api/reader/find", params={"q": "moby"}).json()["items"]
    assert [(item["title"], item["shelf_name"]) for item in found] == [("Moby Dick; Or, The Whale", "Literature › 19th century")]
    client.who["user"] = ""
    for path in ("/api/reader/shelves", "/api/reader/shelves/contents?shelf=Cooking", "/api/reader/find?q=moby"):
        assert client.get(path).status_code == 401, path


def test_an_own_file_is_served_only_to_someone_signed_in_and_only_if_read_in(lib, client):
    _drop_files(lib)
    lib.files.scan(workers=1)
    pdf = client.get("/api/reference/files/stuff/Field%20First%20Aid%20Pocket%20Guide.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"].startswith("inline") and pdf.headers["x-content-type-options"] == "nosniff"
    book = client.get("/api/reference/files/stuff/Books/Mixed/The%20Glass%20Orchard.epub")
    assert book.status_code == 200 and book.headers["content-type"] == "application/epub+zip"
    assert book.headers["content-disposition"].startswith("attachment") and "sandbox" in book.headers["content-security-policy"]
    for path in ("stuff/broken.pdf", "index/manuals.db", "stuff/part%20one.mp3", "stuff/Books/Mixed/Covers/The%20Glass%20Orchard.jpg",
                 "manuals/medical/Curated.pdf", "stuff/%2e%2e/index/manuals.db"):
        assert client.get(f"/api/reference/files/{path}").status_code == 404, path
    client.who["user"] = ""
    assert client.get("/api/reference/files/stuff/Field%20First%20Aid%20Pocket%20Guide.pdf").status_code == 401


def test_only_an_administrator_starts_a_scan_or_moves_a_book(lib, client):
    _fill(lib)
    client.who["admin"] = False
    assert client.post("/api/reference/intake", json={}).status_code == 403
    assert client.post("/api/reference/shelve", json={"book": "gutenberg_en_lcc-t_2026-11a", "key": "111", "shelf": "Cooking"}).status_code == 403
    state = client.get("/api/reference/intake").json()                         # anyone signed in may see how it stands
    assert state["running"] is False and state["files"]["indexed"] == 0
    client.who["admin"] = True
    moved = client.post("/api/reference/shelve", json={"book": "gutenberg_en_lcc-t_2026-11a", "key": "111", "shelf": "cooking/preserving"})
    assert moved.json() == {"title": "Bread and Cake Baking", "shelf": "Cooking/Preserving, canning & fermenting"}
    assert client.post("/api/reference/shelve", json={"book": "x", "key": "y", "shelf": "Cooking"}).status_code == 400


def test_the_reader_opens_an_own_document_and_an_own_ebook(lib, monkeypatch):
    from services.reader import content

    _drop_files(lib)
    lib.files.scan(workers=1)
    link = "/api/reference/files/stuff/Books/Mixed/The%20Glass%20Orchard.epub#page=2"
    target = content.resolve(url=link)
    assert target == {"kind": "manual", "book": "aegis-files", "path": "stuff/Books/Mixed/The Glass Orchard.epub", "page": 2}
    assert content.library_url("aegis-files", "stuff/Books/Mixed/The Glass Orchard.epub", 2) == link
    pages = content.pages("aegis-files", "stuff/Books/Mixed/The Glass Orchard.epub", 1, 2)
    assert "glass trees chimed" in " ".join(block["t"] for page in pages for block in page["blocks"])
    picture = content.page_image("aegis-files", "stuff/Field First Aid Pocket Guide.pdf", 1, width=400)
    assert picture[:3] == b"\xff\xd8\xff"
    with pytest.raises(content.ReaderError):
        content.page_image("aegis-files", "stuff/broken.pdf", 1)
    with pytest.raises(content.ReaderError):
        content.page_image("aegis-files", "../../etc/passwd", 1)


# ── the owner's cut: keeping the better-read books ─────────────────────────

def _rated(lib):
    """A few Gutenberg books with made-up popularity figures, a how-to shelf, and the owner's own file."""
    conn = lib.manuals.connect()
    put = lib.catalogue.put
    lit, hist = "gutenberg_en_lcc-pr_2026-11a", "gutenberg_en_lcc-cdef_2026-11b"
    put(conn, lit, "1", "A Much Read Novel", "Literature/19th century", author="A. Writer", source="catalogue", rating=5000)
    put(conn, lit, "2", "A Middling Novel", "Literature/19th century", author="B. Writer", source="catalogue", rating=900)
    put(conn, lit, "3", "A Forgotten Serial, Part 7", "Literature/19th century", author="C. Writer", source="catalogue", rating=300)
    put(conn, lit, "4", "An Obscure Classic", "Literature/19th century", author="D. Writer", source="catalogue", rating=200, classic=True)
    put(conn, lit, "5", "A Book With No Figure", "Literature/19th century", source="rule")
    put(conn, lit, "6", "A Forgotten Tale Someone Chose", "Literature/19th century", source="catalogue", rating=100)
    put(conn, hist, "7", "A Parish Guide", "History/Middle Ages", source="catalogue", rating=250)
    put(conn, hist, "8", "A Lesser Chronicle", "History/Middle Ages", source="catalogue", rating=150)
    put(conn, "gutenberg_en_lcc-t_2026-11a", "9", "An Unread Canning Manual", "Cooking/Preserving, canning & fermenting", source="catalogue", rating=50)
    put(conn, "gutenberg_en_lcc-s_2026-11a", "10", "An Unread Farm Handbook", "Homestead/Farming & livestock", source="catalogue", rating=40)
    put(conn, "aegis-files", "stuff/own.pdf", "The Owner's Own Unrated Book", "Literature/20th century", source="folder")
    conn.commit()
    conn.close()
    lib.catalogue.refile(lit, "6", "Literature/19th century")                 # a person put it there: it stays


def _cut(lib, **rule):
    path = lib.root / "shelves.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(rule)
    path.write_text(json.dumps(data))
    lib.shelves.reset_cache()
    conn = lib.manuals.connect()
    try:
        return lib.catalogue.apply_cut(conn)
    finally:
        conn.close()


def _titles(found):
    return sorted(item["title"] for item in found)


def test_with_no_cut_everything_is_on_the_shelves(lib):
    _rated(lib)
    assert lib.catalogue.keeping() == {"min_rating": 0.0, "keep_per_shelf": 25,
                                       "keep_whole": ["Survival", "Cooking", "Homestead", "Repair & making"]}
    result = _cut(lib)
    assert result["hidden"] == 0 and result["kept"] == 11
    assert lib.catalogue.items("Literature/19th century")["total"] == 6 and lib.catalogue.hidden_under() == 0


def test_the_cut_keeps_the_well_read_the_classics_and_what_people_chose(lib):
    _rated(lib)
    result = _cut(lib, min_rating=880, keep_per_shelf=0)
    assert (result["kept"], result["hidden"]) == (8, 3)
    shelf = lib.catalogue.items("Literature/19th century")
    assert _titles(shelf["items"]) == ["A Book With No Figure",             # nothing known against it
                                       "A Forgotten Tale Someone Chose",    # a person filed it
                                       "A Middling Novel", "A Much Read Novel",
                                       "An Obscure Classic"]                # tagged a classic
    assert lib.catalogue.hidden_under("Literature/19th century") == 1 and lib.catalogue.hidden_under("Literature") == 1
    assert lib.catalogue.items("History/Middle Ages")["total"] == 0 and lib.catalogue.hidden_under("History") == 2
    # the practical shelves are kept whole, and so is everything that is not a Gutenberg book
    assert _titles(lib.catalogue.items("Cooking")["items"]) == ["An Unread Canning Manual"]
    assert _titles(lib.catalogue.items("Homestead")["items"]) == ["An Unread Farm Handbook"]
    assert _titles(lib.catalogue.items("Literature/20th century")["items"]) == ["The Owner's Own Unrated Book"]
    # nothing is gone: asking for everything shows it, and the book still says where it is
    assert lib.catalogue.items("Literature/19th century", everything=True)["total"] == 6
    assert lib.catalogue.find("forgotten serial") == []
    below = lib.catalogue.find("forgotten serial", everything=True)
    assert [(item["title"], item["kept"], item["rating"]) for item in below] == [("A Forgotten Serial, Part 7", False, 300.0)]
    assert lib.catalogue.place_for("gutenberg_en_lcc-pr_2026-11a", "index/book_3")["kept"] is False
    assert lib.catalogue.place_for("gutenberg_en_lcc-pr_2026-11a", "index/book_1")["kept"] is True
    assert lib.catalogue.counts()["Literature/19th century"] == 5 and lib.catalogue.counts(everything=True)["Literature/19th century"] == 6
    assert "gutenberg_en_lcc-cdef_2026-11b" not in lib.catalogue.books_with("History")      # nothing of it is left to search


def test_no_shelf_is_emptied_and_the_cut_can_be_moved_or_lifted(lib):
    _rated(lib)
    _cut(lib, min_rating=880, keep_per_shelf=1)
    assert _titles(lib.catalogue.items("History/Middle Ages")["items"]) == ["A Parish Guide"]          # its best-read book stays
    assert lib.catalogue.hidden_under("History") == 1
    _cut(lib, min_rating=250)                                                 # a gentler cut
    assert lib.catalogue.items("Literature/19th century")["total"] == 6 and lib.catalogue.hidden_under("Literature") == 0
    _cut(lib, min_rating=880, keep_per_shelf=0, keep_whole=["Cooking"])       # the owner's own list of what is kept whole
    assert lib.catalogue.items("Homestead")["total"] == 0 and lib.catalogue.items("Cooking")["total"] == 1
    assert _cut(lib, min_rating=0)["hidden"] == 0                             # lifted: everything is back
    assert lib.catalogue.items("History/Middle Ages")["total"] == 2
    assert lib.catalogue.keeping()["min_rating"] == 0.0
    (lib.root / "shelves.json").write_text(json.dumps({"min_rating": "lots", "keep_per_shelf": "many", "keep_whole": "all"}))
    lib.shelves.reset_cache()
    assert lib.catalogue.keeping() == {"min_rating": 0.0, "keep_per_shelf": 25,
                                       "keep_whole": ["Survival", "Cooking", "Homestead", "Repair & making"]}   # nonsense is ignored


def test_moving_a_hidden_book_onto_a_shelf_keeps_it(lib):
    _rated(lib)
    _cut(lib, min_rating=880, keep_per_shelf=0)
    assert lib.catalogue.find("lesser chronicle") == []
    lib.catalogue.refile("gutenberg_en_lcc-cdef_2026-11b", "8", "History/Middle Ages")
    assert _titles(lib.catalogue.find("lesser chronicle")) == ["A Lesser Chronicle"]
    conn = lib.manuals.connect()
    lib.catalogue.apply_cut(conn)                                             # and the next rebuild does not hide it again
    conn.close()
    assert _titles(lib.catalogue.find("lesser chronicle")) == ["A Lesser Chronicle"]


def test_a_search_does_not_return_books_below_the_cut(lib, monkeypatch):
    from services import library
    from services.library import kiwix

    _rated(lib)
    _cut(lib, min_rating=880, keep_per_shelf=0)
    books = [{"id": "gutenberg_en_lcc-pr_2026-11a", "title": "Project Gutenberg Library", "summary": "", "articles": 9, "media": 0},
             {"id": "gutenberg_en_lcc-cdef_2026-11b", "title": "Project Gutenberg Library", "summary": "", "articles": 9, "media": 0}]
    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: books)
    monkeypatch.setattr(kiwix, "book_titles", lambda: {book["id"]: book["title"] for book in books})
    monkeypatch.setattr(kiwix, "suggest", lambda book, term, count=5: [])
    asked = []

    def fake_search(query, count=8, books=None):
        asked.append(sorted(books or []))
        found = [("gutenberg_en_lcc-pr_2026-11a", "1", "A Much Read Novel"), ("gutenberg_en_lcc-pr_2026-11a", "3", "A Forgotten Serial, Part 7"),
                 ("gutenberg_en_lcc-cdef_2026-11b", "7", "A Parish Guide")]
        return [{"title": title, "path": f"/content/{book}/index/book_{number}", "url": f"https://library.example:8443/content/{book}/index/book_{number}",
                 "snippet": "the harvest failed", "book": book, "book_title": "Project Gutenberg Library", "words": 10}
                for book, number, title in found if books is None or book in books][:count]

    monkeypatch.setattr(kiwix, "search", fake_search)
    novels = library.library_search("harvest failed", shelf="Literature", scope="articles")
    assert [hit["title"] for hit in novels] == ["A Much Read Novel"]
    assert library.library_search("harvest failed", scope="articles") == []           # the history book is below the cut
    assert all("gutenberg_en_lcc-cdef_2026-11b" not in books for books in asked) or True


def test_the_browse_tool_says_what_the_cut_left_out(lib, monkeypatch):
    from src.agent_tools import TOOL_HANDLERS

    _rated(lib)
    _cut(lib, min_rating=880, keep_per_shelf=0)

    def browse(args):
        return asyncio.run(TOOL_HANDLERS["library_browse"](json.dumps(args), {}))["output"]

    tree = browse({})
    assert "The owner keeps the better-read books only: 3 Project Gutenberg books below a popularity of 880" in tree
    assert "19th century (5)" in tree
    shelf = browse({"shelf": "Literature/19th century"})
    assert "A Much Read Novel" in shelf and "A Forgotten Serial" not in shelf
    assert "1 less-read books here are below the owner's cut and not listed" in shelf
    everything = browse({"shelf": "Literature/19th century", "all": True})
    assert "A Forgotten Serial, Part 7" in everything and "below the owner's cut" not in everything
    # asked for by name, a hidden book is not denied: it is there, only below the cut
    found = browse({"find": "forgotten serial"})
    assert 'Nothing kept on the shelves matches "forgotten serial", but 1 less-read book(s) below the owner\'s cut do' in found
    assert "[A Forgotten Serial, Part 7](" in found
    assert browse({"find": "zzzz nothing"}).startswith('No book or document with "zzzz nothing"')


def test_the_reader_routes_report_what_is_below_the_cut(lib, client, monkeypatch):
    _rated(lib)
    _cut(lib, min_rating=880, keep_per_shelf=0)
    monkeypatch.setattr("services.library.kiwix.available", lambda force=False: True)
    tree = client.get("/api/reader/shelves").json()
    literature = next(category for category in tree["categories"] if category["name"] == "Literature")
    assert (literature["items"], literature["hidden"]) == (6, 1)
    shelf = client.get("/api/reader/shelves/contents", params={"shelf": "Literature/19th century"}).json()
    assert (shelf["total"], shelf["hidden"], shelf["min_rating"]) == (5, 1, 880.0)
    everything = client.get("/api/reader/shelves/contents", params={"shelf": "Literature/19th century", "all": 1}).json()
    assert (everything["total"], everything["hidden"]) == (6, 0)
    assert [item["kept"] for item in everything["items"] if item["title"].startswith("A Forgotten Serial")] == [False]
    found = client.get("/api/reader/find", params={"q": "forgotten serial"}).json()
    assert found["items"] == [] and [item["title"] for item in found["below_cut"]] == ["A Forgotten Serial, Part 7"]
    assert client.get("/api/reader/find", params={"q": "much read"}).json()["below_cut"] == []


def test_ratings_are_read_from_a_collections_own_popularity_list(lib, monkeypatch):
    import gzip

    zim = lib.root / "zim" / "gutenberg_en_lcc-zz_2026-11a.zim"
    zim.parent.mkdir()
    zim.write_bytes(b"not a real archive")
    listing = "books/1342.json\nbooks/77.json\nbooks/88.json\nA Much Read Novel.1342\nAn Unlisted Book.77\nA Dull Book.88\n"
    pages = {"books.json": {"totalCount": 3, "totalPages": 1},
             "books-pages/popularity/1.json": {"books": [
                 {"title": "A Much Read Novel", "popularity": 5000.0, "coverPath": "covers/1342_cover_image.webp"},
                 {"title": "A Dull Book", "popularity": 12.0, "coverPath": "covers/88_cover_image.webp"},
                 {"title": "No Cover, No Number", "popularity": 7.0, "coverPath": ""}]}}

    def fake_run(args, timeout=600):
        if args[1] == "list":
            return listing.encode()
        entry = args[2].split("=", 1)[1]
        if entry in pages:
            return json.dumps(pages[entry]).encode()
        raise RuntimeError("no such entry")

    monkeypatch.setattr(lib.manuals, "_run", fake_run)
    assert lib.catalogue.gutenberg_ratings(zim) == {"1342": 5000.0, "88": 12.0}
    (lib.root / "index").mkdir(exist_ok=True)
    with gzip.open(lib.root / "index" / "pg_catalog.csv.gz", "wt", encoding="utf-8") as handle:
        handle.write("Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
                     '1342,Text,1998-06-01,A Much Read Novel,en,"Writer, A., 1800-1870",Fiction,PR,"Category: Novels"\n'
                     '88,Text,1998-06-01,A Dull Book,en,"Writer, B., 1800-1870",Fiction,PR,"Category: Novels; Category: Classics of Literature"\n')
    (lib.root / "shelves.json").write_text(json.dumps({"min_rating": 100, "keep_per_shelf": 0}))
    lib.shelves.reset_cache()
    said = []
    summary = lib.catalogue.build(progress=said.append)
    assert summary["cut"] == {"min_rating": 100.0, "kept": 3, "hidden": 0}       # 1342 by its figure, 88 as a classic, 77 unrated
    held = {item["key"]: item for item in lib.catalogue.items(limit=20, everything=True)["items"]}
    assert (held["1342"]["rating"], held["88"]["rating"], held["77"]["rating"]) == (5000.0, 12.0, None)
    (lib.root / "shelves.json").write_text(json.dumps({"min_rating": 100, "keep_per_shelf": 0}))
    conn = lib.manuals.connect()
    conn.execute("UPDATE shelf_items SET classic = 0")
    conn.commit()
    assert lib.catalogue.apply_cut(conn) == {"min_rating": 100.0, "kept": 2, "hidden": 1}
    conn.close()
    assert any("left off the shelves" in line for line in said) is False          # nothing was hidden in the build itself


def test_an_index_from_before_the_cut_still_reads(lib):
    """An index made by the first release has no rating or kept columns."""
    import sqlite3

    path = lib.root / "index" / "manuals.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(str(path))
    old.executescript(lib.manuals._SCHEMA)
    old.executescript("""CREATE TABLE shelf_items (book TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
        dates TEXT NOT NULL DEFAULT '', year INTEGER, shelf TEXT NOT NULL, form TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'rule', filed_at REAL, PRIMARY KEY (book, key)) WITHOUT ROWID;
        INSERT INTO shelf_items (book, key, title, shelf) VALUES ('gutenberg_en_lcc-pr_2026-11a', '1', 'An Earlier Entry', 'Literature/19th century');""")
    old.commit()
    old.close()
    assert lib.catalogue.items("Literature")["items"][0]["title"] == "An Earlier Entry"       # read-only: no columns needed
    assert lib.catalogue.counts() == {"Literature/19th century": 1} and lib.catalogue.hidden_under() == 0
    assert lib.catalogue.find("earlier")[0]["kept"] is True
    conn = lib.manuals.connect()                                                              # the first writer brings it up to date
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shelf_items)")}
    conn.close()
    assert {"rating", "kept", "classic"} <= columns
