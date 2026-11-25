"""Auto: decide, for one message, how it should be answered.

Three decisions, made from the message and the state of the turn alone — no
model is asked, so it costs nothing and cannot wait on a busy GPU:

* **The path** — a plain answer (the quick lane: no tool prompt, no tool
  round-trips) or the agent loop (tools). Only a turn the person left in the
  tool-using mode is ever moved to the quick lane, and only when the message
  is plainly conversation: a greeting, a thank-you, or a writing job whose
  material is all in the message. Anything that might need a tool — a
  question the library or the web could answer, anything about "my" things,
  a link, an attachment, a request to do something — stays on the agent path.
  When in doubt, tools stay available.
* **The effort** — how hard the model should think. Low for the quick lane
  and for short, simple requests; high where the message asks for care or is
  plainly hard; otherwise the model's own default. Never when the person set
  Thinking on or off themselves.
* **The model** — unchanged, unless the administrator named a model for a
  kind of work (``auto_models`` in settings: ``"code"``, ``"deep"``,
  ``"quick"``) and the endpoint serves it. Swapping a large model takes many
  seconds, so nothing is swapped by default.

``decide()`` is the whole interface; ``routes/chat_routes.py`` applies it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

LOW, HIGH = "low", "high"


@dataclass
class Turn:
    """What is known about a turn before it is answered."""
    message: str
    mode: str = "agent"                    # "agent" | "chat", after any promotion the route already made
    code_mode: bool = False
    has_attachments: bool = False
    search_on: bool = False                # a web search was asked for before the answer (plain chat's pre-search)
    special: bool = False                  # research, compare, a document or e-mail open beside the chat, a toolbox…
    home_account: bool = False             # the simple home screen: tools are what it is for
    library_ready: bool = False            # an offline library that can answer questions
    think_set: Optional[bool] = None       # Thinking forced on / off by the person; None = not set
    last_reply: str = ""                   # the assistant's previous message
    last_used_tools: bool = False          # … and whether it used tools
    auto_models: Dict[str, str] = field(default_factory=dict)
    available_models: Optional[Iterable[str]] = None     # what the turn's endpoint serves, when known
    current_model: str = ""


@dataclass
class Route:
    path: str                              # "agent" | "chat"
    effort: Optional[str] = None           # "low" | "high" | None (the model's default)
    model: Optional[str] = None            # another model for this turn, or None
    label: str = ""                        # a word for the reply's header: "quick", "careful", …
    reasons: List[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        return bool(self.label or self.model)

    def as_event(self) -> dict:
        return {"path": self.path, "effort": self.effort or "default", "model": self.model or "", "label": self.label,
                "why": "; ".join(self.reasons)}


# ── reading a message ─────────────────────────────────────────────────────

_LEAD = r"^\s*(?:(?:please|pls|ok(?:ay)?|hey|hi|so|now|and|also|then|just|quick(?:ly)?)[\s,.!:-]+)*(?:(?:can|could|would|will)\s+you\s+|i\s+(?:need|want)\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+)?(?:please\s+)?"

# Thanks, hellos, goodbyes: nothing to do, nothing to look up.
_SOCIAL = re.compile(
    r"^\s*(?:hi+|hey+|hello+|yo|howdy|hiya|good\s+(?:morning|afternoon|evening|night)|gm|gn|"
    r"thanks?(?:\s+(?:you|a\s+lot|so\s+much|very\s+much|again))*|thank\s+you(?:\s+(?:so|very)\s+much)?|thx|ty|cheers|"
    r"bye+|goodbye|good\s?night|see\s+you(?:\s+later)?|later|"
    r"how\s+are\s+you(?:\s+doing)?(?:\s+today)?|how'?s\s+it\s+going|what'?s\s+up|sup|"
    r"lol|lmao|haha+|ha|nice(?:\s+one)?|you(?:'re|\s+are)\s+(?:the\s+best|awesome|great|amazing|funny)|love\s+it|well\s+done|good\s+job)"
    r"(?:\s+(?:aegis|there|friend|buddy|mate|again|everyone|all))?[\s!.,?…:)(😀-🙏❤️👍🙏]*$", re.I)

# "Carry on": an instruction to keep working, never small talk.
_GO_AHEAD = re.compile(r"^\s*(?:(?:yes|ok(?:ay)?|sure|please)[\s,]+)?(?:do\s+(?:it|that|so)|go\s+(?:ahead|on|for\s+it)|proceed|continue|carry\s+on|keep\s+going|"
                       r"please\s+do|try\s+again|again|retry|next|more|finish\s+it)[\s!.,…]*$", re.I)
# A reaction that could also be an answer to a question the assistant asked.
_ASSENT = re.compile(r"^\s*(?:ok(?:ay)?|k|cool|great|perfect|got\s+it|sounds\s+good|fine|alright|right|yep|yes|yeah|sure|no|nope|nah)[\s!.,…]*$", re.I)

# Writing from nothing: a poem, a joke, a list of names. No tool can add anything.
# ("Give me tips for…" and "suggest options for…" are requests for knowledge —
# the library may hold it — so they are not here; brainstorming is.)
_WRITTEN = (r"(?:poems?|haikus?|limericks?|sonnets?|stor(?:y|ies)|tales?|jokes?|riddles?|puns?|toasts?|speech(?:es)?|lyrics|raps?|"
            r"slogans?|taglines?|mottos?|names?|nicknames?|titles?|headlines?|captions?|bios?|tweets?|essays?|paragraphs?|sentences?|"
            r"letters?|greetings?|dialogues?|compliments?|excuses?|pick-?up\s+lines?)")
_CREATIVE = re.compile(
    _LEAD + r"(?:(?:write|draft|compose|make\s+up|come\s+up\s+with|give\s+me|suggest|tell\s+me|think\s+of)\b(?:\s+\S+){0,6}?\s+" + _WRITTEN + r"\b|"
    r"brainstorm\b|(?:come\s+up\s+with|think\s+of)\b(?:\s+\S+){0,4}?\s+ideas?\b|tell\s+me\s+(?:a|another)\s+fun\s+fact\b)", re.I)
# Changing words that are already there.
_TRANSFORM = re.compile(
    _LEAD + r"(?:rewrite|re-?word|rephrase|paraphrase|polish|proofread|shorten|tighten|expand|simplify|summari[sz]e|translate|correct|"
    r"fix\s+(?:the\s+)?(?:grammar|spelling|wording|tone|typos?)|"
    r"make\s+(?:this|that|it)\s+(?:shorter|longer|simpler|clearer|funnier|friendlier|warmer|more\s+\w+|less\s+\w+|sound\s+\w+))\b", re.I)
# … provided the words are in the message, or it points back at the conversation.
_BACK_REFERENCE = re.compile(
    r"\b(?:that|this|it|the\s+above|the\s+(?:last|previous)\s+(?:answer|reply|response|message|one|paragraph)|"
    r"your\s+(?:last\s+|previous\s+)?(?:answer|reply|response|message)|what\s+you\s+(?:just\s+)?(?:said|wrote))\b", re.I)

# Things that live in the app or on the machine: a tool would be needed to make or touch one.
_THING = (r"(?:files?|folders?|director(?:y|ies)|projects?|repos?|repositor(?:y|ies)|servers?|"
          r"images?|pictures?|photos?|pics?|drawings?|logos?|icons?|wallpapers?|videos?|clips?|movies?|animations?|music|tracks?|audio|voices?|"
          r"documents?|docs?|pdfs?|slides?|notes?|lists?|reminders?|events?|tasks?|todos?|calendars?|boards?|trackers?|"
          r"recipes?|shel(?:f|ves)|highlights?|librar(?:y|ies)|manuals?|books?|inbox|e-?mails?|mails?|contacts?|settings?|skills?|memor(?:y|ies)|chats?|sessions?)")
_APP_THING = re.compile(rf"\b{_THING}\b", re.I)
# "the file", "this picture", "that list": a real one.
_DEFINITE_THING = re.compile(rf"\b(?:the|this|that|these|those|its|their|your)\s+(?:\w+\s+){{0,2}}{_THING}\b", re.I)
# Code, or something to build: the agent writes those into documents and files.
_CODE = re.compile(
    r"\b(?:code|scripts?|programs?|functions?|classes|apps?|applications?|websites?|web\s?pages?|html|css|javascript|typescript|python|bash|"
    r"powershell|sql|regex|api|quer(?:y|ies)|macros?|formulas?|spreadsheets?|dockerfile|yaml|json|config)\b", re.I)
# The result is to be kept somewhere.
_KEEP = re.compile(r"\b(?:save|store|export|keep|file\s+it|put\s+it\s+(?:in|on|into)|add\s+it\s+to|as\s+a\s+(?:file|document|doc|pdf|note)|to\s+(?:a|the)\s+(?:file|document|doc|note|list|board))\b", re.I)

# The person's own things, or the world as it is right now.
_PERSONAL = re.compile(r"\b(?:my|our|mine|ours|me\s+(?:a|an|the)\s+(?:reminder|note|list))\b", re.I)
_TIMELY = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|right\s+now|currently|current|latest|recent(?:ly)?|news|headlines?|weather|forecast|"
    r"prices?|stock|score|this\s+(?:week|weekend|month|year|morning|afternoon|evening)|next\s+(?:week|month|year)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|20\d\d)\b", re.I)
_LINK = re.compile(r"https?://|\bwww\.", re.I)

# Small jobs the home tools do in one step. ("Show me how to build…" is not one.)
_SIMPLE_ACTION = re.compile(
    _LEAD + r"(?:add\b.{0,80}?\bto\s+(?:my|our|the)\b|remind\s+me\b|log\b|track\b|pin\b|tick\b|check\s+off\b|cross\s+off\b|"
    r"mark\b.{0,60}?\b(?:done|off|complete|finished)\b|set\s+(?:a|an|the|my)\s+(?:timer|alarm|reminder)\b|"
    r"turn\s+(?:on|off)\b|enable\b|disable\b|"
    r"(?:show|open|read)\s+(?:me\s+)?(?:my|the|our)\s+(?:[\w'-]+\s+){0,2}(?:lists?|reminders?|trackers?|boards?|calendar|notes?|recipes?|shelf)\b|"
    r"what'?s\s+on\s+(?:my|the|our)\b|what\s+do\s+i\s+have\b)", re.I)
# A request to do something, as opposed to say something.
_ACTION = re.compile(
    _LEAD + r"(?:add|remind|schedule|reschedule|book|set|create|make|build|generate|draw|paint|sketch|render|design|open|close|show|"
    r"find|search|look\s+up|lookup|google|check|fetch|get|download|upload|install|uninstall|run|execute|deploy|start|stop|restart|"
    r"play|pause|send|e-?mail|text|message|call|save|store|keep|remember|forget|delete|remove|erase|clear|update|change|rename|move|copy|"
    r"log|track|pin|identify|scan|read|browse|visit|go\s+to|turn\s+(?:on|off)|enable|disable|switch|list|export|import|print|"
    r"commit|push|pull|clone|merge|test|compile|ssh|connect|sync|back\s?up|restore|convert|resize|crop|upscale|animate|narrate|sing|record)\b", re.I)

# Asks for care, or is plainly hard.
_CARE = re.compile(
    r"\b(?:think\s+(?:hard|carefully|deeply|it\s+through|this\s+through|step\s+by\s+step)|step[\s-]by[\s-]step|be\s+thorough|thorough(?:ly)?|"
    r"in\s+(?:great\s+)?detail|carefully|rigorous(?:ly)?|double[\s-]?check|triple[\s-]?check|"
    r"prove|proof|derive|derivation|root\s+cause|trade-?offs?|pros\s+and\s+cons|"
    r"strateg(?:y|ic)|architecture|algorithm|complexity|"
    r"debug|diagnos\w+|troubleshoot|why\s+(?:does|is|did|do|are|was|isn'?t|doesn'?t|won'?t|can'?t)\b.{0,80}\b(?:fail\w*|crash\w*|break\w*|error\w*|wrong|slow|hang\w*|not\s+work\w*)|"
    r"optimi[sz]e|refactor|review\s+(?:this|my|the)|audit|analy[sz]e|analysis|evaluate|assess|"
    r"plan\s+out|roadmap|solve|equation|integral|derivative|probability|calculate|compute)\b", re.I)
# Arithmetic: not worth "high", but not something to rush either.
_MATH = re.compile(r"\d\s*[-+*/^=×÷]\s*\d|\b\d+(?:\.\d+)?\s*%|\bsqrt\b")
_QUESTION = re.compile(
    r"^\s*(?:how|what|why|when|where|which|who|whom|whose|is|are|was|were|can|could|does|do|did|should|will|would|has|have)\b|\?\s*$|"
    r"\bhow\s+(?:to|do|does|can|should|long|much|many)\b", re.I)
# A question about the conversation itself: the answer is already on the page.
_ABOUT_THE_REPLY = re.compile(
    r"^\s*(?:why(?:\s+(?:not|is\s+that|so))?|how\s+so|how\s+come|what\s+do\s+you\s+mean|meaning|really|are\s+you\s+sure|seriously|"
    r"(?:can|could)\s+you\s+(?:explain|say|put)\s+(?:that|it|this)(?:\s+(?:again|more\s+simply|simpler|differently|in\s+plain\s+(?:words|english)))?|"
    r"what\s+(?:does|did)\s+(?:that|this|it)\s+mean|in\s+other\s+words|(?:and\s+)?then\s+what|such\s+as|like\s+what|for\s+example)[\s?!.…]*$", re.I)


# ── "do deep research on …" ───────────────────────────────────────────────────
# A Deep Research job reads dozens of pages and takes minutes, so the chat may
# not start one on its own judgment (an ordinary question once spawned them).
# It may when the person asks for one in so many words. These patterns are that
# test: the feature named with a topic, "research" used as an instruction, or a
# research report asked for. A question ("what does the research say…", "how do
# I research…"), talk about the feature ("is deep research still there?") and
# requests about reports that already exist ("open my research on…") are not.
_RESEARCH_TOPIC = r"(?:on|into|about|for|regarding|of)\b"
_RESEARCH_ASK = re.compile(
    rf"\bdeep[\s-]?(?:research|dive)\s+{_RESEARCH_TOPIC}"                         # deep research on …
    rf"|\bdeep[\s-]?research\s*[:—–-]\s*\S"                                       # deep research: …
    rf"|\bdeep[\s-]?research\s+(?:this|that|it|these|those)\b"                    # deep research this
    r"|\b(?:do|run|start|launch|begin|conduct|kick\s+off|carry\s+out|perform|queue(?:\s+up)?)\s+"
    r"(?:(?:me|us)\s+)?(?:(?:some|a|an|more|thorough|in-?depth|proper|full|deep|detailed|serious|real|little|bit\s+of|"
    rf"lot\s+of)\s+)*research\s+(?:job\s+)?{_RESEARCH_TOPIC}"                    # do some research on …
    r"|\b(?:write|prepare|put\s+together|compile|give\s+me|make|produce|draft)\s+(?:me\s+|us\s+)?(?:a|an)\s+"
    r"(?:(?:detailed|full|thorough|in-?depth|comprehensive|proper|sourced|cited|deep|short|quick)\s+)*"
    r"research\s+(?:report|brief|write-?up|summary|paper)\b",                     # write a research report …
    re.I)
# "Research X" as an instruction: at the start, after at most a polite lead-in.
_RESEARCH_COMMAND = re.compile(
    r"^\s*(?:(?:please|pls|hey|hi|ok(?:ay)?|so|now|aegis)[\s,!.]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:go\s+(?:and\s+)?)?|"
    r"i\s*(?:need|want|would\s+like|'d\s+like)\s+you\s+to\s+|(?:go\s+(?:and\s+)?)|let'?s\s+)?"
    r"(?:deep[\s-]?)?research\s+"
    # …and what follows is a topic, not the rest of a sentence about research
    # ("research shows…", "deep research still needs a button").
    r"(?!(?:papers?|grants?|assistants?|labs?|groups?|institutes?|methods?|skills?|shows?|showed|says?|suggests?|"
    r"indicates?|finds?|found|is|isn't|was|wasn't|has|hasn't|have|had|does|doesn't|did|didn't|should|shouldn't|"
    r"can|can't|cannot|could|couldn't|will|won't|would|wouldn't|needs?|seems?|feels?|takes?|took|works?|worked|"
    r"keeps?|kept|gets?|got|still|also|just|only|never|always|now|again|"
    r"on\s+my|report|library|panel|window|button|toggle|mode)\b)\S", re.I)
_RESEARCH_EXISTING = re.compile(
    r"\b(?:open|show|read|list|delete|remove|find|see|view|pull\s+up|where(?:'s|\s+is)|summari[sz]e|continue)\b"
    r".{0,30}\b(?:my|the|past|saved|that|last|previous|old|earlier|existing)\s+(?:deep\s+)?research\b", re.I)
_RESEARCH_HOW = re.compile(
    r"^\s*(?:how\s+(?:do|can|should|would|does|did|to)\b|what\s+(?:is|are|was|does|do|did)\b|why\b|where\b|when\b|"
    r"who\b|which\b|is\b|are\b|does\b|did\b|was\b)", re.I)


def asks_for_research(message: str) -> bool:
    """True when the message asks, in so many words, for a Deep Research job."""
    text = (message or "").strip()
    if not text or len(text) > 2000 or _RESEARCH_EXISTING.search(text):
        return False
    if _RESEARCH_COMMAND.search(text):
        return True
    return bool(_RESEARCH_ASK.search(text)) and not _RESEARCH_HOW.search(text)


def may_start_research(message: str, mode: str, code_mode: bool = False) -> bool:
    """Whether this turn may start a Deep Research job from the chat: the
    person asked for one by name, in the mode that has tools (Auto)."""
    return mode == "agent" and not code_mode and asks_for_research(message)


# ── the web, when the question is dated ───────────────────────────────────────
# Nobody presses a "web search" button any more. The web tools are pressed on
# the assistant when the message is plainly about something that changes —
# news, prices, scores, opening hours, "the latest" — or asks for a lookup in
# so many words; for settled knowledge the offline library comes first. (The
# weather has its own tool.) When the internet is down the search tool answers
# from the library by itself, so this needs no check of the connection.
_WEBBY = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|search\s+for|look\s+(?:it|this|that|them)?\s*up|google|"
    r"on\s+the\s+(?:web|internet)|online|websites?|news|headlines?|latest|newest|recent(?:ly)?|current(?:ly)?|"
    r"today'?s|tonight'?s|yesterday'?s?|last\s+night|this\s+(?:week|weekend|month|year)|right\s+now|these\s+days|"
    r"prices?|how\s+much\s+(?:is|are|does|do)|in\s+stock|on\s+sale|deals?|coupons?|reviews?|"
    r"scores?|who\s+won|standings|fixtures?|release\s+date|c[ao]mes?\s+out|"
    r"opening\s+hours|open\s+(?:now|today|late|on)|closing\s+time|near\s+me|nearby|nearest|closest|"
    r"phone\s+number|stocks?|share\s+price|exchange\s+rate|elections?|20[2-9]\d)\b", re.I)


def wants_web(message: str) -> bool:
    """True when the message is about something dated or asks for a lookup: the web tools are then put
    in front of the assistant rather than left for it to find."""
    text = message or ""
    return bool(_LINK.search(text) or _WEBBY.search(text))


# ── a question that deserves a full report ────────────────────────────────────
# A comparison, a purchase, a plan with many parts: the quick answer is still
# given, and one button under it offers the long sourced report. The person's
# words decide (never the model's mood), and a job starts only when they tap.
_REPORT_WORTHY = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|pros and cons|trade-?offs?|"
    r"which (?:\w+ ){0,3}(?:is|are|would be|should (?:i|we)) (?:the )?(?:best|better|right|cheapest|safest|most reliable)|"
    r"(?:what(?:'s| is| are)|which is) the best (?:\w+ ){0,4}(?:for|to)\b|"
    r"best (?:way|option|choice|kind|type|brand|model|place|time) (?:to|for)\b|"
    r"should (?:i|we) (?:buy|get|choose|pick|switch|invest|replace|rent|lease|refinance|move|install|upgrade)|"
    r"(?:is|are) (?:\w+ ){1,5}worth (?:it|buying|getting|the money|the cost)|"
    r"what (?:should|do) (?:i|we) (?:need to )?(?:know|consider|look for) (?:before|when|about) \w+ing|"
    r"how (?:do|should|can|would) (?:i|we) (?:plan|prepare for|get started with|go about|set up|budget for)\b|"
    r"everything (?:i|we) (?:need|should) know about|in[- ]depth|thorough(?:ly)?|"
    r"detailed (?:guide|plan|overview|breakdown|comparison)|step[- ]by[- ]step plan)\b", re.I)
_report_minutes: Dict[str, float] = {}


def average_report_minutes() -> float:
    """How long a finished report has taken on this machine, on average (0 when none has finished). Read from
    the reports on disk at most every ten minutes."""
    import glob
    import json
    import os
    import time

    if "average" in _report_minutes and time.time() - _report_minutes.get("at", 0.0) < 600:
        return _report_minutes["average"]
    took = []
    try:
        from src.constants import DEEP_RESEARCH_DIR
        for path in sorted(glob.glob(os.path.join(str(DEEP_RESEARCH_DIR), "*.json")), key=os.path.getmtime, reverse=True)[:60]:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            started, finished = data.get("started_at") or 0, data.get("completed_at") or 0
            if data.get("status") == "done" and finished > started > 0:
                took.append(finished - started)
    except Exception:
        logger.debug("report durations not read", exc_info=True)
    _report_minutes["average"] = (sum(took) / len(took) / 60.0) if took else 0.0
    _report_minutes["at"] = time.time()
    return _report_minutes["average"]


def deserves_report(message: str) -> bool:
    """True for a question a long sourced report would serve better than a short answer."""
    text = (message or "").strip()
    if _words(text) < 6 or len(text) > 1500 or asks_for_research(text) or _SIMPLE_ACTION.match(text):
        return False
    return bool(_REPORT_WORTHY.search(text))


def report_offer(message: str) -> Optional[dict]:
    """The offer drawn under an answer — or None. The time is what past reports on this machine took."""
    if not deserves_report(message):
        return None
    minutes = average_report_minutes()
    about = f"about {max(2, round(minutes))} minutes" if minutes else "several minutes"
    return {"type": "offer", "kind": "report", "label": "Dig deeper: a full report", "estimate": about,
            "topic": " ".join(message.split())[:500]}


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _hint_tools(message: str) -> List[str]:
    """Tools the agent's own keyword hints would force in for this message."""
    try:
        from src.tool_index import ToolIndex
    except Exception:
        return []
    lowered = message.lower()
    found: List[str] = []
    try:
        for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
            if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in keywords):
                found.extend(sorted(tools))
        if ToolIndex._SCHEDULE_RE.search(lowered):
            found.append("manage_tasks")
        if ToolIndex._LIST_RE.search(lowered):
            found.append("manage_lists")
        if ToolIndex._WEB_RE.search(message):
            found.append("web_fetch")
    except Exception:                                   # the hints are a help, not a dependency
        logger.debug("tool hints could not be read", exc_info=True)
    return found


def wordsmith(message: str) -> bool:
    """Writing from nothing, or changing words that are in the message (or
    just above it in the conversation)."""
    if _CREATIVE.match(message):
        return True
    if not _TRANSFORM.match(message):
        return False
    rest = message.split(":", 1)[1] if ":" in message else ""
    return _words(rest) >= 4 or _words(message) > 30 or '"' in message or "“" in message or bool(_BACK_REFERENCE.search(message))


def needs_tools(turn: Turn) -> Optional[str]:
    """Why this message has to stay where tools are — or None when it is
    plainly conversation. Errs on the side of tools."""
    message = turn.message or ""
    if turn.code_mode:
        return "Code mode"
    if turn.has_attachments:
        return "something is attached"
    if turn.home_account:
        # On the family screen the tools are what the assistant is for,
        # whatever words are used ("milk and eggs" is a shopping list) — but a
        # hello or a thank-you still needs none, and should not wait for them.
        if _SOCIAL.match(message):
            return None
        return "the home screen is for getting things done"
    if turn.search_on:
        return "a web search was asked for"
    if turn.special:
        return "a document, e-mail, research or toolbox is in play"
    if asks_for_research(message):
        return "it asks for a Deep Research job"
    # "Rewrite this: …" — what follows the colon is material, not a request;
    # only the instruction in front of it can call for a tool.
    material = message.split(":", 1)[1] if ":" in message else ""
    if _TRANSFORM.match(message) and _words(material) >= 4:
        asked = message.split(":", 1)[0]
        if not (_PERSONAL.search(asked) or _DEFINITE_THING.search(asked) or _KEEP.search(asked) or _CODE.search(asked)):
            return None
    if _LINK.search(message):
        return "it has a link"
    try:
        from src.action_intents import classify_tool_intent
        intent = classify_tool_intent(message)
        if intent.needs_tools:
            return f"it asks for an action ({intent.category})"
    except Exception:
        logger.debug("intent check failed", exc_info=True)
    if _GO_AHEAD.match(message):
        return "it tells the assistant to carry on"
    if _SOCIAL.match(message):
        return None                                     # "thanks!" names no tool, whatever words it uses
    if _ASSENT.match(message):
        # "ok" after "Shall I delete it?" is an instruction, not a pleasantry.
        if turn.last_used_tools or (turn.last_reply or "").rstrip().endswith("?"):
            return "it answers the assistant's last message"
        return None
    if _ABOUT_THE_REPLY.match(message):
        if turn.last_used_tools:
            return "it follows up on work done with tools"
        return None if turn.last_reply else "there is nothing above for it to be about"
    # A poem, a toast, a list of names: written here, in the reply. (The
    # agent's keyword hints would offer a document or a board for these.)
    if _CREATIVE.match(message) and not (_PERSONAL.search(message) or _TIMELY.search(message) or _DEFINITE_THING.search(message)
                                         or _KEEP.search(message) or _CODE.search(message)):
        return None
    hinted = _hint_tools(message)
    if wordsmith(message):
        # "Translate that to Spanish", "translate this: …" — the words are right
        # here and the chat model reworks them directly, which is quicker. The
        # translator tool is for the rest ("how do you say … in Japanese?").
        hinted = [tool for tool in hinted if tool != "translate"]
    if hinted:
        return f"it mentions something a tool handles ({hinted[0]})"
    if _PERSONAL.search(message):
        return "it is about the person's own things"
    if _TIMELY.search(message):
        return "it depends on the date or on current information"
    if _DEFINITE_THING.search(message):
        return "it refers to something kept in the app"
    if _CODE.search(message):
        return "it is about code or something to build"
    if wordsmith(message):
        if _KEEP.search(message):
            return "it asks for the result to be kept"
        return None                                     # the material is in the message; a tool has nothing to add
    if _APP_THING.search(message):
        return "it names something kept in the app"
    if _ACTION.match(message):
        return "it asks for something to be done"
    if _QUESTION.search(message):
        return "a question the library can answer" if turn.library_ready else "a question that may need looking up"
    return "not plainly conversation"


def effort_for(turn: Turn, quick_lane: bool) -> Optional[str]:
    """How hard to think: "low", "high", or None for the model's default."""
    if turn.think_set is not None:
        return None                                     # the person chose; leave it
    message = turn.message or ""
    count = _words(message)
    if _CARE.search(message) or message.count("?") >= 3:
        return HIGH
    if count > 350 and not wordsmith(message):
        return HIGH                                     # a lot to take in, and not just to reword
    if turn.code_mode:
        return HIGH if count > 40 else None
    if _MATH.search(message):
        return None
    if quick_lane:
        return LOW
    if count <= 28 and _SIMPLE_ACTION.match(message):
        return LOW                                      # "add milk to my list": a brief plan is all it needs
    return None


def model_for(turn: Turn, effort: Optional[str]) -> Optional[str]:
    """Another model for this turn — only one the administrator named for
    this kind of work, and only if the endpoint is known to serve it."""
    wanted = ""
    choices = turn.auto_models or {}
    if turn.code_mode:
        wanted = str(choices.get("code") or "")
    elif effort == HIGH:
        wanted = str(choices.get("deep") or "")
    elif effort == LOW:
        wanted = str(choices.get("quick") or "")
    wanted = wanted.strip()
    if not wanted or wanted == (turn.current_model or ""):
        return None
    if turn.available_models is None or wanted not in set(turn.available_models):
        return None
    return wanted


def decide(turn: Turn) -> Route:
    """The route for a turn. Never moves a turn out of plain chat, and never
    takes tools away unless the message is plainly conversation."""
    reasons: List[str] = []
    path = "agent" if turn.mode == "agent" else "chat"
    quick_lane = False
    if path == "agent":
        why = needs_tools(turn)
        if why is None:
            path, quick_lane = "chat", True
            reasons.append("plain conversation: answered directly, without the tool prompt")
        else:
            reasons.append(f"tools kept: {why}")
    else:
        # Plain chat stays plain chat; small talk and rewording still need no deep thought.
        message = turn.message or ""
        quick_lane = bool(_SOCIAL.match(message) or (_ASSENT.match(message) and not (turn.last_reply or "").rstrip().endswith("?"))
                          or (wordsmith(message) and not _CODE.search(message)))
        if quick_lane:
            reasons.append("plain conversation: brief thinking")
    effort = effort_for(turn, quick_lane)
    if effort == LOW and not quick_lane:
        reasons.append("a short, simple request: brief thinking")
    elif effort == HIGH:
        reasons.append("asks for care or is hard: thinking it through")
    model = model_for(turn, effort)
    if model:
        reasons.append(f"model for this kind of work: {model}")
    label = "careful" if effort == HIGH else ("quick" if (quick_lane or effort == LOW) else "")
    return Route(path=path, effort=effort, model=model, label=label, reasons=reasons)


def enabled(owner: Optional[str] = None) -> bool:
    """Auto is on unless the administrator (or the person) switched it off
    with the ``auto_route`` setting."""
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        value = get_user_setting("auto_route", owner or "", settings.get("auto_route", True))
        return str(value).strip().lower() not in ("false", "0", "off", "no")
    except Exception:
        return True


def served_models(endpoint_url: str) -> Optional[List[str]]:
    """The models the endpoint behind a chat URL is known to serve, from the
    list kept with the endpoint — or None when that is not known. Reads the
    database only; it never asks the endpoint."""
    import json
    try:
        from core.database import ModelEndpoint, SessionLocal
    except Exception:
        return None
    wanted = str(endpoint_url or "").strip()
    if not wanted:
        return None
    db = SessionLocal()
    try:
        for row in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled.is_(True)).all():
            base = str(row.base_url or "").rstrip("/")
            if not base or not wanted.startswith(base):
                continue
            found: List[str] = []
            for column in (row.cached_models, row.pinned_models):
                try:
                    found.extend(str(m) for m in json.loads(column or "[]") if m)
                except (TypeError, ValueError):
                    pass
            return found
        return None
    except Exception:
        return None
    finally:
        db.close()


def named_models() -> Dict[str, str]:
    """``auto_models`` from settings: {"code" | "deep" | "quick": model id}."""
    try:
        from src.settings import load_settings
        chosen = load_settings().get("auto_models") or {}
        return {str(k): str(v) for k, v in chosen.items() if k in ("code", "deep", "quick") and v} if isinstance(chosen, dict) else {}
    except Exception:
        return {}
