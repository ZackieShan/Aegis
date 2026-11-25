"""Dinner: "what can I make with chicken and rice?" done as one job, the same way every time.

Left to follow a list of steps, the assistant answered from memory one time in three — a recipe of its
own, nothing from the cookbooks, nothing set out — and once said it had set a reminder it had not. So the
steps are code, as they are for an outing (services/outing.py):

    1. the cookbooks   the library's Cooking shelf is searched for the dish or the ingredients
    2. one question to the model: which ONE of those pages has the recipe that fits
    3. the Kitchen     that recipe, cut from its page in its own words, is set out to cook a step at a time
    4. what is missing worked out from the recipe's ingredients and what the person said they have
                       (on the shopping list when they asked what to buy; offered as a button otherwise)
    5. a reminder      when they said when they want to eat: when to start

and the answer is a recipe card: the dish and the book it is from, why it fits, what it still needs, when
to start, and the door to the cook-along view. When no cookbook has a recipe that fits it says so, and
only then answers from general knowledge — marked as that.

``read()`` says whether a message is such a request and what it asks; ``plan()`` does the job, yielding
what a screen shows as it goes; ``shopping()`` is the follow-up "add what it needs to my shopping list".
``routes/chat_routes.py`` drives them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOLS = ("library_search", "manage_kitchen", "manage_lists", "manage_reminders")

_TAIL = re.compile(r"\s*(?:\b(?:for (?:dinner|supper|lunch|breakfast|tea|tonight|today|us|the (?:family|kids))|tonight|today|this evening|please|"
                   r"that (?:i|we) can\b.*|(?:and )?(?:we|i) (?:want|would like|need) to eat\b.*|to eat (?:at|by)\b.*|(?:at|by) \d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*)+$", re.I)
_DISH = (re.compile(r"\brecipe for (?P<x>[^.?!;]+)", re.I),
         re.compile(r"\bme through (?:a |an |the )?(?:making|cooking|baking) (?P<x>[^.?!;]+)", re.I),
         re.compile(r"\bhow (?:do|would|can|should) (?:i|we|you) (?:cook|bake|roast|fry|grill|braise|stew) (?P<x>[^.?!;]+)", re.I))
_HAVE = (re.compile(r"\bwhat (?:can|could|should|shall) (?:i|we) (?:make|cook|bake|do)\b.{0,40}?\b(?:with|from|out of|using) (?P<x>[^.?!;]+)", re.I),
         re.compile(r"\bi(?:'ve| have)(?: only)?(?: got)? (?P<x>[^.?!;]{3,80}?)[,.;]? (?:so )?what (?:can|could|should) (?:i|we)", re.I),
         re.compile(r"\brecipe (?:with|using) (?P<x>[^.?!;]+)", re.I))
_EAT_AT = re.compile(r"\b(?:eat|eating|dinner|supper|lunch|breakfast|ready|on the table|serve)\b[^.?!]{0,24}?\b(?:at|by|for) (\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d:])", re.I)
_TO_BUY = re.compile(r"\bwhat (?:else )?(?:do|will|would|should) (?:i|we) need\b|\bshopping list\b|\b(?:need|have) to buy\b|\bwhat to buy\b", re.I)
_FILLER = re.compile(r"^(?:some|a|an|the|a few|a bit of|a little|a couple of|leftover|left-over|left over|my|our|only|just|fresh|frozen|tinned|canned)\s+", re.I)

# Things a kitchen is taken to have: never "missing", never put on a shopping list unasked.
STAPLES = ("salt", "pepper", "water", "oil", "cooking oil", "black pepper", "boiling water", "cold water", "hot water", "ice")

# "Add the ingredients for Chicken Pilau to my shopping list" — the button under a recipe card says this.
_SHOP = re.compile(r"^(?:please )?(?:add|put) (?:the |all the )?(?:ingredients|things|what (?:i|we) need|what(?:'s| is) missing|the missing (?:things|ingredients)) "
                   r"(?:for|from) (?P<x>.+?) (?:to|on|onto) (?:my |our |the )?(?:shopping|grocery|groceries)(?: list)?[.!]?$", re.I)


def _things(text: str) -> List[str]:
    """ "some chicken, rice and a tin of tomatoes" → ["chicken", "rice", "a tin of tomatoes"] """
    out: List[str] = []
    for piece in re.split(r",|;|&|\+|\band\b|\bor\b", text):
        piece = " ".join(piece.split()).strip(" .")
        while _FILLER.match(piece):
            piece = _FILLER.sub("", piece, count=1)
        if piece and len(piece) <= 40 and piece.lower() not in out:
            out.append(piece.lower())
    return out[:12]


def read(message: str, now: datetime) -> Optional[Dict[str, Any]]:
    """What a cooking request asks for — ``{"dish", "have", "eat_at", "to_buy", "said"}`` — or None when
    the message is not one (services/smart_tools has the trigger)."""
    from services import smart_tools
    if smart_tools.pick(message) is not smart_tools.DINNER:
        return None
    text = " ".join(str(message or "").split())
    dish, have = "", []
    for pattern in _HAVE:
        found = pattern.search(text)
        if found:
            have = _things(_TAIL.sub("", found.group("x")))
            break
    if not have:
        for pattern in _DISH:
            found = pattern.search(text)
            if found:
                dish = _TAIL.sub("", " ".join(found.group("x").split())).strip(" .")
                dish = re.sub(r"^(?:a|an|the|some|my|our)\s+", "", dish, flags=re.I)[:60]
                break
    if not dish and not have:
        return None
    eat_at = None
    when = _EAT_AT.search(text)
    if when:
        hour, minute, half = int(when.group(1)), int(when.group(2) or 0), (when.group(3) or "").lower().replace(".", "")
        if 1 <= hour <= 12 and minute < 60:
            if half == "pm" and hour < 12:
                hour += 12
            elif half == "am" and hour == 12:
                hour = 0
            elif not half and hour < 12:
                # "eat at 6" is the evening; "breakfast at 8" and "lunch at 11" are not
                morning = re.search(r"\bbreakfast\b", text, re.I) or (re.search(r"\blunch\b", text, re.I) and hour >= 10)
                if not morning:
                    hour += 12
            eat_at = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
            if eat_at <= now:
                eat_at = None                          # already past: nothing to remind about
    return {"dish": dish, "have": have, "eat_at": eat_at, "to_buy": bool(_TO_BUY.search(text)), "said": text[:300]}


_CHOOSE = """A household asked: "{said}"

These passages are from the cookbooks in their home library:
{pages}

Answer with ONE JSON object and nothing else:
{{"choice": <the number of the page whose recipe fits best, or 0 if none of these pages has a recipe that fits>,
  "title": "the name of that recipe, as printed on the page",
  "fits": "one short sentence saying why it fits what they asked for or have (say nothing about times or meals)"}}
Each passage is only a part of its page or book. Choose a place only if a real recipe (what goes in, and what to do) starts in its passage; a page that only lists the names of recipes does not count. No commentary."""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def cut(page_text: str, title: str, limit: int = 4200) -> str:
    """The page from where the chosen recipe starts: its title as printed, when that can be found
    (a cookbook page often holds the end of one recipe and the start of another)."""
    text = str(page_text or "").strip()
    words = _norm(title).split()
    if len(words) >= 1 and text:
        pattern = r"[^a-z0-9]+".join(re.escape(word) for word in words)
        found = re.search(pattern, text, re.I)
        if found and found.start() < len(text) - 80:
            return text[found.start():found.start() + limit]
    return text[:limit]


def missing(ingredients: List[Dict[str, Any]], have: List[str]) -> List[str]:
    """What a recipe needs that the person did not say they have, in the recipe's order — staples left out."""
    held = [_norm(thing) for thing in have if _norm(thing)]
    out: List[str] = []
    for ingredient in ingredients or []:
        item = " ".join(str(ingredient.get("item") or "").split())
        key = _norm(item)
        if not key or key in STAPLES or any(key.endswith(" " + staple) for staple in ("water", "salt")):
            continue
        # "chicken" covers "chicken thighs"; "rice" covers "long-grain rice"; "eggs" covers "egg"
        singular = lambda word: word[:-2] if word.endswith("es") and len(word) > 4 else word[:-1] if word.endswith("s") and len(word) > 3 else word  # noqa: E731
        key_words = {singular(word) for word in key.split()}
        if any({singular(word) for word in thing.split()} & key_words for thing in held):
            continue
        if item.lower() not in [entry.lower() for entry in out]:
            out.append(item)
    return out[:12]


def _minutes(recipe: Dict[str, Any]) -> int:
    """How long before eating to start: the recipe's own waiting times, and time to get things ready."""
    steps = (recipe.get("data") or {}).get("steps") or []
    waiting = sum(int(step.get("minutes_to") or step.get("minutes") or 0) for step in steps if isinstance(step, dict))
    return int(waiting + 20 + 2 * len(steps))


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _book(title: str) -> str:
    """ "Camp Cookery (Macmillan, 1910) — page 23" → "Camp Cookery, page 23" """
    if re.match(r"\s*cookbook\s*:", title or "", re.I):
        return "the Wikibooks Cookbook"
    page = re.search(r"—\s*page\s+(\d+)\s*$", title or "")
    name = re.sub(r"\s*\([^)]*\)", "", re.sub(r"\s*—\s*page\s+\d+\s*$", "", title or "")).strip() or "a cookbook"
    short = re.split(r"\s*[:;]\s+|\s+—\s+", name, maxsplit=1)[0].strip()          # a long subtitle is left off
    if len(short) >= 8:
        name = short
    if len(name) > 60:
        name = name[:58].rstrip(" ,:;-") + "…"
    return name + (f", page {page.group(1)}" if page else "")


_MEASURE = re.compile(r"(?:\b\d+(?:[.,/]\d+)?|[½¼¾⅓⅔]|\b(?:one|two|three|four|half|a)\b)\s*-?\s*(?:heaped |level |scant |large |small )?"
                      r"(?:cups?|cupfuls?|teacups?|tablespoons?|tablespoonfuls?|tbsps?|teaspoons?|teaspoonfuls?|tsps?|pounds?|lbs?|ounces?|oz|"
                      r"pints?|quarts?|gills?|grams?|g|kg|ml|litres?|liters?|sticks?|cloves?|pinch|eggs?)\b", re.I)
_COOKING = re.compile(r"\b(?:bake|boil|simmer|fry|saut[eé]|stir|mix|beat|whisk|knead|roast|stew|chop|season|serve)\b", re.I)


def worth_showing(text: str) -> bool:
    """Whether a passage can be offered as the place a recipe may be: not a question about cooking, and
    not a page that is only a list of links (a site's front page, a contents page) with no amounts on it."""
    text = str(text or "")
    if len(text) < 80 or text.lstrip().startswith("## Question:"):
        return False
    return not (text.count(" - ") >= 8 and len(_MEASURE.findall(text)) < 2 and len(_COOKING.findall(text)) < 2)


def _candidates(asked: Dict[str, Any]) -> List[Dict[str, str]]:
    """Places in the cookbooks where the recipe may be: ``[{"title", "url", "text"}]``, best first. The
    text is the passage of the page or book that bears on what was asked."""
    from concurrent.futures import ThreadPoolExecutor
    from services import library
    query = f"{asked['dish']} recipe" if asked.get("dish") else " ".join(asked.get("have") or []) + " recipe"

    def look(hit: Dict[str, Any]) -> Optional[Dict[str, str]]:
        url, title = str(hit.get("url") or ""), " ".join(str(hit.get("title") or "").split())
        try:
            found = library.fetch_content(url, query=query, budget=900)
        except Exception:
            found = {}
        text = " ".join(str(found.get("content") or hit.get("snippet") or "").split())
        return {"title": title or "A page in the library", "url": url, "text": text[:900]} if worth_showing(text) else None

    out: List[Dict[str, str]] = []
    seen = set()
    for shelf in ("Cooking", ""):
        hits = []
        for hit in library.library_search(query, count=16, shelf=shelf):
            url = str(hit.get("url") or "")
            if url and url not in seen and "stackexchange.com" not in url and "/questions/" not in url:
                seen.add(url)
                hits.append(hit)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for page in pool.map(look, hits[:10]):
                if page is not None and len(out) < 6:
                    out.append(page)
        if out:
            break                                     # the cookbooks had something: do not go looking in the encyclopaedia
    return out


def _shopping_list(owner: Optional[str], items: List[str]) -> Optional[Dict[str, Any]]:
    """Put things on the shopping list (the list the person already keeps for it, else a new one): its card."""
    from services.home import lists
    from src.agent_tools.home_tools import list_card
    if not items:
        return None
    mine = lists.find_list(owner, "shopping list") or lists.find_list(owner, "groceries")
    view = lists.add_items(owner, mine["id"], items) if mine else lists.create_list(owner, "Shopping list", items, kind="list")
    return list_card(view)


async def plan(asked: Dict[str, Any], owner: Optional[str], ask_model: Optional[Callable[[str], Any]] = None,
               may: Callable[[str], bool] = lambda tool: True) -> AsyncGenerator[Dict[str, Any], None]:
    """Do the job. Yields ``{"step": tool}`` as each part is reached, ``{"sources": [...]}`` for the pages
    looked at, and last ``{"done": True, "text", "card", "events"}`` — where ``card`` is None when no
    cookbook had a recipe that fits (``text`` then says so, and gives a method marked as general knowledge)."""
    from services.reader import kitchen

    events: List[Dict[str, Any]] = []
    wanted = asked["dish"] or ", ".join(asked["have"])

    # 1. the cookbooks
    yield {"step": "library_search"}
    try:
        pages = await asyncio.to_thread(_candidates, asked)
    except Exception:
        logger.info("dinner: the cookbooks could not be searched", exc_info=True)
        pages = []
    events.append({"tool": "library_search", "command": wanted, "output": "\n".join(page["title"] for page in pages) or "Nothing found."})
    if pages:
        yield {"sources": [{"title": page["title"], "url": page["url"]} for page in pages[:3]]}

    # 2. which one
    chosen, title, fits = None, "", ""
    if pages and ask_model is not None:
        yield {"step": "thinking"}
        try:
            from services.memory.profile import _parse_object
            shown = "\n\n".join(f"[{index}] {page['title']}:\n{page['text']}" for index, page in enumerate(pages, 1))
            answer = _parse_object(str(await ask_model(_CHOOSE.format(said=asked["said"], pages=shown)) or ""))
            number = int(answer.get("choice") or 0)
            if 1 <= number <= len(pages):
                chosen = pages[number - 1]
                title = " ".join(str(answer.get("title") or "").split())[:120]
                fits = " ".join(str(answer.get("fits") or "").split())[:220]
        except Exception:
            logger.info("dinner: no recipe was chosen", exc_info=True)

    if chosen is None:
        # Nothing in the cookbooks: say so, and only then answer from general knowledge — marked as that.
        lead = f"None of the cookbooks in the library has a recipe for {wanted}." if pages else f"I found nothing in the cookbooks for {wanted}."
        method = ""
        if ask_model is not None:
            try:
                method = str(await ask_model(
                    f"A household asked: \"{asked['said']}\"\nTheir cookbooks have no recipe that fits. Give ONE simple recipe from general "
                    "knowledge: a line naming the dish, a short list of ingredients with amounts, then numbered steps. Under 170 words. No preamble.") or "").strip()
                method = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", method).strip()
            except Exception:
                logger.info("dinner: no method from general knowledge", exc_info=True)
        text = lead + (" This one is from general knowledge, not from one of your books:\n\n" + method if method else "")
        yield {"done": True, "text": text, "card": None, "events": events}
        return

    # 3. the Kitchen: the recipe in its own words, cut from where it stands in its book, set out to cook a
    #    step at a time. A recipe the model could not set out is not saved as a heap of sentences: the
    #    answer points at the page instead.
    yield {"step": "manage_kitchen"}
    source_title, source_url = chosen["title"], f"/read?u={kitchen._quote(chosen['url'])}"
    passage = chosen["text"]
    try:
        from services import library
        whole = await asyncio.to_thread(library.fetch_content, chosen["url"], f"{title} {wanted}".strip(), 4500)
        passage = str(whole.get("content") or "").strip() or passage
    except Exception:
        logger.info("dinner: the page could not be read again; using the passage found", exc_info=True)
    text = cut(passage, title, limit=3200)
    # Setting it out takes the model most of a minute: say what was chosen now, so the wait is for something.
    book = _book(source_title)
    lead = f"{title or 'A recipe'}, from {book}." + (f" {fits if fits.endswith(('.', '!', '?')) else fits + '.'}" if fits else "")
    yield {"say": lead + " "}
    data = await kitchen.set_out(text, owner, only=title)
    if not data.get("by_model") or len(data.get("steps") or []) < 2:
        yield {"done": True, "card": None, "events": events,
               "text": lead + f" I could not set it out for cooking just now. The page is here: [{book}]({source_url})"}
        return
    recipe = kitchen.store(owner, kitchen.only_this(text, data), data, title=data.get("title") or title, source_title=source_title, source_url=source_url)
    recipe = kitchen.get(owner, recipe["id"])
    from src.agent_tools.reader_tools import recipe_card
    card = recipe_card(recipe)
    events.append({"tool": "manage_kitchen", "command": f"save {recipe['title']}", "output": f"Set out: {recipe['title']}. Link: {recipe['link']}"})

    # 4. what is missing
    ingredients = (recipe.get("data") or {}).get("ingredients") or []
    needs = missing(ingredients, asked["have"]) if (asked["have"] or asked["to_buy"]) else []
    added = None
    if asked["to_buy"] and needs and may("manage_lists"):
        yield {"step": "manage_lists"}
        try:
            added = await asyncio.to_thread(_shopping_list, owner, needs)
            events.append({"tool": "manage_lists", "command": "add to the shopping list", "output": ", ".join(needs)})
        except Exception:
            logger.info("dinner: the shopping list was not changed", exc_info=True)

    # 5. when to start
    reminder, start_at = "", None
    if asked.get("eat_at") is not None:
        start_at = asked["eat_at"] - timedelta(minutes=_minutes(recipe))
        now = datetime.now(asked["eat_at"].tzinfo)
        if start_at > now + timedelta(minutes=5) and may("manage_reminders"):
            yield {"step": "manage_reminders"}
            try:
                from src.agent_tools import TOOL_HANDLERS
                set_it = await TOOL_HANDLERS["manage_reminders"](json.dumps({"action": "create", "text": f"Start cooking: {recipe['title']}", "when": start_at.isoformat()}), {"owner": owner})
                if not set_it.get("exit_code"):
                    reminder = _clock(start_at)
                    events.append({"tool": "manage_reminders", "command": "create", "output": str(set_it.get("output") or "")})
            except Exception:
                logger.info("dinner: no reminder was set", exc_info=True)

    card.update({
        "from": book, "source_url": source_url, "fits": fits,
        "needs": [] if added else needs, "added": (added or {}).get("title", "") if added else "", "added_items": needs if added else [],
        "start": (f"Start at {reminder} to eat at {_clock(asked['eat_at'])}. I will remind you." if reminder
                  else f"To eat at {_clock(asked['eat_at'])} it needs starting now." if start_at is not None else ""),
    })
    lines = [lead, "It is set out in the Kitchen to cook a step at a time."]
    if added:
        lines.append(f"What it needs is on your {added['title'].lower() if added['title'].lower().endswith('list') else added['title']}.")
    if card["start"]:
        lines.append(card["start"])
    events.append({"tool": "plan_dinner", "command": recipe["title"], "output": " ".join(lines), "card": card})
    yield {"done": True, "text": " ".join(lines), "card": card, "events": events}


def shopping(message: str, owner: Optional[str], history=None) -> Optional[Dict[str, Any]]:
    """ "Add the ingredients for Chicken Pilau to my shopping list": ``{"text", "card", "events"}`` — or
    None when the message is not that, or names no recipe in the person's Kitchen. What the recipe card
    said was missing is what goes on the list when that card is the answer just before; otherwise every
    ingredient but the staples."""
    found = _SHOP.match(" ".join(str(message or "").split()))
    if not found:
        return None
    from services.reader import kitchen
    recipe = kitchen.find(owner, found.group("x").strip(" .\"“”'"))
    if not recipe:
        return None
    recipe = kitchen.get(owner, recipe["id"])
    items: List[str] = []
    for entry in reversed(list(history or [])[-4:]):
        role = entry.get("role") if isinstance(entry, dict) else getattr(entry, "role", "")
        if role != "assistant":
            continue
        metadata = (entry.get("metadata") if isinstance(entry, dict) else getattr(entry, "metadata", None)) or {}
        for event in metadata.get("tool_events") or []:
            card = event.get("card") if isinstance(event, dict) else None
            if isinstance(card, dict) and card.get("type") == "recipe" and card.get("href") == recipe["link"] and card.get("needs"):
                items = [str(item) for item in card["needs"]]
        break
    items = items or missing((recipe.get("data") or {}).get("ingredients") or [], [])
    if not items:
        return None
    card = _shopping_list(owner, items)
    text = f"Added to your {card['title']}: {', '.join(items)}."
    return {"text": text, "card": card, "events": [{"tool": "manage_lists", "command": "add to the shopping list", "output": text, "card": card}]}
