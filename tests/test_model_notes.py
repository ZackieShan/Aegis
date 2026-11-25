"""A sentence on each model in the picker: what it is and what to use it for.

With nothing written, the note says only what the model's name says — its
kind, its size, whether it is an uncensored build — and makes no claim about
quality. What the administrator writes replaces it, for everyone.
"""
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def notes(tmp_path, monkeypatch):
    from services import model_notes

    monkeypatch.setattr(model_notes, "_path", lambda: str(tmp_path / "model_notes.json"))
    return model_notes


@pytest.mark.parametrize("name, summary, use", [
    ("gpt-oss-120b", "Large general model (120B parameters)", "Demanding work: long documents, careful reasoning, agent tasks"),
    ("qwen3-coder-next-80b", "Coding model (80B parameters)", "Writing, reviewing and refactoring code"),
    ("qwen3.6-40b-deckard-vision", "Vision model: reads images as well as text (40B parameters)", "Questions about photos, screenshots and scanned pages"),
    ("qwen3.5-4b-uncensored", "Small general model (4B parameters, uncensored build)", "Quick, simple tasks; limited knowledge and reasoning"),
    ("some-org/Mistral-Nemo-12B-Instruct", "Mid-size general model (12B parameters)", "Everyday questions and drafting"),
    ("wan2.2-t2v", "Video generation model", "Making short clips from text or images; not a chat model"),
    ("qwen-image-edit", "Image editing model", "Changing an existing picture from an instruction; not a chat model"),
    ("qwen-image", "Image generation model", "Making pictures from text; not a chat model"),
    ("chatterbox-tts", "Text-to-speech voice", "Reading replies aloud; not a chat model"),
    ("bge-m3", "Embedding model", "Search and recall behind the scenes; not a chat model"),
    ("chat-lite-cpu", "General model (runs on the CPU only, tuned for speed)", ""),
])
def test_a_name_says_what_kind_of_model_it_is(notes, name, summary, use):
    assert notes.from_name(name) == {"summary": summary, "use": use}


@pytest.mark.parametrize("name", ["mystery-model", "openai/gpt-5", "claude-fable-5-2", "", None, "v2", "model-2026-11-22"])
def test_a_name_that_says_nothing_gets_no_note(notes, name):
    assert notes.from_name(name) == {"summary": "", "use": ""}
    assert notes.notes_for([name] if name else []) == {}


def test_what_the_administrator_writes_replaces_the_guess(notes, tmp_path):
    assert notes.note_for("gpt-oss-120b")["written"] is False
    saved = notes.set_note("gpt-oss-120b", "  The main model here:\n about 50 tokens a second. ", "Everyday default")
    assert saved == {"summary": "The main model here: about 50 tokens a second.", "use": "Everyday default", "written": True}
    assert json.loads((tmp_path / "model_notes.json").read_text()) == {"gpt-oss-120b": {"summary": "The main model here: about 50 tokens a second.", "use": "Everyday default"}}
    # a model listed with its provider in front is the same model
    assert notes.note_for("local/gpt-oss-120b")["summary"].startswith("The main model here")
    # one field written: the other still comes from the name
    notes.set_note("qwen3-coder-next-80b", "", "Long coding sessions")
    assert notes.note_for("qwen3-coder-next-80b") == {"summary": "Coding model (80B parameters)", "use": "Long coding sessions", "written": True}
    found = notes.notes_for(["gpt-oss-120b", "gpt-oss-120b", "mystery-model", "wan2.2-t2v", ""])
    assert list(found) == ["gpt-oss-120b", "wan2.2-t2v"] and found["wan2.2-t2v"]["written"] is False
    # both empty: back to the automatic note
    assert notes.set_note("gpt-oss-120b", "", "") == {"summary": "Large general model (120B parameters)", "use": "Demanding work: long documents, careful reasoning, agent tasks", "written": False}
    assert "gpt-oss-120b" not in json.loads((tmp_path / "model_notes.json").read_text())
    assert len(notes.set_note("x-7b", "s" * 900, "u" * 900)["summary"]) == notes.MAX_LEN
    with pytest.raises(ValueError):
        notes.set_note("  ", "a", "b")


def test_a_damaged_notes_file_is_treated_as_empty(notes, tmp_path):
    (tmp_path / "model_notes.json").write_text("{not json")
    assert notes.note_for("gpt-oss-120b")["summary"] == "Large general model (120B parameters)"
    (tmp_path / "model_notes.json").write_text('["a list"]')
    assert notes.notes_for(["qwen-image"])["qwen-image"]["written"] is False
    (tmp_path / "model_notes.json").write_text('{"qwen-image": "not an object", "wan2.2-t2v": {"summary": 5}}')
    assert notes.note_for("qwen-image")["written"] is False and notes.note_for("wan2.2-t2v")["summary"] == "5"


def test_anyone_signed_in_reads_the_notes_and_only_an_administrator_writes_them(notes, monkeypatch):
    import routes.model_notes_routes as routes

    who = {"user": "mara", "admin": False}

    def require_user(request):
        if not who["user"]:
            raise HTTPException(401, "Not authenticated")
        return who["user"]

    def require_admin(request):
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(routes, "require_user", require_user)
    monkeypatch.setattr(routes, "require_admin", require_admin)
    app = FastAPI()
    app.include_router(routes.setup_model_notes_routes())
    client = TestClient(app)

    looked = client.post("/api/model-notes/lookup", json={"models": ["gpt-oss-120b", "mystery-model", 7]})
    assert looked.status_code == 200 and list(looked.json()["notes"]) == ["gpt-oss-120b"]
    assert client.post("/api/model-notes/lookup", json={"models": "gpt-oss-120b"}).status_code == 400
    assert client.put("/api/model-notes", json={"model": "gpt-oss-120b", "summary": "Mine", "use": "Everything"}).status_code == 403
    assert notes.note_for("gpt-oss-120b")["written"] is False                      # nothing was written
    who["admin"] = True
    written = client.put("/api/model-notes", json={"model": "gpt-oss-120b", "summary": "The main model", "use": "Everything"})
    assert written.status_code == 200 and written.json()["note"]["written"] is True
    assert client.put("/api/model-notes", json={"model": "", "summary": "x"}).status_code == 400
    assert client.post("/api/model-notes/lookup", json={"models": ["gpt-oss-120b"]}).json()["notes"]["gpt-oss-120b"]["summary"] == "The main model"
    who["user"] = ""
    assert client.post("/api/model-notes/lookup", json={"models": ["gpt-oss-120b"]}).status_code == 401
