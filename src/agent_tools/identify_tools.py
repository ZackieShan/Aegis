"""``identify_photo`` — the photo identifier as an agent tool.

"What is this, and is it safe?" for a photo the person sent: the species
classifier and the home library for a living thing or an animal's sign, a
reading for print, a description for anything else (``services/identify``).
Until this tool existed a photo that arrived with a question went to a general
vision model, which names mushrooms by guesswork.

The photo is one of the caller's own uploads: the id from the uploaded-files
list, or — left out — the picture they sent most recently.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

from src.constants import MAX_OUTPUT_CHARS

RECENT = timedelta(hours=2)          # how far back "the photo I just sent" reaches


def _uploads() -> dict:
    from src.constants import UPLOAD_DIR
    try:
        with open(os.path.join(UPLOAD_DIR, "uploads.json"), "r", encoding="utf-8") as handle:
            index = json.load(handle)
        return index if isinstance(index, dict) else {}
    except (OSError, ValueError):
        return {}


def _mine(info: dict, owner: Optional[str]) -> bool:
    """An upload is its owner's alone; with sign-in switched off, the one person's."""
    return info.get("owner") == owner if owner else info.get("owner") is None


def owned_photo(reference: str, owner: Optional[str]) -> Optional[dict]:
    """The caller's own image upload named by id (or its file name) — or, with no reference, the picture
    they uploaded most recently. None when there is none that is theirs."""
    reference = str(reference or "").strip()
    images = [info for info in _uploads().values()
              if isinstance(info, dict) and info.get("path") and str(info.get("mime") or "").startswith("image/") and _mine(info, owner)]
    if reference:
        wanted = os.path.basename(reference)
        for info in images:
            if wanted in (info.get("id"), os.path.basename(str(info["path"])), info.get("name"), info.get("original_name")):
                return info if os.path.isfile(str(info["path"])) else None
        return None
    newest, newest_at = None, None
    for info in images:
        try:
            at = datetime.fromisoformat(str(info.get("uploaded_at") or ""))
        except ValueError:
            continue
        if (newest_at is None or at > newest_at) and os.path.isfile(str(info["path"])):
            newest, newest_at = info, at
    if newest is None or datetime.now() - newest_at > RECENT:
        return None
    return newest


class IdentifyPhotoTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services import identify

        raw = (content or "").strip()
        args: dict = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                args = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                args = {}
        elif raw:
            args = {"photo": raw}
        owner = (ctx.get("owner") if isinstance(ctx, dict) else "") or None
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        want = str(args.get("as") or args.get("want") or "").strip().lower()
        want = {"label": "text", "read": "text", "ocr": "text", "print": "text", "description": "describe", "species": "living",
                "plant": "living", "animal": "living"}.get(want, want)
        if want not in identify.WANTS:
            want = ""
        photo = owned_photo(str(args.get("photo") or args.get("file") or args.get("id") or ""), owner)
        if photo is None:
            return {"error": "identify_photo: there is no photo of theirs to look at. Ask them to send one (the camera button), "
                             "or give the id of an image from the uploaded-files list in `photo`.", "exit_code": 1}
        if progress_cb:
            try:
                await progress_cb({"elapsed_s": 0, "tail": "Looking at the photo…"})
            except Exception:
                pass
        try:
            found = await identify.identify_image(str(photo["path"]), hint=str(args.get("note") or args.get("hint") or "")[:500],
                                                  owner=owner, want=want)
        except identify.IdentifyError as problem:
            return {"error": f"identify_photo: {problem}", "exit_code": 1}
        except Exception as problem:
            return {"error": f"identify_photo failed: {type(problem).__name__}: {str(problem) or 'no details'}", "exit_code": 1}
        lead = ("Identification from the species identifier and the home library. Give it as it stands, cautions included; "
                "do not add identifying details from memory.\n\n" if found.get("living") else "")
        sources = found.get("sources") or []
        cited = "\n\nSources: " + "; ".join(f"[{s['title']}]({s['url']})" for s in sources[:4] if s.get("url")) if sources else ""
        return {"output": (lead + str(found.get("markdown") or "") + cited)[:MAX_OUTPUT_CHARS], "exit_code": 0, "card": found.get("card")}
