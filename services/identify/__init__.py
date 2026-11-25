"""Photo identification: "what is this, and is it safe?"

Pipeline (``identify_image``):

1. **Species classifier** (BioCLIP, optional engine) scores the photo against
   the Tree of Life: candidate species / genera / families with scores.
2. **Vision model** looks at the photo with those candidates as hints and
   returns structured observations: category, visible features, best guess,
   alternatives — or the text, if the photo is of text (a label, a page).
3. **Offline library** supplies reference notes for the leading candidates:
   field marks, lookalikes, toxicity, habitat.
4. **Vision model** writes the answer from the photo plus those notes.

Every stage degrades: no classifier → the vision model works unaided; no
library → no reference notes (the answer says so); only the vision model is
required. Nothing here needs the internet.

Nobody has to say what kind of photo it is. The pipeline sorts it: a living
thing or an animal's sign is identified against the library; print is read
(and put into the household's language when it is in another, and offered to
the Kitchen when it is a recipe); anything else is described. ``want`` is the
correction for when the sorting was wrong: ``"text"`` (it is a label — read
it), ``"describe"`` (just describe it), ``"living"`` (identify it anyway).

The answer comes as markdown and as a ``card``: the few things a screen shows
large — the likely name, how sure that is in words, the closest dangerous
lookalike with the check that separates them, and the library page.

``for_chat`` is the same rule for a photo that arrives with a question in a
chat: when the species classifier says it shows a living thing, the chat is
given this identification rather than a general description of the picture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from . import classifier, reference, vision

logger = logging.getLogger(__name__)

LIVING = {
    "plant", "mushroom", "insect", "spider", "other_invertebrate", "bird", "mammal",
    "reptile", "amphibian", "fish", "animal_sign",
}
CATEGORIES = sorted(LIVING | {"text", "object", "scene", "unclear"})
WANTS = ("", "living", "text", "describe")

# The species classifier's best score (species, genus or family) at which a
# photo is taken to show a living thing. Measured on reference photos: living
# subjects scored 0.26 and up, objects, print, screens and people 0.11 and
# down (services/identify calibration, docs/photo-id.md).
LIVING_SCORE = 0.2

_lock = asyncio.Lock()


class IdentifyError(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


_OBSERVER = (
    "You are a field naturalist and a careful observer. You report what is visible in a photo, "
    "name things as precisely as the evidence allows, and say so when it does not allow more."
)

_ANALYZE = """Look at the attached photo and answer with ONE JSON object and nothing else.

{{
  "category": one of {categories},
  "subject": "<the main subject in a few plain words>",
  "features": ["<visible feature that matters for identification>", "... up to 8"],
  "best_guess": {{"common_name": "", "scientific_name": "", "rank": "species|genus|family|other", "confidence": "high|medium|low"}},
  "alternatives": [{{"common_name": "", "scientific_name": ""}}],
  "text_in_image": "<legible text, verbatim, if the photo is mainly text; otherwise empty>",
  "need_more": ["<extra photo or detail that would settle it>", "... up to 4"]
}}

Rules:
- Describe only what you can see. Use "text" when the photo is mainly a document, label, sign or screen; "object" for a tool, part, product, rock or other non-living thing; "animal_sign" for tracks, scat, nests, bones.
- For a living thing give the scientific name. If the photo cannot support an identification below genus or family, set rank and confidence accordingly instead of guessing a species.
- "alternatives": up to 4 other identifications the photo is consistent with, most likely first — especially anything it could dangerously be confused with.
{candidates}{hint}"""

_CANDIDATES = """
A species classifier trained on the whole tree of life scored this photo. Its top candidates, with scores from 0 to 1, are below. Treat them as hints: keep what the visible features support, drop what they contradict, and name something else if it fits better. A top species score under 0.25 means the classifier is unsure of the species; the genus and family lines are then the more reliable part.
{lines}
"""

_WRITE_LIVING = """You are identifying the subject of the attached photo for someone standing in front of it. Write the answer they need, in markdown, tight and concrete.

What was observed in the photo:
{observed}
{classifier}
Reference notes from the offline library, one block per candidate:
{notes}

Write these parts in this order, each starting with its bold label. At most three short bullets per part; the whole answer under 260 words.
**Most likely:** common name (*Scientific name*) — confidence high / medium / low. One line. If the photo cannot narrow it to a species, name the genus or family and say what would narrow it.
**Why:** the visible features that point to it.
**Check to confirm:** features to verify in hand, taken from the reference notes.
**Lookalikes:** each plausible alternative with the one feature that separates it. Mark the dangerous ones.
**Edible or hazardous?** What the reference notes say about edibility, toxicity, venom or handling, stated plainly, including how serious it is. If it is something a person might eat, name the dangerous lookalike(s) and the check that separates them — a single photo does not settle that.
**Useful to know:** uses, season, habitat, or first aid for a bite, sting or poisoning — whatever applies. Leave the part out if there is nothing.
**For a firmer ID:** extra photos or simple field checks only (underside, stem or leaf base, cross-section, spore print colour, bruising, smell, something for scale, where it grew and on what). No laboratory tests.

Cite reference notes as [1], [2]. Every fact must be either visible in the photo or stated in the notes — add nothing from memory. Where the notes say nothing about edibility or toxicity, say they do not cover it rather than guessing.{hint}"""

_WRITE_OTHER = """Describe and identify the subject of the attached photo for someone who wants to know what it is and what to do with it. Markdown, tight and concrete.

What was observed in the photo:
{observed}
{notes_block}
Write: **What it is:** one line with your confidence. **Details:** bullets — the visible features, markings, model numbers or condition that matter. **Useful to know:** what it is for, how it is used or handled, anything to watch out for. Leave a part out if there is nothing to say. Use only what is visible or stated in the notes. If the photo shows words — a sign, a label, a notice, a screen — end with a part **Text:** giving those words exactly as written, in their own language, one line for each line of print, and nothing else in that part.{hint}"""

_TRANSCRIBE = """Transcribe all legible text in the attached photo exactly as written, keeping line breaks and reading order. Mark anything you cannot read as [illegible]. After the transcription add one line starting with "Summary:" that says what this document or label is.{hint}"""

_DESCRIBE = """Describe what the attached photo shows for someone who wants to know what they are looking at. Markdown, tight and concrete: **What it shows:** one or two sentences. **Details:** bullets — what is visible that matters (objects, markings, numbers, condition, setting). Leave out anything you cannot see. Do not name a species. If the photo shows words — a sign, a label, a notice, a screen — end with a part **Text:** giving those words exactly as written, in their own language, and nothing else in that part.{hint}"""


def likely_living(classified: Optional[dict]) -> bool:
    """Whether the species classifier's scores say the photo shows a living thing (see ``LIVING_SCORE``)."""
    if not classified:
        return False
    best = 0.0
    for key in ("species", "genus", "family"):
        rows = classified.get(key) or []
        if rows:
            try:
                best = max(best, float(rows[0].get("score") or 0))
            except (TypeError, ValueError):
                pass
    return best >= LIVING_SCORE


# Classifier scores above which its answer is taken as the starting point
# without a separate "look" pass — the write-up still checks it against the
# photo. Measured on reference photos: a species score this high was never a
# different genus.
FAST_SPECIES_SCORE = 0.5
FAST_GENUS_SCORE = 0.85

_CLASS_CATEGORY = {
    "insecta": "insect", "arachnida": "spider", "aves": "bird", "mammalia": "mammal",
    "reptilia": "reptile", "squamata": "reptile", "testudines": "reptile", "crocodylia": "reptile",
    "amphibia": "amphibian", "actinopterygii": "fish", "chondrichthyes": "fish", "teleostei": "fish",
}


def analysis_from_classifier(classified: Optional[dict]) -> Optional[dict]:
    """A ready-made analysis when the classifier is confident, else None."""
    species = (classified or {}).get("species") or []
    genus = ((classified or {}).get("genus") or [{}])[0]
    if not species:
        return None
    top = species[0]
    species_score = float(top.get("score") or 0)
    genus_score = float(genus.get("score") or 0)
    if species_score < FAST_SPECIES_SCORE and genus_score < FAST_GENUS_SCORE:
        return None
    kingdom = str(top.get("kingdom") or "").lower()
    if kingdom == "plantae":
        category = "plant"
    elif kingdom == "fungi":
        category = "mushroom"
    elif kingdom == "animalia":
        category = _CLASS_CATEGORY.get(str(top.get("class") or "").lower(), "other_invertebrate")
    else:
        return None
    at_species = species_score >= FAST_SPECIES_SCORE
    name = top.get("species") if at_species else (genus.get("genus") or top.get("genus") or "")
    best = {
        "common_name": (top.get("common_name") or "") if at_species else "",
        "scientific_name": name or "",
        "rank": "species" if at_species else "genus",
        "confidence": "high" if species_score >= 0.75 else "medium",
    }
    alternatives = [
        {"common_name": row.get("common_name") or "", "scientific_name": row.get("species") or ""}
        for row in species[(1 if at_species else 0):4] if row.get("species")
    ][:3]
    return {
        "category": category,
        "subject": best["common_name"] or best["scientific_name"],
        "features": [],
        "best_guess": best,
        "alternatives": alternatives,
        "text_in_image": "",
        "need_more": [],
        "parsed": True,
        "from_classifier": True,
    }


def _hint_line(hint: str) -> str:
    hint = (hint or "").strip()
    return f'\n\nThe person who took the photo adds: "{hint}"' if hint else ""


def parse_analysis(text: str) -> dict:
    """Pull the JSON object out of the vision model's reply, tolerating code
    fences, stray prose and reasoning blocks. Always returns a usable dict."""
    cleaned = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text or "")
    data: dict = {}
    start = cleaned.find("{")
    while start >= 0 and not data:
        depth = 0
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:index + 1])
                        if isinstance(parsed, dict):
                            data = parsed
                    except json.JSONDecodeError:
                        pass
                    break
        start = cleaned.find("{", start + 1)

    def text_of(value) -> str:
        return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""

    def name_of(value) -> dict:
        value = value if isinstance(value, dict) else {}
        return {
            "common_name": text_of(value.get("common_name")),
            "scientific_name": text_of(value.get("scientific_name")),
        }

    category = text_of(data.get("category")).lower().replace(" ", "_")
    if category not in CATEGORIES:
        category = "unclear"
    best_raw = data.get("best_guess") if isinstance(data.get("best_guess"), dict) else {}
    best = name_of(best_raw)
    best["rank"] = text_of(best_raw.get("rank")).lower() or "other"
    confidence = text_of(best_raw.get("confidence")).lower()
    best["confidence"] = confidence if confidence in ("high", "medium", "low") else "low"
    alternatives = [name_of(item) for item in (data.get("alternatives") or []) if isinstance(item, dict)]
    alternatives = [a for a in alternatives if a["common_name"] or a["scientific_name"]][:4]

    def strings(value, limit) -> List[str]:
        return [text_of(item) for item in (value or []) if text_of(item)][:limit] if isinstance(value, list) else []

    return {
        "category": category,
        "subject": text_of(data.get("subject")) or text_of(text)[:160],
        "features": strings(data.get("features"), 8),
        "best_guess": best,
        "alternatives": alternatives,
        "text_in_image": data.get("text_in_image").strip() if isinstance(data.get("text_in_image"), str) else "",
        "need_more": strings(data.get("need_more"), 4),
        "parsed": bool(data),
    }


def lookup_names(analysis: dict, classified: Optional[dict], limit: int = 4) -> List[str]:
    """Names worth looking up, best first: the vision model's guess, the
    classifier's top species when it is not just noise, then alternatives."""
    names: List[str] = []

    def add(name: str) -> None:
        name = re.sub(r"\s+", " ", name or "").strip(" .")
        name = re.sub(r"\s+(spp?|species|cf|aff)\.?$", "", name, flags=re.I)   # "Amanita sp." -> "Amanita"
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)

    best = analysis.get("best_guess") or {}
    add(best.get("scientific_name") or best.get("common_name") or "")
    species = (classified or {}).get("species") or []
    for index, row in enumerate(species[:3]):
        if float(row.get("score") or 0) >= (0.05 if index == 0 else 0.10):
            add(row.get("species") or "")
    for alternative in analysis.get("alternatives") or []:
        add(alternative.get("scientific_name") or alternative.get("common_name") or "")
    genus = ((classified or {}).get("genus") or [{}])[0]
    if len(names) < limit and float(genus.get("score") or 0) >= 0.5:
        add(genus.get("genus") or "")
    return names[:limit]


def _observed(analysis: dict) -> str:
    best = analysis["best_guess"]
    if analysis.get("from_classifier"):
        return ("- No separate description was made: the species classifier was confident, so start "
                "from its candidates below and check them against what you see in the photo yourself.")
    lines = [f"- Subject: {analysis['subject']} (category: {analysis['category']})"]
    if analysis["features"]:
        lines.append("- Visible features: " + "; ".join(analysis["features"]))
    guess = " / ".join(part for part in (best["common_name"], best["scientific_name"]) if part)
    if guess:
        lines.append(f"- First impression: {guess} (rank: {best['rank']}, confidence: {best['confidence']})")
    alternatives = [" / ".join(p for p in (a["common_name"], a["scientific_name"]) if p) for a in analysis["alternatives"]]
    if alternatives:
        lines.append("- Could also be: " + "; ".join(alternatives))
    return "\n".join(lines)


def asset_url(image_path: str) -> str:
    """Same-origin URL for a library image (see /api/reference/asset)."""
    return "/api/reference/asset?p=" + quote(image_path, safe="") if image_path else ""


def _plain_analysis(category: str) -> dict:
    return {"category": category, "subject": "", "features": [], "best_guess": {"common_name": "", "scientific_name": "", "rank": "other", "confidence": "low"},
            "alternatives": [], "text_in_image": "", "need_more": [], "parsed": True}


def _identify_sync(image_path: str, hint: str, owner: Optional[str], want: str = "") -> dict:
    timings: Dict[str, float] = {}
    started = time.time()
    want = want if want in WANTS else ""

    try:
        photo = vision.prepare_image(image_path)
    except Exception as exc:
        raise IdentifyError(f"Could not read that image: {exc}", 400) from exc

    vision.warm_up(owner)       # let the model load while the classifier runs

    # 1. Species classifier (optional) — not asked when the person said it is print, or just wants it described.
    mark = time.time()
    classified = classifier.classify(photo) if want in ("", "living") else None
    timings["classifier_s"] = round(time.time() - mark, 2)
    candidate_lines = classifier.summarize(classified)

    # 2. Structured look at the photo — skipped when the classifier is
    #    confident: its candidates go straight to the write-up, which still
    #    has to square them with what the photo shows. Skipped too when the
    #    person already said what kind of photo it is.
    mark = time.time()
    first: dict = {}
    analysis = _plain_analysis("text" if want == "text" else "scene") if want in ("text", "describe") else analysis_from_classifier(classified)
    if analysis is None:
        prompt = _ANALYZE.format(
            categories=", ".join(f'"{name}"' for name in CATEGORIES),
            candidates=_CANDIDATES.format(lines=candidate_lines) if candidate_lines else "",
            hint=_hint_line(hint),
        )
        try:
            first = vision.vision_call(photo, prompt, system=_OBSERVER, owner=owner, max_tokens=900,
                                       json_mode=True)
        except vision.VisionUnavailable as exc:
            raise IdentifyError(str(exc), 503) from exc
        analysis = parse_analysis(first["text"])
        timings["look_s"] = round(time.time() - mark, 2)
    category = analysis["category"]
    if want == "living" and category not in LIVING:
        category = "unclear"                             # told it is a living thing: identify it as one

    sources: List[dict] = []
    images: List[dict] = []
    references: List[dict] = []

    # 3 + 4. Notes, then the write-up.
    mark = time.time()
    living = False
    if category == "text":
        try:
            second = vision.vision_call(photo, _TRANSCRIBE.format(hint=_hint_line(hint)), owner=owner,
                                        max_tokens=2000, temperature=0.0)
        except vision.VisionUnavailable as exc:
            raise IdentifyError(str(exc), 503) from exc
        body = second["text"].strip()
        if not body.partition("Summary:")[0].strip():
            # A small model sometimes gives only the closing line. Ask once more, for the words alone.
            try:
                again = vision.vision_call(photo, "Read out every word of text in this image, exactly as written, line by line. Give only the text.",
                                           owner=owner, max_tokens=2000, temperature=0.0)
                words = again["text"].strip()
                if words:
                    body = f"{words}\n{body}" if body else words
            except vision.VisionUnavailable:
                pass
    else:
        living = want != "describe" and (want == "living" or category in LIVING or bool(
            classified and category == "unclear" and float(classified["species"][0].get("score") or 0) >= 0.25))
        notes_text = ""
        if living:
            for name in lookup_names(analysis, classified):
                try:
                    found = reference.lookup(name)
                except Exception as exc:
                    logger.debug("reference lookup failed for %s: %s", name, exc)
                    found = None
                if found and found["url"] not in (r["url"] for r in references):
                    references.append(found)
            blocks = []
            for index, ref in enumerate(references, start=1):
                label = ref["title"]
                if ref["is_genus_fallback"]:
                    label += f" (genus article — no article for {ref['query']})"
                elif ref["title"].lower() != ref["query"].lower():
                    label += f" (looked up as {ref['query']})"
                blocks.append(f"[{index}] {label}\n{ref['notes']}")
                sources.append({"url": ref["url"], "title": ref["title"]})
                if ref["image_path"]:
                    images.append({"title": ref["title"], "url": asset_url(ref["image_path"])})
            notes_text = "\n\n".join(blocks)
        timings["library_s"] = round(time.time() - mark, 2)
        mark = time.time()
        if living:
            prompt = _WRITE_LIVING.format(
                observed=_observed(analysis),
                classifier=(f"\nSpecies classifier candidates (score 0–1):\n{candidate_lines}\n" if candidate_lines else ""),
                notes=notes_text or "(none — the offline library has no article for these candidates; say so where it matters)",
                hint=_hint_line(hint),
            )
        elif want == "describe":
            prompt = _DESCRIBE.format(hint=_hint_line(hint))
        else:
            prompt = _WRITE_OTHER.format(observed=_observed(analysis), notes_block="", hint=_hint_line(hint))
        try:
            second = vision.vision_call(photo, prompt, system=_OBSERVER, owner=owner, max_tokens=800)
        except vision.VisionUnavailable as exc:
            raise IdentifyError(str(exc), 503) from exc
        body = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", second["text"]).strip()
    timings["write_s"] = round(time.time() - mark, 2)
    timings["total_s"] = round(time.time() - started, 2)

    parts = [body]
    if images:
        parts.append("**Reference photos from the library** (compare with yours):\n\n" + "\n\n".join(
            f"![{image['title']} — reference photo]({image['url']})\n*{image['title']}*" for image in images[:3]
        ))
    if classified and living:
        top = classified["species"][:3]
        scores = " · ".join(f"{row.get('species')} {float(row.get('score') or 0):.2f}" for row in top)
        genus = (classified.get("genus") or [{}])[0]
        if genus.get("genus"):
            scores += f" · genus {genus['genus']} {float(genus.get('score') or 0):.2f}"
        parts.append(f"*Species classifier scores (0–1): {scores}*")

    return {
        "markdown": "\n\n".join(part for part in parts if part),
        "body": body,
        "living": bool(living),
        "want": want,
        "category": category,
        "subject": analysis["subject"],
        "best_guess": analysis["best_guess"],
        "alternatives": analysis["alternatives"],
        "features": analysis["features"],
        "need_more": analysis["need_more"],
        "classifier": classified,
        "sources": sources,
        "images": images,
        "vision_model": second.get("model") or first.get("model") or "",
        "timings": timings,
    }


# ── the answer as a card ────────────────────────────────────────────────────

# The labels the write-up is asked to use. Only these start a part: other bold words inside a part
# ("the **death cap** is…") are just emphasis.
_LABELS = ("most likely", "why", "check to confirm", "lookalikes", "edible or hazardous", "useful to know", "for a firmer id",
           "what it is", "what it shows", "details", "text")
_PART = re.compile(r"\*\*\s*(" + "|".join(re.escape(label) for label in _LABELS) + r")\s*\??\s*:?\s*\*\*\s*:?[ \t]*", re.I)
_SURE = {"high": "Quite sure", "medium": "Fairly sure", "low": "Not sure: treat this as a guess"}
_DANGER = re.compile(r"danger|deadly|toxic|poison|venom|fatal|lethal|harm|serious|do not eat|not edible|inedible", re.I)


def parts_of(markdown: str) -> Dict[str, str]:
    """The write-up cut at its bold labels: ``{"most likely": "…", "lookalikes": "…", …}``."""
    found: Dict[str, str] = {}
    pieces = _PART.split(markdown or "")
    for index in range(1, len(pieces) - 1, 2):
        label = re.sub(r"[^a-z ]", "", pieces[index].lower()).strip()
        text = pieces[index + 1].strip()
        if label and text and label not in found:
            found[label] = text
    return found


def _plain(text: str, limit: int = 0) -> str:
    """Markdown as the words it says, on one line."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s*\[\d+\]", "", text)                 # reference marks
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = (text[:cut + 1] if cut > limit * 0.5 else text[:limit].rstrip(" ,;") + "…")
    return text


def _bullets(text: str) -> List[str]:
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    marked = [re.sub(r"^[-*•]\s*", "", line) for line in lines if re.match(r"^[-*•]\s+", line)]
    return marked or lines


_AMOUNT = re.compile(r"^\s*(?:[-•*]\s*)?(?:\d+(?:[.,/]\d+)?|[¼½¾⅓⅔⅛]|a|an|one|two|three|half)\b[^\n]{0,40}?\b(?:cups?|c\.|tsp|tbsp|teaspoons?|tablespoons?|"
                     r"oz|ounces?|lbs?|pounds?|g|grams?|kg|ml|l|litres?|liters?|pints?|quarts?|gills?|sticks?|cloves?|pinch|dash|eggs?|cans?|sprigs?)\b", re.I)
_COOKING = re.compile(r"\b(?:bake|stir|mix|simmer|preheat|whisk|boil|fry|saut[eé]|knead|chop|dice|pour|beat|fold|roast|grease|sift|cream|"
                      r"cook|oven|skillet|saucepan|serves|ingredients|until\s+(?:golden|tender|done|smooth|thick))\b", re.I)


def looks_like_recipe(text: str) -> bool:
    """Print that reads like a recipe: amounts of ingredients, and the verbs of cooking."""
    lines = [line for line in (text or "").split("\n") if line.strip()]
    amounts = sum(1 for line in lines if _AMOUNT.match(line))
    verbs = len(set(match.lower() for match in _COOKING.findall(text or "")))
    return (amounts >= 3 and verbs >= 2) or (amounts >= 2 and verbs >= 4)


def words_in(result: dict) -> str:
    """The words read off a photo: the transcription of print, or the **Text:** part of a description."""
    body = str(result.get("body") or "")
    if result.get("category") == "text":
        return body.partition("\nSummary:")[0].strip()
    if result.get("living"):
        return ""
    found = parts_of(body).get("text") or ""
    lines = [re.sub(r"^[-*•]\s*", "", line).strip().strip('"“”') for line in found.split("\n")]
    words = "\n".join(line for line in lines if line)
    return "" if re.fullmatch(r"(?i)(?:none|n/?a|no (?:legible |visible )?(?:text|words)[^\n]*)\.?", words.strip()) else words


def card_for(result: dict) -> dict:
    """The answer as the few things a screen shows large (see the module docstring)."""
    body = str(result.get("body") or result.get("markdown") or "")
    category = str(result.get("category") or "")
    if category == "text":
        text, _, summary = body.partition("\nSummary:")
        if not summary and body.lower().startswith("summary:"):
            text, summary = "", body[8:]
        card = {"type": "reading", "text": text.strip()[:6000], "summary": _plain(summary, 240), "recipe": looks_like_recipe(text)}
        if result.get("translation"):
            card["translation"] = result["translation"]
        return card
    if not result.get("living"):
        words = words_in(result)
        shown = _PART.split(body)                         # the description without its Text part: the card shows that separately
        if words:
            at = next((i for i in range(1, len(shown) - 1, 2) if shown[i].lower() == "text"), None)
            if at is not None:
                body = body[:body.lower().rfind("**text")].rstrip()
        card = {"type": "description", "text": body[:6000], "title": _plain(result.get("subject") or "", 80)}
        if words:
            card["words"] = words[:3000]
            card["recipe"] = looks_like_recipe(words)
        if result.get("translation"):
            card["translation"] = result["translation"]
        return card
    parts = parts_of(body)
    best = result.get("best_guess") or {}
    headline = _plain(parts.get("most likely") or "", 200)
    level = str(best.get("confidence") or "low")
    said = re.search(r"confidence[:\s]+(high|medium|low)|\b(high|medium|low)\s+confidence", headline, re.I)
    if said:
        level = (said.group(1) or said.group(2)).lower()
    name = best.get("common_name") or ""
    latin = best.get("scientific_name") or ""
    if not name and headline:
        name = re.split(r"\s+[—–-]\s+|\s*\(|\.\s", headline, maxsplit=1)[0].strip()
    lookalikes = _bullets(parts.get("lookalikes") or "")
    dangerous = next((item for item in lookalikes if _DANGER.search(item)), lookalikes[0] if lookalikes else "")
    sources = result.get("sources") or []
    images = result.get("images") or []
    more = [{"label": label, "text": parts[key]} for key, label in (
        ("why", "Why"), ("check to confirm", "Check to confirm"), ("lookalikes", "Lookalikes"), ("edible or hazardous", "Edible or hazardous?"),
        ("useful to know", "Useful to know"), ("for a firmer id", "For a firmer ID")) if parts.get(key)]
    return {
        "type": "identification", "kind": category,
        "name": (name or latin or _plain(result.get("subject") or "", 80) or "Not sure what this is")[:120],
        "latin": latin[:120] if latin and latin.lower() != (name or "").lower() else "",
        "sure": {"level": level if level in _SURE else "low", "words": _SURE.get(level, _SURE["low"])},
        "headline": headline,
        "lookalike": _plain(dangerous, 260),
        "hazard": _plain(parts.get("edible or hazardous") or "", 320),
        "library": {"title": str(sources[0].get("title") or ""), "url": str(sources[0].get("url") or "")} if sources else None,
        "photo": {"title": str(images[0].get("title") or ""), "url": str(images[0].get("url") or "")} if images else None,
        "more": more,
    }


async def _into_the_household_language(result: dict, owner: Optional[str]) -> None:
    """Print in another language than the household reads is translated into it: the answer then carries
    both. Quietly does nothing when the language cannot be told or the translator is not available."""
    text = words_in(result)
    if len(text) < 8:
        return
    try:
        from services import household, translate
        from services.translate import detect, languages
        code, sure = detect.detect(text)
        mine = household.language()
        if not code or sure < 0.5 or code.split("-")[0] == mine.split("-")[0]:
            return
        done = await asyncio.wait_for(translate.translate_text(text[:4000], mine, code, owner=owner), timeout=180)
        if done.get("same_language") or not str(done.get("text") or "").strip():
            return
        result["translation"] = {"from": languages.plain_name(code) or code, "from_code": code,
                                 "to": languages.plain_name(mine) or mine, "to_code": mine, "text": str(done["text"]).strip()}
        result["markdown"] += f"\n\n**In {result['translation']['to']}** (it is written in {result['translation']['from']}):\n\n{result['translation']['text']}"
    except Exception as exc:
        logger.info("the text in the photo was not translated (%s: %s)", type(exc).__name__, str(exc)[:120])


async def identify_image(image_path: str, hint: str = "", owner: Optional[str] = None, want: str = "") -> dict:
    """Identify, read or describe the subject of a photo — whichever it calls for. ``want`` overrides the
    sorting (see the module docstring). One at a time: each run occupies the vision model for half a
    minute, and queued runs would only thrash it."""
    async with _lock:
        result = await asyncio.to_thread(_identify_sync, image_path, hint, owner, want)
        await _into_the_household_language(result, owner)
    result["card"] = card_for(result)
    return result


def for_chat(image_path: str, question: str = "", owner: Optional[str] = None) -> str:
    """What a chat is told about a photo that came with a message, when the photo shows a living thing:
    the identification, in place of a general description. "" for any other photo, or when the species
    classifier is not installed — the chat then describes the picture as it always did. Blocking."""
    try:
        photo = vision.prepare_image(image_path)
    except Exception:
        return ""
    if not likely_living(classifier.classify(photo)):
        return ""
    try:
        found = _identify_sync(image_path, question, owner)
    except IdentifyError as problem:
        logger.info("photo in a chat not identified (%s)", problem)
        return ""
    if not found.get("living"):
        return ""
    return ("[This photo was put through the species identifier and checked against the home library. Use this "
            "identification and its cautions as they are; do not name the species yourself.]\n" + str(found.get("markdown") or ""))
