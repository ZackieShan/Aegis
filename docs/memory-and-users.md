# Memory, profiles and more than one user

Aegis remembers who it is talking to. Each account has its own memories and
its own **profile** — a standing picture of that person written from those
memories — and nothing crosses from one account to another.

- [What is remembered](#what-is-remembered)
- [The profile](#the-profile)
- [Nobody mode and the off switches](#nobody-mode-and-the-off-switches)
- [`/me`](#me)
- [More than one user](#more-than-one-user)
- [API](#api)
- [How it works](#how-it-works)

## What is remembered

**Memories** are single facts: *"Keeps bees: three hives"*, *"Prefers metric
units"*, *"Allergic to penicillin"*. They come from three places:

| Source | When |
|---|---|
| Automatic extraction | In the background, every second exchange of a chat, and straight away when you say something like *"remember that…"*, *"don't forget…"*, *"from now on…"*. The utility model picks out at most five lasting facts per pass. |
| You | **Brain** in the sidebar, `/memory add …`, or `/me add …`. |
| The agent | The `manage_memory` tool in agent mode (administrators). |

Each memory has a category: `identity`, `preference`, `interest`, `contact`,
`project`, `goal`, `fact`, `event`, `task`. `interest` is for what you are
into — a hobby, a field, a subject you keep working on. One question about a
topic does not make it an interest; what you say you do, own, practise or
plan does.

On any one turn only a handful of memories are recalled — the ones that match
what you just asked. That is what the profile is for.

## The profile

The profile is the whole picture, given to the model on **every** turn:

```
Who you are talking to: what has been learned about the user so far. …
- Dana is an ER nurse who works nights and lives outside Bozeman. She keeps bees and bakes sourdough.
- About them: Name: Dana Whitlock; ER nurse (night shifts); Allergic to penicillin
- Interests: Beekeeping, Sourdough baking
- Preferences: Prefers short, practical answers; Prefers metric units
- People: Reid – husband; two kids
- Projects: Restoring a 1984 Toyota pickup with dad
- Goals: Ham radio Technician license this year
- Beekeeping: Keeps three hives; Uses oxalic acid vapor; Lost one hive to varroa last winter
- Sourdough baking: Bakes every Sunday; Starter is two months old
```

So a bare "hi" is answered by name, a question about boiling eggs is answered
in metric and nothing else, and a question about amoxicillin starts with the
penicillin allergy.

It is written by the utility model from your memories, in the background,
when at least two new ones have arrived (or one has waited twelve hours), and
no more often than every ten minutes. Until the model has written one — or
if no model is available — it is assembled directly from the memories by
category. It is around 600–800 tokens and identical from turn to turn, so a
local backend keeps it in its prompt cache.

**The profile never outlives its sources.** It records which memories it was
written from. Delete or edit one of them and the written profile is set aside
at once in favour of one assembled from what is left; the model writes a new
one later. To make Aegis forget something, delete the memory — that is enough.

It is handed to the model as untrusted reference data, in the same guarded
block as other saved memories: something to take into account, never
instructions to follow.

## Nobody mode and the off switches

| | Profile and memories used | New things learned | Chat saved |
|---|---|---|---|
| Normal chat | yes | yes | yes |
| **Nobody** (the eye on the welcome screen, or `/incognito`) | no | no | no |
| `/me off` (or the two switches in **Brain**) | no | no | yes |
| Compare mode | no | no | — |

Nobody mode is per chat and is chosen before the first message. In it the
model is told nothing about you, nothing is extracted, and the chat is not
kept. `/me off` is the standing version: memory stays off for your account
until `/me on`, and nothing is deleted.

## `/me`

| Command | Does |
|---|---|
| `/me` | Shows what Aegis knows about you. |
| `/me add <fact>` | Remembers something about you. |
| `/me refresh` | Rewrites the profile from your memories now. |
| `/me off` · `/me on` | Stops · resumes using and learning. Deletes nothing. |
| `/me forget` | Explains, then with `/me forget everything` deletes your profile and all of your memories. |

Aliases: `/whoami`, `/profile`, `/aboutme`. Single memories are edited,
pinned and deleted in **Brain**. A reply that used the profile says so in its
memory pill.

## More than one user

Every account has its own chats, memories, profile, uploads, documents,
notes, calendar, tasks and gallery. An administrator adds accounts under
**Settings → Users → Add User**.

Two things decide whether a new account can chat straight away:

1. **Which model endpoints it can see.** An endpoint is either shared (every
   account sees it) or belongs to whoever added it. Add the endpoints the
   household should use as shared.
2. **Its default model.** Turn on **Settings → Users → Model Defaults → Share
   defaults with users** and accounts without a default of their own inherit
   the global one. Otherwise each user picks a model once.

What a regular account cannot do, whatever it asks the agent: run shell
commands or code on the server, read or write server files, use the
workbench (projects, hosted apps, other machines, hand-offs to coding
agents), manage endpoints, settings, tokens or other users, or use the email
tools. It can chat, use agent mode with the web, the offline library and
photo identification, generate images, do research, and keep its own
documents, notes, calendar and gallery. Per-account switches are under
**Settings → Users** (agent mode, browser, documents, research, images,
memory, allowed models, daily message limit).

### Signing in

Sign-in is rate-limited per client address, and — separately — **per
account**: after five wrong passwords for one account, each further miss
locks that account for 30 seconds, doubling up to 15 minutes, wherever the
attempts come from. A correct sign-in clears the count. This matters behind a
reverse proxy, where every client can appear as one address, and it is what
makes a short password tolerable on a private network. It is still a short
password: use a longer one, or two-factor sign-in, for anything reachable
from the internet. The account file and the session-token file are written
readable by the server's own user only.

The interface asks for at least eight characters when a password is set.

## API

All under `/api/memory`, all scoped to the signed-in account.

| | |
|---|---|
| `GET /profile` | `{profile, markdown, memories, using, learning}` |
| `POST /profile/rebuild` | Rewrite it now. Optional form field `session` (its model is the fallback when no utility model is set). Adds `written_by_model`. |
| `DELETE /profile` | Drop the profile; it is assembled again from the memories on the next turn. |
| `DELETE /profile?memories=true` | Delete the profile **and every memory of this account**. |

The existing memory routes (`GET /api/memory`, `POST /api/memory/add`,
`PUT`/`DELETE /api/memory/{id}`, `POST /api/memory/{id}/pin`, …) are unchanged.

## How it works

| Piece | Where |
|---|---|
| Profile: shape, storage, rebuild rules, prompt block | `services/memory/profile.py` |
| Stored at | `data/profiles/<account>.json`, beside `data/memory.json` |
| Added to each turn | `ChatProcessor.build_context_preface` (`src/chat_processor.py`) |
| On/off per turn (memory switches, Nobody, greetings) | `build_chat_context` (`routes/chat_helpers.py`) |
| Background rebuild, after memory extraction | `run_post_response_tasks` (`routes/chat_helpers.py`) |
| Extraction prompt | `services/memory/memory_extractor.py` |
| Routes | `routes/memory/memory_routes.py` |
| `/me` | `static/js/slashCommands.js` |
| Per-account sign-in slowdown | `FailureBackoff` (`src/rate_limiter.py`), `routes/auth_routes.py` |

The vector index used for recall is one collection shared by all accounts;
results are filtered to the caller's own memories, and the search is widened
in proportion when the index holds other accounts' entries so they cannot
crowd the caller's out.

Not covered yet: scheduled tasks run without the profile, and the profile is
not editable line by line — you change it by changing memories.
