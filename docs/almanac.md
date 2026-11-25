# Almanac

Exact answers, worked out on your own machine: unit conversion, map
positions, the sun and the moon, and calendar arithmetic. No model is asked
and nothing is looked up on the internet — it is arithmetic on definitions and
published formulas, so the answer is the same every time and takes
milliseconds.

A language model is good at knowing *that* a mile is about 1.6 km and bad at
multiplying 137.4 by 1.609344, counting the days to a date, or working a grid
reference. So the assistant does not do these in its head: it calls the
almanac, and so can you.

* **The page** — `/almanac` (in the sidebar, in the simple home screen's
  menu, and `/almanac` as a command in a chat).
* **In a chat** — *"how many cups in 2 litres?"*, *"what is 18TWL8563211326
  in latitude and longitude?"*, *"how far is it from Tulsa to Wichita?"*,
  *"when does it get dark tomorrow?"*, *"what is 90 days from today?"*
* **In a terminal** — `scripts/aegis-almanac` (see the end of this page).

## Convert

`5 ft 10 in` → `177.8 cm`. About 250 units of length, area, volume, weight,
time, temperature, speed, force, pressure, energy and torque, power, flow,
density, data, angle, fuel economy and radiation.

| Write | To get |
|---|---|
| `5 ft 10 in` → `cm` | compound amounts: `2 lb 3 oz`, `6'2"`, `3 1/2 in`, `1½ cups` |
| `177.8 cm` → `ft in` | an answer in two units: `5 ft 10 in`, `lb oz`, `h min` |
| `40 lb/acre` → `kg/ha` | units built from others: `gal/min`, `lb/ft3`, `BTU/h`, `kWh/day` |
| `30 mpg` → `L/100km` | fuel economy, which runs upside down between the two systems |
| `2 cups` (no unit asked) | the usual units of the other system |

Every unit is a factor on the SI unit of its kind taken from its definition
(an inch is 25.4 mm, a pound 0.45359237 kg, a US gallon 231 cubic inches), and
the answer names the units in full so a misreading shows:
`8 oz = 236.5882 mL — US fluid ounces to millilitres`.

Where conversions usually go wrong:

* Gallons, quarts, pints, cups and fluid ounces are the **US** ones unless
  written `imp gal`, `UK pint`. A `ton` is the US short ton (2,000 lb); `t` or
  `tonne` is 1,000 kg; `long ton` is 2,240 lb.
* `cal` is the small calorie. The calorie on a food label is `kcal`.
* `nm` is nanometres, `nmi` nautical miles and `Nm` newton-metres — though
  `5 nm` → `mi` is taken as nautical miles, and says so.
* `mil` is a thousandth of an inch, or beside an angle the NATO mil (6,400 to
  a circle). With nothing to go on it says which it took.
* `MB` and `GB` are powers of ten; `MiB` and `GiB` powers of two.
* `oz` is weight, unless the other unit is a volume.

## Where

A place in every notation, and the distance and direction between two.

```
Position: 4 km NNE of New York City, New York (measured from the middle of that town)
Latitude, longitude: 40.748436, -73.985651  (40°44'54.4"N 73°59'08.3"W)
UTM: 18T 585632 4511326
MGRS: 18TWL8563211326
Time zone: America/New_York
```

A place can be written any of these ways:

| | |
|---|---|
| `40.7484, -73.9857` | decimal degrees (north and east positive) |
| `40.7484 N 73.9857 W`, `N40.7484 W73.9857` | with letters, before or after |
| `40°44'54"N 73°59'08"W`, `40 44 54 N 73 59 8 W`, `40-44-54N 073-59-08W` | degrees, minutes, seconds |
| `40 44.905 N 73 59.139 W` | degrees and decimal minutes |
| `18T 585632 4511326` | UTM: zone and band, easting, northing |
| `18TWL8563211326`, `18T WL 8563 1132` | MGRS, any even number of digits |
| `Tulsa, OK`, `Paris, France`, `Springfield, Missouri` | a town (needs the place list, below) |
| `home` — or nothing | the position you saved as home |

Two places give the straight-line distance (kilometres, miles, nautical
miles) and the bearing to set out on and to come back on, in degrees, compass
points and mils. A place with a bearing and a distance gives where that leads.

* **Bearings are true** — from the pole, not from a compass needle. Apply the
  local declination (it is printed on topographic maps) before walking one.
* **Distance is as the crow flies**, over the Earth's surface. A road or a
  trail is longer.
* A grid reference names a **square**, not a point: ten digits a 1 m square,
  eight a 10 m square, six a 100 m square. Writing one, the numbers are cut
  short, never rounded, so the point is always inside the square named;
  reading one, the answer is the middle of the square.
* `18S …` can mean latitude band S or "zone 18, southern hemisphere"; the
  northing settles it, and the answer says how it was read.

### Town names

Names come from GeoNames' `cities1000` list: every populated place of a
thousand people or more — about 172,000 — with its position, region,
population, elevation and time zone. It is an 11 MB download, once:

```bash
scripts/aegis-almanac places fetch     # downloads three files and builds <data>/almanac/places.db (about 43 MB)
```

Without it everything else works; a place just has to be given as
coordinates. With it, a position also gets a name (`22 km N of …`) and — more
usefully — its **time zone**, so sunrise at a place is given on that place's
clock. The largest place of a name comes first (`Springfield` is the one in
Missouri, and the answer says which others there are); add the state or
country to choose another.

*Place data © GeoNames (https://www.geonames.org), licensed CC BY 4.0.*

### Home

Look a place up on the page and press **Save as my home**. From then on
"when does the sun set?" needs no place. Each person has their own; it is
kept with their preferences on this server and goes nowhere else. A person
who has saved none uses the household's.

## Weather and the household

An administrator sets three things once, for everyone, under **People and
access → Household**: the **home town** (so nobody has to say where they
are), the **units** people read (US or metric), and the **language** answers
come in. They are given to the assistant with every message and used by every
tool that needs a place, a unit or a language.

The same tab has the switches for **live sources** — services on the internet
that are asked for something current. Weather is the first:

> *Will it rain tomorrow?* · *What's the weather this weekend?* · *Do I need a
> coat tonight?* · *Weather in Fernhollow on Friday*

The forecast comes from Open-Meteo (worldwide, no account or key), with the US
National Weather Service as a second source and for its warnings. Only a
position rounded to about a kilometre is sent, with a generic client name. A
forecast is kept for twenty minutes; with the internet down the last one
fetched is offered for up to twelve hours and is marked as old — and beyond
that the answer is "no forecast: offline", never a guess. Switch weather off
and nothing is fetched at all.

The assistant's `weather` tool takes `place` (home when left out) and `when`
(*now*, *today*, *tonight*, *tomorrow*, a weekday, a date, *weekend*, *week*).
`POST /api/almanac/weather` is the same for a program.

## Sun and moon

```
Sun and moon at Denver, Colorado — Monday, November 23, 2026 (times: America/Denver)
Sunrise 6:54 AM · solar noon 11:46 AM (sun 30° up) · sunset 4:39 PM · daylight 9 h 45 min
First light (civil dawn) 6:24 AM, last light (civil dusk) 5:08 PM; nautical twilight 5:51 AM to 5:41 PM; fully dark before 5:19 AM and after 6:14 PM.
Moon: waxing gibbous, 99% lit (growing), 14 days old. Sets 5:54 AM, rises 3:40 PM.
Coming: full moon Tue Nov 24 7:54 AM; last quarter Mon Nov 30 11:09 PM; new moon Tue Dec 8 5:52 PM; first quarter Wed Dec 16 10:43 PM.
```

For any place and any day from 1900 to 2100, a week at a time if wanted, and
— given a time of day — where the sun and the moon are in the sky.

* **First and last light** are civil twilight: the sun 6° below the horizon,
  about when you can no longer work outdoors without a lamp. Nautical
  twilight (12°) is when the horizon at sea disappears; after astronomical
  twilight (18°) the sky is fully dark.
* Far enough north or south a day can have no sunrise or no sunset. The
  answer says so, and still gives the twilight there is.
* Times are on the **place's own clock** when the place list knows it,
  otherwise on yours, with a note when the place is far away.
* They are for a level horizon at sea level and are good to a minute or two.
  The last minute depends on the air that day and on the hills around you,
  which no formula knows.

## Dates

| Ask | Answer |
|---|---|
| `today` to `March 12 2027` | 109 days — 15 weeks and 4 days; 3 months, 17 days; 79 weekdays |
| `today` + `90 days` | Sunday, February 21, 2027 |
| `January 31` + `1 month` | February 28 — "February is shorter, so this lands on its last day" |
| `today` + `10 business days` | skips weekends (not public holidays, and says so) |
| `July 4 1976` | Sunday — day 186 of 366, week 27 |

Dates are read as `2027-03-12`, `March 12, 2027`, `12 March 2027`,
`3/12/2027` (**month first**, as in the United States; `12.3.2027` with dots
is day first), `today`, `tomorrow`, `next Friday`. A date without a year is
this year.

## How it was checked

Each piece was compared with an independent, established implementation, and
the reference numbers are kept in `tests/test_almanac.py` so the comparison
is repeated on every test run without those packages being installed.

| Piece | Compared with | Agreement |
|---|---|---|
| UTM | the `utm` package, 4,000 random points incl. Norway and Svalbard | under 1 mm |
| MGRS | the `mgrs` package (GeoTrans), 20,000 points | identical, except a handful within 1 cm of a square's edge |
| Distance, bearing | Vincenty's published survey line; 2,000 legs out and back | 0.001 m; 0.00001° |
| Sunrise, sunset, twilight | PyEphem, 780 place-days from 78°S to 78°N | within 15 s |
| Moonrise, moonset | PyEphem, the same days | within 12 s |
| Sun and moon in the sky | PyEphem, 3,000 moments | within 0.02° |
| New, quarter and full moons | PyEphem, 240 of them (2026–2029) | within 90 s |
| Units | the defining constants (NIST SP 811, and the 1959 yard and pound agreement) | exact |

The sun follows the low-accuracy solar theory in Meeus, *Astronomical
Algorithms*; the moon uses the fifty largest terms of his lunar series; UTM
uses the Krüger series; distance uses Vincenty's formulas on the WGS84
ellipsoid.

## For the assistant and for programs

Four tools, open to every account because none of them changes anything:

```
convert_units  {"amount": "5 ft 10 in", "to": "cm"}
map_position   {"place": "18TWL8563211326"}  |  {"from": "Tulsa, OK", "to": "Wichita, KS"}  |  {"from": …, "bearing": 45, "distance": "5 km"}
sun_moon       {"place": "Denver", "date": "tomorrow", "time": "9 pm", "days": 7, "zone": "UTC"}     (all optional)
date_math      {"date": "today", "to": "March 12 2027"}  |  {"date": "today", "add": "90 days"}  |  {"date": "July 4 1976"}
```

```
GET    /api/almanac           unit menus, whether place names are installed, your home position
POST   /api/almanac/convert   {"amount", "to"?}
POST   /api/almanac/place     {"place"} | {"from", "to"} | {"from", "bearing", "distance"}
POST   /api/almanac/sky       {"place"?, "date"?, "time"?, "days"?, "zone"?}
POST   /api/almanac/dates     {"date", "to"?, "add"?}
PUT    /api/almanac/home      {"place", "zone"?}
DELETE /api/almanac/home
```

```bash
scripts/aegis-almanac convert "5 ft 10 in" cm
scripts/aegis-almanac where "Tulsa, OK" "Wichita, KS"
scripts/aegis-almanac onward "Denver" 45 "5 km"
scripts/aegis-almanac sky "Denver" --date tomorrow --days 7
scripts/aegis-almanac date today --add "90 days"
scripts/aegis-almanac places find "Springfield, MO"
```

## Limits worth knowing

* No magnetic declination, no elevation for an arbitrary point (only the
  listed height of a town), no routes along roads. Those need map data; they
  are on the [roadmap](../ROADMAP.md).
* The calendar is the Gregorian one throughout, including for dates before
  it was adopted.
* A "month" as a length of time (in `convert`) is a twelfth of a 365.25-day
  year; in `date_math` a month is a calendar month.
* Tides, weather and anything else that needs observations are not here:
  this is arithmetic only.
