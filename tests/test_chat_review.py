"""The daily review: once a day the house's own model rates how the assistant did.

What must hold:
  * the day's exchanges are read from the chat records as they are — a request, the tools run and what they
    returned, the reply, what the person said next — on the house's clock, with slash commands and the
    copies a forked chat carries left out;
  * what the records themselves show (a failed tool, an answer stopped or asked for again, a slow reply, the
    person having to repeat themselves) is counted without any model, and brings the rating down whatever
    the reviewer says;
  * the reviewer is asked in a fixed form and its answer is used only where it keeps to that form;
  * the words of a chat are kept only for an administrator's own chats and shown only to that administrator;
    every other account's chats are kept as figures and categories, and an account can be left out;
  * a run gives way the moment anyone uses the app, keeps what it had rated, and carries on later; a day is
    reviewed once; a server that was off at the set time catches up;
  * only an administrator reaches any of it; nothing is read or rated until an administrator turns it on;
  * none of it touches the real chats, the real settings or a real model: every chat below is made up.

Names, questions and answers are invented.
"""
import asyncio
import json
import os
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.review import collect, config, judge, report, run, signals

REPO = Path(__file__).resolve().parent.parent
REAL_TELL = run._tell                                 # the real one; the fixture below stands in for it in every test
ZONE = ZoneInfo("America/Chicago")
DAY = "2031-03-24"                                    # a Monday
START, END = collect.window(date.fromisoformat(DAY), ZONE)          # 05:00 UTC to 05:00 UTC


def at(hour, minute=0, day=24):
    """A moment on the house's clock, as the naive UTC the records keep."""
    return datetime(2031, 3, day, hour, minute, tzinfo=ZONE).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def said(role, text, when, **meta):
    said.count = getattr(said, "count", 0) + 1
    return {"id": f"m{said.count}", "role": role, "content": text, "meta": meta, "at": when}


def chat(chat_id, owner, messages, mode="agent", model="house-model"):
    return {"id": chat_id, "owner": owner, "mode": mode, "model": model, "messages": messages}


def tool(name, ok=True, output="done", command="{}"):
    return {"tool": name, "command": command, "exit_code": 0 if ok else 1, "output": output, "round": 1}


@pytest.fixture(autouse=True)
def own_review(tmp_path, monkeypatch):
    """Reviews kept in a scratch folder, settings kept in memory, a clock in Chicago, and nobody using the app."""
    import src.settings as settings
    kept = {}
    monkeypatch.setenv("AEGIS_REVIEWS_DIR", str(tmp_path / "reviews"))
    monkeypatch.setattr(settings, "load_settings", lambda: dict(kept))
    monkeypatch.setattr(settings, "save_settings", lambda new: (kept.clear(), kept.update(new)))
    monkeypatch.setattr(config, "zone", lambda: ZONE)
    monkeypatch.setattr(config, "zone_name", lambda: "America/Chicago")
    monkeypatch.setattr(run, "people", lambda: ({"boss"}, {"mia"}, {"boss": "Boss", "mia": "Mia"}))
    monkeypatch.setattr(run, "_busy", lambda: False)
    monkeypatch.setattr(run, "_candidates", lambda: [("http://model.invalid/v1/chat/completions", "house-model", {})])
    monkeypatch.setattr(run, "_jobs", lambda start, end: {"ran": 4, "failed": 1})
    monkeypatch.setattr(judge, "tool_list", lambda: "- weather: the forecast\n- medicine_info: a medicine's label")
    monkeypatch.setattr(signals, "fits", lambda text: ["medicine_info"] if "calmora" in text.lower() else [])
    run._tried.clear()
    run._settled.clear()
    run.status.update(running=False, day="", done=0, total=0, said="", error="", finished="")
    told = []

    async def tell(summary, when):
        told.append((summary, when))

    monkeypatch.setattr(run, "_tell", tell)
    kept["told"] = told
    yield kept


# ── the day's exchanges ────────────────────────────────────────────────────

def test_a_day_is_the_houses_day_not_the_servers():
    assert (START, END) == (datetime(2031, 3, 24, 5, 0), datetime(2031, 3, 25, 5, 0))                # Chicago is five hours behind in March
    summer = collect.window(date(2031, 7, 1), ZoneInfo("Europe/London"))
    assert summer == (datetime(2031, 6, 30, 23, 0), datetime(2031, 7, 1, 23, 0))


def test_a_messages_words_are_read_whatever_shape_it_was_kept_in():
    assert collect.text_of("plain words") == "plain words"
    assert collect.text_of('[{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "x"}}]') == "what is this"
    assert collect.text_of("[1] the first source says so") == "[1] the first source says so"       # ordinary text that opens with a bracket
    assert collect.text_of("<think>hmm, careful</think>The answer is 4.") == "The answer is 4."
    assert collect.text_of(None) == ""


def test_exchanges_are_a_request_what_was_run_and_the_reply(own_review):
    chats = [chat("c1", "boss", [
        said("user", "old question from the day before", at(22, 0, day=23)),
        said("assistant", "old answer", at(22, 1, day=23), response_time=4.0),
        said("user", "What is the weather tomorrow?", at(9, 0)),
        said("assistant", "Rain from noon, high 12.", at(9, 1), tool_events=[tool("weather", output="Tuesday: rain from noon, high 12")],
             response_time=8.2, requested_model="house-model", model="/srv/models/house-model.gguf", input_tokens=9000, read_tokens=300),
        said("user", "/run uptime", at(9, 5), source="slash"),
        said("assistant", "up 3 days", at(9, 5), source="slash"),
        said("user", "And the day after?", at(9, 10)),
        said("assistant", "", at(9, 11), stopped=True),
        said("user", "tomorrow's question", at(1, 0, day=25)),                                    # 1 am on the 25th: the next day
    ])]
    found = collect.exchanges(chats, START, END)
    assert [one["asked"] for one in found] == ["What is the weather tomorrow?", "And the day after?"]          # the day's own, no slash command
    first, second = found
    assert first["reply"] == "Rain from noon, high 12." and first["owner"] == "boss" and first["turn"] == 2 and first["mode"] == "agent"
    assert first["tools"] == [{"tool": "weather", "args": "{}", "ok": True, "out": "Tuesday: rain from noon, high 12"}]
    assert first["before"] == ("old question from the day before", "old answer") and first["next"] == "And the day after?"
    assert first["model"] == "house-model"                                                          # the name it was asked for by, not the file
    assert second["reply"] == "" and second["meta"]["stopped"] is True and second["next"] == "tomorrow's question"


def test_a_forked_chats_copies_are_counted_once(own_review):
    original = [said("user", "Tidy my notes", at(10, 0)), said("assistant", "Done, three notes merged.", at(10, 1), response_time=12.3),
                said("user", "Now list them", at(10, 5)), said("assistant", "Here they are.", at(10, 6), response_time=3.1)]
    copy = [said("user", "Tidy my notes", at(11, 0)), said("assistant", "Done, three notes merged.", at(11, 0), response_time=12.3),
            said("user", "Now list them", at(11, 2)), said("assistant", "A different answer this time.", at(11, 3), response_time=5.5)]
    found = collect.exchanges([chat("c1", "boss", original), chat("c2", "boss", copy)], START, END)
    assert [(one["chat"], one["asked"]) for one in found] == [("c1", "Tidy my notes"), ("c1", "Now list them"), ("c2", "Now list them")]


def test_the_records_are_read_from_the_database_and_nothing_is_written(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import core.database as database
    engine = create_engine(f"sqlite:///{tmp_path / 'chats.db'}")
    database.Session.__table__.create(engine)
    database.ChatMessage.__table__.create(engine)
    make = sessionmaker(bind=engine)
    db = make()
    db.add(database.Session(id="c1", name="made up", endpoint_url="http://model.invalid", model="house-model", owner="mia", mode="agent"))
    db.add(database.Session(id="c2", name="older", endpoint_url="http://model.invalid", model="house-model", owner="boss", mode="chat"))
    db.add_all([
        database.ChatMessage(id="a", session_id="c1", role="user", content="Before the day", timestamp=at(23, 0, day=23)),
        database.ChatMessage(id="b", session_id="c1", role="user", content="How long do I boil an egg?", timestamp=at(8, 0)),
        database.ChatMessage(id="c", session_id="c1", role="assistant", content="Seven minutes.", timestamp=at(8, 1),
                             meta_data=json.dumps({"response_time": 3.5, "tool_events": [tool("library_search")]})),
        database.ChatMessage(id="d", session_id="c2", role="user", content="Last week's chat", timestamp=at(8, 0, day=17)),
    ])
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", make)
    before = os.path.getmtime(tmp_path / "chats.db")
    chats = collect.read(START, END)
    assert [c["id"] for c in chats] == ["c1"] and chats[0]["owner"] == "mia" and chats[0]["mode"] == "agent"
    assert [m["id"] for m in chats[0]["messages"]] == ["a", "b", "c"] and chats[0]["messages"][2]["meta"]["response_time"] == 3.5
    found = collect.exchanges(chats, START, END)
    assert [(one["asked"], one["reply"], one["before"]) for one in found] == [("How long do I boil an egg?", "Seven minutes.", ("Before the day", ""))]
    assert os.path.getmtime(tmp_path / "chats.db") == before


# ── what the records show ──────────────────────────────────────────────────

def exchange(asked="Add milk to the list", reply="Added.", tools=(), next_said="", owner="boss", mode="agent", turn=1, **meta):
    exchange.count = getattr(exchange, "count", 0) + 1
    return {"id": f"x{exchange.count}", "chat": "c1", "owner": owner, "mode": mode, "at": at(9, exchange.count % 60), "turn": turn, "asked": asked,
            "reply": reply, "tools": [{"tool": t["tool"], "args": t["command"], "ok": t["exit_code"] == 0, "out": t["output"]} for t in tools],
            "meta": meta, "pictures": 0, "model": "house-model", "before": None, "next": next_said}


@pytest.mark.parametrize("now, earlier, again", [
    ("No, I meant the other list", "Add milk to the list", True), ("that's wrong", "x", True), ("Try again", "x", True),
    ("you didn't add it", "x", True), ("I said the shopping list", "x", True),
    ("add milk to the shopping list please", "Add milk to the shopping list", True),           # the same thing, asked twice
    ("Thanks! Now add eggs", "Add milk to the list", False), ("What's the weather?", "Add milk to the list", False),
    ("Not bad at all, thank you", "x", False), ("", "Add milk", False),
])
def test_asking_again_or_correcting_is_told_from_carrying_on(now, earlier, again):
    assert signals.asked_again(now, earlier) is again


def test_the_records_settle_what_needs_no_judgement():
    plain = signals.read(exchange(response_time=6.04))
    assert plain == {"tools": [], "failed": [], "seconds": 6.0, "slow": False, "stopped": False, "redone": 0, "no_reply": False,
                     "repeated_after": False, "refusal_words": False, "fits": [], "from_memory": False, "reread": False}
    assert signals.codes(plain) == []
    rough = signals.read(exchange(reply="I'm sorry, but I can't help with that.", tools=[tool("weather", ok=False, output="no forecast"), tool("web_search")],
                                  next_said="No, that's not what I asked", response_time=96.0, stopped=True, redone=2))
    assert rough["failed"] == ["weather"] and rough["tools"] == ["weather", "web_search"] and rough["refusal_words"]
    assert signals.codes(rough) == ["had_to_repeat", "stopped", "redone", "slow"]
    assert signals.codes(signals.read(exchange(reply=""))) == ["no_reply"]
    memory = signals.read(exchange(asked="What are the side effects of Calmora?", reply="Mostly yawning."))
    assert memory["fits"] == ["medicine_info"] and memory["from_memory"] is True          # a hint for the reviewer, who decides
    assert "from_memory" not in signals.codes(memory)
    looked = signals.read(exchange(asked="What are the side effects of Calmora?", reply="Yawning.", tools=[tool("medicine_info")]))
    assert looked["from_memory"] is False


def test_a_prompt_read_again_in_the_middle_of_a_chat_is_noticed():
    assert signals.read(exchange(turn=3, input_tokens=9000, read_tokens=8000))["reread"] is True
    assert signals.read(exchange(turn=1, input_tokens=9000, read_tokens=8000))["reread"] is False       # a chat's first message reads it all
    assert signals.read(exchange(turn=3, input_tokens=9000, read_tokens=400))["reread"] is False


def test_a_rating_cannot_be_better_than_its_problems_allow():
    assert signals.capped(5, []) == 5 and signals.capped(5, ["too_long"]) == 4 and signals.capped(5, ["had_to_repeat"]) == 3
    assert signals.capped(4, ["from_memory", "slow"]) == 2 and signals.capped(5, ["claimed_done"]) == 1
    assert signals.capped(2, ["too_long"]) == 2                                                        # it only ever brings a rating down
    assert signals.capped(9, []) == 5 and signals.capped(0, []) == 1 and signals.capped("x", []) is None and signals.capped(None, ["slow"]) is None
    assert set(signals.CAPS) <= set(signals.CODES) and set(signals.FROM_RECORDS) <= set(signals.CODES)


# ── the reviewer ───────────────────────────────────────────────────────────

def test_the_reviewer_reads_what_a_careful_person_would_look_at():
    one = exchange(asked="What are the side effects of Calmora?", reply="Mostly yawning and hiccups.",
                   tools=[tool("web_search", output="Calmora: common reactions are yawning"), tool("web_fetch", ok=False, output="timed out")],
                   next_said="Is that from the label?", response_time=31.0, redone=1)
    one["before"] = ("Hello", "Hi, how can I help?")
    page = judge.page(one, "e1", signals.read(one), family=True)
    assert page.startswith("EXCHANGE e1 — the family screen: someone on a phone who is not technical.")
    assert "Just before: the person wrote “Hello” and it replied “Hi, how can I help?”" in page
    assert "The person wrote:\nWhat are the side effects of Calmora?" in page
    assert " 1. web_search {} -> worked: Calmora: common reactions are yawning" in page and " 2. web_fetch {} -> FAILED: timed out" in page
    assert "Its reply:\nMostly yawning and hiccups." in page and "What the person wrote next: “Is that from the label?”" in page
    assert page.endswith("On record: took 31 s; the person asked for the answer again 1 time(s); failed tools: web_fetch; "
                         "by its wording the request is one for: medicine_info (not used).")
    bare = judge.page(exchange(reply="", mode="chat"), "e2", signals.read(exchange(reply="")), family=False)
    assert "it has NO tools here" in bare and "It ran no tools." in bare and "(no reply was given)" in bare


def test_a_long_exchange_is_cut_to_fit_not_sent_whole():
    long = exchange(asked="word " * 2000, reply="reply " * 3000, tools=[tool("web_search", output="x" * 5000)] * 14)
    page = judge.page(long, "e1", signals.read(long), family=False)
    assert len(page) < 16000 and " […]" in page and "… and 4 more tool calls." in page


def test_the_reviewer_is_given_the_rubric_the_words_it_may_use_and_the_tools():
    system, user = judge.prompt(["EXCHANGE e1 — made up", "EXCHANGE e2 — made up"])
    assert system["role"] == "system" and user["content"].endswith("Review these 2 exchange(s). JSON only.")
    for code in signals.CODES:
        assert (f'- "{code}": ' in system["content"]) is (code not in signals.FROM_RECORDS), code      # what the records settle is not asked
    assert "- medicine_info: a medicine's label" in system["content"] and '{"reviews": [{"id": "e1"' in system["content"]
    assert "You cannot check facts yourself." in system["content"]


def test_only_an_answer_in_the_asked_for_shape_is_used():
    answer = """<think>let me see</think>```json
{"reviews": [
 {"id": "e1", "rating": 2, "problems": ["from_memory", "made_up_word", "from_memory"], "tool": "medicine_info", "note": " Answered  from memory. ", "fix": "Use the label."},
 {"id": "e2", "rating": 9, "problems": []},
 {"id": "e3", "rating": "four", "problems": []},
 {"id": "e9", "rating": 5, "problems": []},
 {"id": "e4", "rating": 5, "problems": "none", "tool": "rm -rf /", "note": "Fine."}
]}
```"""
    found = judge.parse(answer, ["e1", "e2", "e3", "e4"])
    assert found == {"e1": {"rating": 2, "problems": ["from_memory"], "tool": "medicine_info", "note": "Answered from memory.", "fix": "Use the label."},
                     "e4": {"rating": 5, "problems": [], "tool": "", "note": "Fine.", "fix": ""}}       # a rating out of range, or not a number, is no review
    for garbage in ("", "I think it went well.", "{not json}", '{"reviews": "none"}', '["e1"]'):
        assert judge.parse(garbage, ["e1"]) == {}


def test_the_reviewer_is_asked_again_only_for_what_it_skipped(monkeypatch):
    asked = []

    async def ask(messages, candidates, timeout=240):
        asked.append(messages[1]["content"])
        if len(asked) == 1:
            return '{"reviews": [{"id": "e1", "rating": 5, "problems": []}]}'               # it skipped the second
        return '{"reviews": [{"id": "e2", "rating": 3, "problems": ["not_done"]}]}'

    monkeypatch.setattr(judge, "ask", ask)
    first, second = exchange(asked="one"), exchange(asked="two")
    found = asyncio.run(judge.rate([(first, signals.read(first), False), (second, signals.read(second), False)], [("u", "m", {})]))
    assert {key: value["rating"] for key, value in found.items()} == {first["id"]: 5, second["id"]: 3}
    assert len(asked) == 2 and "EXCHANGE e1" in asked[0] and "EXCHANGE e2" in asked[0] and "EXCHANGE e1" not in asked[1] and "EXCHANGE e2" in asked[1]


def test_the_review_is_made_without_copying_chats_into_the_trace_store():
    source = (REPO / "services" / "review" / "judge.py").read_text()
    assert "from src.llm_core import _llm_call_async_impl" in source and "llm_call_async(" not in source.replace("_llm_call_async_impl(", "")
    assert 'workload="nightly"' in source


def test_the_models_gate_holds_a_nightly_job_for_a_person_not_for_a_screen_left_on(monkeypatch):
    from src import interactive_gate as gate
    from src import llm_core
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: True)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)
    state = {"tab_open": True, "working": False}
    monkeypatch.setattr(gate, "has_foreground_activity", lambda: state["tab_open"] or state["working"])
    monkeypatch.setattr(gate, "someone_is_working", lambda: state["working"])

    async def got_in(workload, within=0.6):
        async def use():
            async with llm_core._local_model_slot("http://127.0.0.1:1/v1", "house-model", workload):
                return True
        try:
            return await asyncio.wait_for(use(), within)
        except asyncio.TimeoutError:
            return False

    assert asyncio.run(got_in("nightly")) is True                      # a tab is open, nobody is doing anything: the review goes ahead
    assert asyncio.run(got_in("background")) is False                  # ordinary background work still waits for the tab, as before
    state["working"] = True
    assert asyncio.run(got_in("nightly")) is False                     # someone is using the app: it waits
    assert llm_core._gate_workload("nightly") == "background"          # and a chat that wants the model still takes it away


def test_a_chat_that_takes_the_model_away_pauses_the_review_and_does_not_end_the_nightly_job(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    config.update(on=True, time="04:00")
    taken = {"left": 1}
    real_rate = judge.rate
    reviewer(monkeypatch)

    async def rate(batch, candidates):
        if taken["left"]:
            taken["left"] -= 1
            raise asyncio.CancelledError()                               # what the model gate does to a background call
        return await real_rate(batch, candidates)

    monkeypatch.setattr(judge, "rate", rate)
    assert asyncio.run(run.nightly(clock(4, 0))) is None                 # paused, not crashed: nightly() came back
    assert run.status["said"].startswith("Paused") and report.load(DAY) is None
    run._tried[DAY] -= run.RETRY_MINUTES * 60 + 1
    assert asyncio.run(run.nightly(clock(4, 20)))["exchanges"] == 4      # and the next look finishes the day
    monkeypatch.setattr(run.asyncio, "sleep", lambda seconds: real_sleep(0))
    taken["left"] = 2
    assert asyncio.run(run.run(DAY, force=True, polite=False))["exchanges"] == 4      # asked for by an administrator: it asks again


real_sleep = asyncio.sleep


# ── what is kept, and who sees it ──────────────────────────────────────────

def test_only_an_administrators_own_chat_keeps_its_words():
    one = exchange(asked="What are the side effects of Calmora? " + "and more " * 40, reply="Yawning.", tools=[tool("web_fetch", ok=False, output="timed out\nmore lines")],
                   response_time=20.0)
    found = signals.read(one)
    review = {"rating": 4, "problems": ["from_memory"], "tool": "medicine_info", "note": "Answered about Calmora from memory.", "fix": "Use the label for Calmora."}
    mine = report.entry(one, found, review, "tools", keep_words=True)
    assert mine["rating"] == 2 and mine["problems"] == ["from_memory"] and mine["tool"] == "medicine_info"        # the rating follows the problem
    assert mine["asked"].startswith("What are the side effects of Calmora?") and len(mine["asked"]) == report.EXCERPT + 1 and mine["asked"].endswith("…")
    assert mine["note"] == "Answered about Calmora from memory." and mine["errors"] == ["timed out more lines"] and mine["chat"] == "c1"
    theirs = report.entry(one, found, review, "family", keep_words=False)
    assert theirs == {"id": one["id"], "owner": "boss", "agent": "family", "model": "house-model", "at": one["at"].isoformat(timespec="seconds"),
                      "rating": 2, "problems": ["from_memory"], "tools": ["web_fetch"], "failed": ["web_fetch"], "tool": "medicine_info",
                      "seconds": 20.0, "reread": False, "refusal_words": False, "words": False}
    assert "Calmora" not in json.dumps(theirs)                                                         # never what was said
    unrated = report.entry(one, found, None, "tools", keep_words=True)
    assert unrated["rating"] is None and unrated["note"] == ""
    assert report.entry(exchange(reply=""), signals.read(exchange(reply="")), None, "tools", False)["rating"] == 1       # no reply at all is a 1


def make_entries():
    rows = [
        ("boss", "tools", exchange(asked="What is Calmora for?", reply="For restlessness."), {"rating": 4, "problems": ["from_memory"], "tool": "medicine_info", "note": "From memory.", "fix": "Use medicine_info."}),
        ("boss", "tools", exchange(asked="Find a bakery near me", reply="I could not look.", tools=[tool("find_places", ok=False, output="find_places: No home position is saved, so I do not know where “nearby” is.")]),
         {"rating": 3, "problems": [], "tool": "", "note": "Said it could not look.", "fix": ""}),
        ("boss", "tools", exchange(asked="Thanks", reply="You're welcome."), {"rating": 5, "problems": [], "tool": "", "note": "Fine.", "fix": ""}),
        ("mia", "family", exchange(asked="Something private of Mia's", reply="A private answer.", owner="mia", tools=[tool("web_fetch", ok=False, output="timed out")], response_time=99.0),
         {"rating": 4, "problems": ["tool_failed"], "tool": "", "note": "Mia asked about something private.", "fix": "A fix naming Mia's private thing."}),
        ("mia", "family", exchange(asked="Another private thing", reply="Sure.", owner="mia", next_said="No, that's wrong"), {"rating": 5, "problems": [], "tool": "", "note": "", "fix": ""}),
    ]
    entries, details = [], {}
    for owner, agent, one, review in rows:
        entries.append(report.entry(one, signals.read(one), review, agent, keep_words=owner == "boss"))
        details[one["id"]] = {"errors": [t["out"] for t in one["tools"] if not t["ok"]]}
    return entries, details


def test_the_day_is_put_together_with_a_score_for_each_assistant_and_what_to_change():
    entries, details = make_entries()
    made = report.build(DAY, entries, details, "house-model", chats=2, left_out=1, jobs={"ran": 4, "failed": 1})
    assert made["totals"] == {"chats": 2, "exchanges": 5, "rated": 5, "score": 3.0, "needs_a_look": 4, "left_out": 1,
                              "by_rating": {"5": 1, "4": 0, "3": 2, "2": 2, "1": 0}}
    assert [(row["label"], row["exchanges"], row["score"], row["needs_a_look"]) for row in made["agents"]] == [
        ("Full app, with tools", 3, 3.3, 2), ("Family screen", 2, 2.5, 2)]
    assert [(row["id"], row["exchanges"], row["score"]) for row in made["people"]] == [("boss", 3, 3.3), ("mia", 2, 2.5)]
    assert made["problems"] == {"from_memory": 1, "tool_failed": 1, "slow": 1, "had_to_repeat": 1}
    assert made["jobs"] == {"ran": 4, "failed": 1} and made["model"] == "house-model" and made["day"] == DAY
    improve = {item["what"]: item for item in made["improve"]}
    assert improve["Set the household's home town (Admin, Household)."]["kind"] == "setting"
    assert improve["Get medicine_info used for questions it fits."]["count"] == 1
    assert improve["Look at why web_fetch fails."]["why"] == "It failed in 1 exchange(s)."            # how often, never what it said
    assert "Look at why find_places fails." not in improve                                            # that one has its real cause above
    assert improve["Find what makes these answers slow."]["why"] == "They took over a minute (the middle one 99 s)."
    assert "Read these exchanges: the person had to ask twice." in improve and "Make the assistant say so when a tool fails." in improve
    named = {code for code, *_ in report.ADVICE} | {"from_memory", "wrong_tool"} | set(signals.FROM_RECORDS)
    assert named >= set(signals.CODES)                                                                # every problem leads to something to do
    assert made["improve"][0]["count"] >= made["improve"][-1]["count"]
    assert "private" not in json.dumps({key: made[key] for key in made if key != "exchanges"})


def test_a_review_is_kept_private_and_found_again_by_its_day(own_review, tmp_path):
    entries, details = make_entries()
    path = report.save(report.build(DAY, entries, details, "house-model", chats=2))
    assert path == str(tmp_path / "reviews" / f"{DAY}.json") and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "reviews").st_mode) == 0o700
    assert report.load(DAY)["totals"]["exchanges"] == 5 and report.load("2031-03-23") is None
    report.save(report.build("2031-03-22", entries[:1], details, "house-model", chats=1))
    assert report.days() == [DAY, "2031-03-22"]
    assert report.history() == [{"day": DAY, "score": 3.0, "exchanges": 5, "chats": 2, "needs_a_look": 4},
                                {"day": "2031-03-22", "score": 2.0, "exchanges": 1, "chats": 1, "needs_a_look": 1}]
    report.prune(keep=1)
    assert report.days() == [DAY]
    assert report.load("../../etc/passwd") is None                                          # a day is a date, never a path
    with pytest.raises(ValueError):
        report.save({"day": "../../somewhere", "exchanges": []})


def test_a_reader_sees_the_words_of_their_own_chats_and_of_nobody_elses():
    entries, details = make_entries()
    made = report.build(DAY, entries, details, "house-model", chats=2)
    mine = report.for_reader(made, "boss", {"boss": "Boss", "mia": "Mia"})
    own = [one for one in mine["exchanges"] if one["mine"]]
    assert len(own) == 3 and own[0]["asked"] == "What is Calmora for?" and own[0]["note"] == "From memory." and own[0]["who"] == "You"
    others = [one for one in mine["exchanges"] if not one["mine"]]
    assert [one["who"] for one in others] == ["Mia", "Mia"] and all(set(one) & {"asked", "note", "fix", "chat", "errors", "owner"} == set() for one in others)
    assert others[0]["rating"] == 2 and others[0]["problems"] == ["tool_failed", "slow"] and others[0]["tools"] == ["web_fetch"]
    assert "private" not in json.dumps(mine) and "private" not in json.dumps(made)                    # Mia's words were never kept at all
    assert [fix["fix"] for fix in mine["fixes"]] == ["Use medicine_info."]                             # the reviewer's advice on the reader's own chats
    assert [row["label"] for row in mine["people"]] == ["You", "Mia"]
    second = report.for_reader(made, "other-admin", {"boss": "Boss", "mia": "Mia"})                    # another administrator
    assert not any(one["mine"] for one in second["exchanges"]) and "Calmora" not in json.dumps(second) and second["fixes"] == []
    assert all(item["examples"] == [] for item in second["improve"])


# ── the job ────────────────────────────────────────────────────────────────

def two_chats():
    return [
        chat("c1", "boss", [said("user", "What is Calmora for?", at(9, 0)), said("assistant", "For restlessness.", at(9, 1), response_time=5.0),
                            said("user", "Add oats to my list", at(9, 5)),
                            said("assistant", "Added oats.", at(9, 6), tool_events=[tool("manage_lists")], response_time=4.0)]),
        chat("c2", "mia", [said("user", "Mia's private question", at(18, 0)), said("assistant", "Mia's private answer.", at(18, 1), response_time=7.0),
                           said("user", "no, that's wrong", at(18, 3)), said("assistant", "Sorry, here it is.", at(18, 4), response_time=6.0)]),
    ]


def reviewer(monkeypatch, verdicts=None, fail=0):
    """A stand-in for the model: rates every exchange it is shown, from ``verdicts`` (by a word in the request) or 5."""
    calls = []

    async def ask(messages, candidates, timeout=240):
        calls.append(messages[1]["content"])
        if len(calls) <= fail:
            raise RuntimeError("the model is not answering")
        reviews = []
        for part in messages[1]["content"].split("EXCHANGE ")[1:]:
            label = part.split(" ", 1)[0]
            asked = part.split("The person wrote", 1)[1].split("\n")[1]
            rating, problems = next(((r, p) for word, (r, p) in (verdicts or {}).items() if word in asked), (5, []))
            reviews.append({"id": label, "rating": rating, "problems": problems, "tool": "medicine_info" if "from_memory" in problems else "",
                            "note": f"note about {asked[:20]}", "fix": "a fix"})
        return json.dumps({"reviews": reviews})

    monkeypatch.setattr(judge, "ask", ask)
    return calls


def test_a_day_is_reviewed_kept_and_told(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    calls = reviewer(monkeypatch, {"Calmora": (4, ["from_memory"])})
    summary = asyncio.run(run.run(DAY))
    assert summary == {"day": DAY, "score": 3.8, "exchanges": 4, "chats": 2, "needs_a_look": 2}
    assert run.said(summary) == f"{DAY}: 3.8 out of 5. 4 exchanges in 2 chats; 2 need a look."
    assert len(calls) == 2                                                                 # one request to the model for each chat
    kept = report.load(DAY)
    ratings = {(one["owner"], one["rating"]): one for one in kept["exchanges"]}
    assert sorted(ratings) == [("boss", 2), ("boss", 5), ("mia", 3), ("mia", 5)]           # from_memory brings a 4 down to 2; "that's wrong" a 5 to 3
    assert ratings[("mia", 3)]["problems"] == ["had_to_repeat"] and "private" not in json.dumps(kept)
    assert ratings[("boss", 2)]["asked"] == "What is Calmora for?" and kept["jobs"] == {"ran": 4, "failed": 1}
    assert own_review["told"] == [(summary, "problems")]
    assert run.status["running"] is False and run.status["done"] == 4 and run.status["total"] == 4
    assert not os.path.exists(run._state_path())                                           # nothing left half-done
    with pytest.raises(run.Nothing, match="already reviewed"):
        asyncio.run(run.run(DAY))
    asyncio.run(run.run(DAY, force=True))                                                   # an administrator may ask for it again


def test_there_is_nothing_to_review_when_nobody_chatted_or_everyone_is_left_out(own_review, monkeypatch):
    monkeypatch.setattr(collect, "read", lambda start, end: [])
    with pytest.raises(run.Nothing, match=f"nobody chatted on {DAY}"):
        asyncio.run(run.run(DAY))
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    config.update(skip=["boss", "mia"])
    calls = reviewer(monkeypatch)
    with pytest.raises(run.Nothing, match="left out"):
        asyncio.run(run.run(DAY))
    assert calls == [] and report.load(DAY) is None


def test_an_account_left_out_is_never_shown_to_the_reviewer(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    config.update(skip=["mia"])
    calls = reviewer(monkeypatch)
    summary = asyncio.run(run.run(DAY))
    assert summary["exchanges"] == 2 and report.load(DAY)["totals"]["left_out"] == 2
    assert len(calls) == 1 and "private" not in calls[0] and {one["owner"] for one in report.load(DAY)["exchanges"]} == {"boss"}


def test_a_polite_run_gives_way_and_carries_on_from_where_it_stopped(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    calls = reviewer(monkeypatch)
    busy = {"after": 1}
    monkeypatch.setattr(run, "_busy", lambda: len(calls) >= busy["after"])                  # someone starts chatting after the first chat is rated
    with pytest.raises(run.Interrupted):
        asyncio.run(run.run(DAY))
    assert len(calls) == 1 and report.load(DAY) is None
    assert len(json.load(open(run._state_path()))["reviews"]) == 2                          # what was rated is kept
    assert run.status["said"].startswith("Paused: someone started using the app.")
    busy["after"] = 99
    summary = asyncio.run(run.run(DAY))
    assert len(calls) == 2 and "Mia's private question" in calls[1] and "Calmora" not in calls[1]       # only the other chat was asked about
    assert summary["exchanges"] == 4 and report.load(DAY)["totals"]["rated"] == 4


def test_a_run_asked_for_by_an_administrator_does_not_give_way(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    calls = reviewer(monkeypatch)
    monkeypatch.setattr(run, "_busy", lambda: True)
    assert asyncio.run(run.run(DAY, polite=False))["exchanges"] == 4 and len(calls) == 2


def test_a_model_that_stops_answering_ends_the_run_and_keeps_what_was_rated(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    reviewer(monkeypatch, fail=99)
    with pytest.raises(RuntimeError, match="The model stopped answering during the review"):
        asyncio.run(run.run(DAY))
    assert report.load(DAY) is None and run.status["error"].startswith("RuntimeError: The model stopped answering")
    reviewer(monkeypatch, fail=1)                                                           # one stumble: that chat is kept as not rated, the rest goes on
    summary = asyncio.run(run.run(DAY, force=True))
    assert summary["exchanges"] == 4 and report.load(DAY)["totals"]["rated"] == 2


def test_two_runs_never_go_at_once(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    reviewer(monkeypatch)

    async def both():
        first = asyncio.ensure_future(run.run(DAY))
        await asyncio.sleep(0)
        with pytest.raises(run.Nothing, match="already running"):
            await run.run(DAY, force=True)
        return await first

    assert asyncio.run(both())["exchanges"] == 4


# ── once a day ─────────────────────────────────────────────────────────────

def clock(hour, minute=0, day=25):
    return datetime(2031, 3, day, hour, minute, tzinfo=ZONE)


def test_it_is_off_until_an_administrator_turns_it_on(own_review):
    assert config.get() == {"on": False, "time": "03:30", "skip": [], "notify": "problems"}
    assert run.due(clock(4)) is None and run.next_time(clock(4)) is None
    config.update(on=True, time="04:00")
    assert run.due(clock(3, 59)) is None                                                    # not yet today
    assert run.due(clock(4, 0)) == DAY and run.due(clock(23, 30)) == DAY                    # the day before, any time from then on
    assert run.next_time(clock(3, 0)) == clock(4, 0) and run.next_time(clock(4, 0)) == clock(4, 0, day=26)


def test_a_day_is_reviewed_once_and_a_server_that_was_off_catches_up(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    reviewer(monkeypatch)
    config.update(on=True, time="04:00")
    assert asyncio.run(run.nightly(clock(3, 0))) is None                                    # before the time: nothing
    assert asyncio.run(run.nightly(clock(9, 15)))["day"] == DAY                             # the server came up at 9:15: yesterday is still reviewed
    assert run.due(clock(9, 16)) is None and asyncio.run(run.nightly(clock(22, 0))) is None  # and only once


def test_the_nightly_job_waits_for_quiet_and_tries_an_interrupted_day_again_later(own_review, monkeypatch):
    chats = two_chats()
    monkeypatch.setattr(collect, "read", lambda start, end: chats)
    calls = reviewer(monkeypatch)
    config.update(on=True, time="04:00")
    monkeypatch.setattr(run, "_busy", lambda: True)
    assert asyncio.run(run.nightly(clock(4, 0))) is None and calls == [] and DAY not in run._tried      # someone is up: it does not even start
    monkeypatch.setattr(run, "_busy", lambda: len(calls) >= 1)
    assert asyncio.run(run.nightly(clock(4, 1))) is None and len(calls) == 1                # started, then gave way
    assert run.due(clock(4, 2)) is None                                                     # not again straight away
    run._tried[DAY] -= run.RETRY_MINUTES * 60 + 1
    monkeypatch.setattr(run, "_busy", lambda: False)
    assert asyncio.run(run.nightly(clock(4, 20)))["exchanges"] == 4 and len(calls) == 2


def test_a_day_with_nothing_to_review_is_not_asked_about_all_day(own_review, monkeypatch):
    reads = []
    monkeypatch.setattr(collect, "read", lambda start, end: reads.append(1) or [])
    config.update(on=True, time="04:00")
    assert asyncio.run(run.nightly(clock(4, 0))) is None and run._settled == {DAY: f"nobody chatted on {DAY}"}
    assert run.due(clock(12, 0)) is None and asyncio.run(run.nightly(clock(12, 0))) is None and len(reads) == 1


def test_the_administrators_are_told_only_as_they_asked(monkeypatch):
    from services import notify
    sent = []

    async def tell_admins(title, body="", url="/admin", tag=None, but=None):
        sent.append((title, body, url, tag))
        return 1

    monkeypatch.setattr(notify, "tell_admins", tell_admins)
    good = {"day": DAY, "score": 4.8, "exchanges": 6, "chats": 2, "needs_a_look": 0}
    poor = {"day": DAY, "score": 3.1, "exchanges": 6, "chats": 2, "needs_a_look": 3}
    asyncio.run(REAL_TELL(good, "problems"))
    asyncio.run(REAL_TELL(poor, "never"))
    assert sent == []                                                                       # all well, or they asked not to be told
    asyncio.run(REAL_TELL(poor, "problems"))
    asyncio.run(REAL_TELL(good, "always"))
    assert sent == [("Daily review: 3.1 out of 5", "3.1 out of 5. 6 exchanges in 2 chats; 3 need a look.", "/admin#review", "daily-review"),
                    ("Daily review: 4.8 out of 5", "4.8 out of 5. 6 exchanges in 2 chats; none needs a look.", "/admin#review", "daily-review")]


def test_settings_that_make_no_sense_are_refused(own_review):
    for change, words in (({"time": "4am"}, "like 03:30"), ({"time": "25:00"}, "like 03:30"), ({"notify": "sometimes"}, "always, or never"),
                          ({"skip": "mia"}, "list of names"), ({"on": "yes"}, "true or false")):
        with pytest.raises(config.Refused, match=words):
            config.update(**change)
    assert config.get()["on"] is False
    assert config.update(on=True, time="22:15", skip=["Mia ", "mia", ""], notify="always") == {"on": True, "time": "22:15", "skip": ["mia"], "notify": "always"}
    own_review["chat_review"]["time"] = "nonsense"                                          # a damaged setting falls back, it does not break the job
    assert config.get()["time"] == "03:30"


def test_a_screen_left_open_does_not_hold_the_review_back_but_a_person_does(monkeypatch):
    from src import interactive_gate as gate
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 0)
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", 0.0)
    monkeypatch.setattr(gate, "_LAST_BROWSER_ACTIVITY", 1000.0)                             # a visible tab's heartbeat, a moment ago
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    assert gate.someone_is_working(now=1001.0) is False and gate.has_foreground_activity(now=1001.0) is True
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", 990.0)                                      # a real request eleven seconds ago
    assert gate.someone_is_working(now=1001.0) is True and gate.someone_is_working(quiet_seconds=5, now=1001.0) is False
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", 0.0)
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 1)
    assert gate.someone_is_working(now=1001.0) is True
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 0)
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: True)                      # an answer still being written
    assert gate.someone_is_working(now=1001.0) is True
    # What an open tab asks for on its own, every few seconds or once a minute, is not a person at work.
    for path in ("/api/activity/heartbeat", "/api/research/active", "/api/email/urgency-state", "/api/email/unread-state", "/api/tasks/notifications"):
        assert gate.should_track_interactive_request(path) is False, path
    assert gate.should_track_interactive_request("/api/chat_stream", "POST") is True


def test_a_review_that_is_due_but_waiting_says_so_once_not_every_minute(own_review, monkeypatch, caplog):
    import logging
    config.update(on=True, time="04:00")
    monkeypatch.setattr(run, "_busy", lambda: True)
    run._waiting.clear()
    with caplog.at_level(logging.INFO, logger="services.review.run"):
        for minute in range(5):
            assert asyncio.run(run.nightly(clock(4, minute))) is None
    assert [record.getMessage() for record in caplog.records] == [f"daily review of {DAY} is due; waiting until nobody is using the app"]


# ── the console ────────────────────────────────────────────────────────────

@pytest.fixture
def console(own_review, monkeypatch):
    import routes.review_routes as routes
    who = {"user": "boss", "admin": True}

    def need_admin(request):
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(routes, "require_admin", need_admin)
    monkeypatch.setattr(routes, "get_current_user", lambda request: who["user"])
    app = FastAPI()
    app.include_router(routes.setup_review_routes())
    client = TestClient(app)
    client.who = who
    return client


def test_the_console_shows_the_settings_the_people_and_the_latest_review(console):
    start = console.get("/api/admin/review").json()
    assert start["settings"] == {"on": False, "time": "03:30", "skip": [], "notify": "problems"} and start["review"] is None and start["history"] == []
    assert start["next"] is None and start["zone"] == "America/Chicago" and start["status"]["running"] is False
    assert start["people"] == [{"username": "boss", "name": "Boss", "admin": True, "family": False, "included": True},
                               {"username": "mia", "name": "Mia", "admin": False, "family": True, "included": True}]
    assert set(start["codes"]) == set(signals.CODES) and start["agents"]["family"] == "Family screen"
    entries, details = make_entries()
    report.save(report.build(DAY, entries, details, "house-model", chats=2))
    shown = console.get("/api/admin/review").json()
    assert shown["history"][0]["day"] == DAY and shown["review"]["day"] == DAY
    assert "private" not in json.dumps(shown) and sum(1 for one in shown["review"]["exchanges"] if one["mine"]) == 3
    assert console.get(f"/api/admin/review/{DAY}").json()["totals"]["exchanges"] == 5
    assert console.get("/api/admin/review/2031-01-01").status_code == 404 and console.get("/api/admin/review/yesterday").status_code == 400
    console.who["user"] = "other-admin"
    assert not any(one["mine"] for one in console.get(f"/api/admin/review/{DAY}").json()["exchanges"])


def test_an_administrator_turns_it_on_sets_the_time_and_leaves_someone_out(console, own_review):
    saved = console.put("/api/admin/review", json={"on": True, "time": "04:00", "skip": ["mia"], "notify": "always"}).json()
    assert saved["settings"] == {"on": True, "time": "04:00", "skip": ["mia"], "notify": "always"} and saved["next"] is not None
    assert [person["included"] for person in saved["people"]] == [True, False]
    refused = console.put("/api/admin/review", json={"time": "late"})
    assert refused.status_code == 400 and refused.json()["detail"] == "Give the time as hours and minutes, like 03:30."
    assert config.get()["time"] == "04:00"


def test_an_administrator_can_have_a_day_reviewed_now(console, monkeypatch):
    started = []

    async def go(day=None, progress=None, force=False, polite=True):
        started.append((day, force, polite))
        return {"day": day}

    monkeypatch.setattr(run, "run", go)
    monkeypatch.setattr(run, "yesterday", lambda: DAY)
    monkeypatch.setattr(run, "today", lambda: "2031-03-25")
    assert console.post("/api/admin/review/run", json={"day": "yesterday"}).json() == {"started": True, "day": DAY}
    assert console.post("/api/admin/review/run", json={"day": "today"}).json()["day"] == "2031-03-25"
    assert console.post("/api/admin/review/run", json={"day": "2031-03-20"}).json()["day"] == "2031-03-20"
    assert console.post("/api/admin/review/run", json={"day": "last week"}).status_code == 400
    assert started == [(DAY, True, False), ("2031-03-25", True, False), ("2031-03-20", True, False)]       # at once, and to the end
    run.status["running"] = True
    assert console.post("/api/admin/review/run", json={}).status_code == 409


def test_nobody_but_an_administrator_reaches_the_review(console):
    entries, details = make_entries()
    report.save(report.build(DAY, entries, details, "house-model", chats=2))
    console.who.update(user="mia", admin=False)
    assert console.get("/api/admin/review").status_code == 403 and console.get(f"/api/admin/review/{DAY}").status_code == 403
    assert console.put("/api/admin/review", json={"on": True}).status_code == 403 and console.post("/api/admin/review/run", json={}).status_code == 403
    assert config.get()["on"] is False


def test_the_review_is_part_of_the_app_and_of_the_console():
    app_py = (REPO / "app.py").read_text()
    assert "app.include_router(setup_review_routes())" in app_py and "_review_run.keep_time()" in app_py
    script = (REPO / "static" / "lite" / "admin.js").read_text()
    assert "['review', 'Review']" in script and "review: loadReview" in script and "drawReview();" in script
    assert "never what was said" in script and "Only you see this." in script
    import re
    named = set(re.findall(r"([a-z_]+): '", script.split("const PROBLEM = {")[1].split("};")[0]))
    assert named == set(signals.CODES)                                                      # every problem has its words on the page
