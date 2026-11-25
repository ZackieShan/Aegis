"""Wolfram|Alpha: maths worked out exactly, and facts from curated data.

One question in plain words ("integrate x^2 sin x", "boiling point of water at 3000 m", "calories in 6 oz of
salmon", "distance from the earth to the moon in light seconds") and one answer in text. Needs an AppID from
Wolfram (Settings → Integrations); the free one allows 2,000 questions a month.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from services import connections
from services.connections import http
from services.connections.http import Problem

logger = logging.getLogger(__name__)

SERVICE = "wolfram"
API = "https://www.wolframalpha.com/api/v1/llm-api"
NAME = "Wolfram|Alpha"


def ask(question: str, room: int = 4000) -> str:
    """Wolfram|Alpha's answer as text. Raises ``Problem`` in words for the person."""
    question = " ".join(str(question or "").split())[:400]
    if not question:
        raise Problem("Give the question to work out.", "refused")
    if not connections.has_key(SERVICE):
        raise Problem(f"{NAME} has no AppID yet (Settings → Integrations).", "key")
    if not connections.on(SERVICE):
        raise Problem(f"{NAME} is switched off (Settings → Integrations).", "off")
    if connections.over_limit(SERVICE):
        raise Problem(f"This month's limit of {connections.limit(SERVICE):,} {NAME} questions is used up "
                      "(Settings → Integrations sets it).", "limit")
    try:
        text = http.get(API, params={"input": question, "appid": connections.value(SERVICE, "app_id"), "maxchars": room},
                        name=NAME, want="text", timeout=25)
    except Problem as problem:
        if problem.status == 501:       # its way of saying "I could not read that as a question"
            raise Problem(f"{NAME} could not make sense of “{question[:80]}”. Put it more simply — the quantity and the "
                          "units, or the expression itself.", "nothing", 501)
        raise
    connections.count(SERVICE, "question")
    return str(text or "").strip()


def answer(args: Dict[str, Any]) -> Tuple[str, List[dict]]:
    question = str(args.get("question") or args.get("query") or args.get("input") or args.get("q") or args.get("_text") or "")
    text = ask(question)
    if not text:
        raise Problem(f"{NAME} sent back nothing for that.", "nothing")
    lines = [f"WOLFRAM|ALPHA: {' '.join(question.split())}", "", text, "",
             "Report the result as given. It was computed, not recalled."]
    return "\n".join(lines), []
