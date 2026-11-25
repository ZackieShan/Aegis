"""Place names to positions, offline.

The names come from GeoNames' ``cities1000`` list — every populated place of
a thousand people or more, about 160,000 of them, each with its position,
its region and country, its population and its time zone (GeoNames,
https://www.geonames.org, CC BY 4.0). ``scripts/aegis-almanac places fetch``
downloads the three small files (about 11 MB) and builds
``<data>/almanac/places.db`` from them; nothing else in the almanac needs it,
and without it a place simply has to be given as coordinates.

    find("Denver")                 the largest place of that name first
    find("Springfield, MO")        region by postal code or by name
    find("Paris, Texas")           … or "Paris, France", "Paris, FR"
    nearest(39.25, -106.29)        the nearest listed place: a name for a
                                   position, and the clock it keeps
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional

FILES = ("cities1000.txt", "admin1CodesASCII.txt", "countryInfo.txt")
SOURCE = "https://download.geonames.org/export/dump/"
_COUNTRY_WORDS = {"usa": "US", "us": "US", "u s": "US", "u s a": "US", "united states": "US", "united states of america": "US", "america": "US",
                  "uk": "GB", "u k": "GB", "great britain": "GB", "britain": "GB", "united kingdom": "GB", "england": "GB", "scotland": "GB",
                  "wales": "GB", "northern ireland": "GB", "holland": "NL", "russia": "RU", "south korea": "KR", "north korea": "KP",
                  "czech republic": "CZ", "ivory coast": "CI", "burma": "MM", "uae": "AE"}
_CANADA = {"ab": "alberta", "bc": "british columbia", "mb": "manitoba", "nb": "new brunswick", "nl": "newfoundland and labrador",
           "ns": "nova scotia", "nt": "northwest territories", "nu": "nunavut", "on": "ontario", "pe": "prince edward island", "qc": "quebec",
           "sk": "saskatchewan", "yt": "yukon"}
_SHORT = (("st", "saint"), ("ste", "sainte"), ("ft", "fort"), ("mt", "mount"), ("pt", "point"))


class Place(NamedTuple):
    name: str
    region: str          # the state or province, by name
    country: str         # by name
    country_code: str
    lat: float
    lon: float
    population: int
    elevation: Optional[int]     # metres
    zone: str            # IANA time zone, e.g. America/Denver

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.region and self.region != self.name:
            parts.append(self.region)
        if self.country and (self.country_code != "US" or not self.region):
            parts.append(self.country)
        return ", ".join(parts)


def path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "almanac" / "places.db"


def fold(text: str) -> str:
    """A name as it is looked up: no accents, no punctuation, lower case."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower().replace("ß", "ss").replace("ø", "o").replace("æ", "ae")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.replace("'", "").replace("’", ""))).strip()


def available() -> bool:
    return path().is_file()


def _connect() -> Optional[sqlite3.Connection]:
    target = path()
    if not target.is_file():
        return None
    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _place(row: sqlite3.Row) -> Place:
    return Place(row["name"], row["region"] or "", row["country"] or "", row["country_code"] or "", row["lat"], row["lon"], row["population"] or 0,
                 row["elevation"], row["zone"] or "")


# ── building ───────────────────────────────────────────────────────────────

def build(source: Path, target: Optional[Path] = None) -> int:
    """Build the lookup database from the GeoNames files in ``source``. Returns how many places went in."""
    source = Path(source)
    missing = [name for name in FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{', '.join(missing)} not found in {source}")
    target = Path(target or path())
    target.parent.mkdir(parents=True, exist_ok=True)
    regions: Dict[str, str] = {}
    with open(source / "admin1CodesASCII.txt", encoding="utf-8") as handle:
        for line in handle:
            cells = line.rstrip("\n").split("\t")
            if len(cells) >= 2:
                regions[cells[0]] = cells[1]
    countries: Dict[str, str] = {}
    with open(source / "countryInfo.txt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            cells = line.rstrip("\n").split("\t")
            if len(cells) >= 5:
                countries[cells[0]] = cells[4]
    building = target.with_suffix(".building")
    building.unlink(missing_ok=True)
    connection = sqlite3.connect(building)
    connection.executescript("""
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL, country_code TEXT, country TEXT, region_code TEXT,
                             region TEXT, population INTEGER, elevation INTEGER, zone TEXT, part INTEGER);
        CREATE TABLE names (key TEXT, place INTEGER);
        CREATE TABLE about (key TEXT PRIMARY KEY, value TEXT);
    """)
    count = 0
    with open(source / "cities1000.txt", encoding="utf-8") as handle:
        for line in handle:
            cells = line.rstrip("\n").split("\t")
            if len(cells) < 18:
                continue
            try:
                number, lat, lon = int(cells[0]), float(cells[4]), float(cells[5])
            except ValueError:
                continue
            height = cells[15] or cells[16]
            connection.execute("INSERT OR REPLACE INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (number, cells[1], lat, lon, cells[8], countries.get(cells[8], cells[8]), cells[10],
                                regions.get(f"{cells[8]}.{cells[10]}", ""), int(cells[14] or 0),
                                int(height) if re.fullmatch(r"-?\d+", height or "") else None, cells[17],
                                1 if cells[7] == "PPLX" else 0))               # a district of a larger place
            keys = {fold(cells[1]), fold(cells[2])}
            for other in cells[3].split(","):
                key = fold(other)
                if 2 <= len(key) <= 40 and re.search(r"[a-z]", key):          # names in other scripts fold to nothing
                    keys.add(key)
            connection.executemany("INSERT INTO names VALUES (?, ?)", [(key, number) for key in keys if key])
            count += 1
    connection.executescript("CREATE INDEX names_key ON names (key); CREATE INDEX places_lat ON places (lat);")
    connection.execute("INSERT INTO about VALUES ('places', ?)", (str(count),))
    connection.execute("INSERT INTO about VALUES ('source', 'GeoNames cities1000 (https://www.geonames.org), CC BY 4.0')")
    connection.commit()
    connection.execute("VACUUM")
    connection.close()
    os.replace(building, target)
    return count


# ── looking up ─────────────────────────────────────────────────────────────

def _spellings(key: str) -> List[str]:
    out = [key]
    words = key.split()
    for short, long in _SHORT:
        if short in words:
            out.append(" ".join(long if word == short else word for word in words))
        if long in words:
            out.append(" ".join(short if word == long else word for word in words))
    return list(dict.fromkeys(out))


def _matches(row: sqlite3.Row, qualifier: str) -> bool:
    """Does "CO", "Colorado", "US" or "France" describe this place's region or country?"""
    region, country, code, region_code = fold(row["region"]), fold(row["country"]), (row["country_code"] or "").lower(), (row["region_code"] or "").lower()
    if qualifier in (region, country, code):
        return True
    if _COUNTRY_WORDS.get(qualifier, "").lower() == code:
        return qualifier not in ("england", "scotland", "wales", "northern ireland") or qualifier == region
    if code in ("us", "au", "br", "mx", "in", "de", "it", "es") and qualifier == region_code:
        return True
    return code == "ca" and _CANADA.get(qualifier) == region


def _lookup(connection: sqlite3.Connection, name: str, qualifiers: Iterable[str], limit: int) -> List[Place]:
    rows: Dict[int, sqlite3.Row] = {}
    for key in _spellings(name):
        for row in connection.execute("SELECT p.* FROM names n JOIN places p ON p.id = n.place WHERE n.key = ? ORDER BY p.population DESC LIMIT 400",
                                      (key,)):
            rows.setdefault(row["id"], row)
    found = [row for row in rows.values() if all(_matches(row, qualifier) for qualifier in qualifiers)]

    def weight(row: sqlite3.Row) -> float:
        own = fold(row["name"]) in _spellings(name)                      # its own name counts for more than one of its other names
        return ((row["population"] or 0) + 1) * (1.0 if own else 0.2)

    found.sort(key=weight, reverse=True)
    if found:                                                            # a village that merely shares the name is not what was meant
        found = [row for row in found if weight(row) * 50 >= weight(found[0])]
    return [_place(row) for row in found[:limit]]


def find(query: str, limit: int = 5) -> List[Place]:
    """Places called ``query``, the likeliest first. Empty when there is no list installed or nothing by that name."""
    connection = _connect()
    if connection is None:
        return []
    try:
        pieces = [fold(piece) for piece in re.split(r"[,;/]|\s+-\s+", str(query or "")) if fold(piece)]
        if not pieces:
            return []
        found = _lookup(connection, pieces[0], pieces[1:], limit)
        if found or len(pieces) > 1:
            return found
        words = pieces[0].split()                                        # "Paris Texas", "Denver CO": the tail may be the region
        for cut in (1, 2, 3):
            if len(words) > cut:
                found = _lookup(connection, " ".join(words[:-cut]), [" ".join(words[-cut:])], limit)
                if found:
                    return found
        return []
    finally:
        connection.close()


def kilometres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Near enough for choosing the nearest place (a sphere)."""
    a = math.sin(math.radians(lat2 - lat1) / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def nearest(lat: float, lon: float, within_km: float = 300.0) -> Optional[Place]:
    """The listed place nearest to a position, if one lies within ``within_km``."""
    connection = _connect()
    if connection is None:
        return None
    try:
        best, best_distance = None, within_km
        for reach in (25.0, 100.0, within_km):
            span = reach / 111.0
            wide = min(180.0, span / max(0.05, math.cos(math.radians(lat))))
            west, east = lon - wide, lon + wide
            if west < -180 or east > 180:
                where, values = "(lon >= ? OR lon <= ?)", [(west + 360) if west < -180 else west, (east - 360) if east > 180 else east]
            else:
                where, values = "lon BETWEEN ? AND ?", [west, east]
            for row in connection.execute(f"SELECT * FROM places WHERE part = 0 AND lat BETWEEN ? AND ? AND {where}",
                                          [lat - span, lat + span, *values]):
                distance = kilometres(lat, lon, row["lat"], row["lon"])
                if distance <= min(reach, best_distance):
                    best, best_distance = row, distance
            if best is not None:
                return _place(best)
        return None
    finally:
        connection.close()


def locality(lat: float, lon: float, within_km: float = 300.0) -> Optional[Place]:
    """The place a person would name for a position: the nearest one, unless a much larger place is about as close
    (a point in a city is "in" the city, not in the suburb whose centre happens to be nearer)."""
    close = nearest(lat, lon, within_km)
    connection = _connect()
    if close is None or connection is None:
        return close
    try:
        distance = kilometres(lat, lon, close.lat, close.lon)
        reach = min(150.0, max(12.0, 1.5 * distance))                   # "about as close": within half as far again
        span = reach / 111.0
        wide = span / max(0.05, math.cos(math.radians(lat)))
        best, best_size = close, max(close.population, 1) * 20
        for row in connection.execute("SELECT * FROM places WHERE part = 0 AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND population >= ?",
                                      (lat - span, lat + span, lon - wide, lon + wide, best_size)):
            if kilometres(lat, lon, row["lat"], row["lon"]) <= reach and row["population"] >= best_size:
                best, best_size = _place(row), row["population"]
        return best
    finally:
        connection.close()


def count() -> int:
    connection = _connect()
    if connection is None:
        return 0
    try:
        row = connection.execute("SELECT value FROM about WHERE key = 'places'").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()
