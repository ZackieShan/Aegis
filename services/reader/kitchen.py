"""The kitchen: a recipe set out for cooking from.

A recipe arrives as words — pasted, picked out of a library page, taken from
a web page — and often old ones ("butter the size of an egg", "bake in a
quick oven"). It is kept three ways:

* **as written** — the original text, untouched;
* **for today** — every ingredient as an amount a kitchen measures now, in
  cups and spoons and in grams or millilitres, with the old wording beside it
  and anything that was a judgement marked as an estimate;
* **step by step** — the method one action at a time, with the minutes to
  wait and the oven temperature where the recipe gives (or implies) one.

The model reads the recipe and separates it (``_ask_model``); the numbers are
then worked out here, the same way every time: unit conversion, weights from
volumes for the common ingredients, °F to °C. With no model to ask, the
recipe is still kept and split into steps, and it says that is all it is.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.database import SessionLocal, utcnow_naive

from .content import ReaderError
from .models import KitchenRecipe, epoch

logger = logging.getLogger(__name__)

MAX_TEXT = 24_000


def _units_from_the_almanac() -> Dict[str, Tuple[str, float]]:
    """The kitchen's measures, read from the Almanac's unit table
    (services/almanac/units.py), so that a cup is the same cup in a recipe and
    in a conversion: unit → (kind, how many ml or grams one is)."""
    from services.almanac import units as table

    symbols = {"teaspoon": "tsp", "tablespoon": "tbsp", "fluid ounce": "fl oz", "cup": "cup", "pint": "pt", "quart": "qt",
               "gallon": "gal", "millilitre": "mL", "litre": "L", "ounce": "oz", "pound": "lb", "gram": "g", "kilogram": "kg"}
    volume, weight = table.KINDS["volume"], table.KINDS["weight"]
    out: Dict[str, Tuple[str, float]] = {}
    for name, symbol in symbols.items():
        unit = next(found for found in table.find(symbol) if found.vector in (volume, weight))
        out[name] = ("volume", unit.factor * 1e6) if unit.vector == volume else ("weight", unit.factor * 1e3)
    return out


UNITS: Dict[str, Tuple[str, float]] = _units_from_the_almanac()
ML_PER_CUP = UNITS["cup"][1]
_UNIT_NAMES = {
    "teaspoon": "tsp t teaspoons teaspoonful teaspoonfuls tsps tsp.",
    "tablespoon": "tbsp tbs tb tablespoons tablespoonful tablespoonfuls tbsps tbsp.",
    "fluid ounce": "floz fl.oz fluidounce fluidounces fl-oz",
    "cup": "c cups cupful cupfuls c.", "pint": "pt pints pt.", "quart": "qt quarts qt.", "gallon": "gal gallons gal.",
    "millilitre": "ml milliliter milliliters millilitres cc", "litre": "l liter liters litres",
    "ounce": "oz ounces oz.", "pound": "lb lbs pounds lb. lbs.", "gram": "g gm grams gramme grammes gr",
    "kilogram": "kg kilo kilos kilograms kilogramme",
}
_ALIASES = {alias: unit for unit, names in _UNIT_NAMES.items() for alias in names.split()}
_PLURAL = {"cup": "cups", "pint": "pints", "quart": "quarts", "gallon": "gallons", "ounce": "ounces", "pound": "pounds",
           "teaspoon": "teaspoons", "tablespoon": "tablespoons", "fluid ounce": "fluid ounces"}

# grams in a US cup, most particular name first (a cup of flour is not a cup of sugar)
GRAMS_PER_CUP: List[Tuple[str, float]] = [
    (r"powdered sugar|confectioner|icing sugar", 115), (r"brown sugar", 213), (r"sugar", 200),
    (r"corn\s?meal|indian meal", 140), (r"corn\s?starch|cornflour", 120), (r"cocoa", 85),
    (r"rolled oats|oatmeal|\boats\b", 90), (r"whole[- ]wheat flour|graham flour", 120), (r"flour", 120),
    (r"cream of tartar", 160), (r"peanut butter|nut butter", 270),
    (r"butter|margarine|oleo", 227), (r"lard|shortening|suet|drippings", 205), (r"\boil\b", 218),
    (r"honey|molasses|treacle|syrup", 335), (r"buttermilk|sour milk|milk|cream|yogh?urt", 242),
    (r"water|stock|broth|vinegar|wine|juice|coffee|\btea\b", 237), (r"rice", 185), (r"raisins|currants", 150),
    (r"walnuts|pecans|almonds|\bnuts\b", 120), (r"bread ?crumbs", 110), (r"\bsalt\b", 290), (r"baking soda|saleratus|\bsoda\b", 220),
    (r"baking powder", 190),
]

# What old cookery books mean. (words, amount, unit, what to tell the cook)
OLD_MEASURES: List[Tuple[str, float, str, str]] = [
    (r"wine ?glass(?:ful)?", 0.25, "cup", "A wineglass was about ¼ cup (2 fl oz)."),
    (r"gill", 0.5, "cup", "A gill is ½ cup (4 fl oz) in American books; a British gill is a little more (about ⅝ cup)."),
    (r"tea ?cup(?:ful)?", 0.5, "cup", "A teacup was about ½ cup in most 19th-century American books; some mean ¾ cup. Start with ½ and add if the mixture looks dry."),
    (r"coffee ?cup(?:ful)?|tumbler(?:ful)?", 1.0, "cup", "A coffee cup or tumbler was about 1 cup (8 fl oz)."),
    (r"breakfast ?cup(?:ful)?", 1.25, "cup", "A British breakfast cup is about 1¼ US cups (10 imperial fl oz)."),
    (r"dessert ?spoon(?:ful)?", 2.0, "teaspoon", "A dessertspoon is 2 teaspoons."),
    (r"salt ?spoon(?:ful)?", 0.25, "teaspoon", "A saltspoon is ¼ teaspoon."),
    (r"dra(?:ch)?m", 0.75, "teaspoon", "A dram (drachm) is about ¾ teaspoon."),
    (r"(?:piece of )?butter (?:the )?size of an egg", 0.25, "cup", "Butter “the size of an egg” is about ¼ cup (2 oz, 57 g)."),
    (r"(?:piece of )?butter (?:the )?size of a walnut", 2.0, "tablespoon", "Butter “the size of a walnut” is about 1–2 tablespoons."),
    (r"peck", 8.0, "quart", "A peck is 8 quarts (2 gallons) of dry goods."),
]
# What the oven words meant: (words, what it is called, °F low, °F high)
OVENS: List[Tuple[str, str, int, int]] = [
    (r"very (?:slow|cool) oven", "a very slow oven", 250, 275), (r"(?:slow|cool|slack) oven", "a slow oven", 300, 325),
    (r"moderately hot oven", "a moderately hot oven", 375, 400), (r"very (?:hot|quick) oven", "a very hot oven", 450, 475),
    (r"moderate oven", "a moderate oven", 350, 375), (r"(?:quick|hot|brisk) oven", "a quick or hot oven", 400, 450),
]
OLD_WORDS: List[Tuple[str, str]] = [
    (r"saleratus", "Saleratus is baking soda."),
    (r"pearl ?ash", "Pearlash was the leavening before baking soda; use the same amount of baking soda."),
    (r"hartshorn|baker'?s ammonia", "Hartshorn is baker's ammonia (ammonium carbonate). It is still sold; baking powder is the usual stand-in."),
    (r"sweet milk", "“Sweet milk” is ordinary fresh whole milk (as opposed to sour milk)."),
    (r"sour milk", "For “sour milk”, use buttermilk, or stir 1 tablespoon of vinegar or lemon juice into each cup of milk."),
    (r"indian meal", "“Indian meal” is cornmeal."),
    (r"graham flour", "Graham flour is coarse whole-wheat flour."),
    (r"isinglass", "Isinglass set jellies before gelatine; use unflavoured gelatine."),
    (r"loaf sugar", "Loaf sugar is ordinary white sugar, sold then as a hard cone and crushed."),
    (r"treacle", "Treacle is molasses (dark treacle) or golden syrup (light)."),
    (r"sweet oil", "“Sweet oil” is olive oil."),
    (r"\boleo\b", "Oleo is margarine."),
    (r"\bsoda\b", "“Soda” in a recipe is baking soda."),
]

_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875}
_GLYPHS = [(0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.375, "⅜"), (0.5, "½"), (0.625, "⅝"), (2 / 3, "⅔"), (0.75, "¾"), (0.875, "⅞")]


class KitchenError(ReaderError):
    """Something to tell the cook in plain words."""


# ── numbers ───────────────────────────────────────────────────────────────

def unit_name(text: Any) -> str:
    """The unit a word means ("tbsp", "Tablespoonfuls" → "tablespoon"), or ""."""
    word = re.sub(r"\s+", " ", str(text or "")).strip()
    if word in ("T", "T."):                          # a capital T is a tablespoon, a small one a teaspoon
        return "tablespoon"
    lowered = word.lower()
    return lowered if lowered in UNITS else _ALIASES.get(lowered.replace(" ", ""), _ALIASES.get(lowered, ""))


def number(value: Any) -> Optional[float]:
    """A quantity from a number or from "1 1/2", "2½", "¾"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    total, found = 0.0, False
    for glyph, amount in _FRACTIONS.items():
        if glyph in text:
            total, found, text = total + amount, True, text.replace(glyph, " ")
    for top, bottom in re.findall(r"(\d+)\s*/\s*(\d+)", text):
        if int(bottom):
            total, found = total + int(top) / int(bottom), True
    text = re.sub(r"\d+\s*/\s*\d+", " ", text)
    whole = re.search(r"\d+(?:\.\d+)?", text)
    if whole:
        total, found = total + float(whole.group(0)), True
    return total if found and total > 0 else None


def grams_per_cup(item: str) -> Optional[float]:
    name = str(item or "").lower()
    for pattern, grams in GRAMS_PER_CUP:
        if re.search(pattern, name):
            return float(grams)
    return None


def measure(amount: Optional[float], unit: str, item: str = "") -> Dict[str, Optional[float]]:
    """``{"ml", "grams"}`` for an amount: millilitres for a volume, grams for
    a weight — and for a volume of something whose weight per cup is known."""
    kind, size = UNITS.get(unit, ("", 0.0))
    if not amount or not kind:
        return {"ml": None, "grams": None}
    if kind == "weight":
        return {"ml": None, "grams": round(amount * size, 1)}
    millilitres = amount * size
    density = grams_per_cup(item)
    return {"ml": round(millilitres, 1), "grams": round(millilitres / ML_PER_CUP * density, 1) if density else None}


def amount_text(amount: Optional[float]) -> str:
    """1.5 → "1 ½": the way a measuring cup is read."""
    if not amount:
        return ""
    whole = int(amount)
    rest = amount - whole
    if rest < 0.06:
        return str(whole) if whole else "a little"
    if rest > 0.94:
        return str(whole + 1)
    glyph = min(_GLYPHS, key=lambda pair: abs(pair[0] - rest))[1]
    return f"{whole} {glyph}" if whole else glyph


def kitchen_text(amount: Optional[float], unit: str) -> str:
    """"1 ½ cups", "2 tablespoons", "3" (things counted)."""
    said = amount_text(amount)
    if not unit or not said:
        return said
    return f"{said} {_PLURAL.get(unit, unit) if (amount or 0) > 1 else unit}"


def _round(value: float) -> str:
    step = 1 if value < 20 else (5 if value < 250 else 10)
    return str(int(value / step + 0.5) * step)            # halves go up, as the page rounds them


def metric_text(ml: Optional[float], grams: Optional[float]) -> str:
    """"57 g", "1.2 kg", "120 ml" — weight where it is known."""
    if grams:
        return f"{grams / 1000:.1f} kg".replace(".0 ", " ") if grams >= 1000 else f"{_round(grams)} g"
    if ml:
        return f"{ml / 1000:.1f} l".replace(".0 ", " ") if ml >= 1000 else f"{_round(ml)} ml"
    return ""


def celsius(fahrenheit: float) -> int:
    return int(round((fahrenheit - 32) * 5 / 9 / 5) * 5)


def oven_for(text: str) -> Optional[Dict[str, Any]]:
    """The oven a piece of text asks for: a stated temperature, or an old
    oven word with what it meant."""
    said = re.search(r"(?:(\d{3})\s*(?:°\s*F?)?\s*(?:-|–|—|to)\s*)?(\d{3})\s*(?:°|degrees?|deg\.?)?\s*(?:F\b|Fahrenheit)", text or "", re.I)
    if said and 150 <= int(said.group(2)) <= 550:
        heat, low = int(said.group(2)), int(said.group(1) or 0)
        if 150 <= low < heat:                             # "350–375 °F": a range
            return {"f": low, "f_to": heat, "c": celsius(low), "c_to": celsius(heat), "word": ""}
        return {"f": heat, "c": celsius(heat), "word": ""}
    metric = re.search(r"(\d{2,3})\s*(?:°|degrees?|deg\.?)?\s*(?:C\b|Celsius|centigrade)", text or "", re.I)
    if metric and 60 <= int(metric.group(1)) <= 300:
        heat = int(metric.group(1))
        return {"f": int(round((heat * 9 / 5 + 32) / 5) * 5), "c": heat, "word": ""}
    for pattern, _label, low, high in OVENS:
        found = re.search(pattern, text or "", re.I)
        if found:
            return {"f": low, "f_to": high, "c": celsius(low), "c_to": celsius(high), "word": found.group(0).lower()}
    return None


def minutes_in(text: str) -> Tuple[int, int]:
    """(minutes, up to) a step says to wait: "ten to twelve minutes" → (10, 12)."""
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60,
             "half an": 0.5, "half a": 0.5, "an": 1, "a": 1}
    count = (r"(\d+\s+\d+\s*/\s*\d+|\d+\s*[½¼¾]|\d+(?:\.\d+)?|\d+\s*/\s*\d+|[½¼¾]|"
             + "|".join(sorted(words, key=len, reverse=True)) + r")")
    found = re.search(rf"\b{count}(?:\s*(?:to|or|-|–|—)\s*{count})?\s*(minutes?|mins?\.?|hours?|hrs?\.?)\b", text or "", re.I)
    if not found:
        return 0, 0

    def value(token: Optional[str]) -> float:
        if not token:
            return 0.0
        token = token.lower().strip()
        return float(words[token]) if token in words else (number(token) or 0.0)

    scale = 60 if found.group(3).lower().startswith("h") else 1
    low, high = value(found.group(1)) * scale, value(found.group(2)) * scale
    return int(round(low)), int(round(high)) if high > low else 0


def old_notes(text: str) -> List[str]:
    """What a modern cook should know about the old words in a recipe."""
    notes: List[str] = []
    lowered = str(text or "").lower()
    for pattern, _amount, _unit, note in OLD_MEASURES:
        if re.search(rf"\b(?:{pattern})s?\b", lowered):
            notes.append(note)
    for pattern, _label, low, high in OVENS:
        found = re.search(pattern, lowered)
        if found:
            notes.append(f"A “{found.group(0)}” is about {low}–{high} °F ({celsius(low)}–{celsius(high)} °C).")
            break
    for pattern, note in OLD_WORDS:
        if re.search(pattern, lowered) and note not in notes:
            if pattern == r"\bsoda\b" and "saleratus" in lowered:
                continue
            notes.append(note)
    return notes


# ── setting a recipe out ──────────────────────────────────────────────────

def _old_measure_table() -> str:
    rows = [f"- {note}" for _p, _a, _u, note in OLD_MEASURES]
    rows += [f"- {label}: {low}–{high} °F" for _p, label, low, high in OVENS]
    return "\n".join(rows)


SYSTEM_PROMPT = """You set a recipe out for someone to cook from today. You are given the recipe exactly as it was written — it may be old, and written as a single paragraph.

Reply with ONE JSON object and nothing else:
{
  "title": "what the dish is called",
  "year": "the year it is from, if the text or its source says so, else \\"\\"",
  "makes": "how much it makes, if said or plainly implied, else \\"\\"",
  "ingredients": [
    {"as_written": "the words the recipe uses for this ingredient and its amount",
     "item": "the ingredient, in today's name (baking soda, not saleratus)",
     "amount": 0.25, "unit": "cup",
     "prep": "softened / sifted / chopped … or \\"\\"",
     "estimate": true,
     "note": "only when the cook needs it: why this is an estimate, or what replaced an ingredient no longer sold"}
  ],
  "steps": [
    {"do": "one action, in plain words of today, complete enough to follow without the original",
     "as_written": "the part of the original this step comes from",
     "minutes": 12, "oven_f": 375}
  ],
  "notes": ["what a modern cook should know that the recipe leaves out: a missing baking time and how to tell it is done, pan size, how stiff the dough should be"]
}

Rules:
- Keep every ingredient and every instruction of the original, in the original order of use. Add none. Do not improve the recipe.
- "amount" is a number in "unit". Units: teaspoon, tablespoon, fluid ounce, cup, pint, quart, gallon, ounce, pound, gram, kilogram, millilitre, litre — or "" for things that are counted (3 eggs: amount 3, unit ""). Use US measures unless the recipe is metric. An amount the recipe does not give ("flour to make a stiff dough", "salt to taste") gets amount 0, and a note with the usual quantity if you can judge it.
- "estimate" is true whenever the amount was a judgement in the original (a teacup, butter the size of an egg, a pinch, a handful) or you had to convert an old measure. Use these values for old measures:
%s
- A weight in the original stays a weight (1 pound of flour: amount 1, unit "pound").
- "minutes": how long this step says to bake, boil, rest or wait; 0 when it gives no time. "oven_f": the oven temperature for this step in °F; 0 when there is none. For "slow / moderate / quick oven" use the lower figure from the table above.
- One action per step. Heating the oven is a step of its own when the recipe bakes.
- Do not wrap the JSON in a code block. No commentary.""" % _old_measure_table()


async def _ask_model(recipe_text: str, owner: Optional[str]) -> Tuple[str, str]:
    """(the model's reply, the model's name). Raises when no model answers."""
    from src.endpoint_resolver import resolve_endpoint
    from src.llm_core import llm_call_async_with_fallback
    from src.task_endpoint import resolve_task_candidates

    candidates: List[Tuple[str, str, dict]] = []
    first = resolve_endpoint("default", owner=owner)
    if first and first[0] and first[1]:
        candidates.append((first[0], first[1], first[2] or {}))
    for url, model, headers in resolve_task_candidates(owner=owner):
        if (url, model) not in [(u, m) for u, m, _ in candidates]:
            candidates.append((url, model, headers or {}))
    if not candidates:
        raise RuntimeError("no model is set up")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "THE RECIPE AS WRITTEN:\n\n" + recipe_text + "\n\nReturn the JSON object now."}]
    raw = await llm_call_async_with_fallback(candidates, messages, temperature=0.1, max_tokens=8000, timeout=240)
    return raw, candidates[0][1]


def _clean(text: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _ingredient(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        raw = {"as_written": raw, "item": raw}
    if not isinstance(raw, dict):
        return None
    written, item = _clean(raw.get("as_written")), _clean(raw.get("item"), 120)
    if not (written or item):
        return None
    unit = unit_name(raw.get("unit"))
    amount = number(raw.get("amount"))
    estimate = bool(raw.get("estimate"))
    note = _clean(raw.get("note"), 400)
    # An old measure the model left as it was: convert it here, the same way every time.
    for pattern, old_amount, old_unit, old_note in OLD_MEASURES:
        if re.fullmatch(rf"(?:{pattern})s?", _clean(raw.get("unit")).lower()):
            amount, unit, estimate = (amount or 1.0) * old_amount, old_unit, True
            note = note or old_note
            break
    result = {"as_written": written or item, "item": item or written, "amount": amount, "unit": unit,
              "prep": _clean(raw.get("prep"), 120), "estimate": estimate, "note": note}
    result.update(measure(amount, unit, item))
    return result


def _step(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        raw = {"do": raw}
    if not isinstance(raw, dict):
        return None
    action = _clean(raw.get("do") or raw.get("text"), 1200)
    if not action:
        return None
    written = _clean(raw.get("as_written"), 1200)
    low, high = minutes_in(action)
    told = number(raw.get("minutes"))
    minutes = int(round(told)) if told and told < 60 * 48 else low
    step: Dict[str, Any] = {"do": action, "as_written": written if written and written != action else "",
                            "minutes": minutes, "minutes_to": high if high > minutes else 0}
    oven = oven_for(action) or oven_for(written)
    heat = number(raw.get("oven_f"))
    if oven is None and heat and 150 <= heat <= 550:
        heat = int(round(heat / 25.0) * 25)               # ovens are set in steps of 25 °F
        oven = {"f": heat, "c": celsius(heat), "word": ""}
    if oven:
        step["oven"] = oven
    return step


def _by_rules(text: str) -> Dict[str, Any]:
    """With no model to read it: the recipe's sentences as steps, nothing
    separated. Honest about being only that."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    # A first line that is a few words and not an amount is the recipe's name ("Jackson Jumbles.").
    named = bool(first) and len(first) <= 80 and len(first.split()) <= 8 and number(first[:6]) is None and len(lines) > 1
    title = first.rstrip(" .:") if named else ""
    sentences: List[str] = []
    for line in (lines[1:] if named else lines):          # a line of the original stays a line; a long paragraph is taken apart
        parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z(])", line) if len(line) > 140 else [line]
        sentences.extend(part.strip() for part in parts if len(part.strip()) > 3)
    return {"title": title, "ingredients": [], "steps": [{"do": s} for s in sentences],
            "notes": ["No model was available to pick out the ingredients, so this is the recipe as written, a sentence at a "
                      "time. Open it again later and choose “Set it out again”."]}


def finish(plan: Dict[str, Any], original: str) -> Dict[str, Any]:
    """Tidy what the model (or the rules) made of a recipe and work out the
    numbers: metric amounts, oven temperatures, times."""
    ingredients = [i for i in (_ingredient(raw) for raw in plan.get("ingredients") or []) if i][:80]
    steps = [s for s in (_step(raw) for raw in plan.get("steps") or []) if s][:80]
    notes = [_clean(note, 500) for note in plan.get("notes") or [] if _clean(note)]
    for note in old_notes(original):
        if not any(note[:28].lower() in existing.lower() for existing in notes):
            notes.append(note)
    return {"title": _clean(plan.get("title"), 160), "year": _clean(plan.get("year"), 12),
            "makes": _clean(plan.get("makes"), 160), "ingredients": ingredients, "steps": steps, "notes": notes[:14]}


async def set_out(text: str, owner: Optional[str] = None,
                  ask: Optional[Callable[[str, Optional[str]], Awaitable[Tuple[str, str]]]] = None, only: str = "") -> Dict[str, Any]:
    """A recipe's text → what the kitchen shows. Never fails for want of a
    model: the result says who set it out (``by_model``).

    ``only`` names the one recipe wanted when the text is a page of a
    cookbook that may hold others beside it (see services/dinner.py)."""
    from services.memory.profile import _parse_object

    text = str(text or "").strip()
    if len(text) < 20:
        raise KitchenError("There is not enough there to be a recipe.")
    text = text[:MAX_TEXT]
    plan: Dict[str, Any] = {}
    model = ""
    try:
        shown = text if not only else (
            f"(This is a page from a cookbook and may hold more than one recipe. Set out ONLY the recipe “{only}”; "
            "leave out every other recipe on the page.)\n\n" + text)
        raw, model = await (ask or _ask_model)(shown, owner)
        plan = _parse_object(raw)
    except Exception as exc:
        logger.warning("The kitchen could not ask a model: %s", exc)
    by_model = bool(plan.get("steps"))
    result = finish(plan if by_model else _by_rules(text), text)
    result["by_model"], result["model"] = by_model, model if by_model else ""
    return result


# ── where a recipe comes from ─────────────────────────────────────────────

def _from_page_data(html: str) -> Tuple[str, str]:
    """Many recipe pages carry the recipe as data (schema.org ``Recipe``):
    the ingredients and the method without the story around them."""
    for block in re.findall(r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html or ""):
        try:
            found = json.loads(block.strip())
        except ValueError:
            continue
        queue = found if isinstance(found, list) else [found]
        while queue:
            node = queue.pop(0)
            if not isinstance(node, dict):
                continue
            queue.extend(node.get("@graph") or [] if isinstance(node.get("@graph"), list) else [])
            kinds = node.get("@type")
            if "Recipe" not in (kinds if isinstance(kinds, list) else [kinds]):
                continue
            ingredients = [str(i).strip() for i in node.get("recipeIngredient") or [] if str(i).strip()]
            steps: List[str] = []

            def walk(part: Any) -> None:
                if isinstance(part, str):
                    for line in re.split(r"\n+", re.sub(r"<[^>]+>", " ", part)):
                        line = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"[ \t]+", " ", line)).strip()
                        if line:
                            steps.append(line)
                elif isinstance(part, list):
                    for each in part:
                        walk(each)
                elif isinstance(part, dict):
                    walk(part.get("itemListElement") or part.get("text") or part.get("name") or "")

            walk(node.get("recipeInstructions") or [])
            if ingredients and steps:
                made = node.get("recipeYield")
                made = made[0] if isinstance(made, list) and made else made
                lines = [str(node.get("name") or "").strip(), ""]
                if made:
                    lines += [f"Makes: {made}", ""]
                lines += ["Ingredients:"] + ingredients + ["", "Method:"] + steps
                return str(node.get("name") or "").strip(), "\n".join(lines).strip()
    return "", ""


def source_text(link: str) -> Dict[str, str]:
    """``{"title", "text", "url"}`` — the words of a recipe from a link: a
    page of the library (as the reader shows it), or a page on the web."""
    from services.library import additions, config as library_config, kiwix
    from services.library.text import html_to_text
    from urllib.parse import urlsplit

    from . import content

    link = str(link or "").strip()
    if link.startswith("/read") or kiwix.owns_url(link) or library_config.is_local_url(link):
        target = content.resolve(url=link)
        book, path = str(target["book"]), str(target["path"])
        if target["kind"] == "article":
            page = content.article_text(book, path)
            return {"title": page["title"], "text": page["text"], "url": f"/read?u={_quote(link)}" if not link.startswith("/read") else link}
        number_ = int(target["page"] or 1)
        info = content.document(book, path)
        return {"title": f"{info['title']} — page {number_}", "text": content.page_text(book, path, number_),
                "url": f"/read?u={_quote(link)}" if not link.startswith("/read") else link}
    if not re.match(r"^https?://", link, re.I):
        raise KitchenError("That is not a link to a page.")
    try:
        body, final, _name = additions.download(link, max_bytes=4_000_000, timeout=30.0)
    except additions.AdditionError as exc:
        raise KitchenError(str(exc))
    except Exception as exc:                          # no network, a private address, a broken site
        raise KitchenError(f"That page could not be fetched ({exc}).")
    html = body.decode("utf-8", errors="replace")
    title, text = _from_page_data(html)
    if not text:
        title, text = html_to_text(html)
    if len(text.strip()) < 40:
        raise KitchenError("There was no recipe text on that page.")
    return {"title": title or urlsplit(final).netloc, "text": text, "url": final}


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


# ── as text (for a chat reply, for keeping) ───────────────────────────────

def ingredient_line(ingredient: Dict[str, Any]) -> str:
    amount = kitchen_text(ingredient.get("amount"), ingredient.get("unit") or "")
    metric = metric_text(ingredient.get("ml"), ingredient.get("grams"))
    line = " ".join(part for part in (amount, ingredient.get("item") or "") if part)
    if ingredient.get("prep"):
        line += f", {ingredient['prep']}"
    # A spoonful is measured with a spoon in any kitchen: no "about 2 g" beside it.
    if metric and ingredient.get("unit") not in ("gram", "kilogram", "millilitre", "litre", "teaspoon", "tablespoon"):
        line += f" ({'about ' if ingredient.get('estimate') or ingredient.get('grams') and ingredient.get('ml') else ''}{metric})"
    written = ingredient.get("as_written") or ""
    if written and _clean(written).lower() != _clean(line).lower() and ingredient.get("estimate"):
        line += f" — as written: “{written}”"
    if ingredient.get("note"):
        line += f" ({ingredient['note']})"
    return line


def as_markdown(recipe: Dict[str, Any], original: bool = True) -> str:
    data = recipe.get("data") or {}
    lines = [f"# {recipe.get('title') or 'Recipe'}"]
    year, source = recipe.get("year") or "", recipe.get("source_title") or ""
    said_already = year and (year in source or year in (recipe.get("title") or ""))
    about = " · ".join(part for part in ("" if said_already else year, data.get("makes") or "", source) if part)
    if about:
        lines += ["", about]
    if data.get("ingredients"):
        lines += ["", "## Ingredients", ""] + [f"- {ingredient_line(i)}" for i in data["ingredients"]]
    if data.get("steps"):
        lines += ["", "## Method", ""]
        for index, step in enumerate(data["steps"], 1):
            extra = []
            if step.get("minutes"):
                extra.append(f"{step['minutes']}–{step['minutes_to']} min" if step.get("minutes_to") else f"{step['minutes']} min")
            if step.get("oven"):
                oven = step["oven"]
                extra.append(f"{oven['f']}–{oven['f_to']} °F / {oven['c']}–{oven['c_to']} °C" if oven.get("f_to")
                             else f"{oven['f']} °F / {oven['c']} °C")
            lines.append(f"{index}. {step['do']}" + (f" ({'; '.join(extra)})" if extra else ""))
    if data.get("notes"):
        lines += ["", "## Good to know", ""] + [f"- {note}" for note in data["notes"]]
    if original and recipe.get("original"):
        lines += ["", "## As written", "", recipe["original"]]
    return "\n".join(lines).strip() + "\n"


# ── the cookbook ──────────────────────────────────────────────────────────

def _mine(query, owner: Optional[str]):
    return query.filter(KitchenRecipe.owner.is_(None)) if not owner else query.filter(KitchenRecipe.owner == owner)


def _view(row: KitchenRecipe, full: bool = True) -> Dict[str, Any]:
    try:
        data = json.loads(row.data or "{}")
    except ValueError:
        data = {}
    view = {"id": row.id, "title": row.title, "year": row.year or "", "source_title": row.source_title or "",
            "source_url": row.source_url or "", "favourite": bool(row.favourite), "link": f"/kitchen?r={row.id}",
            "created": epoch(row.created_at),
            "makes": data.get("makes") or "", "ingredient_count": len(data.get("ingredients") or []),
            "step_count": len(data.get("steps") or []), "by_model": bool(data.get("by_model"))}
    if full:
        view["data"], view["original"] = data, row.original or ""
    return view


def only_this(text: str, data: Dict[str, Any]) -> str:
    """The part of a cookbook page that is the recipe set out: the page up to
    the last words any step or ingredient was taken from. The whole text when
    that cannot be told (or the recipe runs to the end of the page)."""
    end = 0
    pieces = [str(step.get("as_written") or "") for step in data.get("steps") or []]
    pieces += [str(item.get("as_written") or "") for item in data.get("ingredients") or []]
    for piece in pieces:
        words = re.findall(r"[A-Za-z0-9½¼¾⅓⅔]+", piece)[-5:]
        if len(words) < 3:
            continue
        last = None
        for last in re.finditer(r"[^A-Za-z0-9½¼¾⅓⅔]+".join(re.escape(word) for word in words), text, re.I):
            pass
        if last is not None:
            end = max(end, last.end())
    if end < 120 or end >= len(text) - 40:
        return text
    stop = re.search(r"[.!?\n]", text[end:end + 120])
    return text[: end + (stop.end() if stop else 0)].rstrip()


def store(owner: Optional[str], text: str, data: Dict[str, Any], title: str = "", source_title: str = "", source_url: str = "",
          year: str = "") -> Dict[str, Any]:
    """Put a recipe that has been set out (``data``, from :func:`set_out`) in the person's cookbook."""
    owner = owner or None
    data = dict(data)
    found_year = re.search(r"\b(1[5-9]\d\d|20[0-2]\d)\b", f"{source_title} {title}")
    row = KitchenRecipe(
        id=uuid.uuid4().hex[:16], owner=owner,
        title=_clean(title, 160) or data.pop("title", "") or _clean(source_title, 160) or "Recipe",
        source_title=_clean(source_title, 300), source_url=str(source_url or "")[:1000],
        year=_clean(year, 12) or data.get("year") or (found_year.group(1) if found_year else ""),
        original=text, data=json.dumps({k: v for k, v in data.items() if k not in ("title", "year")}, ensure_ascii=False))
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        return _view(row)
    finally:
        db.close()


async def add(owner: Optional[str], text: str, title: str = "", source_title: str = "", source_url: str = "",
              year: str = "", ask=None, only: str = "") -> Dict[str, Any]:
    """Set a recipe out and put it in the person's cookbook. ``only``: the
    one recipe wanted from a page that may hold others (kept as the original:
    that recipe's part of the page, not its neighbours)."""
    owner = owner or None
    text = str(text or "").strip()[:MAX_TEXT]
    data = await set_out(text, owner, ask, only)
    if only and data.get("by_model"):
        text = only_this(text, data)
    return store(owner, text, data, title, source_title, source_url, year)


async def redo(owner: Optional[str], recipe_id: str, ask=None) -> Dict[str, Any]:
    """Set a kept recipe out again from its original text."""
    db = SessionLocal()
    try:
        row = _mine(db.query(KitchenRecipe), owner or None).filter(KitchenRecipe.id == recipe_id).first()
        if row is None:
            raise KitchenError("That recipe is not in your cookbook.")
        original = row.original or ""
    finally:
        db.close()
    data = await set_out(original, owner or None, ask)
    db = SessionLocal()
    try:
        row = _mine(db.query(KitchenRecipe), owner or None).filter(KitchenRecipe.id == recipe_id).first()
        if row is None:
            raise KitchenError("That recipe is not in your cookbook.")
        if not row.year and data.get("year"):
            row.year = data["year"]
        row.data = json.dumps({k: v for k, v in data.items() if k not in ("title", "year")}, ensure_ascii=False)
        row.updated_at = utcnow_naive()
        db.commit()
        return _view(row)
    finally:
        db.close()


def all_recipes(owner: Optional[str]) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        rows = _mine(db.query(KitchenRecipe), owner or None).order_by(KitchenRecipe.created_at.desc()).limit(500).all()
        return [_view(row, full=False) for row in rows]
    finally:
        db.close()


def get(owner: Optional[str], recipe_id: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        row = _mine(db.query(KitchenRecipe), owner or None).filter(KitchenRecipe.id == recipe_id).first()
        if row is None:
            raise KitchenError("That recipe is not in your cookbook.")
        return _view(row)
    finally:
        db.close()


def find(owner: Optional[str], name: str) -> Optional[Dict[str, Any]]:
    """The recipe a person means by a name (or its id)."""
    wanted = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    if not wanted:
        return None
    filler = {"the", "a", "an", "my", "our", "that", "this", "recipe", "for", "of"}
    best, best_score = None, 0.0
    for recipe in all_recipes(owner):
        if recipe["id"] == str(name).strip():
            return get(owner, recipe["id"])
        title = re.sub(r"[^a-z0-9]+", " ", recipe["title"].lower()).strip()
        words, asked = set(title.split()), (set(wanted.split()) - filler) or set(wanted.split())
        score = 2.0 if title == wanted else (1.5 if wanted in title else len(words & asked) / max(len(asked), 1))
        if score > best_score:
            best, best_score = recipe, score
    return get(owner, best["id"]) if best is not None and best_score >= 0.6 else None


def update(owner: Optional[str], recipe_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        row = _mine(db.query(KitchenRecipe), owner or None).filter(KitchenRecipe.id == recipe_id).first()
        if row is None:
            raise KitchenError("That recipe is not in your cookbook.")
        if isinstance(changes.get("title"), str) and _clean(changes["title"], 160):
            row.title = _clean(changes["title"], 160)
        if isinstance(changes.get("favourite"), bool):
            row.favourite = changes["favourite"]
        if isinstance(changes.get("year"), str):
            row.year = _clean(changes["year"], 12)
        row.updated_at = utcnow_naive()
        db.commit()
        return _view(row)
    finally:
        db.close()


def remove(owner: Optional[str], recipe_id: str) -> bool:
    db = SessionLocal()
    try:
        row = _mine(db.query(KitchenRecipe), owner or None).filter(KitchenRecipe.id == recipe_id).first()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()
