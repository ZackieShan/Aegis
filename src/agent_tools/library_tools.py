"""``library_search`` — the offline reference library as an agent tool.

The library (``services/library``) is a local Kiwix server with Wikipedia,
medical references, Stack Exchange archives and how-to collections, plus a
page-level index of the PDF manuals inside it. Nothing here needs the
internet, so this is the tool that keeps answering when ``web_search`` cannot.
"""

import asyncio
import json

from src.constants import MAX_OUTPUT_CHARS

_TIMEOUT_S = 45


class LibrarySearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services import library
        from services.library import config as library_config

        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = (content or "").strip()
        query, scope, count = raw, "all", 8
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                query = str(parsed.get("query") or parsed.get("q") or "").strip()
                wanted = str(parsed.get("scope") or "all").strip().lower()
                if wanted in library.VALID_SCOPES:
                    scope = wanted
                limit = parsed.get("max_results", parsed.get("count"))
                if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 20:
                    count = limit
        if not query:
            query = raw.split("\n")[0].strip() if not raw.startswith("{") else ""
        if not query:
            return {"error": "library_search needs a query (the key terms to look up).", "exit_code": 1}

        if not library_config.enabled():
            return {
                "error": "The offline library is switched off (setting library_enabled). Use web_search instead.",
                "exit_code": 1,
            }

        if progress_cb:
            await progress_cb({"elapsed_s": 0, "tail": f"Searching the offline library for: {query[:160]}"})

        def _run():
            results = library.library_search(query, count=count, scope=scope)
            # The excerpt budget shrinks to keep the whole block under the
            # agent's output cap, so the closing rules are never cut off.
            return library.format_for_model(
                query, results, fetch=3, budget=2600, max_chars=MAX_OUTPUT_CHARS - 100,
            )

        loop = asyncio.get_running_loop()
        try:
            text, sources = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"error": f"library_search timed out after {_TIMEOUT_S}s: {query[:200]}", "exit_code": 1}
        except Exception as e:
            return {
                "error": f"library_search failed: {type(e).__name__}: {str(e) or 'no details'}",
                "exit_code": 1,
            }

        if not sources:
            # "No matches" and "the server is down" must not look the same:
            # the model would burn its turns rephrasing a query nothing can
            # answer. Ask afresh rather than trusting the cached state.
            from services.library import kiwix as library_server
            if not library_server.available(force=True):
                text += (
                    "\n\nNote: the library server (kiwix-serve) is not answering right now, so only the "
                    "PDF-manual index could be searched. It restarts by itself within seconds — "
                    "try this search once more before rephrasing it."
                )
        output = text[:MAX_OUTPUT_CHARS] if len(text) > MAX_OUTPUT_CHARS else text
        if sources:
            output += "\n\n<!-- SOURCES:" + json.dumps(sources) + " -->"
        return {"output": output, "exit_code": 0}
