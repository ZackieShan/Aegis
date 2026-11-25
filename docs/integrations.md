# Integrations: outside services the assistant can use

Settings → Integrations has two parts.

**Services** is a fixed list of outside services Aegis knows how to use. Paste a key, press Save, and the
assistant has a new ability. This page is mostly about that list.

**Your own connections** is everything else: email accounts, calendars, contacts, other HTTP APIs
(reached with the `api_call` tool), MCP servers and coding agents.

## One set of keys for the whole house

A key saved under Services belongs to the server, not to a person. Every account's assistant uses it,
on the full app and on the family screen alike. Only an administrator can open Settings → Integrations,
so only an administrator can see that a key is set, replace it, try it or remove it.

Keys never go into the code or into git:

- they are kept in `data/connections.json` (the `data/` folder is git-ignored), readable only by the
  account the app runs as;
- each secret is encrypted with the app's own key (`data/.app_key`) before it is written;
- once saved, a key is never sent back to a browser. The screen shows only that it is set and its last
  four characters.

A key that an older version kept in `settings.json` (the Search settings) keeps working. Saving the key
on the Integrations screen moves it to the encrypted file and blanks the old copy. Keys given through
environment variables (`BRAVE_API_KEY`, `TAVILY_API_KEY`, …) also still work; a key saved on the screen
wins over them.

## The services

| Service | Key | What the assistant gets |
|---|---|---|
| Brave Search | needed | Web search that returns the passages answering the question, the news with dates, and places nearby |
| Tavily, Serper, Google Programmable Search | needed | Other web search providers, usable as the main provider or as fallbacks |
| Medicine labels (openFDA, RxNorm) | not needed | A medicine's official US label, a section at a time |
| Studies and papers (OpenAlex, Crossref) | not needed | Published studies on a question, with abstracts and free full text links |
| Wolfram\|Alpha | needed | Exact maths and curated facts |

The two that need no key work on a fresh install. Their optional keys only raise the daily allowance.

Each card on the screen says what the service costs and what leaves the house when it is used (the
words searched for; for a place search, the town or a position rounded to about a kilometre). A service
can be switched off without removing its key; nothing is sent to it while it is off.

### Brave Search

With a Brave key, and Brave chosen as the search provider (Settings → Search), a web search is one
request to Brave's "LLM context" endpoint. It returns the parts of the top pages that bear on the
question, so Aegis no longer fetches and trims three pages itself. In a trial that took a search from
about three seconds to under one. If the key's plan does not include that endpoint, or Brave has no
passages for a question, the ordinary search runs instead (a list of results, then the pages are read
here).

Brave charges by the request: $5 per 1,000, with $5 of credit each month. Aegis therefore:

- counts every request, by month, in `data/connections_usage.json`, and shows the count and the cost on
  the card;
- stops asking Brave once the month's limit is reached. Searches then fall back to the next provider in
  the chain (DuckDuckGo by default) until the next month;
- starts with the limit at 1,000 a month, which is what Brave gives away, so nothing is spent until an
  administrator raises it (0 means no limit);
- answers an identical request from memory for fifteen minutes, which costs nothing.

Deep research runs many searches per report. Each one counts.

## The tools

Every account has these. A tool whose service is not set up is not offered to the model at all.

| Tool | Asks | For |
|---|---|---|
| `web_search` | the search provider chain | anything on the web |
| `news_search` | Brave's news index, or the public Google News feed without a key | what is happening now or lately, each article with its date |
| `find_places` | Brave's place index | a pharmacy, a restaurant, a shop: address, phone, today's hours, open now, rating |
| `medicine_info` | openFDA drug labels | what a medicine is for, dose, side effects, warnings, interactions |
| `paper_search` | OpenAlex, then Crossref | what the research says |
| `ask_wolfram` | Wolfram\|Alpha | calculation and exact figures |

`find_places` looks near the person's saved home (theirs, else the household's, set in the admin
console), or near a town they name. With no home saved and no town given it asks which town.

`medicine_info` returns one part of the label per call, chosen by `about` (`uses`, `dose`,
`side effects`, `warnings`, `interactions`, `pregnancy`, `overdose`, `storage`, …), and pages a long
section. A misspelt name is corrected through RxNorm. For a medicine sold both over the counter and on
prescription it gives the over-the-counter label unless asked for the other.

When a service cannot answer, the tool says so in words the assistant can pass on and tells it not to
fill the gap from memory. A medicine answer that could not be checked against the label must say so.

## MCP servers for everyone

MCP servers are added under **Your own connections → Add Integration → MCP**. Out of the box their
tools are an administrator's only. Each server's page has a switch, **Everyone at home can use this**,
which opens that one server's tools to every account's assistant.

A shared server works for everyone with whatever account or token it was set up with: if it is
connected to one person's to-do list, everyone's assistant can read and change that list. Servers that
hold one person's mail, memory or documents, or that act on the server itself (email, memory, document
search, image generation, network troubleshooting, the browser) cannot be shared.

## Adding a service

1. Describe it in `services/connections/catalog.py`: its name, what it is for, its fields, where a key
   comes from, what it costs and what it sends.
2. Write the code that calls it, using `services.connections.http.get` (one way to fail, and no key in
   any log) and `connections.value(...)` for the key. Count requests with `connections.count(...)` if
   the service charges for them.
3. Add a line to `services/connections/checks.py` so the Try button can say whether a key works.
4. If it gives the assistant a new tool, register the tool as the others in
   `src/agent_tools/lookup_tools.py` are registered (`tests/test_connections.py` lists every place).

The test suite sets `AEGIS_CONNECTIONS_DISABLED=1`, which hides every key and refuses every request, so
no test reaches a real service.
