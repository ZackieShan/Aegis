"""Kitchen tool: the assistant setting a recipe out for cooking.

    manage_kitchen     save a recipe (its words, or a link) · show · list · delete

(The tool is named for the Kitchen. In Aegis the words "recipe" and "cookbook"
now mean food only: the workflow feature that was called Recipes is Automations,
and the model catalogue that was called Cookbook is the Model Shopper.)

Works only on the cookbook of the account the chat belongs to, so it is one
of the home tools (``core/access.py``, the "home" group). The work is in
``services/reader/kitchen``: the recipe is kept as written, its measures are
worked out for today, and it is broken into steps; this module words the
result so the model can pass it on with the link to the follow-along view.
"""

import difflib
import json
import re
from typing import Any, Dict, List, Optional

from src.constants import MAX_OUTPUT_CHARS


def _parse(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    words = raw.split(None, 1)
    if words and words[0].lower() in ("save", "add", "show", "list", "delete", "remove", "get", "open"):
        return {"action": words[0].lower(), "recipe": words[1].strip() if len(words) > 1 else ""}
    return {"action": "save", "text": raw}             # a recipe pasted with nothing around it


def _ok(text: str) -> dict:
    return {"output": str(text)[:MAX_OUTPUT_CHARS], "exit_code": 0}


def _fail(message: str) -> dict:
    return {"error": message, "exit_code": 1}


def _plain(line: str) -> str:
    return re.sub(r"[^a-z0-9½¼¾⅓⅔⅛]+", " ", line.lower()).strip()


def as_the_user_wrote_it(given: str, said: str) -> str:
    """The recipe in the user's own words, when it is in what they sent.

    The model is asked to pass a recipe on unchanged, and still tidies it — a
    signature dropped, a spelling mended. "As written" has to be what the
    person actually had in front of them, so when the lines the model passed
    are found in the user's message, the user's lines are kept instead
    (everything from the first to the last of them)."""
    theirs: List[str] = [line.rstrip() for line in str(said or "").replace("\r\n", "\n").split("\n")]
    plain = [_plain(line) for line in theirs]
    wanted = [_plain(line) for line in str(given or "").split("\n") if _plain(line)]
    if not wanted or not any(plain):
        return given
    found: List[int] = []
    for line in wanted:
        best, score = -1, 0.0
        for index, candidate in enumerate(plain):
            if not candidate:
                continue
            if candidate == line or (len(line) > 12 and (candidate.startswith(line) or line.startswith(candidate))):
                best, score = index, 1.0
                break
            ratio = difflib.SequenceMatcher(None, line, candidate).ratio()
            if ratio > score:
                best, score = index, ratio
        if score >= 0.82:
            found.append(best)
    if len(found) < max(1, round(len(wanted) * 0.7)):
        return given                                     # not the same text: the model's is all there is
    first, last = min(found), max(found)
    kept = "\n".join(theirs[first:last + 1]).strip()
    return kept if len(kept) >= len(given) * 0.6 else given


def _last_user_message(session_id: Optional[str]) -> str:
    if not session_id:
        return ""
    try:
        from core.models import get_session_manager_instance

        manager = get_session_manager_instance()
        session = manager.get_session(session_id) if manager else None
        for message in reversed(list(getattr(session, "history", None) or [])):
            if getattr(message, "role", "") == "user":
                content = getattr(message, "content", "")
                if isinstance(content, list):               # text parts of a message that also carries pictures
                    content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                return str(content or "").strip()
    except Exception:
        pass
    return ""


def _told(recipe: dict, lead: str) -> str:
    """What the model is given about a recipe: the link first, then the
    recipe for today (never the whole original again)."""
    from services.reader import kitchen

    data = recipe.get("data") or {}
    lines = [f"{lead} Link (give it to the user exactly as it is, address unchanged): [Follow along: {recipe['title']}]({recipe['link']})"]
    if not data.get("by_model"):
        lines.append("NOTE: no model was available to separate ingredients and convert measures; it is kept as written, "
                     "one sentence per step. Say so.")
    lines.append("")
    lines.append(kitchen.as_markdown(recipe, original=False).strip())
    lines.append("")
    lines.append("In your reply: give the link; list the ingredients for today as above (with the original wording where it is shown)"
                 + ("; say that the amounts marked “as written” were judgements in the original and are estimates"
                    if any(i.get("estimate") for i in data.get("ingredients") or []) else "")
                 + ". The steps are in the follow-along view — do not repeat them unless asked.")
    return "\n".join(lines)


def recipe_card(recipe: dict) -> dict:
    """A recipe as a card: its name, what it makes, how long the waiting is, and the door to the cook-along view."""
    data = recipe.get("data") or {}
    minutes = sum(int(step.get("minutes_to") or step.get("minutes") or 0) for step in data.get("steps") or [] if isinstance(step, dict))
    return {"type": "recipe", "title": str(recipe.get("title") or "Recipe"), "serves": str(recipe.get("makes") or data.get("makes") or ""),
            "time": f"about {minutes} min of baking, boiling or resting" if minutes else "", "href": str(recipe.get("link") or "/kitchen")}


class ManageKitchenTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.reader import ReaderError, kitchen

        args = _parse(content)
        owner = (ctx or {}).get("owner")
        action = str(args.get("action") or "list").lower().replace("-", "_")
        name = str(args.get("recipe") or args.get("name") or args.get("id") or "").strip()
        try:
            if action in ("list", "all", "recipes"):
                recipes = kitchen.all_recipes(owner)
                if not recipes:
                    return _ok("The cookbook is empty. Link: [Kitchen](/kitchen)")
                return _ok("Recipes in the cookbook ([Kitchen](/kitchen)):\n" + "\n".join(
                    f"- [{r['title']}]({r['link']})" + (f" ({r['year']})" if r["year"] else "") for r in recipes[:80]))
            if action in ("save", "add", "new", "create", "convert", "follow", "cook"):
                text = str(args.get("text") or args.get("recipe_text") or "").strip()
                link = str(args.get("url") or args.get("link") or "").strip()
                source_title, source_url = str(args.get("source") or "").strip(), ""
                if not text and link:
                    import asyncio
                    found = await asyncio.to_thread(kitchen.source_text, link)
                    text, source_url = found["text"], found["url"]
                    source_title = source_title or found["title"]
                if len(text) < 20:
                    return _fail("Give the recipe itself in `text` (its own words, unchanged), or a link to it in `url`.")
                if not source_url:
                    text = as_the_user_wrote_it(text, _last_user_message((ctx or {}).get("session_id")))
                recipe = await kitchen.add(owner, text, title=str(args.get("title") or "").strip(), source_title=source_title,
                                           source_url=source_url, year=str(args.get("year") or "").strip())
                return {**_ok(_told(recipe, f"Saved “{recipe['title']}” to the cookbook.")), "card": recipe_card(recipe)}
            found = kitchen.find(owner, name) if name else None
            if found is None:
                titles = ", ".join(r["title"] for r in kitchen.all_recipes(owner)[:40]) or "none yet"
                return _fail(f"I could not tell which recipe you mean{f' by “{name}”' if name else ''}. In the cookbook: {titles}.")
            if action in ("show", "get", "open", "read", "view"):
                return {**_ok(_told(found, f"“{found['title']}” from the cookbook.")), "card": recipe_card(found)}
            if action in ("delete", "remove"):
                kitchen.remove(owner, found["id"])
                return _ok(f"Removed “{found['title']}” from the cookbook.")
            return _fail(f"Unknown action “{action}”. Use save, show, list or delete.")
        except ReaderError as exc:
            return _fail(str(exc))
        except Exception as exc:
            return _fail(f"manage_kitchen failed: {type(exc).__name__}: {exc}")
