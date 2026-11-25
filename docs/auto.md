# Auto — how a message gets answered

The mode switch reads **Auto · Code**. Auto is the default (it is the mode
that used to be labelled *Agent*, with a router in front of it). You do not
pick a mode or a model per message; Aegis decides three things from the message
itself, before anything is sent to a model:

| Decision | What happens |
|---|---|
| **The path** | A plain answer, or the agent loop with tools. Plain conversation — a greeting, a thank-you, a poem, "rewrite this: …" — takes the quick lane: no tool prompt, no tool round-trips. Everything else keeps its tools. |
| **The effort** | How hard the model thinks. Low for the quick lane and for short, simple requests ("add milk to my list"); high where the message asks for care or is plainly hard ("think step by step", "debug why…", "pros and cons", a very long message); otherwise the model's own default. |
| **The model** | Unchanged — unless you named one for a kind of work (below) and the endpoint serves it. |

Each reply says how it was answered: `gpt-oss-120b (quick)`, `(careful)`, or
nothing for an ordinary turn. Hover the name for the reason.

No model is asked to make these decisions — they are rules
(`services/router.py`), so routing is instant and cannot wait behind a render.

## Answered with no model at all

Before the router, two things are tried — on a plain turn in Auto, and only
with tools the person is allowed to use:

- **One exact tool answers it.** *When does it get dark tonight*, *will it
  rain tomorrow*, *convert 3 cups to ml*, *how many days until Easter*, *how
  do I say "good morning" in French*, *what's on my shopping list*: the tool
  is called directly and its answer is the reply, in well under a second,
  marked `(instant)`. Anything not read exactly, or that asks two things,
  goes on to the assistant (`services/direct.py`).
- **A job with fixed steps.** Planning an outing and finding something to cook
  each need several tools; they are run as code, in order, with the model
  asked one narrow question on the way (`services/outing.py`,
  `services/dinner.py` — see [Home](home.md#answers-in-the-shape-of-the-thing-asked)).
  Left to the model, steps were skipped one time in three.

## What keeps its tools

Auto errs on the side of tools. A message stays on the agent path when it:

- has a link or an attachment, or web search is switched on;
- is about your own things ("my list", "our trip"), names something kept in the
  app (a file, a picture, a note, a reminder…), or asks for something to be
  done (add, remind, find, open, make, send…);
- depends on the date or on current information (today, latest, weather);
- is a question the offline library or the web could answer;
- is about code or something to build;
- tells the assistant to carry on ("do it", "continue");
- answers a question the assistant just asked — "ok" after "Shall I delete
  them?" is an instruction, not small talk;
- comes from the simple home screen, or is in Code mode, or has a document, an
  e-mail, research or a toolbox open beside the chat;
- is anything else that is not plainly conversation.

**Answer without tools** (Settings → Appearance, administrators only) is the
old *Chat* position of the switch, for the rare chat that should be plain
conversation with a model: Auto never adds tools there, it only keeps small
talk brief. The web is no longer a button either: Auto searches when the
question needs something current, and the answer says where it came from
(*Home library*, *The web, today*).

## Why a follow-up starts quickly

A model that runs at home reads its prompt from the top every time, except
for the part that is byte for byte what it read last time: that part the model
server still has. Reading costs about a second per thousand tokens here, and
an agent prompt opens with thousands of tokens of instructions and tool
descriptions before the first word of the chat. So what leads the prompt is
kept the same from one message to the next:

- **A chat keeps the tools it was offered** (`src/prompt_stability.py`). The
  tools are picked for every message by likeness to it, and the eight nearest
  come back whatever the message says — so the list used to change with almost
  every message, and every message paid the full read. Now a chat's list only
  grows; a tool found by likeness alone joins it only above a floor (0.2,
  where noise scores 0.08–0.17 and a real match 0.2–0.6), while a tool a word
  in the message names is added as before. Nothing is granted this way: what
  is switched off or not allowed is still removed afterwards.
- **What was looked up for one message sits beside that message**, behind the
  conversation: recalled facts, topic notes, earlier conversations, documents,
  a fetched page, search results (`for_this_turn` in `src/prompt_security.py`).
  What stays the same — the profile, pinned facts, the skills index — leads.
- **A local gpt-oss keeps its notes on every tool step of the turn**, as its
  format intends. Stripped, each tool result was read a second time.

Measured on one chat of six messages: a library question with three searches
went from first words after 60 s to 22 s, and a plain follow-up from 4.6 s to
2.3 s before the first sign of the model. A message that needs a tool the chat
has not used yet still costs a full read, and so do the message after a
"search the web for…" and the one after that: for such a message the personal
tools are taken out of sight on purpose, and put back afterwards. The wait is
at least shown honestly now: the model server reports how far it has read and
the screen draws a bar ([Home](home.md#answers-in-the-shape-of-the-thing-asked)).
Each answer records how much was read and how much was still cached
(`read_tokens`, `cached_tokens`). The setting `keep_prompt_stable: false`
turns all three off, to compare or to go back.

## Deep Research, by asking

A Deep Research job reads dozens of pages and takes several minutes, so the
chat does not start one on its own judgment — an ordinary question never
spawns one. It starts one when you ask for it in so many words:

> *Do deep research on rainwater harvesting for a small homestead.*
> *Research the best budget 3D printers for beginners.*
> *Write me a research report on sodium-ion batteries.*

The reply is a link to the job; the report arrives in the Deep Research
window. Talking *about* research ("what does the research say…", "open my
research on…", "is deep research still there?") is not a request. The same
window opens from the **microscope button** beside the message box — whatever
you have typed is carried over as the topic — from **More tools**, and from the
sidebar. The rule is in `services/router.py` (`asks_for_research`).

## Taking the wheel

- **Chat** in the mode switch: plain answers, no tools.
- **Thinking On / Off** in a model's settings (the sliders by the model name)
  pins the effort; Auto then leaves it alone.
- Picking a model in the model picker pins the model, as before.
- To switch Auto off altogether, set `"auto_route": false` in
  `data/settings.json` (or per person, as a user setting). Mode and Thinking
  then mean exactly what they are set to.

## Naming models for kinds of work

One large model on one GPU is the normal case, and swapping it costs many
seconds — so by default Auto changes only the effort. If your endpoint serves
models you want used for particular work, name them in `data/settings.json`:

```json
"auto_models": {
  "code":  "qwen3-coder-next-80b",
  "deep":  "",
  "quick": ""
}
```

- `code` — every turn in Code mode;
- `deep` — turns Auto judges hard;
- `quick` — the quick lane and short simple requests.

A model is used only if the chat's endpoint is known to serve it; otherwise the
turn stays on the model you selected. While a render holds the GPU, chat is
still answered by the CPU model, whatever Auto would have chosen.

## Code

`services/router.py` (`decide()` and its rules), applied in
`routes/chat_routes.py`; `tests/test_router.py`.
