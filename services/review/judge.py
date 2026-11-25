"""The rating. The house's own model reads each exchange against a fixed rubric and answers in JSON.

The reviewer sees what a careful person would look at: what was asked, which tools the assistant ran and
what they returned, the reply, what the person said next, and what the records show (it was stopped, a tool
failed, a look-up tool that fits was not used). It rates from 1 to 5 and names problems from a fixed list.

The reviewer is the same model as the assistant, so it is not asked whether a fact is true — it cannot know
— but whether the reply is supported by what is on the page. Where the records show a problem, the rating is
brought down to match whatever the reviewer says (``signals.capped``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import signals

logger = logging.getLogger(__name__)

BATCH = 4                 # exchanges in one request to the model
ASKED_ROOM = 1400         # characters of the person's message
REPLY_ROOM = 2600         # characters of the reply
OUTPUT_ROOM = 700         # characters of each tool's output

AGENTS: Dict[str, Tuple[str, str]] = {
    "family": ("Family screen", "the family screen: someone on a phone who is not technical. Answers should be short and plain."),
    "tools": ("Full app, with tools", "the full app with tools on: it can search, look things up and act."),
    "chat": ("Full app, plain chat", "the full app in plain chat: it has NO tools here, so judge what it said, not what it failed to look up."),
    "research": ("Research", "a research chat."),
}

RUBRIC = """You review how a household AI assistant handled requests, so that it can be improved. Be strict and fair, and judge only what is on the page: the request, the tools the assistant ran and what they returned, its reply, and what the person said next.

You cannot check facts yourself. Do not mark a reply down for being untrue; mark it "unsupported" when it states specific figures, names, dates, addresses, prices or medical or legal claims that no tool output on the page supports.

Rate each exchange from 1 to 5:
5 = did what was asked, fully, in a form that fits the person and the screen. Nothing to fix.
4 = did it, with a small flaw (a little long, a minor point missed).
3 = partly: the main thing was done but something that matters was missed, or it went wrong first.
2 = mostly failed: no tool where one fits, a tool failure passed over, an important part missing, a refusal.
1 = failed: no answer, something made up, or it said a thing was done that no tool did.

Name the problems, using only these words:
{codes}

Rules that decide a rating:
- It gave facts (figures, names, medical or legal detail, places, news) without running a tool that fits, and one of the tools listed below does fit: "from_memory", at most 2. (Not in plain chat, where it has no tools.)
- It asked the person for something a listed tool would have told it: "asked_back", at most 4.
- A tool failed and the reply neither says so nor gets the thing done another way: "tool_failed", at most 2.
- The reply says something was saved, sent, added, set or changed, and no tool call on the page did it: "claimed_done", 1.
- The person's next message corrects it or asks the same thing again: at most 3.
- A greeting, thanks or small talk answered in kind is a 5. Do not invent problems.

The tools this assistant has:
{tools}

Answer with JSON only, no other text, in exactly this shape:
{{"reviews": [{{"id": "e1", "rating": 4, "problems": ["too_long"], "tool": "", "note": "one sentence, at most 25 words: what went right or wrong", "fix": "one sentence, at most 25 words: the change to the assistant's instructions, tools or settings that would most have helped; empty if nothing"}}]}}
"tool" is the name of the listed tool it should have used and did not, or "". Give one review for every exchange, with the same id."""


def agent_of(exchange: Dict[str, Any], family: bool) -> str:
    """Which assistant handled this: the family screen's, the full app's with tools, or plain chat."""
    if family:
        return "family"
    mode = (exchange.get("mode") or "").lower()
    if mode == "research":
        return "research"
    return "chat" if mode == "chat" else "tools"


def _clip(text: str, room: int) -> str:
    text = " ".join(str(text or "").split()) if room < 400 else str(text or "").strip()
    return text if len(text) <= room else text[:room].rsplit(" ", 1)[0] + " […]"


def tool_list() -> str:
    """The tools the reviewer should know of, each in a line: the look-ups that are set up, and the everyday ones."""
    try:
        from src.agent_tools import lookup_tools
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        missing = lookup_tools.unavailable()
        names = [name for name in (
            "web_search", "web_fetch", "news_search", "find_places", "medicine_info", "paper_search", "ask_wolfram", "library_search",
            "weather", "sun_moon", "date_math", "convert_units", "map_position", "translate", "identify_photo", "manage_lists",
            "manage_reminders", "manage_trackers", "manage_notes", "manage_calendar", "manage_kitchen", "manage_memory",
            "generate_image", "list_emails", "send_email", "create_document", "bash", "read_file") if name not in missing]
        lines = []
        for name in names:
            about = str(BUILTIN_TOOL_DESCRIPTIONS.get(name) or "").split(". ")[0].strip()
            if about:
                lines.append(f"- {name}: {about[:150]}")
        return "\n".join(lines)
    except Exception:
        logger.debug("the reviewer's tool list could not be built", exc_info=True)
        return "- (the list of tools could not be read)"


def page(exchange: Dict[str, Any], label: str, found: Dict[str, Any], family: bool) -> str:
    """One exchange as the reviewer reads it."""
    agent = AGENTS[agent_of(exchange, family)][1]
    lines = [f"EXCHANGE {label} — {agent}"]
    before = exchange.get("before")
    if before and (before[0] or before[1]):
        lines.append(f"Just before: the person wrote “{_clip(before[0], 220)}” and it replied “{_clip(before[1], 220)}”")
    asked = _clip(exchange.get("asked") or "", ASKED_ROOM)
    lines.append("The person wrote" + (f" (with {exchange['pictures']} picture(s))" if exchange.get("pictures") else "") + f":\n{asked}")
    tools = exchange.get("tools") or []
    if tools:
        lines.append("What the assistant ran:")
        for number, tool in enumerate(tools[:10], 1):
            state = "worked" if tool.get("ok") else "FAILED"
            lines.append(f" {number}. {tool['tool']} {_clip(tool.get('args') or '', 220)} -> {state}: {_clip(tool.get('out') or '', OUTPUT_ROOM if tool.get('ok') else 300)}")
        if len(tools) > 10:
            lines.append(f" … and {len(tools) - 10} more tool calls.")
    else:
        lines.append("It ran no tools.")
    lines.append("Its reply:\n" + (_clip(exchange.get("reply") or "", REPLY_ROOM) or "(no reply was given)"))
    if exchange.get("next"):
        lines.append(f"What the person wrote next: “{_clip(exchange['next'], 240)}”")
    facts = []
    if found.get("seconds"):
        facts.append(f"took {found['seconds']:.0f} s")
    if found.get("stopped"):
        facts.append("the person stopped it before it finished")
    if found.get("redone"):
        facts.append(f"the person asked for the answer again {found['redone']} time(s)")
    if found.get("failed"):
        facts.append("failed tools: " + ", ".join(sorted(set(found["failed"]))))
    unused = [tool for tool in (found.get("fits") or []) if tool not in (found.get("tools") or [])]
    if unused:
        facts.append("by its wording the request is one for: " + ", ".join(unused) + " (not used)")
    if facts:
        lines.append("On record: " + "; ".join(facts) + ".")
    return "\n".join(lines)


def prompt(pages: List[str]) -> List[Dict[str, str]]:
    system = RUBRIC.format(codes="\n".join(f'- "{code}": {about}' for code, about in signals.CODES.items() if code not in signals.FROM_RECORDS),
                           tools=tool_list())
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(pages) + f"\n\nReview these {len(pages)} exchange(s). JSON only."}]


def parse(raw: str, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """What the model answered, as {id: review}. Anything not in the asked-for shape is dropped, not guessed at."""
    text = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", str(raw or ""), flags=re.I)
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        found = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    rows = found.get("reviews") if isinstance(found, dict) else None
    reviews: Dict[str, Dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("id")) not in ids:
            continue
        try:
            rating = int(row.get("rating"))
        except (TypeError, ValueError):
            continue
        if not 1 <= rating <= 5:
            continue
        problems = [code for code in (row.get("problems") or []) if isinstance(code, str) and code in signals.CODES]
        tool = str(row.get("tool") or "").strip()
        reviews[str(row["id"])] = {
            "rating": rating, "problems": list(dict.fromkeys(problems)),
            "tool": tool if re.fullmatch(r"[a-z][a-z0-9_]{2,40}", tool) else "",
            "note": " ".join(str(row.get("note") or "").split())[:240],
            "fix": " ".join(str(row.get("fix") or "").split())[:240],
        }
    return reviews


async def ask(messages: List[Dict[str, str]], candidates: List[tuple], timeout: int = 240) -> str:
    """The model's answer. Asked without the app's call tracing, so that no chat is copied into the trace store."""
    from src.llm_core import _llm_call_async_impl

    problem: Optional[Exception] = None
    for url, model, headers in candidates:
        try:
            return await _llm_call_async_impl(url, model, messages, temperature=0.1, max_tokens=4096, headers=headers or None,
                                              timeout=timeout, max_retries=1, workload="nightly")
        except Exception as error:          # the next model on the list may answer
            problem = error
            logger.warning("review: %s could not answer (%s)", model, type(error).__name__)
    raise problem or RuntimeError("no model to review with")


async def rate(batch: List[Tuple[Dict[str, Any], Dict[str, Any], bool]], candidates: List[tuple]) -> Dict[str, Dict[str, Any]]:
    """Reviews for a few exchanges of one chat: {exchange id: review}. An exchange the model gave no usable
    review for is simply missing from the answer; the caller records it as not rated."""
    labels = {f"e{number}": exchange["id"] for number, (exchange, _, _) in enumerate(batch, 1)}
    pages = [page(exchange, label, found, family) for label, (exchange, found, family) in zip(labels, batch)]
    messages = prompt(pages)
    reviews = parse(await ask(messages, candidates), list(labels))
    if len(reviews) < len(labels):          # once more for the ones it skipped or garbled
        left = [label for label in labels if label not in reviews]
        again = prompt([pages[list(labels).index(label)] for label in left])
        try:
            reviews.update(parse(await ask(again, candidates), left))
        except Exception:
            logger.debug("review: second ask failed", exc_info=True)
    return {labels[label]: review for label, review in reviews.items()}
