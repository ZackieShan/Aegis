"""Home tools: the assistant keeping a household's everyday things.

    manage_lists       lists and plans — add, tick off, remove, show
    manage_reminders   remind at a time, once or repeating
    manage_trackers    follow a habit, an amount or a rating day by day
    manage_boards      collect links, pictures and notes on a board

All four work only on the things of the account the chat belongs to, and so
are open to non-administrators whose role includes the "home" group (see
``core/access.py``). The work is in ``services/home``; this module parses the
model's JSON, finds what a name refers to, and words the result so the model
can pass it on in a sentence, with a link the person can tap.
"""

import asyncio
import json
from typing import Any, Dict, List

from src.constants import MAX_OUTPUT_CHARS

HOME_TOOLS = ("manage_lists", "manage_reminders", "manage_trackers", "manage_boards")


def _parse(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    words = raw.split(None, 1)
    return {"action": words[0].lower(), "name": words[1].strip() if len(words) > 1 else ""}


def _ok(text: str) -> dict:
    return {"output": str(text)[:MAX_OUTPUT_CHARS], "exit_code": 0}


def _fail(message: str) -> dict:
    return {"error": message, "exit_code": 1}


def _texts(args: Dict[str, Any], *keys: str) -> List[str]:
    """Items given as a list, or as one string with commas / new lines."""
    for key in keys:
        value = args.get(key)
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            parts = [p.strip(" -•\t") for p in value.replace("\n", ",").split(",")]
            return [p for p in parts if p]
    return []


async def _run(tool: str, work):
    from services.home import HomeError

    try:
        return await asyncio.to_thread(work)
    except HomeError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"{tool} failed: {type(exc).__name__}: {exc}")


def _list_text(view: dict, limit: int = 40) -> str:
    lines = [f"{view['title']} — {view['open']} to do, {view['done']} done. Link: [{view['title']}](/home#lists/{view['id']})"]
    for item in view["items"][:limit]:
        lines.append(f"{'[x]' if item['done'] else '[ ]'} {item['text']}")
    if len(view["items"]) > limit:
        lines.append(f"… and {len(view['items']) - limit} more")
    return "\n".join(lines)


class ManageListsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        owner = (ctx or {}).get("owner")

        def work():
            from services.home import HomeError, lists

            action = str(args.get("action") or "list").lower().replace("-", "_")
            name = str(args.get("list") or args.get("name") or args.get("title") or "").strip()
            items = _texts(args, "items", "item", "texts", "text")
            if action in ("list", "lists", "all"):
                everything = lists.all_lists(owner)
                if not everything:
                    return _ok("There are no lists yet.")
                return _ok("\n".join(f"- {v['title']} ({v['kind']}): {v['open']} to do, {v['done']} done"
                                     for v in everything))
            if action in ("add", "add_items", "create", "new", "plan"):
                if not name:
                    raise HomeError("Which list? Give its name.")
                found = lists.find_list(owner, name)
                if found is None:
                    view = lists.create_list(owner, name, items, kind="plan" if (action == "plan" or args.get("kind") == "plan") else "list")
                    return _ok(f"Created the list and added {len(items)} item(s).\n{_list_text(view)}")
                if not items:
                    return _ok(f"That list already exists.\n{_list_text(found)}")
                view = lists.add_items(owner, found["id"], items)
                return _ok(f"Added to {view['title']}.\n{_list_text(view)}")
            found = lists.find_list(owner, name) if name else None
            if found is None:
                titles = ", ".join(v["title"] for v in lists.all_lists(owner)) or "none yet"
                raise HomeError(f"I could not tell which list you mean{f' by “{name}”' if name else ''}. Lists: {titles}.")
            if action in ("show", "view", "get", "read"):
                return _ok(_list_text(found))
            if action in ("check", "done", "tick", "complete", "uncheck", "undo"):
                if not items:
                    raise HomeError("Which items?")
                view = lists.check_items(owner, found["id"], items, done=action not in ("uncheck", "undo"))
                missing = view.pop("not_found", [])
                note = f" Not on the list: {', '.join(missing)}." if missing else ""
                return _ok(f"Updated {view['title']}.{note}\n{_list_text(view)}")
            if action in ("remove", "remove_items", "delete_items"):
                if not items:
                    raise HomeError("Which items should come off?")
                return _ok(f"Removed.\n{_list_text(lists.remove_items(owner, found['id'], items))}")
            if action in ("clear_done", "clear", "tidy"):
                return _ok(f"Cleared what was done.\n{_list_text(lists.clear_done(owner, found['id']))}")
            if action == "rename":
                new_name = str(args.get("new_name") or args.get("to") or "").strip()
                return _ok(_list_text(lists.update_list(owner, found["id"], title=new_name)))
            if action in ("delete", "delete_list"):
                lists.delete_list(owner, found["id"])
                return _ok(f"Deleted the list “{found['title']}”.")
            raise HomeError("action is one of: list, add, show, check, uncheck, remove, clear_done, rename, delete")

        return await _run("manage_lists", work)


def _when(iso_text: str) -> str:
    from datetime import datetime

    try:
        from src.user_time import user_timezone
        local = datetime.fromisoformat(iso_text).astimezone(user_timezone())
    except Exception:
        try:
            local = datetime.fromisoformat(iso_text).astimezone()
        except Exception:
            return iso_text
    hour = local.hour % 12 or 12
    return f"{local.strftime('%A %B')} {local.day}, {hour}:{local.minute:02d} {local.strftime('%p').lower()}"


class ManageRemindersTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        owner = (ctx or {}).get("owner")

        def work():
            from services.home import HomeError, reminders
            from services.home.common import best_match

            action = str(args.get("action") or "list").lower().replace("-", "_")
            text = str(args.get("text") or args.get("title") or args.get("name") or "").strip()
            when = args.get("when") or args.get("due") or args.get("time") or args.get("due_date")

            def describe(view: dict) -> str:
                repeat = "" if view["repeat"] == "none" else f", repeats {view['repeat']}"
                return f"“{view['text']}” — {_when(view['due'])}{repeat}"

            if action in ("add", "create", "set", "remind", "new"):
                view = reminders.create_reminder(owner, text, when, args.get("repeat") or "none")
                lead = "That reminder already exists" if view.get("duplicate") else "Reminder set"
                return _ok(f"{lead}: {describe(view)}. Link: [Reminders](/home#reminders)")
            mine = reminders.all_reminders(owner)
            if action in ("list", "show", "all"):
                if not mine:
                    return _ok("There are no reminders.")
                return _ok("\n".join(f"- {describe(v)}{' (overdue)' if v['overdue'] else ''}" for v in mine[:40]))
            chosen = next((v for v in mine if v["id"] == str(args.get("id") or "")), None)
            if chosen is None and text:
                chosen = best_match(mine, text, key=lambda v: v["text"])
            if chosen is None:
                raise HomeError("I could not tell which reminder you mean. Reminders: "
                                + ("; ".join(v["text"] for v in mine[:15]) or "none") + ".")
            if action in ("done", "complete", "finish"):
                view = reminders.update_reminder(owner, chosen["id"], done=True)
                return _ok(f"Done: “{view['text']}”." + (f" Next time: {_when(view['due'])}." if not view["done"] else ""))
            if action == "snooze":
                view = reminders.snooze(owner, chosen["id"], int(args.get("minutes") or 60))
                return _ok(f"Snoozed until {_when(view['due'])}.")
            if action in ("update", "move", "change", "reschedule"):
                view = reminders.update_reminder(owner, chosen["id"], text=args.get("new_text"), due=when,
                                                 repeat=args.get("repeat"))
                return _ok(f"Changed: {describe(view)}.")
            if action in ("delete", "remove", "cancel"):
                reminders.delete_reminder(owner, chosen["id"])
                return _ok(f"Removed the reminder “{chosen['text']}”.")
            raise HomeError("action is one of: add, list, done, snooze, update, delete")

        return await _run("manage_reminders", work)


def _tracker_text(view: dict) -> str:
    unit = f" {view['unit']}" if view["unit"] else ""
    if view["kind"] == "check":
        today = "done today" if view["done_today"] else "not done yet today"
    elif view["kind"] == "scale":
        today = f"today {view['today']:g}/5" if view["today"] else "no rating yet today"
    else:
        goal = f" of {view['goal']:g}" if view["goal"] else ""
        today = f"today {view['today']:g}{goal}{unit}"
    streak = f", {view['streak']}-day streak" if view["streak"] > 1 else ""
    return f"{view['name']}: {today}{streak}. Link: [{view['name']}](/home#trackers/{view['id']})"


class ManageTrackersTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        owner = (ctx or {}).get("owner")

        def work():
            from services.home import HomeError, trackers

            action = str(args.get("action") or "list").lower().replace("-", "_")
            name = str(args.get("tracker") or args.get("name") or "").strip()
            if action in ("list", "all", "summary"):
                everything = trackers.all_trackers(owner)
                if not everything:
                    return _ok("There are no trackers yet.")
                return _ok("\n".join("- " + _tracker_text(v) for v in everything))
            if action in ("create", "new", "start", "add"):
                view = trackers.create_tracker(owner, name, kind=str(args.get("kind") or "check"),
                                               unit=str(args.get("unit") or ""), goal=args.get("goal"))
                lead = "That tracker already exists" if view.get("duplicate") else "Tracker started"
                return _ok(f"{lead}. {_tracker_text(view)}")
            found = trackers.find_tracker(owner, name) if name else None
            if found is None:
                names = ", ".join(v["name"] for v in trackers.all_trackers(owner)) or "none yet"
                raise HomeError(f"I could not tell which tracker you mean{f' by “{name}”' if name else ''}. Trackers: {names}.")
            if action in ("log", "record", "track", "done", "check"):
                value = args.get("value", args.get("amount"))
                try:
                    value = None if value in (None, "") else float(value)
                except (TypeError, ValueError):
                    raise HomeError("The value has to be a number.")
                view = trackers.log(owner, found["id"], value=value, note=str(args.get("note") or ""),
                                    day=args.get("day") or args.get("date"))
                return _ok(f"Logged. {_tracker_text(view)}")
            if action in ("show", "view", "get", "stats"):
                view = trackers.get_tracker(owner, found["id"], days=14)
                days = ", ".join(f"{d['day'][5:]}: {d['value']:g}" for d in view["series"] if d["value"]) or "nothing logged in the last two weeks"
                return _ok(f"{_tracker_text(view)}\nLast 14 days — {days}. Week total {view['week_total']:g}.")
            if action in ("delete", "remove"):
                trackers.delete_tracker(owner, found["id"])
                return _ok(f"Deleted the tracker “{found['name']}” and its history.")
            raise HomeError("action is one of: list, create, log, show, delete")

        return await _run("manage_trackers", work)


class ManageBoardsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        owner = (ctx or {}).get("owner")

        def work():
            from services.home import HomeError, boards

            action = str(args.get("action") or "list").lower().replace("-", "_")
            name = str(args.get("board") or args.get("name") or "").strip()
            if action in ("list", "all"):
                everything = boards.all_boards(owner)
                if not everything:
                    return _ok("There are no boards yet.")
                return _ok("\n".join(f"- {v['name']}: {v['pin_count']} pins" for v in everything))
            if action in ("create", "new"):
                view = boards.create_board(owner, name, str(args.get("description") or ""))
                return _ok(f"{'That board already exists' if view.get('duplicate') else 'Board created'}: "
                           f"[{view['name']}](/home#boards/{view['id']})")
            found = boards.find_board(owner, name) if name else None
            if action in ("pin", "add", "save"):
                if found is None:
                    if not name:
                        raise HomeError("Which board? Give its name (it is created if it does not exist).")
                    found = boards.create_board(owner, name)
                urls = _texts(args, "urls", "url", "links", "link")
                text = str(args.get("text") or args.get("note") or "").strip()
                title = str(args.get("title") or "").strip()
                pinned = []
                for url in urls[:12]:
                    pin = boards.add_link_pin(owner, found["id"], url, title=title if len(urls) == 1 else "",
                                              note=text if len(urls) == 1 else "")
                    pinned.append(pin["title"] or pin["site"] or url)
                if not urls:
                    if not text and not title:
                        raise HomeError("Give a link (url) or some text to pin.")
                    pin = boards.add_note_pin(owner, found["id"], text, title)
                    pinned.append(pin["title"] or "a note")
                return _ok(f"Pinned {len(pinned)} to [{found['name']}](/home#boards/{found['id']}): " + "; ".join(pinned))
            if found is None:
                names = ", ".join(v["name"] for v in boards.all_boards(owner)) or "none yet"
                raise HomeError(f"I could not tell which board you mean{f' by “{name}”' if name else ''}. Boards: {names}.")
            if action in ("show", "view", "get"):
                view = boards.get_board(owner, found["id"])
                lines = [f"{view['name']} — {view['pin_count']} pins. Link: [{view['name']}](/home#boards/{view['id']})"]
                lines += [f"- {p['title'] or p['note'][:80] or p['site'] or 'picture'}" + (f" ({p['site']})" if p['site'] else "")
                          for p in view["pins"][:40]]
                return _ok("\n".join(lines))
            if action in ("delete", "remove"):
                boards.delete_board(owner, found["id"])
                return _ok(f"Deleted the board “{found['name']}” and its pins.")
            raise HomeError("action is one of: list, create, pin, show, delete")

        return await _run("manage_boards", work)
