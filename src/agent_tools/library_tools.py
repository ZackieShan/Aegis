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


class ManageLibraryTool:
    """``manage_library`` — add documents to the offline library, list what
    was added, remove one. Administrators only (it changes what everyone's
    searches find and downloads files to the server)."""

    @staticmethod
    def _upload(reference: str) -> tuple:
        """``(path, the name it was uploaded under)`` for a chat upload given
        by its id or by its stored path; the reference itself and no name for
        any other file."""
        import os
        from src.constants import UPLOAD_DIR

        reference = str(reference or "").strip()
        try:
            with open(os.path.join(UPLOAD_DIR, "uploads.json"), "r", encoding="utf-8") as handle:
                index = json.load(handle)
        except (OSError, ValueError):
            return reference, ""
        wanted = os.path.realpath(reference) if os.path.sep in reference else ""
        for info in (index or {}).values():
            if not isinstance(info, dict) or not info.get("path"):
                continue
            if info.get("id") == reference or (wanted and os.path.realpath(str(info["path"])) == wanted):
                return str(info["path"]), str(info.get("original_name") or info.get("name") or "")
        return reference, ""

    async def execute(self, content: str, ctx: dict) -> dict:
        from services.library import additions, config as library_config

        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        owner = (ctx.get("owner") if isinstance(ctx, dict) else "") or ""
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict) or not args:
            return {"error": 'manage_library takes JSON, e.g. {"action": "add", "urls": ["https://…/manual.pdf"]}', "exit_code": 1}
        if not library_config.enabled():
            return {"error": "The offline library is switched off (setting library_enabled).", "exit_code": 1}
        action = str(args.get("action") or "add").strip().lower()

        def listing() -> str:
            entries = additions.list_added()
            if not entries:
                return "Nothing has been added to the library yet."
            lines = [f"{len(entries)} document(s) added to the library:"]
            for entry in entries[:60]:
                state = {"pending": " · OCR waiting", "running": " · OCR in progress", "failed": " · OCR failed",
                         "unavailable": " · scanned, OCR not installed"}.get(entry.get("ocr"), "")
                lines.append(f"- [{entry['title']}]({entry['url']}) — {entry.get('section')}, {entry.get('pages')} pages{state}")
            if len(entries) > 60:
                lines.append(f"… and {len(entries) - 60} more.")
            return "\n".join(lines)

        try:
            if action == "list":
                return {"output": await asyncio.to_thread(listing), "exit_code": 0}
            if action == "sections":
                return {"output": "Sections: " + ", ".join(additions.sections()), "exit_code": 0}
            if action == "remove":
                gone = await asyncio.to_thread(additions.remove, str(args.get("document") or args.get("path") or args.get("title") or ""))
                return {"output": f"Removed “{gone['removed']}” from the library.", "exit_code": 0}
            if action != "add":
                return {"error": "manage_library actions: add, list, remove, sections.", "exit_code": 1}

            def many(value):
                if isinstance(value, str):
                    return [value] if value.strip() else []
                return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []

            urls = many(args.get("urls") or args.get("url"))
            files = many(args.get("files") or args.get("file") or args.get("paths"))
            if not urls and not files:
                return {"error": "Nothing to add: give urls (links to PDF files) and/or files (uploaded PDFs, by the path or id "
                                 "shown in the uploaded-files list).", "exit_code": 1}
            total = len(urls) + len(files)
            if total > 25:
                return {"error": "At most 25 documents per call. Add them in batches.", "exit_code": 1}
            section = str(args.get("section") or "")
            title = str(args.get("title") or "") if total == 1 else ""
            as_page = args.get("as_page") is True
            lines, added, failed = [], 0, 0
            started = asyncio.get_running_loop().time()
            for number, (kind, reference) in enumerate([("url", u) for u in urls] + [("file", f) for f in files], start=1):
                if progress_cb:
                    try:
                        await progress_cb({"elapsed_s": round(asyncio.get_running_loop().time() - started, 1),
                                           "tail": f"{number}/{total}: {'downloading' if kind == 'url' else 'reading'} {reference[:120]}"})
                    except Exception:
                        pass
                try:
                    if kind == "url":
                        entry = await asyncio.to_thread(lambda: additions.add_from_url(
                            reference, as_page=as_page, section=section, title=title, added_by=owner))
                    else:
                        path, uploaded_as = self._upload(reference)
                        details = {"section": section, "title": title, "added_by": owner}
                        if uploaded_as:                      # file it under the name it had, not the upload's id
                            details.update(name=uploaded_as, source=f"uploaded: {uploaded_as}")
                        entry = await asyncio.to_thread(lambda: additions.add_from_file(path, **details))
                except additions.AdditionError as exc:
                    failed += 1
                    lines.append(f"- NOT added — {reference[:160]}: {exc}")
                    continue
                added += 1
                if entry["status"] == "already":
                    lines.append(f"- Already in the library: [{entry['title']}]({entry['url']}) ({entry.get('section')}).")
                else:
                    note = f" {entry['note']}" if entry.get("note") else ""
                    pages = f"{entry['pages']} page{'' if entry['pages'] == 1 else 's'}"
                    lines.append(f"- Added [{entry['title']}]({entry['url']}) — {pages}, section "
                                 f"{entry['section']}. Searchable now.{note}")
            head = f"{added} of {total} document(s) are in the library." if failed else \
                (f"{added} document(s) are in the library." if total > 1 else "")
            return {"output": "\n".join(([head] if head else []) + lines), "exit_code": 0 if added else 1}
        except additions.AdditionError as exc:
            return {"error": f"manage_library: {exc}", "exit_code": 1}
        except Exception as exc:
            return {"error": f"manage_library failed: {type(exc).__name__}: {str(exc) or 'no details'}", "exit_code": 1}
