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
Write: **What it is:** one line with your confidence. **Details:** bullets — the visible features, markings, model numbers or condition that matter. **Useful to know:** what it is for, how it is used or handled, anything to watch out for. Leave a part out if there is nothing to say. Use only what is visible or stated in the notes.{hint}"""

_TRANSCRIBE = """Transcribe all legible text in the attached photo exactly as written, keeping line breaks and reading order. Mark anything you cannot read as [illegible]. After the transcription add one line starting with "Summary:" that says what this document or label is.{hint}"""


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


def _identify_sync(image_path: str, hint: str, owner: Optional[str]) -> dict:
    timings: Dict[str, float] = {}
    started = time.time()

    try:
        photo = vision.prepare_image(image_path)
    except Exception as exc:
        raise IdentifyError(f"Could not read that image: {exc}", 400) from exc

    vision.warm_up(owner)       # let the model load while the classifier runs

    # 1. Species classifier (optional).
    mark = time.time()
    classified = classifier.classify(photo)
    timings["classifier_s"] = round(time.time() - mark, 2)
    candidate_lines = classifier.summarize(classified)

    # 2. Structured look at the photo — skipped when the classifier is
    #    confident: its candidates go straight to the write-up, which still
    #    has to square them with what the photo shows.
    mark = time.time()
    first: dict = {}
    analysis = analysis_from_classifier(classified)
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
    else:
        living = category in LIVING or bool(classified and category == "unclear"
                                           and float(classified["species"][0].get("score") or 0) >= 0.25)
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


async def identify_image(image_path: str, hint: str = "", owner: Optional[str] = None) -> dict:
    """Identify the subject of a photo. One at a time: each run occupies the
    vision model for half a minute, and queued runs would only thrash it."""
    async with _lock:
        return await asyncio.to_thread(_identify_sync, image_path, hint, owner)
