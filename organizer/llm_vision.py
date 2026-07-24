#!/usr/bin/env python3
"""Vision tagging: caption + classify one photo with the local VL model
(qwen-vl via llama-swap). Produces ADVISORY metadata only — it is written to
new photos.db columns and used for search; it never moves, renames, or deletes
a file. Total-failure-safe: any problem returns None.
"""
import llm_assist
import llm_core

KINDS = ("photo", "screenshot", "document", "meme", "receipt")

_PROMPT = (
    "You are tagging a personal photo for a searchable library. Look at the "
    "image and reply with ONLY a JSON object, no prose, no markdown:\n"
    '{"caption": "<one plain descriptive sentence, at most 140 chars>", '
    '"tags": ["<3 to 8 short lowercase keywords: subjects, objects, place, '
    'activity>"], '
    '"scene": "<one or two words, e.g. beach, kitchen, city street, forest>", '
    '"kind": "one of photo|screenshot|document|meme|receipt", '
    '"quality": <integer 1-5, 5 = sharp and well composed>}'
)


def tag_photo(path, cfg=None, fetcher=None, long_edge=768):
    """Return {caption, tags[list], scene, kind, quality} for an image, or None
    (unreadable image, model unreachable, or unusable reply)."""
    text = llm_core.describe_image(path, _PROMPT, max_tokens=320,
                                   long_edge=long_edge, cfg=cfg, fetcher=fetcher)
    obj = llm_assist._extract_json(text)
    if not isinstance(obj, dict):
        return None
    caption = obj.get("caption")
    caption = caption.strip()[:140] if isinstance(caption, str) else None
    raw_tags = obj.get("tags")
    tags = []
    if isinstance(raw_tags, list):
        seen = set()
        for t in raw_tags:
            t = str(t).strip().lower()
            if t and t not in seen:
                seen.add(t)
                tags.append(t)
            if len(tags) >= 8:
                break
    scene = obj.get("scene")
    scene = str(scene).strip()[:40] if scene else None
    kind = obj.get("kind")
    kind = kind if kind in KINDS else "photo"
    quality = obj.get("quality")
    try:
        quality = min(5, max(1, int(quality)))
    except (TypeError, ValueError):
        quality = None
    if not caption and not tags:
        return None
    return {"caption": caption, "tags": tags, "scene": scene,
            "kind": kind, "quality": quality}
