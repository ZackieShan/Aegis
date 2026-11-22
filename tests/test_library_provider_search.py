"""Offline library as a search provider and as the ``library_search`` agent tool.

Covers the routing rule that makes web search keep working with the internet
down, the merged article + manual ranking, and the tool's registration and
output contract. No network: the library, the internet probe and the web
providers are all stubbed.
"""
import asyncio
import json

import pytest

import services.library.search as lib_search
import services.search.core as core
import services.search.netstate as netstate
from services import library
from services.library import config, kiwix, manuals


@pytest.fixture
def library_on(monkeypatch):
    monkeypatch.delenv("AEGIS_LIBRARY_DISABLED", raising=False)
    monkeypatch.setattr(config, "_settings", lambda: {
        "library_enabled": True,
        "kiwix_url": "http://127.0.0.1:8095",
        "kiwix_public_url": "https://box.example.ts.net:8443",
    })
    kiwix.reset_cache()
    yield
    kiwix.reset_cache()


def _article(title, snippet="", book="wikipedia_en_all_maxi"):
    slug = title.replace(" ", "_")
    return {"title": title, "path": f"/content/{book}/{slug}",
            "url": f"https://box.example.ts.net:8443/content/{book}/{slug}",
            "snippet": snippet, "book": book, "book_title": "Wikipedia", "words": 100}


def _manual(title, page, snippet=""):
    return {"kind": "manual", "title": f"{title} — page {page}", "doc_title": title,
            "url": f"https://box.example.ts.net:8443/content/armypubs_en_all/x.pdf#page={page}",
            "snippet": snippet, "book": "armypubs_en_all", "page": page, "pages": 300, "doc_id": 1}


# ── provider chain ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ready,online,primary,expected", [
    (False, True, "searxng", ["searxng", "duckduckgo"]),           # no library: untouched
    (True, True, "searxng", ["searxng", "duckduckgo", "kiwix"]),   # library is the last resort
    (True, False, "searxng", ["kiwix"]),                           # offline: straight to it
    (True, True, "kiwix", ["kiwix", "duckduckgo"]),                # user put it first
    (False, True, "kiwix", ["duckduckgo"]),                        # chosen but not running
])
def test_effective_provider_chain(monkeypatch, ready, online, primary, expected):
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_fallback_chain": ["duckduckgo"]})
    monkeypatch.setattr(core, "_library_ready", lambda: ready)
    monkeypatch.setattr(core, "_internet_up", lambda: online)
    assert core._effective_provider_chain(primary) == expected


def test_library_is_off_for_the_test_session_by_default():
    # conftest sets AEGIS_LIBRARY_DISABLED so no test reaches a real kiwix-serve.
    assert config.enabled() is False
    assert core._library_ready() is False


def test_internet_probe_env_overrides_and_cache(monkeypatch):
    netstate.reset()
    monkeypatch.setenv("AEGIS_ASSUME_OFFLINE", "1")
    assert netstate.internet_available() is False
    monkeypatch.delenv("AEGIS_ASSUME_OFFLINE")
    monkeypatch.setenv("AEGIS_ASSUME_ONLINE", "1")
    assert netstate.internet_available() is True
    monkeypatch.delenv("AEGIS_ASSUME_ONLINE")

    calls = []
    monkeypatch.setattr(netstate, "_can_connect", lambda target: calls.append(target) or False)
    netstate.reset()
    assert netstate.internet_available() is False
    first = len(calls)
    assert first >= 1
    assert netstate.internet_available() is False           # cached: no new probes
    assert len(calls) == first
    monkeypatch.setattr(netstate, "_can_connect", lambda target: True)
    assert netstate.internet_available(force=True) is True
    netstate.reset()


def test_probe_targets_can_be_overridden(monkeypatch):
    monkeypatch.setenv("AEGIS_NET_PROBES", "10.0.0.1:53, [fd00::1]:443, nonsense")
    assert netstate._probes() == [("10.0.0.1", 53), ("fd00::1", 443)]


# ── comprehensive_web_search through the library ───────────────────────────

def test_web_search_answers_from_the_library_when_offline(monkeypatch):
    hit = {"title": "ATP 3-50.21 — Survival — page 44",
           "url": "https://box.example.ts.net:8443/content/armypubs_en_all/x.pdf#page=44",
           "snippet": "[Army Publishing Directorate] purify water with iodine", "source": "library", "kind": "manual"}
    called = []
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)
    monkeypatch.setattr(core, "_effective_provider_chain", lambda primary: ["kiwix"])
    monkeypatch.setattr(core, "_call_provider", lambda name, q, n, tf=None: called.append(name) or [hit])
    monkeypatch.setattr(core, "rank_search_results",
                        lambda q, r: (_ for _ in ()).throw(AssertionError("library hits must keep their order")))
    monkeypatch.setattr(core, "fetch_webpage_content",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("library links must not use the web fetcher")))
    monkeypatch.setattr(library, "owns_url", lambda url: True)
    monkeypatch.setattr(library, "fetch_content", lambda url, query="", budget=0: {
        "url": url, "title": "ATP 3-50.21 — Survival — page 44",
        "content": "[page 44 of 300]\nTwo tablets per quart canteen; wait 30 minutes.", "success": True, "error": ""})

    text, sources = core.comprehensive_web_search("purify water iodine", max_pages=3, return_sources=True)
    assert called == ["kiwix"]
    assert "OFFLINE LIBRARY RESULTS" in text and "the internet is unreachable" in text
    assert "Two tablets per quart canteen" in text
    assert "[CONTENT 1]" in text
    assert sources == [{"url": hit["url"], "title": hit["title"]}]


def test_offline_with_no_matches_says_so_plainly(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)
    monkeypatch.setattr(core, "_effective_provider_chain", lambda primary: ["kiwix"])
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [])
    message, sources = core.comprehensive_web_search("zzqqxx", return_sources=True)
    assert sources == []
    assert "internet is unreachable" in message and "fewer, more specific words" in message


def test_library_results_are_not_written_to_the_web_search_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "SEARCH_CACHE_DIR", tmp_path)
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)
    monkeypatch.setattr(core, "_effective_provider_chain", lambda primary: ["kiwix"])
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [{"title": "T", "url": "u", "snippet": "s"}])
    monkeypatch.setattr(core, "_record_query", lambda *a, **k: None)
    assert core.searxng_search_results("anything")[0]["title"] == "T"
    assert list(tmp_path.iterdir()) == []


def test_kiwix_provider_maps_library_hits(monkeypatch):
    from services.search import providers

    monkeypatch.setattr(library, "library_search", lambda q, count=8: [
        dict(_article("Tourniquet", "a device"), kind="article"), _manual("First Aid", 46, "apply above the wound")])
    results = providers.kiwix_search("tourniquet", 5)
    assert [r["title"] for r in results] == ["Tourniquet", "First Aid — page 46"]
    assert results[0]["snippet"].startswith("[Wikipedia] a device")
    assert results[1]["kind"] == "manual" and results[1]["source"] == "library"
    assert "kiwix" in providers.PROVIDER_INFO

    def boom(*a, **k):
        raise RuntimeError("index locked")

    monkeypatch.setattr(library, "library_search", boom)
    assert providers.kiwix_search("x", 5) == []


# ── merged library search ──────────────────────────────────────────────────

def test_library_search_merges_ranks_and_dedupes(library_on, monkeypatch):
    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: [
        {"id": "wikipedia_en_all_maxi", "title": "Wikipedia", "articles": 7_000_000, "media": 0}])
    monkeypatch.setattr(kiwix, "suggest", lambda book, term, count=5: [
        {"title": "Hypothermia", "path": "/content/wikipedia_en_all_maxi/Hypothermia",
         "url": "https://box.example.ts.net:8443/content/wikipedia_en_all_maxi/Hypothermia", "book": book},
        {"title": "Hypothermia (album)", "path": "/x", "url": "https://box.example.ts.net:8443/content/w/x", "book": book},
    ])
    monkeypatch.setattr(kiwix, "search", lambda q, count=8, books=None: [
        _article("J wave", "an ECG finding seen in hypothermia"),
        _article("Hypothermia", "signs include shivering; treat by rewarming"),
        _article("Tourniquet (band)", "hypothermia is a song"),
    ])
    monkeypatch.setattr(manuals, "search", lambda q, count=8: [
        _manual("First Aid", 120, "treat hypothermia: signs are shivering and confusion")])

    results = library.library_search("hypothermia", count=6)
    titles = [r["title"] for r in results]
    assert titles[0] == "Hypothermia"                       # exact title first, once
    assert titles.count("Hypothermia") == 1
    assert "Hypothermia (album)" not in titles              # pop-culture title match dropped
    assert titles.index("First Aid — page 120") < titles.index("Tourniquet (band)")
    assert all("_rank" not in r and "_cov" not in r for r in results)
    assert results[titles.index("First Aid — page 120")]["book_title"] == "armypubs_en_all"

    assert [r["kind"] for r in library.library_search("hypothermia", scope="manuals")] == ["manual"]
    assert library.library_search("", count=5) == []


def test_relaxed_search_ladder_retries_with_fewer_words(library_on, monkeypatch):
    asked = []

    def fake_search(query, count=8, books=None):
        asked.append(query)
        if query == "chanterelle jack lantern":             # one word dropped
            return [_article("Omphalotus olearius", "jack-o'-lantern mushroom")]
        return []

    monkeypatch.setattr(kiwix, "search", fake_search)
    hits = lib_search._search_relaxed("chanterelle lookalikes jack lantern", 5, None)
    assert [h["title"] for h in hits] == ["Omphalotus olearius"]
    assert asked[0] == "chanterelle lookalikes jack lantern"
    assert "chanterelle jack lantern" in asked


def test_format_for_model_fits_the_callers_cap(library_on, monkeypatch):
    results = [dict(_article(f"Article {i}", "snippet " * 20), kind="article") for i in range(8)]
    monkeypatch.setattr(lib_search, "fetch_content", lambda url, query="", budget=4000: {
        "url": url, "title": "T", "content": "x" * budget, "success": True, "error": ""})
    text, sources = library.format_for_model("anything", results, fetch=3, budget=4000, max_chars=6000)
    assert len(text) <= 6000
    assert text.rstrip().endswith("not the whole page.")     # closing rules intact
    assert len(sources) == 8 and "[CONTENT 1]" in text

    text, sources = library.format_for_model("nothing", [], fetch=3)
    assert "No matches in the offline library" in text and sources == []


# ── agent tool ─────────────────────────────────────────────────────────────

def test_library_search_is_registered_everywhere():
    from src.agent_loop import TOOL_SECTIONS
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_policy import WEB_TOOL_NAMES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS

    name = "library_search"
    assert name in TOOL_HANDLERS and name in TOOL_TAGS
    assert name in BUILTIN_TOOL_DESCRIPTIONS and name in TOOL_SECTIONS
    schema = next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == name)
    assert schema["parameters"]["required"] == ["query"]
    assert name in PLAN_MODE_READONLY_TOOLS              # read-only: fine in plan mode
    assert name not in NON_ADMIN_BLOCKED_TOOLS
    assert name not in WEB_TOOL_NAMES                    # must survive the web toggle being off
    block = function_call_to_tool_block(name, json.dumps({"query": "bowline", "scope": "manuals"}))
    assert block.tool_type == name and json.loads(block.content)["scope"] == "manuals"
    assert function_call_to_tool_block(name, json.dumps({"query": ""})) is None


def test_library_tool_output_contract(library_on, monkeypatch):
    from src.agent_tools import TOOL_HANDLERS
    from src.constants import MAX_OUTPUT_CHARS

    seen = {}

    def fake_search(query, count=8, scope="all"):
        seen.update(query=query, count=count, scope=scope)
        return [dict(_article("Bowline", "a knot"), kind="article")]

    monkeypatch.setattr(library, "library_search", fake_search)
    monkeypatch.setattr(lib_search, "fetch_content", lambda url, query="", budget=4000: {
        "url": url, "title": "Bowline", "content": "y" * 20000, "success": True, "error": ""})
    progress = []

    async def run(content):
        async def cb(event):
            progress.append(event["tail"])
        return await TOOL_HANDLERS["library_search"](content, {"progress_cb": cb})

    result = asyncio.run(run(json.dumps({"query": "bowline knot", "scope": "manuals", "max_results": 4})))
    assert seen == {"query": "bowline knot", "count": 4, "scope": "manuals"}
    assert result["exit_code"] == 0 and progress and "bowline knot" in progress[0]
    body, marker = result["output"].split("\n\n<!-- SOURCES:")
    assert len(body) <= MAX_OUTPUT_CHARS and "END OF OFFLINE LIBRARY RESULTS" in body
    assert json.loads(marker.removesuffix(" -->"))[0]["title"] == "Bowline"

    plain = asyncio.run(run("bowline"))                   # bare text works too
    assert plain["exit_code"] == 0 and seen["query"] == "bowline" and seen["scope"] == "all"
    assert asyncio.run(run(""))["exit_code"] == 1
    assert asyncio.run(run('{"scope": "manuals"}'))["exit_code"] == 1

    monkeypatch.setenv("AEGIS_LIBRARY_DISABLED", "1")
    off = asyncio.run(run("bowline"))
    assert off["exit_code"] == 1 and "switched off" in off["error"]


def test_tool_is_offered_alongside_web_search_only_when_a_library_exists(monkeypatch):
    import src.tool_index as tool_index

    index = tool_index.ToolIndex.__new__(tool_index.ToolIndex)
    monkeypatch.setattr(tool_index.ToolIndex, "retrieve", lambda self, query, k=8: ["web_search"])
    monkeypatch.setattr(tool_index, "_library_ready", lambda: False)
    assert "library_search" not in index.get_tools_for_query("who won the game last night")
    monkeypatch.setattr(tool_index, "_library_ready", lambda: True)
    assert "library_search" in index.get_tools_for_query("who won the game last night")
    # Plain questions get it too (retrieval is poor at spotting them) —
    # tasks and small talk do not.
    monkeypatch.setattr(tool_index.ToolIndex, "retrieve", lambda self, query, k=8: [])
    assert "library_search" in index.get_tools_for_query("how do I treat a copperhead bite")
    assert "library_search" in index.get_tools_for_query("pokeweed berries, safe to eat?")
    assert "library_search" not in index.get_tools_for_query("rename these files for me")
    assert "library_search" not in index.get_tools_for_query("hi")
    # Keyword intent works with or without a library probe saying yes.
    monkeypatch.setattr(tool_index, "_library_ready", lambda: False)
    assert "library_search" not in index.get_tools_for_query("how do I treat a copperhead bite")
    assert "library_search" in index.get_tools_for_query("what does the field manual say about first aid")


def test_the_same_wikipedia_article_from_three_collections_is_listed_once(library_on, monkeypatch):
    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: [])
    monkeypatch.setattr(kiwix, "suggest", lambda book, term, count=5: [])
    monkeypatch.setattr(kiwix, "search", lambda q, count=8, books=None: [
        _article("Emergency tourniquet", "a device", book="wikipedia_en_medicine_maxi"),
        _article("Emergency tourniquet", "a device", book="mdwiki_en_all_maxi"),
        _article("Emergency tourniquet", "a question", book="outdoors.stackexchange.com_en_all"),
        _article("Emergency tourniquet", "a device", book="wikipedia_en_all_maxi"),
    ])
    monkeypatch.setattr(manuals, "search", lambda q, count=8: [])
    results = library.library_search("emergency tourniquet", count=6)
    assert [r["book"] for r in results] == ["wikipedia_en_medicine_maxi", "outdoors.stackexchange.com_en_all"]
