"""The outside services Aegis knows how to use: what each one is for, what it needs and what it costs.

This file is the whole list. It holds no keys — those are typed into Settings → Integrations by an
administrator and kept in the data folder (``store.py``), never in the code. A service listed here
appears on that screen with a place for its key, a link to where the key comes from, and a button
that tries it.

Adding a service is one ``Service`` here, the code that calls it, and a line in ``checks.py`` so the
Try button can say whether the key works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Field:
    """One thing an administrator types in for a service."""
    name: str
    label: str
    secret: bool = True            # masked on screen and encrypted where it is kept
    optional: bool = False
    placeholder: str = ""
    setting: str = ""              # the settings.json key that held it before this screen existed
    env: Tuple[str, ...] = ()      # environment variables also accepted


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    group: str
    does: str                      # what the assistant can do with it, in a line
    fields: Tuple[Field, ...] = ()
    needs_key: bool = True         # False: it works without one
    key_adds: str = ""             # what a key adds when the service works without one
    signup: str = ""               # where a key comes from
    cost: str = ""                 # in plain words
    sends: str = ""                # what leaves the house when it is used
    tools: Tuple[str, ...] = ()    # the tools it powers
    metered: bool = False          # every request is paid for: counted, with a monthly limit
    free_per_month: int = 0        # requests a month the service does not charge for
    price_per_1000: float = 0.0    # US dollars, beyond the free ones

    def field(self, name: str) -> Optional[Field]:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    @property
    def required(self) -> Tuple[Field, ...]:
        return tuple(item for item in self.fields if not item.optional) if self.needs_key else ()


GROUPS: Tuple[Tuple[str, str], ...] = (
    ("search", "Searching the web"),
    ("research", "Looking things up"),
    ("answers", "Exact answers"),
)

_KEY = "api_key"

SERVICES: Tuple[Service, ...] = (
    Service(
        id="brave", name="Brave Search", group="search",
        does="Searches the web and hands the assistant the passages that answer the question, with their sources. "
             "Also the news with dates, and places nearby with their hours and phone numbers.",
        fields=(Field(_KEY, "API key", placeholder="BSA…", setting="brave_api_key", env=("DATA_BRAVE_API_KEY", "BRAVE_API_KEY")),),
        signup="https://api-dashboard.search.brave.com/",
        cost="$5 for 1,000 searches. Brave gives $5 of credit every month, which is the first 1,000.",
        sends="The words searched for. With a place search, the town or position searched near.",
        tools=("web_search", "news_search", "find_places"),
        metered=True, free_per_month=1000, price_per_1000=5.0,
    ),
    Service(
        id="tavily", name="Tavily", group="search",
        does="Another web search. Used when it is chosen as the search provider, or as a fallback.",
        fields=(Field(_KEY, "API key", placeholder="tvly-…", setting="tavily_api_key", env=("TAVILY_API_KEY",)),),
        signup="https://tavily.com/",
        cost="1,000 searches a month free.",
        sends="The words searched for.",
        tools=("web_search",),
    ),
    Service(
        id="serper", name="Serper", group="search",
        does="Google's results through an API. Used when it is chosen as the search provider, or as a fallback.",
        fields=(Field(_KEY, "API key", setting="serper_api_key", env=("SERPER_API_KEY",)),),
        signup="https://serper.dev/",
        cost="2,500 searches free, then paid.",
        sends="The words searched for.",
        tools=("web_search",),
    ),
    Service(
        id="google_pse", name="Google Programmable Search", group="search",
        does="Google's results through your own search engine ID. Used when it is chosen as the search provider.",
        fields=(Field(_KEY, "API key", setting="google_pse_key", env=("GOOGLE_API_KEY",)),
                Field("cx", "Search engine ID", secret=False, setting="google_pse_cx", env=("GOOGLE_PSE_CX",))),
        signup="https://programmablesearchengine.google.com/",
        cost="100 searches a day free.",
        sends="The words searched for.",
        tools=("web_search",),
    ),
    Service(
        id="medicines", name="Medicine labels", group="research",
        does="Looks a medicine up in its official US label: what it is for, the dose, side effects, warnings and "
             "interactions, with a link to the label. Answers about medicines come from there instead of from memory.",
        fields=(Field(_KEY, "openFDA key", optional=True, env=("OPENFDA_API_KEY",)),),
        needs_key=False,
        key_adds="Without a key: 1,000 look-ups a day. With one: 120,000.",
        signup="https://open.fda.gov/apis/authentication/",
        cost="Free.",
        sends="The name of the medicine, to the US Food and Drug Administration and the National Library of Medicine.",
        tools=("medicine_info",),
    ),
    Service(
        id="papers", name="Studies and papers", group="research",
        does="Finds published studies on a question — title, authors, year, how often cited, a summary and a link to "
             "the free full text where there is one.",
        fields=(Field(_KEY, "OpenAlex key", optional=True, env=("OPENALEX_API_KEY",)),),
        needs_key=False,
        key_adds="Without a key the house shares a small daily allowance with others on the same network address "
                 "(about 50 searches), then falls back to Crossref, which has fewer summaries. "
                 "A free key gives the house about 500 searches a day of its own.",
        signup="https://openalex.org/settings/api",
        cost="Free.",
        sends="The words searched for, to OpenAlex or Crossref.",
        tools=("paper_search",),
    ),
    Service(
        id="wolfram", name="Wolfram|Alpha", group="answers",
        does="Works out maths, physics, chemistry, finance and unit questions exactly, and answers factual ones "
             "(distances, populations, nutrition, dates) from curated data.",
        fields=(Field("app_id", "AppID", env=("WOLFRAM_APP_ID", "WOLFRAM_ALPHA_APPID")),),
        signup="https://developer.wolframalpha.com/access",
        cost="2,000 questions a month free.",
        sends="The question asked.",
        tools=("ask_wolfram",),
        metered=True, free_per_month=2000,
    ),
)

_BY_ID: Dict[str, Service] = {service.id: service for service in SERVICES}

# The search providers that are services here: provider name in services/search → service id.
SEARCH_PROVIDERS: Dict[str, str] = {"brave": "brave", "tavily": "tavily", "serper": "serper", "google_pse": "google_pse"}


def get(service_id: str) -> Optional[Service]:
    return _BY_ID.get(str(service_id or ""))


def for_tool(tool: str) -> Tuple[Service, ...]:
    """The services a tool draws on."""
    return tuple(service for service in SERVICES if tool in service.tools)
