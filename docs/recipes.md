# Recipes — run, schedule, build

Recipes are small workflows that chain your local tools and models together. You
can **run** a ready-made one in a click, **schedule** it as an automation that
delivers results on its own, or **build** your own — no node-wiring required to
get started.

Open Recipes from the sidebar (or the `/recipes` command).

![Run a recipe, schedule it, then build one from a description](media/recipes-howto.gif)

---

## Run a recipe

Recipes opens to a **library** of one-click workflows, grouped by what they do —
Analyze, Communicate, Create, Research, Monitor. Search or pick a category, then
click a card.

![The Recipes library of one-click workflows](media/recipes-library.png)

Each card opens a simple panel: the sample input is pre-filled and the expected
output is shown. Type your input, hit **Run**, and the result streams in — no
graph, no wiring.

Some recipes need a toolbox (e.g. *Stock analysis — Bull / Base / Bear* needs the
Market tools). If it's off, the card shows an **Enable** button; one click turns
the toolbox on, runs a quick check that its tools respond, and the recipe becomes
runnable. You can turn it back off any time in the MCP panel.

**Who can run them:** anyone signed in can run canned recipes. Building your own
stays admin-only, since a hand-built recipe can call tools and the shell.

---

## Schedule it as an automation

Any recipe can become a **job** that runs itself. In a recipe's run panel, click
**⏰ Automate** and choose:

- **When it runs** — *Daily at* a time, *Every N hours*, a *custom cron*, or
  *When new email arrives*.
- **Where the result goes** — an in-app **notification** (the default), or saved
  to a **document** or **note**.

Your automations live under **⏰ Automations** — each with an on/off switch, its
schedule, the next and last run, a run-now button, and the last result.

![Automations — recipes running on a schedule](media/recipes-automations.png)

A couple of things worth knowing:

- **Email-arrives** automations poll your inbox every few minutes and fire when
  new mail lands; they baseline on the first check, so they never fire on your
  existing backlog.
- The **Inbox declutter** recipe *proposes* unsubscribes ranked by volume, each
  with its unsubscribe link — it never unsubscribes for you. You decide.
- Automations run in-process; disable them with `AEGIS_INPROCESS_TASKS=0` if you
  drive scheduling externally.

---

## Build your own

Click **+ Build your own** to open the editor. It never starts blank — a *Start a
new recipe* card offers three ways in:

![Start a recipe from a template or by describing it](media/recipes-editor.png)

- **From a template** — load any library recipe and tweak it.
- **Describe what you want** — type a plain sentence (e.g. *"Take a support email,
  classify it, and if it's a complaint draft an apology"*) and a local model drafts
  the node graph for you. Edit from there.
- **Blank canvas** — drop nodes from the left palette and wire them yourself.

Nodes come in a few kinds: **input** (the run input), **tool** (calls a tool),
**model** (runs a prompt), **output** (the result), plus **branch** (conditional)
and **loop** (refine). A model node automatically receives every upstream node's
output as context, so you rarely need placeholders — when you do, `{{input}}` is
the run input and `{{nodeId}}` is another node's output.

Not sure what a workflow does? Hit **Explain** for a plain-English summary
generated from the graph. **Save** to keep it (it then shows up in *Open…*), and
**Run** to try it with an input.

---

## Under the hood

Recipes execute server-side in topological order; every node runs against your
local models and tools, and nothing leaves the machine. Canned recipes are a
curated catalog; automations are stored as JSON under `data/jobs/` and fired by a
small poll loop. See the source in `src/recipes.py`, `src/recipe_templates.py`,
`src/jobs.py`, and `src/recipe_authoring.py`.
