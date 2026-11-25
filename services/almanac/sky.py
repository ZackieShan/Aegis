"""The sun and the moon: rising and setting, twilight, where they are in
the sky, and the moon's phase — worked out from the date and the place,
with no table to download and no network.

The sun's position follows Meeus's low-accuracy formulas (good to about
0.01°); the moon's uses the largest terms of his lunar series (good to
about 0.02°). Rising and setting are found by following the altitude
through the day, so they are right at high latitudes too, where a day can
have no sunrise at all. Expect times within a minute or two of an almanac:
the last minute depends on the air (refraction) and on the height of the
real horizon, which no formula knows.

Times are worked in UTC and handed back in the time zone asked for.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Callable, Dict, List, Optional, Tuple

RAD = math.pi / 180
SUN_RISE = -0.833            # the sun's centre when its top edge touches the horizon, through average air
TWILIGHT = {"civil": -6.0, "nautical": -12.0, "astronomical": -18.0}
DELTA_T = 69.2               # seconds between the clocks' time and the uniform time the theories use (2020s)
PHASES = ("New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon", "Waning gibbous", "Last quarter", "Waning crescent")
_J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)


def _centuries(when: datetime) -> float:
    """Julian centuries since 2000 January 1.5, in the uniform time of the theories."""
    return ((when - _J2000).total_seconds() + DELTA_T) / 86400 / 36525


def _days(when: datetime) -> float:
    return (when - _J2000).total_seconds() / 86400


def _sin(degrees: float) -> float:
    return math.sin(degrees * RAD)


def _cos(degrees: float) -> float:
    return math.cos(degrees * RAD)


def _obliquity(t: float) -> float:
    return 23.4392911 - 0.0130042 * t - 1.64e-7 * t * t + 5.04e-7 * t ** 3


# ── where they are among the stars ─────────────────────────────────────────

def sun_ecliptic(when: datetime) -> Tuple[float, float]:
    """The sun's apparent ecliptic longitude (degrees) and its distance (astronomical units)."""
    t = _centuries(when)
    mean = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    anomaly = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    eccentricity = 0.016708634 - 0.000042037 * t - 0.0000001267 * t * t
    centre = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * _sin(anomaly) + (0.019993 - 0.000101 * t) * _sin(2 * anomaly)
              + 0.000289 * _sin(3 * anomaly))
    true = mean + centre
    distance = 1.000001018 * (1 - eccentricity ** 2) / (1 + eccentricity * _cos(anomaly + centre))
    node = 125.04 - 1934.136 * t
    return (true - 0.00569 - 0.00478 * _sin(node)) % 360, distance


# Meeus, Astronomical Algorithms, table 47.A/B: multiples of D, M, M′, F; then the coefficient
# (longitude in millionths of a degree with the sine; distance in metres with the cosine; latitude likewise).
_MOON_LONGITUDE = (
    (0, 0, 1, 0, 6288774, -20905355), (2, 0, -1, 0, 1274027, -3699111), (2, 0, 0, 0, 658314, -2955968), (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888), (0, 0, 0, 2, -114332, -3149), (2, 0, -2, 0, 58793, 246158), (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733), (2, -1, 0, 0, 45758, -204586), (0, 1, -1, 0, -40923, -129620), (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755), (2, 0, 0, -2, 15327, 10321), (0, 0, 1, 2, -12528, 0), (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782), (0, 0, 3, 0, 10034, -23210), (4, 0, -2, 0, 8548, -21636), (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824), (1, 0, -1, 0, -5163, -8379), (1, 1, 0, 0, 4987, -16675), (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445), (4, 0, 0, 0, 3861, -11650), (2, 0, -3, 0, 3665, 14403), (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0), (2, -1, -2, 0, 2390, 10056), (1, 0, 1, 0, -2348, 6322), (2, -2, 0, 0, 2236, -9884),
    (0, 1, 2, 0, -2120, 5751), (0, 2, 0, 0, -2069, 0), (2, -2, -1, 0, 2048, -4950), (2, 0, 1, -2, -1773, 4130),
    (2, 0, 0, 2, -1595, 0), (4, -1, -1, 0, 1215, -3958), (0, 0, 2, 2, -1110, 0), (3, 0, -1, 0, -892, 3258),
    (2, 1, 1, 0, -810, 2616), (4, -1, -2, 0, 759, -1897), (0, 2, -1, 0, -713, -2117), (2, 2, -1, 0, -700, 2354),
    (2, 1, -2, 0, 691, 0), (2, -1, 0, -2, 596, 0), (4, 0, 1, 0, 549, -1423), (0, 0, 4, 0, 537, -1117),
    (4, -1, 0, 0, 520, -1571), (1, 0, -2, 0, -487, -1739),
)
_MOON_LATITUDE = (
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693), (2, 0, 0, -1, 173237), (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198), (2, 0, 1, -1, 9266), (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359), (2, -1, -1, 1, 2463), (2, -1, 0, 1, 2211), (2, -1, -1, -1, 2065), (0, 1, -1, -1, -1870),
    (4, 0, -1, -1, 1828), (0, 1, 0, 1, -1794), (0, 0, 0, 3, -1749), (0, 1, -1, 1, -1565), (1, 0, 0, 1, -1491), (0, 1, 1, 1, -1475),
    (0, 1, 1, -1, -1410), (0, 1, 0, -1, -1344), (1, 0, 0, -1, -1335), (0, 0, 3, 1, 1107), (4, 0, 0, -1, 1021), (4, 0, -1, 1, 833),
)


def moon_ecliptic(when: datetime) -> Tuple[float, float, float]:
    """The moon's ecliptic longitude and latitude (degrees) and its distance (kilometres), seen from the Earth's centre."""
    t = _centuries(when)
    mean = 218.3164477 + 481267.88123421 * t - 0.0015786 * t * t + t ** 3 / 538841
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t * t + t ** 3 / 545868
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t * t
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t * t + t ** 3 / 69699
    f = 93.2720950 + 483202.0175233 * t - 0.0036539 * t * t - t ** 3 / 3526000
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    longitude = distance = latitude = 0.0
    for kd, km, kmp, kf, sine, cosine in _MOON_LONGITUDE:
        angle = (kd * d + km * m + kmp * mp + kf * f) * RAD
        fade = e ** abs(km)
        longitude += sine * fade * math.sin(angle)
        distance += cosine * fade * math.cos(angle)
    for kd, km, kmp, kf, sine in _MOON_LATITUDE:
        latitude += sine * e ** abs(km) * math.sin((kd * d + km * m + kmp * mp + kf * f) * RAD)
    a1, a2, a3 = 119.75 + 131.849 * t, 53.09 + 479264.290 * t, 313.45 + 481266.484 * t
    longitude += 3958 * _sin(a1) + 1962 * _sin(mean - f) + 318 * _sin(a2)
    latitude += (-2235 * _sin(mean) + 382 * _sin(a3) + 175 * _sin(a1 - f) + 175 * _sin(a1 + f) + 127 * _sin(mean - mp) - 115 * _sin(mean + mp))
    node = 125.04 - 1934.136 * t
    return (mean + longitude / 1e6 - 0.00478 * _sin(node)) % 360, latitude / 1e6, 385000.56 + distance / 1000


def _equatorial(longitude: float, latitude: float, t: float) -> Tuple[float, float]:
    """Right ascension and declination (degrees) of a point of the ecliptic."""
    tilt = _obliquity(t) + 0.00256 * _cos(125.04 - 1934.136 * t)
    ra = math.atan2(_sin(longitude) * _cos(tilt) - math.tan(latitude * RAD) * _sin(tilt), _cos(longitude)) / RAD
    dec = math.asin(_sin(latitude) * _cos(tilt) + _cos(latitude) * _sin(tilt) * _sin(longitude)) / RAD
    return ra % 360, dec


def _sidereal(when: datetime) -> float:
    """Greenwich sidereal time, in degrees."""
    days = _days(when)
    t = days / 36525
    return (280.46061837 + 360.98564736629 * days + 0.000387933 * t * t) % 360


def _horizon(ra: float, dec: float, when: datetime, lat: float, lon: float) -> Tuple[float, float]:
    """Compass bearing and height above the horizon (degrees), with no air and from the Earth's centre."""
    hour = (_sidereal(when) + lon - ra) * RAD
    height = math.asin(_sin(lat) * _sin(dec) + _cos(lat) * _cos(dec) * math.cos(hour))
    bearing = math.atan2(math.sin(hour), math.cos(hour) * _sin(lat) - math.tan(dec * RAD) * _cos(lat)) / RAD + 180
    return bearing % 360, height / RAD


def refraction(height: float) -> float:
    """How much the air lifts something seen at ``height`` degrees (Bennett's formula), in degrees."""
    if height < -1.0:
        return 0.0
    return 1.02 / math.tan((height + 10.3 / (height + 5.11)) * RAD) / 60


def sun_position(when: datetime, lat: float, lon: float) -> Dict[str, float]:
    """Where the sun is: ``bearing`` (true, from north) and ``height`` above the horizon as seen (degrees)."""
    longitude, _distance = sun_ecliptic(when)
    ra, dec = _equatorial(longitude, 0.0, _centuries(when))
    bearing, height = _horizon(ra, dec, when, lat, lon)
    return {"bearing": bearing, "height": height + refraction(height), "true_height": height}


def moon_position(when: datetime, lat: float, lon: float) -> Dict[str, float]:
    """Where the moon is, as seen from the place (its nearness lowers it by up to a degree)."""
    longitude, latitude, distance = moon_ecliptic(when)
    ra, dec = _equatorial(longitude, latitude, _centuries(when))
    bearing, height = _horizon(ra, dec, when, lat, lon)
    parallax = math.asin(6378.14 / distance) / RAD
    seen = height - parallax * _cos(height)
    return {"bearing": bearing, "height": seen + refraction(seen), "true_height": height, "parallax": parallax, "distance_km": distance}


# ── the moon's face ────────────────────────────────────────────────────────

def moon_elongation(when: datetime) -> float:
    """How far round from the sun the moon is, 0–360°: 0 new, 90 first quarter, 180 full, 270 last quarter."""
    return (moon_ecliptic(when)[0] - sun_ecliptic(when)[0]) % 360


def moon_lit(when: datetime) -> float:
    """The fraction of the moon's face that is lit, 0–1."""
    moon_lon, moon_lat, moon_distance = moon_ecliptic(when)
    sun_lon, sun_distance = sun_ecliptic(when)
    apart = math.acos(max(-1.0, min(1.0, _cos(moon_lat) * _cos(moon_lon - sun_lon))))
    sun_km = sun_distance * 149_597_870.7
    phase = math.atan2(sun_km * math.sin(apart), moon_distance - sun_km * math.cos(apart))
    return (1 + math.cos(phase)) / 2


def phase_name(elongation: float, lit: float) -> str:
    """The everyday name. The four named moments are given a day's width; between them it is a crescent or gibbous."""
    for centre, name in ((0, PHASES[0]), (90, PHASES[2]), (180, PHASES[4]), (270, PHASES[6])):
        if abs((elongation - centre + 180) % 360 - 180) < 6.1:           # the moon gains 12.2° on the sun a day
            return name
    return PHASES[1 + 2 * int(elongation // 90)]


def _crossing(function: Callable[[datetime], float], start: datetime, end: datetime, seconds: float = 20.0) -> datetime:
    """The moment between ``start`` and ``end`` at which ``function`` passes through zero (it has opposite signs at the two)."""
    low, high = start, end
    low_value = function(low)
    while (high - low).total_seconds() > seconds:
        middle = low + (high - low) / 2
        value = function(middle)
        if (value < 0) == (low_value < 0):
            low, low_value = middle, value
        else:
            high = middle
    return low + (high - low) / 2


def next_phase(after: datetime, angle: float) -> datetime:
    """The next time the moon is ``angle`` degrees round from the sun (0 new, 90, 180 full, 270)."""
    def offset(when: datetime) -> float:
        return (moon_elongation(when) - angle + 180) % 360 - 180

    step = timedelta(hours=6)
    at = after
    before = offset(at)
    for _ in range(140):                                                 # 35 days: more than a month of the moon
        following = at + step
        value = offset(following)
        if before < 0 <= value and value - before < 180:
            return _crossing(offset, at, following)
        at, before = following, value
    raise ValueError("no phase found in 35 days")


def moon_phase(when: datetime) -> Dict[str, object]:
    """The moon's face now and the next four named moments."""
    elongation = moon_elongation(when)
    lit = moon_lit(when)
    last_new = next_phase(when - timedelta(days=30), 0.0)
    if last_new > when:
        last_new = next_phase(when - timedelta(days=60), 0.0)
    following = next_phase(last_new + timedelta(days=1), 0.0)
    if following <= when:
        last_new = following
    coming = sorted(((next_phase(when, angle), name) for angle, name in ((0.0, "New moon"), (90.0, "First quarter"), (180.0, "Full moon"),
                                                                           (270.0, "Last quarter"))), key=lambda item: item[0])
    return {"name": phase_name(elongation, lit), "lit": lit, "waxing": elongation < 180, "elongation": elongation,
            "age_days": (when - last_new).total_seconds() / 86400, "next": [{"name": name, "at": moment} for moment, name in coming]}


# ── rising and setting ─────────────────────────────────────────────────────

def _sun_height(when: datetime, lat: float, lon: float) -> float:
    longitude, _distance = sun_ecliptic(when)
    ra, dec = _equatorial(longitude, 0.0, _centuries(when))
    return _horizon(ra, dec, when, lat, lon)[1]


def _moon_clearance(when: datetime, lat: float, lon: float) -> float:
    """Height of the moon's top edge above the horizon as seen, in degrees (zero at rising and setting)."""
    longitude, latitude, distance = moon_ecliptic(when)
    ra, dec = _equatorial(longitude, latitude, _centuries(when))
    height = _horizon(ra, dec, when, lat, lon)[1]
    parallax = math.asin(6378.14 / distance) / RAD
    return height - (0.7275 * parallax - 0.5667)


def _events(function: Callable[[datetime], float], start: datetime, end: datetime, step_minutes: int = 6) -> List[Tuple[datetime, bool]]:
    """Every moment between ``start`` and ``end`` at which ``function`` crosses zero, with whether it was going up."""
    out: List[Tuple[datetime, bool]] = []
    step = timedelta(minutes=step_minutes)
    at = start
    before = function(at)
    while at < end:
        following = min(at + step, end)
        value = function(following)
        if (before < 0) != (value < 0):
            out.append((_crossing(function, at, following, seconds=2.0), value > before))
        at, before = following, value
    return out


def _first(events: List[Tuple[datetime, bool]], rising: bool) -> Optional[datetime]:
    return next((moment for moment, up in events if up == rising), None)


def _last(events: List[Tuple[datetime, bool]], rising: bool) -> Optional[datetime]:
    return next((moment for moment, up in reversed(events) if up == rising), None)


def day(on: date, lat: float, lon: float, zone: tzinfo) -> Dict[str, object]:
    """Everything for one calendar day at a place: the sun's rising, setting and twilights, the length of the day,
    the moon's rising and setting, and its phase at local noon. Times carry ``zone``. A thing that does not happen
    that day is ``None`` — with ``sun_state`` (and ``moon_state``) saying whether it stays ``up`` or ``down`` all day.
    Far north in summer a sunset can fall just after midnight and so come before that date's sunrise."""
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Latitude runs from -90 to 90 and longitude from -180 to 180.")
    start = datetime(on.year, on.month, on.day, tzinfo=zone)
    end = start + timedelta(days=1)
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def local(moment: Optional[datetime]) -> Optional[datetime]:
        return moment.astimezone(zone) if moment else None

    sun = _events(lambda when: _sun_height(when, lat, lon) - SUN_RISE, start_utc, end_utc)
    out: Dict[str, object] = {"date": on, "lat": lat, "lon": lon, "sunrise": local(_first(sun, True)), "sunset": local(_last(sun, False))}
    for name, depth in TWILIGHT.items():
        crossings = _events(lambda when, depth=depth: _sun_height(when, lat, lon) - depth, start_utc, end_utc)
        out[f"{name}_dawn"], out[f"{name}_dusk"] = local(_first(crossings, True)), local(_last(crossings, False))
    # Solar noon: the sun is highest (search the day at a minute's grain around the highest six-minute sample).
    samples = [start_utc + timedelta(minutes=6 * index) for index in range(241)]
    top = max(samples, key=lambda when: _sun_height(when, lat, lon))
    fine = [top + timedelta(seconds=20 * index) for index in range(-18, 19)]
    noon = max(fine, key=lambda when: _sun_height(when, lat, lon))
    out["noon"] = local(noon)
    out["noon_height"] = _sun_height(noon, lat, lon)
    midday = start_utc + (end_utc - start_utc) / 2
    if out["sunrise"] and out["sunset"] and out["sunset"] > out["sunrise"]:
        out["sun_state"], out["day_length"] = "", (out["sunset"] - out["sunrise"]).total_seconds()
    elif not sun:
        up = _sun_height(midday, lat, lon) > SUN_RISE
        out["sun_state"], out["day_length"] = ("up" if up else "down"), (86400.0 if up else 0.0)
    else:                                                               # it rises or sets, but not both, inside this calendar day
        out["sun_state"] = ""
        up_at_start = _sun_height(start_utc, lat, lon) > SUN_RISE
        lit, at, up = 0.0, start_utc, up_at_start
        for moment, rising in sun:
            if up:
                lit += (moment - at).total_seconds()
            at, up = moment, rising
        out["day_length"] = lit + ((end_utc - at).total_seconds() if up else 0.0)
    moon = _events(lambda when: _moon_clearance(when, lat, lon), start_utc, end_utc)
    out["moonrise"], out["moonset"] = local(_first(moon, True)), local(_first(moon, False))
    out["moon_state"] = "" if moon else ("up" if _moon_clearance(midday, lat, lon) > 0 else "down")
    # A moon that rises in the evening sets on the next date: say when, so that an earlier setting on this
    # date (the end of the night before) is not taken for it.
    rose = _first(moon, True)
    out["moon_sets_after"] = None
    if rose is not None:
        later = _events(lambda when: _moon_clearance(when, lat, lon), rose + timedelta(minutes=1), rose + timedelta(hours=26))
        out["moon_sets_after"] = local(_first(later, False))
    out["moon"] = moon_phase(midday)
    out["moon"]["next"] = [{"name": item["name"], "at": item["at"].astimezone(zone)} for item in out["moon"]["next"]]
    return out
