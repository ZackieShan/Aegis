"""An answer in the shape of the thing asked for.

What must hold:
  * a photo is sorted, not explained: a living thing is identified against the library, print is
    read (and put into the household's language when it is in another), anything else is described
    — and the person can correct the sorting; a photo that comes with a chat message gets the same
    treatment, so the species identifier is never bypassed;
  * the identification comes as a card: the name, how sure in words, the dangerous lookalike, what
    the library says about eating or touching it — and bold words inside a part are not mistaken
    for a new part;
  * the assistant can call the identifier itself, on the caller's own photos only;
  * a sentence one exact tool can answer alone is answered by it with no model round — and anything
    the lane does not read, or that asks two things, goes to the assistant as before;
  * waiting is said in plain words, sources say where they are from, a question that deserves a
    full report gets the offer, and a failure can be passed to whoever runs the server;
  * finished work finds the person;
  * the family screen has no sheet that asks what to do with a photo, a line for every tool, and a
    card for every kind of answer.

Names of people and places are invented; the write-ups are made up for the test.
"""
import asyncio
import json
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import direct, identify, notify, router, waiting
from services.almanac import dates, gazetteer
from services.identify import classifier, reference, vision
from services.search import origin
from tests.test_almanac import geonames_row
from tests.test_identify_parse_pipeline import _classified, _ref, _wire

REPO = Path(__file__).resolve().parent.parent
HOME_JS = (REPO / "static" / "lite" / "home.js").read_text(encoding="utf-8")
CHAT_ROUTES = (REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")


@pytest.fixture
def photo(tmp_path):
    from PIL import Image

    path = tmp_path / "photo.png"
    Image.new("RGB", (1200, 900), (40, 120, 60)).save(path)
    return str(path)


WRITE_UP = """**Most likely:** Death cap (*Amanita phalloides*) — confidence high.
**Why:**
- Greenish cap, white gills, a **ring** on the stem and a sac-like **volva** at the base [1]
**Lookalikes:**
- Paddy straw mushroom — edible, but it has **no ring**
- The most dangerous lookalike is the **destroying angel** (*Amanita virosa*): all white, equally deadly
**Edible or hazardous?** The **death cap** is deadly: half a cap can kill an adult [1]. Do not eat it.
**For a firmer ID:**
- Dig up the base to see the volva"""


# ── a photo is sorted, not explained ───────────────────────────────────────

@pytest.mark.parametrize("scores, living", [
    ((0.89, 0.91, 0.97), True), ((0.014, 0.938, 0.941), True), ((0.027, 0.358, 0.486), True), ((0.110, 0.161, 0.261), True),   # living things, as measured
    ((0.083, 0.085, 0.086), False), ((0.039, 0.080, 0.112), False), ((0.008, 0.018, 0.028), False), ((0.004, 0.009, 0.015), False),  # a chair, bread, a person, a sign
])
def test_the_species_classifier_says_whether_a_photo_shows_a_living_thing(scores, living):
    found = {"species": [{"score": scores[0]}], "genus": [{"score": scores[1]}], "family": [{"score": scores[2]}]}
    assert identify.likely_living(found) is living
    assert identify.likely_living(None) is False and identify.likely_living({}) is False and identify.LIVING_SCORE == 0.2


def test_the_person_can_correct_the_sorting(monkeypatch, photo):
    calls = _wire(monkeypatch, _classified(top_score=0.8), ["LOT 22B\nSummary: a label."])
    read = identify._identify_sync(photo, "", None, want="text")
    assert read["category"] == "text" and len(calls) == 1 and calls[0]["prompt"].startswith("Transcribe all legible text")
    assert read["classifier"] is None                                             # told it is print: the species classifier is not asked

    calls = _wire(monkeypatch, _classified(top_score=0.8), ["**What it shows:** a green field"])
    plain = identify._identify_sync(photo, "by the creek", None, want="describe")
    assert plain["living"] is False and calls[0]["prompt"].startswith("Describe what the attached photo shows") and "by the creek" in calls[0]["prompt"]

    look = '{"category": "object", "subject": "a brown lump"}'
    calls = _wire(monkeypatch, _classified(top_score=0.03, genus_score=0.04), [look, "**Most likely:** A puffball"])
    forced = identify._identify_sync(photo, "", None, want="living")
    assert forced["living"] is True and calls[1]["prompt"].startswith("You are identifying the subject")
    assert identify._identify_sync(photo, "", None, want="x-ray")["want"] == "" if _wire(monkeypatch, _classified(), ["**Most likely:** Death cap"]) is not None else True


def test_a_small_model_that_gives_only_the_closing_line_is_asked_again_for_the_words(monkeypatch, photo):
    calls = _wire(monkeypatch, None, ["Summary: a tablet label.", "WATER TABLETS\nDo not swallow."])
    read = identify._identify_sync(photo, "", None, want="text")
    assert len(calls) == 2 and "Read out every word" in calls[1]["prompt"]
    assert read["body"] == "WATER TABLETS\nDo not swallow.\nSummary: a tablet label."
    assert identify.card_for(read)["text"] == "WATER TABLETS\nDo not swallow." and identify.card_for(read)["summary"] == "a tablet label."


def test_the_identification_comes_as_a_card():
    card = identify.card_for({"body": WRITE_UP, "category": "mushroom", "living": True,
                              "best_guess": {"common_name": "Death cap", "scientific_name": "Amanita phalloides", "confidence": "medium"},
                              "sources": [{"title": "Amanita phalloides", "url": "https://lib/content/w/Amanita_phalloides"}],
                              "images": [{"title": "Amanita phalloides", "url": "/api/reference/asset?p=x"}]})
    assert (card["type"], card["kind"], card["name"], card["latin"]) == ("identification", "mushroom", "Death cap", "Amanita phalloides")
    assert card["sure"] == {"level": "high", "words": "Quite sure"}               # what the write-up says, over the first impression
    assert card["hazard"] == "The death cap is deadly: half a cap can kill an adult. Do not eat it."      # bold words inside a part are not a new part
    assert card["lookalike"].startswith("The most dangerous lookalike is the destroying angel")          # the dangerous one, not the first one
    assert card["library"] == {"title": "Amanita phalloides", "url": "https://lib/content/w/Amanita_phalloides"} and card["photo"]["url"]
    assert [part["label"] for part in card["more"]] == ["Why", "Lookalikes", "Edible or hazardous?", "For a firmer ID"]
    unsure = identify.card_for({"body": "**Most likely:** some kind of webcap — confidence low.", "category": "mushroom", "living": True,
                                "best_guess": {"common_name": "", "scientific_name": "Cortinarius", "confidence": "high"}, "sources": [], "images": []})
    assert unsure["sure"]["level"] == "low" and unsure["sure"]["words"].startswith("Not sure") and unsure["name"] == "some kind of webcap" and unsure["library"] is None


def test_print_and_other_photos_have_their_own_cards():
    recipe = "Jumbles\n2 cups flour\n1 cup sugar\n3 eggs\nMix and bake in a quick oven until golden.\nSummary: a recipe card."
    reading = identify.card_for({"body": recipe, "category": "text", "living": False})
    assert reading["type"] == "reading" and reading["recipe"] is True and reading["text"].endswith("until golden.") and reading["summary"] == "a recipe card."
    assert identify.looks_like_recipe("WARNING\nKeep out of reach of children.\n2 tablets every 4 hours") is False
    sign = "**What it is:** A warning sign.\n**Details:**\n- Red border\n**Text:**\nPELIGRO\n\"Agua no potable.\""
    described = {"body": sign, "markdown": sign, "category": "object", "living": False, "subject": "warning sign"}
    assert identify.words_in(described) == "PELIGRO\nAgua no potable."
    card = identify.card_for(described)
    assert card["type"] == "description" and card["words"] == "PELIGRO\nAgua no potable." and "PELIGRO" not in card["text"] and "Red border" in card["text"]
    assert identify.words_in({"body": "**What it is:** a wrench\n**Text:** None", "category": "object", "living": False}) == ""
    assert identify.words_in({"body": WRITE_UP, "category": "mushroom", "living": True}) == ""


def test_words_in_another_language_are_put_into_the_households(monkeypatch, photo):
    from services import household, translate
    from services.translate import detect

    _wire(monkeypatch, None, ['{"category": "text", "subject": "a sign"}', "PELIGRO\nAgua no potable.\nSummary: a warning sign."])
    monkeypatch.setattr(household, "language", lambda: "en")
    monkeypatch.setattr(detect, "detect", lambda text: ("es", 0.9))
    asked = {}

    async def fake_translate(text, target, source="auto", owner=None, on_part=None):
        asked.update(text=text, target=target, source=source)
        return {"text": "DANGER\nWater not drinkable.", "source": "es", "target": "en", "same_language": False}

    monkeypatch.setattr(translate, "translate_text", fake_translate)
    found = asyncio.run(identify.identify_image(photo, owner="mia"))
    assert asked == {"text": "PELIGRO\nAgua no potable.", "target": "en", "source": "es"}
    assert found["card"]["translation"] == {"from": "Spanish", "from_code": "es", "to": "English", "to_code": "en", "text": "DANGER\nWater not drinkable."}
    assert "**In English** (it is written in Spanish)" in found["markdown"]
    # print already in the household's language is left alone, and so is anything the language of which cannot be told
    monkeypatch.setattr(detect, "detect", lambda text: ("en", 0.9))
    _wire(monkeypatch, None, ['{"category": "text"}', "KEEP DRY\nSummary: a label."])
    assert "translation" not in asyncio.run(identify.identify_image(photo, owner="mia"))["card"]


def test_a_photo_in_a_chat_goes_through_the_identifier_when_it_shows_a_living_thing(monkeypatch, photo):
    _wire(monkeypatch, _classified(top_score=0.8), [WRITE_UP])
    told = identify.for_chat(photo, "is this safe to eat?", "mia")
    assert told.startswith("[This photo was put through the species identifier") and "do not name the species yourself" in told and "Death cap" in told
    not_alive = {"species": [{"species": "Xylaria bambusicola", "score": 0.018}], "genus": [{"genus": "Xylaria", "score": 0.024}], "family": [{"score": 0.026}]}
    calls = _wire(monkeypatch, not_alive, ["should not be asked"])
    assert identify.for_chat(photo, "what is this part?", "mia") == "" and calls == []          # an object: described as it always was
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: None)
    assert identify.for_chat(photo, "", "mia") == ""                                            # no classifier installed: nothing changes
    handler = (REPO / "src" / "chat_handler.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(_identify.for_chat, file_info[\"path\"], message or \"\", owner)" in handler
    assert handler.index("_identify.for_chat") < handler.index("vl_result = analyze_image_with_vl_result(")     # sorted first, described only if not living


# ── the identifier as a tool ───────────────────────────────────────────────

@pytest.fixture
def uploads(tmp_path, monkeypatch, photo):
    from src.agent_tools import identify_tools
    store = tmp_path / "uploads"
    store.mkdir()
    now = datetime.now()
    index = {
        "mia:a": {"id": "a" * 32 + ".jpg", "path": photo, "mime": "image/jpeg", "owner": "mia", "name": "old.jpg", "uploaded_at": (now - timedelta(minutes=50)).isoformat()},
        "mia:b": {"id": "b" * 32 + ".jpg", "path": photo, "mime": "image/jpeg", "owner": "mia", "name": "new.jpg", "uploaded_at": (now - timedelta(minutes=2)).isoformat()},
        "mia:c": {"id": "c" * 32 + ".pdf", "path": photo, "mime": "application/pdf", "owner": "mia", "name": "notes.pdf", "uploaded_at": now.isoformat()},
        "zed:d": {"id": "d" * 32 + ".jpg", "path": photo, "mime": "image/jpeg", "owner": "zed", "name": "his.jpg", "uploaded_at": now.isoformat()},
    }
    (store / "uploads.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr("src.constants.UPLOAD_DIR", str(store))
    return identify_tools


def test_the_assistant_can_only_look_at_the_callers_own_photos(uploads):
    assert uploads.owned_photo("", "mia")["name"] == "new.jpg"                    # the photo sent most recently
    assert uploads.owned_photo("a" * 32 + ".jpg", "mia")["name"] == "old.jpg" and uploads.owned_photo("old.jpg", "mia")["name"] == "old.jpg"
    assert uploads.owned_photo("d" * 32 + ".jpg", "mia") is None                  # someone else's
    assert uploads.owned_photo("c" * 32 + ".pdf", "mia") is None                  # not a picture
    assert uploads.owned_photo("", "ada") is None and uploads.owned_photo("", None) is None
    assert uploads.owned_photo("", "zed")["name"] == "his.jpg"


def test_identify_photo_gives_the_answer_as_it_stands_with_its_card(uploads, monkeypatch):
    seen = {}

    async def fake_identify(path, hint="", owner=None, want=""):
        seen.update(hint=hint, owner=owner, want=want)
        return {"markdown": WRITE_UP, "living": True, "sources": [{"title": "Amanita phalloides", "url": "https://lib/a"}], "card": {"type": "identification", "name": "Death cap"}}

    monkeypatch.setattr(identify, "identify_image", fake_identify)
    from src.agent_tools import TOOL_HANDLERS
    out = asyncio.run(TOOL_HANDLERS["identify_photo"](json.dumps({"note": "under oaks", "as": "species"}), {"owner": "mia"}))
    assert out["exit_code"] == 0 and seen == {"hint": "under oaks", "owner": "mia", "want": "living"}
    assert out["output"].startswith("Identification from the species identifier and the home library.") and "Sources: [Amanita phalloides](https://lib/a)" in out["output"]
    assert out["card"] == {"type": "identification", "name": "Death cap"}
    none = asyncio.run(TOOL_HANDLERS["identify_photo"]("{}", {"owner": "ada"}))
    assert none["exit_code"] == 1 and "no photo of theirs" in none["error"]


def test_identify_photo_is_on_every_list_a_tool_must_be_on():
    from src.agent_loop import TOOL_SECTIONS, _DOMAIN_RULES, _DOMAIN_TOOL_MAP
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS

    for listing in (TOOL_HANDLERS, TOOL_TAGS, TOOL_SECTIONS, BUILTIN_TOOL_DESCRIPTIONS, PLAN_MODE_READONLY_TOOLS):
        assert "identify_photo" in listing
    assert "identify_photo" not in NON_ADMIN_BLOCKED_TOOLS and _DOMAIN_TOOL_MAP["photo"] == {"identify_photo"}
    assert "Do not name a living thing from a photo yourself" in _DOMAIN_RULES["photo"]
    schema = next(entry["function"] for entry in FUNCTION_TOOL_SCHEMAS if entry["function"]["name"] == "identify_photo")
    assert set(schema["parameters"]["properties"]) == {"photo", "note", "as"}


# ── straight to the tool ───────────────────────────────────────────────────

@pytest.fixture
def at_home(tmp_path, monkeypatch):
    """A household with a home town (an invented one), a clock, and no network."""
    from services import household
    from services.almanac import weather
    from src import user_time

    monkeypatch.delenv("AEGIS_HOUSEHOLD_DISABLED", raising=False)
    monkeypatch.setattr(gazetteer, "path", lambda: tmp_path / "almanac" / "places.db")
    monkeypatch.setattr("routes.prefs_routes.PREFS_FILE", str(tmp_path / "user_prefs.json"))
    source = tmp_path / "geonames"
    source.mkdir()
    (source / "cities1000.txt").write_text(geonames_row(1, "Elmford", 35.10, -97.10, "US", "OK", 410000, "America/Chicago", "350") + "\n", encoding="utf-8")
    (source / "admin1CodesASCII.txt").write_text("US.OK\tOklahoma\tOklahoma\t4\n", encoding="utf-8")
    (source / "countryInfo.txt").write_text("US\tUSA\t840\tUS\tUnited States\n", encoding="utf-8")
    gazetteer.build(source)
    kept = {"home": {"lat": 35.10, "lon": -97.10, "label": "Elmford, Oklahoma", "zone": "America/Chicago"}, "units": "us"}
    monkeypatch.setattr(household, "_stored", lambda: dict(kept))
    monkeypatch.setattr(direct, "_today", lambda: date(2026, 11, 23))

    def forecast(args, owner=None):
        if args.get("place") == "Xyzzyville":
            raise weather.WeatherProblem("place", "I do not know a place called “Xyzzyville”.")
        if "week" in str(args.get("when") or "") and "weekend" not in str(args.get("when")):
            return "the week", {"type": "weather", "title": "The week ahead", "place": "Elmford, Oklahoma",
                                "days": [{"label": "Today", "summary": "Overcast", "high": "49°", "low": "32°", "rain": "0%"},
                                         {"label": "Tue", "summary": "Light snow", "high": "33°", "low": "20°", "rain": "70%"}]}
        chance = "80%" if args.get("when") == "tomorrow" else "5%"
        title = "Tomorrow" if args.get("when") == "tomorrow" else "Today"
        return "the day", {"type": "weather", "title": title, "place": "Elmford, Oklahoma", "summary": "Light rain" if chance == "80%" else "Overcast",
                           "tiles": [{"label": "Now", "value": "48°"}, {"label": "High", "value": "49°"}, {"label": "Low", "value": "32°"},
                                     {"label": "Rain", "value": chance}, {"label": "Wind", "value": "12 mph"}]}

    monkeypatch.setattr(weather, "answer", forecast)
    user_time.set_user_tz_offset("-360")
    user_time.set_user_tz_name("America/Chicago")
    yield
    user_time.clear_user_time_context()


def ask(message, owner="mia", allowed=None):
    return asyncio.run(direct.answer(message, owner, allowed))


@pytest.mark.parametrize("message, tool, said", [
    ("When does it get dark tonight?", "sun_moon", r"^It is fully dark by \d+:\d\d PM tonight in Elmford, Oklahoma\. The sun sets at \d+:\d\d PM"),
    ("hey, can you tell me when it gets dark", "sun_moon", r"^It is fully dark by"),
    ("what time is sunset", "sun_moon", r"^The sun sets at \d+:\d\d PM today in Elmford, Oklahoma\."),
    ("When does the sun come up tomorrow?", "sun_moon", r"^The sun rises at \d+:\d\d AM tomorrow in Elmford"),
    ("how much daylight is there today", "sun_moon", r"^\d+ h \d\d min of daylight today in Elmford"),
    ("when is the next full moon?", "sun_moon", r"^The next full moon is "),
    ("How many cups is 2 litres?", "convert_units", r"^2 L is 8\.454 cups\.$"),
    ("how many feet in a mile", "convert_units", r"^1 mi is 5,280 ft\.$"),
    ("convert 5 ft 10 in to cm", "convert_units", r"^5 ft 10 in is 177\.8 cm\.$"),
    ("what is 350 F in C", "convert_units", r"^350 °F is 176\.7 °C\.$"),
    ("How many days until Christmas?", "date_math", r"^32 days until Friday, December 25, 2026 \(4 weeks and 4 days\)\.$"),
    ("how many weekdays until december 25", "date_math", r"^24 weekdays until Friday, December 25, 2026 \(32 days in all\)\.$"),
    ("how long until Thanksgiving", "date_math", r"^3 days until Thursday, November 26, 2026\.$"),
    ("how many days since Halloween", "date_math", r"^It has been 23 days since Saturday, October 31, 2026"),
    ("what day of the week is July 4 2027", "date_math", r"^July 4, 2027 is a Sunday\.$"),
    ("what day is it", "date_math", r"^Today is Monday, November 23, 2026\.$"),
    ("what's the date 90 days from now", "date_math", r"^90 days from today is Sunday, February 21, 2027\.$"),
    ("when is Labor Day", "date_math", r"^Labor Day is Monday, September 6, 2027: 287 days away\.$"),
    ("What's the weather like today?", "weather", r"^It is 48° now\. Today in Elmford, Oklahoma: overcast, high 49°, low 32°, no rain expected, wind up to 12 mph\.$"),
    ("will it rain tomorrow", "weather", r"^Yes, rain is likely tomorrow in Elmford, Oklahoma: 80% chance\. Light rain, high 49°, low 32°\.$"),
    ("do I need an umbrella today", "weather", r"^No need: no rain is expected today\."),
    ("is it going to snow this week", "weather", r"^Yes, snow is in the forecast\. The week ahead in Elmford"),
])
def test_one_exact_tool_answers_a_sentence_it_can_answer_alone(at_home, message, tool, said):
    found = ask(message)
    assert found is not None and found.tool == tool, message
    assert re.search(said, found.text), found.text
    assert found.card and found.card["type"] in ("sun", "result", "weather") and found.output


@pytest.mark.parametrize("message", [
    "When does it get dark tonight, and what should I pack?", "Plan a day hike for Saturday", "what is the capital of France",
    "how many people live in Elmford", "how many dollars is 5 euros", "what is love", "when is the meeting", "how do I purify water",
    "what is 5 plus 7", "convert this to a pdf", "thanks!", "how long until dinner is ready", "tell me a story about the weather",
    "how many days until I should water the plants", "what's the weather in Xyzzyville", "when does it get dark in Xyzzyville",
    "what's the weather today? and tomorrow?", "check https://example.com/weather", "", "x" * 300,
])
def test_anything_else_goes_to_the_assistant_as_before(at_home, message):
    assert ask(message) is None, message


def test_a_tool_that_is_not_this_persons_to_use_does_not_answer(at_home):
    assert ask("How many cups is 2 litres?", allowed=lambda tool: tool != "convert_units") is None
    assert ask("When does it get dark tonight?", allowed=lambda tool: False) is None
    assert ask("When does it get dark tonight?", allowed=lambda tool: tool == "sun_moon").tool == "sun_moon"


def test_with_no_forecast_the_answer_says_so_and_nothing_is_guessed(at_home, monkeypatch):
    from services.almanac import weather

    def none(args, owner=None):
        raise weather.WeatherProblem("offline", "No forecast: the internet is not reachable right now.")

    monkeypatch.setattr(weather, "answer", none)
    found = ask("will it rain tomorrow")
    assert found.text == "No forecast: the internet is not reachable right now." and found.card == {"type": "notice", "text": found.text, "reason": "offline"}


def test_a_short_translation_goes_straight_to_the_translator(at_home, monkeypatch):
    from services import translate
    asked = []

    async def fake(text, target, source="auto", owner=None, on_part=None):
        asked.append((text, target))
        return {"text": "¿Dónde está la farmacia?", "source": "en", "source_name": "English", "target": "es", "target_name": "Spanish", "same_language": False}

    monkeypatch.setattr(translate, "translate_text", fake)
    found = ask("How do you say “where is the pharmacy” in Spanish?")
    assert asked == [("where is the pharmacy", "es")] and found.tool == "translate" and found.text == "¿Dónde está la farmacia?"
    assert found.card == {"type": "translation", "from": "English", "from_code": "en", "to": "Spanish", "to_code": "es",
                          "source": "where is the pharmacy", "text": "¿Dónde está la farmacia?"}
    for other in ("translate that to Spanish", "what is 5 km in miles", "how do you say it in Klingonese", "what is the weather in French Lick"):
        asked.clear()
        ask(other)
        assert asked == [], other                                # points back, is a unit, or is not a language: not the translator's


@pytest.fixture
def lists_db(monkeypatch, tmp_path):
    import core.database as cdb
    import services.home as package
    from services.home import boards, lists, reminders, trackers

    engine = create_engine(f"sqlite:///{tmp_path / 'home.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (lists, reminders, trackers, boards):
        monkeypatch.setattr(module, "SessionLocal", factory)
    return lists


def test_something_for_a_list_goes_straight_onto_it(at_home, lists_db):
    assert direct.list_items("milk, eggs and bread") == ["milk", "eggs", "bread"] and direct.list_items("milk and eggs") == ["milk", "eggs"]
    assert direct.list_items("mac and cheese") == ["mac and cheese"] and direct.list_items("some salt and pepper, a lemon") == ["salt and pepper", "lemon"]
    added = ask("Add milk and eggs to my shopping list")
    assert added.tool == "manage_lists" and added.text == "Added milk and eggs to Shopping list."
    assert added.card["type"] == "list" and added.card["items"] == ["milk", "eggs"] and added.card["href"].startswith("/home#lists/")
    assert "Link: [Shopping list](/home#lists/" in added.output                   # the screen draws its Open button from this
    shown = ask("what's on my shopping list?")
    assert shown.text == "Shopping list has 2 to do: milk, eggs." and ask("add bread to the shopping list").card["open"] == 3
    assert ask("what's on my packing list") is None                               # no such list: the assistant sorts that out
    assert ask("add a reminder to my calendar") is None and ask("Add milk to my shopping list", allowed=lambda tool: tool != "manage_lists") is None
    assert lists_db.all_lists("zed") == []                                        # hers, nobody else's


def test_named_days_are_on_the_calendar():
    today = date(2026, 11, 23)
    for said, day in (("Christmas", date(2026, 12, 25)), ("christmas eve", date(2026, 12, 24)), ("Thanksgiving", date(2026, 11, 26)),
                      ("thanksgiving 2027", date(2027, 11, 25)), ("last Easter", date(2026, 4, 5)), ("Easter", date(2027, 3, 28)),
                      ("New Year's Day", date(2027, 1, 1)), ("the Fourth of July", date(2027, 7, 4)), ("Labor Day", date(2027, 9, 6)),
                      ("Memorial Day 2027", date(2027, 5, 31)), ("Mother's Day", date(2027, 5, 9)), ("last Halloween", date(2026, 10, 31)),
                      ("tonight", today), ("the day after tomorrow", date(2026, 11, 25)), ("in 3 days", date(2026, 11, 26)), ("2 weeks from now", date(2026, 12, 7))):
        assert dates.parse(said, today) == day, said
    assert [dates.easter(year) for year in (2024, 2025, 2026, 2038)] == [date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2038, 4, 25)]
    assert dates.named_day("the meeting", today) is None
    with pytest.raises(dates.DateError):
        dates.parse("the meeting", today)


def test_the_lane_is_tried_before_anything_else_is_built():
    lane, context = CHAT_ROUTES.index("_direct_lane.answer(message, owner"), CHAT_ROUTES.rindex("ctx = await build_chat_context(")
    assert lane < context
    block = CHAT_ROUTES[CHAT_ROUTES.index("_plain_turn = ("):lane]
    for condition in ('_mode_as_sent == "agent"', "not code_mode", "not compare_mode", "not incognito", "not do_research", "not active_doc_id",
                      "not active_email_uid", "not _active_toolboxes", "can_use_agent", "block_all_tool_calls", "blocked_tools_for_owner"):
        assert condition in block or condition in CHAT_ROUTES[lane - 1500:lane], condition
    assert 'save_assistant_response(sess, session_manager, session, _direct.text, _direct_metrics, tool_events=[_direct_event])' in CHAT_ROUTES
    assert 'sess.add_message(ChatMessage("user", message))' in CHAT_ROUTES             # the turn is kept, so a follow-up has it


# ── honest waiting, sources, and the offer ─────────────────────────────────

def test_waiting_is_said_in_plain_words():
    assert [waiting.how_long(s) for s in (5, 18, 30, 60, 200)] == ["a few seconds", "about 20 seconds", "about half a minute", "about a minute", "about 3 minutes"]
    assert waiting.served_here("http://127.0.0.1:9090/v1/chat/completions") and waiting.served_here("http://localhost:9090/v1")
    assert not waiting.served_here("https://api.example.com/v1") and not waiting.served_here("http://127.0.0.1:11434/v1") and not waiting.served_here("")
    assert asyncio.run(waiting.waking("https://api.example.com/v1", "some-model")) is None      # not ours to ask
    waiting.note_woke("made-up-model", 21.0)
    assert waiting._took["made-up-model"] == 21.0
    waiting.note_woke("made-up-model", 0.2)                                                      # it was awake after all: not a load time
    assert waiting._took.pop("made-up-model") == 21.0
    assert waiting.OFFLINE["text"] == "The internet is down. This is from the home library." and "smaller helper" in waiting.busy()["text"]
    for wired in ("_waiting.waking(sess.endpoint_url, _turn_model)", "_waiting.busy()", "_waiting.OFFLINE", "_waiting.note_woke(_turn_model"):
        assert wired in CHAT_ROUTES, wired


def test_a_sleeping_model_is_said_to_be_waking(monkeypatch):
    import httpx

    class Answer:
        status_code = 200

        def __init__(self, running):
            self._running = running

        def json(self):
            return {"running": self._running}

    class Client:
        running = []

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url.endswith("/running")
            return Answer(Client.running)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    url = "http://127.0.0.1:9090/v1/chat/completions"
    asleep = asyncio.run(waiting.waking(url, "big-model"))
    assert asleep == {"type": "notice", "code": "waking", "seconds": 30, "text": "Waking up, about half a minute"}
    Client.running = [{"model": "big-model", "state": "starting"}]
    assert asyncio.run(waiting.waking(url, "big-model"))["code"] == "waking"
    Client.running = [{"model": "big-model", "state": "ready"}]
    assert asyncio.run(waiting.waking(url, "big-model")) is None


def test_a_source_says_where_it_is_from(monkeypatch):
    from services.library import kiwix
    monkeypatch.setattr(kiwix, "owns_url", lambda url: str(url).startswith("http://box:8090/content/"))
    assert origin.kind_of("http://box:8090/content/wikipedia/Bleach") == "library" and origin.kind_of("/api/reference/added/x.pdf") == "library"
    assert origin.kind_of("https://example.com/a") == "web" and origin.kind_of("") == "web"
    mixed = origin.marked([{"url": "https://example.com/a", "title": "A"}, {"url": "http://box:8090/content/w/B", "title": "B"}, "junk"])
    assert [source["kind"] for source in mixed] == ["web", "library"]
    assert [source["kind"] for source in origin.marked([{"url": "https://example.com/a"}], "library")] == ["library"]
    loop = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert '_marked(_extracted_sources, _LIB if block.tool_type == "library_search" else "")' in loop
    assert "'Home library'" in HOME_JS and "'The web, today'" in HOME_JS and "'Worked out'" in HOME_JS


@pytest.mark.parametrize("message", [
    "Which is better for heating a small cabin, a wood stove or a propane heater?", "compare heat pumps and gas furnaces for a cold climate",
    "should we buy a generator or a battery backup for the house", "what should I know before buying a used tractor",
    "what are the pros and cons of raising chickens in town", "how should we plan a vegetable garden for a family of four",
])
def test_a_question_that_deserves_a_full_report_gets_the_offer(message, monkeypatch):
    monkeypatch.setattr(router, "average_report_minutes", lambda: 11.6)
    offer = router.report_offer(message)
    assert offer == {"type": "offer", "kind": "report", "label": "Dig deeper: a full report", "estimate": "about 12 minutes", "topic": message}


@pytest.mark.parametrize("message", [
    "hi", "thanks!", "add milk to my shopping list", "when does it get dark tonight", "what is the capital of France",
    "remind me to compare prices tomorrow at 9", "do deep research on heat pumps versus furnaces", "which one",
])
def test_an_ordinary_question_gets_no_offer(message):
    assert router.report_offer(message) is None


def test_the_offer_never_starts_a_job_by_itself():
    assert "_report_offer(message if isinstance(message, str) else \"\")" in CHAT_ROUTES and '"trigger_research" not in _tools_used' in CHAT_ROUTES
    assert "api('/api/research/start', { method: 'POST', body: { query: message.offer.topic } })" in HOME_JS      # only when the person taps
    assert router.average_report_minutes() >= 0


# ── finished work, and trouble, find the right person ──────────────────────

class Queue:
    def __init__(self):
        self.seen = []

    def add_notification(self, task_name, status, task_id=None, owner=None, body=None, url=None, push=True):
        assert push is False                              # notify pushes by itself: the queue must not push it again
        self.seen.append({"title": task_name, "status": status, "tag": task_id, "owner": owner, "body": body, "url": url})


@pytest.fixture
def told(monkeypatch):
    from routes import note_routes
    from services import push
    queue, pushed = Queue(), []

    async def fake_send(owner, title, body="", url="/home", tag=None):
        pushed.append((owner, title, body, url, tag))
        return 1

    monkeypatch.setattr(note_routes, "_scheduler_ref", queue, raising=False)
    monkeypatch.setattr(push, "send", fake_send)
    monkeypatch.setattr(notify, "admins", lambda: ["boss", "deputy"])
    return queue, pushed


def test_a_notification_reaches_the_phone_and_the_open_screen(told):
    queue, pushed = told
    assert asyncio.run(notify.tell("mia", "Your report is ready", "wood stove or propane", url="/api/research/report/rp-1", tag="report-rp-1")) == 1
    assert pushed == [("mia", "Your report is ready", "wood stove or propane", "/api/research/report/rp-1", "report-rp-1")]
    assert queue.seen == [{"title": "Your report is ready", "status": "success", "tag": "report-rp-1", "owner": "mia", "body": "wood stove or propane",
                           "url": "/api/research/report/rp-1"}]
    assert asyncio.run(notify.tell_admins("It did not work for Mia", "cause", url="/admin#activity/mia", but="boss")) == 1
    assert [entry[0] for entry in pushed[1:]] == ["deputy"] and queue.seen[-1]["status"] == "warning"
    handler = (REPO / "src" / "research_handler.py").read_text(encoding="utf-8")
    assert 'notify.tell_soon(entry.get("owner") or None, "Your report is ready", asked,' in handler and 'elif entry.get("status") != "cancelled":' in handler


def test_the_cause_of_a_failure_is_kept_for_whoever_runs_the_server():
    notify.note_failure("chat-1", "RuntimeError: the model server closed the connection")
    assert notify.last_failure("chat-1") == "RuntimeError: the model server closed the connection" and notify.last_failure("chat-2") == ""
    notify._failures["chat-1"]["at"] = time.time() - 4000
    assert notify.last_failure("chat-1") == ""                                   # an old failure is not this one's cause
    for wired in ('_note_failure(session, f"{sess.model}: {chunk[:500]}")', '_note_failure(session, f"{type(_loop_exc).__name__}: {_loop_exc}")'):
        assert wired in CHAT_ROUTES


def _family_api(monkeypatch, user):
    import routes.home_routes as routes

    class Accounts:
        users = {"mia": {}, "boss": {}}

        def get_experience(self, name):
            return {"display_name": {"mia": "Mia", "boss": "Sam"}[name], "mode": "simple", "sections": []}

        def is_admin(self, name):
            return name == "boss"

    monkeypatch.setattr(routes, "require_user", lambda request: user)
    made = routes.setup_home_routes(Accounts())
    request = type("Request", (), {"headers": {}})()

    def call(method, path, **kwargs):
        endpoint = next(r.endpoint for r in made.routes if r.path == f"/api/home{path}" and method in r.methods)
        result = endpoint(request=request, **kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    return call


def test_tell_them_passes_on_who_asked_what_and_why_it_failed(told, monkeypatch):
    queue, pushed = told
    monkeypatch.setattr(notify, "admins", lambda: ["boss"])
    notify.note_failure("chat-9", "gpt-test: event: error 503 model server restarting")
    call = _family_api(monkeypatch, "mia")
    answer = call("POST", "/problem", body={"asked": "When does  it get dark tonight?", "error": "That did not work.", "session": "chat-9"})
    assert answer == {"ok": True, "phones": 1, "anyone": True}
    owner, title, body, url, tag = pushed[-1]
    assert (owner, title, url, tag) == ("boss", "It did not work for Mia", "/admin#activity/mia", "problem-chat-9")
    assert body == "They asked: “When does it get dark tonight?”. Cause: gpt-test: event: error 503 model server restarting"      # the server's cause, not the plain sentence she saw


def test_the_family_screen_start_says_who_to_tell_and_what_is_set(monkeypatch):
    import core.database as cdb
    from services import household
    from services.home import boards, lists, reminders, trackers

    monkeypatch.setattr(household, "get", lambda: {"home": {"label": "Elmford, Oklahoma"}, "units": "us", "language": "en", "live": {}, "chosen": ["home"]})
    for module in (lists, trackers, boards):
        monkeypatch.setattr(module, f"all_{module.__name__.rsplit('.', 1)[-1]}", lambda owner, **kw: [])
    monkeypatch.setattr(reminders, "all_reminders", lambda owner, **kw: [])
    start = _family_api(monkeypatch, "mia")("GET", "/start")
    assert start["house"] == {"home": "Elmford, Oklahoma", "units": "us", "language": "en", "keeper": "Sam"}
    assert start["me"]["full_app"] is False and start["me"]["is_admin"] is False


def test_finished_reports_are_listed_for_their_owner_only(tmp_path, monkeypatch):
    store = tmp_path / "deep_research"
    store.mkdir()
    for name, data in (("rp-1", {"owner": "mia", "status": "done", "query": "wood stove  or propane", "completed_at": 200}),
                       ("rp-2", {"owner": "mia", "status": "done", "query": "older one", "completed_at": 100}),
                       ("rp-3", {"owner": "mia", "status": "error", "query": "broke"}), ("rp-4", {"owner": "zed", "status": "done", "query": "his"}),
                       ("rp-5", {"owner": "mia", "status": "done", "query": "put away", "archived": True})):
        (store / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr("src.constants.DEEP_RESEARCH_DIR", str(store))
    found = _family_api(monkeypatch, "mia")("GET", "/reports")["reports"]
    assert [(r["id"], r["title"], r["href"]) for r in found] == [("rp-1", "wood stove or propane", "/api/research/report/rp-1"), ("rp-2", "older one", "/api/research/report/rp-2")]


# ── the family screen ──────────────────────────────────────────────────────

def test_one_tap_and_no_sheet_that_asks_what_to_do_with_a_photo():
    assert "What would you like to do with it?" not in HOME_JS and "Ask about it" not in HOME_JS and "photoSheet" not in HOME_JS
    assert "photoButton.addEventListener('click'" in HOME_JS and "lookAt({ file, question });" in HOME_JS
    assert "body: { file_id: meta.id, hint: question, session_id: S.chat.sid, want }" in HOME_JS
    for fix in ("Add where I found it", "It is a label: read it", "Just describe it", "It is a plant or an animal"):
        assert fix in HOME_JS, fix


def test_there_is_a_plain_line_for_every_tool_and_a_card_for_every_answer():
    labels = set(re.findall(r"^\s+(\w+): '[^']+…'", HOME_JS[HOME_JS.index("const TOOL_LABEL = {"):HOME_JS.index("};", HOME_JS.index("const TOOL_LABEL = {"))], re.M))
    labels |= set(re.findall(r"(\w+): '[^']+…'", HOME_JS[HOME_JS.index("const TOOL_LABEL = {"):HOME_JS.index("};", HOME_JS.index("const TOOL_LABEL = {"))]))
    for tool in ("manage_lists", "manage_reminders", "manage_trackers", "manage_boards", "manage_kitchen", "manage_memory", "manage_calendar",
                 "web_search", "web_fetch", "library_search", "library_browse", "generate_image", "translate", "convert_units", "map_position",
                 "sun_moon", "date_math", "weather", "identify_photo", "trigger_research"):
        assert tool in labels, tool
    for kind in ("weather", "sun", "result", "translation", "list", "identification", "reading", "description", "recipe", "notice"):
        assert f"card.type === '{kind}'" in HOME_JS, kind
    sentences = re.findall(r"'([A-Z][^'\n]{10,})'", HOME_JS) + re.findall(r"`([A-Z][^`\n]{10,})`", HOME_JS)
    assert len(sentences) > 60
    for word in ("model", "token", "context window", "endpoint", "GPU", "agent", "server", "tool"):   # no back-end word is written on the screen
        assert not [text for text in sentences if re.search(rf"\b{word}s?\b", text, re.I)], word


def test_a_failure_offers_a_next_step_and_never_the_servers_own_words():
    assert "function plainFailure(status, detail)" in HOME_JS and "'That did not work.'" in HOME_JS
    assert "'Try again'" in HOME_JS and "`Tell ${S.house.keeper}`" in HOME_JS and "api('/api/home/problem'" in HOME_JS
    assert "throw new Error(typeof detail === 'string' && detail ? detail" not in HOME_JS      # what the server said is not put in the bubble


def test_the_screen_can_hear_and_speak():
    speech = (REPO / "static" / "lite" / "speech.js").read_text(encoding="utf-8")
    for exported in ("export async function listen(", "export function speak(", "export function stopSpeaking()", "export async function canListen()", "export const sayable = "):
        assert exported in speech, exported
    assert "import readAloud from '../js/readAloud.js';" in speech          # the reading itself is shared with the full app
    assert "micButton" in HOME_JS and "canListen().then((yes) => { micButton.hidden = !yes; })" in HOME_JS and "readAloud(reply" in HOME_JS


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_what_is_read_aloud_has_no_markup_in_it():
    out = subprocess.run(["node", "--input-type=module", "-e", """
        globalThis.window = {};
        const { sayable } = await import('./static/lite/speech.js');
        console.log(JSON.stringify(sayable('## Steps\\n- **Clear** the water [see page](http://x/y)\\n- Add `bleach` | wait')));
    """], cwd=REPO, check=True, capture_output=True, text=True, encoding="utf-8")
    # no marks, no address — and each line ends as a sentence, so a heading and the steps under it are not run together
    assert json.loads(out.stdout) == "Steps. Clear the water see page. Add bleach wait"
