#!/usr/bin/env python3
"""LLM plan-review: a plain-English summary + anomaly flags for a move/rename
plan, shown before Execute (especially valuable when driving from a phone).

The LLM only NARRATES. The hard safety flags are computed deterministically in
Python, so a hallucinated "all clear" can never hide a real red flag, and a
danger flag always forces safe=False regardless of what the model says. This is
advisory only — it gates nothing; the user still clicks Execute.
"""
import json
import os

import llm_core


def _ext(p):
    return os.path.splitext(p or "")[1].lower()


def _norm(p):
    try:
        return os.path.normcase(os.path.abspath(p)) if p else ""
    except Exception:
        return ""


def deterministic_flags(entries, stats):
    """Hard, code-computed observations about the plan — never LLM-authored."""
    flags = []
    troot_n = _norm(stats.get("targetRoot") or "")
    ext_changes = escapes = 0
    for e in entries or []:
        to = e.get("to")
        if not to:
            continue
        if _ext(e.get("from")) != _ext(to):
            ext_changes += 1
        if troot_n and not _norm(to).startswith(troot_n):
            escapes += 1
    if escapes:
        flags.append({"severity": "danger", "count": escapes,
                      "text": f"{escapes} file(s) would be written OUTSIDE "
                              "the target folder"})
    if ext_changes:
        flags.append({"severity": "caution", "count": ext_changes,
                      "text": f"{ext_changes} file(s) change file extension"})
    if stats.get("collisionsResolved"):
        n = stats["collisionsResolved"]
        flags.append({"severity": "info", "count": n,
                      "text": f"{n} name collision(s) auto-suffixed (-2, -3…)"})
    if stats.get("dupeFiles"):
        n = stats["dupeFiles"]
        flags.append({"severity": "info", "count": n,
                      "text": f"{n} duplicate(s) quarantined to _Duplicates "
                              "(reversible)"})
    return flags


def _fallback_summary(stats, domain):
    n = stats.get("totalFiles") or 0
    act = (stats.get("action") or "move").lower()
    root = stats.get("targetRoot") or "the target folder"
    return f"{act.title()} {n} {domain} file(s) into {root}."


_STAT_KEYS = ("totalFiles", "action", "targetRoot", "foldersToCreate",
              "dupeFiles", "companionFiles", "collisionsResolved",
              "unidentifiedFiles", "vaFiles", "singleFiles", "albumFiles")


def summarize_plan(entries, stats, domain="media", cfg=None, fetcher=None):
    """{summary, warnings, safe} for a plan. Never raises."""
    stats = stats or {}
    flags = deterministic_flags(entries, stats)
    sample = [f"{os.path.basename(e['from'])} -> {e['to']}"
              for e in (entries or [])[:12] if e.get("to")]
    digest = {
        "domain": domain,
        "stats": {k: stats.get(k) for k in _STAT_KEYS if k in stats},
        "flags": [f["text"] for f in flags],
        "examples": sample,
    }
    prompt = (
        "You explain a file-organization plan to a non-technical user in 2-3 "
        "short, calm sentences. Describe what will happen; do NOT invent "
        "problems that aren't in the data, and do NOT alarm. "
        "Plan (JSON):\n" + json.dumps(digest, indent=1, default=str) +
        '\n\nReply with ONLY a JSON object: {"summary": "<2-3 sentences>"}.'
    )
    obj = llm_core.chat_json([{"role": "user", "content": prompt}],
                             model=llm_core.TEXT_MODEL, max_tokens=400,
                             cfg=cfg, fetcher=fetcher) or {}
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = _fallback_summary(stats, domain)
    # `safe` is purely deterministic — the LLM narrates but never decides
    # safety, so it can neither hide a real danger nor raise a false alarm.
    safe = not any(f["severity"] == "danger" for f in flags)
    return {"summary": summary.strip(), "warnings": flags, "safe": safe}
