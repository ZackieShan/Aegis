"""Documents added to the offline library after it was built.

What must hold: a PDF handed over (a file or a link) is kept, read page by
page and found by the very next search, with a link that opens that page;
the same file is never stored twice; nothing that is not a readable PDF gets
in; a link is only followed to public addresses; and the batch indexer's
clean-up never throws added documents away.
"""
import asyncio
import json

import pytest

fitz = pytest.importorskip("fitz")        # PyMuPDF: the library's PDF reader


def _pdf(pages, title="", password=None):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 96), text, fontsize=11)
    if title:
        doc.set_metadata({"title": title})
    if password:
        data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def library(tmp_path, monkeypatch):
    from services.library import additions, config, manuals, search

    monkeypatch.setattr(config, "library_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "enabled", lambda: True)
    monkeypatch.setattr("services.library.kiwix.available", lambda force=False: False)     # no Kiwix server in tests
    monkeypatch.setattr("services.library.kiwix.book_titles", lambda: {})
    started = []
    monkeypatch.setattr(additions, "_start_ocr", lambda relative: started.append(relative) or True)
    additions._ocr_running.clear()
    return type("Library", (), {"add": additions, "manuals": manuals, "search": search, "config": config,
                                "root": tmp_path, "ocr_started": started})


BEES = ["Varroa mites are treated with oxalic acid vapour when the colony is broodless.",
        "A nucleus colony is made from two frames of brood and one of stores."]


def test_an_added_pdf_is_kept_and_found_by_the_next_search(library):
    entry = library.add.add_pdf(_pdf(BEES, title="Beekeeping Field Notes"), name="bee notes (2024).pdf", section="Homestead",
                                source="uploaded: bee notes.pdf", added_by="zed")
    assert entry["status"] == "added" and entry["title"] == "Beekeeping Field Notes" and entry["pages"] == 2
    assert entry["path"] == "homestead/bee_notes_2024.pdf" and entry["url"] == "/api/reference/added/homestead/bee_notes_2024.pdf"
    assert (library.root / "added" / "homestead" / "bee_notes_2024.pdf").read_bytes().startswith(b"%PDF-")
    assert entry["ocr"] == "not needed" and library.ocr_started == []

    hits = library.manuals.search("oxalic acid varroa")
    assert [(h["book"], h["page"], h["doc_title"]) for h in hits] == [("aegis-added", 1, "Beekeeping Field Notes")]
    assert hits[0]["url"] == entry["url"] + "#page=1"
    assert library.search.owns_url(hits[0]["url"]) and not library.search.owns_url("/api/reference/search?q=x")
    page_two = library.search.fetch_content(entry["url"] + "#page=2")
    assert page_two["success"] and "nucleus colony" in page_two["content"] and "page 2" in page_two["title"]

    found = library.search.library_search("nucleus colony brood", scope="manuals")
    assert found and found[0]["book_title"] == "Added to the library"
    text, sources = library.search.format_for_model("nucleus colony", found, fetch=1)
    assert entry["url"] in text and sources[0]["url"].startswith("/api/reference/added/")
    assert library.manuals.stats()["collections"][0] == {
        "book": "aegis-added", "documents": 1, "pages": 2, "text_pages": 2, "scanned_documents": 0, "complete": True}
    listed = library.add.list_added()
    assert [(e["title"], e["added_by"], e["source"]) for e in listed] == [("Beekeeping Field Notes", "zed", "uploaded: bee notes.pdf")]


def test_the_same_file_is_never_stored_twice_and_names_do_not_collide(library):
    data = _pdf(BEES, title="Beekeeping Field Notes")
    first = library.add.add_pdf(data, name="notes.pdf")
    again = library.add.add_pdf(data, name="completely-different-name.pdf", section="medical")
    assert again["status"] == "already" and again["path"] == first["path"] == "general/notes.pdf"
    other = library.add.add_pdf(_pdf(["Different text about water filters and chlorine."]), name="notes.pdf")
    assert other["status"] == "added" and other["path"] == "general/notes-2.pdf"
    # A name that says nothing ("1.pdf") is replaced by the title.
    third = library.add.add_pdf(_pdf(["Knots for climbing and rescue work."], title="Rescue Knots"), name="1.pdf")
    assert third["path"] == "general/Rescue_Knots.pdf"
    assert len(library.add.list_added()) == 3
    assert len(list((library.root / "added").rglob("*.pdf"))) == 3


def test_only_readable_pdfs_get_in(library):
    AdditionError = library.add.AdditionError
    for bad, says in ((b"", "empty"), (b"<html><body>Not a PDF</body></html>", "not a PDF"),
                      (b"%PDF-1.7\nthis is not really a pdf at all", "could not be read")):
        with pytest.raises(AdditionError) as refused:
            library.add.add_pdf(bad, name="x.pdf")
        assert says in str(refused.value)
    with pytest.raises(AdditionError) as refused:
        library.add.add_pdf(_pdf(BEES, password="secret"), name="locked.pdf")
    assert "password-protected" in str(refused.value)
    assert library.add.list_added() == [] and not list(library.root.rglob("*.pdf"))


def test_a_title_comes_from_the_document_when_none_is_given(library):
    untitled = library.add.add_pdf(_pdf(["Emergency Water Disinfection Guide\nBoil for one minute."]), name="scan0001.pdf")
    assert untitled["title"].startswith("Emergency Water Disinfection Guide")
    named = library.add.add_pdf(_pdf(["Some other body text for the second file."]), name="x.pdf", title="  My   Own Title ")
    assert named["title"] == "My Own Title"


def test_removing_takes_the_file_the_pages_and_the_entry(library):
    library.add.add_pdf(_pdf(BEES, title="Beekeeping Field Notes"), name="bees.pdf")
    kept = library.add.add_pdf(_pdf(["Chlorine dosing for water storage tanks."], title="Water Storage"), name="water.pdf")
    with pytest.raises(library.add.AdditionError):
        library.add.remove("nothing like this")
    assert library.add.remove("beekeeping field notes") == {"removed": "Beekeeping Field Notes", "path": "general/bees.pdf"}
    assert not (library.root / "added" / "general" / "bees.pdf").exists()
    assert library.manuals.search("oxalic acid varroa") == []
    assert [h["doc_title"] for h in library.manuals.search("chlorine water storage")] == ["Water Storage"]
    assert [e["path"] for e in library.add.list_added()] == [kept["path"]]
    assert library.add.find(kept["url"] + "#page=1")["path"] == kept["path"]            # by its link, too


def test_only_files_inside_the_added_folder_are_ever_served(library):
    entry = library.add.add_pdf(_pdf(BEES), name="bees.pdf")
    (library.root / "secret.pdf").write_bytes(b"%PDF-1.7 not for serving")
    assert library.add.file_for(entry["path"]).name == "bees.pdf"
    for sneaky in ("../secret.pdf", "general/../../secret.pdf", str(library.root / "secret.pdf"), "catalogue.json",
                   "general", "general/missing.pdf", ""):
        assert library.add.file_for(sneaky) is None


def test_the_batch_indexers_cleanup_leaves_added_documents_alone(library):
    library.add.add_pdf(_pdf(BEES, title="Beekeeping Field Notes"), name="bees.pdf")
    (library.root / "zim").mkdir()
    conn = library.manuals.connect()
    conn.execute("INSERT INTO sources (book, zim_file, size, mtime, pdf_count, complete) VALUES ('gone_collection', 'gone.zim', 1, 1, 0, 1)")
    conn.commit()
    conn.close()
    library.manuals.index_all()                         # forgets collections whose ZIM is no longer installed
    conn = library.manuals.connect(readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    conn.close()
    assert [h["doc_title"] for h in library.manuals.search("oxalic acid varroa")] == ["Beekeeping Field Notes"]


def test_a_scanned_document_is_queued_for_ocr_and_becomes_searchable(library, monkeypatch):
    scanned = _pdf(["", "", ""], title="Old Scanned Manual")                 # pages without any text
    entry = library.add.add_pdf(scanned, name="scan.pdf")
    assert entry["ocr"] == "pending" and "OCR" in entry["note"] and library.ocr_started == ["general/scan.pdf"]
    assert library.manuals.search("tourniquet") == []
    monkeypatch.setattr(library.manuals, "extract_pdf",
                        lambda data, ocr=False: ("", ["Apply the tourniquet high and tight.", "",
                                                       "Write the time it was applied on the casualty card."], 2))
    library.add._ocr("general/scan.pdf")                                     # what the background thread runs
    listed = library.add.list_added()[0]
    assert listed["ocr"] == "done" and listed["text_pages"] == 2 and listed["ocr_pages"] == 2
    assert [h["page"] for h in library.manuals.search("tourniquet high tight")] == [1]
    # Reading cut short by a restart is picked up again.
    library.add._update_entry("general/scan.pdf", ocr="running")
    library.ocr_started.clear()
    assert library.add.resume_ocr() == 1 and library.ocr_started == ["general/scan.pdf"]


def test_a_link_is_downloaded_only_from_public_addresses_and_must_be_a_pdf(library, monkeypatch):
    AdditionError = library.add.AdditionError
    for private in ("http://127.0.0.1:9/manual.pdf", "http://localhost/manual.pdf", "http://192.168.1.10/manual.pdf",
                    "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(AdditionError) as refused:
            library.add.add_from_url(private)
        assert "could not be downloaded" in str(refused.value)
    for not_a_link in ("manual.pdf", "ftp://example.org/manual.pdf", "file:///etc/passwd", ""):
        with pytest.raises(AdditionError):
            library.add.add_from_url(not_a_link)

    monkeypatch.setattr(library.add, "_download", lambda url, **_: (b"<html>landing page</html>", url, "manual.pdf"))
    with pytest.raises(AdditionError) as refused:
        library.add.add_from_url("https://example.org/manuals/water")
    assert "web page, not a PDF" in str(refused.value)

    monkeypatch.setattr(library.add, "_download",
                        lambda url, **_: (_pdf(BEES, title="Beekeeping Field Notes"), "https://cdn.example.org/files/bees.pdf", "bees.pdf"))
    entry = library.add.add_from_url("https://example.org/get?id=7", section="homestead", added_by="zed")
    assert entry["status"] == "added" and entry["source"] == "https://cdn.example.org/files/bees.pdf"
    assert entry["path"] == "homestead/bees.pdf"


def test_the_assistants_tool_adds_lists_and_removes(library, tmp_path):
    from src.agent_tools.library_tools import ManageLibraryTool

    tool = ManageLibraryTool()
    upload = tmp_path / "uploads" / "20261122_bees.pdf"
    upload.parent.mkdir()
    upload.write_bytes(_pdf(BEES, title="Beekeeping Field Notes"))
    notes = tmp_path / "uploads" / "notes.txt"
    notes.write_text("just some notes")
    call = lambda body: asyncio.run(tool.execute(json.dumps(body), {"owner": "zed"}))      # noqa: E731

    result = call({"action": "add", "files": [str(upload), str(notes), str(tmp_path / "missing.pdf")], "section": "homestead"})
    assert result["exit_code"] == 0 and "1 of 3 document(s) are in the library." in result["output"]
    assert "Added [Beekeeping Field Notes](/api/reference/added/homestead/20261122_bees.pdf) — 2 pages, section homestead" in result["output"]
    assert "NOT added" in result["output"] and "not a PDF" in result["output"] and "no file at" in result["output"]
    assert "Already in the library" in call({"action": "add", "files": [str(upload)]})["output"]
    assert "Beekeeping Field Notes" in call({"action": "list"})["output"]
    assert "homestead" in call({"action": "sections"})["output"]
    assert call({"action": "add"})["exit_code"] == 1 and call({"action": "burn"})["exit_code"] == 1
    assert "Removed “Beekeeping Field Notes”" in call({"action": "remove", "document": "Beekeeping Field Notes"})["output"]
    assert call({"action": "list"})["output"] == "Nothing has been added to the library yet."
    assert library.add.list_added() == []

    # A chat upload is stored under an id; it is filed under the name it was uploaded as.
    stored = tmp_path / "uploads" / "d77685cd8ae64ea4.pdf"
    stored.write_bytes(_pdf(["Smoker fuel: dry pine needles and burlap burn cool."], title="Apiary Smoker Notes"))
    (tmp_path / "uploads" / "uploads.json").write_text(json.dumps({
        "zed:h": {"id": "d77685cd8ae64ea4.pdf", "path": str(stored), "owner": "zed", "original_name": "apiary smoker notes.pdf"}}))
    from src import constants
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(constants, "UPLOAD_DIR", str(tmp_path / "uploads"))
        by_id = call({"action": "add", "files": ["d77685cd8ae64ea4.pdf"]})["output"]
        assert by_id == ("- Added [Apiary Smoker Notes](/api/reference/added/general/apiary_smoker_notes.pdf) — 1 page, "
                         "section general. Searchable now.")
        by_path = call({"action": "add", "files": [str(stored)]})["output"]             # the path the model is shown
        assert by_path.startswith("- Already in the library: [Apiary Smoker Notes]")
        assert library.add.list_added()[0]["source"] == "uploaded: apiary smoker notes.pdf"


def test_the_tool_is_for_administrators_only():
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, blocked_tools_for_owner

    assert "manage_library" in NON_ADMIN_BLOCKED_TOOLS
    assert "manage_library" in blocked_tools_for_owner("someone-who-is-not-an-admin")


def test_a_web_page_is_refused_with_its_pdf_links_or_kept_as_a_document_on_request(library, monkeypatch):
    page = (b"<html><head><title>Snake bite first aid</title></head><body><h1>Snake bite first aid</h1>"
            b"<p>" + b"Keep the person calm and still. Remove rings and watches. " * 20 + b"</p>"
            b'<a href="/files/snakebite-guide.pdf">Download the guide</a> <a href="guide.html">HTML</a>'
            b'<a href="https://cdn.example.org/a%20b.PDF?v=2&amp;x=1">Poster</a><a href="/files/snakebite-guide.pdf">again</a>'
            b"</body></html>")
    monkeypatch.setattr(library.add, "_download", lambda url, **_: (page, "https://health.example.org/snakes/", "index.html"))
    with pytest.raises(library.add.AdditionError) as refused:
        library.add.add_from_url("https://health.example.org/snakes")
    message = str(refused.value)
    assert "web page, not a PDF file" in message and '"as_page": true' in message
    assert "https://health.example.org/files/snakebite-guide.pdf , https://cdn.example.org/a%20b.PDF?v=2&x=1 ." in message
    assert library.add.pdf_links(b"<a href='x.html'>no pdf here</a>", "https://example.org/") == []
    assert library.add.list_added() == []

    kept = library.add.add_from_url("https://health.example.org/snakes", as_page=True, section="medical", added_by="zed")
    assert kept["status"] == "added" and kept["title"] == "Snake bite first aid" and kept["section"] == "medical"
    assert kept["source"] == "https://health.example.org/snakes/" and kept["path"] == "medical/Snake_bite_first_aid.pdf"
    hits = library.manuals.search("remove rings watches calm")
    assert hits and hits[0]["doc_title"] == "Snake bite first aid"
    assert "Saved from https://health.example.org/snakes/" in library.search.fetch_content(kept["url"])["content"]

    monkeypatch.setattr(library.add, "_download", lambda url, **_: (b"<html><body>Loading...</body></html>", url, ""))
    with pytest.raises(library.add.AdditionError) as refused:
        library.add.add_from_url("https://app.example.org/", as_page=True)
    assert "too little readable text" in str(refused.value)
