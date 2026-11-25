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
        query, scope, count, shelf = raw, "all", 8, ""
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                query = str(parsed.get("query") or parsed.get("q") or "").strip()
                shelf = str(parsed.get("shelf") or parsed.get("category") or "").strip()[:120]
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
            where = f" (shelf: {shelf[:60]})" if shelf else ""
            await progress_cb({"elapsed_s": 0, "tail": f"Searching the offline library for: {query[:160]}{where}"})

        def _run():
            results = library.library_search(query, count=count, scope=scope, shelf=shelf)
            # The excerpt budget shrinks to keep the whole block under the
            # agent's output cap, so the closing rules are never cut off.
            return library.format_for_model(
                query, results, fetch=3, budget=2600, max_chars=MAX_OUTPUT_CHARS - 100, shelf=shelf,
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


class LibraryBrowseTool:
    """``library_browse`` — what the offline library holds, shelf by shelf, and
    whether it has a given book. Reads the catalogue only: no searching of
    text, nothing that needs the internet."""

    PER_PAGE = 40

    @staticmethod
    def _item_line(item: dict, with_shelf: bool = False) -> str:
        from services.library import catalogue, shelves

        line = f"- [{item['title']}]({catalogue.link(item['book'], item['key'])})"
        who = item.get("author") or ""
        if who:
            line += f" — {who}" + (f" ({item['dates']})" if item.get("dates") else "")
        elif item.get("dates"):
            line += f" — {item['dates']}"
        if item.get("form"):
            line += f" · {item['form']}"
        if with_shelf:
            line += f" · on {shelves.display(item['shelf'])}"
        return line

    def _tree(self) -> str:
        from services.library import catalogue, kiwix

        books = kiwix.list_books() if kiwix.available() else []
        lines = ["THE LIBRARY'S SHELVES — category: shelf (collections + books and documents filed there)"]
        for category in catalogue.overview(books):
            lines.append(f"\n{category['name']} — {category['about']}" if category["about"] else f"\n{category['name']}")
            shelves_here = []
            for shelf in category["shelves"]:
                held = len(shelf["collections"]), shelf["items"]
                if not any(held):
                    continue
                parts = ([f"{held[0]} collection{'' if held[0] == 1 else 's'}"] if held[0] else []) + ([f"{held[1]:,}"] if held[1] else [])
                shelves_here.append(f"{shelf['name']} ({' + '.join(parts)})")
            direct = len(category["collections"]), category["own_items"]
            if any(direct):
                parts = ([f"{direct[0]} whole collection{'' if direct[0] == 1 else 's'}"] if direct[0] else []) + \
                        ([f"{direct[1]:,} not sorted further"] if direct[1] else [])
                shelves_here.append("in the category itself: " + " + ".join(parts))
            lines.append("  " + ("; ".join(shelves_here) if shelves_here else "(nothing yet)"))
        rule = catalogue.keeping()
        if rule["min_rating"] > 0 and catalogue.hidden_under():
            lines.append(f"\nThe owner keeps the better-read books only: {catalogue.hidden_under():,} Project Gutenberg books below "
                         f"a popularity of {rule['min_rating']:g} are left off the shelves and out of searches "
                         f"(not {', '.join(rule['keep_whole'])}: those are kept whole). "
                         'They are still on disk: add "all": true to list or find them.')
        lines.append('\nLook at one with {"shelf": "Category/Shelf"}; find a book with {"find": "title or author"}; '
                     'search inside a shelf with library_search and its "shelf".')
        return "\n".join(lines)

    def _shelf(self, place: str, page: int, everything: bool = False) -> str:
        from services.library import catalogue, kiwix, shelves

        books = kiwix.list_books() if kiwix.available() else []
        titles = {book["id"]: book for book in books}
        category, shelf = shelves.split(place)
        lines = [f"{shelves.display(place)} — {shelves.about(place)}".rstrip(" —")]
        if not shelf:                                    # a category: its shelves, then what sits in it directly
            for entry in catalogue.overview(books):
                if entry["name"] != category:
                    continue
                for sub in entry["shelves"]:
                    held = len(sub["collections"]), sub["items"]
                    if any(held):
                        lines.append(f"- {sub['path']}: {held[0]} collection(s), {held[1]:,} books and documents")
        here = catalogue.whole_collections(books, place)
        if here:
            lines.append("\nWhole collections here:")
            for book in here:
                lines.append(f"- {book['title']}: {book['summary']} ({book['articles']:,} pages)")
        found = {"total": 0, "items": []}
        if shelf:
            found = catalogue.items(place, offset=(page - 1) * self.PER_PAGE, limit=self.PER_PAGE, everything=everything)
        elif catalogue.counts().get(place):
            found = {"total": catalogue.counts()[place], "items": []}
            lines.append(f"\n{found['total']:,} more are filed under {category} without a shelf yet.")
        if found["items"]:
            first = (page - 1) * self.PER_PAGE + 1
            lines.append(f"\nBooks and documents {first}–{first + len(found['items']) - 1} of {found['total']:,}, by title:")
            lines += [self._item_line(item) for item in found["items"]]
            if first + len(found["items"]) - 1 < found["total"]:
                lines.append(f'More: {{"shelf": "{place}", "page": {page + 1}}} — or name what you want with "find".')
        elif shelf and not here:
            lines.append("Nothing is filed here yet.")
        below = 0 if everything else catalogue.hidden_under(place)
        if below:
            lines.append(f'\n{below:,} less-read books here are below the owner\'s cut and not listed; '
                         f'{{"shelf": "{place}", "all": true}} lists them too.')
        return "\n".join(lines)

    async def execute(self, content: str, ctx: dict) -> dict:
        from services.library import catalogue, config as library_config, shelves

        if not library_config.enabled():
            return {"error": "The offline library is switched off (setting library_enabled).", "exit_code": 1}
        raw = (content or "").strip()
        args: dict = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                args = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                args = {}
        elif raw:
            args = {"shelf": raw} if shelves.resolve(raw.split("\n")[0]) else {"find": raw.split("\n")[0]}
        wanted = str(args.get("shelf") or args.get("category") or "").strip()
        find = str(args.get("find") or args.get("title") or args.get("author") or "").strip()[:160]
        page = args.get("page")
        page = page if isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 500 else 1
        everything = args.get("all") is True

        def work() -> dict:
            place = shelves.resolve(wanted) if wanted else ""
            if wanted and not place:
                return {"error": f'There is no shelf called "{wanted}". Categories: ' + ", ".join(shelves.categories()) +
                                 '. Call library_browse with no arguments to see every shelf.', "exit_code": 1}
            if find:
                items = catalogue.find(find, limit=20, scope=place, everything=everything)
                where = f" on {shelves.display(place)}" if place else ""
                if not items:
                    below = [] if everything else catalogue.find(find, limit=5, scope=place, everything=True)
                    if below:                            # it is here, only below the owner's cut: say so rather than "no"
                        lines = [f'Nothing kept on the shelves matches "{find}"{where}, but {len(below)} less-read book(s) below '
                                 "the owner's cut do (they are on disk and open normally):"]
                        lines += [self._item_line(item, with_shelf=True) for item in below]
                        return {"output": "\n".join(lines), "exit_code": 0}
                    return {"output": f'No book or document with "{find}" in its title or author{where}. That only says it is not '
                                      "catalogued by that name: library_search looks inside the texts.", "exit_code": 0}
                lines = [f'{len(items)} catalogued under "{find}"{where}:']
                lines += [self._item_line(item, with_shelf=True) for item in items]
                return {"output": "\n".join(lines), "exit_code": 0}
            return {"output": self._shelf(place, page, everything) if place else self._tree(), "exit_code": 0}

        try:
            result = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, work), timeout=_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"error": f"library_browse timed out after {_TIMEOUT_S}s", "exit_code": 1}
        except Exception as e:
            return {"error": f"library_browse failed: {type(e).__name__}: {str(e) or 'no details'}", "exit_code": 1}
        if "output" in result and len(result["output"]) > MAX_OUTPUT_CHARS:
            result["output"] = result["output"][:MAX_OUTPUT_CHARS - 80] + "\n… (cut short — ask for one shelf or use find)"
        return result


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

    @staticmethod
    async def _scan(progress_cb, ask_model: bool = True) -> dict:
        """Take in whatever was copied into the library folder since the last
        look, file it, and (unless told not to) let the model file what the
        folder and title did not settle."""
        from services.library import catalogue, classify, files

        started = asyncio.get_running_loop().time()
        notes: list = []

        def say(message: str) -> None:
            notes.append(message)

        async def tell(message: str) -> None:
            if progress_cb:
                try:
                    await progress_cb({"elapsed_s": round(asyncio.get_running_loop().time() - started, 1), "tail": message})
                except Exception:
                    pass

        await tell("Looking through the library folder for new files…")
        summary = await asyncio.to_thread(lambda: files.scan(progress=say))
        if summary.get("busy"):
            return {"output": "A scan of the library folder is already running. Ask again in a few minutes.", "exit_code": 0}
        await asyncio.to_thread(catalogue.build)
        asked = {"filed": 0, "asked": 0, "left": 0}
        waiting = len(classify.pending())
        if ask_model and waiting:
            await tell(f"Filing {min(waiting, 40)} of them with the model…")
            asked = await classify.run(limit=40)
            waiting = len(classify.pending())
        lines = [f"{summary['new']} new file(s) read into the library; {summary['unchanged']} were already there."]
        if summary["same"]:
            lines.append(f"{summary['same']} were second copies of files already here and were not read twice.")
        if summary["failed"]:
            lines.append(f"{summary['failed']} could not be read (damaged or password-protected).")
        if summary["removed"]:
            lines.append(f"{summary['removed']} that are gone from the folder were taken out of the index.")
        if summary["arriving"]:
            lines.append(f"{summary['arriving']} are still being copied and were left for the next scan.")
        other = {kind: count for kind, count in (summary.get("other") or {}).items() if kind not in (".jpg", ".jpeg", ".png", ".gif")}
        if other:
            lines.append("Not readable as documents: " + ", ".join(f"{count} {kind}" for kind, count in sorted(other.items(), key=lambda kv: -kv[1])[:6]) + ".")
        if asked["filed"]:
            lines.append(f"The model looked at {asked['asked']} and filed {asked['filed']} on shelves.")
        if waiting:
            lines.append(f"{waiting} are filed by folder and title only so far; scan again to have the model look at more.")
        return {"output": "\n".join(lines), "exit_code": 0}

    @staticmethod
    def _move(reference: str, shelf: str) -> dict:
        from services.library import catalogue, shelves

        if not reference.strip() or not shelf.strip():
            return {"error": 'move needs "document" (its title) and "shelf" (where it should go).', "exit_code": 1}
        found = catalogue.lookup_title(reference, limit=4)
        if not found:
            return {"error": f"Nothing in the catalogue matches “{reference}”.", "exit_code": 1}
        exact = [item for item in found if item["title"].lower() == reference.strip().lower() or item["key"] == reference.strip()]
        if len(found) > 1 and len(exact) != 1:
            names = "; ".join(f"“{item['title']}” ({shelves.display(item['shelf'])})" for item in found)
            return {"error": f"More than one matches “{reference}”: {names}. Give the full title.", "exit_code": 1}
        item = exact[0] if exact else found[0]
        try:
            moved = catalogue.refile(item["book"], item["key"], shelf)
        except ValueError as exc:
            return {"error": f"{exc} Shelves: call manage_library with action shelves.", "exit_code": 1}
        return {"output": f"Moved “{moved['title']}” to {shelves.display(moved['shelf'])}.", "exit_code": 0}

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
            if action == "shelves":
                return {"output": await asyncio.to_thread(LibraryBrowseTool()._tree), "exit_code": 0}
            if action == "scan":
                return await self._scan(progress_cb, ask_model=args.get("ask_model") is not False)
            if action == "move":
                return await asyncio.to_thread(self._move, str(args.get("document") or args.get("title") or ""),
                                               str(args.get("shelf") or ""))
            if action == "remove":
                gone = await asyncio.to_thread(additions.remove, str(args.get("document") or args.get("path") or args.get("title") or ""))
                return {"output": f"Removed “{gone['removed']}” from the library.", "exit_code": 0}
            if action != "add":
                return {"error": "manage_library actions: add, list, remove, sections, scan, shelves, move.", "exit_code": 1}

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
