"""The household's settings and the weather that goes with its home.

What must hold:
  * the home position, units and language are set once and used by everyone — a person's own saved
    home still wins for that person, and with nothing set nothing changes;
  * the assistant is told about them in one stable paragraph, and not at all on an untouched server;
  * a forecast is fetched from an outside service only when the household allows it, with the position
    rounded and nothing else sent; a second service answers when the first does not;
  * with the internet down, or the services not answering, the answer says so — the last forecast is
    offered only while it is recent, and nothing is ever made up;
  * the tools' answers also come as cards: the few figures a screen shows large;
  * only an administrator can change any of it;
  * none of it touches the real settings, the real place list or the network.

Town names are invented. The forecasts are made-up numbers in the two services' shapes.
"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services import household
from services.almanac import gazetteer, places, weather
from tests.test_almanac import geonames_row

ZONE = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """A household kept in memory, an invented place list, scratch preferences and a network that is never reached."""
    kept = {}
    monkeypatch.delenv("AEGIS_HOUSEHOLD_DISABLED", raising=False)
    monkeypatch.delenv("AEGIS_WEATHER_DISABLED", raising=False)
    monkeypatch.setattr(household, "_stored", lambda: json.loads(json.dumps(kept.get("household", {}))))
    monkeypatch.setattr(household, "_store", lambda value: kept.__setitem__("household", json.loads(json.dumps(value))))
    monkeypatch.setattr(household, "default_units", lambda: "us")
    monkeypatch.setattr(household, "default_language", lambda: "en")
    monkeypatch.setattr(gazetteer, "path", lambda: tmp_path / "almanac" / "places.db")
    monkeypatch.setattr("routes.prefs_routes.PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(weather, "_cache_path", lambda: str(tmp_path / "almanac" / "weather.json"))
    monkeypatch.setattr(weather, "_get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("a test reached the network")))
    monkeypatch.setattr("services.search.netstate.internet_available", lambda force=False: True)
    weather._memory.clear()
    weather._failed_at.clear()
    source = tmp_path / "geonames"
    source.mkdir()
    rows = [geonames_row(1, "Elmford", 35.10, -97.10, "US", "OK", 410000, "America/Chicago", "350"),
            geonames_row(2, "Kürzberg", 50.90, 6.90, "DE", "07", 300000, "Europe/Berlin", "55", "Kurzberg")]
    (source / "cities1000.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (source / "admin1CodesASCII.txt").write_text("US.OK\tOklahoma\tOklahoma\t4\nDE.07\tNorth Rhine-Westphalia\tNorth Rhine-Westphalia\t6\n", encoding="utf-8")
    (source / "countryInfo.txt").write_text("US\tUSA\t840\tUS\tUnited States\nDE\tDEU\t276\tGM\tGermany\n", encoding="utf-8")
    assert gazetteer.build(source) == 2
    from src import user_time
    user_time.clear_user_time_context()
    yield kept
    user_time.clear_user_time_context()
    weather._memory.clear()


# ── the settings ───────────────────────────────────────────────────────────

def test_an_untouched_server_has_defaults_and_says_nothing_to_the_assistant():
    assert household.get() == {"home": None, "units": "us", "language": "en", "live": {"weather": True, "alerts": True, "air": True}, "chosen": []}
    assert household.home() is None and household.prompt_line() == ""
    assert household.units() == "us" and household.language() == "en" and household.language_name() == "English"


def test_the_home_is_a_town_or_coordinates_and_is_checked_before_it_is_kept():
    saved = household.set_home("Elmford, OK")
    assert saved["home"]["label"] == "Elmford, Oklahoma" and saved["home"]["zone"] == "America/Chicago" and saved["home"]["mgrs"]
    assert household.home().kind == "home" and household.get()["chosen"] == ["home"]
    for wrong in ("Xyzzyville", "", "home", "here"):
        with pytest.raises(places.PlaceError):
            household.set_home(wrong)
    with pytest.raises(places.PlaceError):
        household.set_home("Elmford", zone="Mars/Olympus")
    assert household.home().label == "Elmford, Oklahoma"                         # a refused change changes nothing
    assert household.set_home("50.9, 6.9", zone="UTC")["home"]["zone"] == "UTC"
    assert household.forget_home() is True and household.forget_home() is False and household.home() is None


def test_units_language_and_switches_take_known_values_only():
    assert household.update(units="metric")["units"] == "metric"
    assert household.update(language="German")["language"] == "de" and household.language_name() == "German"
    assert household.update(live={"air": False, "weather": "no", "tides": True})["live"] == {"weather": True, "alerts": True, "air": False}
    assert not household.live("air") and household.live("weather")
    for wrong in ({"units": "furlongs"}, {"language": "Klingonese"}, {"live": ["weather"]}):
        with pytest.raises(ValueError):
            household.update(**wrong)
    assert household.get()["units"] == "metric" and household.get()["chosen"] == ["language", "units"]
    described = household.describe()
    assert [choice["id"] for choice in described["unit_choices"]] == ["us", "metric"] and len(described["language_choices"]) > 100
    assert [source["id"] for source in described["sources"]] == ["weather", "alerts", "air"] and described["sources"][2]["on"] is False


def test_home_is_the_households_unless_the_person_saved_their_own():
    assert places.home("zed") is None
    household.set_home("Elmford, OK")
    assert places.home("zed").label == "Elmford, Oklahoma" and places.home(None).label == "Elmford, Oklahoma"
    assert places.resolve("", owner="mia").label == "Elmford, Oklahoma" and places.resolve("home", owner="mia").kind == "home"
    places.set_home("zed", "Kürzberg")
    assert places.home("zed").label.startswith("Kürzberg") and places.home("mia").label == "Elmford, Oklahoma"
    assert places.own_home("mia") is None and places.home("mia", household=False) is None
    household.forget_home()
    with pytest.raises(places.PlaceError, match="no home position is saved"):
        places.resolve("", owner="mia")


def test_the_assistant_is_told_once_in_one_stable_paragraph():
    household.set_home("Elmford, OK")
    line = household.prompt_line()
    assert "home is Elmford, Oklahoma (time zone America/Chicago)" in line and "do not ask where they are" in line
    assert "°F, miles" in line and "reads" not in line                           # English is the default: nothing to say
    assert household.prompt_line() == line
    household.update(units="metric", language="es")
    assert "°C, kilometres" in household.prompt_line() and "answer in Spanish unless asked otherwise" in household.prompt_line()


def test_the_standing_instructions_carry_it(monkeypatch):
    from src import chat_processor
    monkeypatch.delenv("AEGIS_PERSONA_DISABLED", raising=False)
    chat_processor._persona_cache.clear()
    assert chat_processor._persona_for("") == ""
    household.set_home("Elmford, OK")
    chat_processor._persona_cache.clear()
    assert "home is Elmford, Oklahoma" in chat_processor._persona_for("") and "home is Elmford, Oklahoma" in chat_processor._persona_for("nobody-by-that-name")
    chat_processor._persona_cache.clear()
    monkeypatch.setenv("AEGIS_PERSONA_DISABLED", "1")
    assert chat_processor._persona_for("") == ""


# ── the forecast ───────────────────────────────────────────────────────────

def open_meteo_answer(today: date, zone: str = "America/Chicago"):
    days = [today + timedelta(days=step) for step in range(7)]
    hours = [f"{day.isoformat()}T{hour:02d}:00" for day in days for hour in range(24)]
    return {
        "timezone": zone,
        "current": {"time": f"{today.isoformat()}T13:00", "temperature_2m": 47.6, "apparent_temperature": 38.1, "relative_humidity_2m": 40,
                    "weather_code": 0, "wind_speed_10m": 11.5, "wind_gusts_10m": 17.0, "wind_direction_10m": 30, "is_day": 1},
        "daily": {"time": [day.isoformat() for day in days], "weather_code": [3, 61, 0, 73, 95, 2, 45],
                  "temperature_2m_max": [49.1, 43.3, 48.0, 31.0, 66.2, 70.0, 55.5], "temperature_2m_min": [31.5, 29.9, 26.0, 18.4, 50.0, 49.0, 40.1],
                  "precipitation_probability_max": [0, 80, 5, 90, 60, 10, 0], "precipitation_sum": [0.0, 0.42, 0.0, 0.3, 0.9, 0.0, 0.0],
                  "snowfall_sum": [0.0, 0.0, 0.0, 3.1, 0.0, 0.0, 0.0], "wind_speed_10m_max": [12.4, 9.3, 8.0, 22.0, 30.5, 10.0, 6.0],
                  "wind_gusts_10m_max": [19.5, 21.0, 10.0, 35.0, 48.0, 12.0, 8.0], "uv_index_max": [3.65, 2.5, 4.0, 1.0, 6.2, 5.0, 2.0]},
        "hourly": {"time": hours, "temperature_2m": [30 + (int(moment[11:13]) % 12) * 1.5 for moment in hours],
                   "precipitation_probability": [20 if moment[11:13] in ("14", "15") else 0 for moment in hours],
                   "precipitation": [0.0] * len(hours), "weather_code": [61 if moment[11:13] in ("14", "15") else 3 for moment in hours],
                   "wind_speed_10m": [8.0] * len(hours)},
    }


def nws_hours(today: date):
    periods = []
    start = datetime(today.year, today.month, today.day, 13, 0, tzinfo=ZONE)
    for step in range(60):
        moment = start + timedelta(hours=step)
        periods.append({"startTime": moment.isoformat(), "isDaytime": 6 <= moment.hour < 18, "temperature": 40 + (moment.hour % 10),
                        "probabilityOfPrecipitation": {"value": 30 if moment.hour == 15 else None}, "windSpeed": "5 to 10 mph", "windDirection": "NE",
                        "shortForecast": "Chance Rain Showers" if moment.hour == 15 else "Mostly Cloudy", "relativeHumidity": {"value": 53}})
    return {"properties": {"periods": periods}}


@pytest.fixture
def services(monkeypatch):
    """The outside services, answering from made-up numbers and recording what they were asked."""
    asked = []
    state = {"open_meteo": True, "nws": True, "alerts": [], "today": datetime.now(ZONE).date()}

    def get(url, params, accept="application/json", timeout=8.0, tries=3):
        asked.append((url, dict(params)))
        if "air-quality" in url:
            return {"current": {"us_aqi": 42, "european_aqi": 18, "pm2_5": 6.1, "pm10": 9.0}}
        if "api.open-meteo.com" in url:
            if not state["open_meteo"]:
                raise RuntimeError("503")
            return open_meteo_answer(state["today"])
        if "/alerts/active" in url:
            return {"features": [{"properties": alert} for alert in state["alerts"]]}
        if "/points/" in url:
            if not state["nws"]:
                raise RuntimeError("500")
            return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/XXX/1,2/forecast/hourly", "timeZone": "America/Chicago"}}
        if "/forecast/hourly" in url:
            return nws_hours(state["today"])
        raise AssertionError(f"unexpected address {url}")

    monkeypatch.setattr(weather, "_get", get)
    state["asked"] = asked
    return state


def test_a_forecast_for_home_says_the_day_in_labelled_lines_and_as_a_card(services):
    household.set_home("Elmford, OK")
    text, card = weather.answer({}, "mia")
    lines = text.split("\n")
    today = services["today"]
    assert lines[0] == f"Forecast for Elmford, Oklahoma — {today:%A}, {today:%B} {today.day}, {today.year} (today)"
    assert lines[1] == "Right now: 48 °F (feels like 38 °F), clear, wind 12 mph from the NNE, humidity 40%."
    assert "Sky: overcast." in lines and "High 49 °F, low 32 °F." in lines and "Chance of rain: 0%." in lines
    assert "Wind: up to 12 mph, gusts to 20 mph." in lines and "UV index: up to 4 (moderate)." in lines
    assert "Air quality now: 42 on the US index (good)." in lines and "Weather alerts (US National Weather Service): none in effect." in lines
    assert lines[-1].startswith("Source: Open-Meteo, fetched ") and lines[-1].endswith("Temperatures in °F, wind in mph.")
    assert (card["type"], card["title"], card["place"], card["summary"], card["icon"]) == ("weather", "Today", "Elmford, Oklahoma", "Overcast", "cloud")
    assert [tile["label"] for tile in card["tiles"]] == ["Now", "High", "Low", "Rain", "Wind"] and card["tiles"][1]["value"] == "49°"
    assert card["air"] == "Air quality: good" and card["source"].startswith("Forecast: Open-Meteo, ") and "stale" not in card
    # only the rounded position went out, to the three services, with nothing that names anyone
    sent = [(url.split("/")[2], params) for url, params in services["asked"]]
    assert [host for host, _ in sent] == ["api.open-meteo.com", "api.weather.gov", "air-quality-api.open-meteo.com"]
    assert sent[0][1]["latitude"] == 35.1 and sent[0][1]["longitude"] == -97.1 and sent[1][1] == {"point": "35.1,-97.1"}
    assert sent[0][1]["temperature_unit"] == "fahrenheit" and not any("mia" in json.dumps(params) or "Elmford" in json.dumps(params) for _, params in sent)
    assert weather.AGENT == "aegis-home-server"


def test_other_days_the_week_and_the_weekend(services):
    household.set_home("Elmford, OK")
    today = services["today"]
    text, card = weather.answer({"when": "tomorrow"}, None)
    assert "Sky: light rain." in text and "Chance of rain: 80% (0.42 in expected)." in text and "Right now" not in text and "Air quality" not in text
    assert card["title"] == "Tomorrow" and "Through the day — Morning" in text and "Afternoon" in text and "20% chance of rain" in text
    assert [part["label"] for part in card["parts"]] == ["Morning", "Afternoon", "Evening"]
    snow, snow_card = weather.answer({"when": (today + timedelta(days=3)).isoformat()}, None)
    assert "Sky: snow." in snow and "Chance of snow: 90%" in snow and snow_card["tiles"][2]["label"] == "Snow" and snow_card["icon"] == "snow"
    assert weather.answer({"when": "tonight"}, None)[1]["title"] == "Tonight"
    week, week_card = weather.answer({"when": "week"}, None)
    assert week.split("\n")[1] == "Day | Sky | High °F | Low °F | Chance of rain | Wind mph" and week.count(" | ") == 8 * 5
    assert week_card["title"] == "The week ahead" and len(week_card["days"]) == 7 and week_card["days"][0]["label"] == "Today"
    weekend, weekend_card = weather.answer({"when": "this weekend"}, None)
    assert weekend_card["title"] == "This weekend" and 1 <= len(weekend_card["days"]) <= 2 and all(day["label"] in ("Sat", "Sun", "Today") for day in weekend_card["days"])
    assert len([1 for url, _ in services["asked"] if "api.open-meteo.com/v1/forecast" in url]) == 1      # fetched once, then reused
    for when, reason, said in ((today + timedelta(days=30)).isoformat(), "range", "reaches 7 days ahead"), ("yesterday", "range", "not a record"), ("smarch", "range", "as a date"):
        with pytest.raises(weather.WeatherProblem, match=said) as refused:
            weather.answer({"when": when}, None)
        assert refused.value.reason == reason


def test_a_named_place_and_metric_units(services):
    household.update(units="metric")
    text, card = weather.answer({"place": "Kürzberg", "when": "today"}, None)
    assert text.startswith("Forecast for Kürzberg, North Rhine-Westphalia, Germany") and "Weather alerts" not in text      # the alerts are a US service
    assert "Air quality now: 18 on the European index (good)." in text and text.endswith("Temperatures in °C, wind in km/h.")
    asked = dict(services["asked"])
    assert asked["https://api.open-meteo.com/v1/forecast"]["temperature_unit"] == "celsius" and asked["https://api.open-meteo.com/v1/forecast"]["wind_speed_unit"] == "kmh"
    assert not any("weather.gov" in url for url, _ in services["asked"])
    with pytest.raises(weather.WeatherProblem) as nowhere:
        weather.answer({"place": "Xyzzyville"}, None)
    assert nowhere.value.reason == "place"
    with pytest.raises(weather.WeatherProblem, match="no home position is saved"):
        weather.answer({}, None)


def test_alerts_in_effect_are_said_with_what_to_do(services):
    household.set_home("Elmford, OK")
    until = (datetime.now(ZONE) + timedelta(hours=6)).replace(minute=0, second=0, microsecond=0)
    services["alerts"] = [
        {"event": "Wind Advisory", "severity": "Moderate", "status": "Actual", "messageType": "Alert", "ends": until.isoformat(),
         "headline": "Wind Advisory issued", "instruction": "Secure outdoor\nobjects."},
        {"event": "Tornado Warning", "severity": "Extreme", "status": "Actual", "messageType": "Alert", "expires": until.isoformat(), "instruction": "Take shelter now."},
        {"event": "Test Message", "severity": "Minor", "status": "Test", "messageType": "Alert"},
        {"event": "Flood Watch", "severity": "Severe", "status": "Actual", "messageType": "Cancel"},
    ]
    text, card = weather.answer({}, None)
    block = text[text.index("Weather alerts in effect"):].split("\n")
    assert block[1].startswith("- Tornado Warning, until ") and block[1].endswith("Take shelter now.")           # the gravest first
    assert block[2].startswith("- Wind Advisory, until ") and "Secure outdoor objects." in block[2] and "Test Message" not in text and "Flood Watch" not in text
    assert [alert["event"] for alert in card["alerts"]] == ["Tornado Warning", "Wind Advisory"] and card["alerts"][0]["until"].startswith("until ")


def test_a_second_service_answers_when_the_first_does_not(services):
    household.set_home("Elmford, OK")
    services["open_meteo"] = False
    text, card = weather.answer({"when": "tomorrow"}, None)
    assert "Source: US National Weather Service, fetched" in text and card["source"].startswith("Forecast: US National Weather Service, ")
    assert "High 49 °F, low 40 °F." in text and "Chance of rain: 30%." in text and "Wind: up to 10 mph." in text
    assert weather.code_for("Chance Showers And Thunderstorms") == 95 and weather.code_for("Heavy Snow") == 75 and weather.code_for("Patchy Fog") == 45
    assert weather.code_for("Mostly Sunny") == 1 and weather.code_for("Partly Cloudy") == 2 and weather.code_for("Sunny") == 0 and weather.code_for("Light Rain") == 61
    # the one that failed is asked last for a while, so the next place does not wait on it
    services["asked"].clear()
    weather.answer({"place": "35.6, -97.6"}, None)
    assert "api.weather.gov/points" in services["asked"][0][0] and not any("api.open-meteo.com/v1/forecast" in url for url, _ in services["asked"])
    # outside the United States there is no second service: say so
    with pytest.raises(weather.WeatherProblem, match="not answering right now") as none:
        weather.answer({"place": "Kürzberg"}, None)
    assert none.value.reason == "failed"


def test_with_the_internet_down_it_says_so_and_offers_only_a_recent_forecast(services, monkeypatch):
    household.set_home("Elmford, OK")
    with monkeypatch.context() as down:
        down.setattr("services.search.netstate.internet_available", lambda force=False: False)
        with pytest.raises(weather.WeatherProblem, match="No forecast: the internet is not reachable") as offline:
            weather.answer({}, None)
        assert offline.value.reason == "offline" and services["asked"] == []
    fresh_text, _ = weather.answer({}, None)                                    # fetched while online, and kept
    key = next(iter(weather._memory))
    weather._memory[key]["fetched"] = time.time() - 3 * 3600                    # three hours ago
    monkeypatch.setattr("services.search.netstate.internet_available", lambda force=False: False)
    text, card = weather.answer({}, None)
    assert text.split("\n")[1].startswith("The internet is not reachable right now: this is the last forecast fetched, at ") and card["stale"] is True
    assert card["source"].startswith("Last forecast, from ") and card["source"].endswith("(the internet is down)")
    weather._memory.clear()                                                     # a restart: the last one is still on disk
    kept = json.loads(open(weather._cache_path(), encoding="utf-8").read())
    kept[key]["fetched"] = time.time() - 3 * 3600
    open(weather._cache_path(), "w", encoding="utf-8").write(json.dumps(kept))
    assert weather.answer({}, None)[1]["stale"] is True
    kept[key]["fetched"] = time.time() - 20 * 3600                              # too old to be worth reading
    open(weather._cache_path(), "w", encoding="utf-8").write(json.dumps(kept))
    weather._memory.clear()
    with pytest.raises(weather.WeatherProblem, match="No forecast"):
        weather.answer({}, None)


def test_switched_off_means_nothing_is_asked(services):
    household.set_home("Elmford, OK")
    household.update(live={"weather": False})
    with pytest.raises(weather.WeatherProblem, match="switched off for this household") as off:
        weather.answer({}, None)
    assert off.value.reason == "off" and services["asked"] == [] and weather.today_line("mia") is None
    household.update(live={"weather": True, "alerts": False, "air": False})
    text, card = weather.answer({}, None)
    assert [url.split("/")[2] for url, _ in services["asked"]] == ["api.open-meteo.com"] and "Weather alerts" not in text and "Air quality" not in text
    assert weather.today_line("mia")["title"] == "Today"


# ── the tools ──────────────────────────────────────────────────────────────

def tool(name, args, owner="mia"):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args), {"owner": owner}))


def test_the_weather_tool_never_leaves_room_to_guess(services, monkeypatch):
    household.set_home("Elmford, OK")
    answer = tool("weather", {"when": "tomorrow"})
    assert answer["exit_code"] == 0 and answer["output"].startswith("Forecast for Elmford, Oklahoma") and answer["card"]["type"] == "weather"
    assert tool("weather", {"place": "Xyzzyville"})["exit_code"] == 1 and "I do not know a place" in tool("weather", {"place": "Xyzzyville"})["error"]
    monkeypatch.setattr("services.search.netstate.internet_available", lambda force=False: False)
    weather._memory.clear()
    import os
    if os.path.exists(weather._cache_path()):
        os.remove(weather._cache_path())
    none = tool("weather", {})
    assert none["exit_code"] == 0 and "No forecast: the internet is not reachable" in none["output"] and "do not guess the weather" in none["output"]
    assert none["card"] == {"type": "notice", "text": "No forecast: the internet is not reachable right now.", "reason": "offline"}


def test_the_almanac_tools_answer_with_cards_too():
    household.set_home("Elmford, OK")
    sun = tool("sun_moon", {"date": "2026-11-23"})
    assert sun["output"].startswith("Sun and moon at Elmford, Oklahoma — Monday, November 23, 2026 (times: America/Chicago)")      # no place asked for: home
    assert sun["card"]["type"] == "sun" and sun["card"]["place"] == "Elmford, Oklahoma"
    assert [tile["label"] for tile in sun["card"]["tiles"]] == ["Sunrise", "Sunset", "Last light", "Fully dark", "Daylight"]
    assert sun["card"]["moon"].startswith("Moon: waxing gibbous, 99% lit") and "card" not in tool("sun_moon", {"date": "2026-11-23", "days": 3})
    converted = tool("convert_units", {"amount": "2 L", "to": "cups"})["card"]
    assert converted == {"type": "result", "label": "2 L is", "value": "8.454 cups", "detail": "", "note": "", "source": "Worked out"}
    assert tool("convert_units", {"amount": "5 ft 10 in", "to": "cm"})["card"]["value"] == "177.8 cm"
    apart = tool("date_math", {"date": "2031-03-03", "to": "2031-04-04"})["card"]
    assert (apart["label"], apart["value"], apart["detail"]) == ("Monday, March 3, 2031 to Friday, April 4, 2031", "32 days", "4 weeks and 4 days · 24 weekdays")
    until = tool("date_math", {"date": "today", "to": "2031-04-04"})["card"]
    assert until["label"] == "Until Friday, April 4, 2031" and until["value"].endswith(" days")
    assert tool("date_math", {"date": "July 4 2027"})["card"]["value"] == "Sunday"
    assert tool("date_math", {"date": "2026-11-23", "add": "90 days"})["card"]["value"] == "Sunday, February 21, 2027"
    from src.tool_execution import format_tool_result
    assert "card" not in format_tool_result("sun_moon", sun) and "tiles" not in format_tool_result("sun_moon", sun)     # the model is not shown it twice


def test_the_weather_tool_is_on_every_list_a_tool_must_be_on():
    from src.agent_loop import TOOL_SECTIONS, _DOMAIN_RULES, _DOMAIN_TOOL_MAP
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS

    assert "weather" in TOOL_HANDLERS and "weather" in TOOL_TAGS and "weather" in TOOL_SECTIONS and "weather" in BUILTIN_TOOL_DESCRIPTIONS
    assert "weather" in _DOMAIN_TOOL_MAP["almanac"] and "never guess the weather" in _DOMAIN_RULES["almanac"]
    assert "weather" in PLAN_MODE_READONLY_TOOLS and "weather" not in NON_ADMIN_BLOCKED_TOOLS       # anyone may ask what the weather is
    schema = next(entry["function"] for entry in FUNCTION_TOOL_SCHEMAS if entry["function"]["name"] == "weather")
    assert set(schema["parameters"]["properties"]) == {"place", "when"} and "required" not in schema["parameters"]


# ── the console's routes ───────────────────────────────────────────────────

@pytest.fixture
def console(monkeypatch):
    import routes.account_routes as routes

    who = {"user": "boss", "admin": True}

    class Accounts:
        users = {"boss": {}, "mia": {}}

        def is_admin(self, name):
            return name == "boss"

    def need_admin(request):
        if not who["user"]:
            raise HTTPException(401, "Sign in")
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(routes, "require_admin", need_admin)
    monkeypatch.setattr(routes, "get_current_user", lambda request: who["user"])
    app = FastAPI()
    app.include_router(routes.setup_account_routes(Accounts()))
    client = TestClient(app)
    client.who = who
    return client


def test_only_an_administrator_reads_or_changes_the_household(console):
    start = console.get("/api/admin/household").json()
    assert start["home"] is None and start["units"] == "us" and [source["id"] for source in start["sources"]] == ["weather", "alerts", "air"]
    saved = console.put("/api/admin/household", json={"home": "Elmford, OK", "units": "metric", "language": "fr", "live": {"air": False}}).json()
    assert saved["home"]["label"] == "Elmford, Oklahoma" and saved["units"] == "metric" and saved["language_name"] == "French" and saved["live"]["air"] is False
    assert console.put("/api/admin/household", json={"home": "Xyzzyville"}).status_code == 400
    assert console.put("/api/admin/household", json={"units": "furlongs"}).json()["detail"] == "Units are either US customary or metric."
    assert console.get("/api/admin/household").json()["home"]["label"] == "Elmford, Oklahoma"        # the refused changes changed nothing
    assert console.put("/api/admin/household", json={"home": ""}).json()["home"] is None
    console.who["admin"] = False
    assert console.get("/api/admin/household").status_code == 403 and console.put("/api/admin/household", json={"units": "us"}).status_code == 403
    assert household.units() == "metric"
