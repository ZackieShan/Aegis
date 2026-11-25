"""Smart tools: one request that needs several blades, done as one job.

A smart tool is a named intent with a fixed set of steps and the cards it fills. The person never sees
its name and never asks for it: "Plan a day hike for Saturday" simply comes back with sunset and the
forecast, a packing list that is already saved, the hazards with the library pages they came from, and
a reminder — where it used to come back as a paragraph from general knowledge with nothing saved.

Each one is three things:

    trigger   what a person says that sets it off (their words decide, never the model's mood)
    tools     the blades it presses on the assistant for that turn
    rules     the steps, in order, as a rule block the assistant follows

The same mechanism (a rule block that goes with a set of tools) is what stopped the model doing date
arithmetic in its head. ``pick()`` is the interface; ``routes/chat_routes.py`` applies it, and the cards
come from the tools themselves.

"What is this?" — the third of the first three — is the camera: a photo is sorted and identified by
``services/identify`` without any rule block at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Pattern, Tuple


@dataclass(frozen=True)
class SmartTool:
    key: str
    title: str                  # what the screen says while it works: "Planning your outing…"
    trigger: Pattern
    tools: Tuple[str, ...]
    rules: str
    required: Tuple[str, ...] = ()     # the steps that must have run before the job is done (a subset of ``tools``)
    maybe: Optional[Pattern] = None     # words that set it off only when ``about`` is also in the message
    about: Optional[Pattern] = None


_OUTING = (r"(?:day )?(?:hike|hiking|walk|trek|backpacking|camping|camp(?:ing)? trip|camp ?out|fishing|hunt(?:ing)?|picnic|bike ride|cycling|"
           r"canoe(?:ing)?|kayak(?:ing)?|paddle|day out|day trip|outing|road trip|beach day|trip to the (?:lake|beach|river|mountains|woods|park)|"
           r"ski(?:ing)?(?: trip)?|snowshoe(?:ing)?|climb(?:ing)?|forag(?:e|ing)|bird ?watching|stargazing)")

GOING_OUT = SmartTool(
    key="going_out",
    title="Planning your outing",
    trigger=re.compile(
        rf"\b(?:plan|planning|organi[sz]e|help me (?:plan|get ready for|prepare for)|get(?:ting)? ready for|prepar(?:e|ing) for)\b.{{0,40}}\b{_OUTING}\b"
        rf"|\b(?:we(?:'re| are)|i(?:'m| am)) (?:going|heading|off) (?:on |for |out )?(?:an? |the |to go )?{_OUTING}\b"
        rf"|\bwhat (?:should|do) (?:i|we) (?:need to )?(?:pack|bring|take|wear)\b.{{0,30}}\b(?:for|on)\b.{{0,20}}\b{_OUTING}\b"
        rf"|\b(?:thinking of|want to go|let'?s go|going) {_OUTING}\b.{{0,40}}\b(?:today|tomorrow|tonight|this (?:weekend|morning|afternoon|evening)|"
        r"on (?:mon|tues|wednes|thurs|fri|satur|sun)day|next week(?:end)?)\b", re.I),
    tools=("sun_moon", "weather", "library_search", "manage_lists", "manage_reminders"),
    required=("sun_moon", "weather", "library_search", "manage_lists", "manage_reminders"),
    rules="""## Going out (a hike, a walk, camping, fishing, a picnic, a day on the water)
The user is planning time outdoors. Do these steps with the tools, in this order — do not answer from memory, and do not skip a step because you think you know the answer:
1. `sun_moon` for that day and place (leave `place` out for home unless they named one). You need the times of sunset and last light.
2. `weather` for that day and place. If it says there is no forecast, carry on and say "no forecast: offline" in your reply.
3. `library_search` for that kind of outing in this season — what to carry and what to watch for (the field manuals and outdoor guides are in the library; add `"shelf": "Survival"` if the first search is poor). Take the packing items and the hazards from what the pages say.
4. `manage_lists` with `"action": "add"`, a list named for the outing (for example "Saturday hike"), and 8 to 14 short packing items in `items`, adjusted to the forecast (rain gear only if rain is possible, extra layers if it is cold, a torch if they could be out near dark).
5. `manage_reminders` create, for the evening before the outing (two hours before, if it is today): "Pack for the hike and check the forecast".
Only when all five calls have been made, reply — in at most six short lines, in this order:
- "Turn back by <time>" and "Leave by <time>": turn back so as to be out before last light (sunset minus the time the way back takes, minus half an hour; if they gave no length, assume the way back takes two hours and say so).
- The weather in one line.
- Two or three things to watch for, each with the library page it came from.
- One line: the packing list and the reminder are saved.
Do not print the packing list, and do not repeat the times of sunrise and sunset: their cards are under your reply. Give every link a tool result gave you, exactly as given.""",
)

DINNER = SmartTool(
    key="dinner",
    title="Finding something to cook",
    # Set off only when a dish or ingredients are named: "what's for dinner?" with nothing else is a
    # conversation, and the assistant asks what there is in the house.
    trigger=re.compile(
        r"\bwhat (?:can|could|should|shall) (?:i|we) (?:cook|bake)\b.{0,40}\b(?:with|from|out of|using)\b\s+\w"
        r"|\b(?:walk|talk|take|guide) me through (?:a |an |the )?(?:recipe for|cooking|baking)\s+\w"
        r"|\b(?:find|give|get|show) me (?:a |an |the )?(?:good |simple |easy |quick )?recipe (?:for|with|using)\s+\w"
        r"|^(?:(?:a|an|any|one) )?(?:good |simple |easy |quick )?recipe (?:for|with|using)\s+\w"
        r"|\bhow (?:do|would|can|should) (?:i|we|you) (?:cook|bake|roast|fry|grill|braise|stew)\s+\w", re.I),
    # "What can I make with…" and "walk me through making…" are about food only when food is named:
    # "what can I make with leftover wood" is not dinner.
    maybe=re.compile(
        r"\bwhat (?:can|could|should|shall) (?:i|we) (?:make|do)\b.{0,40}\b(?:with|from|out of|using)\b\s+\w"
        r"|\bi(?:'ve| have) (?:got|only got)?\b.{3,60}\bwhat (?:can|could|should) (?:i|we) (?:make|cook|bake)\b"
        r"|\b(?:walk|talk|take|guide) me through making\s+\w", re.I),
    about=re.compile(
        r"\b(?:chicken|beef|pork|lamb|mutton|venison|turkey|duck|ham|bacon|sausages?|mince|steak|ribs|meat(?:balls)?|fish|salmon|tuna|cod|trout|"
        r"shrimp|prawns?|eggs?|milk|cream|butter|cheese|yogh?urt|flour|sugar|rice|pasta|noodles?|spaghetti|macaroni|bread|tortillas?|oats?|"
        r"oatmeal|cornmeal|potato(?:es)?|tomato(?:es)?|onions?|garlic|carrots?|celery|beans?|lentils|chickpeas|corn|peas|spinach|kale|cabbage|"
        r"broccoli|cauliflower|mushrooms?|peppers?|zucchini|courgettes?|squash|pumpkin|eggplant|aubergine|apples?|bananas?|lemons?|limes?|"
        r"berries|strawberries|blueberries|peaches|pears?|honey|chocolate|cocoa|tofu|stock|broth|soup|stew|chili|chilli|curry|casserole|"
        r"cake|pie|cookies|biscuits|pancakes?|waffles|muffins|pizza|salad|sauce|sandwich(?:es)?|dumplings|gravy|jam|"
        r"leftover (?:chicken|turkey|ham|rice|pasta|roast|potatoes|vegetables)|leftovers|dinner|supper|lunch|breakfast|dessert)\b", re.I),
    tools=("library_search", "manage_kitchen", "manage_lists", "manage_reminders"),
    required=("library_search",),
    rules="""## Dinner (a recipe, or cooking from what is in the house)
The user wants something to cook. Do these steps with the tools, in this order:
1. `library_search` with the dish or the ingredients they named and `"shelf": "Cooking"` (the cookbooks are in the library). Choose ONE recipe from the results: the dish they named, or the one that best fits what they have.
2. `manage_kitchen` with `"action": "save"`, that recipe's own words unchanged in `text`, its name in `title`, and the book it came from in `source`. That sets it out for cooking a step at a time; do not convert its measures yourself.
3. Only if they asked what they need to buy: `manage_lists` add, list "Shopping list", with the things the recipe needs that they did not say they have.
4. Only if they said when they want to eat: `manage_reminders` create for when to start (that time minus the recipe's time), text "Start cooking: <the dish>".
Then reply in at most three short lines: the dish and the book it is from; why it fits what they have and anything it needs that they did not mention; and that it is set out in the Kitchen, with the link the tool gave, exactly as given. Do not print the ingredients or the method: the recipe's card is under your reply.
If the library has no recipe that fits, do not save anything: say so in one line, then give a simple method from general knowledge and say plainly that it is not from one of the cookbooks.""",
)

SMART_TOOLS: Tuple[SmartTool, ...] = (GOING_OUT, DINNER)
_BY_KEY = {tool.key: tool for tool in SMART_TOOLS}

# "Remind me to plan the camping trip" is a reminder, not a plan; "add hiking boots to my list" is a list.
_NOT_A_JOB = re.compile(r"^\s*(?:please\s+)?(?:remind me|add |put |note |remember )", re.I)


def get(key: Optional[str]) -> Optional[SmartTool]:
    return _BY_KEY.get(str(key or ""))


def steps_left(key: Optional[str], used, unavailable=()) -> Tuple[str, ...]:
    """The required steps of a smart tool that have not run yet, leaving out any the person may not use."""
    tool = get(key)
    if tool is None:
        return ()
    used, unavailable = set(used or ()), set(unavailable or ())
    return tuple(step for step in tool.required if step not in used and step not in unavailable)


def pick(message: str) -> Optional[SmartTool]:
    """The smart tool this message sets off, or None. At most one: the first whose trigger matches."""
    text = " ".join(str(message or "").split())
    if len(text) < 12 or len(text) > 600 or _NOT_A_JOB.match(text):
        return None
    for tool in SMART_TOOLS:
        if tool.trigger.search(text):
            return tool
        if tool.maybe is not None and tool.about is not None and tool.maybe.search(text) and tool.about.search(text):
            return tool
    return None
