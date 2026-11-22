"""The per-user profile: who each user is, written from their own memories.

Covers the rules that matter:
  * one profile per account, kept beside that memory store, never shared;
  * it is given to the model on every turn — except in Nobody mode or with
    memory off;
  * it never outlives its sources: delete or edit a memory and the written
    profile is set aside at once;
  * the model rewrites it only when there is something new, not on every chat.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import routes.memory_routes as memory_routes
from services.memory import profile
from src.chat_processor import ChatProcessor
from src.memory import MemoryManager


@pytest.fixture(autouse=True)
def _fresh_bookkeeping():
    profile._last_attempt.clear()
    profile._rebuilding.clear()
    yield
    profile._last_attempt.clear()
    profile._rebuilding.clear()


def _store(tmp_path, rows):
    """A real memory store holding (owner, text, category[, pinned]) rows."""
    manager = MemoryManager(str(tmp_path))
    entries = []
    for row in rows:
        owner, text, category = row[:3]
        entry = manager.add_entry(text, category=category, owner=owner)
        if len(row) > 3 and row[3]:
            entry["pinned"] = True
        entries.append(entry)
    manager.save(entries)
    return manager


ALICE = [
    ("alice", "Name is Alice Moreno", "identity", True),
    ("alice", "Lives outside Flagstaff", "identity"),
    ("alice", "Married to Ben", "contact"),
    ("alice", "Prefers short answers with metric units", "preference"),
    ("alice", "Keeps bees: two hives, wants a third", "interest"),
    ("alice", "Restoring a 1978 Land Cruiser", "project"),
    ("alice", "Wants to pass the ham radio General exam", "goal"),
    ("alice", "Asked about varroa treatment in March", "event"),
]
BOB = [
    ("bob", "Name is Bob Castellanos", "identity", True),
    ("bob", "Collects vintage synthesizers", "interest"),
]

WRITTEN = {
    "summary": "Alice lives near Flagstaff with her husband Ben. She keeps bees and restores an old Land Cruiser.",
    "identity": ["Name is Alice Moreno", "Lives outside Flagstaff"],
    "people": ["Ben, her husband"],
    "preferences": ["Short answers", "Metric units"],
    "interests": [
        {"name": "Beekeeping", "notes": ["Two hives, wants a third", "Dealt with varroa in March"]},
        {"name": "Ham radio", "notes": ["Studying for the General exam"]},
    ],
    "projects": ["Restoring a 1978 Land Cruiser"],
    "goals": ["Pass the ham radio General exam"],
}


def _model_says(monkeypatch, reply, calls=None):
    async def fake_llm(url, model, messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)


def _rebuild(manager, owner="alice"):
    return asyncio.run(profile.rebuild_profile(owner, manager, "http://local/v1", "util"))


# ── shaping ────────────────────────────────────────────────────────────────

def test_normalize_coerces_model_output_and_enforces_limits():
    raw = {
        "summary": "  Alice   keeps\nbees.  ",
        "identity": ["- Name is Alice", "name is alice", "", 7, "x" * 400],
        "people": "Ben",                                   # wrong type → dropped
        "preferences": [f"pref {i}" for i in range(40)],
        "interests": ["Bees", {"name": "bees", "notes": ["dup"]}, {"name": "Radio", "notes": ["a", "a", 3, "b"]},
                      {"notes": ["nameless"]}] + [{"name": f"topic {i}"} for i in range(30)],
        "nonsense": {"ignored": True},
    }
    out = profile.normalize(raw)
    assert out["summary"] == "Alice keeps bees."
    assert out["identity"][0] == "Name is Alice" and len(out["identity"]) == 2      # duplicate, blank, int gone
    assert len(out["identity"][1]) <= profile.MAX_ITEM_CHARS
    assert out["people"] == []
    assert len(out["preferences"]) == 12
    assert [i["name"] for i in out["interests"][:2]] == ["Bees", "Radio"]
    assert out["interests"][1]["notes"] == ["a", "b"]
    assert len(out["interests"]) == profile.MAX_INTERESTS
    assert "nonsense" not in out
    assert profile.normalize("not a dict") == {} and profile.is_empty(profile.normalize({}))


def test_parse_object_survives_reasoning_fences_and_chatter():
    reply = '<think>{"not": "this"}</think>Here you go:\n```json\n{"summary": "S {braces} \\"quoted\\""}\n```\nDone.'
    assert profile._parse_object(reply) == {"summary": 'S {braces} "quoted"'}
    assert profile._parse_object("no json here") == {}
    assert profile._parse_object("[1, 2]") == {}


def test_assembled_profile_is_built_from_categories_without_a_model(tmp_path):
    manager = _store(tmp_path, ALICE)
    out = profile.assemble(manager.load(owner="alice"))
    assert out["identity"][0] == "Name is Alice Moreno"                 # pinned first
    assert out["people"] == ["Married to Ben"]
    assert out["preferences"] == ["Prefers short answers with metric units"]
    assert out["interests"] == [{"name": "Keeps bees: two hives, wants a third", "notes": []}]
    assert out["projects"] and out["goals"]
    assert "varroa" not in json.dumps(out)                              # one-off events stay out


def test_prompt_block_is_stable_bulleted_and_within_budget():
    block = profile.render_for_prompt(profile.normalize(WRITTEN))
    assert block == profile.render_for_prompt(profile.normalize(WRITTEN))    # same bytes every turn
    lines = block.splitlines()
    assert lines[0].startswith("Who you are talking to")
    assert all(line.startswith("- ") for line in lines[1:])
    assert "- Interests: Beekeeping, Ham radio" in lines
    assert "- Beekeeping: Two hives, wants a third; Dealt with varroa in March" in lines
    assert profile.render_for_prompt({}) == ""
    huge = profile.normalize({"summary": "s", "identity": [f"{n} " + "i" * 170 for n in range(10)],
                              "preferences": [f"{n} " + "p" * 170 for n in range(12)]})
    assert len(profile.render_for_prompt(huge, budget=600)) <= 600 + 200      # never runs far past the budget


def test_long_sections_cannot_push_the_interests_out_of_the_prompt_block():
    crowded = profile.normalize({
        "summary": "S" * 500,
        "identity": [f"identity {n} " + "x" * 150 for n in range(10)],
        "people": [f"person {n} " + "x" * 150 for n in range(10)],
        "preferences": [f"preference {n} " + "x" * 150 for n in range(12)],
        "projects": [f"project {n} " + "x" * 150 for n in range(8)],
        "goals": [f"goal {n} " + "x" * 150 for n in range(8)],
        "interests": [{"name": name, "notes": [f"{name} note {n} " + "x" * 100 for n in range(6)]}
                      for name in ("Beekeeping", "Ham radio", "Sourdough")],
    })
    block = profile.render_for_prompt(crowded)
    lines = block.splitlines()
    assert len(block) <= 3200 + 1
    assert "- Interests: Beekeeping, Ham radio, Sourdough" in lines            # named right after who they are
    assert lines.index("- Interests: Beekeeping, Ham radio, Sourdough") == 3
    for label in ("- About them:", "- Preferences:", "- People:", "- Projects:", "- Goals:", "- Beekeeping:"):
        assert any(line.startswith(label) for line in lines), label
    # Whole items only — a section is shortened by dropping items, never by cutting one in half.
    about = next(line for line in lines if line.startswith("- About them:"))
    assert about.endswith("x" * 150) and "identity 0" in about and "identity 9" not in about


def test_markdown_view_reads_as_second_person():
    text = profile.to_markdown(profile.normalize(WRITTEN))
    assert "**About you**" in text and "- **Beekeeping**: Two hives" in text
    assert "Nothing is known about you yet" in profile.to_markdown({})


# ── storage and isolation ──────────────────────────────────────────────────

def test_profiles_live_beside_the_memory_file_one_per_account(tmp_path):
    manager = _store(tmp_path, ALICE + BOB)
    alice = profile.get_profile("alice", manager)
    bob = profile.get_profile("bob", manager)
    assert (tmp_path / "profiles" / "alice.json").exists() and (tmp_path / "profiles" / "bob.json").exists()
    assert "Alice" in json.dumps(alice) and "Bob" not in json.dumps(alice)
    assert "synthesizers" in json.dumps(bob) and "bees" not in json.dumps(bob)
    assert profile.profile_path("../../etc/passwd", manager).parent == tmp_path / "profiles"
    assert profile.profile_path("Alice", manager) == profile.profile_path("alice", manager)


def test_a_mocked_memory_store_keeps_nothing_on_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock = MagicMock()
    assert profile.profiles_dir(mock) is None
    assert profile.get_profile("alice", mock) == {}
    profile.save_profile("alice", {"summary": "x"}, mock)
    assert profile.load_profile("alice", mock) == {} and not any(tmp_path.iterdir())


def test_no_memories_means_no_profile(tmp_path):
    manager = _store(tmp_path, BOB)
    assert profile.get_profile("alice", manager) == {}
    assert not (tmp_path / "profiles" / "alice.json").exists()
    assert not profile.needs_rebuild("alice", manager.load(owner="alice"), manager)


# ── the model-written profile ──────────────────────────────────────────────

def test_rebuild_asks_the_model_with_only_this_users_notes_and_stores_the_result(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE + BOB)
    calls = []
    _model_says(monkeypatch, WRITTEN, calls)
    out = _rebuild(manager)
    assert out["source"] == "model" and out["summary"].startswith("Alice lives near Flagstaff")
    assert out["owner"] == "alice" and out["memory_count"] == len(ALICE)
    prompt = calls[0][1]["content"]
    assert "Name is Alice Moreno (pinned)" in prompt and "[interest]" in prompt
    assert "Bob" not in prompt and "synthesizers" not in prompt            # never another account's notes
    assert profile.get_profile("alice", manager)["summary"] == out["summary"]
    assert (tmp_path / "profiles" / "alice.json").stat().st_mode & 0o077 == 0


def test_rebuild_falls_back_to_the_assembled_profile_when_the_model_fails(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    _model_says(monkeypatch, RuntimeError("backend down"))
    out = _rebuild(manager)
    assert out["source"] == "assembled" and out["people"] == ["Married to Ben"]
    profile._last_attempt.clear()
    _model_says(monkeypatch, "I'd rather not produce JSON today.")
    assert _rebuild(manager)["source"] == "assembled"


def test_a_failed_rebuild_keeps_a_good_written_profile(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    _model_says(monkeypatch, WRITTEN)
    assert _rebuild(manager)["source"] == "model"
    _model_says(monkeypatch, RuntimeError("backend down"))
    out = _rebuild(manager)
    assert out["source"] == "model" and out["summary"].startswith("Alice lives near Flagstaff")


def test_deleting_a_memory_sets_the_written_profile_aside_immediately(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    _model_says(monkeypatch, WRITTEN)
    _rebuild(manager)
    assert "Land Cruiser" in json.dumps(profile.get_profile("alice", manager))

    manager.save([m for m in manager.load_all() if "Land Cruiser" not in m["text"]])
    now = profile.get_profile("alice", manager)
    assert now["source"] == "assembled"
    assert "Land Cruiser" not in json.dumps(now)                            # gone from what the model is told
    assert "Land Cruiser" not in (tmp_path / "profiles" / "alice.json").read_text()   # …and from disk


def test_editing_a_memory_also_sets_the_written_profile_aside(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    _model_says(monkeypatch, WRITTEN)
    _rebuild(manager)
    entries = manager.load_all()
    next(m for m in entries if m["text"] == "Married to Ben")["text"] = "Married to Dana"
    manager.save(entries)
    now = profile.get_profile("alice", manager)
    assert now["source"] == "assembled" and now["people"] == ["Married to Dana"]


def test_new_memories_keep_the_profile_and_only_enough_of_them_trigger_a_rewrite(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    _model_says(monkeypatch, WRITTEN)
    _rebuild(manager)
    later = profile.load_profile("alice", manager)["attempted"] + profile.MIN_ATTEMPT_INTERVAL_S + 1
    profile._last_attempt.clear()

    def add(text):
        entries = manager.load_all()
        entries.append(manager.add_entry(text, category="fact", owner="alice"))
        manager.save(entries)
        return manager.load(owner="alice")

    assert not profile.needs_rebuild("alice", manager.load(owner="alice"), manager, now=later)   # nothing new
    one = add("Bought a refractometer")
    assert profile.get_profile("alice", manager)["source"] == "model"       # additions never set it aside
    assert not profile.needs_rebuild("alice", one, manager, now=later)       # one new fact can wait…
    assert profile.needs_rebuild("alice", one, manager, now=later + profile.STALE_AFTER_S)   # …but not forever
    two = add("Joined the county beekeepers association")
    assert profile.needs_rebuild("alice", two, manager, now=later)
    assert not profile.needs_rebuild("alice", two, manager, now=later - profile.MIN_ATTEMPT_INTERVAL_S)  # throttled


def test_memories_beyond_the_note_caps_do_not_look_new_forever(tmp_path, monkeypatch):
    rows = ALICE + [("alice", f"Went to event number {i}", "event") for i in range(60)]
    manager = _store(tmp_path, rows)
    calls = []
    _model_says(monkeypatch, WRITTEN, calls)
    _rebuild(manager)
    shown = calls[0][1]["content"]
    assert shown.count("Went to event number") == profile._NOTE_CAPS["event"]     # only the newest of the 61 events
    assert "Went to event number 59" in shown and "Went to event number 3\n" not in shown
    stored = profile.load_profile("alice", manager)
    assert len(stored["seen"]) == len(rows) and len(stored["basis"]) < len(rows)
    profile._last_attempt.clear()
    later = stored["attempted"] + profile.MIN_ATTEMPT_INTERVAL_S + 1
    assert not profile.needs_rebuild("alice", manager.load(owner="alice"), manager, now=later)


def test_a_memory_changed_while_the_model_was_writing_is_not_vouched_for(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)

    async def slow_model(url, model, messages, **kwargs):
        manager.save([m for m in manager.load_all() if "Land Cruiser" not in m["text"]])   # deleted mid-rebuild
        return json.dumps(WRITTEN)

    monkeypatch.setattr("src.llm_core.llm_call_async", slow_model)
    out = _rebuild(manager)
    assert out["source"] == "assembled" and "Land Cruiser" not in json.dumps(out)


def test_only_one_rebuild_per_user_runs_at_a_time(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE)
    started = []

    async def slow_model(url, model, messages, **kwargs):
        started.append(1)
        await asyncio.sleep(0.05)
        return json.dumps(WRITTEN)

    monkeypatch.setattr("src.llm_core.llm_call_async", slow_model)

    async def both():
        return await asyncio.gather(
            profile.rebuild_profile("alice", manager, "http://local/v1", "util"),
            profile.maybe_rebuild("alice", manager, "http://local/v1", "util"),
        )

    asyncio.run(both())
    assert len(started) == 1 and not profile._rebuilding


# ── what the model is given on a chat turn ─────────────────────────────────

def _preface(manager, owner="alice", **kwargs):
    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=MagicMock())
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={})
    options = dict(message="what should I do this weekend?", session=session, use_web=False, use_rag=False,
                   use_skills=False, owner=owner)
    options.update(kwargs)
    preface, _, _ = processor.build_context_preface(**options)
    return preface, processor


def _profile_blocks(preface):
    return [m for m in preface if (m.get("metadata") or {}).get("source") == profile.SOURCE_LABEL]


def test_the_profile_is_in_every_turn_as_untrusted_user_role_context(tmp_path, monkeypatch):
    manager = _store(tmp_path, ALICE + BOB)
    _model_says(monkeypatch, WRITTEN)
    _rebuild(manager)
    preface, processor = _preface(manager)
    blocks = _profile_blocks(preface)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["role"] == "user" and block["metadata"]["trusted"] is False
    assert "Who you are talking to" in block["content"] and "Beekeeping" in block["content"]
    assert "Bob" not in json.dumps(preface) and "synthesizers" not in json.dumps(preface)
    # It comes before the per-turn recall, so the stable part of the prompt stays a prefix.
    sources = [(m.get("metadata") or {}).get("source") for m in preface]
    assert sources.index(profile.SOURCE_LABEL) < sources.index("saved memory: pinned user facts")
    assert processor._last_used_memories[0]["type"] == "profile"
    # The same turn again is byte-identical.
    assert _profile_blocks(_preface(manager)[0])[0]["content"] == block["content"]


def test_each_account_gets_its_own_profile(tmp_path):
    manager = _store(tmp_path, ALICE + BOB)
    bob = json.dumps(_preface(manager, owner="bob")[0])
    assert "Bob Castellanos" in bob and "synthesizers" in bob
    assert "Alice" not in bob and "bees" not in bob and "Flagstaff" not in bob


def test_nobody_mode_and_memory_off_leave_the_profile_out(tmp_path):
    manager = _store(tmp_path, ALICE)
    assert _profile_blocks(_preface(manager)[0])                                    # normal turn: there
    assert not _profile_blocks(_preface(manager, incognito=True, use_memory=False)[0])
    assert not _profile_blocks(_preface(manager, incognito=True, use_profile=True)[0])   # Nobody always wins
    assert not _profile_blocks(_preface(manager, use_memory=False)[0])              # memory switched off
    assert "Alice" not in json.dumps(_preface(manager, incognito=True, use_memory=False)[0])
    # Nothing was written for a user who only ever chats as Nobody.
    (tmp_path / "other").mkdir()
    other = _store(tmp_path / "other", ALICE)
    _preface(other, incognito=True, use_memory=False)
    assert not (tmp_path / "other" / "profiles").exists()


def test_a_greeting_still_carries_the_profile_but_recalls_nothing(tmp_path):
    manager = _store(tmp_path, ALICE)
    preface, _ = _preface(manager, message="hi", use_memory=False, use_profile=True)
    sources = [(m.get("metadata") or {}).get("source") for m in preface]
    assert profile.SOURCE_LABEL in sources
    assert "saved memory: pinned user facts" not in sources and "saved memory: retrieved context" not in sources


def test_a_mocked_store_adds_no_profile_and_loads_memories_once():
    manager = MagicMock()
    manager.load.return_value = []
    preface, _ = _preface(manager, use_memory=True)
    assert not _profile_blocks(preface)
    manager.load.assert_called_once_with(owner="alice")


def test_shared_vector_index_is_searched_wider_when_other_accounts_fill_it(tmp_path):
    manager = _store(tmp_path, ALICE)
    vector = MagicMock()
    vector.healthy = True
    vector.count.return_value = 400                      # mostly other accounts' entries
    vector.search.return_value = []
    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=MagicMock(), memory_vector=vector)
    mine = manager.load(owner="alice")
    processor._hybrid_retrieve("bees and hives", mine, k=3)
    assert vector.search.call_args.kwargs["k"] == min(9 * -(-400 // len(mine)), 200)
    vector.count.return_value = len(mine)                # single-user: unchanged
    processor._hybrid_retrieve("bees and hives", mine, k=3)
    assert vector.search.call_args.kwargs["k"] == 9


# ── the reduced document prompt ────────────────────────────────────────────

def test_reduced_document_prompt_keeps_the_profile_apart_from_the_eight_facts():
    from src.agent_loop import _minimal_saved_memory_message
    from src.prompt_security import untrusted_context_message

    block = untrusted_context_message(profile.SOURCE_LABEL, profile.render_for_prompt(profile.normalize(WRITTEN)))
    pinned = untrusted_context_message(
        "saved memory: pinned user facts",
        "Core facts about the user:\n- " + "\n- ".join(f"fact {i}" for i in range(12)),
    )
    content = _minimal_saved_memory_message([block, pinned])["content"]
    facts = [line[2:] for line in content.splitlines() if line.startswith("- ")]
    assert facts[0].startswith("Alice lives near Flagstaff")
    assert sum(1 for f in facts if f.startswith("fact ")) == 8            # the usual eight are all still there
    assert len(facts) == 6 + 8
    assert "Who you are talking to" not in content


# ── the memory API ─────────────────────────────────────────────────────────

def _routes(monkeypatch, manager, user, vector=None):
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: user)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda u=None: {"auto_memory": False})
    router = memory_routes.setup_memory_routes(manager, MagicMock(), vector)

    def endpoint(path, method):
        return next(r.endpoint for r in router.routes if r.path == path and method in r.methods)

    return router, endpoint


def test_profile_routes_come_before_the_memory_id_wildcards(monkeypatch, tmp_path):
    router, _ = _routes(monkeypatch, _store(tmp_path, ALICE), "alice")
    paths = [(r.path, sorted(r.methods)[0]) for r in router.routes]
    for method in ("GET", "DELETE"):
        assert paths.index(("/api/memory/profile", method)) < paths.index(("/api/memory/{memory_id}", method))


def test_get_profile_returns_only_the_callers(monkeypatch, tmp_path):
    manager = _store(tmp_path, ALICE + BOB)
    _, endpoint = _routes(monkeypatch, manager, "bob")
    out = endpoint("/api/memory/profile", "GET")(request=None)
    assert out["memories"] == len(BOB) and out["using"] is True and out["learning"] is False
    assert "Bob Castellanos" in out["markdown"] and "Alice" not in json.dumps(out)
    assert "basis" not in out["profile"] and "seen" not in out["profile"]      # bookkeeping stays server-side
    assert out["profile"]["interests"] == [{"name": "Collects vintage synthesizers", "notes": []}]


def test_rebuild_route_writes_the_callers_profile(monkeypatch, tmp_path):
    manager = _store(tmp_path, ALICE + BOB)
    _, endpoint = _routes(monkeypatch, manager, "alice")
    monkeypatch.setattr(memory_routes, "resolve_task_endpoint", lambda *a, **k: ("http://local/v1", "util", {}))
    _model_says(monkeypatch, WRITTEN)
    out = asyncio.run(endpoint("/api/memory/profile/rebuild", "POST")(request=None, session=None))
    assert out["ok"] and out["written_by_model"] and out["profile"]["summary"].startswith("Alice lives")
    assert not (tmp_path / "profiles" / "bob.json").exists()


def test_rebuild_route_without_any_model_still_answers(monkeypatch, tmp_path):
    manager = _store(tmp_path, ALICE)
    _, endpoint = _routes(monkeypatch, manager, "alice")
    monkeypatch.setattr(memory_routes, "resolve_task_endpoint", lambda *a, **k: (None, None, None))
    out = asyncio.run(endpoint("/api/memory/profile/rebuild", "POST")(request=None, session=None))
    assert out["ok"] and out["written_by_model"] is False and out["profile"]["people"] == ["Married to Ben"]


def test_forget_deletes_only_the_callers_profile_and_memories(monkeypatch, tmp_path):
    manager = _store(tmp_path, ALICE + BOB)
    profile.get_profile("alice", manager)
    profile.get_profile("bob", manager)
    vector = MagicMock()
    vector.healthy = True
    _, endpoint = _routes(monkeypatch, manager, "alice", vector)
    forget = endpoint("/api/memory/profile", "DELETE")

    assert forget(request=None, memories=False) == {"ok": True, "memories_removed": 0}
    assert len(manager.load(owner="alice")) == len(ALICE)                      # profile only: memories kept
    assert not (tmp_path / "profiles" / "alice.json").exists()

    assert forget(request=None, memories=True) == {"ok": True, "memories_removed": len(ALICE)}
    assert manager.load(owner="alice") == [] and len(manager.load(owner="bob")) == len(BOB)
    assert vector.remove.call_count == len(ALICE)
    assert (tmp_path / "profiles" / "bob.json").exists()
    assert profile.get_profile("alice", manager) == {}


# ── learning: the extractor and the background queue ───────────────────────

def test_extractor_is_asked_for_interests():
    from services.memory.memory_extractor import EXTRACT_SYSTEM_PROMPT

    assert "'interest'" in EXTRACT_SYSTEM_PROMPT and "MAX 5 facts" in EXTRACT_SYSTEM_PROMPT
    assert "One question about a subject is not an interest" in EXTRACT_SYSTEM_PROMPT


def test_interest_is_a_category_everywhere_a_memory_can_be_added():
    from src.request_models import MemoryAddRequest
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert MemoryAddRequest(text="Keeps bees", category="interest").category == "interest"
    assert MemoryAddRequest(text="x", category="made-up").category == "fact"
    tool = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "manage_memory")
    assert "interest" in tool["parameters"]["properties"]["category"]["enum"]
    from pathlib import Path
    assert "'interest'" in Path("static/js/memory.js").read_text(encoding="utf-8").split("MEMORY_CATEGORIES")[1][:160]


def test_a_request_to_remember_does_not_wait_for_the_usual_cadence():
    from routes.chat_helpers import _asks_to_remember

    for text in ("Remember that I'm allergic to penicillin", "don't forget my anniversary is in May",
                 "For future reference, I use metric", "From now on call me Al", "keep in mind I work nights"):
        assert _asks_to_remember(text), text
    for text in ("hi", "what's the weather tomorrow?", "write a haiku about bees", "", None):
        assert not _asks_to_remember(text), text


def test_inline_remember_command_saves_to_the_account_that_said_it(tmp_path, monkeypatch):
    from src.chat_handler import ChatHandler

    import sys
    import types

    database = types.ModuleType("src.database")          # the handler only touches the session clock here
    database.update_session_last_accessed = lambda session_id: None
    monkeypatch.setitem(sys.modules, "src.database", database)
    manager = _store(tmp_path, [("alice", "Uses metric units", "preference")])
    handler = ChatHandler(MagicMock(), manager, MagicMock(), MagicMock(), MagicMock(), MagicMock())
    session = MagicMock()
    session.owner = "bob"
    # Bob may save something Alice already has: her memories are not his duplicates.
    asyncio.run(handler.handle_memory_command(session, "remember: Uses metric units"))
    bobs = manager.load(owner="bob")
    assert [m["text"] for m in bobs] == ["Uses metric units"] and bobs[0]["owner"] == "bob"
    assert all(m.get("owner") for m in manager.load_all())                     # nothing left ownerless
    asyncio.run(handler.handle_memory_command(session, "remember: Uses metric units"))
    assert len(manager.load(owner="bob")) == 1                                 # his own duplicate is caught


# ── the chat command ───────────────────────────────────────────────────────

def test_me_command_is_registered_and_wired_to_the_profile_api():
    from pathlib import Path
    import re

    src = Path("static/js/slashCommands.js").read_text(encoding="utf-8")
    registry = src[src.index("const COMMANDS = {"):src.index("const LEGACY_ALIASES")]
    entry = registry[registry.index("\n  me: {"):]
    entry = entry[: entry.index("\n  },") + 5]
    assert "_cmdMe(args)" in entry and "'whoami'" in entry
    keys = re.findall(r"^  ([a-zA-Z_][\w-]*): \{", registry, flags=re.M)
    aliases = re.findall(r"^    alias: \[([^\]]*)\]", registry, flags=re.M)
    names = keys + [a.strip().strip("'\"") for group in aliases for a in group.split(",") if a.strip()]
    for name in ("me", "whoami", "profile", "aboutme"):
        assert names.count(name) == 1, name
    body = src[src.index("async function _cmdMe("):src.index("async function _cmdHosts(")]
    for needle in ("/api/memory/profile'", "/api/memory/profile/rebuild", "/api/memory/profile?memories=true",
                   "'DELETE'", "'memory_enabled', 'auto_memory'", "forget everything"):
        assert needle in body, needle
    # Deleting everything takes a second, explicit step.
    assert body.index("!== 'everything'") < body.index("memories=true")
    renderer = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")
    assert "m.type === 'profile'" in renderer
