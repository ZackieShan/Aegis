"""The library's shelves.

Everything the library holds is filed in one tree — ``Category/Shelf`` — so a
person can walk it and a model can tell what kind of source it is reading: a
cookbook is under Cooking, a novel under Literature, a field manual under
Survival. Three kinds of thing get a place:

* a whole **collection** (Wikipedia, a Stack Exchange site), by rule, from its id;
* a **book inside a collection** — Project Gutenberg keeps thousands in one
  file — from its catalogue record: subject class, Gutenberg's own categories,
  its subjects, and when the author lived;
* a **document** (a PDF manual, a file dropped into the library folder) from
  its folder, its title and, when a model is asked, what it says.

``<library>/shelves.json`` holds the owner's own additions: extra categories
and shelves, folder hints, and anything filed by hand. This module is rules
only — no network, no index. :mod:`.catalogue` stores what the rules decide.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SEP = "/"
ARROW = " › "
UNSORTED = "Unsorted"

# (category, what it is, ((shelf, what goes on it), …)). Order is the order shown.
TREE: Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...] = (
    ("Survival", "Staying alive and self-reliant when help is far away", (
        ("Medical & first aid", "Treating injuries and illness in the field"),
        ("Water", "Finding, storing and purifying water"),
        ("Food, foraging & preserving", "Emergency food, wild food, storing what you have"),
        ("Hunting, trapping & fishing", "Taking and dressing game and fish"),
        ("Plants", "Edible, medicinal and poisonous plants"),
        ("Mushrooms", "Identifying fungi, edible and deadly"),
        ("Insects, snakes & pests", "Bites, stings, venom and vermin"),
        ("Shelter, fire & fieldcraft", "General survival, camping, knots, living outdoors"),
        ("Navigation", "Map, compass, stars and finding the way"),
        ("Communications & power", "Radio, signalling, batteries and generators"),
        ("Defence & weapons", "Firearms, marksmanship and self-defence"),
        ("Security, escape & evasion", "Staying unseen, getting away, resisting capture"),
        ("Disaster readiness", "Planning for and living through disasters"),
    )),
    ("Medicine", "Health, illness and the body", (
        ("Medical references", "Encyclopedias and official guidance on conditions and medicines"),
        ("Textbooks & study", "Course books and quick guides for learning medicine"),
        ("Veterinary", "Animal health"),
        ("Old medical books", "Medicine as it was written before 1930 — history, not current advice"),
    )),
    ("Cooking", "Cookbooks and food know-how, by style of cooking", (
        ("General cooking & technique", "All-round cookbooks, methods and kitchen questions"),
        ("Baking & desserts", "Bread, cakes, pastry, sweets"),
        ("Preserving, canning & fermenting", "Putting food by: canning, pickling, drying, curing"),
        ("Meat, fish & game", "Butchery, poultry, seafood, game cookery"),
        ("Vegetarian & vegan", "Cooking without meat"),
        ("Healthy & special diets", "Diet cookbooks, cooking for the sick, nutrition"),
        ("Quick & easy meals", "Everyday, one-pot, slow-cooker and appliance cooking"),
        ("Grilling, smoking & camp cooking", "Fire, barbecue, smoker and campfire food"),
        ("World & regional cuisines", "The cooking of one country or region"),
        ("Drinks & brewing", "Beer, wine, spirits, coffee, tea and other drinks"),
    )),
    ("Homestead", "Growing food, keeping animals, running a household", (
        ("Gardening", "Vegetables, fruit, flowers and the plants themselves"),
        ("Farming & livestock", "Crops, soil, animals, bees, forestry"),
        ("Off-grid power & building", "Energy, water systems and low-tech building"),
        ("Animals & pets", "Dogs, cats, horses and other companions"),
        ("Household & everyday", "Housekeeping, thrift and everyday tips"),
    )),
    ("Repair & making", "Fixing, building and how things work", (
        ("Repair guides", "Step-by-step repairs for devices and appliances"),
        ("Home & building", "Carpentry, plumbing, wiring and construction"),
        ("Vehicles & engines", "Cars, trucks, bicycles, engines, aircraft"),
        ("Electronics & radio", "Circuits, components, amateur radio"),
        ("Woodwork & crafts", "Woodworking, metalwork, sewing and handicrafts"),
        ("Engineering & industry", "Engineering, manufacturing, mining, trades"),
    )),
    ("Science", "How the world works", (
        ("Biology & nature", "Life, animals, plants, natural history"),
        ("Chemistry", "Substances and reactions"),
        ("Physics & astronomy", "Matter, energy, the sky"),
        ("Earth & weather", "Geology, oceans, climate, weather"),
        ("Mathematics & statistics", "Numbers, proof and data"),
        ("General science", "Science as a whole, its history and method"),
    )),
    ("Computing", "Computers, software and networks", (
        ("Linux & system administration", "Running and fixing Linux machines"),
        ("Programming references", "Language and tool documentation"),
        ("Hardware projects", "Raspberry Pi, Arduino and other boards"),
        ("Security & privacy", "Protecting systems, data and identity"),
    )),
    ("History", "What happened, by era", (
        ("Ancient world", "From the first cities to the fall of Rome (to about 500)"),
        ("Middle Ages", "From Rome's fall to the Renaissance (500 to 1450)"),
        ("Early modern (1450-1750)", "Renaissance, Reformation, exploration, colonies"),
        ("Modern (1750-1914)", "Revolutions, empires, the nineteenth century"),
        ("World wars & 20th century", "From 1914 on"),
        ("General histories", "Histories that span many eras, by country or of the world"),
        ("Lives & memoirs", "Biography and autobiography not tied to one era"),
        ("Exploration & travel", "Voyages, expeditions and travellers' accounts"),
        ("Peoples, customs & archaeology", "Anthropology, daily life, digging up the past"),
    )),
    ("Literature", "Novels, stories, poetry and plays, by when they were written", (
        ("Ancient & classical", "Greece, Rome and the ancient world (to about 500)"),
        ("Medieval & Renaissance", "From the sagas to Shakespeare (500 to 1640)"),
        ("17th & 18th century", "From Milton to the first novels (1640 to 1800)"),
        ("19th century", "The great age of the novel (1800 to 1900)"),
        ("20th century", "From 1900 on"),
        ("Children's & young readers", "Fairy tales, picture books, stories for the young"),
        ("Myths, legends & folk tales", "Mythology and traditional stories of every people"),
        ("Collections & undated", "Anthologies and works whose author's time is not known"),
    )),
    ("Philosophy & religion", "Belief, thought and the mind", (
        ("The Bible & Christianity", "Scripture, theology, church history, sermons"),
        ("World religions & mythology", "Judaism, Islam, Buddhism, Hinduism and the study of religion"),
        ("Philosophy & ethics", "From the Greeks to the moderns"),
        ("Psychology & the mind", "How people think, feel and behave"),
        ("Occult & the unexplained", "Spiritualism, magic, astrology, psychical research"),
    )),
    ("Society", "Law, government, money and how people live together", (
        ("Law", "Statutes, cases and legal questions"),
        ("Government & politics", "Constitutions, political thought, public affairs"),
        ("Money, business & economics", "Trade, finance, work and industry"),
        ("Education & parenting", "Teaching, schools, raising children"),
        ("Social questions", "Family, reform, crime, society"),
    )),
    ("Military", "Armies, navies and the art of war", (
        ("Field manuals & doctrine", "How the Army says to do it: FM, ATP, TC, STP"),
        ("Technical manuals", "Operating and maintaining equipment: TM, TB"),
        ("Regulations & paperwork", "Regulations, pamphlets, orders and forms"),
        ("Tactics, strategy & war", "Military science and its classics"),
        ("Ships & seamanship", "Navies, sailing and navigation at sea"),
    )),
    ("Arts & leisure", "Art, music, sport and play", (
        ("Art & architecture", "Painting, sculpture, design, buildings, photography"),
        ("Music", "Scores, instruments and writing about music"),
        ("Sports, games & hobbies", "Recreation, games, collecting"),
        ("Fitness & martial arts", "Training the body, fighting arts"),
    )),
    ("Reference", "Look-it-up works that cover everything", (
        ("Encyclopedias", "General encyclopedias"),
        ("Dictionaries & language", "Dictionaries, grammars, learning languages"),
        ("Maps & travel", "Maps and travel guides"),
        ("Source texts", "Original documents, laws, speeches and transcribed books"),
        ("Courses & study guides", "Open textbooks and lessons on many subjects"),
        ("Magazines & journals", "Periodicals and their back numbers"),
    )),
)

FICTION_CATEGORY = "Literature"

# Other words people (and models) use for a category or a shelf.
_ALIASES: Dict[str, str] = {
    "cookbook": "Cooking", "cookbooks": "Cooking", "cook books": "Cooking", "recipes": "Cooking", "food": "Cooking",
    "baking": "Cooking/Baking & desserts", "canning": "Cooking/Preserving, canning & fermenting",
    "preserving": "Cooking/Preserving, canning & fermenting", "bbq": "Cooking/Grilling, smoking & camp cooking",
    "barbecue": "Cooking/Grilling, smoking & camp cooking", "brewing": "Cooking/Drinks & brewing",
    "novels": "Literature", "fiction": "Literature", "classics": "Literature", "poetry": "Literature", "plays": "Literature",
    "stories": "Literature", "childrens": "Literature/Children's & young readers",
    "children": "Literature/Children's & young readers", "kids": "Literature/Children's & young readers",
    "fairy tales": "Literature/Children's & young readers", "mythology": "Literature/Myths, legends & folk tales",
    "myths": "Literature/Myths, legends & folk tales", "folklore": "Literature/Myths, legends & folk tales",
    "medical": "Survival/Medical & first aid", "first aid": "Survival/Medical & first aid",
    "hunting": "Survival/Hunting, trapping & fishing", "trapping": "Survival/Hunting, trapping & fishing",
    "fishing": "Survival/Hunting, trapping & fishing", "weapons": "Survival/Defence & weapons",
    "firearms": "Survival/Defence & weapons", "guns": "Survival/Defence & weapons", "defense": "Survival/Defence & weapons",
    "defence": "Survival/Defence & weapons", "foraging": "Survival/Food, foraging & preserving",
    "radio": "Survival/Communications & power", "comms": "Survival/Communications & power",
    "escape and evasion": "Survival/Security, escape & evasion", "sere": "Survival/Security, escape & evasion",
    "prepping": "Survival/Disaster readiness", "preparedness": "Survival/Disaster readiness",
    "fieldcraft": "Survival/Shelter, fire & fieldcraft", "bushcraft": "Survival/Shelter, fire & fieldcraft",
    "camping": "Survival/Shelter, fire & fieldcraft",
    "health": "Medicine", "medicine": "Medicine", "vet": "Medicine/Veterinary",
    "gardening": "Homestead/Gardening", "farming": "Homestead/Farming & livestock", "homesteading": "Homestead",
    "repair": "Repair & making", "diy": "Repair & making", "technology": "Repair & making", "electronics": "Repair & making/Electronics & radio",
    "cars": "Repair & making/Vehicles & engines", "woodworking": "Repair & making/Woodwork & crafts",
    "math": "Science/Mathematics & statistics", "maths": "Science/Mathematics & statistics",
    "mathematics": "Science/Mathematics & statistics", "physics": "Science/Physics & astronomy",
    "astronomy": "Science/Physics & astronomy", "biology": "Science/Biology & nature", "nature": "Science/Biology & nature",
    "geology": "Science/Earth & weather", "weather": "Science/Earth & weather",
    "linux": "Computing/Linux & system administration", "programming": "Computing/Programming references",
    "coding": "Computing/Programming references", "computers": "Computing",
    "ancient history": "History/Ancient world", "ancient": "History/Ancient world", "antiquity": "History/Ancient world",
    "medieval": "History/Middle Ages", "medieval history": "History/Middle Ages", "middle ages": "History/Middle Ages",
    "early modern": "History/Early modern (1450-1750)", "renaissance": "History/Early modern (1450-1750)",
    "modern history": "History/Modern (1750-1914)", "20th century history": "History/World wars & 20th century",
    "world wars": "History/World wars & 20th century", "ww2": "History/World wars & 20th century",
    "ww1": "History/World wars & 20th century", "biography": "History/Lives & memoirs",
    "biographies": "History/Lives & memoirs", "memoirs": "History/Lives & memoirs", "travel writing": "History/Exploration & travel",
    "bible": "Philosophy & religion/The Bible & Christianity", "christianity": "Philosophy & religion/The Bible & Christianity",
    "religion": "Philosophy & religion", "philosophy": "Philosophy & religion/Philosophy & ethics",
    "psychology": "Philosophy & religion/Psychology & the mind", "occult": "Philosophy & religion/Occult & the unexplained",
    "law": "Society/Law", "politics": "Society/Government & politics", "government": "Society/Government & politics",
    "economics": "Society/Money, business & economics", "business": "Society/Money, business & economics",
    "money": "Society/Money, business & economics", "finance": "Society/Money, business & economics",
    "education": "Society/Education & parenting", "parenting": "Society/Education & parenting",
    "army manuals": "Military", "field manuals": "Military/Field manuals & doctrine", "war": "Military/Tactics, strategy & war",
    "strategy": "Military/Tactics, strategy & war", "tactics": "Military/Tactics, strategy & war", "navy": "Military/Ships & seamanship",
    "art": "Arts & leisure/Art & architecture", "architecture": "Arts & leisure/Art & architecture",
    "music": "Arts & leisure/Music", "sports": "Arts & leisure/Sports, games & hobbies", "games": "Arts & leisure/Sports, games & hobbies",
    "hobbies": "Arts & leisure/Sports, games & hobbies", "fitness": "Arts & leisure/Fitness & martial arts",
    "martial arts": "Arts & leisure/Fitness & martial arts",
    "encyclopedia": "Reference/Encyclopedias", "encyclopedias": "Reference/Encyclopedias", "wikipedia": "Reference/Encyclopedias",
    "dictionary": "Reference/Dictionaries & language", "dictionaries": "Reference/Dictionaries & language",
    "languages": "Reference/Dictionaries & language", "maps": "Reference/Maps & travel", "travel": "Reference/Maps & travel",
    "textbooks": "Reference/Courses & study guides", "courses": "Reference/Courses & study guides",
    "magazines": "Reference/Magazines & journals", "unsorted": UNSORTED, "unfiled": UNSORTED,
}


def _key(text: str) -> str:
    """How names are compared: case, ``&``/``and``, apostrophes and punctuation do not matter."""
    text = str(text or "").lower().replace("&", " and ").replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# ── the owner's own shelves and filing ─────────────────────────────────────

_custom_lock = threading.Lock()
_custom_cache: Optional[Tuple[str, float, dict]] = None


def custom_path() -> Path:
    from . import config
    return config.library_dir() / "shelves.json"


def custom() -> dict:
    """``<library>/shelves.json`` as a dict (``categories``, ``collections``,
    ``folders``, ``items``); empty when there is none or it cannot be read."""
    global _custom_cache
    path = custom_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    with _custom_lock:
        if _custom_cache and _custom_cache[0] == str(path) and _custom_cache[1] == stamp:
            return _custom_cache[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    with _custom_lock:
        _custom_cache = (str(path), stamp, data)
    return data


def save_custom(data: dict) -> None:
    import os
    import tempfile

    path = custom_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".shelves-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reset_cache()


def reset_cache() -> None:
    global _custom_cache
    with _custom_lock:
        _custom_cache = None


def _clean_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace(SEP, " ")).strip()[:60]


def tree() -> List[dict]:
    """The whole tree: ``[{"name", "about", "shelves": [{"name", "about", "path"}]}]``,
    the built-in categories followed by the owner's own (which may also add
    shelves to a built-in category)."""
    out: List[dict] = [
        {"name": name, "about": about, "custom": False,
         "shelves": [{"name": shelf, "about": what, "path": f"{name}{SEP}{shelf}"} for shelf, what in shelves]}
        for name, about, shelves in TREE
    ]
    by_key = {_key(category["name"]): category for category in out}
    for extra in custom().get("categories") or []:
        if not isinstance(extra, dict):
            continue
        name = _clean_name(extra.get("name"))
        if not name:
            continue
        category = by_key.get(_key(name))
        if category is None:
            category = {"name": name, "about": _clean_name(extra.get("about"))[:160] or "", "custom": True, "shelves": []}
            out.append(category)
            by_key[_key(name)] = category
        have = {_key(shelf["name"]) for shelf in category["shelves"]}
        for shelf in extra.get("shelves") or []:
            shelf_name = _clean_name(shelf.get("name") if isinstance(shelf, dict) else shelf)
            if shelf_name and _key(shelf_name) not in have:
                have.add(_key(shelf_name))
                about = str(shelf.get("about") or "")[:160] if isinstance(shelf, dict) else ""
                category["shelves"].append({"name": shelf_name, "about": about, "path": f"{category['name']}{SEP}{shelf_name}"})
    return out


def categories() -> List[str]:
    return [category["name"] for category in tree()]


def all_paths() -> List[str]:
    """Every ``Category/Shelf`` path, in tree order."""
    return [shelf["path"] for category in tree() for shelf in category["shelves"]]


def split(path: str) -> Tuple[str, str]:
    category, _, shelf = str(path or "").partition(SEP)
    return category, shelf


def display(path: str) -> str:
    """``History/Ancient world`` → ``History › Ancient world``."""
    return ARROW.join(part for part in split(path) if part)


def under(path: str, scope: str) -> bool:
    """Is ``path`` on the shelf ``scope`` or anywhere inside the category ``scope``?"""
    if not scope:
        return True
    path, scope = str(path or ""), str(scope)
    return path == scope or path.startswith(scope + SEP)


def is_fiction(path: str) -> bool:
    """Imaginative writing — kept out of a factual search unless asked for."""
    return split(path)[0] == FICTION_CATEGORY


def resolve(text: str, loose: bool = True) -> str:
    """The shelf or category someone means, however they wrote it:
    ``History > Ancient``, ``cooking/baking``, ``cookbooks``, ``medieval``.
    "" when nothing fits. ``loose=False`` takes only a name written out in
    full or a known other word for one — for reading folder names, where a
    folder called "Technical" must not become the shelf "Technical manuals"."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    parts = [part for part in re.split(r"\s*(?:›|>|/|\\|→|::|\|)\s*", raw) if part.strip()]
    known = tree()
    by_category = {_key(category["name"]): category for category in known}

    def shelf_in(category: dict, wanted: str) -> str:
        wanted_key = _key(wanted)
        if not wanted_key:
            return ""
        for shelf in category["shelves"]:
            if _key(shelf["name"]) == wanted_key:
                return shelf["path"]
        if not loose:
            return ""
        starts = [shelf["path"] for shelf in category["shelves"] if _key(shelf["name"]).startswith(wanted_key)]
        if len(starts) == 1:
            return starts[0]
        words = set(wanted_key.split())
        holds = [shelf["path"] for shelf in category["shelves"] if words <= set(_key(shelf["name"]).split())]
        return holds[0] if len(holds) == 1 else ""

    def category_for(wanted: str) -> Optional[dict]:
        wanted_key = _key(wanted)
        if wanted_key in by_category:
            return by_category[wanted_key]
        alias = _ALIASES.get(wanted_key, "")
        if alias and SEP not in alias:
            return by_category.get(_key(alias))
        if not loose:
            return None
        starts = [category for key, category in by_category.items() if key.startswith(wanted_key) and len(wanted_key) >= 4]
        return starts[0] if len(starts) == 1 else None

    if len(parts) >= 2:
        category = category_for(parts[0])
        if category is not None:
            return shelf_in(category, parts[1]) or category["name"]
    whole = _key(raw)
    if whole == _key(UNSORTED):
        return UNSORTED
    if whole in by_category:
        return by_category[whole]["name"]
    for path in all_paths():                       # a full path written with other punctuation
        if _key(path) == whole:
            return path
    alias = _ALIASES.get(whole) or _ALIASES.get(whole.rstrip("s"))
    if alias:
        return alias
    found = [shelf_in(category, raw) for category in known]
    found = [path for path in found if path]
    if len(found) == 1:
        return found[0]
    category = category_for(raw)
    return category["name"] if category is not None else ""


def valid(path: str) -> bool:
    """A real place in the tree: a category, a shelf, or the unsorted pile."""
    if path == UNSORTED:
        return True
    category, shelf = split(path)
    for known in tree():
        if known["name"] == category:
            return not shelf or any(entry["name"] == shelf for entry in known["shelves"])
    return False


def about(path: str) -> str:
    category, shelf = split(path)
    for known in tree():
        if known["name"] == category:
            if not shelf:
                return known["about"]
            return next((entry["about"] for entry in known["shelves"] if entry["name"] == shelf), "")
    return ""


# ── whole collections ──────────────────────────────────────────────────────

_DATED = re.compile(r"_\d{4}-\d{2}(?:-\d{2})?[a-z]?$")

# (pattern on the collection id without its date, shelf, what kind of source it is)
_COLLECTION_RULES: Tuple[Tuple[str, str, str], ...] = (
    (r"^wikipedia_\w+_medicine|^mdwiki|^medlineplus|^nhs\.uk|^wwwnc\.cdc\.gov", "Medicine/Medical references", "medical reference"),
    (r"^medicalsciences\.stackexchange", "Medicine/Medical references", "questions and answers"),
    (r"^wikem|^quickguidesformedicine|^libretexts\.org_\w+_med", "Medicine/Textbooks & study", "textbooks"),
    (r"^wikivet", "Medicine/Veterinary", "encyclopedia"),
    (r"^wikipedia_", "Reference/Encyclopedias", "encyclopedia"),
    (r"^wiktionary|^wikiquote", "Reference/Dictionaries & language", "dictionary"),
    (r"^wikisource", "Reference/Source texts", "transcribed books and documents"),
    (r"^wikiversity|^wikibooks|^libretexts\.org_\w+_(human|k12)|^khan|^crashcourse|^ted_", "Reference/Courses & study guides", "open textbooks"),
    (r"^wikivoyage|^maps_", "Reference/Maps & travel", "maps and travel guides"),
    (r"^aegis-manuals", "Survival", "manuals"),
    (r"^armypubs", "Military", "official publications"),
    (r"^irp\.fas\.org_\w+_military-medicine|^zimgit-medicine", "Survival/Medical & first aid", "manuals"),
    (r"^zimgit-water", "Survival/Water", "manuals"),
    (r"^zimgit-knots", "Survival/Shelter, fire & fieldcraft", "manuals"),
    (r"^zimgit-food-preparation", "Survival/Food, foraging & preserving", "manuals"),
    (r"^zimgit-post-disaster|^ready\.gov|^trueprepper|^survivorlibrary", "Survival/Disaster readiness", "guides"),
    (r"^outdoors\.stackexchange", "Survival/Shelter, fire & fieldcraft", "questions and answers"),
    (r"^usda-2015", "Cooking/Preserving, canning & fermenting", "official guide"),
    (r"^homebrew\.stackexchange", "Cooking/Drinks & brewing", "questions and answers"),
    (r"^cooking\.stackexchange", "Cooking/General cooking & technique", "questions and answers"),
    (r"^based\.cooking|^foss\.cooking|^grimgrains|^publicdomainrecipes", "Cooking/General cooking & technique", "recipes"),
    (r"^gardening\.stackexchange", "Homestead/Gardening", "questions and answers"),
    (r"^gardenology", "Homestead/Gardening", "encyclopedia"),
    (r"^appropedia", "Homestead", "encyclopedia"),          # everything from solar cookers to first-aid skills
    (r"^energypedia", "Homestead/Off-grid power & building", "encyclopedia"),
    (r"^sustainability\.stackexchange", "Homestead/Off-grid power & building", "questions and answers"),
    (r"^pets\.stackexchange", "Homestead/Animals & pets", "questions and answers"),
    (r"^lifehacks\.stackexchange", "Homestead/Household & everyday", "questions and answers"),
    (r"^ifixit", "Repair & making/Repair guides", "repair guides"),
    (r"^diy\.stackexchange", "Repair & making/Home & building", "questions and answers"),
    (r"^(mechanics|bicycles)\.stackexchange", "Repair & making/Vehicles & engines", "questions and answers"),
    (r"^(electronics|ham)\.stackexchange", "Repair & making/Electronics & radio", "questions and answers"),
    (r"^woodworking\.stackexchange|^crafts\.stackexchange", "Repair & making/Woodwork & crafts", "questions and answers"),
    (r"^engineering\.stackexchange", "Repair & making/Engineering & industry", "questions and answers"),
    (r"^libretexts\.org_\w+_(eng|workforce)", "Repair & making/Engineering & industry", "textbooks"),
    (r"^biology\.stackexchange", "Science/Biology & nature", "questions and answers"),
    (r"^libretexts\.org_\w+_bio", "Science/Biology & nature", "textbooks"),
    (r"^chemistry\.stackexchange", "Science/Chemistry", "questions and answers"),
    (r"^libretexts\.org_\w+_chem", "Science/Chemistry", "textbooks"),
    (r"^(physics|astronomy)\.stackexchange", "Science/Physics & astronomy", "questions and answers"),
    (r"^libretexts\.org_\w+_phys", "Science/Physics & astronomy", "textbooks"),
    (r"^earthscience\.stackexchange", "Science/Earth & weather", "questions and answers"),
    (r"^libretexts\.org_\w+_geo", "Science/Earth & weather", "textbooks"),
    (r"^(math|stats)\.stackexchange", "Science/Mathematics & statistics", "questions and answers"),
    (r"^libretexts\.org_\w+_(math|stats)", "Science/Mathematics & statistics", "textbooks"),
    (r"^archlinux", "Computing/Linux & system administration", "documentation"),
    (r"^(unix|askubuntu|serverfault)\.stackexchange|^superuser\.com|^askubuntu|^serverfault", "Computing/Linux & system administration", "questions and answers"),
    (r"^devdocs_|^stackoverflow", "Computing/Programming references", "documentation"),
    (r"^(raspberrypi|arduino)\.stackexchange", "Computing/Hardware projects", "questions and answers"),
    (r"^security\.stackexchange", "Computing/Security & privacy", "questions and answers"),
    (r"^privacydefence", "Computing/Security & privacy", "guide"),
    (r"^law\.stackexchange", "Society/Law", "questions and answers"),
    (r"^(parenting)\.stackexchange", "Society/Education & parenting", "questions and answers"),
    (r"^libretexts\.org_\w+_biz", "Society/Money, business & economics", "textbooks"),
    (r"^libretexts\.org_\w+_socialsci", "Society/Social questions", "textbooks"),
    (r"^(martialarts|fitness)\.stackexchange", "Arts & leisure/Fitness & martial arts", "questions and answers"),
    # Project Gutenberg by subject class: the category here, each book's own shelf from its record.
    (r"^gutenberg_\w+_lcc-a\b", "Reference", "books"),
    (r"^gutenberg_\w+_lcc-b\b", "Philosophy & religion", "books"),
    (r"^gutenberg_\w+_lcc-(c|d|e|f|cdef)\b", "History", "books"),
    (r"^gutenberg_\w+_lcc-g\b", "History/Exploration & travel", "books"),
    (r"^gutenberg_\w+_lcc-h\b", "Society", "books"),
    (r"^gutenberg_\w+_lcc-j\b", "Society/Government & politics", "books"),
    (r"^gutenberg_\w+_lcc-k\b", "Society/Law", "books"),
    (r"^gutenberg_\w+_lcc-l\b", "Society/Education & parenting", "books"),
    (r"^gutenberg_\w+_lcc-m\b", "Arts & leisure/Music", "books"),
    (r"^gutenberg_\w+_lcc-n\b", "Arts & leisure/Art & architecture", "books"),
    (r"^gutenberg_\w+_lcc-p(b|c|d|e|f|h|m)?\b", "Reference/Dictionaries & language", "books"),
    (r"^gutenberg_\w+_lcc-p[a-z]\b", "Literature", "books"),
    (r"^gutenberg_\w+_lcc-q\b", "Science", "books"),
    (r"^gutenberg_\w+_lcc-r\b", "Medicine/Old medical books", "books"),
    (r"^gutenberg_\w+_lcc-s\b", "Homestead/Farming & livestock", "books"),
    (r"^gutenberg_\w+_lcc-t\b", "Repair & making/Engineering & industry", "books"),
    (r"^gutenberg_\w+_lcc-u\b", "Military/Tactics, strategy & war", "books"),
    (r"^gutenberg_\w+_lcc-v\b", "Military/Ships & seamanship", "books"),
    (r"^gutenberg_\w+_lcc-z\b", "Reference/Magazines & journals", "books"),
    (r"^gutenberg_", "Literature", "books"),
)
_COLLECTION_PATTERNS = tuple((re.compile(pattern, re.I), shelf, kind) for pattern, shelf, kind in _COLLECTION_RULES)


def collection_key(book_id: str) -> str:
    """A collection id without its edition (``…_2026-11a``)."""
    return _DATED.sub("", str(book_id or ""))


def is_gutenberg(book_id: str) -> bool:
    return str(book_id or "").lower().startswith("gutenberg_")


def collection_place(book_id: str) -> Tuple[str, str]:
    """``(shelf, kind of source)`` for a whole collection. The owner's own
    filing (``shelves.json`` → ``collections``) wins over the rules; a
    collection nothing recognises goes on the unsorted pile."""
    key = collection_key(book_id)
    filed = custom().get("collections") or {}
    if isinstance(filed, dict):
        chosen = filed.get(key) or filed.get(str(book_id))
        if chosen and valid(str(chosen)):
            return str(chosen), "collection"
    for pattern, shelf, kind in _COLLECTION_PATTERNS:
        if pattern.search(key):
            return shelf, kind
    return UNSORTED, "collection"


def collection_shelf(book_id: str) -> str:
    return collection_place(book_id)[0]


# ── documents: sections, folders, words ────────────────────────────────────

_SECTIONS: Dict[str, str] = {
    "medical": "Survival/Medical & first aid", "first-aid": "Survival/Medical & first aid", "medicine": "Survival/Medical & first aid",
    "water": "Survival/Water", "food-water-homestead": "Survival/Food, foraging & preserving",
    "food": "Survival/Food, foraging & preserving", "hunting-trapping-fishing": "Survival/Hunting, trapping & fishing",
    "hunting": "Survival/Hunting, trapping & fishing", "plants": "Survival/Plants", "mushrooms": "Survival/Mushrooms",
    "insects-snakes": "Survival/Insects, snakes & pests", "navigation-fieldcraft": "Survival/Navigation",
    "navigation": "Survival/Navigation", "comms-power": "Survival/Communications & power",
    "defence-weapons": "Survival/Defence & weapons", "defense-weapons": "Survival/Defence & weapons",
    "weapons": "Survival/Defence & weapons", "survival": "Survival/Shelter, fire & fieldcraft",
    "homestead": "Homestead/Farming & livestock", "gardening": "Homestead/Gardening", "cooking": "Cooking/General cooking & technique",
    "repair": "Repair & making/Repair guides", "general": "",
}


def section_shelf(section: str) -> str:
    """The shelf for one of the manual sections (``medical``, ``plants`` …), a
    shelf written out, or "" when the name says nothing."""
    name = str(section or "").strip()
    if not name:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug in _SECTIONS:
        return _SECTIONS[slug]
    found = resolve(name.replace("-", " "))
    return found if found != UNSORTED else ""


# Words that place a document. Each entry: shelf, pattern. First match wins
# within a group; survival and cooking are tried before the broad subjects.
_WORD_RULES: Tuple[Tuple[str, str], ...] = (
    ("Survival/Mushrooms", r"\bmushrooms?\b|\bfung(i|us)\b|\btoadstools?\b|\bmycolog"),
    ("Survival/Plants", r"\b(edible|medicinal|poisonous|wild) plants?\b|\bherbal\b|\bherbs? (and|for)\b|\bforag(e|ing)\b(?!.*cook)|\bwild (foods?|edibles)\b|\bbotanical medicine"),
    ("Survival/Insects, snakes & pests", r"\b(venomous|poisonous) (snakes?|animals|arthropods|spiders)\b|\bsnake ?bites?\b|\bsnakes? of\b|\b(ticks?|mosquitoe?s?|scorpions?|spiders?)\b.*\b(bites?|control|identif)|\bpest (management|control)\b|\binsect (pests?|bites|stings)\b|\bvector[- ]borne\b"),
    ("Survival/Water", r"\bwater (purif|treat|filtr|disinfect|storage|collect|suppl(y|ies) in)|\bdrinking water\b|\bsafe water\b|\brainwater\b|\bwell (drilling|digging|construction)\b|\bdesalinat"),
    ("Survival/Medical & first aid", r"\bfirst[- ]aid\b|\bcasualty (care|evacuation)\b|\btccc\b|\bcombat lifesaver\b|\b(wilderness|austere|field|emergency|tactical|survival|disaster) (medicine|medical|surgery)\b|\bwhere there is no (doctor|dentist)\b|\btourniquet|\bwound (care|closure|management)\b|\bmedical (handbook|specialist|emergenc)\b|\bfield (hygiene|sanitation)\b|\bhealth care specialist\b|\bcombat medic\b|\bparamedic\b|\bemergency war surgery\b|\bditch medicine\b|\bmedical evacuation\b"),
    ("Survival/Hunting, trapping & fishing", r"\b(hunting|trapping|trappers?|snares?|deadfalls?|angling|fly[- ]fishing)\b|\bfishing\b(?!.*(industry|fleet|rights))|\bfield dress|\bgame (processing|butcher|animals|birds)\b|\b(deer|elk|duck|turkey|squirrel|rabbit) hunt|\bbowhunt|\bfur[- ]?(bearers|trade trapp)|\btann(ing|ery) (hides|skins)"),
    ("Survival/Navigation", r"\b(land|celestial|wilderness) navigation\b|\bmap reading\b|\bmap and compass\b|\borienteering\b|\bcompass\b.*\b(use|course|navigation)"),
    ("Survival/Communications & power", r"\b(amateur|ham|tactical|emergency|shortwave|two-way) radios?\b|\bradio (operator|communication|telegraph)|\bantennas?\b|\bmorse code\b|\bsignal(l)?ing\b|\bsolar (power|panels?|electric)|\bgenerators?\b.*\b(portable|emergency|backup)|\boff[- ]grid (power|electric)|\bbatter(y|ies)\b.*\b(bank|storage|charging)"),
    ("Survival/Defence & weapons", r"\b(rifles?|carbines?|pistols?|revolvers?|shotguns?|handguns?|firearms?|small arms|marksmanship|ammunition|reloading|gunsmith|ballistics)\b|\bcombatives\b|\bhand[- ]to[- ]hand\b|\bself[- ]defen[cs]e\b|\bknife fighting\b|\barchery\b|\bsniper\b|\bshooting\b"),
    ("Survival/Security, escape & evasion", r"\b(escape|evasion)\b.*\b(evasion|escape|recovery|resistance)\b|\bsere\b|\bsurvival,? evasion\b|\bevasion (and|&) (escape|recovery)\b|\bresistance to interrogation\b|\bcamouflage\b|\bcounter[- ]?surveillance\b|\bopsec\b|\boperations security\b|\block ?pick"),
    ("Survival/Disaster readiness", r"\b(disaster|emergency) (prepared|readiness|planning|response|supplies|management)\b|\bprepper|\bprepping\b|\bpreparedness\b|\b(nuclear|fallout|radiological|pandemic|hurricane|earthquake|flood|wildfire) (survival|prepared|shelter|protection|safety|emergency)|\bcivil defen[cs]e\b|\bbug[- ]out\b|\bshelter[- ]in[- ]place\b|\bgrid[- ]down\b"),
    ("Survival/Food, foraging & preserving", r"\b(emergency|survival|long[- ]term) food\b|\bfood stor(age|ing)\b|\bfood (reserve|supply) (for|in) (emergenc|disaster)|\bration(s|ing)\b"),
    ("Survival/Shelter, fire & fieldcraft", r"\bsurvival\b|\bbushcraft\b|\bwoodcraft\b|\bfieldcraft\b|\bcamp(ing|craft)\b|\bbackpacking\b|\boutdoor (life|skills|living)\b|\bwilderness (living|skills|travel)\b|\bknots?\b.*\b(splic|rope|tying|book|guide)|\brope ?work\b|\bfire ?(making|starting|craft)\b|\bshelters? (building|construction)\b|\bcold weather (operations|survival)\b|\bmountain(eering| operations)\b|\bdesert operations\b|\bjungle operations\b|\bboy scouts?\b|\bscout(ing|craft)\b"),
)
_WORD_PATTERNS = tuple((shelf, re.compile(pattern, re.I)) for shelf, pattern in _WORD_RULES)

_COOKING_WORDS = re.compile(
    r"\bcook(ing|ery|book|books)?\b|\brecipes?\b|\bbaking\b|\bcuisine\b|\bkitchen\b|\bconfectioner|\bdesserts?\b|\bbread ?making\b|"
    r"\bcanning\b|\bpickling\b|\bmenus?\b|\bbeverages\b|\bcocktails?\b|\bgastronom|\bdinners?\b|\bchef\b|\bgrill(ing)?\b|\bbarbecue\b|\bbbq\b", re.I)

_COOKING_STYLES: Tuple[Tuple[str, str], ...] = (
    ("Cooking/Preserving, canning & fermenting", r"\bcanning\b|\bpreserv(ing|es|ation)\b|\bpickl(es|ing|ed)\b|\bferment|\bdehydrat|\bdrying (food|fruit|vegetables)|\bjams?\b|\bjell(y|ies)\b|\bcuring\b|\bcharcuterie\b|\bsausage ?making\b|\broot cellar|\bputting (food )?(up|by)\b|\bfood preservation\b"),
    ("Cooking/Drinks & brewing", r"\bbeverages?\b|\bcocktails?\b|\bbartend|\bmixolog|\bliquors?\b|\bwines?\b(?! (sauce|vinegar))|\bwine ?making\b|\bbrew(ing|ery|er)\b|\bbeer\b|\bcider\b|\bmead\b|\bdistill|\bcoffee\b|\btea\b(?!spoon)|\bsmoothies?\b|\bjuic(e|es|ing)\b|\bdrinks?\b|\bspirits\b|\bwhisk(e)?y\b"),
    ("Cooking/Grilling, smoking & camp cooking", r"\bgrill(ing|ed|s)?\b|\bbarbecu(e|ing)\b|\bbbq\b|\bsmok(er|ing|ed) (meat|food|cook)|\bsmoker\b|\bcampfire\b|\bcamp cook|\bdutch oven\b|\boutdoor cook|\bopen[- ]fire\b|\bcast[- ]iron\b|\bpit ?master\b"),
    ("Cooking/Baking & desserts", r"\bbak(ing|ery|er|ed goods)\b|\bbreads?\b|\bsourdough\b|\bcakes?\b|\bcupcakes?\b|\bpastr(y|ies)\b|\bpies?\b|\bcookies\b|\bbiscuits\b|\bdesserts?\b|\bconfection|\bcand(y|ies)\b|\bchocolates?\b|\bice[- ]creams?\b|\bpuddings?\b|\bsweets\b|\bdoughnuts?\b|\bmuffins?\b|\bpatisserie\b"),
    ("Cooking/Vegetarian & vegan", r"\bvegetarian\b|\bvegan\b|\bplant[- ]based\b|\bmeatless\b|\bmeat[- ]free\b|\bveggie\b|\bvegetable cook"),
    ("Cooking/Healthy & special diets", r"\bdiet(s|ary|etic|etics)?\b|\bketo(genic)?\b|\bpaleo\b|\blow[- ](carb|fat|sodium|calorie|sugar)\b|\bgluten[- ]free\b|\bdairy[- ]free\b|\bdiabet|\bwhole ?30\b|\bweight[- ]loss\b|\bheart[- ]healthy\b|\bclean eating\b|\bnutrition\b|\bcook(ing|ery) for (the sick|invalids)\b|\binvalid cook|\banti[- ]inflammatory\b|\bdetox\b|\bhealthy\b|\bsuperfoods?\b|\bmacrobiotic\b|\bfodmap\b"),
    ("Cooking/Quick & easy meals", r"\bquick\b|\beasy\b|\bsimple\b|\b(15|20|30)[- ]minute\b|\bweeknight\b|\bslow[- ]cook(er|ing)\b|\bcrock[- ]?pot\b|\binstant pot\b|\bpressure cook|\bair[- ]fryer\b|\bone[- ](pot|pan|dish)\b|\bsheet[- ]pan\b|\bmicrowave\b|\bmeal[- ]prep\b|\bmake[- ]ahead\b|\b(5|five|four|three)[- ]ingredients?\b|\bbudget\b|\bfamily meals\b|\beveryday\b"),
    ("Cooking/Meat, fish & game", r"\bmeats?\b|\bbutcher|\bbeef\b|\bpork\b|\blamb\b|\bpoultry\b|\bchicken\b|\bsteaks?\b|\bgame cook|\bvenison\b|\bfish\b|\bseafood\b|\bshellfish\b|\boysters?\b|\bsausages?\b|\bbacon\b|\bburgers?\b|\bcarnivore\b"),
    ("Cooking/World & regional cuisines", r"\bcook(ing|ery),? (french|italian|spanish|german|indian|chinese|japanese|mexican|creole|southern|jewish|russian|scottish|irish|swedish|polish|hungarian|turkish|greek|thai|korean|vietnamese|african|caribbean|philippine|hawaiian|cajun)\b|"
                                         r"\b(french|italian|tuscan|sicilian|spanish|mexican|tex[- ]mex|indian|chinese|sichuan|cantonese|japanese|korean|thai|vietnamese|filipino|indonesian|malaysian|greek|turkish|lebanese|persian|moroccan|ethiopian|caribbean|jamaican|cuban|brazilian|peruvian|german|polish|russian|hungarian|scandinavian|nordic|cajun|creole|southern|mediterranean|middle eastern|asian|african|latin american|hawaiian|jewish|kosher|new england|amish)\b"
                                         r"|\bsushi\b|\bramen\b|\bcurr(y|ies)\b|\btacos?\b|\bpasta\b|\bpizza\b|\btapas\b|\bdim sum\b|\bwok\b|\bsoul food\b"),
)
_COOKING_STYLE_PATTERNS = tuple((shelf, re.compile(pattern, re.I)) for shelf, pattern in _COOKING_STYLES)
COOKING_DEFAULT = "Cooking/General cooking & technique"


def cooking_style(text: str) -> str:
    """Which cooking shelf a cookbook belongs on, from its title and subjects."""
    for shelf, pattern in _COOKING_STYLE_PATTERNS:
        if pattern.search(text or ""):
            return shelf
    return COOKING_DEFAULT


_IMAGINATIVE = re.compile(r"\b(fiction|novels?|stories|poetry|poems?|drama|plays?|fairy tales|juvenile literature|humor|humour|tales)\b", re.I)


def place_by_words(title: str, more: str = "") -> str:
    """A survival or cooking shelf read off a title (and subjects); "" when the
    words do not say. Never for imaginative writing — a novel about a hunting
    trip is a novel."""
    text = f"{title or ''} \n {more or ''}"
    if _IMAGINATIVE.search(more or ""):
        return ""
    if _COOKING_WORDS.search(text):
        return cooking_style(text)
    for shelf, pattern in _WORD_PATTERNS:
        if pattern.search(text):
            return shelf
    return ""


# Folder names that only say "a pile of things": whatever a folder above them
# says does not carry through ("survival/Books/…" is not all survival).
_PLAIN_FOLDERS = frozenset(
    "books book ebooks ebook e-books docs documents files file incoming inbox downloads download "
    "library libraries collection collections pack packs pdf pdfs epub epubs stuff data archive archives uploads temp tmp".split())
# Folder names that say nothing themselves but belong to the folder above ("Canning/Other").
_PASS_FOLDERS = frozenset(
    "other others misc miscellaneous extra extras general new various unsorted random covers cover images maps map "
    "audiobook audiobooks mp3 audio".split())

# What a folder's name says, by the phrases in it — the longest that fits wins.
_FOLDER_PHRASES: Dict[str, str] = {
    "food production animals": "Homestead/Farming & livestock", "food production gardening": "Homestead/Gardening",
    "food production": "Homestead/Farming & livestock", "food storage": "Survival/Food, foraging & preserving",
    "food preservation": "Cooking/Preserving, canning & fermenting", "food preparation": "Cooking/General cooking & technique",
    "preserving meat": "Cooking/Preserving, canning & fermenting", "recipes and cookbooks": "Cooking",
    "wild food": "Survival/Food, foraging & preserving", "edible plants": "Survival/Plants",
    "medicinal plants": "Survival/Plants", "edible and medicinal plants": "Survival/Plants",
    "water purification": "Survival/Water", "water storage": "Survival/Water", "water pumps": "Survival/Water",
    "hot water": "Homestead/Off-grid power & building", "wood heat": "Homestead/Off-grid power & building",
    "emergency medicine": "Survival/Medical & first aid", "first aid": "Survival/Medical & first aid",
    "alternative medicine": "Survival/Medical & first aid", "general health": "Survival/Medical & first aid",
    "hygiene and sanitation": "Survival/Medical & first aid", "pest control": "Survival/Insects, snakes & pests",
    "hand to hand combat": "Survival/Defence & weapons", "home defense": "Survival/Defence & weapons",
    "home defence": "Survival/Defence & weapons", "security and defense": "Survival/Defence & weapons",
    "self defense": "Survival/Defence & weapons", "self defence": "Survival/Defence & weapons",
    "primitive weapons": "Survival/Defence & weapons",
    "nuclear biological": "Survival/Disaster readiness", "disaster prepar": "Survival/Disaster readiness",
    "street smarts": "Survival/Security, escape & evasion", "urban survival": "Survival/Security, escape & evasion",
    "escape and evasion": "Survival/Security, escape & evasion",
    "cold weather survival": "Survival/Shelter, fire & fieldcraft", "wilderness survival": "Survival/Shelter, fire & fieldcraft",
    "general survival": "Survival/Shelter, fire & fieldcraft", "primitive living": "Survival/Shelter, fire & fieldcraft",
    "special forces": "Military/Field manuals & doctrine", "military operations": "Military/Field manuals & doctrine",
    "military combat": "Military/Field manuals & doctrine", "military skills": "Military/Field manuals & doctrine",
    "military tactics": "Military/Tactics, strategy & war",
    "alternative energy": "Homestead/Off-grid power & building", "alternative fuels": "Homestead/Off-grid power & building",
    "off grid": "Homestead/Off-grid power & building", "shelter construction": "Homestead/Off-grid power & building",
    "alcohol and distillation": "Cooking/Drinks & brewing", "soap and candles": "Homestead/Household & everyday",
    "homesteading skills": "Homestead/Household & everyday", "sheep and goats": "Homestead/Farming & livestock",
    "tree crops": "Homestead/Gardening", "specific crops": "Homestead/Gardening",
}
_FOLDER_WORDS: Dict[str, str] = {
    "navigation": "Survival/Navigation", "compass": "Survival/Navigation", "orienteering": "Survival/Navigation",
    "shelter": "Survival/Shelter, fire & fieldcraft", "shelters": "Survival/Shelter, fire & fieldcraft",
    "firecraft": "Survival/Shelter, fire & fieldcraft", "knots": "Survival/Shelter, fire & fieldcraft",
    "rope": "Survival/Shelter, fire & fieldcraft", "cordage": "Survival/Shelter, fire & fieldcraft",
    "gear": "Survival/Shelter, fire & fieldcraft", "wilderness": "Survival/Shelter, fire & fieldcraft",
    "water": "Survival/Water", "plants": "Survival/Plants", "herbs": "Survival/Plants", "herbal": "Survival/Plants",
    "mushrooms": "Survival/Mushrooms", "fungi": "Survival/Mushrooms", "snakes": "Survival/Insects, snakes & pests",
    "insects": "Survival/Insects, snakes & pests", "snaring": "Survival/Hunting, trapping & fishing",
    "communications": "Survival/Communications & power", "signals": "Survival/Communications & power",
    "combat": "Survival/Defence & weapons", "tactical": "Military/Field manuals & doctrine",
    "disaster": "Survival/Disaster readiness", "pandemics": "Survival/Disaster readiness", "terrorism": "Survival/Disaster readiness",
    "security": "Survival/Security, escape & evasion", "evasion": "Survival/Security, escape & evasion",
    "medical": "Survival/Medical & first aid", "medicine": "Survival/Medical & first aid",
    "energy": "Homestead/Off-grid power & building", "solar": "Homestead/Off-grid power & building",
    "generators": "Homestead/Off-grid power & building", "gasifiers": "Homestead/Off-grid power & building",
    "refrigeration": "Homestead/Off-grid power & building", "permaculture": "Homestead/Gardening",
    "greenhouses": "Homestead/Gardening", "soil": "Homestead/Gardening", "beekeeping": "Homestead/Farming & livestock",
    "cattle": "Homestead/Farming & livestock", "chickens": "Homestead/Farming & livestock", "rabbits": "Homestead/Farming & livestock",
    "livestock": "Homestead/Farming & livestock", "poultry": "Homestead/Farming & livestock",
    "butchering": "Cooking/Meat, fish & game", "smoking": "Cooking/Grilling, smoking & camp cooking",
    "dehydration": "Cooking/Preserving, canning & fermenting", "dehydrating": "Cooking/Preserving, canning & fermenting",
    "preservation": "Cooking/Preserving, canning & fermenting", "fermenting": "Cooking/Preserving, canning & fermenting",
    "blacksmithing": "Repair & making/Woodwork & crafts", "electronic": "Repair & making/Electronics & radio",
    "electrical": "Repair & making/Electronics & radio", "wire": "Repair & making/Electronics & radio",
    "motors": "Repair & making/Vehicles & engines", "engines": "Repair & making/Vehicles & engines",
    "psychological": "Philosophy & religion/Psychology & the mind", "spiritual": "Philosophy & religion",
}


def folder_hint(relative_path: str) -> str:
    """What the folders a file sits in say about its shelf. The nearest folder
    with something to say wins: one the owner has named in the ``folders`` map
    of ``shelves.json`` (the folder's path → a shelf; a trailing ``*``
    matches the rest of a folder's name), else one whose name is a shelf, a
    category or a telling word. A plain name ("Books") stops the search, so a
    file under ``survival/Books/Pack 3`` is not filed as survival just
    because of where the pile was put down."""
    parts = [part for part in str(relative_path or "").replace("\\", "/").split("/")[:-1] if part]
    mapped = custom().get("folders") or {}
    mapped = {str(folder).strip("/"): str(shelf) for folder, shelf in mapped.items()} if isinstance(mapped, dict) else {}
    known_categories = {_key(category): category for category in categories()}
    for depth in range(len(parts), 0, -1):
        name = parts[depth - 1]
        here = "/".join(parts[:depth])
        chosen = mapped.get(here) or next((shelf for folder, shelf in mapped.items()
                                           if folder.endswith("*") and here.startswith(folder[:-1])
                                           and "/" not in here[len(folder) - 1:]), "")
        if chosen and valid(chosen):
            return chosen                                # the owner said what this folder is
        words = _key(re.sub(r"\d+", " ", name))          # "01.2) Navigation, Reading Maps (9)" → its words
        if not words or words in _PASS_FOLDERS:
            continue
        if words in _PLAIN_FOLDERS:
            return ""
        found = resolve(re.sub(r"[\d().]+", " ", name), loose=False)
        if not found:
            for phrase in sorted(_FOLDER_PHRASES, key=len, reverse=True):
                if phrase in words:
                    found = _FOLDER_PHRASES[phrase]
                    break
        if not found:
            # "Cookbooks Collection Pack 10": the telling word is inside the name.
            for word in words.split():
                if word in _PLAIN_FOLDERS or word in _PASS_FOLDERS or len(word) < 4:
                    continue
                found = (_FOLDER_WORDS.get(word) or _ALIASES.get(word) or _ALIASES.get(word.rstrip("s"))
                         or known_categories.get(word) or "")
                if found:
                    break
        if found and found != UNSORTED:
            return found
        if all(word in _PLAIN_FOLDERS or word in _PASS_FOLDERS or len(word) < 4 for word in words.split()):
            return ""                                    # "Assorted Books Collection Pack": a pile, like "Books"
    return ""


# ── Army publications ──────────────────────────────────────────────────────

_ARMY_PAPERWORK = re.compile(
    r"^(general orders?\b|(da|dd|sf|opm)[ -]?form\b|AR \d|DA PAM \d|AGO ?\d|GO \d|HQDA\b|HPN \d|army regulation\b|pamphlet \d|"
    r"department of the army pamphlet\b|army directive\b|ALARACT\b|JTA \d|CTA \d|.*\b(scorecard|datasheet|checklist|"
    r"inspection record|feeder card|planning card|firing table|record)\b)", re.I)
_ARMY_DOCTRINE = re.compile(r"\b(ATTP|ATP|ADRP|ADP|FMI|FM|STP|GTA|TC|JP|MCRP|MCWP|MCTP|NTTP|AFTTP)[ -]?\d", re.I)
_ARMY_TECHNICAL = re.compile(r"\b(TM|TB ?MED|TB|SB|LO|MWO)[ -]?\d", re.I)


def army_place(title: str) -> str:
    """The shelf for an Army publication: what it teaches when that is a
    survival skill (first aid, land navigation, marksmanship), otherwise the
    kind of publication it is."""
    title = str(title or "")
    paperwork = bool(_ARMY_PAPERWORK.search(title))
    if not paperwork:
        skill = place_by_words(title)
        if skill and not skill.startswith("Cooking"):
            return skill
    if paperwork:
        return "Military/Regulations & paperwork"
    if _ARMY_TECHNICAL.search(title):
        return "Military/Technical manuals"
    if _ARMY_DOCTRINE.search(title):
        return "Military/Field manuals & doctrine"
    return "Military/Regulations & paperwork"


# ── Project Gutenberg books ────────────────────────────────────────────────

_ROLE = re.compile(r"\[([^\]]+)\]\s*$")
# People catalogued under a given name ("Thomas, Aquinas, Saint").
_GIVEN_FIRST = frozenset({"Thomas", "John", "Francis", "Teresa", "Catherine", "Bernard", "Ignatius", "Anselm", "Benedict",
                          "Gregory", "Jerome", "Patrick", "Bede", "Bonaventure", "Julian", "Hildegard", "Isidore", "Leo"})
_YEARS = re.compile(r"(?:(\d{1,4})\??\s*(BCE|B\.C\.?)?)?\s*-\s*(?:(\d{1,4})\??\s*(BCE|B\.C\.?)?)?\s*$", re.I)
_CENTURY = re.compile(r"(\d{1,2})(?:st|nd|rd|th) century\s*(B\.?C\.?E?\.?)?", re.I)


def _person(raw: str) -> Tuple[str, Optional[int], Optional[int]]:
    """``("Jane Austen", 1775, 1817)`` from ``"Austen, Jane, 1775-1817"``."""
    text = _ROLE.sub("", raw or "").strip().strip(",")
    born: Optional[int] = None
    died: Optional[int] = None
    century = _CENTURY.search(text)
    match = _YEARS.search(text)
    if match and (match.group(1) or match.group(3)):
        if match.group(1):
            born = -int(match.group(1)) if match.group(2) else int(match.group(1))
        if match.group(3):
            died = -int(match.group(3)) if match.group(4) else int(match.group(3))
        if born is not None and died is not None and born > died and died > 0 and not match.group(2):
            born = None                                   # "active 1896-1" style damage
        text = text[:match.start()].strip().strip(",")
    elif century:
        middle = int(century.group(1)) * 100 - 50
        born = died = -middle if century.group(2) else middle
        text = text[:century.start()].strip().strip(",")
    text = re.sub(r",?\s*\b(active|approximately|fl\.|circa|ca\.)\s*$", "", text, flags=re.I).strip().strip(",")
    text = re.sub(r"\s*\([^)]*\)", "", text)              # "(Lyman Frank)"
    parts = [part.strip() for part in text.split(",") if part.strip()]
    saint = any(part.lower() in ("saint", "st.", "st") for part in parts[1:])
    honour = next((part for part in parts[2:] if part.lower() in ("sir", "lord", "lady", "dame")), "")
    parts = [part for part in parts if part.lower() not in ("saint", "st.", "st", "sir", "lord", "lady", "dame")]
    if not parts:
        name = ""
    elif len(parts) == 1 or re.search(r"\b(of|the|emperor|king|queen|pope|bishop|active)\b", parts[1], re.I):
        # "Augustine, of Hippo" — a name with a description, not a family name first
        name = parts[0] + (f" {parts[1]}" if len(parts) > 1 and re.match(r"^(of|the)\b", parts[1], re.I) else "")
    elif parts[0] in _GIVEN_FIRST or re.match(r"^[a-zà-ÿ]", parts[1]):
        name = f"{parts[0]} {parts[1]}"                # "Thomas, Aquinas" / "Thomas, à Kempis"
    else:
        name = f"{parts[1]} {parts[0]}"
    if saint and name:
        name = f"Saint {name}"
    elif honour and name:
        name = f"{honour} {name}"
    return re.sub(r"\s+", " ", name).strip(), born, died


def author_of(authors: str) -> Tuple[str, str, Optional[int]]:
    """``(name, dates as text, the year the author was writing)`` for a
    catalogue ``Authors`` field — the first person who is the author rather
    than an editor, translator or illustrator."""
    people = [part.strip() for part in str(authors or "").split(";") if part.strip()]
    chosen = ""
    for person in people:
        role = _ROLE.search(person)
        if not role or role.group(1).strip().lower() in ("author", "creator"):
            chosen = person
            break
    if not chosen and people:
        # Only translators and editors are named: the work is older than they are,
        # so their dates say nothing about when it was written.
        role = _ROLE.search(people[0])
        name, _born, _died = _person(people[0])
        label = {"translator": "translated by", "editor": "edited by", "compiler": "compiled by"}.get(
            role.group(1).strip().lower() if role else "", "")
        return (f"{label} {name}" if label and name else name), "", None
    name, born, died = _person(chosen)
    if name.lower() in ("various", "anonymous", "unknown"):
        return name, "", None

    def show(year: int) -> str:
        return f"{-year} BC" if year < 0 else str(year)

    dates = ""
    if born is not None and died is not None:
        dates = show(born) if born == died else f"{show(born)}–{show(died)}"
    elif born is not None:
        dates = f"born {show(born)}"
    elif died is not None:
        dates = f"died {show(died)}"
    if born is not None and died is not None:
        writing = (born + died) // 2 + (10 if died - born > 30 else 0)
    elif born is not None:
        writing = born + 40
    elif died is not None:
        writing = died - 20
    else:
        writing = None
    return name, dates, writing


def literature_era(year: Optional[int]) -> str:
    if year is None:
        return "Literature/Collections & undated"
    if year <= 500:
        return "Literature/Ancient & classical"
    if year <= 1640:
        return "Literature/Medieval & Renaissance"
    if year <= 1800:
        return "Literature/17th & 18th century"
    if year <= 1900:
        return "Literature/19th century"
    return "Literature/20th century"


def history_era(year: int) -> str:
    if year < 500:
        return "History/Ancient world"
    if year < 1450:
        return "History/Middle Ages"
    if year < 1750:
        return "History/Early modern (1450-1750)"
    if year < 1914:
        return "History/Modern (1750-1914)"
    return "History/World wars & 20th century"


_ERA_CATEGORY = (
    ("history - ancient", "History/Ancient world"),
    ("history - medieval", "History/Middle Ages"),
    ("history - early modern", "History/Early modern (1450-1750)"),
    ("history - modern", "History/Modern (1750-1914)"),
)
_LOCC_ERA = {
    "D501": "History/World wars & 20th century", "D731": "History/World wars & 20th century",
    "E186": "History/Early modern (1450-1750)", "E201": "History/Modern (1750-1914)", "E300": "History/Modern (1750-1914)",
    "E456": "History/Modern (1750-1914)", "E660": "History/Modern (1750-1914)", "E740": "History/World wars & 20th century",
    "E838": "History/World wars & 20th century", "E895": "History/World wars & 20th century", "DE": "History/Ancient world",
}
_ERA_WORDS = (
    ("History/World wars & 20th century", r"world war|great war, 1914|european war, 1914|20th century|twentieth century|\b19[1-9]\d\b"),
    ("History/Ancient world", r"\bancient\b|antiquit|\broman (empire|republic|emperors)|\brome -- history|\bto 146 b\.c|\bto 332 b\.c|\bb\.c\.|\bpharaoh|\bbabylon|\bassyria|\bcarthag|\bhellen|classical antiquities|\bbyzantine empire -- history -- to 527"),
    ("History/Middle Ages", r"middle ages|medieval|crusades|feudal|\bvikings?\b|\bnorman(s| conquest)\b|\bbyzantine\b|\banglo-saxon|\bplantagenet|\bto 1500\b|\bto 1485\b|\b(5|6|7|8|9|10|11|12|13|14)th century"),
    ("History/Early modern (1450-1750)", r"reformation|renaissance|\btudors?\b|\bstuarts?\b|elizabeth i\b|thirty years' war|colonial period|puritan|\b(15|16|17)th century|\b1[5-6]\d\d\b|\b17[0-4]\d\b|discovery and exploration"),
    ("History/Modern (1750-1914)", r"french revolution|napole|\b(18|19)th century|victoria|civil war, 1861|revolution, 1775|war of 1812|\b17[5-9]\d\b|\b18\d\d\b|\b190\d\b|\b191[0-3]\b"),
)
_ERA_WORD_PATTERNS = tuple((shelf, re.compile(pattern, re.I)) for shelf, pattern in _ERA_WORDS)
_REGION = (
    ("DA", "Britain & Ireland"), ("DB", "Austria & Hungary"), ("DC", "France"), ("DD", "Germany"), ("DE", "Greece & Rome"),
    ("DF", "Greece"), ("DG", "Italy & Rome"), ("DH", "Low Countries"), ("DJ", "Low Countries"), ("DK", "Russia & Poland"),
    ("DL", "Scandinavia"), ("DP", "Spain & Portugal"), ("DQ", "Switzerland"), ("DR", "Balkans & Turkey"), ("DS", "Asia"),
    ("DT", "Africa"), ("DU", "Pacific & Australia"), ("DX", "Roma"), ("D", "World & Europe"), ("E0", "The Americas"),
    ("E", "United States"), ("F10", "Canada"), ("F1", "Latin America"), ("F2", "South America"), ("F3", "South America"),
    ("F", "United States, by region"), ("CT", "Biography"), ("CB", "Civilisation"), ("CC", "Archaeology"), ("C", "Sources of history"),
)

_FORMS = (
    ("novels", "novel"), ("short stories", "short stories"), ("poetry", "poetry"), ("plays/films/dramas", "play"),
    ("essays, letters & speeches", "essays and letters"), ("biographies", "biography"), ("travel writing", "travel"),
    ("journals", "periodical"), ("encyclopedias/dictionaries/reference", "reference"), ("how to ...", "how-to"),
)
_GENRES = (
    ("children & young adult reading", "for young readers"), ("science-fiction & fantasy", "science fiction and fantasy"),
    ("crime, thrillers and mystery", "crime and mystery"), ("historical novels", "historical"), ("adventure", "adventure"),
    ("romance", "romance"), ("humour", "humour"), ("mythology, legends & folklore", "myth and folklore"),
)
_LITERARY_FORMS = {"novels", "short stories", "poetry", "plays/films/dramas", "children & young adult reading",
                   "science-fiction & fantasy", "historical novels", "romance", "crime, thrillers and mystery"}
_LITERARY_LIGHT = {"british literature", "american literature", "french literature", "german literature",
                   "russian literature", "literature - other", "classics of literature", "humour", "adventure",
                   "mythology, legends & folklore"}
_LITERATURE_CLASSES = {"PA", "PG", "PJ", "PK", "PL", "PN", "PQ", "PR", "PS", "PT", "PZ"}
_LANGUAGE_CLASSES = {"P", "PB", "PC", "PD", "PE", "PF", "PH", "PM"}

_CATEGORY_DOMAIN = {
    "religion/spirituality": "Philosophy & religion", "philosophy & ethics": "Philosophy & religion",
    "psychiatry/psychology": "Philosophy & religion",
    "health & medicine": "Medicine", "drugs/alcohol/pharmacology": "Medicine",
    "cooking & drinking": "Cooking",
    "politics": "Society", "law & criminology": "Society", "economics": "Society", "business/management": "Society",
    "sociology": "Society", "teaching & education": "Society", "parenthood & family relations": "Society",
    "gender & sexuality studies": "Society", "old age & the elderly": "Society",
    "engineering & technology": "Repair & making",
    "art": "Arts & leisure", "architecture": "Arts & leisure", "music": "Arts & leisure", "sports/hobbies": "Arts & leisure",
    "fashion": "Arts & leisure",
    "encyclopedias/dictionaries/reference": "Reference", "language & communication": "Reference", "journals": "Reference",
    "science - biology": "Science", "science - physics": "Science", "science - chemistry/biochemistry": "Science",
    "science - earth/agricultural/farming": "Science", "mathematics": "Science", "environmental issues": "Science",
    "research methods/statistics/information sys": "Science",
    "archaeology & anthropology": "History", "travel writing": "History", "biographies": "History",
}
_TIE_ORDER = ("Cooking", "Survival", "Medicine", "Philosophy & religion", "History", "Science", "Military", "Homestead",
              "Repair & making", "Society", "Literature", "Arts & leisure", "Reference")


def _class_domain(code: str) -> str:
    letters = re.match(r"[A-Z]+", code or "")
    if not letters:
        return ""
    letters = letters.group(0)
    first = letters[0]
    if letters in _LITERATURE_CLASSES:
        return "Literature"
    if letters in _LANGUAGE_CLASSES:
        return "Reference"
    if first == "G":
        return {"GV": "Arts & leisure", "GR": "Literature", "GA": "Science", "GB": "Science", "GC": "Science", "GE": "Science"}.get(letters, "History")
    return {"A": "Reference", "B": "Philosophy & religion", "C": "History", "D": "History", "E": "History", "F": "History",
            "H": "Society", "J": "Society", "K": "Society", "L": "Society", "M": "Arts & leisure", "N": "Arts & leisure",
            "Q": "Science", "R": "Medicine", "S": "Homestead", "T": "Repair & making", "U": "Military", "V": "Military",
            "Z": "Reference"}.get(first, "")


def _history_shelf(codes: Sequence[str], cats: Sequence[str], subjects: str, title: str) -> Tuple[str, str]:
    """``(shelf, region)`` for a history book: its era when one can be told."""
    region = ""
    for code in codes:
        for prefix, name in _REGION:
            if code.startswith(prefix):
                region = name
                break
        if region:
            break
    letters = {re.match(r"[A-Z]+", code).group(0) for code in codes if re.match(r"[A-Z]+", code)}
    for wanted, shelf in _ERA_CATEGORY:                    # Gutenberg's own era, the earliest it names
        if any(cat.startswith(wanted) for cat in cats):
            if shelf == "History/Modern (1750-1914)" and (
                    {"D501", "D731", "E740", "E838", "E895"} & set(codes) or _ERA_WORD_PATTERNS[0][1].search(subjects)):
                return "History/World wars & 20th century", region
            return shelf, region
    for code in codes:
        if code in _LOCC_ERA:
            return _LOCC_ERA[code], region
    text = f"{subjects}\n{title}"
    for shelf, pattern in _ERA_WORD_PATTERNS:
        if pattern.search(text):
            return shelf, region
    if "biographies" in cats or "CT" in letters:
        return "History/Lives & memoirs", region or "Biography"
    if "travel writing" in cats or (letters & {"G"}):
        return "History/Exploration & travel", region
    if "archaeology & anthropology" in cats or letters & {"GN", "GT", "CC", "GF"}:
        return "History/Peoples, customs & archaeology", region
    return "History/General histories", region



# A practical skill, for a Project Gutenberg book: the words that name it and the
# subject classes in which those words can be believed. ("Survival" in a book on
# psychical research, "rifles" in a regimental history and "hunting" in a
# traveller's memoir are not manuals.) The last field: must it read as a how-to?
_GUTENBERG_SKILLS: Tuple[Tuple[str, str, Tuple[str, ...], bool], ...] = (
    ("Survival/Mushrooms", r"\bmushrooms?\b|\bfungi\b|\btoadstools?\b", ("QK", "SB", "TX", "RA"), False),
    ("Survival/Plants", r"(edible|medicinal|poisonous) plants|plants, (edible|medicinal|poisonous)|wild (foods?|plants, edible)|"
                        r"\bherbs\b.*(therapeutic|medic)|materia medica, vegetable|botany, medical|\bherbals?\b",
     ("QK", "SB", "RS", "RM", "RV", "TX", "QH", "S", "GT"), False),
    ("Survival/Insects, snakes & pests", r"poisonous (snakes|animals)|snake ?(bites|venom)|\bvenom\b|insect pests|household pests|"
                                        r"\b(ticks|mosquitoes|flies) (as carriers|-- control)|pests -- control",
     ("QL", "SB", "RA", "RC", "S", "TX"), False),
    ("Survival/Medical & first aid", r"first aid|medical emergencies|accidents and emergencies|wounds and injuries|"
                                    r"(military|naval|backwoods|camp|wilderness) (hygiene|surgery|medicine)|medicine, (military|naval)|"
                                    r"surgery, military|emergency (nursing|treatment)|home nursing|nursing -- handbooks",
     ("R", "U", "V"), False),
    ("Survival/Water", r"water -- purification|water[- ]supply, rural|drinking water|\bwells\b|\bcisterns\b", ("TD", "RA", "S", "TH"), False),
    ("Survival/Navigation", r"navigation -- handbooks|\borienteering\b|map reading|maps, military|\bcompass\b|nautical astronomy|"
                           r"navigation\b(?!.*(history|law|laws))", ("VK", "GA", "U", "G"), True),
    ("Survival/Communications & power", r"radio -- (amateurs|handbooks|receivers|apparatus)|amateur radio|wireless telegraph|"
                                       r"telegraph -- handbooks|signals and signaling|morse code|electric batteries|storage batteries",
     ("TK", "UG", "VK", "VB", "QC"), False),
    ("Survival/Defence & weapons", r"\b(rifles?|pistols?|revolvers?|shotguns?|firearms|marksmanship|musketry|ammunition|archery|"
                                  r"self-defen[cs]e|jiu[- ]?jitsu|boxing|fencing|swordsmanship|bayonet)\b|(rifle|pistol|target|trap|wing|game) shooting",
     ("GV", "UD", "UF", "TS", "U", "SK"), True),
    ("Survival/Shelter, fire & fieldcraft", r"\bcamping\b|outdoor life|\bwoodcraft\b|wilderness survival|knots and splices|\bknots\b|"
                                           r"\bcampcraft\b|boy scouts -- handbooks|scouting \(reconnaissance\)|\bscoutcraft\b|"
                                           r"mountaineering -- handbooks|\bbackpacking\b|\bbushcraft\b|log cabins",
     ("GV", "SK", "VM", "HS", "U", "TH", "TT"), True),
    ("Survival/Disaster readiness", r"civil defen[cs]e|disaster relief|emergency management|air raid|fallout shelters|survival skills",
     ("UA", "HV", "TH", "U"), False),
)
_GUTENBERG_SKILL_PATTERNS = tuple((shelf, re.compile(pattern, re.I), classes, howto) for shelf, pattern, classes, howto in _GUTENBERG_SKILLS)


def _gutenberg_skill(words: str, classes: set, cats: Sequence[str]) -> str:
    """The survival shelf a Project Gutenberg book teaches for, or ""."""
    if classes & {"SK", "SH"}:                       # the hunting and fishing classes themselves
        return "Survival/Hunting, trapping & fishing"
    narrative = any(cat in ("biographies", "travel writing", "adventure") or cat.startswith("history -") for cat in cats)
    reads_as_howto = "how to ..." in cats or not narrative
    for shelf, pattern, wanted, howto in _GUTENBERG_SKILL_PATTERNS:
        if howto and not reads_as_howto:
            continue
        if not any(code == prefix or (len(prefix) == 1 and code.startswith(prefix)) for code in classes for prefix in wanted):
            continue
        if pattern.search(words):
            return shelf
    return ""



_ERA_ORDER = ("Literature/Ancient & classical", "Literature/Medieval & Renaissance", "Literature/17th & 18th century",
              "Literature/19th century", "Literature/20th century", "Literature/Collections & undated")
_OLDER_SUBJECTS = (
    ("Literature/Ancient & classical", r"\b(greek|latin|classical) (literature|poetry|drama|fiction)|mythology, (greek|roman|classical)|"
                                       r"epic poetry, (greek|latin)|\b(sanskrit|assyro-babylonian|egyptian) literature|\bto 500\b"),
    ("Literature/Medieval & Renaissance", r"\bold english\b|anglo-saxon|middle english|old norse|\bsagas\b|\bmedieval\b|\bto 1500\b|arthurian|"
                                          r"epic poetry, (english \(old\)|germanic|french|irish)|old french|middle high german|nibelung|"
                                          r"romances, (english|french)|\b(1100-1500|500-1500|to 1100)\b|early modern, 1500-1700"),
)
_OLDER_SUBJECT_PATTERNS = tuple((shelf, re.compile(pattern, re.I)) for shelf, pattern in _OLDER_SUBJECTS)


def _older_by_subject(subjects: str) -> str:
    for shelf, pattern in _OLDER_SUBJECT_PATTERNS:
        if pattern.search(subjects or ""):
            return shelf
    return ""


def gutenberg_place(record: dict) -> dict:
    """Where one Project Gutenberg book goes, from its catalogue record
    (``title``, ``authors``, ``subjects``, ``locc``, ``bookshelves``):
    ``{"shelf", "form", "author", "dates", "year"}``."""
    title = str(record.get("title") or "")
    subjects = str(record.get("subjects") or "")
    codes = [code.strip().upper() for code in str(record.get("locc") or "").split(";") if code.strip()]
    letters = [re.match(r"[A-Z]+", code).group(0) for code in codes if re.match(r"[A-Z]+", code)]
    cats = [entry.strip()[9:].strip().lower() for entry in str(record.get("bookshelves") or "").split(";")
            if entry.strip().lower().startswith("category:")]
    author, dates, writing = author_of(str(record.get("authors") or ""))
    literary = bool(_LITERARY_FORMS & set(cats)) or bool(_IMAGINATIVE.search(subjects) and set(letters) & _LITERATURE_CLASSES)

    forms = [label for wanted, label in _FORMS if wanted in cats]
    genres = [label for wanted, label in _GENRES if wanted in cats]
    form = ", ".join(([forms[0]] if forms else []) + genres[:2])
    out = {"author": author, "dates": dates, "year": writing, "form": form}

    def done(shelf: str, form: str = "") -> dict:
        out["shelf"] = shelf
        if form and form not in out["form"]:
            out["form"] = f"{out['form']}, {form}".strip(", ")
        return out

    words = f"{title}\n{subjects}"
    classes = set(letters)
    # Cookbooks and survival skills first: they are what the library is searched for most.
    if not literary and ("cooking & drinking" in cats or ("TX" in letters and _COOKING_WORDS.search(words))):
        return done(cooking_style(words), "cookbook")
    if not literary:
        skill = _gutenberg_skill(words, classes, cats)
        if skill:
            return done(skill)

    score: Dict[str, float] = {}
    for code in dict.fromkeys(letters):
        domain = _class_domain(code)
        if domain:
            score[domain] = max(score.get(domain, 0.0), 2.0 if code == "PA" else 4.0)
    extra: Dict[str, float] = {}
    for cat in cats:
        if cat in _LITERARY_FORMS:
            extra["Literature"] = min(extra.get("Literature", 0.0) + 3.0, 6.0)
        elif cat in _LITERARY_LIGHT:
            extra["Literature"] = extra.get("Literature", 0.0) + 1.0
        elif cat.startswith("history -"):
            extra["History"] = min(extra.get("History", 0.0) + 2.0, 3.0)
        elif cat in _CATEGORY_DOMAIN:
            domain = _CATEGORY_DOMAIN[cat]
            weight = 1.0 if cat == "biographies" else 2.0
            extra[domain] = min(extra.get(domain, 0.0) + weight, 3.0)
        elif cat == "nature/gardening/animals":
            extra["Homestead" if set(letters) & {"S", "SB", "SF", "SD"} else "Science"] = 2.0
        elif cat == "nutrition":
            extra["Medicine"] = extra.get("Medicine", 0.0) + 1.0
    for domain, value in extra.items():
        score[domain] = score.get(domain, 0.0) + value
    # Political philosophy is philosophy: Plato's Republic is classed with government.
    if "philosophy & ethics" in cats and "JC" in letters and "politics" not in cats:
        score["Philosophy & religion"] = score.get("Philosophy & religion", 0.0) + 2.0
    if not score:
        return done("Literature/Collections & undated" if literary else UNSORTED)
    best = max(score.values())
    domain = next(name for name in _TIE_ORDER if score.get(name) == best)

    if domain == "Literature":
        if "GR" in letters and not literary:
            return done("Literature/Myths, legends & folk tales")
        if "children & young adult reading" in cats or (set(letters) & _LITERATURE_CLASSES) == {"PZ"}:
            return done("Literature/Children's & young readers")
        if "mythology, legends & folklore" in cats and not (set(letters) & (_LITERATURE_CLASSES - {"PZ"})):
            return done("Literature/Myths, legends & folk tales")
        if writing is None and "PA" in letters:
            return done("Literature/Ancient & classical")
        era = literature_era(writing)
        older = _older_by_subject(subjects)
        if older and _ERA_ORDER.index(older) < _ERA_ORDER.index(era):
            era = older                                # Beowulf is not a Victorian poem because its translator was
        return done(era)
    if domain == "History":
        shelf, region = _history_shelf(codes, cats, subjects, title)
        return done(shelf, region)
    if domain == "Philosophy & religion":
        if set(letters) & {"BR", "BS", "BT", "BV", "BX"}:
            return done("Philosophy & religion/The Bible & Christianity")
        if "BF" in letters or "psychiatry/psychology" in cats:
            if re.search(r"spiritual(ism|ist)|occult|witch|magic|astrolog|parapsycholog|ghost|psychical|alchem|demonolog|"
                         r"theosoph|clairvoyan|telepath|fortune[- ]telling|divination|apparitions|\\bmediums?\\b|second sight", words, re.I):
                return done("Philosophy & religion/Occult & the unexplained")
            return done("Philosophy & religion/Psychology & the mind")
        if set(letters) & {"BL", "BM", "BP", "BQ"}:
            return done("Philosophy & religion/World religions & mythology")
        if set(letters) & {"B", "BC", "BD", "BH", "BJ", "JC"} or "philosophy & ethics" in cats:
            return done("Philosophy & religion/Philosophy & ethics")
        return done("Philosophy & religion/The Bible & Christianity" if "religion/spirituality" in cats
                    else "Philosophy & religion/Philosophy & ethics")
    if domain == "Medicine":
        if re.search(r"veterinar|diseases of (animals|horses|cattle|dogs)|\bfarriery\b", words, re.I) or "SF" in letters:
            return done("Medicine/Veterinary")
        return done("Medicine/Old medical books")
    if domain == "Cooking":
        return done(cooking_style(words), "cookbook")
    if domain == "Science":
        for wanted, shelf in ((("QA",), "Science/Mathematics & statistics"), (("QB", "QC"), "Science/Physics & astronomy"),
                              (("QD",), "Science/Chemistry"), (("QE", "GA", "GB", "GC", "GE"), "Science/Earth & weather"),
                              (("QH", "QK", "QL", "QM", "QP", "QR"), "Science/Biology & nature")):
            if set(letters) & set(wanted):
                return done(shelf)
        for wanted, shelf in (("mathematics", "Science/Mathematics & statistics"), ("science - physics", "Science/Physics & astronomy"),
                              ("science - chemistry/biochemistry", "Science/Chemistry"),
                              ("science - earth/agricultural/farming", "Science/Earth & weather"),
                              ("science - biology", "Science/Biology & nature"), ("nature/gardening/animals", "Science/Biology & nature"),
                              ("environmental issues", "Science/Earth & weather")):
            if wanted in cats:
                return done(shelf)
        return done("Science/General science")
    if domain == "Homestead":
        if "SB" in letters or re.search(r"\bgarden|horticultur|fruit[- ]culture|vegetable|floricultur|\bflowers\b|\borchard", words, re.I):
            return done("Homestead/Gardening")
        if re.search(r"\bdogs?\b|\bcats?\b|\bpets?\b|\bhorses?\b|horsemanship|\bcage birds", words, re.I):
            return done("Homestead/Animals & pets")
        return done("Homestead/Farming & livestock")
    if domain == "Repair & making":
        if "TX" in letters:
            return done("Homestead/Household & everyday")
        for wanted, shelf in ((("TH",), "Repair & making/Home & building"), (("TK",), "Repair & making/Electronics & radio"),
                              (("TL", "TF", "VM"), "Repair & making/Vehicles & engines"), (("TT",), "Repair & making/Woodwork & crafts"),
                              (("TR",), "Arts & leisure/Art & architecture")):
            if set(letters) & set(wanted):
                return done(shelf)
        if re.search(r"woodwork|carpentry|joinery|cabinet|handicraft|needlework|sewing|knitting|weaving|blacksmith|metal-?work|leather-?work|basket", words, re.I):
            return done("Repair & making/Woodwork & crafts")
        return done("Repair & making/Engineering & industry")
    if domain == "Society":
        if any(code.startswith("J") for code in letters) or "HX" in letters or \
                ("politics" in cats and not any(code.startswith(("H", "L", "K")) for code in letters)):
            return done("Society/Government & politics")
        if any(code.startswith("K") for code in letters) or ("law & criminology" in cats and not any(code.startswith(("H", "L")) for code in letters)):
            return done("Society/Law")
        if any(code.startswith("L") for code in letters) or {"teaching & education", "parenthood & family relations"} & set(cats) and not any(code.startswith("H") for code in letters):
            return done("Society/Education & parenting")
        if set(letters) & {"HB", "HC", "HD", "HE", "HF", "HG", "HJ"} or {"economics", "business/management"} & set(cats):
            return done("Society/Money, business & economics")
        return done("Society/Social questions")
    if domain == "Military":
        if any(code.startswith("V") for code in letters) and not any(code.startswith("U") for code in letters):
            return done("Military/Ships & seamanship")
        return done("Military/Tactics, strategy & war")
    if domain == "Arts & leisure":
        if any(code.startswith("M") for code in letters) or "music" in cats and not any(code.startswith(("N", "G")) for code in letters):
            return done("Arts & leisure/Music")
        if "GV" in letters or "sports/hobbies" in cats and not any(code.startswith("N") for code in letters):
            if re.search(r"boxing|wrestling|fencing|jiu[- ]?jitsu|judo|physical (culture|training|education)|gymnast|calisthen|exercise", words, re.I):
                return done("Arts & leisure/Fitness & martial arts")
            return done("Arts & leisure/Sports, games & hobbies")
        return done("Arts & leisure/Art & architecture")
    # Reference
    if set(letters) & {"AP"} or "journals" in cats:
        return done("Reference/Magazines & journals")
    if set(letters) & _LANGUAGE_CLASSES or "language & communication" in cats:
        return done("Reference/Dictionaries & language")
    if set(letters) & {"AE", "AG", "AY"} or "encyclopedias/dictionaries/reference" in cats:
        return done("Reference/Encyclopedias")
    if "Z" in letters:
        return done("Reference/Magazines & journals" if "journals" in cats else "Reference/Source texts")
    return done("Reference/Source texts")


# What a book's own subject headings say (publishers' headings: "Fiction /
# Fantasy", "History / Ancient / Rome", "Cooking / Methods / Baking"). First match
# wins. A category alone means "somewhere in here" — a model, or a person, says where.
_SUBJECT_RULES: Tuple[Tuple[str, str], ...] = (
    (r"\bjuvenile\b|\bchildren'?s (books|stories|fiction)\b|\bpicture books\b|\bfairy tales\b", "Literature/Children's & young readers"),
    (r"\bfiction\b|\bnovels?\b|\bpoetry\b|\bdrama\b|\bshort stories\b|\bfantasy\b|\bthrillers?\b|\bmystery\b|\bromance\b", "Literature/Collections & undated"),
    (r"\bbiograph|\bautobiograph|\bmemoirs?\b", "History/Lives & memoirs"),
    (r"\bhistory\b|\bhistorical\b", "History"),
    (r"\bbible\b|\bchristian|\btheolog", "Philosophy & religion/The Bible & Christianity"),
    (r"\bphilosoph|\bethics\b|\bstoic", "Philosophy & religion/Philosophy & ethics"),
    (r"\bpsycholog|\bself-help\b", "Philosophy & religion/Psychology & the mind"),
    (r"\breligio|\bspiritual", "Philosophy & religion"),
    (r"\bcomputers?\b|\bprogramming\b|\bsoftware\b", "Computing"),
    (r"\bgardening\b|\bhorticultur", "Homestead/Gardening"),
    (r"\bpets\b|\bdogs\b|\bcats\b", "Homestead/Animals & pets"),
    (r"\bmathematics\b|\bstatistics\b", "Science/Mathematics & statistics"),
    (r"\bscience\b|\bnature\b|\bastronomy\b|\bphysics\b|\bbiology\b|\bchemistry\b", "Science"),
    (r"\bmedical\b|\bmedicine\b|\bnursing\b", "Medicine"),
    (r"\btechnology\b|\bengineering\b|\bdo-it-yourself\b|\bhome improvement\b", "Repair & making"),
    (r"\blaw\b|\blegal\b", "Society/Law"),
    (r"\bpolitical science\b|\bpolitics\b|\bgovernment\b", "Society/Government & politics"),
    (r"\bbusiness\b|\beconomics\b|\bfinance\b|\binvesting\b", "Society/Money, business & economics"),
    (r"\beducation\b|\bparenting\b", "Society/Education & parenting"),
    (r"\bmusic\b", "Arts & leisure/Music"),
    (r"\bart\b|\bphotography\b|\barchitecture\b|\bdesign\b", "Arts & leisure/Art & architecture"),
    (r"\bsports?\b|\bgames\b|\bhobbies\b|\bcrafts\b", "Arts & leisure/Sports, games & hobbies"),
    (r"\btravel\b", "Reference/Maps & travel"),
    (r"\breference\b|\bdictionar|\bencyclop", "Reference"),
)
_SUBJECT_PATTERNS = tuple((re.compile(pattern, re.I), shelf) for pattern, shelf in _SUBJECT_RULES)


def place_by_subject(subjects: str, title: str = "") -> str:
    """A shelf — or only a category — from a book's own subject headings; "" when it has none that tell."""
    subjects = str(subjects or "")
    if not subjects.strip():
        return ""
    for pattern, shelf in _SUBJECT_PATTERNS:
        if pattern.search(subjects):
            if shelf == "History":                      # which era, if its words say
                for era, era_pattern in _ERA_WORD_PATTERNS:
                    if era_pattern.search(f"{subjects}\n{title}"):
                        return era
                return "History/General histories"
            return shelf
    return ""


# ── documents read by a person or a model ──────────────────────────────────

def place_document(title: str, text: str = "", folder: str = "", subjects: str = "") -> Tuple[str, str]:
    """``(shelf, why)`` for a document from what is known without asking a
    model: the folder it was put in, then the words of its title and
    subjects. A folder that names only a category still narrows it — a file
    in a cookbooks folder goes on a cooking shelf, chosen by its title."""
    hint = folder_hint(folder) if folder else ""
    words = place_by_words(title, subjects)
    if hint and SEP in hint:
        return hint, "folder"
    if not hint and not words:
        by_subject = place_by_subject(subjects, title)
        if by_subject:
            return by_subject, "subject"
    if hint == "Cooking":
        return cooking_style(f"{title}\n{subjects}"), "folder"
    if hint and words and split(words)[0] == hint:
        return words, "folder and title"
    if words:
        return words, "title"
    if hint:
        return hint, "folder"          # the category is known, the shelf inside it is not (yet)
    return UNSORTED, ""
