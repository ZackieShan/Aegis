"""Units of measure: the same quantity written another way, exactly.

Every unit is a factor on the SI unit of its kind, taken from its
definition (an inch is 25.4 mm, a pound 0.45359237 kg, a US gallon 231
cubic inches…), so a conversion is arithmetic and nothing is looked up or
guessed. Units combine: ``km/h``, ``lb/ft3``, ``gal/min``, ``BTU/h`` and
``kWh/day`` work without being listed, because a unit knows what it is made
of (length, mass, time…).

    convert("5 ft 10 in", "cm")        → 177.8 cm
    convert("72 F", "C")               → 22.2222 °C
    convert("30 mpg", "L/100km")       → 7.84049 L/100 km
    convert("177.8 cm", "ft in")       → 5 ft 10 in
    convert("2 cups")                  → what that is in the other system

Things worth knowing, because they are where conversions go wrong:

* gallons, quarts, pints, cups and fluid ounces are the US ones unless
  written ``imp gal`` / ``UK pint``; a ``ton`` is the US short ton (2,000 lb)
  — ``tonne`` or ``t`` is 1,000 kg, ``long ton`` 2,240 lb;
* ``cal`` is the small calorie; the calorie on food labels is ``kcal``;
* ``nm`` is nanometres — nautical miles are ``nmi`` — and ``Nm`` is the
  newton-metre;
* ``mil`` is a thousandth of an inch, or — beside an angle — the NATO mil
  (6,400 to a circle);
* ``MB`` and ``GB`` are powers of ten; ``MiB`` and ``GiB`` powers of two.

The answer always names the units in full, so a misreading shows.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

Vector = Tuple[int, ...]
_ORDER = "LMTKBAFHGQ"       # length, mass, time, temperature, data, angle; then kinds kept apart on purpose:
#                             F fuel economy, H radiation dose (sievert), G absorbed dose (gray), Q radioactivity


def _v(**powers: int) -> Vector:
    return tuple(powers.get(base, 0) for base in _ORDER)


KINDS: Dict[str, Vector] = {
    "length": _v(L=1), "area": _v(L=2), "volume": _v(L=3), "weight": _v(M=1), "time": _v(T=1), "temperature": _v(K=1),
    "speed": _v(L=1, T=-1), "acceleration": _v(L=1, T=-2), "force": _v(M=1, L=1, T=-2), "pressure": _v(M=1, L=-1, T=-2),
    "energy": _v(M=1, L=2, T=-2), "power": _v(M=1, L=2, T=-3), "flow": _v(L=3, T=-1), "density": _v(M=1, L=-3),
    "weight per area": _v(M=1, L=-2), "data": _v(B=1), "data rate": _v(B=1, T=-1), "angle": _v(A=1), "turning speed": _v(A=1, T=-1),
    "fuel economy": _v(F=1), "fuel use": _v(F=-1), "radiation dose": _v(H=1), "dose rate": _v(H=1, T=-1), "absorbed dose": _v(G=1),
    "radioactivity": _v(Q=1), "rate": _v(T=-1),
}
_KIND_OF = {vector: name for name, vector in KINDS.items()}
_NONE = _v()


class UnitError(ValueError):
    """Why something could not be converted, in words for the person."""


class Unit(NamedTuple):
    symbol: str
    one: str
    many: str
    vector: Vector
    factor: float                 # multiply by this to get SI
    offset: float = 0.0           # temperatures only: SI = (value + offset) * factor
    system: str = ""              # "metric", "us", "uk", or "" for both


_EXACT: Dict[str, List[Unit]] = {}
_LOOSE: Dict[str, List[Unit]] = {}
_BY_KIND: Dict[str, List[Unit]] = {}


def _key(text: str) -> str:
    text = str(text or "").replace("²", "2").replace("³", "3").replace("^", "").replace("μ", "µ").replace(".", "")
    text = text.replace("º", "°").replace("’", "'").replace("″", '"').replace("′", "'")
    return re.sub(r"\s+", " ", text).strip()


def _register(names: Sequence[str], unit: Unit) -> None:
    for name in names:
        key = _key(name)
        if not key:
            continue
        for table, entry in ((_EXACT, key), (_LOOSE, key.lower())):
            known = table.setdefault(entry, [])
            if unit not in known:
                known.append(unit)


def _add(kind: str, symbol: str, one: str, many: str, factor: float, aliases: str = "", system: str = "", offset: float = 0.0,
         listed: bool = True) -> Unit:
    unit = Unit(symbol, one.split("|")[0], many.split("|")[0], KINDS[kind], factor, offset, system)
    _register([symbol, *one.split("|"), *many.split("|"), *[alias for alias in aliases.split("|") if alias]], unit)
    if listed:
        _BY_KIND.setdefault(kind, []).append(unit)
    return unit


_PREFIX = {"n": ("nano", 1e-9), "µ": ("micro", 1e-6), "m": ("milli", 1e-3), "c": ("centi", 1e-2), "d": ("deci", 1e-1), "h": ("hecto", 1e2),
           "k": ("kilo", 1e3), "M": ("mega", 1e6), "G": ("giga", 1e9), "T": ("tera", 1e12), "P": ("peta", 1e15),
           "Ki": ("kibi", 2.0 ** 10), "Mi": ("mebi", 2.0 ** 20), "Gi": ("gibi", 2.0 ** 30), "Ti": ("tebi", 2.0 ** 40)}


def _metric(kind: str, symbol: str, one: str, many: str, factor: float, prefixes: str, aliases: str = "", listed: str = "",
            system: str = "metric") -> None:
    """A unit and its prefixed forms (``prefixes`` separated by spaces; ``listed`` says which appear in menus)."""
    shown = listed.split()
    _add(kind, symbol, one, many, factor, aliases, system, listed="-" in shown or not listed)
    for prefix in prefixes.split():
        word, scale = _PREFIX[prefix]
        extra = [prefix.replace("µ", "u") + symbol, prefix.replace("µ", "mc") + symbol] if prefix == "µ" else []
        _add(kind, prefix + symbol, "|".join(word + name for name in one.split("|")), "|".join(word + name for name in many.split("|")),
             factor * scale, "|".join(extra), system, listed=prefix in shown)


IN, FT, YD, MI, NMI = 0.0254, 0.3048, 0.9144, 1609.344, 1852.0
LB = 0.45359237
GAL, IMP_GAL = 231 * IN ** 3, 4.54609e-3
LBF, G0 = LB * 9.80665, 9.80665
BTU = 1055.05585262
PI = math.pi

# ── length ──
_metric("length", "m", "metre|meter", "metres|meters", 1.0, "n µ m c d k", listed="- m c k")
_add("length", "in", "inch", "inches", IN, 'ins|"', "us")
_add("length", "ft", "foot", "feet", FT, "'|foots", "us")
_add("length", "yd", "yard", "yards", YD, "yds", "us")
_add("length", "mi", "mile", "miles", MI, "statute mile|statute miles", "us")
_add("length", "nmi", "nautical mile", "nautical miles", NMI, "NM|naut mi|nm", "")
_add("length", "thou", "thousandth of an inch", "thousandths of an inch", IN / 1000, "mil|mils|thous", "us", listed=False)
_add("length", "fathom", "fathom", "fathoms", 6 * FT, "ftm", "us", listed=False)
_add("length", "rod", "rod", "rods", 16.5 * FT, "perch|pole", "us", listed=False)
_add("length", "chain", "chain", "chains", 66 * FT, "ch", "us", listed=False)
_add("length", "furlong", "furlong", "furlongs", 660 * FT, "fur", "us", listed=False)
_add("length", "league", "league", "leagues", 3 * MI, "", "us", listed=False)
_add("length", "hand", "hand", "hands", 4 * IN, "hh", "us", listed=False)
_add("length", "Å", "ångström|angstrom", "ångströms|angstroms", 1e-10, "", "metric", listed=False)
_add("length", "au", "astronomical unit", "astronomical units", 149_597_870_700.0, "AU", "", listed=False)
_add("length", "ly", "light-year|light year|lightyear", "light-years|light years|lightyears", 9_460_730_472_580_800.0, "", "", listed=False)
_add("length", "klick", "klick", "klicks", 1000.0, "click|clicks", "metric", listed=False)

# ── area and volume, from every length (m2, sq ft, cubic yards…) ──
_LENGTHS = [("nm", "nanometre"), ("µm", "micrometre"), ("mm", "millimetre"), ("cm", "centimetre"), ("dm", "decimetre"), ("m", "metre"),
            ("km", "kilometre"), ("in", "inch"), ("ft", "foot"), ("yd", "yard"), ("mi", "mile"), ("nmi", "nautical mile")]
for _symbol, _name in _LENGTHS:
    _base = _EXACT[_symbol][0]
    _names = sorted({name for name, units in _EXACT.items() if units[0] is _base and name not in ('"', "'")}, key=len)
    _listed = _symbol in ("mm", "cm", "m", "km", "in", "ft", "yd", "mi")
    _many = _base.many
    _add("area", f"{_symbol}²", f"square {_base.one}", f"square {_many}", _base.factor ** 2,
         "|".join([f"{n}2|sq {n}|square {n}|{n} squared|sq{n}" for n in _names]), _base.system, listed=_listed)
    _add("volume", f"{_symbol}³", f"cubic {_base.one}", f"cubic {_many}", _base.factor ** 3,
         "|".join([f"{n}3|cu {n}|cubic {n}|{n} cubed|cu{n}" for n in _names]), _base.system, listed=_listed and _symbol not in ("km", "mi", "mm"))
_add("area", "ha", "hectare", "hectares", 1e4, "", "metric")
_add("area", "acre", "acre", "acres", 43560 * FT ** 2, "ac", "us")
_add("area", "are", "are", "ares", 100.0, "", "metric", listed=False)
_add("area", "section", "section (square mile)", "sections (square miles)", MI ** 2, "sections", "us", listed=False)

_metric("volume", "L", "litre|liter", "litres|liters", 1e-3, "µ m c d", "l|ltr", listed="- m")
_add("volume", "cc", "cubic centimetre", "cubic centimetres", 1e-6, "ccs", "metric", listed=False)
_add("volume", "tsp", "US teaspoon|teaspoon", "US teaspoons|teaspoons", GAL / 768, "tsps", "us")
_add("volume", "tbsp", "US tablespoon|tablespoon", "US tablespoons|tablespoons", GAL / 256, "tbs|tbl|tbsps", "us")
_add("volume", "fl oz", "US fluid ounce|fluid ounce", "US fluid ounces|fluid ounces", GAL / 128, "floz|oz fl|US fl oz", "us")
_add("volume", "cup", "US cup|cup", "US cups|cups", GAL / 16, "US cup", "us")
_add("volume", "pt", "US pint|pint", "US pints|pints", GAL / 8, "US pt|US pint", "us")
_add("volume", "qt", "US quart|quart", "US quarts|quarts", GAL / 4, "US qt|US quart", "us")
_add("volume", "gal", "US gallon|gallon", "US gallons|gallons", GAL, "US gal|gals|US gallon|US gallons", "us")
_add("volume", "imp fl oz", "imperial fluid ounce", "imperial fluid ounces", IMP_GAL / 160, "UK fl oz|imp floz|imperial fl oz", "uk", listed=False)
_add("volume", "imp pt", "imperial pint", "imperial pints", IMP_GAL / 8, "UK pt|UK pint|UK pints|imp pint|imp pints", "uk")
_add("volume", "imp qt", "imperial quart", "imperial quarts", IMP_GAL / 4, "UK qt|UK quart|UK quarts|imp quart|imp quarts", "uk", listed=False)
_add("volume", "imp gal", "imperial gallon", "imperial gallons", IMP_GAL, "UK gal|UK gallon|UK gallons|imp gallon|imp gallons", "uk")
_add("volume", "metric cup", "metric cup", "metric cups", 250e-6, "", "metric", listed=False)
_add("volume", "bbl", "barrel of oil", "barrels of oil", 42 * GAL, "barrel|barrels|oil barrel|oil barrels", "us", listed=False)
_add("volume", "bu", "US bushel|bushel", "US bushels|bushels", 2150.42 * IN ** 3, "bsh", "us", listed=False)
_add("volume", "pk", "US peck|peck", "US pecks|pecks", 2150.42 * IN ** 3 / 4, "", "us", listed=False)
_add("volume", "dry qt", "US dry quart", "US dry quarts", 2150.42 * IN ** 3 / 32, "", "us", listed=False)
_add("volume", "cord", "cord of wood|cord", "cords of wood|cords", 128 * FT ** 3, "", "us", listed=False)
_add("volume", "board foot", "board foot", "board feet", 144 * IN ** 3, "bd ft|bf|fbm", "us", listed=False)
_add("volume", "acre-foot", "acre-foot|acre foot", "acre-feet|acre feet", 43560 * FT ** 3, "ac ft|ac-ft|acre ft|acre-ft", "us", listed=False)

# ── weight ──
_metric("weight", "g", "gram|gramme", "grams|grammes", 1e-3, "n µ m k", "gm|gms", listed="- m k")
_add("weight", "t", "tonne|metric ton", "tonnes|metric tons", 1000.0, "mt|metric tonne|metric tonnes", "metric")
_add("weight", "oz", "ounce", "ounces", LB / 16, "ozs|oz wt", "us")
_register(["oz", "ozs"], _EXACT["fl oz"][0])           # after the ounce of weight, so that one is tried first
_add("weight", "lb", "pound", "pounds", LB, "lbs|lbm|#", "us")
_add("weight", "st", "stone", "stone|stones", 14 * LB, "", "uk", listed=False)
_add("weight", "short ton", "short ton (2,000 lb)|short ton|US ton|ton", "short tons (2,000 lb)|short tons|US tons|tons", 2000 * LB, "tn", "us")
_add("weight", "long ton", "long ton (2,240 lb)|long ton|UK ton|imperial ton", "long tons (2,240 lb)|long tons|UK tons|imperial tons", 2240 * LB, "", "uk",
     listed=False)
_add("weight", "cwt", "US hundredweight|hundredweight", "US hundredweight|hundredweights", 100 * LB, "", "us", listed=False)
_add("weight", "gr", "grain", "grains", 64.79891e-6, "grs", "us")
_add("weight", "dr", "dram", "drams", LB / 256, "drachm", "us", listed=False)
_add("weight", "ozt", "troy ounce", "troy ounces", 31.1034768e-3, "oz t|troy oz|t oz", "us", listed=False)
_add("weight", "dwt", "pennyweight", "pennyweights", 31.1034768e-3 / 20, "pwt", "us", listed=False)
_add("weight", "ct", "carat", "carats", 0.2e-3, "", "metric", listed=False)
_add("weight", "slug", "slug", "slugs", LBF / FT, "", "us", listed=False)

# ── time ──
_metric("time", "s", "second", "seconds", 1.0, "n µ m", "sec|secs", listed="- m", system="")
_add("time", "min", "minute", "minutes", 60.0, "mins")
_add("time", "h", "hour", "hours", 3600.0, "hr|hrs")
_add("time", "d", "day", "days", 86400.0)
_add("time", "wk", "week", "weeks", 7 * 86400.0, "wks")
_add("time", "fortnight", "fortnight", "fortnights", 14 * 86400.0, listed=False)
_add("time", "mo", "month (a twelfth of a year)|month", "months (twelfths of a year)|months", 365.25 * 86400 / 12, "mos|mth|mths")
_add("time", "yr", "year (365.25 days)|year", "years (of 365.25 days)|years", 365.25 * 86400, "y|yrs")

# ── temperature ──
_add("temperature", "°C", "degree Celsius", "degrees Celsius", 1.0, "C|degC|deg C|celsius|centigrade|degrees C|degree C|degrees centigrade", "metric", 273.15)
_add("temperature", "°F", "degree Fahrenheit", "degrees Fahrenheit", 5 / 9, "F|degF|deg F|fahrenheit|degrees F|degree F", "us", 459.67)
_add("temperature", "K", "kelvin", "kelvins|kelvin", 1.0, "°K|degK|deg K", "metric")
_add("temperature", "°R", "degree Rankine", "degrees Rankine", 5 / 9, "R|degR|deg R|rankine|Ra", "us", listed=False)

# ── speed ──
_add("speed", "m/s", "metre per second", "metres per second", 1.0, "mps", "metric")
_add("speed", "km/h", "kilometre per hour", "kilometres per hour", 1 / 3.6, "kph|kmh|kmph", "metric")
_add("speed", "mph", "mile per hour", "miles per hour", MI / 3600, "mi/h", "us")
_add("speed", "kn", "knot", "knots", NMI / 3600, "kt|kts", "")
_add("speed", "ft/s", "foot per second", "feet per second", FT, "fps", "us")
_add("acceleration", "gₙ", "standard gravity", "standard gravities", G0, "gee|gees|g0|g-force|g force", listed=False)

# ── force, pressure ──
_metric("force", "N", "newton", "newtons", 1.0, "m k M", listed="- k")
_add("force", "lbf", "pound-force|pound force", "pounds-force|pounds force", LBF, "lb-f|lb f", "us")
_add("force", "kgf", "kilogram-force|kilogram force", "kilograms-force|kilograms force", G0, "kp|kilopond", "metric")
_add("force", "kip", "kip (1,000 lbf)", "kips", 1000 * LBF, "", "us", listed=False)
_add("force", "dyn", "dyne", "dynes", 1e-5, "", "metric", listed=False)
_metric("pressure", "Pa", "pascal", "pascals", 1.0, "h k M G", listed="k M")
_metric("pressure", "bar", "bar", "bar|bars", 1e5, "m", "", listed="- m")
_add("pressure", "mb", "millibar", "millibars", 100.0, "", "metric", listed=False)
_add("pressure", "psi", "pound per square inch", "pounds per square inch", LBF / IN ** 2, "psig|psia|lb/in2|lbs/in2|lb/sq in|lbs/sq in|lbf/in2|lbf/sq in", "us")
_add("pressure", "ksi", "thousand pounds per square inch", "thousand pounds per square inch", 1000 * LBF / IN ** 2, "", "us", listed=False)
_add("pressure", "psf", "pound per square foot", "pounds per square foot", LBF / FT ** 2, "lb/ft2|lbs/ft2|lb/sq ft|lbs/sq ft|lbf/ft2", "us", listed=False)
_add("pressure", "atm", "standard atmosphere|atmosphere", "standard atmospheres|atmospheres", 101325.0, "", "")
_add("pressure", "mmHg", "millimetre of mercury", "millimetres of mercury", 133.322387415, "mm Hg", "metric")
_add("pressure", "Torr", "torr", "torr", 101325 / 760, "torrs", "metric", listed=False)
_add("pressure", "inHg", "inch of mercury", "inches of mercury", 133.322387415 * 25.4, "in Hg|\"Hg", "us")
_add("pressure", "inH2O", "inch of water", "inches of water", 249.08891, "in H2O|inWC|in WC|\"WC|inches water column", "us", listed=False)
_add("pressure", "ftH2O", "foot of water", "feet of water", 249.08891 * 12, "ft H2O|feet of head|ft of head", "us", listed=False)
_add("pressure", "mH2O", "metre of water", "metres of water", 9806.65, "m H2O|metres of head|meters of head", "metric", listed=False)
_add("pressure", "kgf/cm²", "kilogram-force per square centimetre", "kilograms-force per square centimetre", G0 / 1e-4, "kg/cm2|kgf/cm2|at", "metric",
     listed=False)

# ── energy (and torque, which is the same thing on paper), power ──
_metric("energy", "J", "joule", "joules", 1.0, "m k M G", listed="- k M")
_metric("energy", "Wh", "watt-hour|watt hour", "watt-hours|watt hours", 3600.0, "m k M G", listed="- k")
_metric("energy", "cal", "calorie (small)|calorie", "calories (small)|calories", 4.184, "k", listed="k")
_add("energy", "Cal", "food Calorie (kilocalorie)", "food Calories (kilocalories)", 4184.0, "Calorie|Calories|food calorie|food calories|kilocal", "",
     listed=False)
_add("energy", "BTU", "British thermal unit", "British thermal units", BTU, "Btu|BTUs|btus", "us")
_add("energy", "therm", "therm (US)", "therms (US)", 105_480_400.0, "thm", "us", listed=False)
_add("energy", "ft·lbf", "foot-pound", "foot-pounds", LBF * FT, "ft lbf|ft-lbf|ft lb|ft-lb|ft lbs|ft-lbs|lb ft|lb-ft|lbf ft|lbf-ft|foot pound|foot pounds|ftlb|ftlbs",
     "us")
_add("energy", "in·lbf", "inch-pound", "inch-pounds", LBF * IN, "in lbf|in-lbf|in lb|in-lb|in lbs|in-lbs|lb in|lb-in|inch pound|inch pounds", "us")
_add("energy", "N·m", "newton-metre|newton metre|newton-meter|newton meter", "newton-metres|newton metres|newton-meters|newton meters", 1.0,
     "Nm|N m|N-m|N*m", "metric")
_add("energy", "kgf·m", "kilogram-force metre", "kilogram-force metres", G0, "kgf m|kgf-m|kg m|kg-m|kgm", "metric", listed=False)
_metric("energy", "eV", "electronvolt", "electronvolts", 1.602176634e-19, "k M G", listed="", system="")
_add("energy", "t TNT", "tonne of TNT", "tonnes of TNT", 4.184e9, "ton of TNT|tons of TNT|ton TNT|tons TNT", "", listed=False)
_metric("power", "W", "watt", "watts", 1.0, "m k M G", listed="- k M")
_add("power", "hp", "horsepower", "horsepower", 550 * LBF * FT, "HP|bhp|horse power", "us")
_add("power", "PS", "metric horsepower", "metric horsepower", 75 * G0, "cv|CV|metric hp", "metric", listed=False)
_add("power", "BTU/h", "BTU per hour", "BTU per hour", BTU / 3600, "Btu/h|BTU/hr|Btu/hr|btuh|BTUH", "us")
_add("power", "TR", "ton of refrigeration", "tons of refrigeration", 12000 * BTU / 3600, "ton refrigeration|tons refrigeration|RT", "us", listed=False)

# ── flow, density ──
_add("flow", "L/min", "litre per minute", "litres per minute", 1e-3 / 60, "lpm|LPM", "metric")
_add("flow", "L/s", "litre per second", "litres per second", 1e-3, "lps", "metric")
_add("flow", "m³/h", "cubic metre per hour", "cubic metres per hour", 1 / 3600, "m3/h|m3/hr|cmh", "metric")
_add("flow", "gpm", "US gallon per minute", "US gallons per minute", GAL / 60, "GPM|gal/min", "us")
_add("flow", "gph", "US gallon per hour", "US gallons per hour", GAL / 3600, "GPH|gal/h|gal/hr", "us", listed=False)
_add("flow", "cfm", "cubic foot per minute", "cubic feet per minute", FT ** 3 / 60, "CFM|ft3/min", "us")
_add("flow", "cfs", "cubic foot per second", "cubic feet per second", FT ** 3, "CFS|ft3/s|cusec|cusecs", "us", listed=False)
_add("density", "kg/m³", "kilogram per cubic metre", "kilograms per cubic metre", 1.0, "kg/m3", "metric")
_add("density", "g/cm³", "gram per cubic centimetre", "grams per cubic centimetre", 1000.0, "g/cm3|g/cc|g/mL|g/ml|kg/L|kg/l", "metric")
_add("density", "lb/ft³", "pound per cubic foot", "pounds per cubic foot", LB / FT ** 3, "lb/ft3|lbs/ft3|pcf|lb/cu ft|lbs/cu ft", "us")
_add("density", "lb/gal", "pound per US gallon", "pounds per US gallon", LB / GAL, "lbs/gal|ppg", "us")

# ── data ──
_metric("data", "B", "byte", "bytes", 8.0, "k M G T P Ki Mi Gi Ti", "", listed="- k M G T Mi Gi Ti", system="")
_metric("data", "bit", "bit", "bits", 1.0, "k M G", "b", listed="- M G", system="")
_add("data", "KB", "kilobyte", "kilobytes", 8e3, "", "", listed=False)
_metric("data rate", "bit/s", "bit per second", "bits per second", 1.0, "k M G", "bps", listed="k M G", system="")
_add("data rate", "kbps", "kilobit per second", "kilobits per second", 1e3, "Kbps|kb/s", "", listed=False)
_add("data rate", "Mbps", "megabit per second", "megabits per second", 1e6, "Mb/s|mbit/s", "", listed=False)
_add("data rate", "Gbps", "gigabit per second", "gigabits per second", 1e9, "Gb/s|gbit/s", "", listed=False)
_add("data rate", "MB/s", "megabyte per second", "megabytes per second", 8e6, "MBps", "", listed=True)

# ── angle ──
_add("angle", "°", "degree", "degrees", PI / 180, "deg|degs|degree of arc|degrees of arc")
_add("angle", "rad", "radian", "radians", 1.0, "rads")
_add("angle", "mil", "mil (NATO, 6,400 to a circle)|mil", "mils (NATO, 6,400 to a circle)|mils", 2 * PI / 6400, "NATO mil|NATO mils|angular mil|angular mils|mils NATO")
_add("angle", "′", "minute of arc|arcminute", "minutes of arc|arcminutes", PI / 10800, "arcmin|arc minute|arc minutes|MOA|moa", listed=False)
_add("angle", "″", "second of arc|arcsecond", "seconds of arc|arcseconds", PI / 648000, "arcsec|arc second|arc seconds", listed=False)
_add("angle", "gon", "gon (grad)", "gons (grads)", PI / 200, "grad|grads|gradian|gradians", listed=False)
_add("angle", "turn", "full turn", "full turns", 2 * PI, "rev|revs|revolution|revolutions|turns|circle|circles", listed=False)
_add("turning speed", "rpm", "revolution per minute", "revolutions per minute", 2 * PI / 60, "RPM|rev/min|r/min")
_add("turning speed", "rad/s", "radian per second", "radians per second", 1.0)
_add("turning speed", "°/s", "degree per second", "degrees per second", PI / 180, "deg/s")

# ── fuel ──
_add("fuel economy", "mpg", "mile per US gallon", "miles per US gallon", MI / GAL, "MPG|mpg US|mi/gal|miles per gallon", "us")
_add("fuel economy", "mpg (imp)", "mile per imperial gallon", "miles per imperial gallon", MI / IMP_GAL, "mpg imp|mpg UK|imp mpg|UK mpg|mi/imp gal", "uk")
_add("fuel economy", "km/L", "kilometre per litre", "kilometres per litre", 1000 / 1e-3, "km/l|kmpl|kpl", "metric")
_add("fuel use", "L/100 km", "litre per 100 kilometres", "litres per 100 kilometres", 1e-3 / 1e5, "L/100km|l/100km|l/100 km", "metric")
_add("fuel use", "gal/100 mi", "US gallon per 100 miles", "US gallons per 100 miles", GAL / (100 * MI), "gal/100mi", "us", listed=False)

# ── radiation ──
_metric("radiation dose", "Sv", "sievert", "sieverts", 1.0, "n µ m", listed="- m µ")
_metric("radiation dose", "rem", "rem", "rem|rems", 0.01, "µ m", listed="- m", system="us")
_add("dose rate", "Sv/h", "sievert per hour", "sieverts per hour", 1 / 3600, "Sv/hr", "metric", listed=False)
_metric("absorbed dose", "Gy", "gray", "grays", 1.0, "µ m c", listed="- m")
_add("absorbed dose", "rad", "rad (absorbed dose)", "rad (absorbed dose)", 0.01, "rads|rd", "us")
_add("absorbed dose", "mrad", "millirad (absorbed dose)", "millirad (absorbed dose)", 1e-5, "", "us", listed=False)
_metric("radioactivity", "Bq", "becquerel", "becquerels", 1.0, "k M G T", listed="- k M G")
_metric("radioactivity", "Ci", "curie", "curies", 3.7e10, "n µ m", listed="- m µ", system="us")
_add("rate", "Hz", "hertz", "hertz", 1.0, "cps", listed=False)

_COMMON: Dict[str, List[str]] = {
    "length": ["mm", "cm", "m", "km", "in", "ft", "yd", "mi"], "area": ["cm²", "m²", "ha", "km²", "in²", "ft²", "acre", "mi²"],
    "volume": ["mL", "L", "m³", "tsp", "tbsp", "fl oz", "cup", "qt", "gal", "ft³", "yd³"], "weight": ["mg", "g", "kg", "t", "lb", "oz", "short ton"],
    "time": ["s", "min", "h", "d", "wk", "yr"], "temperature": ["°C", "°F", "K"], "speed": ["km/h", "m/s", "mph", "kn", "ft/s"],
    "force": ["N", "kN", "lbf", "kgf"], "pressure": ["kPa", "bar", "psi", "inHg", "atm"], "energy": ["J", "kJ", "kWh", "kcal", "BTU", "ft·lbf", "N·m"],
    "power": ["W", "kW", "hp", "BTU/h"], "flow": ["L/min", "m³/h", "gpm", "cfm"], "density": ["kg/m³", "g/cm³", "lb/ft³", "lb/gal"],
    "data": ["kB", "MB", "GB", "TB", "MiB", "GiB", "TiB"], "data rate": ["Mbit/s", "MB/s"], "angle": ["°", "rad", "mil"],
    "turning speed": ["rpm", "rad/s", "°/s"], "fuel economy": ["mpg", "km/L", "mpg (imp)"], "fuel use": ["L/100 km"],
    "radiation dose": ["mSv", "µSv", "Sv", "rem", "mrem"], "absorbed dose": ["Gy", "rad"], "radioactivity": ["Bq", "kBq", "MBq", "Ci", "mCi", "µCi"],
}
_FUEL = {KINDS["fuel economy"]: KINDS["fuel use"], KINDS["fuel use"]: KINDS["fuel economy"]}


# ── finding a unit ─────────────────────────────────────────────────────────

def kind_of(vector: Vector) -> str:
    return _KIND_OF.get(vector, "")


def _simple(text: str) -> List[Unit]:
    key = _key(text)
    if key in _EXACT:
        return list(_EXACT[key])
    found = list(_LOOSE.get(key.lower(), []))
    if not found and key.lower().startswith(("a ", "an ", "one ")):
        found = _simple(key.split(" ", 1)[1])
    return found


def find(text: str) -> List[Unit]:
    """Every unit ``text`` could mean, likeliest first; ``a/b`` and ``a per b`` are built from their two halves."""
    key = _key(text)
    if not key:
        return []
    whole = _simple(key)
    if whole:
        return whole
    match = re.fullmatch(r"(.+?)\s*(?:/|\bper\b|\ba\b|\ban\b)\s*(.+)", key)
    if not match:
        return []
    out: List[Unit] = []
    for top in _simple(match.group(1)):
        for bottom in _simple(match.group(2)):
            if top.offset and bottom.vector != KINDS["time"]:
                continue
            vector = tuple(a - b for a, b in zip(top.vector, bottom.vector))
            factor = top.factor / bottom.factor
            if vector == _v(L=-2):
                vector = KINDS["fuel economy"]          # miles per gallon, not "one over an area"
            elif vector == _v(L=2) and top.vector == KINDS["volume"]:
                vector = KINDS["fuel use"]
            if vector == _NONE:
                continue
            system = top.system if top.system == bottom.system or not bottom.system else (bottom.system if not top.system else "")
            out.append(Unit(f"{top.symbol}/{bottom.symbol}", f"{top.one} per {bottom.one}", f"{top.many} per {bottom.one}", vector, factor, 0.0, system))
    return out


def _pair(sources: List[Unit], targets: List[Unit]) -> Optional[Tuple[Unit, Unit]]:
    for source in sources:
        for target in targets:
            if source.vector == target.vector or _FUEL.get(source.vector) == target.vector:
                return source, target
    return None


# ── reading an amount ──────────────────────────────────────────────────────

_FRACTIONS = {"½": 1 / 2, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 1 / 4, "¾": 3 / 4, "⅕": 1 / 5, "⅖": 2 / 5, "⅗": 3 / 5, "⅘": 4 / 5, "⅙": 1 / 6, "⅚": 5 / 6,
              "⅛": 1 / 8, "⅜": 3 / 8, "⅝": 5 / 8, "⅞": 7 / 8}
_GLYPHS = "".join(_FRACTIONS)
_NUMBER = re.compile(rf"""[-+]?(?:
      \d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?        # 12,345.6
    | \d+\s+\d+/\d+                           # 3 1/2
    | \d+/\d+                                 # 1/4
    | \d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?        # 3.5   3,5   1e6
    | \.\d+
    )[{_GLYPHS}]? | [-+]?[{_GLYPHS}]""", re.X)
_NEXT_NUMBER = re.compile(rf"\s+(?=[-+]?(?:\d|\.\d|[{_GLYPHS}]))")


def _number(text: str) -> float:
    text = text.strip()
    extra = 0.0
    if text and text[-1] in _FRACTIONS:
        extra, text = _FRACTIONS[text[-1]], text[:-1].strip()
        if text in ("", "+", "-"):
            return -extra if text == "-" else extra
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        value = float(text.replace(",", ""))
    elif " " in text or "/" in text:
        whole, _, fraction = text.rpartition(" ")
        top, bottom = fraction.split("/")
        if not float(bottom):
            raise UnitError("A fraction cannot have zero underneath.")
        value = (float(whole) if whole.strip() else 0.0) + float(top) / float(bottom)
    else:
        value = float(text.replace(",", "."))
    return sign * (value + extra)


def quantities(text: str) -> List[Tuple[float, str]]:
    """``"5 ft 10 in"`` → ``[(5.0, "ft"), (10.0, "in")]``. A unit is whatever stands between one number and the next."""
    text = str(text or "").replace("−", "-").replace("–", "-").strip()
    text = re.sub(r"(/|\bper\s+)100\s*(km|mi)\b", lambda found: f"{found.group(1)}100{found.group(2)}", text)
    text = re.sub(r"(\d)\s*(?:''|\"|″|”)", r"\1 in ", text)                   # 6'2" — feet and inches
    text = re.sub(r"(\d)\s*(?:'|′|’)(?=\s*(?:\d|$))", r"\1 ft ", text)
    text = re.sub(r"\s+", " ", text).strip()
    out: List[Tuple[float, str]] = []
    position = 0
    while position < len(text):
        skip = re.compile(r"(?:\s|,|\band\b|\bplus\b)*").match(text, position)
        position = skip.end() if skip else position
        if position >= len(text):
            break
        match = _NUMBER.match(text, position)
        if not match:
            raise UnitError(f"I could not read a number at “{text[position:position + 20]}”. Write an amount like 5 ft 10 in, 72 F or 2.5 kg.")
        value = _number(match.group(0))
        position = match.end()
        following = _NEXT_NUMBER.search(text, position)
        end = following.start() if following else len(text)
        unit = re.sub(r"(?i)[\s,]*\b(?:and|plus)\s*$", "", text[position:end]).strip(" ,")
        if unit.lower().startswith("of "):
            unit = unit[3:]
        out.append((value, unit))
        position = end
    if not out:
        raise UnitError("There is no amount there. Write one like 5 ft 10 in, 72 F or 2.5 kg.")
    return out


def split_request(text: str) -> Tuple[str, str]:
    """``"5 ft 10 in to cm"`` → ``("5 ft 10 in", "cm")``; no target gives ``""``."""
    text = re.sub(r"\s+", " ", str(text or "")).strip().rstrip("?").strip()
    text = re.sub(r"(?i)^(?:convert|what is|what's|how much is|how many is)\s+", "", text)
    for pattern in (r"(?<=\s)(?:to|into|as|=|->|=>|→)(?=\s)", r"(?<=\s)in(?=\s)"):
        found = list(re.finditer(pattern, text, flags=re.I))
        if found and text[:found[-1].start()].strip() and text[found[-1].end():].strip():
            return text[:found[-1].start()].strip(), text[found[-1].end():].strip()
    return text, ""


# ── writing a number ───────────────────────────────────────────────────────

def show(value: float, digits: int = 7) -> str:
    """A number to ``digits`` significant figures, without the noise: 177.8, 1,609.344, 0.0254, 3.2e-12."""
    if value == 0 or not math.isfinite(value):
        return "0" if value == 0 else str(value)
    size = abs(value)
    if size >= 1e15 or size < 1e-6:
        mantissa, exponent = f"{value:.{digits - 1}e}".split("e")
        return f"{mantissa.rstrip('0').rstrip('.')}e{int(exponent)}"
    places = max(0, digits - 1 - math.floor(math.log10(size)))
    text = f"{value:,.{min(places, 12)}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _named(value: float, unit: Unit) -> str:
    return unit.one if abs(value) == 1 else unit.many


_WORDS = {"cup": "cups", "acre": "acres", "rod": "rods", "chain": "chains", "furlong": "furlongs", "league": "leagues", "hand": "hands",
          "fathom": "fathoms", "klick": "klicks", "cord": "cords", "board foot": "board feet", "acre-foot": "acre-feet", "short ton": "short tons",
          "long ton": "long tons", "slug": "slugs", "therm": "therms", "turn": "turns", "section": "sections", "kip": "kips",
          "fortnight": "fortnights", "metric cup": "metric cups"}


def _written(value: float, unit: Unit) -> str:
    gap = "" if unit.symbol in ("°", "′", "″") else " "
    symbol = _WORDS.get(unit.symbol, unit.symbol) if abs(value) != 1 else unit.symbol
    return f"{show(round(value, 2) if unit.offset or unit.symbol == 'K' else value)}{gap}{symbol}"


def _plausible(value: float) -> bool:
    return value == 0 or 1e-6 <= abs(value) < 1e9


# ── converting ─────────────────────────────────────────────────────────────

def _to_si(value: float, unit: Unit) -> float:
    return (value + unit.offset) * unit.factor


def _from_si(value: float, unit: Unit) -> float:
    return value / unit.factor - unit.offset


def _targets(text: str) -> List[List[Unit]]:
    """What ``to`` names: one unit, or several of a kind for an answer like 5 ft 10 in."""
    whole = find(text)
    if whole:
        return [whole]
    pieces = [piece for piece in re.split(r"\s*(?:,|\+|&|\band\b|\s)\s*", _key(text)) if piece]
    found = [find(piece) for piece in pieces]
    if len(pieces) < 2 or len(pieces) > 4 or not all(found):
        return []
    return found


def convert(amount: str, to: str = "", source: str = "") -> Dict[str, object]:
    """Convert ``amount`` (a number with its unit, or several: ``5 ft 10 in``)
    into ``to``. ``source`` names the unit when ``amount`` is a bare number.
    With no ``to``, the answer gives the same quantity in the usual units of
    the other system of measurement.

    Returns ``value``, ``unit`` (symbol), ``text`` (the sentence), ``kind``,
    ``from`` (the amount as read), ``also`` (other ways of writing it), ``note``.
    """
    amount = str(amount if amount is not None else "").strip()
    if not str(to or "").strip():
        amount, to = split_request(amount)
    read = quantities(amount)
    if source and len(read) == 1 and not read[0][1]:
        read = [(read[0][0], str(source))]
    if any(not unit for _value, unit in read):
        raise UnitError("Say what the number is a number of: 5 ft, 72 F, 2.5 kg.")
    choices = []
    for _value, unit in read:
        found = find(unit)
        if not found:
            raise UnitError(f"I do not know the unit “{unit[:30]}”.")
        choices.append(found)
    targets = _targets(to) if str(to or "").strip() else []
    if str(to or "").strip() and not targets:
        raise UnitError(f"I do not know the unit “{str(to).strip()[:30]}”.")

    first: Optional[Unit] = None
    target_units: List[Unit] = []
    reread = ""
    if targets:
        fits = []
        for candidate in choices[0]:
            picked = [next((unit for unit in options if unit.vector == candidate.vector or _FUEL.get(candidate.vector) == unit.vector), None)
                      for options in targets]
            if all(picked) and len({unit.vector for unit in picked if unit}) == 1:
                fits.append((candidate, [unit for unit in picked if unit]))
        if fits:
            first, target_units = fits[0]
            if len(fits) > 1 and len(read) == 1 and not first.offset and first.vector == target_units[0].vector:
                # The same letters for two units of one kind: take the reading that gives a number a person would write.
                for candidate, units in fits:
                    if _plausible(read[0][0] * candidate.factor / units[0].factor):
                        if candidate is not first:
                            reread = f"Read “{read[0][1]}” as {candidate.many}, not {first.many}."
                        first, target_units = candidate, units
                        break
        if first is None:
            have, want = choices[0][0], targets[0][0]
            raise UnitError(f"{have.many.capitalize()} measure {kind_of(have.vector) or 'one thing'} and {want.many} measure "
                            f"{kind_of(want.vector) or 'another'}: one cannot be turned into the other.")
    else:
        first = choices[0][0]
    sources: List[Unit] = []
    for index, options in enumerate(choices):
        unit = first if index == 0 else next((unit for unit in options if unit.vector == first.vector), None)
        if unit is None:
            raise UnitError(f"{options[0].many.capitalize()} and {first.many} are different kinds of thing and cannot be added together.")
        sources.append(unit)
    if len(sources) > 1 and any(unit.offset for unit in sources):
        raise UnitError("Give a temperature as one number.")
    total = sum(_to_si(value, unit) if len(sources) == 1 else value * unit.factor for (value, _text), unit in zip(read, sources))
    kind = kind_of(first.vector)
    said = " ".join(_written(value, unit) for (value, _text), unit in zip(read, sources))
    note = reread
    others = [unit for unit in choices[0] if unit is not first and unit.symbol != first.symbol]
    if others and not targets:
        note = f"Read “{read[0][1]}” as {first.many}; it can also mean {others[0].many}."
    names = " and ".join(dict.fromkeys(unit.many for unit in sources))
    spelled = "" if all(_WORDS.get(unit.symbol, unit.symbol) in (unit.many, unit.one) for unit in sources) else \
        " (" + " and ".join(dict.fromkeys(_named(value, unit) for (value, _text), unit in zip(read, sources))) + ")"

    def into(unit: Unit) -> float:
        if unit.vector != first.vector:                 # miles per gallon ↔ litres per 100 km: one is the other upside down
            if total == 0:
                raise UnitError("Zero cannot be turned upside down: no distance on any fuel is not a rate of use.")
            return 1 / total / unit.factor
        return _from_si(total, unit)

    if len(target_units) > 1:
        ordered = sorted(target_units, key=lambda unit: -unit.factor)
        if any(unit.offset for unit in ordered):
            raise UnitError("A temperature is given in one unit.")
        left = abs(total)
        smallest = ordered[-1]
        left = round(left / smallest.factor, 4) * smallest.factor       # so 5 ft 12 in never appears
        parts = []
        for unit in ordered[:-1]:
            whole = math.floor(left / unit.factor + 1e-9)
            parts.append((float(whole), unit))
            left -= whole * unit.factor
        parts.append((round(max(left, 0.0) / smallest.factor, 4), smallest))
        text = ("-" if total < 0 else "") + " ".join(_written(value, unit) for value, unit in parts)
        return {"value": into(ordered[0]), "unit": " ".join(unit.symbol for unit in ordered), "answer": text, "kind": kind, "from": said,
                "also": [], "note": note, "text": f"{said} = {text} — {names} to {' and '.join(unit.many for unit in ordered)}"}

    if target_units:
        target = target_units[0]
        value = into(target)
        answer = _written(value, target)
        return {"value": value, "unit": target.symbol, "answer": answer, "kind": kind_of(target.vector) or kind, "from": said, "also": [], "note": note,
                "text": f"{said} = {answer} — {names} to {target.many}"}

    # No target: the usual units of the other system (or simply the other usual ones).
    system = sources[0].system
    seen = {unit.symbol for unit in sources}
    candidates: List[Tuple[float, float, Unit]] = []
    pools = [first.vector] + ([_FUEL[first.vector]] if first.vector in _FUEL else [])
    for vector in pools:
        for symbol in _COMMON.get(kind_of(vector), []):
            unit = next((known for known in _EXACT.get(_key(symbol), []) if known.vector == vector), None)
            if unit is None or unit.symbol in seen:
                continue
            if system and unit.system == system and kind not in ("temperature",):
                continue
            try:
                value = into(unit)
            except UnitError:
                continue
            size = abs(value)
            if unit.offset or unit.symbol == "K" or size == 0 or 1 <= size < 1000:
                candidates.append((0.0, value, unit))
            elif 0.01 <= size < 100_000:
                candidates.append((abs(math.log10(size)) if size < 1 else math.log10(size) - 3, value, unit))
    candidates.sort(key=lambda item: item[0])                           # stable: the usual order within the same rank
    also = [{"value": value, "unit": unit.symbol, "text": _written(value, unit), "name": _named(value, unit)} for _rank, value, unit in candidates[:3]]
    if not also:
        base = _from_si(total, first) if len(sources) == 1 else total / first.factor
        return {"value": base, "unit": first.symbol, "answer": said, "kind": kind, "from": said, "also": [], "note": note,
                "text": f"{said} — {show(base)} {_named(base, first)}. Say what to convert it to."}
    return {"value": also[0]["value"], "unit": also[0]["unit"], "answer": also[0]["text"], "kind": kind, "from": said, "also": also, "note": note,
            "text": f"{said}{spelled} = " + " = ".join(item["text"] for item in also)}


def listing() -> List[Dict[str, object]]:
    """For a menu: the kinds of quantity and the usual units of each."""
    return [{"kind": kind, "units": [{"symbol": unit.symbol, "name": unit.many} for unit in units]} for kind, units in _BY_KIND.items()]
