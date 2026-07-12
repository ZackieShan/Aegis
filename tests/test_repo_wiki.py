"""Unit tests for the repo → wiki generator — src/repo_wiki.py.

Covers the dependency-free repo scan/digest logic, the safety of the saved-wiki
store, and a fully-mocked generate (no model, no network).
"""
import asyncio
import os

import pytest

from src import repo_wiki as rw


def _make_repo(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nA tiny demo project.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "util.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    # things that must be skipped
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    (tmp_path / "model.gguf").write_bytes(b"\x00\x01\x02")
    dotdir = tmp_path / ".git"
    dotdir.mkdir()
    (dotdir / "config").write_text("[core]\n", encoding="utf-8")
    return tmp_path


# ── scan ───────────────────────────────────────────────────────────────────────
def test_scan_repo_tree_and_skips(tmp_path):
    _make_repo(tmp_path)
    scan = rw.scan_repo(str(tmp_path))
    tree = scan["tree"]
    assert "README.md" in tree
    assert "app.py" in tree
    assert "src/util.py" in tree
    # skipped dirs / binary extensions never appear
    assert not any("node_modules" in t for t in tree)
    assert not any(t.endswith(".gguf") for t in tree)
    assert not any(".git" in t for t in tree)
    assert scan["langs"].get("Python") == 2  # app.py + src/util.py (README.md isn't Python)
    # README is captured as a key file
    assert any(rel.lower().endswith("readme.md") for rel, _ in scan["key_files"])


def test_scan_repo_rejects_non_directory(tmp_path):
    with pytest.raises(ValueError):
        rw.scan_repo(str(tmp_path / "nope"))


def test_digest_text_includes_tree_and_files(tmp_path):
    _make_repo(tmp_path)
    scan = rw.scan_repo(str(tmp_path))
    with_files = rw._digest_text(scan, include_files=True)
    tree_only = rw._digest_text(scan, include_files=False)
    assert "File tree:" in with_files and "app.py" in with_files
    assert "Key files:" in with_files
    assert "Key files:" not in tree_only


def test_slug():
    assert rw._slug("My Repo!") == "my-repo"
    assert rw._slug("  ...  ") == "wiki"


# ── generate (mocked model) ────────────────────────────────────────────────────
def test_generate_wiki_happy_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    wikis = tmp_path / "wikis"
    monkeypatch.setattr(rw, "WIKIS_DIR", str(wikis))

    async def _fake_gen(model_spec, system, user, owner):
        if "Overview" in user:
            return "It is a demo."
        if "Architecture" in user:
            return "One entrypoint, one helper."
        return "- app.py: entrypoint"

    import src.recipes as recipes
    monkeypatch.setattr(recipes, "_model_generate", _fake_gen)

    res = asyncio.run(rw.generate_wiki(str(repo), "qwen3-coder-30b"))
    assert res["ok"] is True
    md = res["markdown"]
    assert "## Overview" in md and "It is a demo." in md
    assert "## Architecture" in md and "## Module guide" in md
    assert "## File tree" in md
    # saved to the store
    assert res["saved_path"] and os.path.exists(res["saved_path"])
    assert rw.get_wiki(res["name"]) == md
    assert any(w["name"] == rw._slug(res["name"]) for w in rw.list_wikis())


def test_generate_wiki_requires_model(tmp_path):
    res = asyncio.run(rw.generate_wiki(str(tmp_path), ""))
    assert res["ok"] is False and "model" in res["error"].lower()


def test_generate_wiki_bad_path(tmp_path):
    res = asyncio.run(rw.generate_wiki(str(tmp_path / "missing"), "m"))
    assert res["ok"] is False and "not a directory" in res["error"]


def test_generate_wiki_empty_repo(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = asyncio.run(rw.generate_wiki(str(empty), "m"))
    assert res["ok"] is False and "no source files" in res["error"]


# ── store safety ───────────────────────────────────────────────────────────────
def test_get_wiki_no_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "WIKIS_DIR", str(tmp_path / "wikis"))
    os.makedirs(rw.WIKIS_DIR, exist_ok=True)
    # a traversal attempt is slugified into a harmless filename → simply not found
    assert rw.get_wiki("../../secret") is None
