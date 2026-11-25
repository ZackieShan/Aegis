"""``translate`` — the offline translator as an agent tool.

Text from one language into another, on this machine (``services/translate``):
a dedicated translation model when it is installed, the chat model otherwise.
It keeps nothing: the Translate page has a history, a chat does not need one.
"""

import asyncio
import json

from src.constants import MAX_OUTPUT_CHARS

_TIMEOUT_S = 240


class TranslateTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services import translate

        owner = (ctx.get("owner") if isinstance(ctx, dict) else "") or None
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = (content or "").strip()
        args: dict = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                args = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                args = {}
        text = str(args.get("text") or "")
        target = str(args.get("to") or args.get("target") or args.get("language") or "").strip()
        source = str(args.get("from") or args.get("source") or "auto").strip()
        if not text.strip() or not target:
            return {"error": 'translate takes JSON: {"text": "…", "to": "Spanish"} (and "from" when you know the language it is in).',
                    "exit_code": 1}
        if progress_cb:
            try:
                await progress_cb({"elapsed_s": 0, "tail": f"Translating into {target[:30]}…"})
            except Exception:
                pass
        try:
            result = await asyncio.wait_for(translate.translate_text(text, target, source, owner=owner), timeout=_TIMEOUT_S)
        except translate.TranslateError as exc:
            return {"error": f"translate: {exc}", "exit_code": 1}
        except asyncio.TimeoutError:
            return {"error": f"translate timed out after {_TIMEOUT_S}s — send a shorter piece.", "exit_code": 1}
        except Exception as exc:
            return {"error": f"translate failed: {type(exc).__name__}: {str(exc) or 'no details'}", "exit_code": 1}
        if result.get("same_language"):
            return {"output": f"That text is already in {result['target_name']}; nothing to translate.", "exit_code": 0}
        told = {"detected": " (language detected)", "asked": " (language detected)"}.get(str(result.get("detected")), "")
        head = f"{result['source_name'] or 'Unknown language'}{told} → {result['target_name']}:"
        output = f"{head}\n{result['text']}"
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS - 60] + "\n… (cut short — translate it in smaller pieces)"
        card = {"type": "translation", "from": result.get("source_name") or "", "from_code": result.get("source") or "",
                "to": result.get("target_name") or "", "to_code": result.get("target") or "", "source": text.strip()[:400],
                "text": str(result.get("text") or "").strip()[:2000]}
        return {"output": output, "exit_code": 0, "card": card}
