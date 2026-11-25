"""One of each, and the wrenches on the administrator's side.

What must hold:
  * the knowledge graph and the second research handler are gone, and nothing still reaches for them;
  * a page is fetched one way: the Web toolbox reads through the same guarded fetcher as search, so it
    cannot be pointed at this machine or the home network;
  * a cup is the same cup in a recipe and in a conversion: the Kitchen's measures come from the
    Almanac's table;
  * "Scheduled" is one list: automations that run by themselves are shown beside the tasks, and an
    automation's "at 7 every day" means 7 on the house clock, not 7 UTC;
  * the slash list is the current commands only: older names, the skins and the extra doors to code
    are offered nowhere, and an administrator's command is neither offered to nor run for anyone else;
  * finished work finds the person: a picture or video that took a while, a scheduled task, an
    automation — and a notification is never pushed twice;
  * the server looks at itself each night and tells whoever runs it only when something is wrong;
  * a screen that does not draw cards gets the card's content in words.
"""
import asyncio
import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent


def read(*parts):
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


SLASH = read("static", "js", "slashCommands.js")
MENU = read("static", "js", "slashAutocomplete.js")
TASKS_JS = read("static", "js", "tasks.js")
APP = read("app.py")


# ── removed, and nothing reaches for it ─────────────────────────────────────

def test_the_knowledge_graph_is_gone():
    assert not (REPO / "src" / "graph_memory.py").exists() and not (REPO / "routes" / "graph_routes.py").exists()
    assert "graph_routes" not in APP and "graph_memory" not in read("src", "control_center.py")
    assert "_cmdGraph" not in SLASH and "/api/graph" not in SLASH and "\n  graph: {" not in SLASH
    assert "/graph" not in read("README.md")


def test_there_is_one_research_handler():
    assert not (REPO / "services" / "research").exists() and (REPO / "src" / "research_handler.py").exists()
    services = read("services", "__init__.py")
    assert "research" not in services.lower()
    import services as package
    assert "ResearchService" not in package.__all__ and {"SearchService", "MemoryService"} <= set(package.__all__)


# ── one page fetcher ────────────────────────────────────────────────────────

def test_the_web_toolbox_reads_pages_through_the_guarded_fetcher():
    source = read("mcp_servers", "web_server.py")
    assert "from services.search.content import read_public_page" in source
    assert "httpx" not in source and "BeautifulSoup" not in source and "follow_redirects" not in source          # no fetcher of its own
    spec = importlib.util.spec_from_file_location("web_server_under_test", REPO / "mcp_servers" / "web_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for private in ("http://127.0.0.1:9090/v1/models", "http://localhost/admin", "http://192.168.1.1/", "http://10.0.0.5:8080/"):
        answer = asyncio.run(module._extract_one(private))
        assert answer.startswith("Fetch failed") and "Blocked non-public" in answer, private
    assert "No pages could be crawled" in asyncio.run(module._crawl("http://127.0.0.1:7000/", 3)) or "failed" in asyncio.run(module._crawl("http://127.0.0.1:7000/", 3))


def test_a_page_is_read_as_markdown_with_its_own_sites_links():
    from services.search import content
    html = ("<html><head><title>Hollow Almanack</title></head><body><nav><a href='/menu'>menu</a></nav><main><h2>Frost</h2>"
            "<p>The first frost comes late.</p><ul><li>cover the beans</li></ul><blockquote>so they say</blockquote>"
            "<a href='/next#top'>next</a><a href='https://elsewhere.test/x'>away</a><a href='/logo.png'>logo</a></main>"
            "<script>bad()</script></body></html>")
    title, text, links = content.page_markdown(html, "https://almanack.test/a")
    assert title == "Hollow Almanack" and text == "## Frost\n\nThe first frost comes late.\n\n- cover the beans\n\n> so they say"
    assert links == ["https://almanack.test/next"]                       # same site, no assets, no fragment; the menu went with the nav
    for private in ("http://127.0.0.1/", "http://localhost:9090/", "file:///etc/hostname", "http://169.254.169.254/latest"):
        with pytest.raises(httpx.RequestError):
            content.read_public_page(private, 2)


# ── one unit table ──────────────────────────────────────────────────────────

def test_the_kitchens_measures_are_the_almanacs():
    from services.almanac import units
    from services.reader import kitchen
    assert "_units_from_the_almanac" in read("services", "reader", "kitchen.py") and "236.588" not in read("services", "reader", "kitchen.py")
    cup = units.find("cup")[0]
    assert kitchen.UNITS["cup"] == ("volume", cup.factor * 1e6) and kitchen.ML_PER_CUP == kitchen.UNITS["cup"][1]
    assert kitchen.UNITS["pound"] == ("weight", pytest.approx(453.59237)) and kitchen.UNITS["gram"] == ("weight", 1.0)
    assert kitchen.UNITS["teaspoon"][1] * 3 == pytest.approx(kitchen.UNITS["tablespoon"][1]) and kitchen.UNITS["tablespoon"][1] * 16 == pytest.approx(kitchen.UNITS["cup"][1])
    # the same answer either way: 2 cups in millilitres
    converted = units.convert("2 cups", "mL")
    assert kitchen.measure(2, "cup")["ml"] == pytest.approx(473.2, abs=0.05) and "473" in json.dumps(converted, default=str)
    assert kitchen.measure(1, "cup", "flour") == {"ml": 236.6, "grams": 120.0}


# ── one Scheduled list, one clock ───────────────────────────────────────────

def test_an_automations_hour_is_the_hour_on_the_house_clock():
    from src import jobs
    job = {"id": "j1", "enabled": True, "trigger": {"kind": "schedule", "schedule": {"kind": "cron", "cron": "0 7 * * *", "tz": "America/Chicago"}}}
    start = datetime(2031, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    nxt = datetime.fromtimestamp(jobs.compute_next_run(job, after=start), tz=timezone.utc)
    assert (nxt.hour, nxt.minute) == (12, 0) and nxt.day == 11                      # 7:00 in Chicago is 12:00 UTC in March — not 07:00 UTC
    local = dict(job, trigger={"kind": "schedule", "schedule": {"kind": "cron", "cron": "30 6 * * *"}})
    nxt = datetime.fromtimestamp(jobs.compute_next_run(local, after=time.time())).astimezone()
    assert (nxt.hour, nxt.minute) == (6, 30)                                        # with no zone kept: the server's own clock
    assert jobs.compute_next_run(dict(job, enabled=False)) is None
    assert jobs._zone("Not/AZone") is not None and "timezone.utc" not in read("src", "jobs.py").split("def compute_next_run")[1].split("def due_jobs")[0]


def test_scheduled_is_one_list():
    assert "fetch(`${API_BASE}/api/jobs`" in TASKS_JS and "job.trigger.kind === 'schedule' || job.trigger.kind === 'email'" in TASKS_JS
    assert "function _appendAutomations(list)" in TASKS_JS and TASKS_JS.count("_appendAutomations(list)") >= 3          # with tasks, and with none
    for piece in ("/api/jobs/${encodeURIComponent(job.id)}/toggle", "/api/jobs/${encodeURIComponent(job.id)}/run", "'Run now'", "the last run failed"):
        assert piece in TASKS_JS, piece
    assert ">Scheduled <span id=\"tasks-head-count\"" in TASKS_JS and "Ongoing Tasks" not in TASKS_JS
    assert "Everything that runs by itself" in TASKS_JS
    assert ".tasks-auto-row" in read("static", "style.css")


def test_an_automations_schedule_is_said_in_words(tmp_path):
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("node is not installed")
    start = TASKS_JS.index("function _autoScheduleLabel(job) {")
    body = TASKS_JS[start:TASKS_JS.index("\n}\n", start) + 3]
    script = tmp_path / "label.mjs"
    script.write_text(body + """
const cases = [[{trigger: {kind: 'email'}}, 'when new mail arrives'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'interval', interval_seconds: 7200}}}, 'every 2 hours'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'interval', interval_seconds: 86400}}}, 'every day'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'interval', interval_seconds: 900}}}, 'every 15 minutes'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'cron', cron: '0 7 * * *'}}}, 'at 07:00 every day'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'cron', cron: '30 18 * * 1-5'}}}, 'at 18:30 on weekdays'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'cron', cron: '0 9 * * 6'}}}, 'at 09:00 every Saturday'],
  [{trigger: {kind: 'schedule', schedule: {kind: 'cron', cron: '*/5 * * * *'}}}, 'on the schedule */5 * * * *']];
for (const [job, want] of cases) { const got = _autoScheduleLabel(job); if (got !== want) { console.log('WRONG', JSON.stringify(job), got); process.exit(1); } }
console.log('ok');
""", encoding="utf-8")
    done = __import__("subprocess").run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "ok", done.stdout + done.stderr


# ── the slash list ──────────────────────────────────────────────────────────

def _command(name):
    start = SLASH.index(f"\n  {name}: {{") if f"\n  {name}: {{" in SLASH else SLASH.index(f"\n  '{name}': {{")
    return SLASH[start:SLASH.index("\n  },", start)]


def test_older_names_skins_and_extra_doors_are_offered_nowhere():
    assert "PROMOTED_ALIASES" not in MENU and "LEGACY_ALIASES" not in MENU                 # no rows for older names
    assert "export const LEGACY_ALIASES = {" in SLASH and "'new':         { parent: 'chats', sub: 'new' }" in SLASH          # they still work when typed
    for name in ("code", "wiki", "canvas", "bonzi", "winamp", "win98", "toggle"):
        assert "hidden: true" in _command(name), name
    assert "if (def.hidden) continue;" in MENU
    assert "hidden: true" not in _command("codemode")                                     # Code mode is the one door


def test_an_administrators_command_is_neither_offered_to_nor_run_for_anyone_else():
    start = SLASH.index("export const ADMIN_COMMANDS = new Set([")
    listed = set(__import__("re").findall(r"'([a-z0-9-]+)'", SLASH[start:SLASH.index("]);", start)]))
    assert {"models", "engine", "shopper", "control", "automations", "tasks", "doctor", "mcp", "codemode", "library", "research", "theme", "skills"} <= listed
    assert not listed & {"chats", "help", "kitchen", "translate", "almanac", "notes", "image", "identify", "memory", "me", "note", "studio"}
    registry = SLASH[SLASH.index("const COMMANDS = {"):SLASH.index("export const LEGACY_ALIASES")]
    for name in listed:
        assert f"\n  {name}: {{" in registry or f"\n  '{name}': {{" in registry, f"{name} is not a command"
    assert "document.documentElement.dataset.admin === '1'" in SLASH
    assert "if (!admin && ADMIN_COMMANDS.has(name)) continue;" in MENU
    dispatch = SLASH[SLASH.index("async function handleSlashCommand(input)"):]
    guard, legacy = dispatch.index("ADMIN_COMMANDS.has(_adminKey) && !viewerIsAdmin()"), dispatch.index("// --- 2. Try legacy alias ---")
    assert guard < legacy and "That command is for whoever runs this server." in dispatch            # before an older name can reach it either


# ── finished work finds the person ──────────────────────────────────────────

@pytest.fixture
def told(monkeypatch):
    from services import notify
    said = []
    monkeypatch.setattr(notify, "tell_soon", lambda owner, title, body="", url="/home", tag=None, status="success": said.append((owner, title, body, url, status)))
    return said


def test_a_picture_or_video_that_took_a_while_tells_whoever_asked(told):
    from src import job_queue
    slow = job_queue.add("video", "a heron lifting off the marsh at dawn", owner="mia")
    job_queue.start(slow)
    job_queue._ENTRIES[slow]["started"] = time.time() - 300
    job_queue.finish(slow, "done", result_url="/api/generated/heron.mp4")
    assert told == [("mia", "Your video is ready", "a heron lifting off the marsh at dawn", "/api/generated/heron.mp4", "success")]
    job_queue.finish(slow, "done")                                   # finished twice: said once
    assert len(told) == 1
    quick = job_queue.add("image", "a fox", owner="mia")
    job_queue.start(quick)
    job_queue.finish(quick, "done", result_url="/api/generated/fox.png")
    assert len(told) == 1                                            # they watched it being made: nothing to say
    failed = job_queue.add("audio", "a slow song about rain", owner="dana")
    job_queue._ENTRIES[failed]["created"] = time.time() - 120
    job_queue.finish(failed, "error", error="out of memory")
    assert told[-1] == ("dana", "Your song did not finish", "a slow song about rain", "/home", "error")
    nobody = job_queue.add("video", "clip")
    job_queue._ENTRIES[nobody]["created"] = time.time() - 500
    job_queue.finish(nobody, "done")
    other = job_queue.add("recipe", "summarise the inbox", owner="mia")
    job_queue._ENTRIES[other]["created"] = time.time() - 500
    job_queue.finish(other, "done")
    stopped = job_queue.add("video", "clip", owner="mia")
    job_queue._ENTRIES[stopped]["created"] = time.time() - 500
    job_queue.cancel(stopped)
    job_queue.finish(stopped, "cancelled")
    assert len(told) == 2                                            # no owner, not media, cancelled by the person: silence


def test_a_scheduled_task_or_automation_reaches_the_phone_once(monkeypatch):
    from services import notify
    from src.task_scheduler import TaskScheduler
    pushed = []
    monkeypatch.setattr(notify, "push_soon", lambda owner, title, body="", url="/", tag=None: pushed.append((owner, title, body, url, tag)))
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._pending_notifications = []
    scheduler.add_notification("Morning digest", "success", "t1", owner="boss", body="Three  things\nto know.")
    scheduler.add_notification("Morning digest", "error", "t1", owner="boss", body="The model did not answer.")
    scheduler.add_notification("Nobody's task", "success", "t2")
    assert pushed == [("boss", "Finished: Morning digest", "Three things to know.", "/", "task-t1"),
                      ("boss", "Did not finish: Morning digest", "The model did not answer.", "/", "task-t1")]
    assert len(scheduler._pending_notifications) == 3                # an open screen hears all three
    # a task that keeps failing buzzes the phone once, not at every run; a success always does
    scheduler.add_notification("Morning digest", "error", "t1", owner="boss", body="The model did not answer.")
    scheduler.add_notification("Morning digest", "success", "t1", owner="boss")
    assert [title for _owner, title, *_rest in pushed] == ["Finished: Morning digest", "Did not finish: Morning digest", "Finished: Morning digest"]
    del pushed[2:]
    # a notification that came through notify.tell was pushed by it: the queue must not push it again
    scheduler.add_notification("Your report is ready", "success", "report-1", owner="mia", url="/api/research/report/1", push=False)
    assert len(pushed) == 2
    source = read("services", "notify.py")
    assert "scheduler.add_notification(title, status, task_id=tag, owner=owner or None, body=body, url=url, push=False)" in source
    assert "_notify.bind_loop(asyncio.get_running_loop())" in APP     # work finishing on a worker thread can still reach a phone


# ── the server looks at itself ──────────────────────────────────────────────

def test_the_nightly_look_reports_only_what_is_wrong(monkeypatch, tmp_path):
    from services import notify, selfcheck
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    results = [{"name": "The model server", "ok": True, "detail": ""}, {"name": "The library", "ok": False, "detail": "Its server does not answer (ConnectError)."},
               {"name": "Room on the data drive", "ok": True, "detail": "60 GB free."}]
    monkeypatch.setattr(selfcheck, "run", lambda: list(results))
    said = []

    async def tell_admins(title, body="", url="/admin", tag=None, but=None):
        said.append((title, body, url, tag))
        return 1
    monkeypatch.setattr(notify, "tell_admins", tell_admins)
    record = asyncio.run(selfcheck.nightly())
    assert record["problems"] == ["The library: Its server does not answer (ConnectError)."]
    assert said == [("The library needs a look", "The library: Its server does not answer (ConnectError).", "/admin#activity", "selfcheck")]
    assert selfcheck.last()["problems"] == record["problems"]          # kept for the administrator's page
    asyncio.run(selfcheck.nightly(tell=False))                         # asked for by hand: the asker is looking at it
    assert len(said) == 1
    results[1] = {"name": "The library", "ok": True, "detail": ""}
    assert asyncio.run(selfcheck.nightly())["problems"] == [] and len(said) == 1          # all well: silence
    results[0] = {"name": "The model server", "ok": False, "detail": "It does not answer (ConnectError)."}
    results[2] = {"name": "Room on the data drive", "ok": False, "detail": "Only 2 GB free of 900 GB."}
    asyncio.run(selfcheck.nightly())
    assert said[-1][0] == "The server needs a look" and "Only 2 GB free" in said[-1][1]


def test_what_the_nightly_look_checks(monkeypatch, tmp_path):
    from services import selfcheck
    low = selfcheck.room(str(tmp_path), "Room on the data drive")
    assert low["name"] == "Room on the data drive" and isinstance(low["ok"], bool) and "GB free" in low["detail"]
    usage = __import__("collections").namedtuple("usage", "total used free")
    monkeypatch.setattr(selfcheck.shutil, "disk_usage", lambda path: usage(1000 * 1024 ** 3, 998 * 1024 ** 3, 2 * 1024 ** 3))
    assert selfcheck.room("/x", "Room")["ok"] is False and selfcheck.room("/x", "Room")["detail"] == "Only 2 GB free of 1000 GB."
    monkeypatch.setattr(selfcheck.shutil, "disk_usage", lambda path: usage(100 * 1024 ** 3, 97 * 1024 ** 3, 3 * 1024 ** 3))
    assert selfcheck.room("/x", "Room")["ok"] is False                 # under 5 GB, and under 5%
    monkeypatch.setattr(selfcheck.shutil, "disk_usage", lambda path: usage(1000 * 1024 ** 3, 400 * 1024 ** 3, 600 * 1024 ** 3))
    assert selfcheck.room("/x", "Room") == {"name": "Room", "ok": True, "detail": "600 GB free."}
    assert selfcheck.problems([{"name": "A", "ok": True, "detail": "fine"}, {"name": "B", "ok": False, "detail": ""}]) == ["B: not well."]
    source = read("services", "selfcheck.py")
    # the model server is asked for its list only: never the probe that loads a model, never an outside provider
    assert '_get(base + "/models")' in source and "is_local_endpoint(url)" in source and "/probe" not in source
    assert "_selfcheck_nightly_loop" in APP and 'get_setting("selfcheck_nightly", True)' in APP
    routes = read("routes", "account_routes.py")
    assert '@router.get("/api/admin/selfcheck")' in routes and '@router.post("/api/admin/selfcheck")' in routes
    assert routes.count("_admin(request)\n        from services import selfcheck") == 2          # an administrator's, both of them


# ── a screen with no cards gets the card in words ───────────────────────────

def test_a_card_is_written_out_for_a_screen_that_does_not_draw_it():
    from services.cards import as_markdown
    trip = {"type": "trip", "title": "Saturday hike", "when": "Saturday, March 15", "place": "Fernhollow", "note": "",
            "times": [{"label": "Leave by", "value": "12:40 PM"}, {"label": "Turn back by", "value": "2:40 PM"}], "assumed": "That allows about 4 hours out and back.",
            "weather": {"summary": "Overcast", "tiles": [{"label": "High", "value": "63°"}], "alerts": []}, "no_forecast": "",
            "watch_for": [{"name": "Getting cold", "why": "a low of 47°F", "text": "Carry a dry layer.", "source": {"label": "Field Guide, p. 9", "url": "http://library.test/g#page=9"}},
                          {"name": "Ticks", "why": "mild weather", "text": "Check when home.", "source": None}],
            "list": {"items": ["Water", "Map"], "open": 5, "href": "/home#lists/a"}, "essentials": False, "reminder": "Friday, 7:00 PM"}
    written = as_markdown(trip)
    for piece in ("**Saturday hike** · Saturday, March 15 · Fernhollow", "Leave by **12:40 PM** · Turn back by **2:40 PM**", "Weather: Overcast, high 63°.",
                  "- **Getting cold** (a low of 47°F): Carry a dry layer. — [Field Guide, p. 9](http://library.test/g#page=9)", "- **Ticks** (mild weather): Check when home.",
                  "**What to pack**: Water, Map, and 3 more. [Open the packing list](/home#lists/a)", "Reminder set for Friday, 7:00 PM."):
        assert piece in written, piece
    recipe = as_markdown({"type": "recipe", "title": "Plain Pilau", "from": "The Hollow Kitchen Book", "needs": ["onion", "butter"], "href": "/kitchen?r=r1",
                          "source_url": "/read?u=x", "serves": "four", "time": ""})
    assert "It also needs: onion, butter." in recipe and "[Start cooking](/kitchen?r=r1) · [The page it is from](/read?u=x)" in recipe
    # an answer that already says everything in words gets nothing added
    assert as_markdown({"type": "weather", "summary": "Rain"}) == "" and as_markdown({"type": "recipe", "title": "x", "href": "/kitchen"}) == "" and as_markdown(None) == ""
    routes = read("routes", "chat_routes.py")
    assert '_draws_cards = str(form_data.get("screen") or "").lower() == "home"' in routes and routes.count("if not _draws_cards:") == 2
    assert "form.append('screen', 'home');" in read("static", "lite", "home.js")
