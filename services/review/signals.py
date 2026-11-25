"""What the chat records themselves show about an exchange, before any model reads it.

A tool that failed, an answer the person stopped or asked for again, a reply that took a minute, a
question a look-up tool exists for that was answered without one, the person having to say it twice:
none of these needs judgement, and each is counted here. They go to the reviewer as facts, and into the
report whether or not the reviewer mentions them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

# What can be wrong with an exchange. The reviewer may use only these words; the report counts them.
CODES: Dict[str, str] = {
    "from_memory": "gave facts from memory where a look-up tool that fits was on hand",
    "unsupported": "stated specific facts that nothing on the page supports",
    "wrong_tool": "used a tool that does not fit when a better one was on hand",
    "tool_failed": "a tool failed and the reply did not say so or work round it",
    "not_done": "did not do what was asked, or only part of it",
    "claimed_done": "said something was done that no tool call did",
    "misread": "misunderstood what was being asked",
    "refused": "refused, lectured or hedged where a plain answer was wanted",
    "too_long": "far longer or more padded than the question needed",
    "unclear": "hard to follow, or in the wrong form for the person and the screen",
    "asked_back": "asked the person for something a tool would have told it, or that they had already said",
    "had_to_repeat": "the person had to ask again or correct it",
    "stopped": "the person stopped the answer",
    "redone": "the person asked for the answer again",
    "slow": "took over a minute",
    "no_reply": "no reply was given",
}
# The ones the records settle on their own; the reviewer need not find them.
FROM_RECORDS = ("had_to_repeat", "stopped", "redone", "slow", "no_reply")
SLOW_SECONDS = 75.0
# The best rating an exchange can have with each problem. A reviewer that is the same model as the
# assistant tends to be kind to it; where a problem is on record, the rating follows from the problem.
CAPS: Dict[str, int] = {
    "no_reply": 1, "claimed_done": 1,
    "from_memory": 2, "tool_failed": 2, "not_done": 2, "refused": 2, "misread": 2,
    "unsupported": 3, "wrong_tool": 3, "had_to_repeat": 3, "redone": 3,
    "stopped": 4, "asked_back": 4, "too_long": 4, "unclear": 4, "slow": 4,
}

# Tools that look a fact up in the place that keeps it. A question one of them fits, answered with no tool
# at all, was answered from memory.
FACT_TOOLS = ("medicine_info", "paper_search", "find_places", "news_search", "ask_wolfram", "weather", "date_math",
              "convert_units", "sun_moon", "map_position")

_CORRECTION = re.compile(
    r"^\W*(?:no\b|nope\b|wrong\b|incorrect\b|that(?:'s| is| was)? (?:wrong|not|incorrect)|not what i|i (?:said|asked|meant)|"
    r"you (?:didn'?t|did not|missed|forgot|ignored)|try again|again\b|still (?:wrong|not|no)|that didn'?t work|it didn'?t work)", re.I)
_REFUSAL = re.compile(
    r"\b(?:i can(?:'|no)t (?:help|assist|provide|comply|do that|share)|i(?:'m| am) (?:unable|not able) to (?:help|assist|provide|comply)|"
    r"i(?:'m| am) sorry,? but i (?:can(?:'|no)t|won'?t)|i (?:won'?t|will not) (?:help|provide|assist)|as an ai\b|"
    r"against (?:my|our) (?:guidelines|policy|policies)|i must decline)", re.I)
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> Set[str]:
    return {word for word in _WORD.findall(str(text or "").lower()) if len(word) > 2}


def asked_again(text: str, earlier: Optional[str]) -> bool:
    """Is this message the person correcting the assistant, or asking the same thing a second time?"""
    said = " ".join(str(text or "").split())
    if _CORRECTION.match(said):
        return True
    mine, before = _words(said), _words(earlier or "")
    if len(mine) < 3 or len(before) < 3:
        return False
    return len(mine & before) / len(mine | before) >= 0.6


def fits(text: str) -> List[str]:
    """The fact tools a message is a question for, of those set up on this server. Words alone decide."""
    said = " ".join(str(text or "").split())
    if len(said) < 6:
        return []
    found: Set[str] = set()
    try:
        from src.agent_tools.lookup_tools import wanted
        found |= wanted(said)
    except Exception:
        pass
    try:
        from src.agent_tools import lookup_tools
        from src.tool_index import ToolIndex
        low = said.lower()
        unavailable = lookup_tools.unavailable()
        for words, tools in ToolIndex._KEYWORD_HINTS.items():
            wanted_here = set(tools) & set(FACT_TOOLS)
            if wanted_here and any(re.search(rf"\b{re.escape(word)}\b", low) for word in words):
                found |= wanted_here - unavailable
    except Exception:
        pass
    return sorted(found)


def read(exchange: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the records show about one exchange."""
    meta = exchange.get("meta") or {}
    tools = exchange.get("tools") or []
    used = [tool["tool"] for tool in tools]
    failed = [tool["tool"] for tool in tools if not tool.get("ok")]
    seconds = meta.get("response_time")
    try:
        seconds = round(float(seconds), 1) if seconds is not None else None
    except (TypeError, ValueError):
        seconds = None
    fitting = fits(exchange.get("asked") or "")
    unused = [tool for tool in fitting if tool not in used]
    read_tokens, sent = meta.get("read_tokens"), meta.get("input_tokens")
    return {
        "tools": used, "failed": failed,
        "seconds": seconds, "slow": bool(seconds and seconds > SLOW_SECONDS),
        "stopped": bool(meta.get("stopped") or meta.get("cancelled")),
        "redone": int(meta.get("redone") or 0),
        "no_reply": not (exchange.get("reply") or "").strip(),
        "repeated_after": asked_again(exchange.get("next") or "", exchange.get("asked")),
        "refusal_words": bool(_REFUSAL.search(exchange.get("reply") or "")),
        "fits": fitting,
        # A question a fact tool fits, and nothing at all was looked up: answered from memory.
        "from_memory": bool(unused) and not used and bool((exchange.get("reply") or "").strip()),
        # After a chat's first message the model should have most of its prompt cached. A large re-read
        # means something near the top of the prompt changed.
        "reread": bool(exchange.get("turn", 1) > 1 and isinstance(read_tokens, int) and isinstance(sent, int) and sent > 0
                       and read_tokens > 3000 and read_tokens > 0.6 * sent),
    }


def codes(found: Dict[str, Any]) -> List[str]:
    """The problems the records settle without a reviewer."""
    settled = []
    if found.get("no_reply"):
        settled.append("no_reply")
    if found.get("repeated_after"):
        settled.append("had_to_repeat")
    if found.get("stopped"):
        settled.append("stopped")
    if found.get("redone"):
        settled.append("redone")
    if found.get("slow"):
        settled.append("slow")
    return settled


def capped(rating: Any, problems: List[str]) -> Optional[int]:
    """A rating brought down to what its problems allow. None stays None (the exchange was not rated)."""
    try:
        number = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        return None
    for problem in problems:
        number = min(number, CAPS.get(problem, 5))
    return number
