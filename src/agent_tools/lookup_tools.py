"""Looking things up in the place that keeps them, as agent tools.

``news_search``    recent articles on a subject, each with its date
``find_places``    shops, restaurants and services near home or a town: address, phone, hours
``medicine_info``  a medicine's official US label, a section at a time
``paper_search``   published studies on a question
``ask_wolfram``    exact maths and curated facts from Wolfram|Alpha

Each asks an outside service that an administrator connected (or that needs no key) in Settings →
Integrations; the work is in ``services/lookups``. The keys belong to the house, so every account's
assistant has these tools. A tool whose service is not set up is not offered at all (``unavailable``).

When a service cannot answer, the tool says so in words the assistant can pass on, and tells it not to
fill the gap from memory — the reason these tools exist.
"""

import asyncio
import json
import logging
import re
from typing import Callable, Dict, Optional, Set

from src.constants import MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)

TIMEOUT = 40.0
# The tools here whose answers carry a list of sources for the screen (see agent_loop).
SOURCE_TOOLS = ("news_search", "paper_search", "medicine_info", "find_places")
LOOKUP_TOOLS = ("news_search", "find_places", "medicine_info", "paper_search", "ask_wolfram")

# What the assistant must do when the tool could not answer, by tool.
_INSTEAD = {
    "news_search": "You may try web_search for it instead; if that also fails, say the news could not be checked.",
    "find_places": "Say that places could not be looked up just now; do not give addresses, phone numbers or opening hours from memory.",
    "medicine_info": "Say the label could not be checked. Anything you add about the medicine from memory must be marked as "
                     "from memory and not checked against the label.",
    "paper_search": "You may try web_search for studies instead; do not cite papers from memory as if they had been looked up.",
    "ask_wolfram": "Work it out step by step yourself and say that it was not checked by Wolfram|Alpha.",
}


def _args(content: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"_text": raw.split("\n")[0].strip()} if raw else {}


def _owner(ctx) -> Optional[str]:
    return (ctx.get("owner") if isinstance(ctx, dict) else "") or None


async def _run(tool: str, work: Callable[[], tuple]) -> dict:
    from services.connections import Problem

    try:
        text, sources = await asyncio.wait_for(asyncio.to_thread(work), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"{tool}: no answer within {int(TIMEOUT)} seconds. {_INSTEAD[tool]}", "exit_code": 1}
    except Problem as problem:
        if problem.reason == "refused":           # the call itself was wrong: the model can put it right
            return {"error": f"{tool}: {problem}", "exit_code": 1}
        if problem.reason == "nothing":           # it was asked, and has nothing: that is an answer
            return {"output": f"{problem} Tell the person that, plainly; do not make up a result.", "exit_code": 0}
        return {"error": f"{tool}: {problem} {_INSTEAD[tool]}", "exit_code": 1}
    except Exception as problem:
        logger.exception("%s failed", tool)
        return {"error": f"{tool} failed: {type(problem).__name__}. {_INSTEAD[tool]}", "exit_code": 1}
    output = text[:MAX_OUTPUT_CHARS]
    if sources and tool in SOURCE_TOOLS:
        output += "\n\n<!-- SOURCES:" + json.dumps(sources[:12]) + " -->"
    return {"output": output, "exit_code": 0}


class NewsSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.lookups import news
        args = _args(content)
        return await _run("news_search", lambda: news.answer(args))


class FindPlacesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.lookups import places
        args, owner = _args(content), _owner(ctx)
        return await _run("find_places", lambda: places.answer(args, owner))


class MedicineInfoTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.lookups import medicines
        args = _args(content)
        return await _run("medicine_info", lambda: medicines.answer(args))


class PaperSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.lookups import papers
        args = _args(content)
        return await _run("paper_search", lambda: papers.answer(args))


class AskWolframTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.lookups import wolfram
        args = _args(content)
        return await _run("ask_wolfram", lambda: wolfram.answer(args))


def unavailable() -> Set[str]:
    """The tools here that cannot work on this server as it is set up, so that they are not offered.

    A paid service that has reached its monthly limit stays on offer: the tool then says the limit is
    reached, which is how the administrator finds out.
    """
    try:
        from services import connections
        from services.connections import store
        if store._off():
            return set(LOOKUP_TOOLS)
        off: Set[str] = set()
        if not connections.ready("brave"):
            off.add("find_places")
        if not connections.ready("medicines"):
            off.add("medicine_info")
        if not connections.ready("papers"):
            off.add("paper_search")
        if not connections.ready("wolfram"):
            off.add("ask_wolfram")
        return off
    except Exception:
        logger.debug("could not tell which look-ups are set up", exc_info=True)
        return set(LOOKUP_TOOLS)


# ── telling that a message is one of these questions ───────────────────────
#
# The agent loop answers a first message that matches none of its topics straight from the model, with
# no tools on offer. "What are the side effects of <a medicine>" matched none, and was answered from
# memory. These say what a look-up question looks like, so that it gets its tool instead.

_MEDICINE = re.compile(
    r"\b(?:side[- ]effects?|adverse (?:effects?|reactions?)|dos(?:e|es|age|ing)|medications?|medicines?|prescri(?:bed|ptions?)|"
    r"drug interactions?|interacts? with|overdos(?:e|ed|ing)|contraindicat\w*|black[- ]box warning|boxed warning|"
    r"(?:can|could|should|may) (?:i|you|we|he|she|they|my \w+) take|safe to (?:take|mix|combine)|take (?:it|them|this|that) with|"
    r"missed (?:a |my )?dose|used to treat|what is \w+ (?:used|prescribed) for|\d+ ?(?:mg|mcg|milligrams?)|"
    r"antibiotics?|painkillers?|pain relievers?|blood thinners?|"
    r"ozempic|wegovy|mounjaro|zepbound|tylenol|advil|motrin|aleve|benadryl|claritin|zyrtec|allegra|sudafed|mucinex|nyquil|"
    r"ibuprofen|acetaminophen|paracetamol|aspirin|naproxen|metformin|insulin|prednisone|"
    r"[a-z]{4,}(?:statin|sartan|prazole|conazole|mycin|cillin|cycline|floxacin|glutide|zepatide|[uzx]imab|umab|[ia]nib|zepam|zolam|"
    r"oxetine|gliptin|gliflozin|profen|pril|olol))\b", re.I)
_PAPERS = re.compile(
    r"\b(?:stud(?:y|ies) (?:on|about|of|show\w*|say\w*|suggest\w*|find\w*|found)|research (?:says?|shows?|suggests?|on|about|into)|"
    r"(?:is there|any|the|good|scientific|what) evidence|clinical trials?|meta-?analys[ie]s|systematic reviews?|peer[- ]reviewed|"
    r"scientific (?:literature|consensus|papers?|studies)|the literature on|journal articles?|published (?:papers?|studies|research)|"
    r"randomi[sz]ed (?:controlled )?trials?)\b", re.I)
_PLACES = re.compile(
    r"\b(?:near me|nearest|nearby|closest|close to (?:me|here|us)|around here|open (?:now|today|tonight|late)|"
    r"(?:what time|when) does .{1,40}? (?:open|close)|opening hours|(?:hours|phone number|address) (?:for|of)|"
    r"(?:somewhere|place|places) to eat|where can (?:i|we) (?:get|buy|eat|find)|pharmac(?:y|ies)|drugstore|urgent care|"
    r"hardware store|grocery store|gas station|petrol station|locksmith|takeout|take-?away)\b", re.I)
_WOLFRAM = re.compile(
    r"\b(?:wolfram|integrate|integral of|derivative of|differentiate|solve (?:for|the equation)|(?:monthly|mortgage|loan) payment|"
    r"compound interest|amortization|molar mass|half-life of|how many calories|calories in|grams of protein in)\b", re.I)
_INTENTS = (("medicine_info", _MEDICINE), ("paper_search", _PAPERS), ("find_places", _PLACES), ("ask_wolfram", _WOLFRAM))


def wanted(text: str) -> Set[str]:
    """The look-ups a message asks for, of those that are set up on this server. Empty for anything else,
    and always empty where none is set up — so a server without them behaves exactly as it did."""
    said = " ".join(str(text or "").split())
    if len(said) < 6:
        return set()
    found = {tool for tool, pattern in _INTENTS if pattern.search(said)}
    return found - unavailable() if found else set()


HANDLERS: Dict[str, Callable] = {
    "news_search": NewsSearchTool().execute,
    "find_places": FindPlacesTool().execute,
    "medicine_info": MedicineInfoTool().execute,
    "paper_search": PaperSearchTool().execute,
    "ask_wolfram": AskWolframTool().execute,
}
