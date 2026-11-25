"""Connected services: the keys the house keeps for outside services, and what is done with them.

What must hold:
  * a key typed in by an administrator is kept encrypted, in a file only the app can read, and no answer
    from the server ever carries it again — only whether it is set and its last four characters;
  * one set of keys serves every account; only an administrator can see, change, try or remove them;
  * a key saved here replaces the plain copy the settings file used to hold;
  * a paid service is counted by the request and is not asked again once the month's limit is reached —
    out of the box that limit is what the service gives away, so nothing is spent unasked;
  * with Brave as the search provider a search brings back the passages that answer it in one request,
    numbered like its sources, and the ordinary search takes over whenever Brave cannot be asked;
  * the look-up tools (news, places, medicine labels, studies, Wolfram|Alpha) say plainly when their
    service cannot answer and tell the assistant not to fill the gap from memory; a tool whose service
    is not set up is not offered at all;
  * nothing here reaches the network or the real keys: every outside answer below is made up.

Names of medicines, shops, towns, people and papers are invented.
"""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services import connections
from services.connections import catalog, checks, http, store
from services.connections.http import Problem
from services.lookups import medicines, news, papers, places, wolfram
from services.search import brave

REPO = Path(__file__).resolve().parent.parent
KEY = "made-up-brave-key-0123456789abcd"


@pytest.fixture(autouse=True)
def own_store(tmp_path, monkeypatch):
    """A key store, a usage count, a settings file and an app key in a scratch folder; a network that is never reached."""
    import requests
    import src.constants as constants
    import src.secret_storage as secrets
    import src.settings as settings

    monkeypatch.delenv("AEGIS_CONNECTIONS_DISABLED", raising=False)
    for name in ("DATA_BRAVE_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY", "GOOGLE_API_KEY", "GOOGLE_PSE_CX",
                 "OPENFDA_API_KEY", "OPENALEX_API_KEY", "WOLFRAM_APP_ID", "WOLFRAM_ALPHA_APPID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AEGIS_CONNECTIONS_FILE", str(tmp_path / "connections.json"))
    monkeypatch.setenv("AEGIS_CONNECTIONS_USAGE_FILE", str(tmp_path / "connections_usage.json"))
    monkeypatch.setattr(secrets, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets, "_fernet", None)
    monkeypatch.setattr(constants, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings._invalidate_caches()

    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(requests, "get", no_network)
    store.forget_cache()
    brave.forget()
    yield tmp_path
    store.forget_cache()
    brave.forget()
    settings._invalidate_caches()


class Answers:
    """Stands in for ``http.get``: hands back made-up answers by address and keeps what was asked."""

    def __init__(self, monkeypatch, by_address):
        self.by_address = by_address
        self.asked = []
        monkeypatch.setattr(http, "get", self)

    def __call__(self, url, params=None, headers=None, timeout=12.0, name="The service", want="json", keep_headers=None):
        self.asked.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        for part, answer in self.by_address.items():
            if part in url:
                found = answer(params or {}) if callable(answer) else answer
                if isinstance(found, Exception):
                    raise found
                return found
        raise AssertionError(f"nothing made up for {url}")

    def to(self, part):
        return [one for one in self.asked if part in one["url"]]


def write_settings(folder, values):
    import src.settings as settings
    (folder / "settings.json").write_text(json.dumps(values))
    settings._invalidate_caches()


def read_settings(folder):
    return json.loads((folder / "settings.json").read_text())


# ── the key store ──────────────────────────────────────────────────────────

def test_a_key_is_kept_encrypted_and_never_handed_back(own_store):
    store.save("brave", {"api_key": KEY})
    on_disk = (own_store / "connections.json").read_text()
    assert KEY not in on_disk and '"enc:' in on_disk
    assert stat.S_IMODE(os.stat(own_store / "connections.json").st_mode) == 0o600
    assert store.value("brave") == KEY and connections.has_key("brave") and connections.ready("brave")
    shown = json.dumps(store.overview())
    assert KEY not in shown
    field = next(s for s in store.overview()["services"] if s["id"] == "brave")["fields"][0]
    assert field == {"name": "api_key", "label": "API key", "secret": True, "optional": False, "placeholder": "BSA…", "set": True,
                     "shown": "••••••abcd", "from": "here"}
    store.forget("brave")
    assert store.value("brave") == "" and not connections.ready("brave")


def test_a_short_secret_is_not_shown_at_all(own_store):
    store.save("wolfram", {"app_id": "AB12-CD34"})
    field = store.describe(catalog.get("wolfram"))["fields"][0]
    assert field["set"] is True and field["shown"] == "••••••"


def test_the_screen_first_then_the_settings_file_then_the_environment(own_store, monkeypatch):
    assert store.value("brave") == "" and store.where("brave") == ""
    monkeypatch.setenv("BRAVE_API_KEY", "from-the-environment")
    assert store.value("brave") == "from-the-environment" and store.where("brave") == "environment"
    write_settings(own_store, {"brave_api_key": "from-the-settings-file"})
    assert store.value("brave") == "from-the-settings-file" and store.where("brave") == "settings"
    store.save("brave", {"api_key": KEY})
    assert store.value("brave") == KEY and store.where("brave") == "here"


def test_saving_a_key_here_blanks_the_plain_copy_in_the_settings_file(own_store):
    write_settings(own_store, {"brave_api_key": "plain-old-copy", "search_provider": "brave", "tavily_api_key": "another"})
    store.save("brave", {"api_key": KEY})
    assert read_settings(own_store) == {"brave_api_key": "", "search_provider": "brave", "tavily_api_key": "another"}
    store.save("brave", on=False)                                    # a switch alone leaves the settings file be
    assert read_settings(own_store)["tavily_api_key"] == "another"
    store.forget("tavily")
    assert read_settings(own_store)["tavily_api_key"] == ""


def test_the_search_providers_read_their_keys_from_the_store(own_store):
    from services.search import providers
    assert providers._get_provider_key("brave") == ""
    store.save("brave", {"api_key": KEY})
    store.save("google_pse", {"api_key": "pse-key-0123456789", "cx": "engine-id-42"})
    assert providers._get_provider_key("brave") == KEY
    assert providers._get_provider_key("google_pse") == "pse-key-0123456789" and providers._kept_for("google_pse", "cx") == "engine-id-42"
    plain = json.loads((own_store / "connections.json").read_text())["services"]["google_pse"]["fields"]
    assert plain["cx"] == "engine-id-42" and plain["api_key"].startswith("enc:")          # an id is not a secret


def test_what_cannot_be_kept_is_refused_in_words(own_store):
    for bad, words in ((("nowhere", {"api_key": "x"}), "not a service"), (("brave", {"token": "x"}), "no field called token"),
                       (("brave", {"api_key": "two words"}), "spaces or line breaks"), (("brave", {"api_key": "x" * 600}), "too long")):
        with pytest.raises(connections.Refused, match=words):
            store.save(*bad)
    with pytest.raises(connections.Refused, match="no limit to set"):
        store.save("medicines", limit=10)
    with pytest.raises(connections.Refused, match="whole number"):
        store.save("brave", limit="lots")
    assert not (own_store / "connections.json").exists()


def test_switched_off_means_not_ready_and_a_keyless_service_is_ready_as_it_comes(own_store):
    assert connections.ready("medicines") and connections.ready("papers")                # they need no key
    assert not connections.ready("brave") and not connections.ready("wolfram")           # they do
    store.save("medicines", on=False)
    assert not connections.ready("medicines") and connections.ready("papers")
    store.save("medicines", on=True)
    assert connections.ready("medicines")


def test_the_test_suites_switch_hides_every_key_and_refuses_every_request(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    monkeypatch.setenv("BRAVE_API_KEY", "from-the-environment")
    monkeypatch.setenv("AEGIS_CONNECTIONS_DISABLED", "1")
    store.forget_cache()
    assert store.value("brave") == "" and not connections.ready("brave") and not connections.on("medicines")
    with pytest.raises(Problem) as refused:
        http.get("https://example.invalid/")
    assert refused.value.reason == "off"
    with pytest.raises(RuntimeError):
        store.save("brave", {"api_key": KEY})


# ── counting what is paid for ──────────────────────────────────────────────

def test_a_paid_service_is_counted_and_stops_at_the_months_limit(own_store):
    store.save("brave", {"api_key": KEY})
    assert connections.limit("brave") == 1000                        # what Brave gives away: nothing is spent unasked
    assert connections.limit("medicines") == 0
    store.save("brave", limit=3)
    for kind in ("web", "news", "web"):
        assert connections.usable("brave")
        connections.count("brave", kind)
    assert connections.used("brave")["requests"] == 3 and connections.used("brave")["kinds"] == {"web": 2, "news": 1}
    assert connections.over_limit("brave") and connections.ready("brave") and not connections.usable("brave")
    store.save("brave", limit=0)                                     # 0 is "no limit"
    assert not connections.over_limit("brave")
    assert stat.S_IMODE(os.stat(own_store / "connections_usage.json").st_mode) == 0o600


def test_last_months_use_does_not_count_against_this_month(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY}, limit=2)
    monkeypatch.setattr(store, "_month", lambda when=None: "2031-03")
    connections.count("brave", "web", 2)
    assert connections.over_limit("brave")
    monkeypatch.setattr(store, "_month", lambda when=None: "2031-04")
    assert connections.used("brave")["requests"] == 0 and not connections.over_limit("brave")


def test_the_cost_is_what_goes_beyond_the_free_requests(own_store):
    store.save("brave", {"api_key": KEY}, limit=0)
    connections.count("brave", "web", 900)
    assert store.cost_so_far("brave") == 0.0
    connections.count("brave", "web", 500)                           # 1,400 in all: 400 paid for at $5 a thousand
    assert store.cost_so_far("brave") == 2.0
    seen = store.describe(catalog.get("brave"))["metered"]
    assert (seen["requests"], seen["free"], seen["cost"], seen["limit"], seen["over"]) == (1400, 1000, 2.0, 0, False)


# ── asking an outside service ──────────────────────────────────────────────

class Reply:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = text if text is not None else json.dumps(body if body is not None else {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("reply, reason, words", [
    (Reply(401, {"error": {"detail": "The token is invalid."}}), "key", "did not accept the key. It said: The token is invalid."),
    (Reply(402, {}), "limit", "no credit left"),
    (Reply(429, {}), "busy", "slow down"),
    (Reply(503, None, "<html>down</html>"), "offline", "having trouble"),
    (Reply(400, {"message": "Query too long"}), "refused", "would not answer that: Query too long"),
    (Reply(200, None, "<html>not json</html>"), "offline", "could not be read"),
])
def test_every_way_a_request_fails_becomes_words_and_a_reason(monkeypatch, reply, reason, words):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: reply)
    with pytest.raises(Problem) as problem:
        http.get("https://example.invalid/x", name="The service")
    assert problem.value.reason == reason and words in str(problem.value) and problem.value.status == reply.status_code


def test_a_request_that_cannot_connect_or_times_out_is_offline(monkeypatch):
    import requests

    def refuse(*a, **k):
        raise requests.ConnectionError("no route")

    def stall(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(requests, "get", refuse)
    with pytest.raises(Problem, match="could not be reached") as problem:
        http.get("https://example.invalid/x")
    assert problem.value.reason == "offline"
    monkeypatch.setattr(requests, "get", stall)
    with pytest.raises(Problem, match="did not answer in time"):
        http.get("https://example.invalid/x")


def test_a_good_answer_comes_back_as_json_or_text_with_its_headers(monkeypatch):
    import requests
    sent = {}

    def answer(url, params=None, headers=None, timeout=None):
        sent.update(url=url, params=params, headers=headers)
        return Reply(200, {"ok": True}, headers={"X-RateLimit-Remaining": "49"})

    monkeypatch.setattr(requests, "get", answer)
    kept = {}
    assert http.get("https://example.invalid/x", params={"q": "tea"}, headers={"X-Key": "k"}, keep_headers=kept) == {"ok": True}
    assert kept == {"x-ratelimit-remaining": "49"} and sent["headers"]["X-Key"] == "k" and sent["headers"]["Accept"] == "application/json"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Reply(200, None, "42"))
    assert http.get("https://example.invalid/x", want="text") == "42"


# ── Brave ──────────────────────────────────────────────────────────────────

WEB = {"web": {"results": [
    {"title": "How <strong>kettles</strong> work", "url": "https://kettles.example/how", "description": "A <strong>kettle</strong> boils water &amp; switches off.",
     "age": "March 3, 2031", "extra_snippets": ["The switch is a bimetal strip.", "Steam trips it."]},
    {"title": "No address", "url": "", "description": "dropped"},
    {"title": "Kettle history", "url": "https://history.example/kettle", "description": "Since 1891.", "page_age": "2030-01-05T10:00:00"},
]}}
PASSAGES = {
    "grounding": {"generic": [
        {"url": "https://kettles.example/how", "title": "How kettles work", "snippets": ["A kettle boils water with an element.", "A bimetal strip cuts the power."]},
        {"url": "https://history.example/kettle", "title": "Kettle history", "snippets": ["The electric kettle dates from 1891."]},
        {"url": "https://empty.example/", "title": "Nothing", "snippets": []},
    ], "map": []},
    "sources": {
        "https://kettles.example/how": {"title": "How kettles work", "hostname": "kettles.example",
                                        "age": ["Monday, March 3, 2031", "2031-03-03", "22 days ago", "2031-03-03T09:00:00Z"], "snippet": "A <strong>kettle</strong> boils water."},
        "https://history.example/kettle": {"title": "Kettle history", "hostname": "history.example", "age": []},
    },
}


def test_brave_cannot_be_asked_without_a_key_when_off_or_over_the_limit(own_store):
    assert brave.why_not().reason == "key" and not brave.ready()
    store.save("brave", {"api_key": KEY})
    assert brave.why_not() is None and brave.ready()
    store.save("brave", on=False)
    assert brave.why_not().reason == "off"
    store.save("brave", on=True, limit=1)
    connections.count("brave", "web")
    problem = brave.why_not()
    assert problem.reason == "limit" and "limit of 1 Brave Search requests is used up" in str(problem)


def test_a_brave_search_sends_the_key_in_a_header_and_reads_the_results(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    write_settings(own_store, {"search_safesearch": "off"})
    net = Answers(monkeypatch, {"/web/search": WEB})
    results = brave.web("how do kettles work", 5, "week")
    assert results == [
        {"title": "How kettles work", "url": "https://kettles.example/how", "snippet": "A kettle boils water & switches off.", "age": "March 3, 2031"},
        {"title": "Kettle history", "url": "https://history.example/kettle", "snippet": "Since 1891.", "age": "2030-01-05T10:00:00"},
    ]
    asked = net.asked[0]
    assert asked["headers"] == {"X-Subscription-Token": KEY}
    assert asked["params"] == {"q": "how do kettles work", "count": 5, "safesearch": "off", "freshness": "pw"}
    assert KEY not in json.dumps(asked["params"]) and KEY not in asked["url"]
    assert connections.used("brave") == {"requests": 1, "kinds": {"web": 1}, "last": connections.used("brave")["last"]}


def test_the_same_question_again_is_answered_from_memory_and_costs_nothing(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/web/search": WEB})
    first = brave.web("kettles", 5)
    assert brave.web("kettles", 5) == first and len(net.asked) == 1 and connections.used("brave")["requests"] == 1
    brave.web("kettles", 5, "day")                                   # a different question is a new request
    assert len(net.asked) == 2 and connections.used("brave")["requests"] == 2


def test_a_failed_request_is_not_counted_and_the_provider_gives_way(own_store, monkeypatch):
    from services.search import providers
    store.save("brave", {"api_key": KEY})
    Answers(monkeypatch, {"/web/search": Problem("Brave Search asked us to slow down.", "busy", 429)})
    assert providers.brave_search("kettles", 5) == []                # an empty list: the chain moves on
    assert connections.used("brave")["requests"] == 0


def test_a_long_question_is_cut_to_what_brave_takes(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/web/search": WEB})
    brave.web(" ".join(["word"] * 90), 3)
    assert len(net.asked[0]["params"]["q"].split()) == 50
    brave.web("x" * 900, 3)
    assert len(net.asked[1]["params"]["q"]) == 400


def test_passages_come_back_in_braves_order_with_their_pages(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/llm/context": PASSAGES})
    results, texts = brave.passages("how do kettles work", "month", urls=4, tokens=2048)
    assert [r["url"] for r in results] == ["https://kettles.example/how", "https://history.example/kettle"]       # the page with no passages is left out
    assert results[0] == {"title": "How kettles work", "url": "https://kettles.example/how", "snippet": "A kettle boils water.", "age": "2031-03-03 (22 days ago)"}
    assert results[1]["snippet"] == "The electric kettle dates from 1891." and results[1]["age"] == ""
    assert texts["https://kettles.example/how"] == ["A kettle boils water with an element.", "A bimetal strip cuts the power."]
    params = net.asked[0]["params"]
    assert (params["maximum_number_of_urls"], params["maximum_number_of_tokens"], params["freshness"]) == (4, 2048, "pm")
    assert connections.used("brave")["kinds"] == {"passages": 1}


def test_a_plan_without_passages_is_asked_once_not_every_time(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/llm/context": Problem("Brave Search would not answer that: The option is not included in the plan.", "refused", 400)})
    assert brave.passages("kettles") == ([], {})
    assert brave.passages("teapots") == ([], {}) and len(net.asked) == 1
    brave.forget()                                                    # a new key may be on a better plan
    brave.passages("teapots")
    assert len(net.asked) == 2


PLACES = {"locations": {"results": [
    {"title": "Far Hardware", "description": "Hardware store", "coordinates": [35.30, -97.10], "postal_address": {"displayAddress": "9 Mill Rd, Elmford, OK 73000"},
     "contact": {"telephone": "+15555550102"}, "url": "https://far.example/store?utm_source=listing&id=7", "timezone": "America/Chicago"},
    {"title": "Corner <strong>Hardware</strong>", "description": "Hardware store", "coordinates": [35.01, -97.01],
     "postal_address": {"displayAddress": "1 Main St, Elmford, OK 73000"}, "contact": {"telephone": "+15555550101"},
     "opening_hours": {"current_day": [{"abbr_name": "Tue", "full_name": "Tuesday", "opens": "09:00", "closes": "18:00"}],
                       "days": [[{"abbr_name": "Mon", "full_name": "Monday", "opens": "09:00", "closes": "18:00"}],
                                [{"abbr_name": "Wed", "full_name": "Wednesday", "opens": "09:00", "closes": "12:00"},
                                 {"abbr_name": "Wed", "full_name": "Wednesday", "opens": "13:00", "closes": "18:00"}]]},
     "rating": {"ratingValue": 4.6, "reviewCount": 212}, "price_range": "$$", "url": "https://corner.example/", "timezone": "America/Chicago",
     "serves_cuisine": []},
    {"title": "", "description": "nameless"},
]}}


def test_places_are_read_with_todays_hours_and_put_nearest_first(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/web/search": PLACES})
    spots = brave.places("hardware store", near="Elmford, Oklahoma", position=(35.0, -97.0), count=5)
    assert [spot["name"] for spot in spots] == ["Corner Hardware", "Far Hardware"]
    corner = spots[0]
    assert corner["address"] == "1 Main St, Elmford, OK 73000" and corner["phone"] == "+15555550101" and corner["price"] == "$$"
    assert corner["today"] == "09:00–18:00"
    assert corner["week"] == {"Monday": "09:00–18:00", "Wednesday": "09:00–12:00, 13:00–18:00", "Tuesday": "09:00–18:00"}     # today is among them
    assert corner["rating"] == 4.6 and corner["reviews"] == 212 and 0.8 < corner["miles"] < 1.0
    assert spots[1]["website"] == "https://far.example/store" and spots[1]["today"] == "" and spots[1]["miles"] > 20
    asked = net.asked[0]
    assert asked["params"]["q"] == "hardware store near Elmford, Oklahoma" and asked["params"]["result_filter"] == "locations,web"
    assert asked["headers"]["X-Loc-Lat"] == "35.0000" and asked["headers"]["X-Loc-Long"] == "-97.0000"
    assert connections.used("brave")["kinds"] == {"places": 1}


# ── a search that brings back its passages ─────────────────────────────────

@pytest.fixture
def searching(own_store, monkeypatch):
    """The main search with Brave as its provider and nothing else in the chain."""
    from services.search import core
    store.save("brave", {"api_key": KEY})
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "brave", "search_result_count": 5})
    monkeypatch.setattr(core, "_effective_provider_chain", lambda primary: ["brave", "duckduckgo"])
    fetched = []
    monkeypatch.setattr(core, "_fetch_result_content", lambda url, query: fetched.append(url) or {"success": True, "url": url, "title": "Fetched", "content": "fetched text " * 30})
    core.fetched = fetched
    return core


def test_a_search_with_brave_is_one_request_and_no_page_is_fetched(searching, monkeypatch):
    net = Answers(monkeypatch, {"/llm/context": PASSAGES})
    text, sources = searching.comprehensive_web_search("how do kettles work", max_pages=5, return_sources=True)
    assert len(net.asked) == 1 and searching.fetched == []
    assert sources == [{"url": "https://kettles.example/how", "title": "How kettles work"}, {"url": "https://history.example/kettle", "title": "Kettle history"}]
    assert "The parts of 2 pages that bear on the question" in text and "PASSAGES FROM THE PAGES" in text
    first, second = text.index("[CONTENT 1] From: https://kettles.example/how"), text.index("[CONTENT 2] From: https://history.example/kettle")
    assert first < second and "A bimetal strip cuts the power." in text[first:second]
    assert "Key Points:" not in text and "TL;DR:" not in text                # those are guessed from whole pages; passages need none
    assert "Prioritize information from the PASSAGES section" in text
    assert "    2031-03-03 (22 days ago)" in text.split("```")[1]             # the page's date is in the source list


def test_the_passages_fit_inside_what_a_tool_may_return(searching, monkeypatch):
    from src.constants import MAX_OUTPUT_CHARS
    big = {"grounding": {"generic": [{"url": f"https://page{n}.example/", "title": f"Page {n}", "snippets": ["made-up words " * 250]} for n in range(6)]},
           "sources": {}}
    Answers(monkeypatch, {"/llm/context": big})
    text = searching.comprehensive_web_search("anything at all", max_pages=6)
    assert len(text) < MAX_OUTPUT_CHARS and text.rstrip().endswith("to fully answer the question")       # the closing instructions are not cut off
    assert text.count("[CONTENT ") >= 3 and "… [more on the page]" in text


def test_without_passages_the_ordinary_search_takes_over(searching, monkeypatch):
    net = Answers(monkeypatch, {"/llm/context": {"grounding": {"generic": []}, "sources": {}}, "/web/search": WEB})
    text = searching.comprehensive_web_search("how do kettles work", max_pages=2)
    assert [one["url"].rsplit("/res/v1", 1)[1] for one in net.asked] == ["/llm/context", "/web/search"]
    assert sorted(searching.fetched) == ["https://history.example/kettle", "https://kettles.example/how"]
    assert "FETCHED PAGE CONTENT:" in text and "PASSAGES FROM THE PAGES" not in text


def test_over_the_limit_the_next_provider_answers_and_brave_is_not_asked(searching, monkeypatch):
    from services.search import core
    store.save("brave", limit=1)
    connections.count("brave", "web")
    net = Answers(monkeypatch, {})
    monkeypatch.setattr(core, "duckduckgo_search", lambda query, count, time_filter=None: [{"title": "Free result", "url": "https://free.example/", "snippet": "from the free provider", "age": ""}])
    text = searching.comprehensive_web_search("how do kettles work", max_pages=1)
    assert net.asked == [] and "Free result" in text and connections.used("brave")["requests"] == 1


def test_a_search_limited_to_some_sites_does_not_use_passages(searching, monkeypatch):
    net = Answers(monkeypatch, {"/web/search": WEB})
    searching.comprehensive_web_search("how do kettles work", max_pages=2, domain_whitelist={"kettles.example"})
    assert [one["url"].rsplit("/res/v1", 1)[1] for one in net.asked] == ["/web/search"] and searching.fetched == ["https://kettles.example/how"]


# ── the news ───────────────────────────────────────────────────────────────

NEWS = {"results": [
    {"title": "Elmford bridge <strong>reopens</strong>", "url": "https://gazette.example/bridge", "description": "The bridge reopened on Monday.",
     "age": "20 hours ago", "page_age": "2031-03-24T19:03:12", "meta_url": {"hostname": "www.gazette.example"}},
    {"title": "No link", "url": ""},
]}
FEED = """<?xml version="1.0" encoding="UTF-8"?><rss><channel>
<item><title>Bridge work ends early - Elmford Courier</title><link>https://news.example/a</link><pubDate>Mon, 24 Mar 2031 15:00:00 GMT</pubDate><source url="https://courier.example">Elmford Courier</source></item>
<item><title>Older story &amp; more - The Ledger</title><link>https://news.example/b</link><pubDate>Sat, 22 Mar 2031 09:00:00 GMT</pubDate><source url="https://ledger.example">The Ledger</source></item>
<item><title></title><link>https://news.example/c</link></item>
</channel></rss>"""


def test_the_news_comes_from_brave_with_dates_when_the_house_has_a_key(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/news/search": NEWS})
    text, sources = news.answer({"query": "Elmford bridge", "when": "week", "count": 4})
    assert "From Brave Search." in text and "[1] Elmford bridge reopens" in text
    assert "    gazette.example · 2031-03-24 · 20 hours ago" in text and "    The bridge reopened on Monday." in text
    assert sources == [{"url": "https://gazette.example/bridge", "title": "Elmford bridge reopens"}]
    assert net.asked[0]["params"]["freshness"] == "pw" and net.asked[0]["params"]["count"] == 4


def test_without_a_key_the_news_comes_from_the_public_feed_and_says_so(own_store, monkeypatch):
    net = Answers(monkeypatch, {"news.google.com": FEED})
    text, sources = news.answer({"query": "Elmford bridge", "when": "week"})
    assert "From Google News (headlines only)." in text
    assert text.index("[1] Bridge work ends early") < text.index("[2] Older story & more")           # newest first, the paper's name off the title
    assert "    Elmford Courier · 2031-03-24" in text and len(sources) == 2
    assert net.asked[0]["params"]["q"] == "Elmford bridge when:7d"
    assert net.to("api.search.brave.com") == []


def test_when_brave_fails_the_feed_answers_and_when_both_fail_the_first_reason_is_given(own_store, monkeypatch):
    store.save("brave", {"api_key": KEY})
    Answers(monkeypatch, {"/news/search": Problem("Brave Search asked us to slow down.", "busy", 429), "news.google.com": FEED})
    assert "From Google News" in news.answer({"query": "Elmford bridge"})[0]
    Answers(monkeypatch, {"/news/search": Problem("Brave Search asked us to slow down.", "busy", 429),
                          "news.google.com": Problem("Google News could not be reached.", "offline")})
    with pytest.raises(Problem, match="slow down"):
        news.answer({"query": "Elmford bridge"})
    with pytest.raises(Problem, match="what the news should be about"):
        news.answer({"query": "  "})


# ── places ─────────────────────────────────────────────────────────────────

def spot(today="", week=None, zone="America/Chicago"):
    return {"today": today, "week": week or {}, "zone": zone}


def at(day, hour, minute=0):
    return datetime(2031, 3, day, hour, minute, tzinfo=ZoneInfo("America/Chicago"))          # 24 March 2031 is a Monday


def test_open_now_is_read_from_the_hours_including_past_midnight():
    shop = spot(week={"Monday": "09:00–18:00", "Tuesday": "09:00–12:00, 13:00–18:00"})
    assert places.open_now(shop, at(24, 9)) is True and places.open_now(shop, at(24, 18)) is False and places.open_now(shop, at(24, 8, 59)) is False
    assert places.open_now(shop, at(25, 12, 30)) is False and places.open_now(shop, at(25, 13)) is True       # shut for lunch
    assert places.open_now(shop, at(26, 10)) is False                                                         # no hours for Wednesday: shut
    bar = spot(week={"Friday": "17:00–02:00", "Saturday": "17:00–02:00"})
    assert places.open_now(bar, at(28, 23)) is True and places.open_now(bar, at(29, 1, 30)) is True           # Saturday 1:30 am, on Friday's hours
    assert places.open_now(bar, at(29, 3)) is False and places.open_now(bar, at(30, 1)) is True and places.open_now(bar, at(31, 1)) is False
    assert places.open_now(spot()) is None                                                                    # hours not known: say nothing
    assert places.open_now(spot(today="09:00–18:00"), at(24, 10)) is True                                     # only today's hours known


def test_hours_are_written_the_way_the_household_reads_the_clock():
    assert places._twelve("09:00–18:00") == "9 am–6 pm" and places._twelve("11:30–00:00, 17:00–02:15") == "11:30 am–12 am, 5 pm–2:15 am"


def test_places_near_a_named_town_with_distance_hours_and_phone(own_store, monkeypatch):
    from services.almanac import places as almanac
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/web/search": PLACES})
    monkeypatch.setattr(almanac, "resolve", lambda text, owner=None, household=True: almanac.Where(
        35.0049, -97.0049, "Elmford, Oklahoma", "America/Chicago", "name", "Took that to be Elmford, Oklahoma; there is also Elmford, Ohio."))
    monkeypatch.setattr(places, "open_now", lambda spot, now=None: True if spot["today"] else None)
    monkeypatch.setattr(places, "_household_units", lambda: "us")
    text, sources = places.answer({"query": "hardware store", "near": "Elmford"}, "mia")
    assert text.startswith("PLACES: hardware store — near Elmford, Oklahoma (from Brave Search)\n"
                           "(Took that to be Elmford, Oklahoma; there is also Elmford, Ohio.)\n")
    assert "[1] Corner Hardware — Hardware store · $$" in text and "    1 Main St, Elmford, OK 73000 (0.5 miles away)" in text
    assert "    Open now — today 9 am–6 pm" in text and "    Phone +15555550101" in text and "    Rated 4.6 out of 5 from 212 reviews" in text
    assert "[2] Far Hardware" in text and "Open now" not in text.split("[2] Far Hardware")[1]                 # hours not known: nothing is claimed
    assert sources[0] == {"url": "https://corner.example/", "title": "Corner Hardware"}
    sent = net.asked[0]["headers"]
    assert (sent["X-Loc-Lat"], sent["X-Loc-Long"]) == ("35.0000", "-97.0000")                                 # the position goes out rounded to about a kilometre
    monkeypatch.setattr(places, "_household_units", lambda: "metric")
    assert "(0.8 km away)" in places.answer({"query": "hardware store", "near": "Elmford"}, "mia")[0]
    only_open, _ = places.answer({"query": "hardware store", "near": "Elmford", "open_now": True}, "mia")
    assert "Corner Hardware" in only_open and "Far Hardware" not in only_open


def test_nearby_with_no_home_saved_asks_for_a_town_and_asks_nobody_else(own_store, monkeypatch):
    from services.almanac import places as almanac
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {})

    def no_home(text, owner=None, household=True):
        raise almanac.PlaceError("No place was given and no home position is saved.")

    monkeypatch.setattr(almanac, "resolve", no_home)
    with pytest.raises(Problem, match="which town") as problem:
        places.answer({"query": "pharmacy"}, "mia")
    assert problem.value.reason == "refused" and net.asked == []
    store.forget("brave")
    with pytest.raises(Problem) as no_key:
        places.answer({"query": "pharmacy", "near": "Elmford"}, "mia")
    assert no_key.value.reason == "key"


def test_a_town_the_place_list_does_not_know_is_searched_by_its_name(own_store, monkeypatch):
    from services.almanac import places as almanac
    store.save("brave", {"api_key": KEY})
    net = Answers(monkeypatch, {"/web/search": PLACES})

    def unknown(text, owner=None, household=True):
        raise almanac.PlaceError("I do not know a place called that.")

    monkeypatch.setattr(almanac, "resolve", unknown)
    text, _ = places.answer({"query": "hardware store", "near": "the old mill district"}, None)
    assert "near the old mill district" in text and "miles away" not in text
    assert net.asked[0]["params"]["q"] == "hardware store near the old mill district" and "X-Loc-Lat" not in net.asked[0]["headers"]


# ── medicine labels ────────────────────────────────────────────────────────

def label(brand, generic, kind="HUMAN PRESCRIPTION DRUG", when="20310301", set_id="set-1", **sections):
    made = {"openfda": {"brand_name": [brand], "generic_name": [generic], "manufacturer_name": ["Made-Up Pharma Inc."], "product_type": [kind],
                        "route": ["ORAL"], "spl_set_id": [set_id]}, "effective_time": when, "set_id": set_id, "version": "3",
            "spl_product_data_elements": ["skipped"]}
    made.update({name: [text] for name, text in sections.items()})
    return made


CALMORA = label("Calmora", "VELTRANIB", set_id="set-calmora",
                boxed_warning="WARNING: MADE-UP RISK Calmora may cause invented harm.",
                indications_and_usage="1 INDICATIONS AND USAGE Calmora is indicated for invented restlessness in adults.",
                dosage_and_administration="2 DOSAGE AND ADMINISTRATION The starting dose is 10 mg once daily. " + "Increase by 10 mg each week. " * 300,
                adverse_reactions="6 ADVERSE REACTIONS The most common reactions (5% or more) were yawning and hiccups.",
                drug_interactions="7 DRUG INTERACTIONS Do not take with grapefruit-flavoured anything.",
                storage_and_handling="16 Store below 25 C.")
CALMORA_XR = label("Calmora XR", "VELTRANIB", when="20300101", set_id="set-xr", indications_and_usage="Extended release.")
OTC = label("Achefree", "DOLOPRENE", kind="HUMAN OTC DRUG", when="20310201", set_id="set-otc", purpose="Pain reliever",
            dosage_and_administration="Directions take 1 tablet every 6 hours; do not exceed 4 in 24 hours", warnings="Warnings Do not use with other pain relievers.")
RX = label("Doloprene", "DOLOPRENE", when="20310315", set_id="set-rx", dosage_and_administration="DOSAGE 400 mg to 800 mg three times a day.",
           indications_and_usage="Indicated for invented aches.")
COMBO = label("Doloprene PM", "DOLOPRENE AND SLEEPINOL", kind="HUMAN OTC DRUG", when="20310320", set_id="set-combo", dosage_and_administration="2 caplets at bedtime")


def fda(by_name):
    def answer(params):
        asked = params["search"].split('"')[1].lower()
        found = by_name.get(asked)
        if not found:
            return Problem("The FDA's label service would not answer that: No matches found!", "refused", 404)
        return {"results": found}
    return answer


def test_a_question_about_a_medicine_gets_that_part_of_its_label(own_store, monkeypatch):
    net = Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA_XR, CALMORA]})})
    text, sources = medicines.answer({"name": "Calmora", "about": "side effects"})
    assert text.startswith("MEDICINE LABEL: Calmora (veltranib)\nMade-Up Pharma Inc. · prescription · oral · label dated 2031-03-01\n"
                           "Label: https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=set-calmora")
    assert "## Adverse reactions\n6 ADVERSE REACTIONS The most common reactions (5% or more) were yawning and hiccups." in text
    assert "DOSAGE AND ADMINISTRATION" not in text                               # only what was asked about
    assert "The label also has: Boxed warning (the FDA's strongest); Indications and usage; Dosage and administration; Drug interactions; Storage and handling." in text
    assert "The same ingredient is also sold as: Calmora XR" in text
    assert sources == [{"url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=set-calmora", "title": "Calmora — official label (DailyMed)"}]
    params = net.asked[0]["params"]
    assert params["search"] == 'openfda.brand_name:"Calmora" openfda.generic_name:"Calmora" openfda.substance_name:"Calmora"' and "api_key" not in params


@pytest.mark.parametrize("about, heading", [
    ("what is it for", "## Indications and usage"), ("how much should I take", "## Dosage and administration"), ("dose", "## Dosage and administration"),
    ("can I take it with alcohol", "## Drug interactions"), ("warnings", "## Boxed warning (the FDA's strongest)"), ("how do I store it", "## Storage and handling"),
    ("drug_interactions", "## Drug interactions"), ("Storage and handling", "## Storage and handling"),
])
def test_what_is_asked_about_finds_its_section(own_store, monkeypatch, about, heading):
    Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]})})
    assert heading in medicines.answer({"name": "Calmora", "about": about})[0]


def test_with_no_particular_question_the_label_is_summed_up(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]})})
    text, _ = medicines.answer({"name": "Calmora"})
    order = [text.index(heading) for heading in ("## Boxed warning", "## Indications and usage", "## Dosage and administration", "## Adverse reactions", "## Drug interactions")]
    assert order == sorted(order) and '[more: "about": "dosage_and_administration"]' in text and len(text) < 9000


def test_a_long_section_comes_a_page_at_a_time_and_loses_nothing(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]})})
    whole = " ".join(CALMORA["dosage_and_administration"][0].split())
    first, _ = medicines.answer({"name": "Calmora", "about": "dose"})
    assert "## Dosage and administration (page 1 of 2)" in first and '[The section goes on. For the rest: the same call with "page": 2.]' in first
    second, _ = medicines.answer({"name": "Calmora", "about": "dose", "page": 2})
    assert "## Dosage and administration (page 2 of 2)" in second and "The section goes on" not in second
    assert " ".join(medicines._pages(whole)) == whole                           # the pages put together are the section
    assert all(page.endswith(".") for page in medicines._pages(whole))          # each ends with a sentence
    assert "There is no page 5: this section ends on page 2." in medicines.answer({"name": "Calmora", "about": "dose", "page": 5})[0]


def test_a_medicine_sold_both_ways_gives_the_shelf_label_unless_asked(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": fda({"doloprene": [COMBO, RX, OTC]})})
    text, _ = medicines.answer({"name": "doloprene", "about": "dose"})
    assert text.startswith("MEDICINE LABEL: Achefree (doloprene)\nMade-Up Pharma Inc. · over the counter · oral")
    assert 'This is the over-the-counter label; for the other one: the same call with "kind": "prescription".' in text
    assert "take 1 tablet every 6 hours" in text and "also sold as" not in text
    other, _ = medicines.answer({"name": "doloprene", "about": "dose", "kind": "prescription"})
    assert other.startswith("MEDICINE LABEL: Doloprene\n") and "400 mg to 800 mg three times a day" in other      # and never the combination


def test_a_misspelt_name_is_put_right_and_said_so(own_store, monkeypatch):
    net = Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]}),
                                "rxnav.nlm.nih.gov": {"approximateGroup": {"candidate": [{"name": "Calmora", "rank": "1"}, {"rank": "1"}, {"name": "Calmora", "rank": "2"}]}}})
    text, _ = medicines.answer({"name": "calmorra", "about": "uses"})
    assert "(“calmorra” was taken to mean Calmora.)" in text and "invented restlessness" in text
    assert [one["url"].split("/")[2] for one in net.asked] == ["api.fda.gov", "rxnav.nlm.nih.gov", "api.fda.gov"]


def test_no_label_a_bare_name_and_a_switched_off_service_are_said_plainly(own_store, monkeypatch):
    net = Answers(monkeypatch, {"api.fda.gov": fda({}), "rxnav.nlm.nih.gov": {"approximateGroup": {}}})
    with pytest.raises(Problem, match="No US label was found for “nothingol”") as nothing:
        medicines.answer({"name": "nothingol"})
    assert nothing.value.reason == "nothing"
    with pytest.raises(Problem, match="Give the name of the medicine"):
        medicines.answer({"name": " "})
    store.save("medicines", on=False)
    asked = len(net.asked)
    with pytest.raises(Problem) as off:
        medicines.answer({"name": "Calmora"})
    assert off.value.reason == "off" and len(net.asked) == asked


def test_an_openfda_key_is_sent_when_the_house_has_one(own_store, monkeypatch):
    store.save("medicines", {"api_key": "fda-made-up-key-123"})
    net = Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]})})
    medicines.answer({"name": "Calmora", "about": "uses"})
    assert net.asked[0]["params"]["api_key"] == "fda-made-up-key-123"


# ── studies ────────────────────────────────────────────────────────────────

def work(number, title, cited, year=2029, kind="article", words="made up abstract words", free=""):
    index = {}
    for place, word in enumerate(words.split()):
        index.setdefault(word, []).append(place)
    return {"id": f"https://openalex.example/W{number}", "doi": f"https://doi.org/10.5555/made.{number}", "display_name": title, "publication_year": year,
            "publication_date": f"{year}-05-01", "cited_by_count": cited, "type": kind, "is_retracted": number == 99,
            "authorships": [{"author": {"display_name": name}} for name in ("Ada Example", "Ben Sample", "Cy Specimen", "Di Instance", "Ed Token")[: 1 + number % 5]],
            "primary_location": {"landing_page_url": f"https://journal.example/{number}", "source": {"display_name": "Journal of Invented Results"}},
            "best_oa_location": {"pdf_url": free} if free else None, "open_access": {}, "abstract_inverted_index": index}


def test_an_abstract_is_put_back_in_order_and_a_span_of_years_is_read():
    assert papers._abstract({"tea": [1, 4], "Green": [0], "and": [2], "black": [3]}) == "Green tea and black tea"
    assert papers._abstract(None) == ""
    this_year = datetime.now().year
    assert papers._since("2021") == 2021 and papers._since("since 2019") == 2019 and papers._since("last 5 years") == this_year - 5
    assert papers._since("") is None and papers._since("lately") is None


def test_studies_are_weighed_on_the_title_the_citations_and_the_match(own_store, monkeypatch):
    matched = [work(1, "Kettle descaling methods compared", 0), work(2, "Descaling kettles with citric acid: a trial", 40),
               work(3, "A thesis that mentions kettles once", 0)]
    cited = [work(7, "Global review of everything domestic", 5000, kind="review"), work(8, "Citric acid descaling of kettles and boilers", 900, free="https://free.example/8.pdf"),
             work(2, "Descaling kettles with citric acid: a trial", 40)]

    def openalex(params):
        return {"results": cited if params.get("sort") == "cited_by_count:desc" else matched}

    net = Answers(monkeypatch, {"api.openalex.org": openalex})
    found, who = papers.find("kettle descaling citric acid", count=4)
    assert who == "OpenAlex" and len(net.asked) == 2                              # the best matches, and the most cited
    assert [paper["title"] for paper in found] == [
        "Citric acid descaling of kettles and boilers",        # on the subject and much cited: found only by asking for the most cited
        "Descaling kettles with citric acid: a trial",         # on the subject, cited, and well placed by the index
        "Kettle descaling methods compared",                   # on the subject, not yet cited
        "Global review of everything domestic",                # famous, but about something else: last, for all its citations
    ]
    filters = net.asked[0]["params"]["filter"]
    assert filters.startswith("title_and_abstract.search:kettle descaling citric acid,type:article|review|preprint,to_publication_date:")
    assert "api_key" not in net.asked[0]["params"]


def test_a_studies_answer_names_who_when_where_and_the_free_copy(own_store, monkeypatch):
    store.save("papers", {"api_key": "openalex-made-up-key"})
    one = work(8, "Citric acid descaling of kettles and boilers", 900, year=2027, free="https://free.example/8.pdf", words="Citric acid removed scale in every kettle tested")
    net = Answers(monkeypatch, {"api.openalex.org": {"results": [one, work(99, "A withdrawn paper", 3)]}})
    text, sources = papers.answer({"query": "kettle descaling", "since": "2025", "sort": "cited", "count": 5})
    assert text.startswith("STUDIES about: kettle descaling\nFrom OpenAlex. 2 papers:")
    assert "[1] Citric acid descaling of kettles and boilers\n    Ada Example, Ben Sample, Cy Specimen and 1 others\n    Journal of Invented Results · 2027 · cited 900 times" in text
    assert "    Abstract: Citric acid removed scale in every kettle tested\n    https://doi.org/10.5555/made.8\n    Free full text: https://free.example/8.pdf" in text
    assert "[2] A withdrawn paper  — RETRACTED" in text
    assert sources[0] == {"url": "https://doi.org/10.5555/made.8", "title": "Citric acid descaling of kettles and boilers"}
    params = net.asked[0]["params"]
    assert len(net.asked) == 1 and params["sort"] == "cited_by_count:desc" and params["api_key"] == "openalex-made-up-key"
    assert "from_publication_date:2025-01-01" in params["filter"]


def test_when_openalex_cannot_answer_crossref_does_and_the_answer_says_so(own_store, monkeypatch):
    crossref = {"message": {"items": [{"title": ["Scale in kettles: <i>a survey</i>"], "author": [{"given": "ADA", "family": "EXAMPLE"}, {"given": "Ben", "family": "Sample"}],
                                       "container-title": ["Invented Letters"], "issued": {"date-parts": [[2030, 4, 2]]}, "is-referenced-by-count": 12,
                                       "DOI": "10.5555/made.cr1", "abstract": "<jats:p>Scale builds up.</jats:p>"}, {"title": [""]}]}}
    net = Answers(monkeypatch, {"api.openalex.org": Problem("OpenAlex asked us to slow down.", "busy", 429), "api.crossref.org": crossref})
    found, who = papers.find("kettle scale")
    assert who.startswith("Crossref (OpenAlex could not be asked")
    assert found == [{"id": "10.5555/made.cr1", "title": "Scale in kettles: a survey", "authors": ["Ada Example", "Ben Sample"], "year": 2030, "date": "2030-04-02",
                      "journal": "Invented Letters", "cited": 12, "kind": "", "abstract": " Scale builds up. ", "link": "https://doi.org/10.5555/made.cr1",
                      "free": "", "retracted": False}]
    assert net.to("api.crossref.org")[0]["params"]["query.bibliographic"] == "kettle scale"
    Answers(monkeypatch, {"api.openalex.org": Problem("OpenAlex asked us to slow down.", "busy", 429), "api.crossref.org": Problem("Crossref could not be reached.", "offline")})
    with pytest.raises(Problem, match="slow down"):
        papers.find("kettle scale")
    Answers(monkeypatch, {"api.openalex.org": {"results": []}, "api.crossref.org": {"message": {"items": []}}})
    with pytest.raises(Problem, match="No published studies were found") as nothing:
        papers.find("kettle scale", since="2030")
    assert nothing.value.reason == "nothing"


# ── Wolfram|Alpha ──────────────────────────────────────────────────────────

def test_wolfram_needs_its_appid_and_then_reports_what_it_worked_out(own_store, monkeypatch):
    with pytest.raises(Problem) as no_key:
        wolfram.answer({"question": "2+2"})
    assert no_key.value.reason == "key"
    store.save("wolfram", {"app_id": "MADE-UP-APPID"})
    net = Answers(monkeypatch, {"wolframalpha.com": "Input: 2+2\nResult: 4"})
    text, sources = wolfram.answer({"question": "2+2"})
    assert text.startswith("WOLFRAM|ALPHA: 2+2\n\nInput: 2+2\nResult: 4") and sources == []
    assert net.asked[0]["params"] == {"input": "2+2", "appid": "MADE-UP-APPID", "maxchars": 4000}
    assert connections.used("wolfram")["kinds"] == {"question": 1}


def test_a_question_wolfram_cannot_read_and_a_used_up_month_are_said_in_words(own_store, monkeypatch):
    store.save("wolfram", {"app_id": "MADE-UP-APPID"})
    Answers(monkeypatch, {"wolframalpha.com": Problem("Wolfram|Alpha is having trouble (it answered 501).", "offline", 501)})
    with pytest.raises(Problem, match="could not make sense of “colourless green ideas”") as unread:
        wolfram.ask("colourless green ideas")
    assert unread.value.reason == "nothing" and connections.used("wolfram")["requests"] == 0
    assert connections.limit("wolfram") == 2000
    store.save("wolfram", limit=1)
    connections.count("wolfram", "question")
    with pytest.raises(Problem) as full:
        wolfram.ask("2+2")
    assert full.value.reason == "limit"


# ── the tools ──────────────────────────────────────────────────────────────

def tool(name, args, owner="mia"):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args) if isinstance(args, dict) else args, {"owner": owner}))


def test_a_look_up_answers_with_its_sources_for_the_screen(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": fda({"calmora": [CALMORA]})})
    found = tool("medicine_info", {"name": "Calmora", "about": "uses"})
    text, marker = found["output"].split("\n\n<!-- SOURCES:")
    assert found["exit_code"] == 0 and text.startswith("MEDICINE LABEL: Calmora (veltranib)")
    assert json.loads(marker[: -len(" -->")]) == [{"url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=set-calmora", "title": "Calmora — official label (DailyMed)"}]
    plain = tool("medicine_info", "Calmora")                                      # a bare name works too
    assert plain["exit_code"] == 0 and "## Indications and usage" in plain["output"]


def test_a_look_up_that_finds_nothing_says_so_and_forbids_making_one_up(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": fda({}), "rxnav.nlm.nih.gov": {"approximateGroup": {}}})
    found = tool("medicine_info", {"name": "nothingol"})
    assert found == {"output": "No US label was found for “nothingol”. Check the spelling, or give the other name it goes by (the brand name or the ingredient). "
                               "Tell the person that, plainly; do not make up a result.", "exit_code": 0}


def test_a_look_up_whose_service_cannot_be_reached_tells_the_assistant_what_to_do_instead(own_store, monkeypatch):
    Answers(monkeypatch, {"api.fda.gov": Problem("The FDA's label service could not be reached.", "offline")})
    found = tool("medicine_info", {"name": "Calmora"})
    assert found["exit_code"] == 1
    assert found["error"] == ("medicine_info: The FDA's label service could not be reached. Say the label could not be checked. Anything you add about the "
                              "medicine from memory must be marked as from memory and not checked against the label.")
    wrong = tool("find_places", {"query": ""})                                    # a call the assistant can put right
    assert wrong["exit_code"] == 1 and wrong["error"].startswith("find_places: ")


def test_a_look_up_that_breaks_is_reported_not_raised(own_store, monkeypatch):
    def broken(args):
        raise KeyError("an unexpected shape")
    monkeypatch.setattr(papers, "answer", broken)
    found = tool("paper_search", {"query": "kettles"})
    assert found["exit_code"] == 1 and found["error"].startswith("paper_search failed: KeyError.") and "web_search" in found["error"]


def test_only_the_look_ups_that_are_set_up_are_offered(own_store, monkeypatch):
    from src.agent_tools import lookup_tools
    assert lookup_tools.unavailable() == {"find_places", "ask_wolfram"}          # the keyless ones work as they come; the news has a keyless fallback
    store.save("brave", {"api_key": KEY})
    store.save("wolfram", {"app_id": "MADE-UP-APPID"})
    assert lookup_tools.unavailable() == set()
    store.save("brave", limit=1)
    connections.count("brave", "web")
    assert "find_places" not in lookup_tools.unavailable()                        # over the limit it stays, to say that the limit is reached
    store.save("papers", on=False)
    store.save("brave", on=False)
    assert lookup_tools.unavailable() == {"paper_search", "find_places"}
    monkeypatch.setenv("AEGIS_CONNECTIONS_DISABLED", "1")
    store.forget_cache()
    assert lookup_tools.unavailable() == set(lookup_tools.LOOKUP_TOOLS)


def test_every_look_up_is_registered_everywhere_a_tool_must_be():
    from src import agent_loop, tool_index, tool_policy, tool_schemas, tool_security
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS, lookup_tools

    schemas = {entry["function"]["name"]: entry["function"] for entry in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    hinted = set().union(*tool_index.ToolIndex._KEYWORD_HINTS.values())
    for name in lookup_tools.LOOKUP_TOOLS:
        assert name in TOOL_HANDLERS and name in TOOL_TAGS, name
        assert name in schemas and schemas[name]["parameters"]["required"], name
        assert name in tool_schemas._REQUIRED_NATIVE_TOOL_ARGS, name
        assert name in tool_index.BUILTIN_TOOL_DESCRIPTIONS and name in hinted, name
        assert name in agent_loop.TOOL_SECTIONS and name in agent_loop._DOMAIN_TOOL_MAP["lookups"], name
        assert name in tool_security.PLAN_MODE_READONLY_TOOLS, name                # they only read
        assert name not in tool_security.NON_ADMIN_BLOCKED_TOOLS, name             # every account has them
        assert name in tool_policy.known_tool_names(), name
        assert catalog.for_tool(name), name                                        # and each says which service it draws on
    assert set(lookup_tools.SOURCE_TOOLS) <= set(lookup_tools.LOOKUP_TOOLS)
    assert "## Looking things up" in agent_loop._DOMAIN_RULES["lookups"]


@pytest.mark.parametrize("said, name", [
    ("any news about the Elmford bridge", "news_search"), ("what's the latest on the moon mission", "news_search"),
    ("where is the nearest pharmacy", "find_places"), ("is there a hardware store open now", "find_places"),
    ("what are the side effects of calmora", "medicine_info"), ("can I take calmora with ibuprofen", "medicine_info"),
    ("is there evidence that creatine helps memory", "paper_search"), ("find me a meta-analysis on cold plunges", "paper_search"),
    ("what is the monthly payment on a 300k loan", "ask_wolfram"),
])
def test_what_a_person_says_brings_the_right_look_up_into_reach(said, name):
    import re
    from src.tool_index import ToolIndex
    reached = set()
    for words, tools in ToolIndex._KEYWORD_HINTS.items():
        if any(re.search(rf"\b{re.escape(word)}\b", said.lower()) for word in words):
            reached |= tools
    assert name in reached


@pytest.mark.parametrize("said", ["is there a way to rename this chat", "what's happening with my code", "write a poem about tablets of stone",
                                  "open the settings", "how many days until Friday"])
def test_ordinary_messages_do_not_drag_the_look_ups_in(said):
    import re
    from src.agent_tools import lookup_tools
    from src.tool_index import ToolIndex
    reached = set()
    for words, tools in ToolIndex._KEYWORD_HINTS.items():
        if any(re.search(rf"\b{re.escape(word)}\b", said.lower()) for word in words):
            reached |= tools
    assert not reached & set(lookup_tools.LOOKUP_TOOLS)


# ── trying a key, and the routes ───────────────────────────────────────────

def test_trying_a_service_makes_one_real_request_and_remembers_how_it_went(own_store, monkeypatch):
    assert checks.try_it("brave") == (False, "Type in the API key first.")
    assert checks.try_it("nowhere") == (False, "That is not a service this app knows.")
    store.save("brave", {"api_key": KEY})
    Answers(monkeypatch, {"/web/search": WEB, "/llm/context": PASSAGES})
    ok, said = checks.try_it("brave")
    assert ok and said == "Brave Search answered. Web search, news and places are ready. A search also brings back the passages that answer it."
    assert store.describe(catalog.get("brave"))["tried"]["ok"] is True
    brave.forget()
    Answers(monkeypatch, {"/web/search": Problem("Brave Search did not accept the key.", "key", 401)})
    assert checks.try_it("brave") == (False, "Brave Search did not accept the key.")
    assert store.describe(catalog.get("brave"))["tried"]["said"] == "Brave Search did not accept the key."
    store.save("brave", {"api_key": KEY + "x"})                                   # a new key: the old verdict no longer stands
    assert store.describe(catalog.get("brave"))["tried"] is None
    store.save("brave", on=False)
    assert checks.try_it("brave") == (False, "Brave Search is switched off. Switch it on to try it.")


def test_every_service_has_a_try_and_says_what_it_is(own_store):
    assert set(checks.CHECKS) == {service.id for service in catalog.SERVICES}
    groups = {key for key, _ in catalog.GROUPS}
    for service in catalog.SERVICES:
        assert service.group in groups and service.does and service.cost and service.sends and service.tools, service.id
        assert service.signup.startswith("https://"), service.id
        assert service.needs_key or service.key_adds, service.id                   # a key that is optional says what it adds
    assert catalog.get("brave").metered and catalog.get("brave").free_per_month == 1000 and catalog.get("brave").price_per_1000 == 5.0


@pytest.fixture
def screen(own_store, monkeypatch):
    """The Integrations routes behind a sign-in that can be an administrator or not."""
    import routes.connections_routes as routes
    who = {"admin": True}

    def need_admin(request):
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(routes, "require_admin", need_admin)
    app = FastAPI()
    app.include_router(routes.setup_connections_routes())
    client = TestClient(app)
    client.who = who
    return client


def test_an_administrator_saves_tries_limits_and_removes_a_key_and_never_sees_it_again(screen, monkeypatch):
    start = screen.get("/api/connections").json()
    assert [group["id"] for group in start["groups"]] == ["search", "research", "answers"]
    assert {service["id"]: service["ready"] for service in start["services"]} == {
        "brave": False, "tavily": False, "serper": False, "google_pse": False, "medicines": True, "papers": True, "wolfram": False}
    saved = screen.put("/api/connections/brave", json={"fields": {"api_key": KEY}})
    assert saved.status_code == 200 and KEY not in saved.text
    assert saved.json()["ready"] is True and saved.json()["fields"][0]["shown"] == "••••••abcd" and saved.json()["metered"]["limit"] == 1000
    Answers(monkeypatch, {"/web/search": WEB, "/llm/context": PASSAGES})
    tried = screen.post("/api/connections/brave/try").json()
    assert tried["ok"] is True and tried["said"].startswith("Brave Search answered.") and tried["service"]["tried"]["ok"] is True
    assert tried["service"]["metered"]["requests"] == 2 and KEY not in json.dumps(tried)
    assert screen.put("/api/connections/brave", json={"limit": 2500}).json()["metered"]["limit"] == 2500
    assert screen.put("/api/connections/brave", json={"on": False}).json()["ready"] is False
    assert KEY not in screen.get("/api/connections").text
    gone = screen.delete("/api/connections/brave").json()
    assert gone["has_key"] is False and gone["fields"][0]["set"] is False and store.value("brave") == ""


def test_what_the_screen_sends_wrong_is_refused_and_changes_nothing(screen):
    assert screen.put("/api/connections/nowhere", json={"on": True}).status_code == 404
    assert screen.post("/api/connections/nowhere/try").status_code == 404
    for body, words in (({"fields": "a string"}, "fields must be an object"), ({"on": "yes"}, "on must be true or false"),
                        ({"fields": {"api_key": "two words"}}, "API key has spaces or line breaks in it — paste it again."),
                        ({"limit": -5}, "The monthly limit must be between 0 (no limit) and 10,000,000.")):
        answer = screen.put("/api/connections/brave", json=body)
        assert answer.status_code == 400 and answer.json()["detail"] == words
    assert store.value("brave") == ""


def test_nobody_but_an_administrator_reaches_the_keys(screen):
    store.save("brave", {"api_key": KEY})
    screen.who["admin"] = False
    assert screen.get("/api/connections").status_code == 403
    assert screen.put("/api/connections/brave", json={"fields": {"api_key": "someone-elses-key-123"}}).status_code == 403
    assert screen.delete("/api/connections/brave").status_code == 403 and screen.post("/api/connections/brave/try").status_code == 403
    assert store.value("brave") == KEY


def test_the_routes_are_part_of_the_app_and_the_keys_stay_out_of_git():
    app_py = (REPO / "app.py").read_text()
    assert "from routes.connections_routes import setup_connections_routes" in app_py and "app.include_router(setup_connections_routes())" in app_py
    ignored = (REPO / ".gitignore").read_text().splitlines()
    assert "data/" in ignored                                                      # where connections.json lives


# ── the screen ─────────────────────────────────────────────────────────────

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def test_the_integrations_tab_has_the_services_card_and_starts_it():
    page = (REPO / "static" / "index.html").read_text()
    panel = page.split('data-settings-panel="integrations"')[1].split("<!-- ═══ TOOLS TAB ═══ -->")[0]
    assert panel.index('id="connections-list"') < panel.index('id="unified-integrations-list"')      # the services come first
    assert "every account's assistant uses it" in panel and "never shown again" in panel
    settings = (REPO / "static" / "js" / "settings.js").read_text()
    assert "import connectionsModule from './connections.js';" in settings and "connectionsModule.init();" in settings
    script = (REPO / "static" / "js" / "connections.js").read_text()
    assert "innerHTML" not in script                                               # what the server sends is set as text, never as markup
    assert "type: field.secret ? 'password' : 'text'" in script and "rel: 'noopener noreferrer'" in script


@needs_node
def test_the_chip_on_a_card_says_the_state_it_is_in():
    script = """
import { state, money } from './static/js/connections.js';
const base = { on: true, needs_key: true, has_key: true, tried: null, metered: null };
console.log(JSON.stringify([
  state({ ...base, on: false }), state({ ...base, has_key: false }), state({ ...base, metered: { over: true } }),
  state({ ...base, tried: { ok: false, said: 'no' } }), state({ ...base, needs_key: false, has_key: false }), state(base),
  state({ ...base, tried: { ok: true } }), money(2), money(0.125), money(undefined)]));
"""
    out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=REPO, check=True, capture_output=True, text=True, encoding="utf-8")
    assert json.loads(out.stdout) == [["idle", "Off"], ["idle", "Needs a key"], ["warn", "Limit reached"], ["bad", "Not working"],
                                      ["ready", "Ready, no key needed"], ["ready", "Ready"], ["ready", "Ready"], "$2.00", "$0.13", "$0.00"]


# ── MCP servers opened to every account ────────────────────────────────────

@pytest.fixture
def sharing(own_store, monkeypatch):
    """The setting that lists the MCP servers opened to everyone, kept in memory."""
    import src.settings as settings
    kept = {}
    monkeypatch.setattr(settings, "load_settings", lambda: dict(kept))
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: kept.get(key, default))
    monkeypatch.setattr(settings, "save_settings", lambda new: (kept.clear(), kept.update(new)))
    return kept


def test_mcp_is_an_administrators_until_one_opens_a_server_to_everyone(sharing):
    from src import tool_security as security
    assert security.shared_mcp_servers() == set()
    assert security.is_public_blocked_tool("mcp__homehub__turn_on") and security.is_public_blocked_tool("mcp__email__send_email")
    sharing["mcp_shared_servers"] = ["homehub"]
    assert security.shared_mcp_servers() == {"homehub"}
    assert not security.is_public_blocked_tool("mcp__homehub__turn_on")
    assert security.is_public_blocked_tool("mcp__todo__add")                       # another server stays closed
    assert security.is_public_blocked_tool("mcp__homehub") and security.is_public_blocked_tool("mcp____x")       # not a tool's name at all
    assert security.is_public_blocked_tool("bash") and not security.is_public_blocked_tool("web_search")          # the rest is as it was


def test_servers_with_private_data_or_the_servers_own_reach_can_never_be_opened(sharing):
    from src import tool_security as security
    sharing["mcp_shared_servers"] = ["email", "memory", "rag", "image_gen", "troubleshoot", "builtin_browser", "homehub", "bad__id", 7]
    assert security.shared_mcp_servers() == {"homehub"}
    for server in security.UNSHAREABLE_MCP:
        assert security.is_public_blocked_tool(f"mcp__{server}__anything"), server
    sharing["mcp_shared_servers"] = "homehub"                                       # a damaged setting opens nothing
    assert security.shared_mcp_servers() == set()


def test_for_another_account_every_tool_outside_the_opened_servers_is_hidden():
    from src.tool_security import mcp_hidden_from_others
    tools = [{"server_id": "homehub", "name": "turn_on"}, {"server_id": "homehub", "name": "status"},
             {"server_id": "todo", "name": "add"}, {"server_id": "email", "name": "send_email"}, {"server_id": "todo", "name": "list"}]
    assert mcp_hidden_from_others(tools, {"homehub"}) == {"todo": {"add", "list"}, "email": {"send_email"}}
    assert mcp_hidden_from_others(tools, set()) == {"homehub": {"turn_on", "status"}, "todo": {"add", "list"}, "email": {"send_email"}}
    assert mcp_hidden_from_others([], {"homehub"}) == {}
    loop = (REPO / "src" / "agent_loop.py").read_text()
    assert "mcp_hidden_from_others(mcp_mgr.get_all_tools(), _shared_mcp_only)" in loop       # the agent loop narrows what it offers with it
    assert "if mcp_mgr and _shared_mcp_only is None:" in loop                                  # and never builds the shared index from that narrowed view


def test_another_accounts_assistant_runs_an_opened_servers_tool_and_no_other(sharing, monkeypatch):
    from types import SimpleNamespace
    from src import tool_execution

    called = []

    class Manager:
        async def call_tool(self, name, args):
            called.append((name, args))
            return {"output": "done", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: False)
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: Manager())
    sharing["mcp_shared_servers"] = ["homehub", "email"]

    def run(name, content="{}"):
        return asyncio.run(tool_execution.execute_tool_block(SimpleNamespace(tool_type=name, content=content), owner="mia"))

    desc, result = run("mcp__homehub__turn_on", '{"what": "porch light"}')
    assert result == {"output": "done", "exit_code": 0} and called == [("mcp__homehub__turn_on", {"what": "porch light"})]
    for name in ("mcp__todo__add", "mcp__email__send_email", "send_email"):
        desc, result = run(name)
        assert desc == f"{name}: BLOCKED" and "restricted to admin users" in result["error"], name
    assert len(called) == 1
    sharing["mcp_shared_servers"] = []
    assert run("mcp__homehub__turn_on")[0] == "mcp__homehub__turn_on: BLOCKED"


@pytest.fixture
def mcp_screen(sharing, monkeypatch):
    """The MCP routes over two made-up servers, behind a sign-in that can be an administrator or not."""
    import routes.mcp_routes as routes
    who = {"admin": True}
    servers = {name: __import__("types").SimpleNamespace(id=name, name=name.title(), transport="stdio", command="made-up", args="[]", env="{}", url=None,
                                                         is_enabled=True, oauth_config=None, disabled_tools=None) for name in ("homehub", "email")}

    class Query:
        def __init__(self, wanted=None):
            self.wanted = wanted

        def filter(self, condition):
            return Query(condition.right.value)

        def first(self):
            return servers.get(self.wanted)

        def all(self):
            return list(servers.values())

    class Session:
        def query(self, model):
            return Query()

        def delete(self, row):
            servers.pop(row.id, None)

        def commit(self):
            pass

        def close(self):
            pass

    class Manager:
        def get_server_status(self, server_id):
            return {"status": "connected", "tool_count": 2}

        async def disconnect_server(self, server_id):
            pass

    def need_admin(request):
        if not who["admin"]:
            raise HTTPException(403, "Administrators only")

    monkeypatch.setattr(routes, "require_admin", need_admin)
    monkeypatch.setattr(routes, "SessionLocal", lambda: Session())
    router = routes.setup_mcp_routes(Manager())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.who = who
    return client


def test_an_administrator_opens_and_closes_an_mcp_server_and_the_list_says_how_it_stands(mcp_screen, sharing):
    listed = {server["id"]: (server["shared"], server["shareable"]) for server in mcp_screen.get("/api/mcp/servers").json()}
    assert listed == {"homehub": (False, True), "email": (False, False)}
    assert mcp_screen.put("/api/mcp/servers/homehub/shared", json={"shared": True}).json() == {"id": "homehub", "shared": True}
    assert sharing["mcp_shared_servers"] == ["homehub"]
    assert {server["id"]: server["shared"] for server in mcp_screen.get("/api/mcp/servers").json()} == {"homehub": True, "email": False}
    refused = mcp_screen.put("/api/mcp/servers/email/shared", json={"shared": True})
    assert refused.status_code == 400 and "stays with administrators" in refused.json()["detail"]
    assert mcp_screen.put("/api/mcp/servers/homehub/shared", json={"shared": "yes"}).status_code == 400
    assert mcp_screen.put("/api/mcp/servers/nowhere/shared", json={"shared": True}).status_code == 404
    assert sharing["mcp_shared_servers"] == ["homehub"]
    assert mcp_screen.put("/api/mcp/servers/homehub/shared", json={"shared": False}).json()["shared"] is False and sharing["mcp_shared_servers"] == []


def test_only_an_administrator_changes_who_may_use_a_server_and_removing_one_closes_it(mcp_screen, sharing):
    mcp_screen.put("/api/mcp/servers/homehub/shared", json={"shared": True})
    mcp_screen.who["admin"] = False
    assert mcp_screen.put("/api/mcp/servers/homehub/shared", json={"shared": False}).status_code == 403
    assert mcp_screen.get("/api/mcp/servers").status_code == 403 and sharing["mcp_shared_servers"] == ["homehub"]
    mcp_screen.who["admin"] = True
    assert mcp_screen.delete("/api/mcp/servers/homehub").json() == {"status": "deleted"}
    assert sharing["mcp_shared_servers"] == []                                     # a later server with the same id starts closed


def test_the_mcp_form_has_the_switch_and_says_what_it_means():
    script = (REPO / "static" / "js" / "settings.js").read_text()
    assert 'id="uf-mcp-share"' in script and "/api/mcp/servers/${srv.id}/shared" in script
    assert "Everyone at home can use this." in script and "it cannot be opened to other accounts" in script


# ── a key typed the old way, on the Search tab ─────────────────────────────

def test_a_key_typed_on_the_search_tab_is_kept_encrypted_with_the_others(own_store):
    current = {"search_provider": "brave", "brave_api_key": KEY, "tavily_api_key": "", "google_pse_cx": "engine-id-42"}
    taken = store.take_from_settings({"search_provider": "brave", "brave_api_key": KEY, "tavily_api_key": "", "google_pse_cx": "engine-id-42"}, current)
    assert taken == ["brave_api_key"] and current["brave_api_key"] == "" and store.value("brave") == KEY
    assert current["google_pse_cx"] == "engine-id-42"                               # an id is not a secret: it stays where it was
    assert KEY not in (own_store / "connections.json").read_text()
    # An empty box on that tab does not remove the key kept here; a bad key stays where it was typed.
    again = {"brave_api_key": ""}
    assert store.take_from_settings({"brave_api_key": ""}, again) == [] and store.value("brave") == KEY
    odd = {"serper_api_key": "two words"}
    assert store.take_from_settings({"serper_api_key": "two words"}, odd) == [] and odd == {"serper_api_key": "two words"}
    assert store.take_from_settings(None, {}) == []


def test_the_settings_route_hands_search_keys_over_and_the_search_tab_knows_a_key_is_kept():
    route = (REPO / "routes" / "auth_routes.py").read_text()
    assert "_connections_store.take_from_settings(body, current)" in route
    assert route.index("take_from_settings(body, current)") < route.index("_save_settings(current)\n        return current")
    script = (REPO / "static" / "js" / "settings.js").read_text()
    assert "fetch('/api/search/providers'" in script and "|| _hasKey[active]" in script
    assert "if (kf && (keyInput.value.trim() || (_settings[kf] || '').trim())) {" in script


# ── a first message that is a look-up gets its tool, not an answer from memory ──

MEDICINE_QUESTIONS = ["What are the most common side effects of Zepbound, and how common is nausea?", "can I take ibuprofen with lisinopril",
                      "how much tylenol for a 40 lb child", "tell me about semaglutide", "is 800 mg too much", "what is metoprolol used for",
                      "I missed a dose yesterday, what now", "does atorvastatin interact with grapefruit"]


@pytest.mark.parametrize("said, tool_name", [(said, "medicine_info") for said in MEDICINE_QUESTIONS] + [
    ("where is the nearest pharmacy", "find_places"), ("is there a hardware store open now", "find_places"), ("what time does the library close", "find_places"),
    ("is there evidence that creatine helps memory", "paper_search"), ("what does the research say about cold plunges", "paper_search"),
    ("find a meta-analysis on fasting", "paper_search"), ("what is the monthly payment on a 300k loan at 6%", "ask_wolfram"),
])
def test_a_look_up_question_is_recognised(own_store, said, tool_name):
    from src.agent_tools import lookup_tools
    store.save("brave", {"api_key": KEY})
    store.save("wolfram", {"app_id": "MADE-UP-APPID"})
    assert tool_name in lookup_tools.wanted(said)


@pytest.mark.parametrize("said", ["tell me a joke about cats", "what is the capital of Peru", "write a poem about April", "how do I fix a leaky faucet",
                                  "what's the tide schedule in Maine", "I need to study for my exam", "how do I open a jar", "thanks!", "peptide bonds explained simply"])
def test_an_ordinary_message_is_not_taken_for_a_look_up(own_store, said):
    from src.agent_tools import lookup_tools
    store.save("brave", {"api_key": KEY})
    store.save("wolfram", {"app_id": "MADE-UP-APPID"})
    assert lookup_tools.wanted(said) == set()


def test_only_look_ups_that_are_set_up_are_recognised(own_store, monkeypatch):
    from src.agent_tools import lookup_tools
    assert lookup_tools.wanted("where is the nearest pharmacy") == set()               # no Brave key: no places
    assert lookup_tools.wanted("what are the side effects of calmora") == {"medicine_info"}
    store.save("medicines", on=False)
    assert lookup_tools.wanted("what are the side effects of calmora") == set()
    monkeypatch.setenv("AEGIS_CONNECTIONS_DISABLED", "1")
    store.forget_cache()
    assert lookup_tools.wanted("can I take ibuprofen with lisinopril") == set()        # a server without them behaves as it always did


def test_a_first_message_about_a_medicine_is_no_longer_answered_from_memory(own_store):
    from src import agent_loop
    asked = "What are the most common side effects of Calmora, and how common is nausea?"
    intent = agent_loop._classify_agent_request([{"role": "user", "content": asked}], asked)
    assert intent["low_signal"] is False and intent["domains"] == {"lookup"} and intent["lookup_tools"] == {"medicine_info"}
    plain = agent_loop._classify_agent_request([{"role": "user", "content": "tell me a joke about cats"}], "tell me a joke about cats")
    assert plain["low_signal"] is True and plain["lookup_tools"] == set()
    store.save("medicines", on=False)                                                # with the look-up switched off, it is as it was
    off = agent_loop._classify_agent_request([{"role": "user", "content": asked}], asked)
    assert off["low_signal"] is True and off["domains"] == set()
    assert "lookup" not in agent_loop._DOMAIN_TOOL_MAP                               # only the tools asked for are seeded, not all five
    assert '_relevant_tools.update(_intent.get("lookup_tools") or set())' in (REPO / "src" / "agent_loop.py").read_text()


@needs_node
def test_the_full_app_drops_a_models_stray_source_marks_and_nothing_else():
    import re
    from tests.test_markdown_rendering_js import _run_markdown_case

    def words(markdown):
        return re.sub(r"<[^>]+>", "", _run_markdown_case(markdown)).strip()

    assert words("A 2022 study found a link 【1†L1-L4】. A review agreed【2†L1-L3】【3†source】.") == "A 2022 study found a link. A review agreed."
    assert words("日本語の【重要】なお知らせ") == "日本語の【重要】なお知らせ"                    # ordinary brackets are left alone
    assert "【1†L1-L4】" in words("Keep `【1†L1-L4】` in code.")                        # and so is anything written as code
    assert "【1†L1-L4】" in words("```\ncite as 【1†L1-L4】\n```")
