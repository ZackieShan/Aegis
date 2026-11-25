"""The almanac: exact answers the assistant works out rather than recalls.

What must hold:
  * a unit conversion is arithmetic on the units' definitions — the known
    exact factors come out exactly, and going there and back changes nothing;
  * an amount is read the way people write one (5 ft 10 in, 6'2", 1½ cups,
    30 mpg) and the answer names its units, so a misreading shows;
  * map positions agree with independent implementations (the ``utm`` and
    ``mgrs`` packages, Vincenty's published line) and a grid reference names
    the square its point is in;
  * the sun and the moon agree with PyEphem to well inside a minute, and
    days with no sunrise are said to have none;
  * calendar answers are the calendar's;
  * a place can be coordinates, a town's name (when the list is installed)
    or the person's saved home — and one person's home is not another's;
  * none of it needs a model, the network or the real data folder.

The reference numbers below were produced once by those packages (see
``docs/almanac.md``); the tests need none of them installed. Town names in
the place-list tests are invented.
"""
import asyncio
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.almanac import coordinates, dates, gazetteer, places, sky, units

REPO = Path(__file__).resolve().parent.parent
UTC = timezone.utc


@pytest.fixture(autouse=True)
def away_from_real_data(tmp_path, monkeypatch):
    """No place list unless a test builds one, and preferences in a scratch file."""
    monkeypatch.setattr(gazetteer, "path", lambda: tmp_path / "almanac" / "places.db")
    monkeypatch.setattr("routes.prefs_routes.PREFS_FILE", str(tmp_path / "user_prefs.json"))
    from src import user_time
    user_time.clear_user_time_context()
    yield
    user_time.clear_user_time_context()


# ── units ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount, to, value", [
    ("1 in", "cm", 2.54), ("1 ft", "m", 0.3048), ("1 yd", "m", 0.9144), ("1 mi", "km", 1.609344), ("1 nmi", "m", 1852), ("1 fathom", "ft", 6),
    ("1 lb", "g", 453.59237), ("1 oz", "g", 28.349523125), ("1 short ton", "lb", 2000), ("1 long ton", "lb", 2240), ("1 t", "kg", 1000),
    ("1 grain", "mg", 64.79891), ("1 troy oz", "g", 31.1034768), ("1 stone", "lb", 14),
    ("1 gal", "L", 3.785411784), ("1 imp gal", "L", 4.54609), ("1 gal", "in3", 231), ("1 cup", "tbsp", 16), ("1 tbsp", "tsp", 3),
    ("1 qt", "pt", 2), ("1 fl oz", "mL", 29.5735295625), ("1 imp pt", "mL", 568.26125), ("1 cord", "ft3", 128), ("1 bushel", "in3", 2150.42),
    ("1 bbl", "gal", 42), ("1 acre", "ft2", 43560), ("1 ha", "m2", 10000), ("1 mi2", "acre", 640),
    ("32 F", "C", 0), ("212 F", "C", 100), ("-40 F", "C", -40), ("0 K", "C", -273.15), ("100 C", "K", 373.15), ("0 C", "F", 32), ("491.67 R", "C", 0),
    ("60 mph", "ft/s", 88), ("1 kn", "km/h", 1.852), ("36 km/h", "m/s", 10),
    ("1 atm", "kPa", 101.325), ("1 psi", "Pa", 6894.757293168361), ("1 bar", "kPa", 100), ("1 mmHg", "Pa", 133.322387415), ("760 torr", "atm", 1), ("760 Torr", "Pa", 101325),
    ("1 kgf", "N", 9.80665), ("1 lbf", "N", 4.4482216152605),
    ("1 BTU", "J", 1055.05585262), ("1 kWh", "MJ", 3.6), ("1 kcal", "kJ", 4.184), ("1 ft-lb", "J", 1.3558179483314004), ("1 Nm", "J", 1),
    ("1 hp", "W", 745.6998715822702), ("12000 BTU/h", "TR", 1),
    ("1 Sv", "rem", 100), ("1 Gy", "rad", 100), ("1 Ci", "GBq", 37), ("6400 mil", "deg", 360), ("180 deg", "rad", math.pi), ("1 turn", "deg", 360),
    ("1 GiB", "MiB", 1024), ("1 GB", "MB", 1000), ("1 B", "bit", 8), ("8 Mbps", "MB/s", 1), ("1 d", "h", 24), ("1 wk", "d", 7),
    ("1 L/100km", "km/L", 100), ("100 km/L", "L/100km", 1), ("235.214583 mpg", "L/100km", 1.0000000), ("1 gpm", "gal/h", 60), ("1 g/cm3", "kg/m3", 1000),
])
def test_a_conversion_is_the_arithmetic_of_the_definitions(amount, to, value):
    assert units.convert(amount, to)["value"] == pytest.approx(value, rel=2e-9, abs=1e-9)


def test_there_and_back_changes_nothing():
    chooser = random.Random(5)
    for entry in units.listing():
        symbols = [unit["symbol"] for unit in entry["units"]]
        for _ in range(6):
            first, second = chooser.choice(symbols), chooser.choice(symbols)
            start = round(chooser.uniform(0.5, 500), 3)
            there = units.convert(f"{start} {first}", second)["value"]
            back = units.convert(f"{there!r} {second}", first)["value"]
            assert back == pytest.approx(start, rel=1e-9), (entry["kind"], first, second)


@pytest.mark.parametrize("amount, to, answer", [
    ("5 ft 10 in", "cm", "177.8 cm"), ("5 ft and 10 in", "m", "1.778 m"), ("6'2\"", "cm", "187.96 cm"), ("5′ 10″", "cm", "177.8 cm"),
    ("2 lb 3 oz", "g", "992.2333 g"), ("3 1/2 inches", "mm", "88.9 mm"), ("1/4 in", "mm", "6.35 mm"), ("½ cup", "mL", "118.2941 mL"),
    ("1½ cups", "mL", "354.8824 mL"), ("1,500 ft", "m", "457.2 m"), ("1e3 m", "km", "1 km"), ("−40 C", "F", "-40 °F"),
    ("177.8 cm", "ft in", "5 ft 10 in"), ("2.5 lb", "lb oz", "2 lb 8 oz"), ("90 min", "h min", "1 h 30 min"), ("71.99999 in", "ft in", "6 ft 0 in"),
    ("40 lb/acre", "kg/ha", "44.83405 kg/ha"), ("2 kWh/day", "W", "83.33333 W"), ("60 miles per hour", "feet per second", "88 ft/s"),
    ("15 miles a gallon", "km/L", "6.377156 km/L"), ("30 mpg", "L/100 km", "7.840486 L/100 km"), ("72 F", "C", "22.22 °C"),
])
def test_an_amount_is_read_the_way_people_write_one(amount, to, answer):
    assert units.convert(amount, to)["answer"] == answer


def test_the_request_can_come_as_one_sentence():
    assert units.split_request("5 ft 10 in to cm") == ("5 ft 10 in", "cm")
    assert units.split_request("what is 72 F in C?") == ("72 F", "C")
    assert units.split_request("10 in in cm") == ("10 in", "cm")                 # the last "in" means "into"
    assert units.split_request("convert 3 gal into L") == ("3 gal", "L")
    assert units.split_request("2 in") == ("2 in", "")
    assert units.convert("10 in in cm")["answer"] == "25.4 cm" and units.convert(5, "m", source="ft")["answer"] == "1.524 m"


def test_the_answer_names_its_units_so_a_misreading_shows():
    assert units.convert("8 oz", "mL")["text"] == "8 fl oz = 236.5882 mL — US fluid ounces to millilitres"
    assert units.convert("8 oz", "g")["text"] == "8 oz = 226.7962 g — ounces to grams"
    assert units.convert("8 oz")["text"] == "8 oz (ounces) = 226.7962 g = 0.2267962 kg"     # with nothing to go on, an ounce is weight
    assert units.convert("1 ton", "kg")["text"].endswith("short tons (2,000 lb) to kilograms")
    assert units.convert("3 gal", "L")["text"].endswith("US gallons to litres")
    # the same letters for two units of one kind: the reading a person would write wins, and is said
    sea = units.convert("5 nm", "mi")
    assert sea["answer"] == "5.753897 mi" and sea["note"] == "Read “nm” as nautical miles, not nanometres."
    assert units.convert("500 nm", "µm")["answer"] == "0.5 µm" and units.convert("500 nm", "µm")["note"] == ""
    # … of two kinds: the other unit decides, and with none given the choice is said
    assert units.convert("3200 mils", "deg")["answer"] == "180°" and units.convert("1 mil", "mm")["answer"] == "0.0254 mm"
    assert "it can also mean mils (NATO" in units.convert("5 mil")["note"]
    assert units.convert("2 rad", "deg")["kind"] == "angle" and units.convert("50 rad", "Gy")["kind"] == "absorbed dose"


def test_with_no_unit_asked_for_it_gives_the_usual_ones_of_the_other_system():
    assert units.convert("1 mile")["text"] == "1 mi (mile) = 1.609344 km = 1,609.344 m"
    assert units.convert("5 kg")["text"] == "5 kg (kilograms) = 11.02311 lb = 176.3698 oz"
    assert units.convert("2 cups")["text"].startswith("2 cups (US cups) = 473.1765 mL")
    assert units.convert("350 F")["text"] == "350 °F (degrees Fahrenheit) = 176.67 °C = 449.82 K"
    assert units.convert("55 mph")["also"][0]["unit"] == "km/h" and units.convert("10 acres")["also"][0]["text"] == "4.046856 ha"
    assert units.convert("1 L/100km")["also"][0]["unit"] == "mpg"               # upside down, into the other system


@pytest.mark.parametrize("amount, to, said", [
    ("3 m", "seconds", "Metres measure length and seconds measure time"), ("5 ft 3 kg", "m", "cannot be added together"),
    ("5 florps", "m", "do not know the unit “florps”"), ("5 m", "florps", "do not know the unit “florps”"),
    ("five feet", "m", "could not read a number"), ("", "m", "no amount there"), ("5", "m", "what the number is a number of"),
    ("0 mpg", "L/100km", "Zero cannot be turned upside down"), ("70 F 2 F", "C", "one number"), ("1/0 in", "cm", "zero underneath"),
])
def test_what_cannot_be_converted_is_refused_in_plain_words(amount, to, said):
    with pytest.raises(units.UnitError, match=said):
        units.convert(amount, to)


def test_numbers_are_written_without_noise():
    shown = {value: units.show(value) for value in (177.8, 1609.344, 0.0254, 12754311.2, 0, 1e-9, 3.2e18, 1.0000001, -0.5, 999999.96)}
    assert shown == {177.8: "177.8", 1609.344: "1,609.344", 0.0254: "0.0254", 12754311.2: "12,754,311", 0: "0", 1e-9: "1e-9",
                     3.2e18: "3.2e18", 1.0000001: "1", -0.5: "-0.5", 999999.96: "1,000,000"}


def test_the_menu_lists_the_usual_units_of_each_kind():
    menu = {entry["kind"]: [unit["symbol"] for unit in entry["units"]] for entry in units.listing()}
    assert {"length", "area", "volume", "weight", "temperature", "speed", "pressure", "energy", "power", "fuel economy", "angle"} <= set(menu)
    assert menu["temperature"] == ["°C", "°F", "K"] and {"m", "km", "ft", "mi"} <= set(menu["length"]) and "thou" not in menu["length"]
    assert all(unit["name"] for entry in units.listing() for unit in entry["units"])


# ── map positions ──────────────────────────────────────────────────────────

# (lat, lon) -> zone, band, easting, northing from the `utm` package; the reference from the `mgrs` package
GRID = [
    (40.748433, -73.985656, 18, "T", 585632.081, 4511326.154, "18TWL8563211326"),
    (-33.8568, 151.2153, 56, "H", 334900.570, 6252288.753, "56HLH3490052288"),
    (51.4779, -0.0015, 30, "U", 708213.495, 5707235.661, "30UYC0821307235"),
    (35.6586, 139.7454, 54, "S", 386437.603, 3946808.155, "54SUE8643746808"),
    (-54.8019, -68.303, 19, "F", 544805.097, 3927029.884, "19FEV4480527029"),
    (64.1466, -21.9426, 27, "W", 454138.377, 7113689.870, "27WVM5413813689"),
    (60.3913, 5.3221, 32, "V", 297353.933, 6700648.346, "32VKN9735300648"),      # south-west Norway: zone 32 is widened
    (78.2232, 15.6267, 33, "X", 514278.715, 8683355.470, "33XWG1427883355"),     # Svalbard: zones 32, 34, 36 are not used
    (0.5, 0.5, 31, "N", 221734.222, 55318.040, "31NBA2173455318"),
    (-0.5, -179.5, 1, "M", 221734.222, 9944681.960, "01MBV2173444681"),
    (19.4326, -99.1332, 14, "Q", 486017.331, 2148700.220, "14QMG8601748700"),
    (-77.8463, 166.6682, 58, "C", 539201.928, 1358225.375, "58CEU3920158225"),
    (71.2906, -156.7886, 4, "W", 579155.064, 7911254.869, "04WEE7915511254"),
    (1.3521, 103.8198, 48, "N", 368700.272, 149479.866, "48NUG6870049479"),
]


@pytest.mark.parametrize("lat, lon, zone, band, easting, northing, reference", GRID)
def test_grid_positions_agree_with_independent_implementations(lat, lon, zone, band, easting, northing, reference):
    found = coordinates.to_utm(lat, lon)
    assert found[:2] == (zone, band) and found[2] == pytest.approx(easting, abs=0.002) and found[3] == pytest.approx(northing, abs=0.002)
    back = coordinates.from_utm(zone, found[2], found[3], south=lat < 0)
    assert back[0] == pytest.approx(lat, abs=1e-8) and back[1] == pytest.approx(lon, abs=1e-8)
    assert coordinates.to_mgrs(lat, lon) == reference
    middle = coordinates.from_mgrs(reference)                                     # the middle of the 1 m square: within its half-diagonal
    assert coordinates.between((lat, lon), middle)["metres"] < 0.75 and coordinates.to_mgrs(*middle) == reference
    assert coordinates.read(reference).kind == "mgrs" and coordinates.read(f"{zone}{band} {found[2]:.0f} {found[3]:.0f}").kind == "utm"


def test_a_grid_reference_names_a_square_and_is_cut_short_not_rounded():
    lat, lon = 40.748433, -73.985656
    assert [coordinates.to_mgrs(lat, lon, digits) for digits in (1, 2, 3, 4, 5)] == ["18TWL81", "18TWL8511", "18TWL856113", "18TWL85631132", "18TWL8563211326"]
    assert coordinates.mgrs_square("18TWL8511")[2] == 1000 and coordinates.mgrs_square("18TWL")[2] == 100000
    middle = coordinates.mgrs_square("18T WL 85 11")
    easting, northing = coordinates.to_utm(middle[0], middle[1])[2:]
    assert (easting, northing) == (pytest.approx(585500, abs=0.01), pytest.approx(4511500, abs=0.01))
    assert coordinates.read("18TWL8511").note == "That reference names a 1 km square; this is its middle."
    assert coordinates.describe(-33.8568, 151.2153) == {"decimal": "-33.856800, 151.215300", "dms": "33°51'24.5\"S 151°12'55.1\"E",
                                                         "utm": "56H 334900 6252288", "mgrs": "56HLH3490052288"}
    assert "outside the UTM grid" in coordinates.describe(89.5, 10)["mgrs"]
    assert -180 <= coordinates.from_mgrs("60XWG9394")[1] < 180                    # a square that runs past the date line


@pytest.mark.parametrize("written, lat, lon", [
    ("40.748433, -73.985656", 40.748433, -73.985656), ("40.748433 -73.985656", 40.748433, -73.985656),
    ("40.748433 N 73.985656 W", 40.748433, -73.985656), ("N 40.748433 W 73.985656", 40.748433, -73.985656),
    ("N40.748433 W73.985656", 40.748433, -73.985656), ("73.985656 W, 40.748433 N", 40.748433, -73.985656),
    ("lat 40.748433 lon -73.985656", 40.748433, -73.985656), ("lon -73.985656 lat 40.748433", 40.748433, -73.985656),
    ("Latitude: 40.748433, Longitude: -73.985656", 40.748433, -73.985656), ("40.748433 north, 73.985656 west", 40.748433, -73.985656),
    ("40°44'54.36\"N 73°59'08.36\"W", 40.748433, -73.985656), ("40° 44' 54.36\" N, 73° 59' 8.36\" W", 40.748433, -73.985656),
    ("40 44 54.36 N 73 59 8.36 W", 40.748433, -73.985656), ("40-44-54.36N 073-59-08.36W", 40.748433, -73.985656),
    ("40 44.906 N 73 59.1394 W", 40.748433, -73.985656), ("40 degrees 44.906 minutes N, 73 degrees 59.1394 minutes W", 40.748433, -73.985656),
    ("40,748433; -73,985656", 40.748433, -73.985656), ("40,-73", 40.0, -73.0), ("33°51'24.48\"S 151°12'55.08\"E", -33.8568, 151.2153),
    ("18T 585632 4511326", 40.748433, -73.985656), ("zone 18T 585632mE 4511326mN", 40.748433, -73.985656),
    ("56H 334900 6252288", -33.8568, 151.2153), ("18TWL8563211326", 40.748433, -73.985656), ("mgrs 18T WL 85632 11326", 40.748433, -73.985656),
])
def test_a_place_is_read_however_it_is_written(written, lat, lon):
    found = coordinates.read(written)
    assert coordinates.between((found.lat, found.lon), (lat, lon))["metres"] < 2.0, (found.lat, found.lon)
    assert coordinates.parse(written) == (found.lat, found.lon)


def test_a_letter_that_can_mean_a_band_or_a_hemisphere_is_settled_by_the_numbers():
    north = coordinates.read("18N 585632 4511326")                               # band N is 0°–8°N; this northing is at 40°N
    assert north.lat == pytest.approx(40.7484, abs=1e-3) and "northern hemisphere" in north.note
    south = coordinates.read("56S 334900 6252288")                               # band S is 32°–40°N; taken as "south"
    assert south.lat == pytest.approx(-33.8568, abs=1e-3) and "southern hemisphere" in south.note
    assert coordinates.read("18S 585632 4000000").lat == pytest.approx(36.14, abs=0.01) and coordinates.read("18S 585632 4000000").note == ""
    assert "read as north of the equator" in coordinates.read("18 585632 4511326").note
    assert "lies in latitude band T, not Q" in coordinates.read("18Q 585632 4511326").note


@pytest.mark.parametrize("written, said", [
    ("", "Give a place"), ("hello", "could not find a latitude and a longitude"), ("meet me at the east gate", "could not find"),
    ("91, 10", "Latitude runs from -90 to 90"), ("40 61 0 N 73 0 0 W", "under 60"), ("40.5 30 N 73 0 W", "cannot be followed by minutes"),
    ("40 N 73 N", "north or south, and the other east or west"), ("40 N 73", "could not find"), ("1 2 3", "could not find"),
    ("18TZZ1234", "no 100 km square ZZ in zone 18"), ("18TWL123", "even number of digits"), ("99TWL1234", "from 1 to 60"),
    ("18IWL1234", "I is not a latitude band"), ("99T 585632 4511326", "from 1 to 60"), ("18I 585632 4511326", "I is not a UTM latitude band"),
    ("18CWL8563211326", "does not lie in band C"),
])
def test_what_is_not_a_place_is_refused_in_plain_words(written, said):
    with pytest.raises(coordinates.PlaceError, match=said):
        coordinates.read(written)


def test_distance_and_bearing_match_the_published_survey_line():
    flinders, buninyong = (-37.951033417, 144.424867889), (-37.652821139, 143.926495528)      # Vincenty's worked example
    found = coordinates.between(flinders, buninyong)
    assert found["metres"] == pytest.approx(54972.271, abs=0.002)
    assert found["bearing"] == pytest.approx(306.86816, abs=1e-5) and found["back_bearing"] == pytest.approx(127.17363, abs=1e-5)
    there = coordinates.onward(flinders, 306.86816, 54972.271)
    assert there[0] == pytest.approx(buninyong[0], abs=1e-7) and there[1] == pytest.approx(buninyong[1], abs=1e-7)
    chooser = random.Random(9)
    for _ in range(200):                                                          # going out and measuring back agree
        start, bearing, metres = (chooser.uniform(-85, 85), chooser.uniform(-180, 180)), chooser.uniform(0, 360), chooser.uniform(1, 9_000_000)
        assert coordinates.between(start, coordinates.onward(start, bearing, metres))["metres"] == pytest.approx(metres, abs=0.001)
    assert coordinates.between((10, 10), (10, 10)) == {"metres": 0.0, "bearing": 0.0, "back_bearing": 0.0}
    assert coordinates.between((0, 0), (0, 180))["metres"] == pytest.approx(20_015_000, rel=1e-3)   # opposite points: still an answer
    assert [coordinates.compass(angle) for angle in (0, 11, 12, 45, 100, 180, 269, 349, 359.9)] == ["N", "N", "NNE", "NE", "E", "S", "W", "N", "N"]


# ── sun and moon ───────────────────────────────────────────────────────────

# place, lat, lon, UTC day -> sunrise, sunset, civil dawn, civil dusk, moonrise, moonset (UTC, from PyEphem; None = not that day)
SKY = [
    ("Denver", 39.7392, -104.9847, "2026-11-23", "13:53:36", "23:38:53", "13:24:11", "00:08:45", "22:39:37", "12:53:50"),
    ("Sydney", -33.8679, 151.2073, "2026-12-21", "18:41:06", "09:05:25", "18:11:58", "09:34:34", "06:05:50", "16:13:00"),
    ("Quito", -0.2299, -78.5249, "2027-03-20", "11:18:18", "23:24:48", "10:57:38", "23:45:28", "22:04:57", "09:38:27"),
    ("Tromso", 69.6489, 18.9551, "2026-12-21", None, None, "08:31:15", "12:53:10", None, None),
    ("Tromso", 69.6489, 18.9551, "2027-06-21", None, None, None, None, None, None),
    ("London", 51.5085, -0.1257, "2027-06-21", "03:43:04", "20:21:27", "02:55:16", "21:09:16", "22:12:26", "05:38:57"),
    ("Anchorage", 61.2181, -149.9003, "2027-01-15", "18:56:48", "01:19:27", "18:00:47", "02:15:54", "19:57:34", "10:59:04"),
    ("Singapore", 1.2897, 103.8501, "2028-02-29", "23:14:13", "11:19:40", "22:53:23", "11:40:30", "01:35:17", "13:58:47"),
]


@pytest.mark.parametrize("name, lat, lon, day, sunrise, sunset, dawn, dusk, moonrise, moonset", SKY)
def test_rising_and_setting_agree_with_an_almanac(name, lat, lon, day, sunrise, sunset, dawn, dusk, moonrise, moonset):
    on = date.fromisoformat(day)
    found = sky.day(on, lat, lon, UTC)
    for key, expected in (("sunrise", sunrise), ("sunset", sunset), ("civil_dawn", dawn), ("civil_dusk", dusk), ("moonrise", moonrise), ("moonset", moonset)):
        if expected is None:
            assert found[key] is None, (name, key, found[key])
        else:
            want = datetime.fromisoformat(f"{day}T{expected}+00:00")
            assert found[key] is not None and abs((found[key] - want).total_seconds()) < 45, (name, key, found[key], want)


def test_days_with_no_sunrise_or_no_sunset_say_so():
    dark = sky.day(date(2026, 12, 21), 69.6489, 18.9551, ZoneInfo("Europe/Oslo"))
    assert dark["sun_state"] == "down" and dark["day_length"] == 0 and dark["sunrise"] is None and dark["civil_dawn"] is not None
    light = sky.day(date(2027, 6, 21), 69.6489, 18.9551, ZoneInfo("Europe/Oslo"))
    assert light["sun_state"] == "up" and light["day_length"] == 86400 and light["astronomical_dusk"] is None
    assert light["noon_height"] == pytest.approx(43.8, abs=0.2) and light["noon"].hour in (11, 12)
    # far north in summer the evening's sunset falls after midnight: on this date the sun sets, then rises
    edge = sky.day(date(2027, 6, 21), 64.8378, -147.7164, ZoneInfo("America/Anchorage"))
    assert edge["sun_state"] == "" and edge["sunset"] < edge["sunrise"] and 21 * 3600 < edge["day_length"] < 22.5 * 3600
    with pytest.raises(ValueError):
        sky.day(date(2027, 1, 1), 91, 0, UTC)


def test_times_are_given_on_the_clock_asked_for():
    found = sky.day(date(2026, 11, 23), 39.7392, -104.9847, ZoneInfo("America/Denver"))
    assert (found["sunrise"].hour, found["sunrise"].minute) in ((6, 53), (6, 54)) and found["sunrise"].utcoffset() == timedelta(hours=-7)
    assert (found["sunset"].hour, found["sunset"].minute) in ((16, 38), (16, 39)) and found["day_length"] == pytest.approx(9.755 * 3600, abs=120)
    assert found["noon_height"] == pytest.approx(29.9, abs=0.3) and found["moonset"] < found["moonrise"]      # last night's moon sets first
    after = found["moon_sets_after"]                                              # … and the one that rises today sets tomorrow (almanac: 14:13 UTC)
    assert after.date() == date(2026, 11, 24) and abs((after - datetime(2026, 11, 24, 14, 13, tzinfo=UTC)).total_seconds()) < 120
    assert [item["at"].tzinfo for item in found["moon"]["next"]] == [ZoneInfo("America/Denver")] * 4


def test_the_moons_phases_agree_with_an_almanac():
    after = datetime(2026, 11, 23, 18, tzinfo=UTC)
    for angle, moment in ((0.0, "2026-12-09T00:51:47"), (90.0, "2026-12-17T05:42:36"), (180.0, "2026-11-24T14:53:29"), (270.0, "2026-12-01T06:08:35")):
        assert abs((sky.next_phase(after, angle) - datetime.fromisoformat(moment + "+00:00")).total_seconds()) < 180, angle
    found = sky.moon_phase(after)
    assert found["name"] == "Waxing gibbous" and found["waxing"] and found["lit"] == pytest.approx(0.987, abs=0.005)
    assert found["age_days"] == pytest.approx(14.46, abs=0.05) and [item["name"] for item in found["next"]] == ["Full moon", "Last quarter", "New moon", "First quarter"]
    assert sky.moon_phase(datetime(2026, 11, 24, 15, tzinfo=UTC))["name"] == "Full moon"
    assert sky.moon_phase(datetime(2026, 12, 9, 1, tzinfo=UTC))["name"] == "New moon" and sky.moon_phase(datetime(2026, 12, 9, 1, tzinfo=UTC))["lit"] < 0.01
    names = [sky.phase_name(angle, 0.5) for angle in (0, 45, 90, 135, 180, 225, 270, 315, 359)]
    assert names == ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon", "Waning gibbous", "Last quarter", "Waning crescent", "New moon"]


# place, lat, lon, UTC time -> sun bearing, sun height, moon bearing, moon height as seen from the place, % lit (PyEphem, no air)
SKY_AT = [
    ("Denver", 39.7392, -104.9847, "2026-11-24T04:00:00", 285.945, -49.687, 108.806, 57.032, 99.51),
    ("Sydney", -33.8679, 151.2073, "2027-01-10T02:30:00", 331.135, 76.675, 49.550, 66.108, 4.49),
    ("London", 51.5085, -0.1257, "2027-06-21T12:00:00", 178.894, 61.924, 310.366, -53.041, 94.52),
    ("Nairobi", -1.2833, 36.8167, "2028-09-01T15:45:00", 277.862, -3.270, 105.998, 26.845, 95.11),
]


@pytest.mark.parametrize("name, lat, lon, moment, sun_bearing, sun_height, moon_bearing, moon_height, lit", SKY_AT)
def test_where_the_sun_and_moon_are_agrees_with_an_almanac(name, lat, lon, moment, sun_bearing, sun_height, moon_bearing, moon_height, lit):
    when = datetime.fromisoformat(moment + "+00:00")
    sun, moon = sky.sun_position(when, lat, lon), sky.moon_position(when, lat, lon)
    assert sun["bearing"] == pytest.approx(sun_bearing, abs=0.05) and sun["true_height"] == pytest.approx(sun_height, abs=0.03)
    seen = moon["true_height"] - moon["parallax"] * math.cos(math.radians(moon["true_height"]))
    assert moon["bearing"] == pytest.approx(moon_bearing, abs=0.05) and seen == pytest.approx(moon_height, abs=0.03)
    assert sky.moon_lit(when) * 100 == pytest.approx(lit, abs=0.1)


def test_the_textbook_examples_come_out():
    """Meeus, Astronomical Algorithms, examples 47.a and 25.a — at the book's instant (its time scale is ours less DELTA_T)."""
    book = timedelta(seconds=sky.DELTA_T)
    longitude, latitude, distance = sky.moon_ecliptic(datetime(1992, 4, 12, tzinfo=UTC) - book)
    assert longitude == pytest.approx(133.167265, abs=0.003) and latitude == pytest.approx(-3.229126, abs=0.001)
    assert distance == pytest.approx(368409.7, abs=12)
    sun_longitude, sun_distance = sky.sun_ecliptic(datetime(1992, 10, 13, tzinfo=UTC) - book)
    assert sun_longitude == pytest.approx(199.90895, abs=0.001) and sun_distance == pytest.approx(0.99766, abs=1e-5)
    assert sky.refraction(0) == pytest.approx(0.48, abs=0.03) and sky.refraction(90) < 0.001 and sky.refraction(-5) == 0


# ── dates ──────────────────────────────────────────────────────────────────

TODAY = date(2026, 11, 23)        # a Monday


@pytest.mark.parametrize("written, expected", [
    ("2027-03-12", date(2027, 3, 12)), ("March 12, 2027", date(2027, 3, 12)), ("12 March 2027", date(2027, 3, 12)), ("12th of March 2027", date(2027, 3, 12)),
    ("3/12/2027", date(2027, 3, 12)), ("13/3/2027", date(2027, 3, 13)), ("12.3.2027", date(2027, 3, 12)), ("Mar 12", date(2026, 3, 12)),
    ("today", TODAY), ("tomorrow", date(2026, 11, 24)), ("yesterday", date(2026, 11, 22)), ("friday", date(2026, 11, 27)),
    ("next friday", date(2026, 11, 27)), ("monday", date(2026, 11, 30)), ("last monday", date(2026, 11, 16)), ("last sunday", date(2026, 11, 22)),
    ("Dec 25", date(2026, 12, 25)), ("July 4 1976", date(1976, 7, 4)), ("the 4th of July", date(2026, 7, 4)), ("feb 2028", date(2028, 2, 1)),
    ("1/2/03", date(2003, 1, 2)), ("2026-11-23T18:30:00", TODAY),
])
def test_a_date_is_read_the_way_people_write_one(written, expected):
    assert dates.parse(written, TODAY) == expected


def test_what_is_not_a_date_is_refused():
    for written, said in (("2/29/2027", "not a day on the calendar"), ("blah", "could not read “blah” as a date"), ("", "Give a date"), ("32 March 2027", "not a day")):
        with pytest.raises(dates.DateError, match=said):
            dates.parse(written, TODAY)


def test_the_time_between_two_dates_is_counted_by_the_calendar():
    assert dates.apart(TODAY, date(2027, 3, 12)) == {"days": 109, "weeks": 15, "odd_days": 4, "years": 0, "months": 3, "rest_days": 17,
                                                      "weekdays": 79, "backwards": False}
    born = dates.apart(date(1976, 7, 4), TODAY)
    assert (born["days"], born["years"], born["months"], born["rest_days"]) == (18404, 50, 4, 19)
    assert dates.apart(date(2024, 1, 31), date(2024, 2, 29))["months"] == 1 and dates.apart(date(2024, 1, 31), date(2024, 2, 29))["rest_days"] == 0
    assert dates.apart(date(2026, 11, 20), TODAY)["weekdays"] == 1 and dates.apart(TODAY, date(2026, 11, 30))["weekdays"] == 5   # Friday to Monday is one weekday
    assert dates.apart(TODAY, date(2026, 11, 20))["backwards"] is True
    assert dates.apart_text(TODAY, date(2027, 3, 12)) == ("From Monday, November 23, 2026 to Friday, March 12, 2027: 109 days — 15 weeks and 4 days; "
                                                           "3 months, 17 days; 79 weekdays (Monday to Friday).")
    assert dates.apart_text(TODAY, TODAY) == "Monday, November 23, 2026 — the same day." and "3 days back" in dates.apart_text(TODAY, date(2026, 11, 20))


@pytest.mark.parametrize("start, amount, expected, noted", [
    (TODAY, "90 days", date(2027, 2, 21), ""), (TODAY, "6 weeks", date(2027, 1, 4), ""), (TODAY, "-3 months", date(2026, 8, 23), ""),
    (TODAY, "2 weeks ago", date(2026, 11, 9), ""), (TODAY, "1 year 2 months 3 days", date(2028, 1, 26), ""), (TODAY, "1 fortnight", date(2026, 12, 7), ""),
    (date(2027, 1, 31), "1 month", date(2027, 2, 28), "February is shorter"), (date(2028, 2, 29), "1 year", date(2029, 2, 28), "February 29 does not exist"),
    (TODAY, "10 business days", date(2026, 12, 7), "weekends are skipped"), (date(2026, 11, 27), "1 business day", date(2026, 11, 30), "weekends"),
    (date(2026, 11, 28), "-1 business day", date(2026, 11, 27), "weekends"), (TODAY, "27 working days", date(2026, 12, 30), "public holidays are not"),
])
def test_a_date_is_moved_by_the_calendar(start, amount, expected, noted):
    moved, notes = dates.move(start, amount)
    assert moved == expected and (noted in " ".join(notes) if noted else notes == [])


def test_a_date_says_its_weekday_and_where_it_falls():
    assert dates.about(date(2027, 3, 12), TODAY) == "Friday, March 12, 2027 — day 71 of 365, week 10 of 2027 (ISO); 109 days from today."
    assert dates.about(date(1976, 7, 4), TODAY) == "Sunday, July 4, 1976 — day 186 of 366, week 27 of 1976 (ISO); 18,404 days ago."
    assert dates.about(TODAY, TODAY).endswith("; today.")
    for amount, said in (("soon", "could not read “soon”"), ("", "Say how far"), ("99999 years", "beyond the calendar"), ("3 days and a bit", "could not read")):
        with pytest.raises(dates.DateError, match=said):
            dates.move(TODAY, amount)


# ── town names (an invented list) ──────────────────────────────────────────

def geonames_row(number, name, lat, lon, country, region, population, zone, height="", other="", kind="PPL"):
    cells = [str(number), name, gazetteer.fold(name).title(), other, str(lat), str(lon), "P", kind, country, "", region, "", "", "", str(population),
             height, height, zone, "2026-01-01"]
    return "\t".join(cells)


@pytest.fixture
def towns(tmp_path):
    source = tmp_path / "geonames"
    source.mkdir()
    rows = [
        geonames_row(1, "Elmford", 39.50, -105.00, "US", "CO", 410000, "America/Denver", "1600", "Elm Ford,ELF"),
        geonames_row(2, "Elmford", 44.20, -70.10, "US", "ME", 21000, "America/New_York", "40"),
        geonames_row(3, "Elmford", 52.10, -1.20, "GB", "ENG", 9000, "Europe/London", "90"),
        geonames_row(4, "Elmford", 35.10, -97.10, "US", "OK", 1200, "America/Chicago", "350"),
        geonames_row(5, "St. Brannoc", 38.60, -90.20, "US", "MO", 150000, "America/Chicago", "140", "Saint Brannoc"),
        geonames_row(6, "Kürzberg", 50.90, 6.90, "DE", "07", 300000, "Europe/Berlin", "55", "Kurzberg,Courtmont"),
        geonames_row(7, "Courtmont", 45.50, 9.90, "IT", "09", 4000, "Europe/Rome", "200"),
        geonames_row(8, "Harrow Hill", 39.52, -104.97, "US", "CO", 15000, "America/Denver", "1650"),
        geonames_row(9, "Old Market", 39.505, -104.99, "US", "CO", 0, "America/Denver", "1600", "", "PPLX"),      # a district of Elmford
        geonames_row(10, "Pinewater", 43.60, -79.40, "CA", "08", 80000, "America/Toronto", "80"),
        geonames_row(11, "Far Rock", -16.50, 179.95, "FJ", "03", 2000, "Pacific/Fiji", "5"),
    ]
    (source / "cities1000.txt").write_text("\n".join(rows) + "\nbroken line\n", encoding="utf-8")
    (source / "admin1CodesASCII.txt").write_text("US.CO\tColorado\tColorado\t1\nUS.ME\tMaine\tMaine\t2\nGB.ENG\tEngland\tEngland\t3\nUS.OK\tOklahoma\tOklahoma\t4\n"
                                                 "US.MO\tMissouri\tMissouri\t5\nDE.07\tNorth Rhine-Westphalia\tNorth Rhine-Westphalia\t6\nIT.09\tLombardy\tLombardy\t7\n"
                                                 "CA.08\tOntario\tOntario\t8\nFJ.03\tNorthern\tNorthern\t9\n", encoding="utf-8")
    (source / "countryInfo.txt").write_text("# comment\nUS\tUSA\t840\tUS\tUnited States\nGB\tGBR\t826\tUK\tUnited Kingdom\nDE\tDEU\t276\tGM\tGermany\n"
                                            "IT\tITA\t380\tIT\tItaly\nCA\tCAN\t124\tCA\tCanada\nFJ\tFJI\t242\tFJ\tFiji\n", encoding="utf-8")
    assert gazetteer.build(source) == 11
    return gazetteer


def test_without_the_list_there_are_no_names_and_nothing_breaks():
    assert not gazetteer.available() and gazetteer.find("Elmford") == [] and gazetteer.nearest(39.5, -105) is None and gazetteer.count() == 0
    assert gazetteer.locality(39.5, -105) is None
    with pytest.raises(FileNotFoundError):
        gazetteer.build(Path("/nonexistent-geonames"))


def test_a_town_is_found_by_name_the_likeliest_first(towns):
    assert towns.available() and towns.count() == 11
    found = towns.find("Elmford")
    assert [place.label for place in found] == ["Elmford, Colorado", "Elmford, Maine", "Elmford, England, United Kingdom"]    # a village of the name is not offered
    assert (found[0].lat, found[0].lon, found[0].zone, found[0].elevation, found[0].population) == (39.5, -105.0, "America/Denver", 1600, 410000)
    for asked, label in (("Elmford, ME", "Elmford, Maine"), ("Elmford, Maine", "Elmford, Maine"), ("Elmford Maine", "Elmford, Maine"), ("Elmford OK", "Elmford, Oklahoma"),
                         ("Elmford, UK", "Elmford, England, United Kingdom"), ("Elmford, England", "Elmford, England, United Kingdom"),
                         ("elmford, united states", "Elmford, Colorado"), ("Elm Ford", "Elmford, Colorado"), ("ELF", "Elmford, Colorado"),
                         ("Saint Brannoc", "St. Brannoc, Missouri"), ("St Brannoc", "St. Brannoc, Missouri"), ("st. brannoc, mo", "St. Brannoc, Missouri"),
                         ("Kurzberg", "Kürzberg, North Rhine-Westphalia, Germany"), ("KÜRZBERG", "Kürzberg, North Rhine-Westphalia, Germany"),
                         ("Pinewater, ON", "Pinewater, Ontario, Canada"), ("Pinewater, Canada", "Pinewater, Ontario, Canada")):
        assert towns.find(asked)[0].label == label, asked
    # a big place known by another name comes before a hamlet that bears it
    assert [place.name for place in towns.find("Courtmont")] == ["Kürzberg", "Courtmont"]
    assert towns.find("Elmford, France") == [] and towns.find("Nowheresville") == [] and towns.find("") == [] and towns.find(", ,") == []


def test_a_position_is_given_the_name_a_person_would_use(towns):
    assert towns.nearest(39.52, -104.97).name == "Harrow Hill" and towns.nearest(39.505, -104.99).name == "Elmford"     # never a district
    assert towns.locality(39.521, -104.972).name == "Elmford"                    # in the suburb, but the city is what it is called
    assert towns.locality(44.21, -70.11).name == "Elmford" and towns.locality(44.21, -70.11).region == "Maine"
    assert towns.nearest(0.0, -30.0) is None and towns.nearest(0.0, -30.0, within_km=10) is None
    assert towns.nearest(-16.5, -179.99).name == "Far Rock"                      # across the date line
    assert towns.kilometres(39.5, -105.0, 44.2, -70.1) == pytest.approx(2915, abs=3)      # on a sphere: near enough for "nearest"


# ── where, home and the clock ──────────────────────────────────────────────

def test_a_place_is_coordinates_a_name_or_home(towns):
    by_name = places.resolve("Elmford")
    assert (by_name.label, by_name.zone, by_name.kind, by_name.elevation) == ("Elmford, Colorado", "America/Denver", "name", 1600)
    assert by_name.note == "Took that to be Elmford, Colorado; there is also Elmford, Maine; Elmford, England, United Kingdom."
    assert places.resolve("near Elmford, ME").label == "Elmford, Maine" and places.resolve("near Elmford, ME").note == ""
    by_position = places.resolve("39.5, -105.0")
    assert (by_position.label, by_position.zone, by_position.kind, by_position.elevation) == ("Elmford, Colorado", "America/Denver", "latlon", 1600)
    away = places.resolve("39.7, -105.0")
    assert away.label == "22 km N of Elmford, Colorado" and away.zone == "America/Denver" and away.elevation is None
    nowhere = places.resolve("0, -30")
    assert (nowhere.label, nowhere.zone) == ("0.0000, -30.0000", "")
    assert places.resolve("13S 500000 4372000").kind == "utm" and places.resolve("13SED0000072000").kind == "mgrs"
    for asked, said in (("Xyzzyville", "do not know a place called “Xyzzyville”"), ("91, 10", "Latitude runs"), ("13SZZ1234", "no 100 km square"),
                        ("home", "no home position is saved"), ("", "no home position is saved")):
        with pytest.raises(places.PlaceError, match=said):
            places.resolve(asked, owner="zed")


def test_without_the_list_a_name_says_what_is_missing():
    with pytest.raises(places.PlaceError, match="list of place names is not installed"):
        places.resolve("Elmford")
    assert places.resolve("39.5, -105.0").label == "39.5000, -105.0000" and places.resolve("39.5, -105.0").zone == ""


def test_home_is_each_persons_own(towns):
    assert places.home("zed") is None and places.forget_home("zed") is False
    kept = places.set_home("zed", "Elmford, ME")
    assert (kept.label, kept.zone, kept.kind) == ("Elmford, Maine", "America/New_York", "home")
    places.set_home("ada", "39.7, -105.0", zone="UTC")
    assert places.home("zed").label == "Elmford, Maine" and places.home("ada").zone == "UTC" and places.home("mia") is None
    for word in ("", "home", "Home.", "my house", "here", "at home", "where I live"):
        assert places.resolve(word, owner="zed").label == "Elmford, Maine", word
    assert places.resolve("Elmford", owner="zed").label == "Elmford, Colorado"   # a name is a name, whoever asks
    with pytest.raises(places.PlaceError, match="not a time zone"):
        places.set_home("zed", "Elmford", zone="Mars/Olympus")
    with pytest.raises(places.PlaceError):
        places.set_home("zed", "Xyzzyville")
    assert places.home("zed").label == "Elmford, Maine"                          # a refused change changes nothing
    assert places.forget_home("zed") is True and places.home("zed") is None and places.home("ada") is not None
    saved = json.loads(Path(__import__("routes.prefs_routes", fromlist=["PREFS_FILE"]).PREFS_FILE).read_text())
    assert saved["_users"]["ada"][places.HOME_KEY] == {"lat": 39.7, "lon": -105.0, "label": "22 km N of Elmford, Colorado", "zone": "UTC"}


def test_times_are_on_the_places_clock_unless_another_is_asked_for(towns):
    from src import user_time

    denver = places.resolve("Elmford")
    assert places.clock(denver)[1:] == ("America/Denver", "") and places.clock(denver)[0] == ZoneInfo("America/Denver")
    assert places.clock(denver, "America/Anchorage")[1] == "America/Anchorage" and places.clock(denver, "UTC")[0] is UTC
    assert places.clock(denver, "-07:00")[0].utcoffset(None) == timedelta(hours=-7) and places.clock(denver, "+5:30")[0].utcoffset(None) == timedelta(hours=5, minutes=30)
    with pytest.raises(places.PlaceError, match="not a time zone"):
        places.clock(denver, "Mars/Olympus")
    # a place with no known clock: the clock of the person asking, and a word about it when the place is far away
    user_time.set_user_tz_name("America/Chicago")
    user_time.set_user_tz_offset(-360)
    zone, label, note = places.clock(places.resolve("0, 100"))
    assert zone == ZoneInfo("America/Chicago") and label == "America/Chicago" and "keep a different one" in note
    assert places.clock(places.resolve("20, -95"))[2] == ""                      # out at sea, but on about the same clock
    user_time.set_user_tz_name(None)
    assert places.clock(places.resolve("20, -95"))[1] == "UTC-06:00"
    assert places.clock(places.resolve("36, -95"))[1] == "America/Chicago"       # 215 km from a listed town: its clock


def test_between_two_places():
    found = places.between(places.resolve("36.154, -95.9928"), places.resolve("37.6922, -97.3375"))
    assert found["km"] == pytest.approx(208.56, abs=0.05) and found["miles"] == pytest.approx(129.59, abs=0.05) and found["compass"] == "NW"
    assert found["bearing"] == pytest.approx(325.3, abs=0.1) and found["back_bearing"] == pytest.approx(144.5, abs=0.1) and found["back_compass"] == "SE"
    described = places.describe(places.resolve("40.748433, -73.985656"))
    assert described["mgrs"] == "18TWL8563211326" and described["utm"] == "18T 585632 4511326" and described["kind"] == "latlon"


# ── the assistant's tools ──────────────────────────────────────────────────

def tool(name, args, owner="zed"):
    from src.agent_tools import TOOL_HANDLERS

    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args) if isinstance(args, dict) else args, {"owner": owner}))


def test_the_convert_tool():
    done = tool("convert_units", {"amount": "5 ft 10 in", "to": "cm"})
    assert (done["output"], done["exit_code"]) == ("5 ft 10 in = 177.8 cm — feet and inches to centimetres", 0)
    assert done["card"]["value"] == "177.8 cm"                                   # the same answer as the figure a screen shows large
    assert tool("convert_units", "30 mpg to L/100km")["output"].startswith("30 mpg = 7.840486 L/100 km")
    assert tool("convert_units", {"amount": "5 nm", "to": "mi"})["output"].endswith("\nRead “nm” as nautical miles, not nanometres.")
    assert tool("convert_units", {"value": 5, "from": "ft", "to": "m"})["output"].startswith("5 ft = 1.524 m")
    assert tool("convert_units", {"amount": "3 florps", "to": "m"}) == {"error": "convert_units: I do not know the unit “florps”.", "exit_code": 1}
    assert tool("convert_units", {})["exit_code"] == 1 and tool("convert_units", "")["exit_code"] == 1


def test_the_map_tool(towns):
    one = tool("map_position", {"place": "18TWL8563211326"})["output"].split("\n")
    assert one == ["Position:", "Latitude, longitude: 40.748436, -73.985651  (40°44'54.4\"N 73°59'08.3\"W)", "UTM: 18T 585632 4511326", "MGRS: 18TWL8563211326"]
    named = tool("map_position", {"place": "Elmford, ME"})["output"].split("\n")
    assert named[0] == "Elmford, Maine" and named[-2:] == ["Height above sea level: about 40 m (131 ft)", "Time zone: America/New_York"]
    near = tool("map_position", {"place": "39.7, -105.0"})["output"].split("\n")          # every line says what it is: nothing to mistake for a height
    assert near[0] == "Position: 22 km N of Elmford, Colorado (measured from the middle of that town)" and near[-1] == "Time zone: America/Denver"
    assert not any(line.startswith("Height") for line in near)
    far = tool("map_position", {"from": "Elmford", "to": "Elmford, ME"})["output"]
    assert far.startswith("Elmford, Colorado to Elmford, Maine: 2,922.2 km (1,815.8 mi, 1,577.8 nautical miles) in a straight line.") and "Bearing to set out on:" in far and "Bearings are true" in far
    assert "there is also Elmford, Maine" in far                                  # which Elmford it took the first one to be
    onward = tool("map_position", {"from": "39.5, -105.0", "bearing": 45, "distance": "5 km"})["output"]
    assert onward.startswith("5 km (3.1069 mi, 2.6998 nautical miles) from Elmford, Colorado on a true bearing of 45.0° (NE; 800 mils) is:")
    assert "Latitude, longitude: 39.531" in onward
    assert "true bearing of 45.0°" in tool("map_position", {"from": "Elmford", "bearing": "800 mils", "distance": "2 mi"})["output"]
    for args, said in (({"place": "Xyzzyville"}, "do not know a place"), ({}, "takes JSON"), ({"from": "Elmford", "bearing": "north", "distance": "5 km"}, "bearing"),
                       ({"from": "Elmford", "bearing": 10, "distance": "5 kg"}, "distance"), ({"place": "home"}, "no home position")):
        answer = tool("map_position", args)
        assert answer["exit_code"] == 1 and said in answer["error"], args


def test_the_sun_and_moon_tool(towns):
    answer = tool("sun_moon", {"place": "39.7392, -104.9847", "date": "2026-11-23", "time": "9 pm"})["output"].split("\n")
    assert answer[0] == "Sun and moon at 27 km N of Elmford, Colorado — Monday, November 23, 2026 (times: America/Denver)"
    assert answer[1].startswith("Sunrise 6:54 AM · solar noon 11:46 AM (sun 30° up) · sunset 4:39 PM · daylight 9 h 4")
    assert answer[2].startswith("First light (civil dawn) 6:24 AM, last light (civil dusk) 5:08 PM; nautical twilight") and "fully dark before" in answer[2]
    assert answer[3] == ("Moon: waxing gibbous, 99% lit (growing), 14 days old. It sets at 5:54 AM (the end of the night before), "
                         "then rises at 3:40 PM and sets at 7:13 AM the next day.")
    assert answer[4].startswith("Coming: full moon Tue Nov 24 7:5") and "new moon Tue Dec 8 5:5" in answer[4]
    assert answer[5].startswith("At 9:00 PM: the sun is 50° below the horizon; the moon is 57° above the horizon, bearing 108.")
    polar = tool("sun_moon", {"place": "69.6489, 18.9551", "date": "2026-12-21", "zone": "Europe/Oslo"})["output"]
    assert "The sun does not rise this day (polar night)" in polar and "with twilight from 9:31 AM to 1:53 PM" in polar
    assert "The sun does not set this day (midnight sun)" in tool("sun_moon", {"place": "69.6489, 18.9551", "date": "2027-06-21", "zone": "Europe/Oslo"})["output"]
    summer = tool("sun_moon", {"place": "64.8378, -147.7164", "date": "June 21 2027", "zone": "America/Anchorage"})["output"]
    assert "The sun sets at 12:48 AM, rises again at 2:58 AM and is still up at midnight" in summer and "It does not get fully dark." in summer
    week = tool("sun_moon", {"place": "Elmford", "date": "2026-11-23", "days": 3})["output"].split("\n")
    assert len(week) == 7 and week[1].startswith("Date        | Sunrise") and week[2].startswith("Mon Nov 23  | 6:5") and week[4].startswith("Wed Nov 25")
    assert "there is also Elmford, Maine" in week[-1]
    for args, said in (({}, "no home position is saved"), ({"place": "Elmford", "date": "smarch"}, "as a date"), ({"place": "Elmford", "time": "25:00"}, "not a time of day"),
                       ({"place": "Elmford", "zone": "Mars/Olympus"}, "not a time zone"), ({"place": "Elmford", "date": "1700-01-01"}, "1900 to 2100")):
        answer = tool("sun_moon", args)
        assert answer["exit_code"] == 1 and said in answer["error"], args
    places.set_home("zed", "Elmford, ME")
    assert tool("sun_moon", {"date": "2026-11-23"})["output"].startswith("Sun and moon at Elmford, Maine — Monday, November 23, 2026 (times: America/New_York)")
    assert tool("sun_moon", {"date": "2026-11-23"}, owner="ada")["exit_code"] == 1  # one person's home is not another's


def test_the_date_tool(monkeypatch):
    monkeypatch.setattr("src.user_time.now_user_local", lambda now_utc=None: datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    assert tool("date_math", {"date": "today", "to": "2027-03-12"})["output"].startswith("From Monday, November 23, 2026 to Friday, March 12, 2027: 109 days")
    assert tool("date_math", {"date": "today", "add": "90 days"})["output"] == ("Monday, November 23, 2026 moved by 90 days: Sunday, February 21, 2027 — day 52 of 365, "
                                                                                "week 7 of 2027 (ISO); 90 days from today.")
    assert tool("date_math", {"add": "10 business days"})["output"].endswith("Counted Monday to Friday; weekends are skipped, public holidays are not.")
    assert tool("date_math", {"date": "2027-01-31", "add": "1 month"})["output"].split("\n")[1] == "February is shorter, so this lands on its last day."
    assert "Monday, November 9, 2026" in tool("date_math", {"date": "today", "subtract": "2 weeks"})["output"]
    assert tool("date_math", {"date": "July 4 1976"})["output"].startswith("Sunday, July 4, 1976 — day 186 of 366")
    assert tool("date_math", "tomorrow")["output"].startswith("Tuesday, November 24, 2026")
    assert tool("date_math", {"date": "smarch 3"}) == {"error": "date_math: I could not read “smarch 3” as a date. Write it like 2027-03-12, March 12 2027, "
                                                                 "today or next Friday.", "exit_code": 1}
    assert tool("date_math", {})["exit_code"] == 1


def test_the_tools_are_wired_in_everywhere_they_must_be():
    from src import tool_policy, tool_security
    from src.agent_loop import _CHANGE_TOOLS
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import ToolIndex
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    names = ("convert_units", "map_position", "sun_moon", "date_math")
    schemas = {entry["function"]["name"]: entry["function"] for entry in FUNCTION_TOOL_SCHEMAS}
    loop = (REPO / "src/agent_loop.py").read_text(encoding="utf-8")
    policy = open(tool_policy.__file__, encoding="utf-8").read()
    from src.agent_loop import _domain_rules_for_tools
    for name in names:
        assert any("Do not work these out yourself" in rule for rule in _domain_rules_for_tools({name})), name     # told to ask, not to reckon
        assert name in TOOL_HANDLERS and name in TOOL_TAGS and name not in _CHANGE_TOOLS, name
        assert name in tool_security.PLAN_MODE_READONLY_TOOLS and name not in tool_security.NON_ADMIN_BLOCKED_TOOLS, name   # for everyone, changes nothing
        assert schemas[name]["parameters"]["type"] == "object" and f'"{name}"' in policy and f'    "{name}": """' in loop, name
    assert schemas["convert_units"]["parameters"]["required"] == ["amount"] and "required" not in schemas["sun_moon"]["parameters"]
    index = ToolIndex.__new__(ToolIndex)
    index.retrieve = lambda query, k=8: []
    for said, name in (("how many ounces in a cup", "convert_units"), ("what is 72 F in celsius", "convert_units"), ("what's 20 psi in bar", "convert_units"),
                       ("what is the MGRS for 40.7, -73.9", "map_position"), ("how far is it from Tulsa to Wichita", "map_position"),
                       ("when does the sun set tonight", "sun_moon"), ("when does it get dark in Anchorage", "sun_moon"), ("is there a full moon this week", "sun_moon"),
                       ("how many days until Christmas", "date_math"), ("what day of the week was July 4 1976", "date_math"), ("what is 90 days from today", "date_math")):
        assert name in index.get_tools_for_query(said, always_include=set()), said
    for said in ("tell me a joke", "write a poem about the dawn", "what is the weather like", "she coordinates the team", "bearing in mind what you said"):
        assert not set(names) & set(index.get_tools_for_query(said, always_include=set())), said

    served = (REPO / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/almanac")' in served and "setup_almanac_routes()" in served
    for name in ("static/almanac.html", "static/lite/almanac.js", "static/lite/almanac.css", "scripts/aegis-almanac", "docs/almanac.md"):
        assert (REPO / name).is_file(), name
    assert 'id="tool-almanac-link" href="/almanac"' in (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "location.href = '/almanac'" in (REPO / "static/lite/home.js").read_text(encoding="utf-8")
    assert "\n  almanac: {" in (REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    assert " sun: '" in (REPO / "static/lite/lite.js").read_text(encoding="utf-8")
    # "Toolboxes" are another feature (tool sets summoned with /toolbox <name>): this page must not take their command or address
    assert "\n  toolbox: {" not in (REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    assert '"/toolbox"' not in served and "/api/toolbox" not in (REPO / "routes/almanac_routes.py").read_text(encoding="utf-8")


# ── the page's routes ──────────────────────────────────────────────────────

@pytest.fixture
def client(towns, monkeypatch):
    import routes.almanac_routes as routes

    who = {"user": "zed"}

    def fake_user(request):
        if not who["user"]:
            raise HTTPException(401, "Sign in")
        return who["user"]

    monkeypatch.setattr(routes, "require_user", fake_user)
    app = FastAPI()
    app.include_router(routes.setup_almanac_routes())
    test_client = TestClient(app)
    test_client.who = who
    return test_client


def test_the_page_gets_what_it_needs_to_start(client):
    start = client.get("/api/almanac").json()
    assert start["places"] == 11 and start["home"] is None and {"length", "volume", "temperature"} <= {entry["kind"] for entry in start["units"]}
    saved = client.put("/api/almanac/home", json={"place": "Elmford, ME"}).json()["home"]
    assert saved["label"] == "Elmford, Maine" and saved["zone"] == "America/New_York" and saved["mgrs"].startswith("19T")
    assert client.get("/api/almanac").json()["home"]["label"] == "Elmford, Maine"
    client.who["user"] = "ada"
    assert client.get("/api/almanac").json()["home"] is None                     # hers, not his
    client.who["user"] = "zed"
    assert client.put("/api/almanac/home", json={"place": "Xyzzyville"}).status_code == 400
    assert client.delete("/api/almanac/home").json() == {"removed": True} and client.delete("/api/almanac/home").json() == {"removed": False}


def test_asking_through_the_api(client):
    done = client.post("/api/almanac/convert", json={"amount": "5 ft 10 in", "to": "cm"}).json()
    assert done["answer"] == "177.8 cm" and done["kind"] == "length" and {"symbol": "cm", "name": "centimetres"} in done["choices"]
    assert client.post("/api/almanac/convert", json={"amount": "2 cups"}).json()["also"][0]["text"] == "473.1765 mL"
    refused = client.post("/api/almanac/convert", json={"amount": "3 florps", "to": "m"})
    assert refused.status_code == 400 and refused.json()["detail"] == "I do not know the unit “florps”."
    place = client.post("/api/almanac/place", json={"place": "Elmford"}).json()
    assert place["text"].startswith("Elmford, Colorado\n") and place["place"]["mgrs"].startswith("13S") and place["place"]["elevation_m"] == 1600
    assert "2,922.2 km" in client.post("/api/almanac/place", json={"from": "Elmford", "to": "Elmford, ME"}).json()["text"]
    assert client.post("/api/almanac/place", json={"place": "Xyzzyville"}).json()["detail"].startswith("I do not know a place called")
    sky_answer = client.post("/api/almanac/sky", json={"place": "Elmford", "date": "2026-11-23"}).json()
    assert sky_answer["text"].startswith("Sun and moon at Elmford, Colorado — Monday, November 23, 2026 (times: America/Denver)\nSunrise 6:5")
    day = sky_answer["summary"]                                                  # what the page shows large
    assert (day["label"], day["date"], day["zone"], day["state"], day["south"]) == ("Elmford, Colorado", "Monday, November 23, 2026", "America/Denver", "", False)
    assert day["sunrise"].endswith("AM") and day["sunset"].endswith("PM") and day["first_light"] and day["last_light"] and day["daylight"].startswith("9 h 4")
    assert (day["moon"]["name"], day["moon"]["lit"], day["moon"]["waxing"], day["moon"]["set_first"]) == ("Waxing gibbous", 99, True, True)
    assert day["moon"]["until"].endswith("AM") and day["moon"]["rise"].endswith("PM")       # tonight's moon sets tomorrow morning
    assert [item["name"] for item in day["moon"]["next"]] == ["Full moon", "Last quarter", "New moon", "First quarter"]
    assert "there is also Elmford, Maine" in day["notes"][0]
    week = client.post("/api/almanac/sky", json={"place": "Elmford", "date": "2026-11-23", "days": 3}).json()
    assert week["summary"] is None and week["text"].count("\n") == 6
    polar = client.post("/api/almanac/sky", json={"place": "69.6489, 18.9551", "date": "2026-12-21", "zone": "Europe/Oslo"}).json()["summary"]
    assert (polar["state"], polar["sunrise"], polar["sunset"], polar["first_light"], polar["last_light"]) == ("down", None, None, "9:31 AM", "1:53 PM")
    assert client.post("/api/almanac/sky", json={"date": "2026-11-23"}).json()["detail"].startswith("No place was given and no home position is saved")
    assert "109 days" in client.post("/api/almanac/dates", json={"date": "2026-11-23", "to": "2027-03-12"}).json()["text"]
    assert client.post("/api/almanac/dates", json={"date": "smarch"}).status_code == 400
    assert client.post("/api/almanac/convert", content="[1]", headers={"Content-Type": "application/json"}).status_code in (400, 422)


def test_the_routes_need_a_signed_in_person(client):
    client.who["user"] = ""
    assert client.get("/api/almanac").status_code == 401 and client.delete("/api/almanac/home").status_code == 401
    for path, body in (("/api/almanac/convert", {"amount": "1 m"}), ("/api/almanac/place", {"place": "0, 0"}), ("/api/almanac/sky", {"place": "0, 0"}),
                       ("/api/almanac/dates", {"date": "today"})):
        assert client.post(path, json=body).status_code == 401, path
    assert client.put("/api/almanac/home", json={"place": "0, 0"}).status_code == 401
