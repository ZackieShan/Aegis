"""One request that needs several blades, done as one job.

What must hold:
  * a person's own words set a smart tool off — never a reminder or a list that merely mentions an
    outing or a meal, and never "what can I make with leftover wood";
  * an outing is planned by fixed steps (the sun, the forecast, the library, a saved list, a reminder)
    and answered with a trip card: times that fit the kind of outing, warnings chosen by rule from the
    forecast with a library page beside each, the packing list, the reminder;
  * the plan stands with no model (the usual essentials, plain advice), leaves out what the person may
    not use, and says so honestly when it had to assume a day, a length or a place;
  * "it will be about 6 hours" and "make it Sunday" plan the same outing again, and the packing list is
    not doubled;
  * dinner is found in the cookbooks, one recipe is chosen, set out in the Kitchen in its own words,
    what is missing is worked out from what the person said they have, and a start-by reminder is set —
    and when no cookbook has it, that is said before anything from general knowledge;
  * a recipe that could not be set out is not saved;
  * "add the ingredients for … to my shopping list" reads the recipe, and is not taken for a thing
    called "ingredients for …".

Names of people, places, books and recipes are invented.
"""
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import dinner, direct, outing, smart_tools
from services.almanac import places

REPO = Path(__file__).resolve().parent.parent
HOME_JS = (REPO / "static" / "lite" / "home.js").read_text(encoding="utf-8")
CHAT_ROUTES = (REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")
AGENT_LOOP = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
ZONE = ZoneInfo("America/Chicago")
MONDAY = date(2031, 3, 10)


# ── what sets one off ──────────────────────────────────────────────────────

@pytest.mark.parametrize("message, key", [
    ("Plan a day hike for this Saturday near Fernhollow. When does it get dark, and what should I pack?", "going_out"),
    ("we're going camping this weekend", "going_out"),
    ("what should I pack for a fishing trip", "going_out"),
    ("help me get ready for a picnic on Sunday", "going_out"),
    ("What can I make with chicken and rice?", "dinner"),
    ("walk me through a recipe for pancakes", "dinner"),
    ("find me a simple recipe for beef stew", "dinner"),
    ("I've got eggs, flour and milk, what can I make?", "dinner"),
    ("how should I roast a chicken", "dinner"),
    ("what can I do with leftover turkey?", "dinner"),
    # not jobs: a reminder, a list, a question about the thing, and things that only sound like food
    ("remind me to plan the camping trip", None),
    ("add hiking boots to my shopping list", None),
    ("what is the history of hiking", None),
    ("plan a birthday party", None),
    ("what's for dinner", None),
    ("what can I make with leftover wood", None),
    ("what can I do with my old phone", None),
    ("walk me through making a budget", None),
    ("how do I make a paper airplane", None),
    ("hi", None),
])
def test_a_persons_own_words_set_a_smart_tool_off(message, key):
    picked = smart_tools.pick(message)
    assert (picked.key if picked else None) == key


def test_the_steps_still_to_do_are_known_without_asking_a_model():
    assert smart_tools.steps_left("going_out", {"sun_moon", "weather"}) == ("library_search", "manage_lists", "manage_reminders")
    assert smart_tools.steps_left("going_out", {"sun_moon", "weather", "library_search", "manage_lists"}, {"manage_reminders"}) == ()
    assert smart_tools.steps_left("dinner", set()) == ("library_search",)
    assert smart_tools.steps_left("nothing", set()) == () and smart_tools.steps_left(None, set()) == ()
    for tool in smart_tools.SMART_TOOLS:
        assert set(tool.required) <= set(tool.tools) and tool.rules.startswith("## ")


# ── an outing: what was asked ───────────────────────────────────────────────

def test_an_outing_request_is_read_for_its_kind_day_place_and_length():
    asked = outing.read("Plan a day hike for this Saturday near Fernhollow. When does it get dark?", MONDAY)
    assert asked["kind"] == "hike" and asked["day"] == date(2031, 3, 15) and asked["place"] == "Fernhollow" and asked["hours"] is None
    assert outing.read("we're going camping this weekend at Lake Marrow", MONDAY)["place"] == "Lake Marrow"
    assert outing.read("we're going camping this weekend", MONDAY)["day"].weekday() == 5
    long_one = outing.read("help me plan a 6 hour hike tomorrow", MONDAY)
    assert long_one["hours"] == 6.0 and long_one["day"] == date(2031, 3, 11) and long_one["place"] == ""
    assert outing.read("planning a picnic in St. Orrin on Sunday", MONDAY)["place"] == "St. Orrin"
    no_day = outing.read("what should I pack for a fishing trip", MONDAY)
    assert no_day["kind"] == "fishing trip" and no_day["day"] == MONDAY and no_day["said_day"] is False
    assert outing.read("plan a ten mile walk on Friday", MONDAY)["kind"] == "walk"
    assert outing.read("plan a 10 mile walk on Friday", MONDAY)["hours"] == 5.0          # a walking pace with stops
    assert outing.read("plan a birthday party", MONDAY) is None


def _planned(asked):
    return [{"role": "user", "content": "plan a hike"},
            {"role": "assistant", "content": "Here is the plan.", "metadata": {"tool_events": [{"tool": "plan_outing", "card": {"type": "trip", "asked": asked}}]}}]


@pytest.mark.parametrize("message, changed", [
    ("It will be about 6 hours out and back.", {"hours": 6.0}),
    ("more like 3 hours", {"hours": 3.0}),
    ("two hours", {"hours": 2.0}),
    ("make it Sunday instead", {"day": date(2031, 3, 16)}),
    ("what about tomorrow?", {"day": date(2031, 3, 11)}),
])
def test_a_short_answer_plans_the_same_outing_again_with_one_thing_changed(message, changed):
    before = {"kind": "hike", "day": "2031-03-15", "place": "Fernhollow", "hours": None}
    again = outing.again(message, _planned(before), MONDAY)
    expected = {"kind": "hike", "day": date(2031, 3, 15), "place": "Fernhollow", "hours": None, "said_day": True, **changed}
    assert again == expected


def test_only_an_answer_to_a_plan_plans_again():
    before = {"kind": "hike", "day": "2031-03-15", "place": "", "hours": None}
    for message in ("remind me on Sunday to buy milk", "what's the weather on Sunday", "thanks", "It will be about __ hours out and back.", "40 hours"):
        assert outing.again(message, _planned(before), MONDAY) is None, message
    assert outing.again("6 hours", [{"role": "assistant", "content": "hello"}], MONDAY) is None          # the answer before was not a plan
    assert outing.again("6 hours", [], MONDAY) is None
    # a plan whose day has passed is not planned again
    assert outing.again("6 hours", _planned({"kind": "hike", "day": "2031-03-01", "place": "", "hours": None}), MONDAY) is None

    class Kept:                                           # history as the session keeps it: objects, not dicts
        role, content = "assistant", "x"
        metadata = {"tool_events": [{"tool": "plan_outing", "card": {"asked": before}}]}
    assert outing.again("6 hours", [Kept()], MONDAY)["hours"] == 6.0


# ── an outing: the times and the warnings ───────────────────────────────────

def _sky(sunset=(17, 10)):
    day = datetime(2031, 11, 29, tzinfo=ZONE)
    at = lambda hour, minute: day.replace(hour=hour, minute=minute)          # noqa: E731
    return {"sunrise": at(7, 14), "sunset": at(*sunset), "civil_dusk": at(17, 38), "astronomical_dusk": at(18, 41),
            "moonrise": at(21, 45), "moon": {"name": "Waning gibbous", "lit": 0.77}}


def test_the_times_fit_the_kind_of_outing():
    hike = outing.timing("hike", _sky(), None)
    assert [(t["label"], t["value"]) for t in hike["tiles"]] == [("Leave by", "12:40 PM"), ("Turn back by", "2:40 PM"), ("Sunset", "5:10 PM"), ("Last light", "5:38 PM")]
    assert hike["line"] == "Turn back by 2:40 PM to be out before dark." and "about 4 hours" in hike["note"] and "redo" in hike["note"]
    six = outing.timing("hike", _sky(), 6.0)                              # the way back is half of it, with half an hour in hand
    assert [t["value"] for t in six["tiles"][:2]] == ["10:40 AM", "1:40 PM"] and six["note"].startswith("For 6 hours")
    camp = outing.timing("camping trip", _sky(), None)
    assert camp["tiles"][0] == {"label": "Camp set up by", "value": "3:40 PM"} and camp["note"] == "" and "camp set up by 3:40 PM" in camp["line"]
    picnic = outing.timing("picnic", _sky(), None)
    assert picnic["tiles"][0] == {"label": "Pack up by", "value": "4:40 PM"} and not any(t["label"] == "Leave by" for t in picnic["tiles"])
    stars = outing.timing("night under the stars", _sky(), None)
    assert [t["label"] for t in stars["tiles"]] == ["Sunset", "Fully dark"] and stars["line"] == "It is fully dark from 6:41 PM."
    assert "waning gibbous, 77% lit, rises 9:45 PM" in stars["note"]
    plain = outing.timing("road trip", _sky(), None)
    assert [t["label"] for t in plain["tiles"]] == ["Sunset", "Last light"] and plain["line"] == "The light is gone by 5:38 PM."
    assert outing.timing("hike", {}, None)["tiles"] == []                 # a place where the sun does not set that day


@pytest.mark.parametrize("kind, numbers, units, light, names", [
    ("hike", {"high": 63, "low": 47, "gust": 28, "rain_chance": 1, "code": 3}, "us", 9.9, ["Getting cold", "Being caught out after dark", "Ticks"]),
    ("hike", {"high": 95, "low": 74, "rain_chance": 10, "code": 1}, "us", 14.5, ["Heat", "Ticks", "Snakes"]),
    ("fishing trip", {"high": 49, "low": 32, "rain_chance": 0, "code": 3}, "us", 9.9, ["Getting cold", "The water"]),
    ("camping trip", {"high": 30, "low": 18, "snow": 1.2, "code": 73, "rain_chance": 80}, "us", 9.5, ["Frostbite and deep cold", "Fire and fumes", "Being caught out after dark"]),
    ("paddle", {"high": 66, "low": 52, "rain_chance": 70, "code": 63}, "us", 12, ["The water", "Rain"]),
    ("picnic", {"high": 78, "low": 60, "code": 95, "rain_chance": 80}, "us", 14, ["Lightning", "Ticks"]),
    ("hike", {"high": 17, "low": 8, "gust": 45, "rain_chance": 1, "code": 3}, "metric", 9.9, ["Getting cold", "Being caught out after dark", "Ticks"]),
    ("hike", None, "us", 9.9, ["Being caught out after dark", "Blisters and turned ankles"]),          # no forecast: the season and the kind still speak
    ("day out", None, "us", None, []),
])
def test_what_to_watch_for_is_chosen_by_rule_from_the_forecast(kind, numbers, units, light, names):
    found = outing.concerns(kind, numbers, units, light)
    assert [entry["name"] for entry in found] == names and len(found) <= 3
    assert all(entry["why"] and entry["query"] and outing.advice(entry["name"]) for entry in found)


def test_the_reason_for_a_warning_is_said_in_the_households_units():
    us = outing.concerns("hike", {"high": 63, "low": 47, "gust": 28, "rain_chance": 1, "code": 3}, "us", 12)[0]
    metric = outing.concerns("hike", {"high": 17, "low": 8, "gust": 45, "rain_chance": 1, "code": 3}, "metric", 12)[0]
    assert us["why"] == "a low of 47°F with gusts to 28 mph" and metric["why"] == "a low of 8°C with gusts to 45 km/h"
    wet = outing.concerns("walk", {"high": 55, "low": 44, "rain_chance": 80, "code": 63}, "us", 12)
    assert wet[0]["why"] == "a low of 44°F, and rain likely" and wet[1] == {"name": "Rain", "why": "rain is likely (80%)", "query": outing._CONCERNS["Rain"][0]}


def test_the_models_two_lists_are_read_whatever_shape_they_come_in():
    good = outing.parse_plan('{"packing": ["Water, 2 L", "Rain shell."], "watch_for": [{"about": 2, "what": "Carry a light each."}]}')
    assert good == {"packing": ["Water, 2 L", "Rain shell"], "watch_for": [{"about": 2, "what": "Carry a light each."}]}
    cut_off = outing.parse_plan('Here you go:\n```json\n{"packing": ["Water", "Map"], "watch_for": [{"about": 1, "what": "Add a layer before you stop."}, {"about": 2, "what": "Turn ba')
    assert cut_off == {"packing": ["Water", "Map"], "watch_for": [{"about": 1, "what": "Add a layer before you stop."}]}
    assert outing.parse_plan('<think>they want lists</think>{"packing": ["Torch"]}')["packing"] == ["Torch"]
    assert outing.parse_plan("I think the user wants a list of things.") == {"packing": [], "watch_for": []}
    assert len(outing.parse_plan(json.dumps({"packing": [f"thing {n}" for n in range(30)]}))["packing"]) == 14


# ── an outing: the whole job ────────────────────────────────────────────────

@pytest.fixture
def trip(monkeypatch):
    """Everything the job touches, stood in for: a place, a forecast, a library, lists and reminders."""
    from services.almanac import weather
    from src.agent_tools import TOOL_HANDLERS

    made = {"lists": [], "reminders": [], "asked": []}
    where = places.Where(36.15, -95.99, "Fernhollow, Oklahoma", "America/Chicago", "name", "")
    monkeypatch.setattr(places, "resolve", lambda text, owner=None, household=True: where)
    day = date.today() + timedelta(days=3)

    def answer(args, owner=None):
        return ("Saturday in Fernhollow\nOvercast. High 63°F, low 47°F.\nRain 1%.\nWind 10 mph, gusts 28.",
                {"type": "weather", "summary": "Overcast", "icon": "cloud", "source": "Forecast: Open-Meteo, 2:06 PM",
                 "tiles": [{"label": "Now", "value": "50°"}, {"label": "High", "value": "63°"}, {"label": "Low", "value": "47°"}]})
    monkeypatch.setattr(weather, "answer", answer)
    monkeypatch.setattr(weather, "forecast", lambda lat, lon, units: {"days": [{"date": day.isoformat(), "high": 63, "low": 47, "gust": 28, "rain_chance": 1, "code": 3}]})
    monkeypatch.setattr(outing, "_page_for", lambda name: {"title": f"The Fernhollow Field Guide (Hollow Press, 1950) — page {len(name)}",
                                                           "url": f"http://library.test/guide.pdf#page={len(name)}", "text": f"What the guide says about {name.lower()}."})

    def packing_list(owner, name, items):
        made["lists"].append((owner, name, list(items)))
        return {"type": "list", "title": name, "open": len(items), "done": 0, "items": list(items)[:8], "more": max(0, len(items) - 8), "href": "/home#lists/abc"}, False
    monkeypatch.setattr(outing, "_packing_list", packing_list)

    async def remind(content, ctx):
        made["reminders"].append((ctx.get("owner"), json.loads(content)))
        return {"output": "Reminder set.", "exit_code": 0}
    monkeypatch.setitem(TOOL_HANDLERS, "manage_reminders", remind)

    async def model(prompt):
        made["asked"].append(prompt)
        return json.dumps({"packing": ["Warm layer", "Wind shell", "Water, 2 litres", "Torch", "Map", "First-aid kit", "Food", "Hat and gloves"],
                           "watch_for": [{"about": 1, "what": "Put a layer on before you stop moving."}, {"about": 3, "what": "Check for ticks when you get home."}]})
    made["model"], made["day"], made["where"] = model, day, where
    return made


def _run(asked, owner="mia", ask_model=None, may=lambda tool: True):
    async def go():
        return [step async for step in outing.plan(asked, owner, ask_model, may)]
    return asyncio.run(go())


def test_an_outing_is_planned_by_fixed_steps_and_answered_with_a_trip_card(trip):
    asked = {"kind": "hike", "day": trip["day"], "said_day": True, "place": "Fernhollow", "hours": None}
    steps = _run(asked, ask_model=trip["model"])
    assert [step["step"] for step in steps if "step" in step] == ["sun_moon", "weather", "library_search", "thinking", "manage_lists", "manage_reminders"]
    done = steps[-1]
    card = done["card"]
    weekday = f"{trip['day']:%A}"
    assert card["type"] == "trip" and card["title"] == f"{weekday} hike" and card["place"] == "Fernhollow, Oklahoma"
    assert [tile["label"] for tile in card["times"]] == ["Leave by", "Turn back by", "Sunset", "Last light"] and card["can_change_length"] is True
    assert "redo the times" in card["assumed"] and card["note"] == ""
    assert card["asked"] == {"kind": "hike", "day": trip["day"].isoformat(), "place": "Fernhollow", "hours": None}
    # the weather as the forecast gave it, without the "now" that is not about that day
    assert card["weather"]["summary"] == "Overcast" and [tile["label"] for tile in card["weather"]["tiles"]] == ["High", "Low"]
    # each warning: chosen by rule, a library page beside it, the model's sentence or the plain advice
    names = [item["name"] for item in card["watch_for"]]
    assert names[0] == "Getting cold" and "Ticks" in names and len(names) == 3
    assert card["watch_for"][0]["text"] == "Put a layer on before you stop moving." and card["watch_for"][0]["why"] == "a low of 47°F with gusts to 28 mph"
    assert card["watch_for"][2]["text"] == "Check for ticks when you get home."
    assert card["watch_for"][1]["text"] == outing.advice(names[1])                 # the model said nothing about this one
    assert all(item["source"]["url"].startswith("http://library.test/") and item["source"]["label"].startswith("The Fernhollow Field Guide, p. ") for item in card["watch_for"])
    assert all("query" not in item for item in card["watch_for"])
    # the list is saved under the outing's name and date, the reminder is for the evening before
    owner, name, items = trip["lists"][0]
    assert owner == "mia" and name == f"{weekday} hike, {trip['day']:%b} {trip['day'].day}" and items[0] == "Warm layer" and len(items) == 8
    assert card["list"]["title"] == name and card["essentials"] is False
    reminder_owner, reminder = trip["reminders"][0]
    evening_before = trip["day"] - timedelta(days=1)
    assert reminder_owner == "mia" and reminder["text"] == "Pack for the hike and check the forecast" and reminder["when"].startswith(f"{evening_before.isoformat()}T19:00")
    assert card["reminder"] == f"{evening_before:%A}, 7:00 PM"
    # the words are short: the card carries the rest
    assert done["text"].startswith("Here is the plan for your hike near Fernhollow, Oklahoma") and "Turn back by" in done["text"] and "remind you" in done["text"]
    assert [event["tool"] for event in done["events"]] == ["sun_moon", "weather", "library_search", "manage_lists", "manage_reminders", "plan_outing"]
    assert done["events"][-1]["card"] is card
    # the model was asked once, about these pages and this forecast, and for nothing else
    assert len(trip["asked"]) == 1 and "[1] Getting cold (a low of 47°F with gusts to 28 mph)" in trip["asked"][0] and "What the guide says about getting cold." in trip["asked"][0]
    pages = next(step["sources"] for step in steps if "sources" in step)
    assert len(pages) == 3


def test_with_no_model_the_plan_still_stands(trip):
    asked = {"kind": "hike", "day": trip["day"], "said_day": True, "place": "", "hours": 6.0}
    card = _run(asked)[-1]["card"]
    assert trip["lists"][0][2] == outing.ESSENTIALS and card["essentials"] is True
    assert [item["text"] for item in card["watch_for"]] == [outing.advice(item["name"]) for item in card["watch_for"]]
    assert all(item["source"] for item in card["watch_for"]) and card["assumed"].startswith("For 6 hours")

    async def nonsense(prompt):
        return "I would be happy to help you plan."
    steps = _run(asked, ask_model=nonsense)
    assert steps[-1]["card"]["essentials"] is True                              # asked twice, read neither time: the essentials


def test_a_plan_leaves_out_what_the_person_may_not_use(trip):
    asked = {"kind": "hike", "day": trip["day"], "said_day": True, "place": "", "hours": None}
    steps = _run(asked, ask_model=trip["model"], may=lambda tool: tool not in ("manage_reminders", "weather"))
    assert [step["step"] for step in steps if "step" in step] == ["sun_moon", "library_search", "thinking", "manage_lists"]
    card = steps[-1]["card"]
    assert trip["reminders"] == [] and card["reminder"] == "" and card["weather"] is None
    assert "remind you" not in steps[-1]["text"] and "The packing list is saved." in steps[-1]["text"]


def test_a_plan_is_honest_about_what_it_had_to_assume(trip, monkeypatch):
    from services.almanac import weather
    asked = {"kind": "fishing trip", "day": date.today(), "said_day": False, "place": "", "hours": None}
    card = _run(asked, ask_model=trip["model"])[-1]["card"]
    assert card["note"] == "You did not say which day, so this is for today. Say a day and I will redo it."
    assert card["reminder"] == "" and trip["reminders"] == [] and card["can_change_length"] is False          # nothing to remind about today
    unknown = {"kind": "camping trip", "day": trip["day"], "said_day": True, "place": "", "place_unknown": "Lake Marrow", "hours": None}
    card = _run(unknown, ask_model=trip["model"])[-1]["card"]
    assert card["note"].startswith("I could not place “Lake Marrow”, so the times and the forecast are for Fernhollow, Oklahoma.")
    assert card["times"][0]["label"] == "Camp set up by"

    def offline(args, owner=None):
        raise weather.WeatherProblem("offline", "The internet is not reachable.")
    monkeypatch.setattr(weather, "answer", offline)
    card = _run({"kind": "hike", "day": trip["day"], "said_day": True, "place": "", "hours": None})[-1]["card"]
    assert card["weather"] is None and card["no_forecast"] == "No forecast: offline."
    assert [item["name"] for item in card["watch_for"]][-1] == "Blisters and turned ankles"          # no numbers: the kind of outing still speaks


def test_with_no_place_to_plan_for_the_job_does_not_start(monkeypatch):
    def nowhere(text, owner=None, household=True):
        raise places.PlaceError("No place was given and no home position is saved.")
    monkeypatch.setattr(places, "resolve", nowhere)
    steps = _run({"kind": "hike", "day": date.today(), "said_day": True, "place": "", "hours": None})
    assert steps == [{"unreadable": "No place was given and no home position is saved."}]


@pytest.fixture
def home_lists(monkeypatch, tmp_path):
    import core.database as cdb
    import services.home as package
    from services.home import boards, lists, reminders, trackers

    engine = create_engine(f"sqlite:///{tmp_path / 'home.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (lists, reminders, trackers, boards):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    package.db = factory
    return lists


def test_planning_the_same_day_again_does_not_double_the_packing_list(home_lists):
    card, kept = outing._packing_list("mia", "Saturday hike, Mar 15", ["Water", "Map", "Torch"])
    assert kept is False and card["title"] == "Saturday hike, Mar 15" and card["open"] == 3 and card["href"].startswith("/home#lists/")
    again, kept = outing._packing_list("mia", "Saturday hike, Mar 15", ["2 L of water", "A map", "Headlamp"])          # the same things in other words
    assert kept is True and again["open"] == 3 and again["items"] == ["Water", "Map", "Torch"]
    other, kept = outing._packing_list("mia", "Saturday hike, Mar 22", ["Water"])                                      # another week is another list
    assert kept is False and other["open"] == 1 and len(home_lists.all_lists("mia")) == 2
    assert outing._packing_list("dana", "Saturday hike, Mar 15", ["Boots"])[1] is False                                 # and another person's is their own


# ── dinner: what was asked ──────────────────────────────────────────────────

NOW = datetime(2031, 3, 10, 15, 30, tzinfo=ZONE)


def test_a_cooking_request_is_read_for_the_dish_or_what_is_in_the_house():
    with_things = dinner.read("What can I make with some chicken, rice and a tin of tomatoes for dinner tonight?", NOW)
    assert with_things["have"] == ["chicken", "rice", "tin of tomatoes"] and with_things["dish"] == "" and with_things["eat_at"] is None
    got = dinner.read("I've got eggs, flour and milk, what can I make? We want to eat at 6.", NOW)
    assert got["have"] == ["eggs", "flour", "milk"] and got["eat_at"] == NOW.replace(hour=18, minute=0) and got["to_buy"] is False
    dish = dinner.read("walk me through a recipe for pancakes please", NOW)
    assert dish["dish"] == "pancakes" and dish["have"] == []
    assert dinner.read("find me a simple recipe for beef stew. What do I need to buy?", NOW) == {
        "dish": "beef stew", "have": [], "eat_at": None, "to_buy": True, "said": "find me a simple recipe for beef stew. What do I need to buy?"}
    assert dinner.read("how should I roast a chicken for dinner at 7:30pm", NOW)["eat_at"] == NOW.replace(hour=19, minute=30)
    assert dinner.read("how do I bake bread for breakfast at 8", NOW.replace(hour=5))["eat_at"] == NOW.replace(hour=8, minute=0)
    assert dinner.read("recipe for chili, to eat at 2", NOW)["eat_at"] is None          # two this afternoon has passed: nothing to remind about
    assert dinner.read("what's for dinner", NOW) is None and dinner.read("plan a hike for Saturday", NOW) is None


def test_what_a_recipe_still_needs_is_worked_out_from_what_they_said_they_have():
    ingredients = [{"item": "chicken thighs"}, {"item": "long-grain rice"}, {"item": "onion"}, {"item": "salt"}, {"item": "butter"},
                   {"item": "boiling water"}, {"item": "Onion"}, {"item": "eggs"}, {"item": ""}]
    assert dinner.missing(ingredients, ["chicken", "rice", "an egg"]) == ["onion", "butter"]
    assert dinner.missing(ingredients, []) == ["chicken thighs", "long-grain rice", "onion", "butter", "eggs"]          # staples are never missing


def test_the_recipe_is_cut_from_where_it_starts_on_its_page():
    page = ("...and serve at once with the gravy poured round. PLAIN PILAU. Wash a teacup of rice. Fry an onion in butter, add the fowl "
            "jointed, the rice and a pint of stock, and simmer for forty minutes. BREAD SAUCE. Boil a pint of milk with an onion.")
    assert dinner.cut(page, "Plain Pilau").startswith("PLAIN PILAU. Wash a teacup of rice.")
    assert dinner.cut(page, "Mulligatawny") == page                                    # not found: the passage as it is
    assert len(dinner.cut("x" * 9000, "", limit=3200)) == 3200


def test_questions_and_contents_pages_are_not_offered_as_recipes():
    assert dinner.worth_showing("## Question: Why is my pilau sticky? Asked Feb 26 at 17:26. I made a pilau following a recipe I found and it came out sticky and wet.") is False
    front_page = "## Recipes for Everyone ## Newest - Desert roses - Pesto bowls - Peanut cookies - Herb pizza - Indian tea - Limoncini - Castagnole - Fritters - Corn bread"
    assert dinner.worth_showing(front_page) is False and dinner.worth_showing("too short") is False
    assert dinner.worth_showing("PLAIN PILAU. Wash a teacup of rice. Fry an onion in 2 tablespoons of butter, add the fowl, the rice and 1 pint of stock, and simmer for forty minutes.")


def test_a_book_is_named_shortly():
    assert dinner._book("The Hollow Kitchen Book : a collection of a hundred cheap and practical recipes") == "The Hollow Kitchen Book"
    assert dinner._book("Camp Fare (Hollow Press, 1910) — page 23") == "Camp Fare, page 23"
    assert dinner._book("Cookbook:Corn Pancakes") == "the Wikibooks Cookbook" and dinner._book("") == "a cookbook"


# ── dinner: the whole job ───────────────────────────────────────────────────

PILAU = ("PLAIN PILAU. Wash a teacup of rice. Fry an onion in butter, add the fowl jointed, the rice and a pint of stock, and simmer "
         "for forty minutes. BREAD SAUCE. Boil a pint of milk with an onion.")


@pytest.fixture
def supper(monkeypatch):
    from services import library
    from services.reader import kitchen
    from src.agent_tools import TOOL_HANDLERS

    made = {"stored": [], "reminders": [], "asked": [], "lists": []}
    pages = [{"title": "The Hollow Kitchen Book : a hundred cheap recipes", "url": "http://library.test/books/hollow", "text": "A book of plain cookery. PLAIN PILAU. Wash a teacup of rice."},
             {"title": "Camp Fare (Hollow Press, 1910) — page 23", "url": "http://library.test/camp.pdf#page=23", "text": "Stewed fowl with dumplings. Joint a fowl and stew it."}]
    monkeypatch.setattr(dinner, "_candidates", lambda asked: list(pages))
    monkeypatch.setattr(library, "fetch_content", lambda url, query="", budget=4000: {"content": "...poured round. " + PILAU, "success": True})

    async def set_out(text, owner=None, ask=None, only=""):
        made["set_out"] = (text, only)
        return {"title": "Plain Pilau", "year": "", "makes": "enough for four", "by_model": True, "model": "test", "notes": [],
                "ingredients": [{"item": "rice", "as_written": "a teacup of rice"}, {"item": "onion", "as_written": "an onion"}, {"item": "butter", "as_written": "butter"},
                                {"item": "chicken", "as_written": "the fowl jointed"}, {"item": "stock", "as_written": "a pint of stock"}],
                "steps": [{"do": "Wash the rice.", "as_written": "Wash a teacup of rice.", "minutes": 0},
                          {"do": "Fry the onion in the butter.", "as_written": "Fry an onion in butter", "minutes": 0},
                          {"do": "Add the chicken, rice and stock and simmer.", "as_written": "and simmer for forty minutes", "minutes": 40}]}
    monkeypatch.setattr(kitchen, "set_out", set_out)

    def store(owner, text, data, title="", source_title="", source_url="", year=""):
        made["stored"].append({"owner": owner, "text": text, "title": title, "source_title": source_title, "source_url": source_url})
        made["recipe"] = {"id": "r1", "title": title, "link": "/kitchen?r=r1", "makes": data["makes"], "by_model": True, "data": data}
        return made["recipe"]
    monkeypatch.setattr(kitchen, "store", store)
    monkeypatch.setattr(kitchen, "get", lambda owner, recipe_id: made["recipe"])
    monkeypatch.setattr(kitchen, "find", lambda owner, name: made.get("recipe") if "pilau" in name.lower() else None)

    async def remind(content, ctx):
        made["reminders"].append(json.loads(content))
        return {"output": "Reminder set.", "exit_code": 0}
    monkeypatch.setitem(TOOL_HANDLERS, "manage_reminders", remind)

    def shopping_list(owner, items):
        made["lists"].append((owner, list(items)))
        return {"type": "list", "title": "Shopping list", "open": len(items), "done": 0, "items": list(items), "more": 0, "href": "/home#lists/shop"}
    monkeypatch.setattr(dinner, "_shopping_list", shopping_list)

    async def model(prompt):
        made["asked"].append(prompt)
        return '{"choice": 1, "title": "Plain Pilau", "fits": "It is chicken and rice cooked in one pot"}'
    made["model"] = model
    return made


def _cook(asked, ask_model, may=lambda tool: True, owner="mia"):
    async def go():
        return [step async for step in dinner.plan(asked, owner, ask_model, may)]
    return asyncio.run(go())


def test_dinner_is_found_in_the_cookbooks_and_set_out_in_the_kitchen(supper):
    asked = {"dish": "", "have": ["chicken", "rice"], "eat_at": None, "to_buy": False, "said": "What can I make with chicken and rice?"}
    steps = _cook(asked, supper["model"])
    assert [step["step"] for step in steps if "step" in step] == ["library_search", "thinking", "manage_kitchen"]
    # which recipe is said before the slow part
    said = next(step["say"] for step in steps if "say" in step)
    assert said == "Plain Pilau, from The Hollow Kitchen Book. It is chicken and rice cooked in one pot. "
    assert steps.index(next(step for step in steps if "say" in step)) < len(steps) - 1
    # the recipe goes to the Kitchen in its own words, from where it starts, without its neighbour on the page
    passage, only = supper["set_out"]
    assert passage.startswith("PLAIN PILAU. Wash a teacup of rice.") and only == "Plain Pilau"
    kept = supper["stored"][0]
    assert kept["owner"] == "mia" and kept["title"] == "Plain Pilau" and kept["text"].endswith("simmer for forty minutes.") and "BREAD SAUCE" not in kept["text"]
    assert kept["source_title"].startswith("The Hollow Kitchen Book") and kept["source_url"] == "/read?u=http%3A%2F%2Flibrary.test%2Fbooks%2Fhollow"
    done = steps[-1]
    card = done["card"]
    assert card["type"] == "recipe" and card["title"] == "Plain Pilau" and card["href"] == "/kitchen?r=r1" and card["from"] == "The Hollow Kitchen Book"
    assert card["needs"] == ["onion", "butter", "stock"] and card["added"] == "" and card["start"] == ""          # they have the chicken and the rice
    assert done["text"] == said + "It is set out in the Kitchen to cook a step at a time."
    assert [event["tool"] for event in done["events"]] == ["library_search", "manage_kitchen", "plan_dinner"]
    assert supper["lists"] == [] and supper["reminders"] == []                                                   # nothing bought or set unasked
    assert len(supper["asked"]) == 1 and "[1] The Hollow Kitchen Book" in supper["asked"][0] and '"What can I make with chicken and rice?"' in supper["asked"][0]


def test_asked_what_to_buy_and_when_to_eat_it_fills_the_list_and_sets_the_start(supper):
    eat_at = datetime.now(ZONE) + timedelta(hours=5)
    asked = {"dish": "pilau", "have": [], "eat_at": eat_at, "to_buy": True, "said": "recipe for pilau. What do I need to buy? We eat late."}
    steps = _cook(asked, supper["model"])
    assert [step["step"] for step in steps if "step" in step] == ["library_search", "thinking", "manage_kitchen", "manage_lists", "manage_reminders"]
    card = steps[-1]["card"]
    assert supper["lists"] == [("mia", ["rice", "onion", "butter", "chicken", "stock"])]
    assert card["added"] == "Shopping list" and card["added_items"] == ["rice", "onion", "butter", "chicken", "stock"] and card["needs"] == []
    # forty minutes of simmering, twenty to get ready, two for each of three steps
    start = eat_at - timedelta(minutes=66)
    assert supper["reminders"][0]["text"] == "Start cooking: Plain Pilau" and supper["reminders"][0]["when"] == start.isoformat()
    assert card["start"] == f"Start at {dinner._clock(start)} to eat at {dinner._clock(eat_at)}. I will remind you." and card["start"] in steps[-1]["text"]
    assert "What it needs is on your shopping list." in steps[-1]["text"]
    # the person may not use reminders or lists: the recipe is still found
    supper["lists"].clear(); supper["reminders"].clear()
    card = _cook(asked, supper["model"], may=lambda tool: tool not in ("manage_lists", "manage_reminders"))[-1]["card"]
    assert supper["lists"] == [] and supper["reminders"] == [] and card["needs"] == ["rice", "onion", "butter", "chicken", "stock"]
    # too late to start on time: said, with no reminder
    soon = dict(asked, eat_at=datetime.now(ZONE) + timedelta(minutes=30), to_buy=False)
    card = _cook(soon, supper["model"])[-1]["card"]
    assert card["start"].endswith("it needs starting now.") and supper["reminders"] == []


def test_when_no_cookbook_has_it_that_is_said_before_anything_from_general_knowledge(supper, monkeypatch):
    asked = {"dish": "mulligatawny", "have": [], "eat_at": None, "to_buy": False, "said": "recipe for mulligatawny"}

    async def none_fit(prompt):
        return '{"choice": 0}' if "passages" in prompt else "Mulligatawny\n- 1 onion\n1. Fry the onion."
    done = _cook(asked, none_fit)[-1]
    assert done["card"] is None and supper["stored"] == []
    assert done["text"].startswith("None of the cookbooks in the library has a recipe for mulligatawny. This one is from general knowledge, not from one of your books:")
    assert done["text"].endswith("1. Fry the onion.")
    monkeypatch.setattr(dinner, "_candidates", lambda asked: [])
    done = _cook(asked, none_fit)[-1]
    assert done["text"].startswith("I found nothing in the cookbooks for mulligatawny.") and done["card"] is None
    assert _cook(asked, None)[-1]["text"] == "I found nothing in the cookbooks for mulligatawny."          # and with no model, only that


def test_a_recipe_that_could_not_be_set_out_is_not_saved(supper, monkeypatch):
    from services.reader import kitchen

    async def by_rules(text, owner=None, ask=None, only=""):
        return {"title": "", "by_model": False, "ingredients": [], "steps": [{"do": "Wash a teacup of rice."}], "notes": []}
    monkeypatch.setattr(kitchen, "set_out", by_rules)
    asked = {"dish": "", "have": ["chicken", "rice"], "eat_at": None, "to_buy": False, "said": "What can I make with chicken and rice?"}
    done = _cook(asked, supper["model"])[-1]
    assert supper["stored"] == [] and done["card"] is None
    assert "could not set it out for cooking just now" in done["text"] and "[The Hollow Kitchen Book](/read?u=" in done["text"]


def test_the_kitchen_keeps_only_the_chosen_recipes_part_of_the_page():
    from services.reader import kitchen
    data = {"steps": [{"as_written": "Wash a teacup of rice."}, {"as_written": "and simmer for forty minutes"}], "ingredients": [{"as_written": "a pint of stock"}]}
    assert kitchen.only_this(PILAU, data) == PILAU[:PILAU.index(" BREAD SAUCE")]
    assert kitchen.only_this(PILAU, {"steps": [{"as_written": "nothing like the page at all here"}]}) == PILAU          # cannot tell: all of it
    assert kitchen.only_this(PILAU, {"steps": [], "ingredients": []}) == PILAU


def test_the_ingredients_of_a_recipe_go_on_the_shopping_list_when_asked(supper):
    _cook({"dish": "", "have": ["chicken", "rice"], "eat_at": None, "to_buy": False, "said": "What can I make with chicken and rice?"}, supper["model"])
    # with the card just above: what it said was missing
    history = [{"role": "assistant", "content": "x", "metadata": {"tool_events": [{"tool": "plan_dinner", "card": {"type": "recipe", "href": "/kitchen?r=r1", "needs": ["onion", "butter"]}}]}}]
    answer = dinner.shopping("Add the ingredients for Plain Pilau to my shopping list", "mia", history)
    assert supper["lists"][-1] == ("mia", ["onion", "butter"]) and answer["text"] == "Added to your Shopping list: onion, butter."
    assert answer["card"]["type"] == "list" and answer["events"][0]["tool"] == "manage_lists"
    # later, with no card to go by: everything but the staples
    answer = dinner.shopping("add what I need for the plain pilau to our grocery list", "mia", [])
    assert supper["lists"][-1] == ("mia", ["rice", "onion", "butter", "chicken", "stock"])
    assert dinner.shopping("add the ingredients for moon cake to my shopping list", "mia", []) is None          # no such recipe in their Kitchen
    assert dinner.shopping("add milk to my shopping list", "mia", []) is None


def test_the_quick_lane_does_not_take_a_recipes_ingredients_for_a_thing(monkeypatch):
    from src.agent_tools import TOOL_HANDLERS
    calls = []

    async def lists_tool(content, ctx):
        calls.append(json.loads(content))
        return {"output": "Added.", "exit_code": 0}
    monkeypatch.setitem(TOOL_HANDLERS, "manage_lists", lists_tool)
    for message in ("add the ingredients for the stew to my shopping list", "put what I need for pancakes on my shopping list",
                    "add what's missing for the pilau to my shopping list"):
        assert asyncio.run(direct.answer(message, "mia", None)) is None, message
    assert calls == []
    assert asyncio.run(direct.answer("add milk and eggs to my shopping list", "mia", None)) is not None and calls[0]["items"] == ["milk", "eggs"]


# ── the wiring ──────────────────────────────────────────────────────────────

def test_the_jobs_run_before_any_model_round_and_only_for_tools_the_person_has():
    stream = CHAT_ROUTES[CHAT_ROUTES.rindex("async def chat_stream("):]
    lane, supper_at, trip_at, context = stream.index("_direct_lane.answer("), stream.index("_dinner_job.read("), stream.index("_outing_job.read("), stream.index("build_chat_context(")
    assert lane < supper_at < trip_at < context
    assert '"library_search" not in _lane_off' in stream and '"manage_kitchen" not in _lane_off' in stream
    assert '"sun_moon" not in _lane_off and "manage_lists" not in _lane_off' in stream
    assert "may=lambda tool: tool not in _lane_off" in stream
    # a plain turn only: not Code mode, not a document or an email open, not incognito
    assert "_plain_turn and _lane_off is not None" in stream
    # an unknown lake is planned for home, said on the card; no home at all and the assistant asks
    assert '_outing["place_unknown"], _outing["place"] = _outing["place"], ""' in stream
    # the answer is kept with its card, so the history shows it again and "6 hours" can find it
    assert stream.count("save_assistant_response(sess, session_manager, session, _text, _metrics, tool_events=_events)") >= 2
    assert "_outing_job.again(message, getattr(sess, \"history\", None)" in stream


def test_a_smart_tool_left_to_the_assistant_is_not_finished_until_its_steps_have_run():
    assert "smart_tool: Optional[str] = None" in AGENT_LOOP
    assert "_smart_tools.steps_left(smart_tool, {str(event.get(\"tool\")) for event in tool_events}, disabled_tools)" in AGENT_LOOP
    assert "_smart_nudges < 2" in AGENT_LOOP and "You stopped before the job was done." in AGENT_LOOP
    assert "smart_tool=_smart.key if _smart is not None else None" in CHAT_ROUTES


def test_a_list_the_assistant_touched_comes_back_as_a_card():
    from src.agent_tools.home_tools import list_card
    view = {"id": "abc", "title": "Saturday hike", "open": 10, "done": 1,
            "items": [{"text": f"thing {n}", "done": n == 0} for n in range(11)]}
    card = list_card(view)
    assert card == {"type": "list", "title": "Saturday hike", "open": 10, "done": 1, "items": [f"thing {n}" for n in range(1, 9)], "more": 2, "href": "/home#lists/abc"}


def test_the_family_screen_draws_the_trip_and_the_recipe():
    drawing = HOME_JS[HOME_JS.index("function drawCard("):]
    trip_card = drawing[drawing.index("if (card.type === 'trip') {"):]
    trip_card = trip_card[:trip_card.index("if (card.type === 'notice')")]
    for piece in ("card.times", "card.assumed", "card.note", "'Watch for'", "item.source.label", "'Open the packing list'", "card.reminder",
                  "card.can_change_length", "'It will be about __ hours out and back.'", "card.no_forecast", "weather.alerts"):
        assert piece in trip_card, piece
    recipe_card = drawing[drawing.index("if (card.type === 'recipe') {"):drawing.index("if (card.type === 'trip') {")]
    for piece in ("card.from", "'It also needs'", "card.added", "card.start", "'Start cooking'", "'Add what is missing to my shopping list'",
                  "Add the ingredients for ${card.title} to my shopping list", "'The page it is from'"):
        assert piece in recipe_card, piece
    assert "event.type === 'smart'" in HOME_JS and "plan_outing: 'Putting the plan together…'" in HOME_JS and "plan_dinner:" in HOME_JS
    # a page shown beside the warning it backs is not listed a second time under the answer
    assert "inCards.has(source.url)" in HOME_JS
