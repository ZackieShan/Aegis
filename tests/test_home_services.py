"""Home: lists and plans, reminders, trackers, boards — the services, the
assistant's tools on top of them, and the API.

What matters: each account only ever sees and changes its own things; lists
and reminders are ordinary notes (so the rest of the app sees them too); a
repeating reminder moves on by itself; pictures are stored privately.
"""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def home(monkeypatch, tmp_path):
    import core.database as cdb
    import services.home as package                     # registers the tracker and board tables
    from services.home import boards, lists, reminders, trackers

    engine = create_engine(f"sqlite:///{tmp_path / 'home.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (lists, reminders, trackers, boards):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    from src import user_time
    user_time.set_user_tz_offset(None)
    user_time.set_user_tz_name(None)
    package.db = factory
    return package


# ── lists ──────────────────────────────────────────────────────────────────

def test_a_list_is_a_checklist_note_owned_by_one_account(home):
    from core.database import Note

    made = home.lists.create_list("alice", "Groceries", ["milk", "eggs", " ", "Milk"])
    assert [i["text"] for i in made["items"]] == ["milk", "eggs"] and made["open"] == 2 and made["kind"] == "list"
    home.lists.create_list("bob", "Groceries", ["beer"])
    assert [l["title"] for l in home.lists.all_lists("alice")] == ["Groceries"]
    assert home.lists.all_lists("alice")[0]["items"][0]["text"] == "milk"
    db = home.db()
    note = db.query(Note).filter(Note.id == made["id"]).one()
    assert note.note_type == "checklist" and note.owner == "alice" and note.source == "home" and not note.due_date
    db.close()
    with pytest.raises(home.HomeError):
        home.lists.get_list("bob", made["id"])                         # not his
    with pytest.raises(home.HomeError):
        home.lists.add_items("bob", made["id"], ["hacked"])
    with pytest.raises(home.HomeError):
        home.lists.create_list("alice", "   ")


def test_adding_ticking_and_clearing_items(home):
    made = home.lists.create_list("alice", "Groceries", ["milk", "eggs"])
    view = home.lists.add_items("alice", made["id"], ["Eggs", "coffee beans"])
    assert [i["text"] for i in view["items"]] == ["milk", "eggs", "coffee beans"]       # no duplicate
    eggs = next(i for i in view["items"] if i["text"] == "eggs")
    view = home.lists.set_item("alice", made["id"], eggs["id"], done=True)
    assert view["open"] == 2 and view["done"] == 1
    view = home.lists.add_items("alice", made["id"], ["eggs"])                           # needed again: un-ticked
    assert view["done"] == 0 and len(view["items"]) == 3
    view = home.lists.check_items("alice", made["id"], ["Milk", "coffee", "bananas"])
    assert view["not_found"] == ["bananas"] and view["done"] == 2
    view = home.lists.clear_done("alice", made["id"])
    assert [i["text"] for i in view["items"]] == ["eggs"]
    view = home.lists.remove_items("alice", made["id"], ["eggs"])
    assert view["items"] == []
    assert home.lists.delete_list("alice", made["id"]) and home.lists.all_lists("alice") == []


def test_a_list_is_found_by_the_name_people_use(home):
    home.lists.create_list("alice", "Groceries", ["milk"])
    home.lists.create_list("alice", "Packing for Maine")
    home.lists.create_list("alice", "Emma's birthday", kind="plan")
    find = lambda name: (home.lists.find_list("alice", name) or {}).get("title")
    assert find("groceries") == "Groceries" and find("the grocery list") == "Groceries"
    assert find("packing") == "Packing for Maine" and find("my packing list") == "Packing for Maine"
    assert find("emma's birthday plan") == "Emma's birthday"
    assert find("christmas") is None and home.lists.find_list("bob", "groceries") is None
    assert [l["kind"] for l in home.lists.all_lists("alice") if l["title"] == "Emma's birthday"] == ["plan"]


def test_notes_with_a_due_time_or_from_the_calendar_are_not_lists(home):
    from core.database import Note

    db = home.db()
    db.add(Note(id="n1", owner="alice", title="Reminder: dentist", note_type="todo", items="[]", due_date="2030-01-01T09:00:00+00:00"))
    db.add(Note(id="n2", owner="alice", title="Calendar thing", note_type="todo", items="[]", label="calendar"))
    db.add(Note(id="n3", owner="alice", title="Just a note", note_type="note", content="hello"))
    db.add(Note(id="n4", owner="alice", title="From the notes screen", note_type="checklist",
                items=json.dumps([{"text": "a", "checked": True}, "b"])))
    db.commit()
    db.close()
    lists = home.lists.all_lists("alice")
    assert [l["title"] for l in lists] == ["From the notes screen"]
    assert [(i["text"], i["done"]) for i in lists[0]["items"]] == [("a", True), ("b", False)]


# ── reminders ──────────────────────────────────────────────────────────────

def test_a_reminder_is_a_note_with_a_due_time(home):
    from core.database import Note

    made = home.reminders.create_reminder("alice", "Pick up the dry cleaning", "2031-03-04T15:00:00-07:00")
    assert made["text"] == "Pick up the dry cleaning" and made["repeat"] == "none" and not made["overdue"]
    assert datetime.fromisoformat(made["due"]) == datetime(2031, 3, 4, 22, 0, tzinfo=timezone.utc)
    again = home.reminders.create_reminder("alice", "pick up the dry cleaning", "2031-03-04T22:00:00+00:00")
    assert again["duplicate"] and again["id"] == made["id"]                          # the same moment, said twice
    db = home.db()
    note = db.query(Note).filter(Note.id == made["id"]).one()
    assert note.owner == "alice" and note.due_date and note.note_type == "todo" and note.source == "home"
    db.close()
    home.reminders.create_reminder("bob", "Call mum", "2031-03-04T10:00:00+00:00")
    assert [r["text"] for r in home.reminders.all_reminders("alice")] == ["Pick up the dry cleaning"]
    assert home.lists.all_lists("alice") == []                                      # a reminder is not a list
    for bad in ("", "sometime, whenever", None):
        with pytest.raises(home.HomeError):
            home.reminders.create_reminder("alice", "x", bad)
    with pytest.raises(home.HomeError):
        home.reminders.update_reminder("bob", made["id"], done=True)


def test_words_for_a_time_are_read_on_the_persons_own_clock(home):
    from src import user_time

    user_time.set_user_tz_offset(-420)                      # seven hours behind UTC
    made = home.reminders.create_reminder("alice", "Call the vet", "tomorrow at 3pm")
    due = datetime.fromisoformat(made["due"]).astimezone(timezone(timedelta(minutes=-420)))
    tomorrow = (datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=-420))) + timedelta(days=1)).date()
    assert (due.date(), due.hour, due.minute) == (tomorrow, 15, 0)


def test_done_archives_a_one_off_and_moves_a_repeating_one_on(home):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)
    once = home.reminders.create_reminder("alice", "Water the plants", past.isoformat())
    daily = home.reminders.create_reminder("alice", "Take vitamins", past.isoformat(), "every day")
    assert once["overdue"] and daily["repeat"] == "daily"
    assert home.reminders.update_reminder("alice", once["id"], done=True)["done"] is True
    moved = home.reminders.update_reminder("alice", daily["id"], done=True)
    assert moved["done"] is False and datetime.fromisoformat(moved["due"]) == past + timedelta(days=1)
    assert [r["text"] for r in home.reminders.all_reminders("alice")] == ["Take vitamins"]
    assert [r["text"] for r in home.reminders.all_reminders("alice", include_done=True)] == ["Take vitamins", "Water the plants"]
    snoozed = home.reminders.snooze("alice", daily["id"], 30)
    assert 28 * 60 < (datetime.fromisoformat(snoozed["due"]) - datetime.now(timezone.utc)).total_seconds() <= 30 * 60
    assert home.reminders.delete_reminder("alice", daily["id"])


def test_next_time_of_a_repeating_reminder():
    from services.home.reminders import next_due, normal_repeat

    start = datetime(2031, 1, 31, 8, 0).astimezone()
    after = start + timedelta(minutes=1)
    assert next_due(start, "daily", after).date() == datetime(2031, 2, 1).date()
    assert next_due(start, "weekly", after).date() == datetime(2031, 2, 7).date()
    assert next_due(start, "monthly", after).date() == datetime(2031, 2, 28).date()             # no 31st: last day
    assert next_due(next_due(start, "monthly", after), "monthly", start + timedelta(days=40)).day in (28, 31)
    assert next_due(datetime(2028, 2, 29, 8, 0).astimezone(), "yearly", datetime(2028, 3, 1).astimezone()).date() \
        == datetime(2029, 2, 28).date()
    assert next_due(start, "none", after) is None and next_due(start, "nonsense", after) is None
    long_ago = datetime(2020, 1, 1, 8, 0).astimezone()
    upcoming = next_due(long_ago, "daily")                                         # skips everything missed
    assert upcoming > datetime.now(timezone.utc) and (upcoming.hour, upcoming.minute) == (8, 0)
    assert [normal_repeat(w) for w in ("every week", "Daily", "annually", "", "fortnightly")] == \
        ["weekly", "daily", "yearly", "none", "none"]


def test_the_scanner_moves_a_delivered_repeating_reminder_to_its_next_time(home):
    import inspect

    from core.database import Note
    from services.home.reminders import advance_after_firing
    from src import builtin_actions

    fired = datetime.now(timezone.utc).replace(microsecond=0)
    note = Note(id="r1", owner="alice", title="Bins out", due_date=fired.isoformat(), repeat="weekly")
    assert advance_after_firing(note) is True
    assert datetime.fromisoformat(note.due_date) == fired + timedelta(days=7)
    once = Note(id="r2", owner="alice", title="Dentist", due_date=fired.isoformat(), repeat="none")
    assert advance_after_firing(once) is False and once.due_date == fired.isoformat()
    odd = Note(id="r3", owner="alice", title="Odd", due_date=fired.isoformat(), repeat="monthly:nth:2:tue")
    assert advance_after_firing(odd) is False                    # richer patterns stay with the notes screen
    source = inspect.getsource(builtin_actions.action_ping_notes)
    assert source.index("dispatch_reminder(") < source.index("advance_after_firing(n)")


# ── trackers ───────────────────────────────────────────────────────────────

def test_a_habit_an_amount_and_a_rating(home, monkeypatch):
    from services.home import trackers

    monkeypatch.setattr("services.home.trackers.user_today", lambda: "2031-05-10")
    monkeypatch.setattr("services.home.common.user_today", lambda: "2031-05-10")
    vitamins = trackers.create_tracker("alice", "Vitamins")
    water = trackers.create_tracker("alice", "Water", kind="number", unit="glasses", goal=8)
    mood = trackers.create_tracker("alice", "Mood", kind="scale")
    assert (vitamins["kind"], water["goal"], mood["kind"]) == ("check", 8.0, "scale")
    assert trackers.create_tracker("alice", "water")["duplicate"]

    for day in ("2031-05-08", "2031-05-09", "2031-05-10"):
        trackers.log("alice", vitamins["id"], day=day)
    view = trackers.log("alice", vitamins["id"])                     # ticking twice is still once
    assert view["done_today"] and view["streak"] == 3 and view["today"] == 1.0
    assert trackers.log("alice", vitamins["id"], value=0)["done_today"] is False       # not done after all
    assert trackers.find_tracker("alice", "vitamins")["streak"] == 2                   # yesterday's run still stands

    trackers.log("alice", water["id"], value=3)
    view = trackers.log("alice", water["id"], value=2, note="after the run")
    assert view["today"] == 5.0 and view["done_today"] is False                        # goal is 8
    assert trackers.log("alice", water["id"], value=3)["done_today"] is True
    assert trackers.log("alice", water["id"], value=-1)["today"] == 7.0                # a correction
    assert trackers.log("alice", water["id"], value=4, day="yesterday")["series"][-2] == {"day": "2031-05-09", "value": 4.0}

    trackers.log("alice", mood["id"], value=2)
    assert trackers.log("alice", mood["id"], value=4)["today"] == 4.0                  # a rating replaces the day's
    for bad in (None, 0, 6):
        with pytest.raises(home.HomeError):
            trackers.log("alice", mood["id"], value=bad)
    with pytest.raises(home.HomeError):
        trackers.log("alice", water["id"], value=1, day="next tuesday-ish")

    detail = trackers.get_tracker("alice", water["id"])
    assert len(detail["entries"]) == 5 and detail["entries"][0]["day"] == "2031-05-10"
    assert trackers.delete_entry("alice", water["id"], detail["entries"][0]["id"])["today"] == 8.0
    assert [t["name"] for t in trackers.all_trackers("alice")] == ["Vitamins", "Water", "Mood"]
    assert trackers.all_trackers("bob") == [] and trackers.find_tracker("bob", "water") is None
    with pytest.raises(home.HomeError):
        trackers.log("bob", water["id"], value=1)
    assert trackers.delete_tracker("alice", water["id"]) and len(trackers.all_trackers("alice")) == 2


# ── boards ─────────────────────────────────────────────────────────────────

def _png(size=(2400, 1200), colour=(200, 80, 40)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def test_pictures_are_resized_and_kept_private_to_their_owner(home, tmp_path):
    board = home.boards.create_board("alice", "Patio ideas")
    assert home.boards.create_board("alice", "patio ideas")["duplicate"]
    pin = home.boards.add_image_pin("alice", board["id"], _png(), title="Stone path")
    assert pin["kind"] == "image" and (pin["width"], pin["height"]) == (1600, 800)
    assert pin["image"] == f"/api/home/pins/{pin['id']}/image" and pin["thumb"].endswith("?thumb=1")
    full = home.boards.image_path("alice", pin["id"])
    thumb = home.boards.image_path("alice", pin["id"], thumb=True)
    assert full.parent == tmp_path / "home" / "pins" / "alice" and full.stat().st_mode & 0o077 == 0
    from PIL import Image
    assert Image.open(thumb).size == (640, 320) and Image.open(full).format == "JPEG"
    assert home.boards.image_path("bob", pin["id"]) is None                   # the id alone opens nothing
    with pytest.raises(home.HomeError):
        home.boards.add_image_pin("alice", board["id"], b"#!/bin/sh\necho not a picture")
    with pytest.raises(home.HomeError):
        home.boards.add_image_pin("bob", board["id"], _png())                # not his board
    view = home.boards.get_board("alice", board["id"])
    assert view["pin_count"] == 1 and view["covers"] == [pin["thumb"]]
    assert home.boards.delete_pin("alice", pin["id"]) and not full.exists() and not thumb.exists()


def test_a_link_is_pinned_with_the_pages_title_and_picture_when_it_can_be_fetched(home, monkeypatch):
    board = home.boards.create_board("alice", "Recipes to try")
    fetched = []

    def online(url):
        fetched.append(url)
        return {"title": "Sourdough for beginners", "description": "A two-day loaf.", "image_url": "https://x/y.jpg"}, _png((900, 600))

    monkeypatch.setattr(home.boards, "_fetch", online)
    pin = home.boards.add_link_pin("alice", board["id"], "example.com/sourdough")
    assert fetched == ["https://example.com/sourdough"]
    assert (pin["kind"], pin["title"], pin["site"], pin["has_image"]) == ("link", "Sourdough for beginners", "example.com", True)
    assert pin["note"] == "A two-day loaf."

    monkeypatch.setattr(home.boards, "_fetch", lambda url: ({"title": "", "description": "", "image_url": ""}, None))
    offline = home.boards.add_link_pin("alice", board["id"], "https://example.org/kimchi", title="Kimchi")
    assert (offline["kind"], offline["title"], offline["has_image"]) == ("link", "Kimchi", False)   # still pinned
    for bad in ("javascript:alert(1)", "ftp://x/y", "not a link", ""):
        with pytest.raises(home.HomeError):
            home.boards.add_link_pin("alice", board["id"], bad)
    with pytest.raises(home.HomeError):
        home.boards.add_link_pin("bob", board["id"], "https://example.com/")
    assert len(fetched) == 1                                   # nothing was fetched for a board that is not his

    note = home.boards.add_note_pin("alice", board["id"], "Ask mum for her bread recipe")
    assert [p["kind"] for p in home.boards.get_board("alice", board["id"])["pins"]] == ["note", "link", "link"]
    other = home.boards.create_board("alice", "Kitchen")
    assert home.boards.update_pin("alice", note["id"], board_id=other["id"])["board_id"] == other["id"]
    assert home.boards.find_board("alice", "the recipes to try board")["name"] == "Recipes to try"
    assert home.boards.all_boards("bob") == []
    picture = home.boards.image_path("alice", pin["id"])
    assert home.boards.delete_board("alice", board["id"]) and not picture.exists()


def test_fetching_goes_through_the_guarded_fetcher_and_never_raises(home, monkeypatch):
    from services.search import content as web

    asked = []

    def refuse(url, headers=None, timeout=0, max_redirects=5, max_bytes=None):
        asked.append((url, max_bytes))
        raise RuntimeError("Blocked non-public URL")

    monkeypatch.setattr(web, "_get_public_url", refuse)
    facts, picture = home.boards._fetch("http://192.168.1.1/admin")
    assert picture is None and facts == {"title": "", "description": "", "image_url": ""}
    assert asked == [("http://192.168.1.1/admin", home.boards.MAX_FETCH_BYTES)]


# ── the assistant's tools ──────────────────────────────────────────────────

def _tool(name, args, owner="alice"):
    from src.agent_tools import TOOL_HANDLERS

    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args), {"owner": owner, "session_id": "chat"}))


def test_lists_tool_creates_adds_ticks_and_reads_back(home):
    out = _tool("manage_lists", {"action": "add", "list": "groceries", "items": ["milk", "eggs"]})
    assert out["exit_code"] == 0 and "Created the list" in out["output"] and "[ ] milk" in out["output"]
    list_id = home.lists.all_lists("alice")[0]["id"]
    assert f"](/home#lists/{list_id})" in out["output"]                     # a link the person can tap
    out = _tool("manage_lists", {"action": "add", "list": "the grocery list", "items": "bread, butter"})
    assert "Added to groceries" in out["output"] and len(home.lists.all_lists("alice")) == 1
    out = _tool("manage_lists", {"action": "check", "list": "groceries", "items": ["milk", "cheese"]})
    assert "[x] milk" in out["output"] and "Not on the list: cheese" in out["output"]
    assert "3 to do, 1 done" in _tool("manage_lists", {"action": "list"})["output"]
    missing = _tool("manage_lists", {"action": "show", "list": "christmas"})
    assert missing["exit_code"] == 1 and "Lists: groceries" in missing["error"]
    assert "no lists yet" in _tool("manage_lists", {"action": "list"}, owner="bob")["output"]      # Bob has none
    assert _tool("manage_lists", {"action": "show", "list": "groceries"}, owner="bob")["exit_code"] == 1


def test_reminders_trackers_and_boards_tools(home, monkeypatch):
    out = _tool("manage_reminders", {"action": "add", "text": "call the vet", "when": "2031-03-04T15:00:00+00:00", "repeat": "weekly"})
    assert out["exit_code"] == 0 and "Reminder set: “call the vet”" in out["output"] and "repeats weekly" in out["output"]
    assert "March 4" in out["output"] and "](/home#reminders)" in out["output"]
    assert _tool("manage_reminders", {"action": "add", "text": "call the vet"})["exit_code"] == 1     # no time given
    assert "call the vet" in _tool("manage_reminders", {"action": "list"})["output"]
    done = _tool("manage_reminders", {"action": "done", "text": "vet"})
    assert "Done: “call the vet”" in done["output"] and "Next time" in done["output"]
    assert _tool("manage_reminders", {"action": "delete", "text": "call the vet"})["exit_code"] == 0
    assert "no reminders" in _tool("manage_reminders", {"action": "list"})["output"]

    assert "Tracker started" in _tool("manage_trackers", {"action": "create", "tracker": "Water", "kind": "number", "unit": "glasses", "goal": 8})["output"]
    logged = _tool("manage_trackers", {"action": "log", "tracker": "water", "value": 3})
    assert "Water: today 3 of 8 glasses" in logged["output"]
    assert "Last 14 days" in _tool("manage_trackers", {"action": "show", "tracker": "water"})["output"]
    assert _tool("manage_trackers", {"action": "log", "tracker": "steps", "value": 1})["exit_code"] == 1

    monkeypatch.setattr(home.boards, "_fetch", lambda url: ({"title": "Small patio makeover", "description": "", "image_url": ""}, None))
    pinned = _tool("manage_boards", {"action": "pin", "board": "patio ideas", "urls": ["https://example.com/a", "https://example.com/b"]})
    assert "Pinned 2 to [patio ideas](/home#boards/" in pinned["output"]
    assert "2 pins" in _tool("manage_boards", {"action": "list"})["output"]
    assert "Small patio makeover (example.com)" in _tool("manage_boards", {"action": "show", "board": "patio"})["output"]
    assert _tool("manage_boards", {"action": "pin", "board": "patio ideas"})["exit_code"] == 1
    assert "no boards yet" in _tool("manage_boards", {"action": "list"}, owner="bob")["output"]


def test_home_tools_are_registered_everywhere_a_tool_must_be():
    from src import agent_loop, tool_index, tool_policy
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.agent_tools.home_tools import HOME_TOOLS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import HOME_TOOLS as SECURITY_HOME_TOOLS, NON_ADMIN_BLOCKED_TOOLS

    schemas = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    for tool in HOME_TOOLS:
        assert tool in TOOL_HANDLERS and tool in TOOL_TAGS and tool in schemas
        assert schemas[tool]["parameters"]["required"] == ["action"]
        assert tool in tool_index.BUILTIN_TOOL_DESCRIPTIONS and tool in agent_loop.TOOL_SECTIONS
        assert tool in agent_loop._DOMAIN_TOOL_MAP["home"] and tool in tool_policy._COMMON_TOOL_NAMES
        assert tool not in NON_ADMIN_BLOCKED_TOOLS and tool in SECURITY_HOME_TOOLS
    assert "home" in agent_loop._DOMAIN_RULES
    hints = next(value._KEYWORD_HINTS for value in vars(tool_index).values()
                 if isinstance(value, type) and hasattr(value, "_KEYWORD_HINTS"))
    hinted = {tool for words, tools in hints.items() for tool in tools}
    assert set(HOME_TOOLS) <= hinted


# ── the API ────────────────────────────────────────────────────────────────

def _api(monkeypatch, user, auth=None):
    import routes.home_routes as routes

    monkeypatch.setattr(routes, "require_user", lambda request: user)
    router = routes.setup_home_routes(auth)
    request = type("Request", (), {"headers": {"x-tz-offset": "-420", "x-tz-name": "America/Phoenix"}})()

    def call(method, path, **kwargs):
        endpoint = next(r.endpoint for r in router.routes if r.path == f"/api/home{path}" and method in r.methods)
        result = endpoint(request=request, **kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    return call


def test_the_home_screen_api_end_to_end(home, monkeypatch):
    from fastapi import HTTPException

    call = _api(monkeypatch, "alice")
    made = call("POST", "/lists", body={"title": "Groceries", "items": ["milk"]})["list"]
    item = call("POST", "/lists/{list_id}/items", list_id=made["id"], body={"text": "eggs"})["list"]["items"][1]
    assert call("PUT", "/lists/{list_id}/items/{item_id}", list_id=made["id"], item_id=item["id"], body={"done": True})["list"]["done"] == 1
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    reminder = call("POST", "/reminders", body={"text": "Call mum", "due": soon})["reminder"]
    tracker = call("POST", "/trackers", body={"name": "Walk", "kind": "check"})["tracker"]
    assert call("POST", "/trackers/{tracker_id}/log", tracker_id=tracker["id"], body={})["tracker"]["done_today"] is True
    board = call("POST", "/boards", body={"name": "Gift ideas"})["board"]
    pin = call("POST", "/boards/{board_id}/pins", board_id=board["id"], body={"text": "A cast iron pan"})["pin"]
    assert pin["kind"] == "note"

    start = call("GET", "/start")
    assert start["counts"] == {"lists": 1, "open_items": 1, "reminders": 1, "overdue": 0, "trackers": 1,
                               "trackers_left": 0, "boards": 1}
    assert [r["id"] for r in start["due"]] == [reminder["id"]] and start["me"]["assistant"] == "Aegis"

    with pytest.raises(HTTPException) as bad:
        call("POST", "/reminders", body={"text": "No time"})
    assert bad.value.status_code == 400 and "needs a time" in bad.value.detail
    with pytest.raises(HTTPException) as bad:
        call("GET", "/pins/{pin_id}/image", pin_id=pin["id"], thumb=0)
    assert bad.value.status_code == 404

    theirs = _api(monkeypatch, "bob")
    assert theirs("GET", "/lists")["lists"] == [] and theirs("GET", "/start")["counts"]["reminders"] == 0
    for path, kwargs in (("/lists/{list_id}", {"list_id": made["id"]}), ("/boards/{board_id}", {"board_id": board["id"]}),
                         ("/trackers/{tracker_id}", {"tracker_id": tracker["id"], "days": 30})):
        with pytest.raises(HTTPException) as refused:
            theirs("GET", path, **kwargs)
        assert refused.value.status_code == 400
