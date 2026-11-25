"""A medicine's official US label, read a section at a time.

The label is what the maker filed with the Food and Drug Administration: what the medicine is for, the dose,
the side effects and how often each was seen, the warnings, the interactions. It is asked of openFDA (no key
needed); a name that is misspelt is put right by the National Library of Medicine's RxNorm first. The answer
carries the label's date and a link to it on DailyMed.

Labels are long — one section can run to several pages — so the answer holds the section asked about, a page
at a time, and says what else the label has.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services import connections
from services.connections import http
from services.connections.http import Problem

logger = logging.getLogger(__name__)

SERVICE = "medicines"
FDA = "https://api.fda.gov/drug/label.json"
RXNORM = "https://rxnav.nlm.nih.gov/REST/approximateTerm.json"
PAGE = 5200          # characters of one section in one answer
ROOM = 8600          # characters of label text in one answer

# What a person asks about → the label's sections that answer it, in the order to give them.
# Prescription labels and over-the-counter "Drug Facts" labels name their sections differently; both are listed.
TOPICS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("uses", ("use", "uses", "used for", "for", "indication", "indications", "treat", "treats", "what is it"),
     ("indications_and_usage", "purpose")),
    ("dose", ("dose", "doses", "dosage", "dosing", "how much", "how to take", "how often", "schedule", "titration", "missed dose", "administration"),
     ("dosage_and_administration", "dosage_forms_and_strengths")),
    ("side effects", ("side effect", "side effects", "adverse", "reaction", "reactions", "nausea", "common", "symptoms"),
     ("adverse_reactions", "stop_use")),
    ("warnings", ("warning", "warnings", "caution", "cautions", "risk", "risks", "danger", "dangerous", "safe", "safety",
                  "contraindication", "contraindications", "who should not", "boxed", "black box"),
     ("boxed_warning", "contraindications", "warnings_and_cautions", "warnings", "do_not_use", "ask_doctor", "when_using")),
    ("interactions", ("interaction", "interactions", "interact", "with other", "together with", "alcohol", "mix", "combine"),
     ("drug_interactions", "ask_doctor_or_pharmacist")),
    ("pregnancy", ("pregnancy", "pregnant", "breastfeeding", "breast feeding", "nursing", "lactation", "fertility", "children",
                   "pediatric", "elderly", "geriatric", "kidney", "liver", "renal", "hepatic"),
     ("pregnancy", "lactation", "nursing_mothers", "pregnancy_or_breast_feeding", "females_and_males_of_reproductive_potential",
      "pediatric_use", "geriatric_use", "use_in_specific_populations")),
    ("overdose", ("overdose", "overdosage", "too much", "took too many", "poison"), ("overdosage",)),
    ("storage", ("storage", "store", "keep", "refrigerate", "fridge", "expire", "expiry", "shelf life", "supplied", "sizes", "strengths"),
     ("storage_and_handling", "how_supplied", "dosage_forms_and_strengths")),
    ("how it works", ("how it works", "how does it work", "mechanism", "pharmacology", "half life", "half-life", "works"),
     ("mechanism_of_action", "pharmacodynamics", "pharmacokinetics", "clinical_pharmacology")),
    ("ingredients", ("ingredient", "ingredients", "contains", "what is in", "made of", "inactive"),
     ("active_ingredient", "inactive_ingredient", "description")),
    ("for patients", ("patient", "patients", "plain", "guide", "medication guide", "instructions", "how to use", "inject", "injection"),
     ("spl_medguide", "information_for_patients", "spl_patient_package_insert", "instructions_for_use")),
    ("studies", ("study", "studies", "trial", "trials", "evidence", "effective", "efficacy", "how well", "weight loss results"),
     ("clinical_studies",)),
)
# With no particular question: the things most often wanted, each kept short.
OVERVIEW: Tuple[Tuple[str, int], ...] = (
    ("boxed_warning", 1400), ("indications_and_usage", 1300), ("purpose", 400), ("dosage_and_administration", 1700),
    ("contraindications", 700), ("do_not_use", 500), ("adverse_reactions", 1500), ("drug_interactions", 900),
)
_TITLES = {"spl_medguide": "Medication guide (written for patients)", "information_for_patients": "What patients should be told",
           "spl_patient_package_insert": "Patient leaflet", "stop_use": "Stop use and ask a doctor if",
           "do_not_use": "Do not use", "ask_doctor": "Ask a doctor before use if", "when_using": "When using this product",
           "ask_doctor_or_pharmacist": "Ask a doctor or pharmacist before use if", "boxed_warning": "Boxed warning (the FDA's strongest)"}
_SKIP = {"openfda", "set_id", "id", "version", "effective_time", "spl_product_data_elements", "package_label_principal_display_panel",
         "recent_major_changes", "spl_unclassified_section"}


def _title(section: str) -> str:
    return _TITLES.get(section) or section.replace("_", " ").capitalize()


def _clean(name: str) -> str:
    return re.sub(r"[\"\\()\[\]{}:^~*?!+/]", " ", " ".join(str(name or "").split()))[:80].strip()


def _labels(name: str) -> List[dict]:
    """The newest labels whose brand, generic or substance name matches, from openFDA."""
    quoted = f'"{name}"'
    params = {"search": f"openfda.brand_name:{quoted} openfda.generic_name:{quoted} openfda.substance_name:{quoted}",
              "sort": "effective_time:desc", "limit": 12}
    key = connections.value(SERVICE)
    if key:
        params["api_key"] = key
    try:
        found = http.get(FDA, params=params, name="The FDA's label service", timeout=20)
    except Problem as problem:
        if problem.status == 404:          # openFDA answers "no matches" with a 404
            return []
        raise
    return [item for item in (found.get("results") or []) if isinstance(item, dict)]


def _names(label: dict, field: str) -> List[str]:
    return [str(item) for item in ((label.get("openfda") or {}).get(field) or []) if item]


def _score(label: dict, name: str) -> int:
    asked = name.lower()
    brands = [item.lower() for item in _names(label, "brand_name")]
    generics = [item.lower() for item in _names(label, "generic_name") + _names(label, "substance_name")]
    score = 0
    if asked in generics:
        # Asked for by its ingredient. A label whose brand name is the ingredient's own name (most
        # generics) is no better a match than one sold under another brand.
        score += 80
        if not any(re.search(r"\band\b|,|/", item) for item in _names(label, "generic_name")):
            score += 20                    # the medicine alone, not a combination that contains it
    elif asked in brands:
        score += 100
    elif any(asked in re.split(r"[^a-z0-9]+", item) for item in brands):
        score += 40
    elif any(asked in item for item in generics):
        score += 30
    kind = " ".join(_names(label, "product_type")).upper()
    if "HUMAN" in kind:
        score += 10
    score += min(len([key for key in label if key not in _SKIP and not key.endswith("_table")]), 30) // 3
    return score


def _respelt(name: str) -> List[str]:
    """Names RxNorm thinks were meant, best first."""
    try:
        found = http.get(RXNORM, params={"term": name, "maxEntries": 4}, name="The medicine-name service", timeout=10)
    except Problem:
        return []
    names: List[str] = []
    for item in (((found.get("approximateGroup") or {}).get("candidate")) or []):
        text = str(item.get("name") or "").strip()
        if text and text.lower() not in (other.lower() for other in names) and text.lower() != name.lower():
            names.append(text)
    return names[:3]


def _kind(label: dict) -> str:
    kind = " ".join(_names(label, "product_type")).upper()
    return "otc" if "OTC" in kind else "prescription" if "PRESCRIPTION" in kind else ""


KINDS = {"otc": "otc", "over the counter": "otc", "over-the-counter": "otc", "nonprescription": "otc", "drug facts": "otc",
         "prescription": "prescription", "rx": "prescription"}


def find(name: str, prefer: str = "") -> Tuple[dict, str, List[str], List[str]]:
    """(the label, the name it was found under, other prescription brands of the same medicine, the kinds of
    label there are for it: "otc", "prescription").

    A medicine sold both ways (ibuprofen, famotidine) has two different labels with different doses. The
    one on the shelf at home is the over-the-counter one, so that is given unless ``prefer`` says otherwise.
    """
    asked = _clean(name)
    if len(asked) < 2:
        raise Problem("Give the name of the medicine.", "refused")
    if not connections.on(SERVICE):
        raise Problem("Medicine labels are switched off (Settings → Integrations).", "off")
    tried = asked
    labels = _labels(asked)
    if not labels:
        for other in _respelt(asked):
            labels = _labels(_clean(other))
            if labels:
                tried = _clean(other)
                break
    if not labels:
        raise Problem(f"No US label was found for “{asked}”. Check the spelling, or give the other name it goes by "
                      "(the brand name or the ingredient).", "nothing")
    close = [label for label in labels if _score(label, tried) >= 80]
    kinds = sorted({_kind(label) for label in close} - {""})
    want = KINDS.get(str(prefer or "").strip().lower(), "")
    if want not in kinds:
        want = "otc" if len(kinds) > 1 else ""
    best = max(labels, key=lambda label: _score(label, tried) + (25 if want and _kind(label) == want else 0))
    others: List[str] = []
    if _kind(best) == "prescription":
        mine = {item.lower() for item in _names(best, "brand_name")}
        same = sorted(item.lower() for item in _names(best, "generic_name"))
        for label in labels:
            if _kind(label) != "prescription" or sorted(item.lower() for item in _names(label, "generic_name")) != same:
                continue
            for brand in _names(label, "brand_name"):
                shown = brand.title() if brand.isupper() else brand
                if brand.lower() not in mine and brand.lower() not in same and shown.lower() not in (item.lower() for item in others):
                    others.append(shown)
    return best, tried, others[:4], kinds


def _text(label: dict, section: str) -> str:
    value = label.get(section)
    parts = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    return "\n\n".join(" ".join(str(part).split()) for part in parts if part).strip()


def _cut(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    kept = text[:room]
    end = max(kept.rfind(". "), kept.rfind("; "))
    return (kept[: end + 1] if end > room * 0.6 else kept.rsplit(" ", 1)[0]) + " …"


def _pages(text: str) -> List[str]:
    """A long section as pages of about PAGE characters, each ending at the end of a sentence."""
    pages: List[str] = []
    start = 0
    while start < len(text):
        end = start + PAGE
        if end >= len(text):
            pages.append(text[start:].strip())
            break
        stop = text.find(". ", end, end + 400)
        if stop != -1:
            end = stop + 1
        else:
            space = text.rfind(" ", start, end)
            end = space if space > start else end
        pages.append(text[start:end].strip())
        start = end
    return [page for page in pages if page] or [""]


def topic_for(about: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """The label sections that answer what was asked about, or None for the overview."""
    asked = " " + re.sub(r"[^a-z0-9 ]+", " ", str(about or "").lower()) + " "
    if not asked.strip() or asked.strip() in ("all", "everything", "overview", "summary", "about"):
        return None
    best = None
    for topic, words, sections in TOPICS:
        hits = sum(1 for word in words if f" {word} " in asked)
        if topic in asked:
            hits += 2
        if hits and (best is None or hits > best[0]):
            best = (hits, topic, sections)
    return (best[1], best[2]) if best else None


def answer(args: Dict[str, Any]) -> Tuple[str, List[dict]]:
    name = str(args.get("name") or args.get("medicine") or args.get("drug") or args.get("query") or args.get("_text") or "")
    about = str(args.get("about") or args.get("section") or args.get("question") or "")
    try:
        page = max(1, int(args.get("page") or args.get("part") or 1))
    except (TypeError, ValueError):
        page = 1
    label, found_as, others, kinds = find(name, str(args.get("kind") or args.get("type") or ""))
    brand = (_names(label, "brand_name") or [found_as])[0]
    brand = brand.title() if brand.isupper() else brand
    generic = ", ".join(item.lower() for item in _names(label, "generic_name")[:3])
    maker = (_names(label, "manufacturer_name") or [""])[0]
    kind = " ".join(_names(label, "product_type")).lower()
    route = ", ".join(item.lower() for item in _names(label, "route")[:3])
    stamp = str(label.get("effective_time") or "")
    dated = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else ""
    set_id = str(label.get("set_id") or (_names(label, "spl_set_id") or [""])[0])
    link = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}" if set_id else "https://dailymed.nlm.nih.gov/"

    head = f"MEDICINE LABEL: {brand}" + (f" ({generic})" if generic and generic != brand.lower() else "")
    facts = " · ".join(part for part in (maker, "prescription" if "prescription" in kind else "over the counter" if "otc" in kind else kind,
                                         route, f"label dated {dated}" if dated else "") if part)
    lines = [head, facts, f"Label: {link}"]
    if found_as.lower() != _clean(name).lower():
        lines.append(f"(“{_clean(name)}” was taken to mean {found_as}.)")
    if others:
        lines.append("The same ingredient is also sold as: " + ", ".join(others) + " — each has its own label and may be approved for different uses.")
    if len(kinds) > 1:
        mine, other = ("over-the-counter", "prescription") if _kind(label) == "otc" else ("prescription", "otc")
        lines.append(f"This medicine is sold both over the counter and on prescription, at different doses. This is the {mine} label; "
                     f'for the other one: the same call with "kind": "{other}".')
    lines.append("")

    have = [key for key in label if key not in _SKIP and not key.endswith("_table") and _text(label, key)]
    exact = re.sub(r"[^a-z]+", "_", about.strip().lower()).strip("_")
    chosen = (_title(exact), (exact,)) if exact in have else topic_for(about)
    shown: List[str] = []
    if chosen is not None:
        topic, sections = chosen
        present = [section for section in sections if section in have]
        if not present:
            lines.append(f"This label has no section on {topic}.")
            lines.append("")
        room = ROOM
        for position, section in enumerate(present):
            text = _text(label, section)
            if position == 0:
                parts = _pages(text)
                if page > len(parts):
                    lines.append(f"## {_title(section)}")
                    lines.append(f"(There is no page {page}: this section ends on page {len(parts)}.)")
                    lines.append("")
                    shown.append(section)
                    break
                piece = parts[page - 1]
                lines.append(f"## {_title(section)}" + (f" (page {page} of {len(parts)})" if len(parts) > 1 else ""))
                lines.append(piece)
                if page < len(parts):
                    lines.append(f'[The section goes on. For the rest: the same call with "page": {page + 1}.]')
                lines.append("")
                room -= len(piece)
                shown.append(section)
                if page > 1:
                    break
            elif room >= 500:
                piece = _cut(text, min(PAGE, room))
                room -= len(piece)
                shown.append(section)
                lines.append(f"## {_title(section)}")
                lines.append(piece + (f' [more: "about": "{section}"]' if len(text) > len(piece) else ""))
                lines.append("")
    else:
        for section, room in OVERVIEW:
            if section in have:
                text = _text(label, section)
                shown.append(section)
                lines.append(f"## {_title(section)}")
                lines.append(_cut(text, room) + (f' [more: "about": "{section}"]' if len(text) > room else ""))
                lines.append("")
    rest = [_title(section) for section in have if section not in shown]
    if rest:
        lines.append("The label also has: " + "; ".join(rest[:24]) + '. Ask for any of them with "about".')
    lines.append("This is the maker's label as filed with the US Food and Drug Administration. Give its figures as written and "
                 "say they come from the label; where the label does not answer the question, say so rather than filling in from memory.")
    return "\n".join(lines), [{"url": link, "title": f"{brand} — official label (DailyMed)"}]
