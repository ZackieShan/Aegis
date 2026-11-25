# The daily review

Once a day the server reads the previous day's chats and rates how the assistant did. The result is a
report for the administrators: a score for the day, a score for each kind of assistant, what went wrong
and how often, and a short list of what to change.

It is off until an administrator turns it on, under **Admin → Review** (`/admin#review`).

Everything happens on the server, with its own model. Nothing is sent anywhere.

## What is rated

The unit is an *exchange*: one thing a person asked, and what the assistant did about it — the tools it
ran, what they returned, and its reply. Slash commands are left out (the person ran a command and got its
output), and so are the copies of old messages that a forked chat carries.

Each exchange gets a rating from 1 to 5:

| Rating | Meaning |
|---|---|
| 5 | Did what was asked, fully, in a form that fits the person and the screen |
| 4 | Did it, with a small flaw |
| 3 | Partly: something that matters was missed, or it went wrong first |
| 2 | Mostly failed: no tool where one fits, a tool failure passed over, a refusal |
| 1 | Failed: no answer, something made up, or it said a thing was done that no tool did |

and zero or more problems, in words from a fixed list (`services/review/signals.py`): answered from memory
where a look-up tool fits, unsupported facts, wrong tool, a tool failure passed over, not done, said done
when it was not, misread, refused, too long, unclear, asked instead of looking, the person had to repeat
themselves, stopped, asked again, slow, no reply.

Two things decide a rating.

**The records.** A failed tool, an answer the person stopped or asked for again, a reply that took over a
minute, the next message being a correction: these are in the chat records and need no judgement. They are
counted without a model, and they cap the rating whatever the reviewer says (a correction afterwards
means at most 3).

**The reviewer.** The house's own chat model reads each exchange against the rubric above and answers in
JSON. It sees the request, the tools and their output, the reply, what the person said next and what the
records show. It is the same model as the assistant, so it is not asked whether a fact is true, only
whether the reply is supported by what is on the page. An answer that does not keep to the form is not
used; that exchange is recorded as not rated.

A rating can be off by a point from one run to the next. The counts of problems and the list of what to
change are the dependable part.

## What to change

The report ends with a list, most frequent first, each item marked as a setting an administrator can
change today, work on the assistant, a tool that fails, or speed. Examples:

- a place search had no home to search near → set the household's home town;
- facts were given from memory although `medicine_info` fits → get the tool used;
- a tool failed in several exchanges → look at why;
- the model re-read its whole prompt in the middle of a chat → find what changes at the top of the prompt.

For an administrator's own chats that went badly, the reviewer's suggested fix is listed too.

## Whose chats, and what is kept

A review is one file a day in `data/reviews/` (readable only by the account the app runs as; 120 days
are kept).

- **An administrator's own chats** keep a short excerpt of what was asked, the reviewer's note and its
  suggested fix. These are shown only to that administrator. Another administrator sees those exchanges
  as figures.
- **Every other account's chats** are kept as figures and categories only: the rating, the problems,
  which tools were used, how long it took. Never what was said. The reviewer's free-text note about
  such a chat is discarded before anything is written.
- Any account can be **left out** altogether on the Review page. Its chats are then never shown to the
  reviewer.

The model does read every included chat in order to rate it. It runs on this server, and the review's
requests are kept out of the app's own trace store.

## When it runs

Each day from the set time on (03:30 unless changed, on the house's clock), the day before is reviewed,
once. If the server was off at that time, the review is made when it is next up.

The nightly job gives way to people: it does not start while anyone is using the app, and it stops the
moment someone does, keeping what it had rated. It tries again a quarter of an hour later and carries on
from where it stopped. A screen that is merely left open does not hold it back.

An administrator can also review a day at once with the buttons on the page; that run does not wait.

When a review finds exchanges that need a look, the administrators get a notification on the phones
that have them turned on. This can be set to every day, or never.

## The files, for whoever works on the app

`data/reviews/YYYY-MM-DD.json` holds `totals`, `agents`, `people`, `models`, `problems`, `improve` and
`exchanges`. Reading the last few files is the quickest way to see where the assistant falls short:
`improve` is the ranked list, and the administrators' own exchanges carry `asked`, `note`, `fix` and any
tool `errors`.

Code: `services/review/` (`collect`, `signals`, `judge`, `report`, `run`, `config`),
`routes/review_routes.py`, the Review tab in `static/lite/admin.js`. Tests: `tests/test_chat_review.py`.
