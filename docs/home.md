# Home: the simple screen

For someone who wants help with everyday things and does not want an "AI
workspace": one screen on their phone with a chat box and four things —
lists, reminders, trackers and boards. No models, modes, agents or settings.

An administrator gives a person this screen by giving them the **Household**
role, or by choosing *Simple home screen* for them under
[People and access](memory-and-users.md#people-roles-and-each-persons-app).
From then on signing in lands them on `/home`. Anyone else can open `/home`
too; it works alongside the full app on the same data.

<p>
  <img src="media/home-screen.png" alt="The home screen" width="210">
  <img src="media/home-chat.png" alt="Chat" width="210">
  <img src="media/home-list.png" alt="A list" width="210">
  <img src="media/home-trackers.png" alt="Trackers" width="210">
</p>

## What it does

**Chat.** They say what they need in their own words:

> *Add bread and butter to my groceries, and remind me to call the vet tomorrow at 3.*
> *I drank two glasses of water — start tracking that, eight a day.*
> *What's on my packing list?* · *Help me plan Emma's birthday.*
> *Find ideas for a cozy reading nook and pin the best ones.*

The assistant does it — it does not explain how — and answers in a sentence,
with a button to open whatever it just changed. It is told who it is talking
to and how (their name, the note the administrator wrote about them), to use
plain language, and never to talk about how it works.

**Lists and plans.** Shopping, packing, to-do. Tick things off, add several
at once ("milk, eggs, coffee"), clear what is done. A *plan* is a list for
getting something done, usually written by the assistant from a goal.

**Reminders.** A line and a time, once or every day / week / month / year.
*In an hour*, *This evening*, *Tomorrow morning* are one tap. Done marks a
one-off finished and moves a repeating one to its next time.

**Trackers.** Follow something day by day: a habit ticked off, an amount that
adds up toward a daily goal, or a rating from 1 to 5. Each shows today, the
last two weeks and how many days in a row.

**Boards.** Pictures, links and notes collected around one idea. Add a photo
from the phone, paste a link (its title and picture are fetched), or write a
note. *Find ideas* asks the assistant to search and pin for you.

**What it knows about me** opens [`/mind`](memory-and-users.md#seeing-tracing-and-changing-it-mind):
the same full view of their memory everyone gets, with everything editable.

## Reminders on the phone

Under the round button at the top right: **Turn on notifications**. After
that, reminders appear on that phone with the app closed.

- On an iPhone the page has to be added to the Home Screen first (Share →
  *Add to Home Screen*) and opened from there.
- It needs the internet at the moment the reminder fires: the message goes
  through the phone maker's notification service (Google, Apple, Mozilla),
  sealed so that only the phone can read it. With no internet the reminder
  still shows inside the app the next time it is opened.
- The server signs each message with its own key (`data/push_vapid.json`,
  created on first use). The contact address in that signature is the
  `push_contact` setting — a placeholder unless you set one.
- Without notifications turned on, reminders show under *Coming up* and as a
  banner while the app is open, and still go out through whatever the server
  is set up for (Settings → reminders: e-mail, ntfy, a webhook).

## How it fits with the rest

- **Lists and reminders are notes.** A list is a checklist note; a reminder
  is a note with a due time. They show up in the full app's Notes, and
  reminders ride on its existing delivery. Repeating reminders now move to
  their next time on the server, so they keep coming with no browser open.
  Repeats follow the server's clock, so a daily 8:00 stays 8:00 across a
  daylight-saving change.
- **Trackers and boards** have their own tables. Pictures are resized and
  stored in `data/home/pins/<account>/`, and served only to their owner.
- **Links are fetched safely**: through the same guarded fetcher the web
  tools use — public addresses only, re-checked on every redirect, size
  capped. Offline, a link is pinned without its picture.
- **Everything is per account.** One person's lists, reminders, trackers and
  boards are invisible to everyone else, administrators included.

## The assistant's tools

Four tools, available to any account whose role includes the *home* group
(Member and Household do; Guest does not):

| Tool | Does |
|---|---|
| `manage_lists` | `add` (creates the list if needed), `check`, `uncheck`, `remove`, `show`, `list`, `clear_done`, `rename`, `delete` |
| `manage_reminders` | `add` (`when` in words or ISO, optional `repeat`), `list`, `done`, `snooze`, `update`, `delete` |
| `manage_trackers` | `create` (`check` / `number` / `scale`), `log`, `show`, `list`, `delete` |
| `manage_boards` | `pin` (links or a note; creates the board if needed), `create`, `show`, `list`, `delete` |

They find a list, tracker or board by the name the person uses ("the grocery
list" finds *Groceries*). They work in the full app's agent mode as well.

## API

All under `/api/home`, scoped to the signed-in account; the browser sends its
time zone with each request.

| | |
|---|---|
| `GET /start` | name, sections, look, what is due, counts |
| `GET/POST /lists`, `GET/PUT/DELETE /lists/{id}` | lists |
| `POST /lists/{id}/items`, `PUT/DELETE /lists/{id}/items/{item}`, `POST /lists/{id}/clear-done` | items |
| `GET/POST /reminders`, `PUT/DELETE /reminders/{id}`, `POST /reminders/{id}/snooze` | reminders |
| `GET/POST /trackers`, `GET/PUT/DELETE /trackers/{id}`, `POST /trackers/{id}/log` | trackers |
| `GET/POST /boards`, `GET/PUT/DELETE /boards/{id}` | boards |
| `POST /boards/{id}/pins` `{url}` or `{text}`, `POST /boards/{id}/pins/upload` | pin a link, a note, a picture |
| `PUT/DELETE /pins/{id}`, `GET /pins/{id}/image[?thumb=1]` | pins |
| `GET /api/push/key`, `POST /api/push/subscribe`, `/unsubscribe`, `/test` | phone notifications |

## Code

| Piece | Where |
|---|---|
| Lists, reminders, trackers, boards | `services/home/` |
| API | `routes/home_routes.py`, `routes/push_routes.py` |
| Tools | `src/agent_tools/home_tools.py` |
| Notifications | `services/push.py`, `static/lite/home-sw.js` |
| The screen | `static/home.html`, `static/lite/home.js`, `static/lite/home.css` |
| Shared look and helpers for the standalone pages | `static/lite/lite.css`, `static/lite/lite.js` |

Not there yet: sharing a list between two accounts; photos in chat from the
home screen; drag to reorder.
