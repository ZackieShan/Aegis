"""Deep memory: the change journal, topic dossiers, conversation digests, the
person's own rules, the learning pass, recall into a chat turn, and the
inspector API.

The promises under test:
  * every change to a memory leaves a journal line saying who caused it, and
    can be undone;
  * the model may update and retire what it learned itself, never what the
    person wrote or pinned;
  * what the person removes stays removed, what they wrote stays written;
  * one account never sees, changes or is shown another's.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import routes.mind_routes as mind_routes
from services.memory import journal, learn, mind, profile
from src.chat_processor import ChatProcessor
from src.memory import MemoryManager


@pytest.fixture(autouse=True)
def _fresh():
    from services.memory import memory_extractor

    profile._last_attempt.clear()
    profile._rebuilding.clear()
    mind._last_digest_attempt.clear()
    memory_extractor._extractions_since_audit = 0        # the tidy pass is not under test here
    yield
    memory_extractor._extractions_since_audit = 0


def _store(tmp_path, rows=()):
    manager = MemoryManager(str(tmp_path))
    entries = []
    for row in rows:
        owner, text, category = row[:3]
        entry = manager.add_entry(text, category=category, owner=owner, source=row[3] if len(row) > 3 else "auto")
        entries.append(entry)
    if entries:
        manager.save(entries)
    return manager


def _model_says(monkeypatch, reply, calls=None):
    async def fake_llm(url, model, messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)


def _session(owner, turns, session_id="chat-1"):
    messages = [{"role": role, "content": text} for role, text in turns]
    return SimpleNamespace(id=session_id, owner=owner, name="A chat", get_context_messages=lambda: messages,
                           history=[SimpleNamespace(role=r, content=t) for r, t in turns])


# ── the journal ────────────────────────────────────────────────────────────

def test_every_change_to_a_memory_is_journaled_with_its_cause(tmp_path):
    manager = _store(tmp_path)
    a = manager.add_entry("Keeps bees", category="interest", owner="alice")
    b = manager.add_entry("Collects synths", category="interest", owner="bob")
    with journal.cause("auto", session_id="chat-9"):
        manager.save([a, b])
    alice = journal.read(manager, "alice")
    assert [(e["op"], e["after"]["text"], e["cause"]) for e in alice] == [
        ("add", "Keeps bees", {"by": "auto", "session_id": "chat-9"})]
    assert [e["after"]["text"] for e in journal.read(manager, "bob")] == ["Collects synths"]   # each to its owner

    entries = manager.load_all()
    entries[0]["text"] = "Keeps bees: four hives"
    with journal.cause("you"):
        manager.save(entries)
    entries = manager.load_all()
    entries[0]["pinned"] = True
    manager.save(entries)                                   # no cause given
    manager.increment_uses([a["id"]])                       # bookkeeping: not a change
    manager.save([m for m in manager.load_all() if m["id"] != a["id"]])

    ops = [(e["op"], e["cause"]["by"]) for e in reversed(journal.read(manager, "alice"))]
    assert ops == [("add", "auto"), ("edit", "you"), ("pin", "system"), ("delete", "system")]
    edit = journal.read(manager, "alice", kinds=["memory"])[2]
    assert edit["before"]["text"] == "Keeps bees" and edit["after"]["text"] == "Keeps bees: four hives"
    assert len(journal.read(manager, "bob")) == 1                                               # untouched
    assert (tmp_path / "mind" / "alice" / "journal.jsonl").stat().st_mode & 0o077 == 0


def test_a_change_can_be_undone_and_only_by_its_owner(tmp_path):
    manager = _store(tmp_path, [("alice", "Lives in Flagstaff", "identity"), ("bob", "Lives in Reno", "identity")])
    mine = manager.load(owner="alice")[0]
    entries = manager.load_all()
    next(m for m in entries if m["id"] == mine["id"])["text"] = "Lives in Tucson"
    manager.save(entries)
    edit = journal.read(manager, "alice", limit=1)[0]

    assert journal.undo_memory_change(manager, "bob", edit["id"])["ok"] is False               # not his entry
    done = journal.undo_memory_change(manager, "alice", edit["id"])
    assert done == {"ok": True, "action": "reverted", "memory_id": mine["id"], "text": "Lives in Flagstaff"}
    assert manager.load(owner="alice")[0]["text"] == "Lives in Flagstaff"
    assert journal.read(manager, "alice", limit=1)[0]["cause"]["by"] == "undo"

    manager.save([m for m in manager.load_all() if m["id"] != mine["id"]])
    deletion = journal.read(manager, "alice", limit=1)[0]
    restored = journal.undo_memory_change(manager, "alice", deletion["id"])
    assert restored["action"] == "restored" and manager.load(owner="alice")[0]["id"] == mine["id"]
    assert manager.load(owner="alice")[0]["owner"] == "alice"

    addition = [e for e in journal.read(manager, "alice") if e["op"] == "add"][-1]
    assert journal.undo_memory_change(manager, "alice", addition["id"])["action"] == "removed"
    assert manager.load(owner="alice") == [] and len(manager.load(owner="bob")) == 1


def test_a_stand_in_memory_store_is_never_journaled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock = MagicMock()
    assert journal.record_memory_diff(mock, [], [{"id": "1", "text": "x"}]) == 0
    assert journal.record(mock, "alice", "topic", "create", "t") is not None      # returns the entry, writes nothing
    assert journal.read(mock, "alice") == [] and mind.list_topics(mock, "alice") == []
    assert mind.add_notes(mock, "alice", "Bees", ["Three hives now"]) == (None, 0)
    assert not any(tmp_path.iterdir())


# ── topics ─────────────────────────────────────────────────────────────────

def test_notes_collect_under_one_topic_per_subject_and_per_account(tmp_path):
    manager = _store(tmp_path)
    topic, added = mind.add_notes(manager, "alice", "Beekeeping", ["Has three hives", "Lost one to varroa last winter"],
                                  kind="interest", keywords=["bees", "hive", "varroa"], session_id="chat-1")
    assert added == 2 and topic["kind"] == "interest" and topic["note_count"] == 2
    same, added = mind.add_notes(manager, "alice", "beekeeping", ["Has three hives.", "Trying oxalic acid vapor"])
    assert same["id"] == topic["id"] and added == 1                       # same subject; the repeat is skipped
    assert mind.add_notes(manager, "alice", "Sourdough", ["Bakes every Sunday"])[0]["id"] != topic["id"]
    assert mind.list_topics(manager, "bob") == []
    mind.add_notes(manager, "bob", "Beekeeping", ["Wants to start next spring"])
    notes = [n["text"] for n in mind.get_topic(manager, "alice", topic["id"])["notes"]]
    assert sorted(notes) == ["Has three hives", "Lost one to varroa last winter", "Trying oxalic acid vapor"]
    ops = [e["op"] for e in journal.read(manager, "alice", kinds=["topic"])]
    assert ops.count("create") == 2 and ops.count("note_add") == 4
    first = next(n for n in mind.get_topic(manager, "alice", topic["id"])["notes"] if n["text"] == "Has three hives")
    assert first["session_id"] == "chat-1" and first["by"] == "auto"


def test_a_topic_is_recalled_when_the_message_is_about_it(tmp_path):
    manager = _store(tmp_path)
    mind.add_notes(manager, "alice", "Beekeeping", ["Has three hives", "Lost one colony to varroa mites"],
                   keywords=["bees", "hive", "varroa", "honey"])
    mind.add_notes(manager, "alice", "Land Cruiser restoration", ["Rebuilding the brakes on the 1978 FJ40"],
                   keywords=["truck", "brakes", "fj40"])
    names = lambda text: [t["name"] for t in mind.relevant_topics(manager, "alice", text)]
    assert names("how do I get my bees through winter?") == ["Beekeeping"]
    assert names("anything new on the beekeeping front") == ["Beekeeping"]
    assert names("what torque for the brake caliper bolts on the truck") == ["Land Cruiser restoration"]
    assert names("what's the capital of Australia") == []
    assert names("") == [] and mind.relevant_topics(manager, "bob", "my bees") == []
    block = mind.render_topics_for_prompt(mind.relevant_topics(manager, "alice", "my bees"))
    assert "- Beekeeping" in block and "Lost one colony to varroa mites" in block


DOSSIER = {"summary": "Keeps three hives outside Flagstaff and treats for varroa.",
           "state": ["Three hives", "Uses oxalic acid vapor"],
           "timeline": [{"when": "last winter", "text": "Lost one hive to varroa"}],
           "open": ["Wants a fourth hive"], "keywords": ["bees", "honey"]}


def test_the_digest_is_rewritten_from_notes_and_leaves_the_persons_edits_alone(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    topic, _ = mind.add_notes(manager, "alice", "Beekeeping", ["Has three hives", "Uses oxalic acid vapor",
                                                               "Lost one hive to varroa last winter"])
    calls = []
    _model_says(monkeypatch, DOSSIER, calls)
    assert asyncio.run(mind.maybe_consolidate(manager, "alice", "http://local/v1", "util")) == 1
    written = mind.get_topic(manager, "alice", topic["id"])
    assert written["summary"].startswith("Keeps three hives") and written["new_notes"] == 0
    assert written["timeline"] == [{"when": "last winter", "text": "Lost one hive to varroa"}]
    assert "Has three hives" in calls[0][1]["content"]
    assert asyncio.run(mind.maybe_consolidate(manager, "alice", "http://local/v1", "util")) == 0   # nothing new
    mind.add_notes(manager, "alice", "Work", ["Works nights at the hospital"], kind="work")
    assert [t["name"] for t in mind.relevant_topics(manager, "alice", "what were we working on?")] == []
    assert [t["name"] for t in mind.relevant_topics(manager, "alice", "my work at the hospital on nights")] == ["Work"]

    mind.update_topic(manager, "alice", topic["id"], {"summary": "My bees. Three hives, all Carniolans."})
    mind.add_notes(manager, "alice", "Beekeeping", ["Bought a refractometer", "Joined the county association",
                                                    "Harvested 40 lb of honey"])
    mind._last_digest_attempt.clear()
    _model_says(monkeypatch, dict(DOSSIER, summary="The model's own summary.", state=["Four hives"]))
    asyncio.run(mind.maybe_consolidate(manager, "alice", "http://local/v1", "util"))
    after = mind.get_topic(manager, "alice", topic["id"])
    assert after["summary"] == "My bees. Three hives, all Carniolans."            # theirs: kept
    assert after["state"] == ["Four hives"] and after["locked"] == ["summary"]    # the rest: rewritten


def test_deleting_a_note_takes_what_it_said_out_of_the_digest(tmp_path, monkeypatch):
    manager = _store(tmp_path, [("alice", "Keeps bees", "interest")])
    topic, _ = mind.add_notes(manager, "alice", "Beekeeping", ["Has three hives", "Lost one hive to varroa"])
    _model_says(monkeypatch, DOSSIER)
    asyncio.run(mind.consolidate_topic(manager, "alice", topic["id"], "http://local/v1", "util"))
    note = next(n for n in mind.get_topic(manager, "alice", topic["id"])["notes"] if "varroa" in n["text"])
    assert mind.delete_note(manager, "alice", topic["id"], note["id"]) is True
    after = mind.get_topic(manager, "alice", topic["id"])
    assert after["summary"] == "" and after["state"] == [] and after["timeline"] == []
    assert "varroa" not in json.dumps(after)
    assert mind.delete_note(manager, "bob", topic["id"], note["id"]) is False
    assert mind.delete_topic(manager, "alice", topic["id"]) and mind.list_topics(manager, "alice") == []


# ── conversations ──────────────────────────────────────────────────────────

def test_conversation_digests_give_continuity_and_are_recalled_when_relevant(tmp_path):
    manager = _store(tmp_path)
    mind.save_episode(manager, "alice", "chat-1", "Varroa treatment", "Asked how to treat hives for varroa mites "
                      "with oxalic acid vapor; decided to treat in December.", outcomes=["Treat in December"])
    mind.save_episode(manager, "alice", "chat-2", "Brake rebuild", "Worked through rebuilding the front brake "
                      "calipers on the Land Cruiser.", topics=["Land Cruiser restoration"])
    mind.save_episode(manager, "bob", "chat-3", "Synth repair", "Fixed a stuck key on a Juno synthesizer.")
    assert [e["session_id"] for e in mind.recent_episodes(manager, "alice", exclude_session="chat-2")] == ["chat-1"]
    assert [e["title"] for e in mind.relevant_episodes(manager, "alice", "when should I do the oxalic acid "
                                                       "treatment for varroa?")] == ["Varroa treatment"]
    assert mind.relevant_episodes(manager, "alice", "varroa treatment oxalic", exclude=["chat-1"]) == []
    assert mind.relevant_episodes(manager, "alice", "stuck key on my Juno synthesizer") == []      # that is Bob's
    mind.save_episode(manager, "alice", "chat-1", "Varroa treatment", "Treated all three hives in December; "
                      "mite counts dropped.")
    assert len(mind.list_episodes(manager, "alice")) == 2                                          # updated in place
    block = mind.render_episodes_for_prompt(mind.recent_episodes(manager, "alice"), mind.RECENT_HEADING)
    assert "Varroa treatment: Treated all three hives" in block
    assert mind.delete_episode(manager, "alice", "chat-1") and not mind.delete_episode(manager, "alice", "chat-3")


# ── the person's own rules ─────────────────────────────────────────────────

def test_lines_the_person_wrote_stay_and_lines_they_removed_never_return(tmp_path, monkeypatch):
    manager = _store(tmp_path, [("alice", "Name is Alice", "identity"), ("alice", "Married to Ben", "contact"),
                                ("alice", "Prefers long detailed answers", "preference")])
    assert profile.get_profile("alice", manager)["preferences"] == ["Prefers long detailed answers"]
    mind.suppress_profile_line(manager, "alice", "preferences", "Prefers long detailed answers")
    mind.lock_profile_line(manager, "alice", "preferences", "Keep answers short")
    mind.lock_profile_line(manager, "alice", "people", "Married to Benjamin (Ben)", replaces="Married to Ben")
    now = profile.get_profile("alice", manager)
    assert now["preferences"] == ["Keep answers short"]
    assert now["people"] == ["Married to Benjamin (Ben)"]

    # A rewrite by the model that brings the removed line back, reworded or not, does not get it through.
    calls = []
    _model_says(monkeypatch, {"summary": "Alice.", "identity": ["Name is Alice"], "people": ["Married to Ben"],
                              "preferences": ["Prefers long, detailed answers", "Likes tables"]}, calls)
    written = asyncio.run(profile.rebuild_profile("alice", manager, "http://local/v1", "util"))
    assert written["preferences"] == ["Keep answers short", "Likes tables"]
    assert written["people"] == ["Married to Benjamin (Ben)"]
    asked = calls[0][1]["content"]
    assert "[written by the person]" in asked and "- (preferences) Keep answers short" in asked
    assert "[removed by the person]" in asked and "- Prefers long detailed answers" in asked
    assert "Keep answers short" in profile.render_for_prompt(profile.get_profile("alice", manager))
    ops = [e["op"] for e in journal.read(manager, "alice", kinds=["profile"])]
    assert sorted(ops) == ["add_line", "edit_line", "remove_line"]
    assert profile.get_profile("bob", manager) == {}


def test_a_profile_can_be_written_by_hand_before_anything_is_learned(tmp_path):
    manager = _store(tmp_path)
    mind.lock_profile_line(manager, "alice", "identity", "Name is Alice")
    mind.lock_profile_line(manager, "alice", "interests", "Beekeeping")
    mine = profile.get_profile("alice", manager)
    assert mine["identity"] == ["Name is Alice"] and mine["interests"] == [{"name": "Beekeeping", "notes": []}]


# ── the learning pass ──────────────────────────────────────────────────────

def _plan(prompt: str) -> dict:
    """The model's answer for TURNS, using whatever handles the prompt gave
    to the known facts."""
    handle = {}
    for line in prompt.splitlines():
        head, _, rest = line.partition(" ")
        if head[:1] == "m" and head[1:].isdigit() and "] " in rest:
            handle[rest.split("] ", 1)[1]] = head
    plan = json.loads(json.dumps(PLAN))
    plan["update"][0]["id"] = handle.get("Keeps bees: three hives", "m?")
    plan["update"][1]["id"] = handle.get("Prefers detailed answers about bees", "m?")
    plan["remove"][0]["id"] = handle.get("Owns a canoe that she sold", "m?")
    plan["remove"][1]["id"] = handle.get("Keeps answers about penicillin pinned", "m?")
    return plan


def _model_plans(monkeypatch, calls=None):
    async def fake_llm(url, model, messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        return json.dumps(_plan(messages[1]["content"]))

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)


PLAN = {
    "add": [
        {"text": "Works nights as an ER nurse", "category": "identity", "quote": "I'm an ER nurse, nights mostly",
         "sensitive": "work"},
        {"text": "Allergic to penicillin", "category": "fact", "quote": "allergic to penicillin", "sensitive": "health"},
        {"text": "Owns a 3D printer", "category": "fact", "quote": "words the user never said"},
    ],
    "update": [{"id": "m1", "text": "Keeps bees: four hives", "quote": "I'm up to four hives now"},
               {"id": "m2", "text": "Prefers long answers", "quote": "keep answers short"}],
    "remove": [{"id": "m3", "reason": "sold it"}, {"id": "m4", "reason": "model overreach"}],
    "topics": [{"name": "Beekeeping", "kind": "interest", "keywords": ["bees", "hive"],
                "notes": ["Up to four hives after a split in May", "Bought a second vaporizer"]},
               {"name": "Sinus infection", "kind": "health", "notes": ["Prescribed amoxicillin"]}],
    "episode": {"title": "Hive count and work", "summary": "Said they now have four hives and work nights "
                                                           "as an ER nurse.", "outcomes": ["Order more frames"]},
}
TURNS = [("user", "I'm an ER nurse, nights mostly. And I'm up to four hives now. Also keep answers short, I'm "
                  "allergic to penicillin, and I sold the canoe."),
         ("assistant", "Got it. You should buy a 3D printer.")]


def _known(tmp_path):
    manager = MemoryManager(str(tmp_path))
    rows = [("alice", "Keeps bees: three hives", "interest", "auto"),
            ("alice", "Prefers detailed answers about bees", "preference", "user"),       # written by her
            ("alice", "Owns a canoe that she sold", "fact", "auto"),
            ("alice", "Keeps answers about penicillin pinned", "fact", "ai_agent"),
            ("bob", "Keeps bees: one hive", "interest", "auto")]
    entries = [manager.add_entry(text, source=source, category=category, owner=owner)
               for owner, text, category, source in rows]
    entries[3]["pinned"] = True                                                   # pinned by hand, old style
    manager.save(entries)
    return manager, entries


def test_one_pass_adds_updates_retires_and_files_notes_with_guards(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    mind.set_blocked_kinds(manager, "alice", ["health"])
    already = journal.count(manager, "alice")               # the set-up above
    calls = []
    _model_plans(monkeypatch, calls)
    done = asyncio.run(learn.learn_from_session(_session("alice", TURNS, "chat-7"), manager, None,
                                                "http://local/v1", "util"))

    asked = calls[0][1]["content"]
    assert "[interest] Keeps bees: three hives" in asked and "one hive" not in asked        # only her own facts
    assert "NEVER STORE anything about: Health and medical" in asked
    mine = {m["text"]: m for m in manager.load(owner="alice")}
    # update: the fact she had is now current, with her words as evidence and the chat it came from
    assert "Keeps bees: three hives" not in mine
    bees = mine["Keeps bees: four hives"]
    assert bees["id"] == entries[0]["id"] and bees["quote"] == "I'm up to four hives now"
    assert bees["session_id"] == "chat-7"
    # a fact she wrote herself is not rewritten; what she said now is kept beside it
    assert "Prefers detailed answers about bees" in mine and "Prefers long answers" in mine
    # remove: the learned fact goes, the one she pinned by hand stays
    assert "Owns a canoe that she sold" not in mine and "Keeps answers about penicillin pinned" in mine
    # add: evidence checked against her own words; blocked kinds dropped; identity pinned automatically
    nurse = mine["Works nights as an ER nurse"]
    assert nurse["pinned"] and nurse["pinned_by"] == "auto" and nurse["sensitive"] == "work"
    assert "Allergic to penicillin" not in mine                                 # health is blocked
    assert mine["Owns a 3D printer"].get("quote") is None                       # she never said that: no evidence kept
    assert done["added"] == 3 and done["updated"] == 1 and done["removed"] == 1
    # topics: notes filed, the health topic refused
    assert [t["name"] for t in mind.list_topics(manager, "alice")] == ["Beekeeping"]
    assert done["topics"] == ["Beekeeping"] and done["notes"] == 2 and done["episode"] is True
    assert mind.get_episode(manager, "alice", "chat-7")["outcomes"] == ["Order more frames"]
    # everything is journaled as learned from that chat
    log = journal.read(manager, "alice")
    log = log[: len(log) - already]                          # what the pass itself wrote
    assert {e["cause"]["by"] for e in log} == {"auto"}
    assert {e["cause"].get("session_id") for e in log if e["kind"] == "memory"} == {"chat-7"}
    assert sorted(e["op"] for e in log if e["kind"] == "memory") == ["add", "add", "add", "delete", "edit"]
    # and Bob is exactly as he was
    assert [m["text"] for m in manager.load(owner="bob")] == ["Keeps bees: one hive"]
    assert [e["cause"]["by"] for e in journal.read(manager, "bob")] == ["system"]     # only his own set-up entry
    assert mind.list_topics(manager, "bob") == []


def test_what_is_already_known_is_reaffirmed_not_duplicated(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    _model_says(monkeypatch, {"add": [{"text": "Keeps bees: three hives", "category": "interest"}]})
    done = asyncio.run(learn.learn_from_session(_session("alice", TURNS), manager, None, "http://local/v1", "util"))
    assert done["added"] == 0 and done["confirmed"] == 1
    bees = next(m for m in manager.load(owner="alice") if m["id"] == entries[0]["id"])
    assert bees["mentions"] == 2 and bees["confirmed"]
    assert len(manager.load(owner="alice")) == 4


def test_without_a_model_the_obvious_statements_are_still_caught(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    _model_says(monkeypatch, RuntimeError("backend down"))
    turns = [("user", "My name is Dana and I live in Bozeman."), ("assistant", "Nice to meet you.")]
    done = asyncio.run(learn.learn_from_session(_session("dana", turns), manager, None, "http://local/v1", "util"))
    assert done["added"] >= 1
    assert any("Dana" in m["text"] for m in manager.load(owner="dana"))
    assert learn.is_protected({"source": "user"}) and learn.is_protected({"pinned": True, "pinned_by": "user"})
    assert not learn.is_protected({"source": "auto", "pinned": True})           # auto-pinned identity facts


def test_an_older_style_bare_list_of_facts_is_still_understood(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    _model_says(monkeypatch, '[{"text": "Restoring a 1984 Toyota pickup", "category": "project"}]')
    turns = [("user", "I'm restoring a 1984 Toyota pickup with my dad."), ("assistant", "Nice project.")]
    asyncio.run(learn.learn_from_session(_session("dana", turns), manager, None, "http://local/v1", "util"))
    assert [m["category"] for m in manager.load(owner="dana")] == ["project"]


# ── recall into a chat turn ────────────────────────────────────────────────

def _blocks(manager, owner, message, session=None, **kwargs):
    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=MagicMock())
    session = session or SimpleNamespace(id="now", history=[], owner=owner)
    messages, used = processor.memory_messages(message, session, owner=owner, **kwargs)
    return {(m.get("metadata") or {}).get("source"): m["content"] for m in messages}, used


def test_a_turn_gets_recent_conversations_and_the_dossier_of_its_subject(tmp_path):
    manager = _store(tmp_path, [("alice", "Name is Alice", "identity"), ("bob", "Name is Bob", "identity")])
    mind.add_notes(manager, "alice", "Beekeeping", ["Has three hives", "Lost one colony to varroa"],
                   keywords=["bees", "hive"])
    mind.save_episode(manager, "alice", "earlier", "Brake rebuild", "Rebuilt the front brake calipers on the "
                      "Land Cruiser and bled the lines.")
    mind.save_episode(manager, "alice", "now", "This chat", "The chat in progress, which must not be echoed back.")
    mind.save_episode(manager, "bob", "bobs", "Synths", "Fixed a stuck key on a Juno synthesizer.")

    blocks, used = _blocks(manager, "alice", "how are my bees likely to do this winter?")
    assert "Brake rebuild" in blocks["deep memory: recent conversations"]
    assert "must not be echoed" not in json.dumps(blocks)                       # the current chat is left out
    assert "Lost one colony to varroa" in blocks["deep memory: topic notes"]
    assert {"text": "Topic: Beekeeping", "category": "topic", "type": "topic"} in used
    assert "Juno" not in json.dumps(blocks) and "Bob" not in json.dumps(blocks)
    order = list(blocks)
    assert order.index("saved memory: user profile") < order.index("deep memory: recent conversations") \
        < order.index("deep memory: topic notes")

    # A follow-up that does not name the subject still finds it through the message before.
    history = [SimpleNamespace(role="user", content="how are my bees likely to do this winter?"),
               SimpleNamespace(role="assistant", content="They should be fine if mite counts are low."),
               SimpleNamespace(role="user", content="and how often should I check on them?")]
    follow, _ = _blocks(manager, "alice", "and how often should I check on them?",
                        session=SimpleNamespace(id="now", history=history, owner="alice"))
    assert "deep memory: topic notes" in follow

    assert "deep memory: topic notes" not in _blocks(manager, "alice", "what's 12 times 12?")[0]
    nobody, used = _blocks(manager, "alice", "how are my bees?", incognito=True, use_memory=False)
    assert nobody == {} and used == []
    bobs, _ = _blocks(manager, "bob", "how are my bees likely to do this winter?")
    assert "deep memory: topic notes" not in bobs and "varroa" not in json.dumps(bobs)


def test_looking_at_what_the_model_is_given_does_not_count_as_use(tmp_path):
    manager = _store(tmp_path, [("alice", "Keeps bees in three hives", "interest")])
    entry = manager.load(owner="alice")[0]
    entries = manager.load_all()
    entries[0]["pinned"] = True
    manager.save(entries)
    _blocks(manager, "alice", "bees", count_uses=False)
    assert manager.load(owner="alice")[0]["uses"] == 0
    _blocks(manager, "alice", "bees")
    assert manager.load(owner="alice")[0]["uses"] == 1 and entry["id"]


# ── the inspector API ──────────────────────────────────────────────────────

def _api(monkeypatch, manager, user, sessions=None, vector=None):
    monkeypatch.setattr(mind_routes, "get_current_user", lambda request: user)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda u=None: {})
    session_manager = MagicMock()

    def get_session(session_id):
        if sessions and session_id in sessions:
            return sessions[session_id]
        raise KeyError(session_id)

    session_manager.get_session.side_effect = get_session
    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=MagicMock())
    router = mind_routes.setup_mind_routes(manager, session_manager, vector, processor)

    def call(method, path, **kwargs):
        endpoint = next(r.endpoint for r in router.routes if r.path == f"/api/mind{path}" and method in r.methods)
        result = endpoint(request=None, **kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    return call


def test_inspector_shows_each_fact_with_where_it_came_from(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    _model_plans(monkeypatch)
    asyncio.run(learn.learn_from_session(_session("alice", TURNS, "chat-7"), manager, None, "http://local/v1", "util"))
    sessions = {"chat-7": SimpleNamespace(id="chat-7", owner="alice", name="Hives and work")}
    call = _api(monkeypatch, manager, "alice", sessions)

    seen = {m["text"]: m for m in call("GET", "/memories")["memories"]}
    assert "Keeps bees: one hive" not in seen                                    # Bob's
    bees = seen["Keeps bees: four hives"]
    assert bees["origin"] == {"session_id": "chat-7", "name": "Hives and work", "exists": True}
    assert bees["quote"] == "I'm up to four hives now" and bees["source"] == "auto" and not bees["protected"]
    assert seen["Prefers detailed answers about bees"]["protected"] is True

    overview = call("GET", "/overview")
    assert overview["account"] == "alice" and overview["counts"]["memories"] == len(seen)
    assert overview["counts"]["topics"] == 2 and overview["counts"]["conversations"] == 1
    assert overview["counts"]["by_source"]["auto"] >= 3 and overview["switches"] == {"using": True, "learning": True}

    # A chat that belongs to someone else is never named as an origin.
    sessions["chat-7"].owner = "bob"
    again = {m["text"]: m for m in call("GET", "/memories")["memories"]}
    assert again["Keeps bees: four hives"]["origin"]["exists"] is False
    assert again["Keeps bees: four hives"]["origin"]["name"] == "Hive count and work"   # its own digest title


def test_facts_are_edited_pinned_and_deleted_only_by_their_owner(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    vector = MagicMock()
    vector.healthy = True
    call = _api(monkeypatch, manager, "alice", vector=vector)
    bees, bobs = entries[0]["id"], entries[4]["id"]

    edited = call("PUT", "/memories/{memory_id}", memory_id=bees,
                  body={"text": "Keeps bees: five hives", "category": "project", "pinned": True})["memory"]
    assert edited["text"] == "Keeps bees: five hives" and edited["category"] == "project"
    assert edited["pinned"] and edited["protected"]                        # hers now: the learner keeps off
    vector.remove.assert_called_with(bees)
    vector.add.assert_called_with(bees, "Keeps bees: five hives")
    from fastapi import HTTPException
    for method, kwargs in (("PUT", {"body": {"text": "hacked"}}), ("DELETE", {})):
        with pytest.raises(HTTPException) as refused:
            call(method, "/memories/{memory_id}", memory_id=bobs, **kwargs)
        assert refused.value.status_code == 404
    assert manager.load(owner="bob")[0]["text"] == "Keeps bees: one hive"

    added = call("POST", "/memories", body={"text": "  Has two   dogs ", "category": "fact"})["memory"]
    assert added["text"] == "Has two dogs" and added["source"] == "user" and added["protected"]
    assert call("POST", "/memories", body={"text": "Has two dogs"}) == {"ok": True, "duplicate": True}
    assert call("DELETE", "/memories/{memory_id}", memory_id=bees) == {"ok": True}

    log = call("GET", "/history")["entries"][:3]            # newest first: what she just did
    assert {e["cause"]["by"] for e in log} == {"you"}
    assert [e["op"] for e in log] == ["delete", "add", "edit"]
    deletion = log[0]
    assert deletion["can_undo"] and call("POST", "/history/{entry_id}/undo", entry_id=deletion["id"])["action"] == "restored"
    assert any(m["id"] == bees for m in manager.load(owner="alice"))


def test_removing_a_profile_line_can_forget_the_facts_behind_it(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    call = _api(monkeypatch, manager, "alice")
    related = call("GET", "/related", text="Prefers detailed answers about bees")["memories"]
    assert [m["text"] for m in related] == ["Prefers detailed answers about bees"]
    out = call("POST", "/profile/line", body={"action": "remove", "section": "preferences",
                                              "text": "Prefers detailed answers about bees",
                                              "forget": [related[0]["id"], entries[4]["id"]]})   # + one of Bob's
    assert out["forgotten"] == 1 and "detailed" not in json.dumps(out["profile"])
    assert len(manager.load(owner="bob")) == 1                                    # his is not hers to forget
    out = call("POST", "/profile/line", body={"action": "add", "section": "goals", "text": "Run a half marathon"})
    assert out["profile"]["goals"] == ["Run a half marathon"]
    out = call("POST", "/profile/line", body={"action": "edit", "section": "goals", "text": "Run a half marathon",
                                              "new_text": "Run a full marathon"})
    assert out["profile"]["goals"] == ["Run a full marathon"]


def test_topics_conversations_rules_export_and_forgetting(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    mind.add_notes(manager, "bob", "Synths", ["Owns a Juno-106"])
    call = _api(monkeypatch, manager, "alice")

    topic = call("POST", "/topics", body={"name": "Beekeeping", "kind": "interest", "note": "Has three hives"})["topic"]
    topic = call("POST", "/topics/{topic_id}/notes", topic_id=topic["id"], body={"text": "Wants a fourth hive"})["topic"]
    assert [n["by"] for n in topic["notes"]] == ["you", "you"] and len(call("GET", "/topics")["topics"]) == 1
    topic = call("PUT", "/topics/{topic_id}", topic_id=topic["id"], body={"name": "Bees", "summary": "My bees."})["topic"]
    assert topic["name"] == "Bees" and topic["locked"] == ["summary"]
    note = topic["notes"][0]["id"]
    assert len(call("DELETE", "/topics/{topic_id}/notes/{note_id}", topic_id=topic["id"], note_id=note)["topic"]["notes"]) == 1

    mind.save_episode(manager, "alice", "chat-1", "Hives", "Talked about getting a fourth hive this spring.")
    assert [c["title"] for c in call("GET", "/conversations", limit=50)["conversations"]] == ["Hives"]

    # Rules: block a kind and purge what is already marked as that kind.
    everything = manager.load_all()
    everything[2]["sensitive"] = "money"
    manager.save(everything)
    out = call("PUT", "/rules", body={"blocked_kinds": ["money", "nonsense"], "purge": True})
    assert out["rules"]["blocked_kinds"] == ["money"] and out["purged"] == 1
    assert all(m.get("sensitive") != "money" for m in manager.load(owner="alice"))

    preview = call("POST", "/preview", body={"message": "how are my bees doing?"})
    labels = [b["label"] for b in preview["blocks"]]
    assert "saved memory: user profile" in labels and "deep memory: topic notes" in labels
    assert all("UNTRUSTED" not in b["text"] and "<<<" not in b["text"] for b in preview["blocks"])
    assert "Juno" not in json.dumps(preview)

    exported = json.loads(call("GET", "/export", format="json").body)
    assert exported["account"] == "alice" and {t["name"] for t in exported["topics"]} == {"Bees"}
    assert exported["conversations"] and exported["history"] and exported["memories"]
    assert "Juno" not in json.dumps(exported) and "one hive" not in json.dumps(exported)
    assert "# Everything Aegis knows about alice" in call("GET", "/export", format="md").body.decode()

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        call("DELETE", "/everything", confirm="")
    assert call("DELETE", "/everything", confirm="yes")["memories_removed"] >= 3
    assert manager.load(owner="alice") == [] and mind.list_topics(manager, "alice") == []
    assert journal.read(manager, "alice") == [] and not (tmp_path / "mind" / "alice").exists()
    assert len(manager.load(owner="bob")) == 1 and mind.list_topics(manager, "bob")[0]["name"] == "Synths"


# ── when it runs ───────────────────────────────────────────────────────────

def test_a_first_exchange_with_substance_is_learned_from_and_a_greeting_is_not():
    import inspect

    from routes import chat_helpers

    source = inspect.getsource(chat_helpers.run_post_response_tasks)
    assert "learn_from_session" in source and "maybe_consolidate" in source and "maybe_rebuild" in source
    assert source.index('("memory"') < source.index('("topics"') < source.index('("profile"')
    assert "_msg_count == 2" in source and "_is_casual_low_signal(message)" in source


# ── facts and topics that move on instead of piling up ─────────────────────

def test_a_fact_restated_with_a_new_number_replaces_the_old_one(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    _model_says(monkeypatch, {"add": [{"text": "Keeps bees: four hives", "category": "interest",
                                       "quote": "up to four hives now"}]})       # added, not sent as an update
    done = asyncio.run(learn.learn_from_session(_session("alice", TURNS, "chat-8"), manager, None,
                                                "http://local/v1", "util"))
    texts = [m["text"] for m in manager.load(owner="alice")]
    assert "Keeps bees: four hives" in texts and "Keeps bees: three hives" not in texts
    assert done["updated"] == 1 and done["added"] == 0 and len(texts) == 4
    moved = next(m for m in manager.load(owner="alice") if m["text"] == "Keeps bees: four hives")
    assert moved["id"] == entries[0]["id"] and moved["session_id"] == "chat-8"
    assert journal.read(manager, "alice", limit=1)[0]["op"] == "edit"
    assert [m["text"] for m in manager.load(owner="bob")] == ["Keeps bees: one hive"]   # his count is his own

    # Once she has pinned it herself, a restatement is kept beside it instead of replacing it.
    everything = manager.load_all()
    next(m for m in everything if m["id"] == entries[0]["id"]).update(pinned=True, pinned_by="user")
    manager.save(everything)
    _model_says(monkeypatch, {"add": [{"text": "Keeps bees: five hives", "category": "interest"},
                                      {"text": "Prefers detailed answers about wasps", "category": "preference"}]})
    asyncio.run(learn.learn_from_session(_session("alice", TURNS, "chat-9"), manager, None, "http://local/v1", "util"))
    texts = [m["text"] for m in manager.load(owner="alice")]
    assert "Keeps bees: four hives" in texts and "Keeps bees: five hives" in texts
    # A different fact that merely shares most of its words is not mistaken for a duplicate.
    assert "Prefers detailed answers about bees" in texts and "Prefers detailed answers about wasps" in texts


def test_a_very_close_vector_match_is_a_restatement_and_a_loose_one_is_a_new_fact(tmp_path, monkeypatch):
    manager, entries = _known(tmp_path)
    bees = entries[0]["id"]
    vector = MagicMock()
    vector.healthy = True
    # 0.92 → no match; 0.86 → the bees fact: close, not identical.
    vector.find_similar.side_effect = lambda text, threshold=0.92: bees if threshold < 0.9 else None
    _model_says(monkeypatch, {"add": [{"text": "Now runs four colonies of honey bees", "category": "interest"}]})
    done = asyncio.run(learn.learn_from_session(_session("alice", TURNS), manager, vector, "http://local/v1", "util"))
    assert done["updated"] == 1
    assert next(m for m in manager.load(owner="alice") if m["id"] == bees)["text"] == "Now runs four colonies of honey bees"
    vector.remove.assert_called_with(bees)
    vector.add.assert_called_with(bees, "Now runs four colonies of honey bees")

    vector.find_similar.side_effect = lambda text, threshold=0.92: None           # merely related: its own fact
    _model_says(monkeypatch, {"add": [{"text": "Sells honey at the farmers market", "category": "interest"}]})
    done = asyncio.run(learn.learn_from_session(_session("alice", TURNS), manager, vector, "http://local/v1", "util"))
    assert done["added"] == 1 and done["updated"] == 0


def test_the_same_subject_under_another_name_joins_its_dossier(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    first, _ = mind.add_notes(manager, "alice", "car restoration", ["Restoring a 1984 Toyota pickup with dad"],
                              kind="project", keywords=["toyota", "pickup", "brakes", "restoration", "dad"])
    joined, added = mind.add_notes(manager, "alice", "Toyota pickup restoration", ["Front brakes finished"],
                                   keywords=["brakes", "carburetor", "toyota", "pickup", "restoration"])
    assert joined["id"] == first["id"] and added == 1 and len(mind.list_topics(manager, "alice")) == 1
    # Another project of the same kind is its own topic.
    other, _ = mind.add_notes(manager, "alice", "Land Cruiser restoration", ["Looking for a 1970s Land Cruiser"],
                              keywords=["land", "cruiser", "restoration", "dad"])
    assert other["id"] != first["id"]
    # A topic the person names by hand is never folded into another.
    mine, _ = mind.add_notes(manager, "alice", "Pickup brakes", ["Rear drums done"], by="you",
                             keywords=["toyota", "pickup", "brakes", "restoration"])
    assert mine["id"] not in (first["id"], other["id"])

    call = _api(monkeypatch, manager, "alice")
    merged = call("POST", "/topics/{topic_id}/merge", topic_id=mine["id"], body={"into": first["id"]})["topic"]
    assert merged["id"] == first["id"] and {n["text"] for n in merged["notes"]} == {
        "Restoring a 1984 Toyota pickup with dad", "Front brakes finished", "Rear drums done"}
    assert len(mind.list_topics(manager, "alice")) == 2
    assert [t["name"] for t in mind.relevant_topics(manager, "alice", "what about the pickup brakes?")][:1] == ["car restoration"]
    assert journal.read(manager, "alice", limit=1)[0]["op"] == "merge"



def test_passes_for_one_chat_wait_as_one_and_then_read_everything_since_the_last(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    learn._seen_messages.clear()
    learn._seen_messages["busy-chat"] = 6                  # the last pass ran six messages in
    release = asyncio.Event()
    waits = []

    async def lull(label):
        waits.append(label)
        await release.wait()

    monkeypatch.setattr(learn, "_wait_until_quiet", lull)
    calls = []
    _model_says(monkeypatch, {"add": []}, calls)
    turns = [("user", f"message number {n} about my garden") if n % 2 == 0 else ("assistant", f"reply {n}") for n in range(30)]
    session = _session("alice", turns, "busy-chat")

    async def burst():
        first = asyncio.create_task(learn.learn_from_session(session, manager, None, "http://local/v1", "util"))
        await asyncio.sleep(0)
        second = await learn.learn_from_session(session, manager, None, "http://local/v1", "util")
        third = await learn.learn_from_session(session, manager, None, "http://local/v1", "util")
        release.set()
        return await first, second, third

    first, second, third = asyncio.run(burst())
    assert "skipped" in second and "skipped" in third and "skipped" not in first
    assert len(waits) == 1 and len(calls) == 1                         # one wait, one model call
    shown = calls[0][1]["content"]
    assert "message number 8 about my garden" in shown and "message number 28" in shown   # the stretch, not just the tail
    assert "message number 4 about my garden" not in shown                                # …within the cap
    # Next time only what is new is looked at (never fewer than the usual window).
    release.set()
    more = turns + [("user", "one more thing about the garden"), ("assistant", "ok")]
    asyncio.run(learn.learn_from_session(_session("alice", more, "busy-chat"), manager, None, "http://local/v1", "util"))
    assert "one more thing about the garden" in calls[1][1]["content"]
    assert "message number 20 about my garden" not in calls[1][1]["content"]



def test_messages_the_model_could_not_read_are_picked_up_by_the_next_pass(tmp_path, monkeypatch):
    manager = _store(tmp_path)
    learn._seen_messages.clear()
    turns = [("user", f"garden note {n}: planted row {n}") if n % 2 == 0 else ("assistant", "ok") for n in range(12)]
    _model_says(monkeypatch, RuntimeError("model server is restarting"))
    asyncio.run(learn.learn_from_session(_session("alice", turns, "c"), manager, None, "http://local/v1", "util"))
    assert "c" not in learn._seen_messages                         # nothing was read
    calls = []
    _model_says(monkeypatch, {"add": []}, calls)
    more = turns + [("user", "garden note 12: watered everything"), ("assistant", "ok")] * 3
    asyncio.run(learn.learn_from_session(_session("alice", more, "c"), manager, None, "http://local/v1", "util"))
    assert "garden note 10: planted row 10" in calls[0][1]["content"]        # the stretch it missed
    assert learn._seen_messages["c"] == len(more)


def test_a_finished_background_job_whose_chat_is_gone_is_dropped_not_retried_forever(monkeypatch):
    from src import ai_interaction, bg_monitor

    class Sessions:
        def get_session(self, session_id):
            raise KeyError(f"Session {session_id} not found")

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: Sessions())
    done = asyncio.run(bg_monitor._run_followup({"id": "job1", "session_id": "deleted-chat"}))
    assert done is True                                   # handled: the monitor stops asking
