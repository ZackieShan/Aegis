"""Places on the map: latitude and longitude, UTM, MGRS, and the distance
and bearing between two of them.

All on the WGS84 ellipsoid (the one GPS uses). UTM is worked with the Krüger
series, good to a millimetre inside a zone; distance and bearing with
Vincenty's formulas, good to a millimetre anywhere two points are not
exactly opposite each other. Bearings are true (from the pole), not
magnetic: a compass needs the local declination added.

A place can be written the ways people write one:

    40.7484, -73.9857            decimal degrees (north and east positive)
    40.7484 N 73.9857 W          … or with letters
    40°44'54.3"N 73°59'08.4"W    degrees, minutes, seconds
    40 44.905 N 73 59.139 W      degrees and decimal minutes
    18T 585632 4511326           UTM: zone and band, easting, northing
    18TWL8563211326              MGRS, any even number of digits (spaces allowed)

A grid reference names a square, not a point: ten digits a 1 m square, eight
a 10 m square, and so on. Writing one, the numbers are cut short, never
rounded (so the point is always inside the square named); reading one, the
answer is the middle of the square.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

A = 6378137.0                     # WGS84 semi-major axis, metres
F = 1 / 298.257223563
B = A * (1 - F)
K0 = 0.9996
_N = F / (2 - F)
_A = A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_ALPHA = (_N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16, 13 * _N ** 2 / 48 - 3 * _N ** 3 / 5, 61 * _N ** 3 / 240)
_BETA = (_N / 2 - 2 * _N ** 2 / 3 + 37 * _N ** 3 / 96, _N ** 2 / 48 + _N ** 3 / 15, 17 * _N ** 3 / 480)
_DELTA = (2 * _N - 2 * _N ** 2 / 3 - 2 * _N ** 3, 7 * _N ** 2 / 3 - 8 * _N ** 3 / 5, 56 * _N ** 3 / 15)
_BANDS = "CDEFGHJKLMNPQRSTUVWX"                      # 8° of latitude each from 80°S; X is 12°
_COLUMNS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")       # 100 km squares, by zone number
_ROWS = "ABCDEFGHJKLMNPQRSTUV"
_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


class PlaceError(ValueError):
    """Why a place could not be read, in words for the person."""


# ── UTM ────────────────────────────────────────────────────────────────────

def zone_for(lat: float, lon: float) -> int:
    zone = int((lon + 180) // 6) % 60 + 1
    if 56 <= lat < 64 and 3 <= lon < 12:             # south-west Norway
        return 32
    if 72 <= lat <= 84:                              # Svalbard
        if 0 <= lon < 9:
            return 31
        if 9 <= lon < 21:
            return 33
        if 21 <= lon < 33:
            return 35
        if 33 <= lon < 42:
            return 37
    return zone


def band_for(lat: float) -> str:
    if not -80 <= lat <= 84:
        raise PlaceError("UTM and MGRS cover 80°S to 84°N; the poles use another grid.")
    return _BANDS[min(int((lat + 80) // 8), 19)]


def to_utm(lat: float, lon: float, zone: Optional[int] = None) -> Tuple[int, str, float, float]:
    """``(zone, band, easting, northing)`` in metres."""
    band = band_for(lat)
    zone = zone or zone_for(lat, lon)
    phi = math.radians(lat)
    lam = math.radians(lon - ((zone - 1) * 6 - 180 + 3))
    s = 2 * math.sqrt(_N) / (1 + _N)
    t = math.sinh(math.atanh(math.sin(phi)) - s * math.atanh(s * math.sin(phi)))
    xi = math.atan2(t, math.cos(lam))
    eta = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    easting = 500000 + K0 * _A * (eta + sum(a * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, a in enumerate(_ALPHA, 1)))
    northing = K0 * _A * (xi + sum(a * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, a in enumerate(_ALPHA, 1)))
    if lat < 0:
        northing += 10_000_000
    return zone, band, easting, northing


def from_utm(zone: int, easting: float, northing: float, south: bool = False) -> Tuple[float, float]:
    """``(latitude, longitude)`` for a UTM position."""
    if not 1 <= zone <= 60:
        raise PlaceError("A UTM zone is a number from 1 to 60.")
    if not 100_000 <= easting <= 900_000 or not 0 <= northing <= 10_000_000:
        raise PlaceError("That is outside the range of UTM eastings (100,000–900,000 m) or northings (0–10,000,000 m).")
    xi = (northing - (10_000_000 if south else 0)) / (K0 * _A)
    eta = (easting - 500000) / (K0 * _A)
    xi1 = xi - sum(b * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, b in enumerate(_BETA, 1))
    eta1 = eta - sum(b * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, b in enumerate(_BETA, 1))
    chi = math.asin(math.sin(xi1) / math.cosh(eta1))
    phi = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(_DELTA, 1))
    lam = math.atan2(math.sinh(eta1), math.cos(xi1))
    lon = (zone - 1) * 6 - 180 + 3 + math.degrees(lam)
    return math.degrees(phi), (lon + 180) % 360 - 180          # a square at the edge of zone 60 runs past 180°


# ── MGRS ───────────────────────────────────────────────────────────────────

def to_mgrs(lat: float, lon: float, digits: int = 5) -> str:
    """The MGRS reference, ``digits`` (1–5) for each of easting and northing: 5 is a metre, 4 ten metres…"""
    digits = max(1, min(5, int(digits)))
    zone, band, easting, northing = to_utm(lat, lon)
    column = _COLUMNS[(zone - 1) % 3][int(easting // 100000) - 1]
    row = _ROWS[(int(northing // 100000) + (5 if zone % 2 == 0 else 0)) % 20]
    scale = 10 ** (5 - digits)
    east = int(easting % 100000) // scale
    north = int(northing % 100000) // scale
    return f"{zone:02d}{band}{column}{row}{east:0{digits}d}{north:0{digits}d}"


def mgrs_square(text: str) -> Tuple[float, float, int]:
    """``(latitude, longitude, side)``: the middle of the square an MGRS
    reference names, and the length of that square's side in metres."""
    match = re.fullmatch(r"(\d{1,2})([A-Z])([A-Z])([A-Z])(\d*)", re.sub(r"[\s-]+", "", str(text or "")).upper())
    if not match or len(match.group(5)) % 2 or len(match.group(5)) > 10:
        raise PlaceError("An MGRS reference looks like 18TWL8563211326: zone, band letter, two square letters, "
                         "then an even number of digits (up to ten).")
    zone, band, column, row, numbers = int(match.group(1)), match.group(2), match.group(3), match.group(4), match.group(5)
    if not 1 <= zone <= 60:
        raise PlaceError("An MGRS zone is a number from 1 to 60.")
    if band not in _BANDS:
        raise PlaceError(f"{band} is not a latitude band: they run from C to X, leaving out I and O.")
    columns = _COLUMNS[(zone - 1) % 3]
    if column not in columns or row not in _ROWS:
        raise PlaceError(f"There is no 100 km square {column}{row} in zone {zone}.")
    half = len(numbers) // 2
    side = 10 ** (5 - half)
    easting = (columns.index(column) + 1) * 100000 + (int(numbers[:half]) * side if half else 0) + side / 2
    northing = ((_ROWS.index(row) - (5 if zone % 2 == 0 else 0)) % 20) * 100000 + (int(numbers[half:]) * side if half else 0) + side / 2
    # The row letters repeat every 2,000 km: take the repeat that lands in this latitude band.
    band_south = -80 + 8 * _BANDS.index(band)
    lowest = to_utm(band_south, (zone - 1) * 6 - 180 + 3, zone)[3]
    while northing < lowest - 100000:
        northing += 2_000_000
    if northing > 10_000_000:
        raise PlaceError(f"Square {column}{row} does not lie in band {band} of zone {zone}.")
    lat, lon = from_utm(zone, easting, northing, south=band < "N")
    slack = (side + 2000) / 111_000
    if not band_south - slack <= lat <= band_south + (12 if band == "X" else 8) + slack:
        raise PlaceError(f"Square {column}{row} does not lie in band {band} of zone {zone}.")
    return lat, lon, side


def from_mgrs(text: str) -> Tuple[float, float]:
    """``(latitude, longitude)`` of the middle of the square an MGRS reference names."""
    lat, lon, _side = mgrs_square(text)
    return lat, lon


# ── reading a place ────────────────────────────────────────────────────────

class Reading(NamedTuple):
    lat: float
    lon: float
    kind: str        # "latlon", "utm" or "mgrs"
    note: str        # something worth telling the person about how it was read


_UTM = re.compile(r"(?:utm\s*)?(?:zone\s*)?(\d{1,2})\s*([A-Za-z])?[\s,]+(\d{6}(?:\.\d+)?)\s*(?:m\s*)?E?[\s,]+"
                  r"(\d{1,8}(?:\.\d+)?)\s*(?:m\s*)?N?", re.I)
_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_SIDE = r"(?<![A-Za-z])[NSEWnsew](?![A-Za-z])"
_NO_PLACE = ("I could not find a latitude and a longitude in that. Write them like 40.7484, -73.9857 "
             "or 40°44'54\"N 73°59'08\"W — or give a UTM position (18T 585632 4511326) or an MGRS reference (18TWL8563211326).")


def _side_size(side: int) -> str:
    return f"{side // 1000} km" if side >= 1000 else f"{side} m"


def _read_utm(match: "re.Match[str]") -> Reading:
    zone, band = int(match.group(1)), (match.group(2) or "").upper()
    easting, northing = float(match.group(3)), float(match.group(4))
    if band and band not in _BANDS:
        raise PlaceError(f"{band} is not a UTM latitude band: they run from C to X, leaving out I and O.")
    lat, lon = from_utm(zone, easting, northing, south=bool(band) and band < "N")
    if not band:
        return Reading(lat, lon, "utm", "No band letter was given, so this is read as north of the equator.")
    south_edge = -80 + 8 * _BANDS.index(band)
    if south_edge - 0.5 <= lat <= south_edge + (12 if band == "X" else 8) + 0.5:
        return Reading(lat, lon, "utm", "")
    if band == "S":                                   # "18S" is also how some devices write "zone 18, southern hemisphere"
        lat, lon = from_utm(zone, easting, northing, south=True)
        return Reading(lat, lon, "utm", "Read the S as “southern hemisphere”: that northing is not in latitude band S (32°–40°N).")
    if band == "N":
        return Reading(lat, lon, "utm", "Read the N as “northern hemisphere”: that northing is not in latitude band N (0°–8°N).")
    try:
        actual = band_for(lat)
    except PlaceError:
        actual = "none"
    return Reading(lat, lon, "utm", f"That northing lies in latitude band {actual}, not {band}; the position is worked from the numbers.")


def _angle(numbers: List[str], what: str) -> float:
    degrees = abs(float(numbers[0]))
    minutes = abs(float(numbers[1])) if len(numbers) > 1 else 0.0
    seconds = abs(float(numbers[2])) if len(numbers) > 2 else 0.0
    if minutes >= 60 or seconds >= 60:
        raise PlaceError(f"In the {what}, minutes and seconds each have to be under 60.")
    if len(numbers) > 1 and degrees != int(degrees):
        raise PlaceError(f"In the {what}, degrees with a decimal point cannot be followed by minutes.")
    value = degrees + minutes / 60 + seconds / 3600
    return -value if numbers[0].lstrip().startswith("-") else value


def _read_latlon(raw: str) -> Reading:
    labels = [found.group(1).lower()[:2] for found in re.finditer(r"(?i)\b(lat|lon|lng)[a-z]*\b", raw)]
    text = re.sub(r"(?i)\b(?:lat|lon|lng)[a-z]*\b\s*[:=]?", " ", raw)
    text = re.sub(r"(?i)\b(north|south|east|west)\b", lambda found: found.group(1)[0].upper(), text)
    text = re.sub(r"(?i)\b(?:degrees?|deg|minutes?|min|seconds?|sec)\b\.?", " ", text)
    if "." not in text and len(re.findall(r"\d,\d", text)) == 2:
        text = re.sub(r"(\d),(\d)", r"\1.\2", text)        # 40,7484; -73,9857 — commas as decimal points
    if re.sub(rf"{_NUMBER}|{_SIDE}|[\s,;/|°º'’′\"”″:()]+", "", text):
        raise PlaceError(_NO_PLACE)
    tokens = re.findall(rf"{_NUMBER}|{_SIDE}", text)
    sides = [token.upper() for token in tokens if token.isalpha()]
    numbers = [token for token in tokens if not token.isalpha()]
    if len(numbers) not in (2, 4, 6) or len(sides) not in (0, 2):
        raise PlaceError(_NO_PLACE)
    each = len(numbers) // 2
    if sides:
        shape = "".join("L" if token.isalpha() else "n" for token in tokens)
        if shape not in ("L" + "n" * each + "L" + "n" * each, "n" * each + "L" + "n" * each + "L"):
            raise PlaceError(_NO_PLACE)
        if (sides[0] in "NS") == (sides[1] in "NS"):
            raise PlaceError("One of the two has to be north or south, and the other east or west.")
    first, second = _angle(numbers[:each], "first number"), _angle(numbers[each:], "second number")
    if sides:
        first = -abs(first) if sides[0] in "SW" else abs(first)
        second = -abs(second) if sides[1] in "SW" else abs(second)
    if (sides and sides[0] in "EW") or (not sides and labels[:2] in (["lo", "la"], ["ln", "la"])):
        first, second = second, first                 # written longitude first
    if not -90 <= first <= 90 or not -180 <= second <= 180:
        raise PlaceError("Latitude runs from -90 to 90 and longitude from -180 to 180.")
    return Reading(first, second, "latlon", "")


def read(text: str) -> Reading:
    """A place written in any of the ways in this module's description."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip().strip(".")
    if not raw:
        raise PlaceError("Give a place: latitude and longitude, a UTM position or an MGRS reference.")
    squeezed = re.sub(r"[\s-]+", "", re.sub(r"(?i)^\s*mgrs\s*:?", "", raw)).upper()
    if re.fullmatch(r"\d{1,2}[A-Z]{3}\d*", squeezed):
        lat, lon, side = mgrs_square(squeezed)
        return Reading(lat, lon, "mgrs", f"That reference names a {_side_size(side)} square; this is its middle." if side > 1 else "")
    utm = _UTM.fullmatch(raw)
    if utm:
        return _read_utm(utm)
    return _read_latlon(raw)


def parse(text: str) -> Tuple[float, float]:
    """``(latitude, longitude)`` from a place written in any of those ways."""
    found = read(text)
    return found.lat, found.lon


# ── writing a place ────────────────────────────────────────────────────────

def dms(value: float, positive: str, negative: str) -> str:
    side = positive if value >= 0 else negative
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = (value - degrees - minutes / 60) * 3600
    if seconds >= 59.95:
        seconds, minutes = 0.0, minutes + 1
    if minutes >= 60:
        minutes, degrees = 0, degrees + 1
    return f"{degrees}°{minutes:02d}'{seconds:04.1f}\"{side}"


def describe(lat: float, lon: float) -> Dict[str, str]:
    """One place in every notation."""
    out = {"decimal": f"{lat:.6f}, {lon:.6f}", "dms": f"{dms(lat, 'N', 'S')} {dms(lon, 'E', 'W')}"}
    try:
        zone, band, easting, northing = to_utm(lat, lon)
        out["utm"] = f"{zone}{band} {int(easting)} {int(northing)}"
        out["mgrs"] = to_mgrs(lat, lon)
    except PlaceError:
        out["utm"] = out["mgrs"] = "(outside the UTM grid: beyond 80°S or 84°N)"
    return out


# ── between two places ─────────────────────────────────────────────────────

def compass(bearing: float) -> str:
    return _POINTS[int((bearing % 360) / 22.5 + 0.5) % 16]


def between(a: Tuple[float, float], b: Tuple[float, float]) -> Dict[str, float]:
    """Distance (metres) and the bearing to set out on from ``a`` to ``b``,
    and the bearing to come back on (Vincenty's inverse formula)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    u1 = math.atan((1 - F) * math.tan(lat1))
    u2 = math.atan((1 - F) * math.tan(lat2))
    width = lon2 - lon1
    lam = width
    sin_u1, cos_u1, sin_u2, cos_u2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        if sin_sigma == 0:
            return {"metres": 0.0, "bearing": 0.0, "back_bearing": 0.0}
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha ** 2
        cos_2sm = cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha if cos2_alpha else 0.0
        c = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
        previous = lam
        lam = width + (1 - c) * F * sin_alpha * (sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1 + 2 * cos_2sm ** 2)))
        if abs(lam - previous) < 1e-12:
            break
    else:                                             # nearly opposite points: the sphere is close enough
        return _on_a_sphere(a, b)
    u_sq = cos2_alpha * (A ** 2 - B ** 2) / B ** 2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta = big_b * sin_sigma * (cos_2sm + big_b / 4 * (cos_sigma * (-1 + 2 * cos_2sm ** 2)
                                 - big_b / 6 * cos_2sm * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos_2sm ** 2)))
    metres = B * big_a * (sigma - delta)
    forward = math.degrees(math.atan2(cos_u2 * math.sin(lam), cos_u1 * sin_u2 - sin_u1 * cos_u2 * math.cos(lam))) % 360
    back = math.degrees(math.atan2(cos_u1 * math.sin(lam), -sin_u1 * cos_u2 + cos_u1 * sin_u2 * math.cos(lam))) % 360
    return {"metres": metres, "bearing": forward, "back_bearing": (back + 180) % 360}


def _on_a_sphere(a: Tuple[float, float], b: Tuple[float, float]) -> Dict[str, float]:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    metres = 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))
    forward = math.degrees(math.atan2(math.sin(lon2 - lon1) * math.cos(lat2),
                                      math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))) % 360
    back = math.degrees(math.atan2(math.sin(lon1 - lon2) * math.cos(lat1),
                                   math.cos(lat2) * math.sin(lat1) - math.sin(lat2) * math.cos(lat1) * math.cos(lon1 - lon2))) % 360
    return {"metres": metres, "bearing": forward, "back_bearing": back}


def onward(start: Tuple[float, float], bearing: float, metres: float) -> Tuple[float, float]:
    """Where you are after going ``metres`` from ``start`` on ``bearing`` (Vincenty's direct formula)."""
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    alpha1 = math.radians(bearing % 360)
    u1 = math.atan((1 - F) * math.tan(lat1))
    sigma1 = math.atan2(math.tan(u1), math.cos(alpha1))
    sin_alpha = math.cos(u1) * math.sin(alpha1)
    cos2_alpha = 1 - sin_alpha ** 2
    u_sq = cos2_alpha * (A ** 2 - B ** 2) / B ** 2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    sigma = metres / (B * big_a)
    for _ in range(200):
        cos_2sm = math.cos(2 * sigma1 + sigma)
        delta = big_b * math.sin(sigma) * (cos_2sm + big_b / 4 * (math.cos(sigma) * (-1 + 2 * cos_2sm ** 2)
                                           - big_b / 6 * cos_2sm * (-3 + 4 * math.sin(sigma) ** 2) * (-3 + 4 * cos_2sm ** 2)))
        previous = sigma
        sigma = metres / (B * big_a) + delta
        if abs(sigma - previous) < 1e-12:
            break
    cos_2sm = math.cos(2 * sigma1 + sigma)
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    lat2 = math.atan2(sin_u1 * math.cos(sigma) + cos_u1 * math.sin(sigma) * math.cos(alpha1),
                      (1 - F) * math.hypot(sin_alpha, sin_u1 * math.sin(sigma) - cos_u1 * math.cos(sigma) * math.cos(alpha1)))
    lam = math.atan2(math.sin(sigma) * math.sin(alpha1), cos_u1 * math.cos(sigma) - sin_u1 * math.sin(sigma) * math.cos(alpha1))
    c = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
    width = lam - (1 - c) * F * sin_alpha * (sigma + c * math.sin(sigma) * (cos_2sm + c * math.cos(sigma) * (-1 + 2 * cos_2sm ** 2)))
    lon2 = (math.degrees(lon1 + width) + 540) % 360 - 180
    return math.degrees(lat2), lon2
