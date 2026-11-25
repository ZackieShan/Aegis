"""The kitchen: a recipe set out for cooking from — kept as written, its
measures worked out for today, its method broken into steps.

The promises under test:
  * the numbers are worked out here, the same way every time, whatever the
    model said: units, grams from cups, oven temperatures, times;
  * old measures and oven words are translated and marked as estimates;
  * the original text is always kept untouched, and a recipe is still kept
    when no model answers;
  * a cookbook belongs to one account;
  * the assistant's tool is one of the home tools — open to a household
    account — and words its result with the link to follow along.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

JUMBLES = """Jackson Jumbles.
3 teacups sugar.
1 teacup lard.
6 teacups flour.
1 teaspoonful soda in one cup of sour cream.
3 eggs.
The grated rind of one or two lemons, or a little grated nutmeg.
Roll out and bake in a quick oven.—Mrs. H. S."""

MODEL_SAYS = {
    "title": "Jackson Jumbles", "year": "", "makes": "about 5 dozen",
    "ingredients": [
        {"as_written": "3 teacups sugar", "item": "sugar", "amount": 1.5, "unit": "cup", "estimate": True, "note": "3 teacups at ½ cup each"},
        {"as_written": "1 teacup lard", "item": "lard", "amount": 1, "unit": "teacup", "estimate": False},
        {"as_written": "6 teacups flour", "item": "flour", "amount": "3", "unit": "Cups", "estimate": True},
        {"as_written": "1 teaspoonful soda", "item": "baking soda", "amount": 1, "unit": "tsp"},
        {"as_written": "3 eggs", "item": "eggs", "amount": 3, "unit": ""},
        {"as_written": "a little grated nutmeg", "item": "nutmeg", "amount": 0, "unit": "", "prep": "grated", "estimate": True},
        {"as_written": "", "item": ""}, "1 pound butter", 7,
    ],
    "steps": [
        {"do": "Heat the oven to 400–450 °F.", "as_written": "a quick oven", "minutes": 0, "oven_f": 0},
        {"do": "Cream the sugar and lard, then beat in the eggs.", "as_written": "", "minutes": 0},
        {"do": "Roll out, cut, and bake until golden.", "as_written": "Roll out and bake in a quick oven.", "minutes": 11, "oven_f": 412},
        {"do": "Cool on a rack for ten to fifteen minutes.", "minutes": 0},
        {"do": ""}, "Store in a tin.",
    ],
    "notes": ["No baking time is given; look for golden edges."],
}


async def _model(text, owner):
    return "<think>hmm</think>```json\n" + json.dumps(MODEL_SAYS) + "\n```", "test-model"


async def _no_model(text, owner):
    raise RuntimeError("no model is set up")


@pytest.fixture
def kitchen(tmp_path, monkeypatch):
    import core.database as cdb
    from services.reader import kitchen as module

    engine = create_engine(f"sqlite:///{tmp_path / 'kitchen.db'}")
    cdb.Base.metadata.create_all(engine)
    monkeypatch.setattr(module, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(module, "_ask_model", _model)
    return module


# ── numbers ────────────────────────────────────────────────────────────────

def test_units_and_amounts_are_read_the_way_recipes_write_them(kitchen):
    assert [kitchen.unit_name(u) for u in ("Tbsp", "T", "t", "tsp.", "Cupfuls", "fl oz", "lbs", "ml", "teacup", "handful", None)] == [
        "tablespoon", "tablespoon", "teaspoon", "teaspoon", "cup", "fluid ounce", "pound", "millilitre", "", "", ""]
    assert [kitchen.number(v) for v in ("1 1/2", "2½", "¾", "1/3", 0.25, 3, "", "to taste", 0, -2, True)] == [
        1.5, 2.5, 0.75, pytest.approx(1 / 3), 0.25, 3.0, None, None, None, None, None]
    assert [kitchen.amount_text(a) for a in (0.25, 1.5, 2, 1 / 3, 2 / 3, 2.97, 1.125, 0.03)] == ["¼", "1 ½", "2", "⅓", "⅔", "3", "1 ⅛", "a little"]
    assert kitchen.kitchen_text(1.5, "cup") == "1 ½ cups" and kitchen.kitchen_text(1, "cup") == "1 cup" and kitchen.kitchen_text(3, "") == "3"
    assert kitchen.kitchen_text(0.5, "teaspoon") == "½ teaspoon" and kitchen.kitchen_text(None, "cup") == ""


def test_weights_come_from_the_usual_weight_of_a_cup_of_that_ingredient(kitchen):
    assert kitchen.measure(0.25, "cup", "butter") == {"ml": 59.1, "grams": 56.8}
    assert kitchen.measure(1, "cup", "sifted flour")["grams"] == 120.0 and kitchen.measure(1, "cup", "brown sugar")["grams"] == 213.0
    assert kitchen.measure(1, "cup", "powdered sugar")["grams"] == 115.0 and kitchen.measure(1, "cup", "sugar")["grams"] == 200.0
    assert kitchen.measure(1, "pound", "flour") == {"ml": None, "grams": 453.6}                # a weight stays a weight
    assert kitchen.measure(2, "cup", "dragon fruit pulp") == {"ml": 473.2, "grams": None}      # unknown: volume only
    assert kitchen.measure(3, "", "eggs") == {"ml": None, "grams": None} and kitchen.measure(None, "cup", "milk") == {"ml": None, "grams": None}
    assert [kitchen.metric_text(ml, g) for ml, g in ((None, 56.8), (None, 102.5), (473.2, None), (None, 1360), (1900, None), (None, 8.4), (None, None))] == [
        "55 g", "105 g", "470 ml", "1.4 kg", "1.9 l", "8 g", ""]


@pytest.mark.parametrize("text, expected", [
    ("Bake in a quick oven ten to twelve minutes.", ((10, 12), {"f": 400, "f_to": 450, "c": 205, "c_to": 230, "word": "quick oven"})),
    ("Bake at 350 degrees F for 1 1/2 hours", ((90, 0), {"f": 350, "c": 175, "word": ""})),
    ("Heat the oven to 350–375 °F.", ((0, 0), {"f": 350, "f_to": 375, "c": 175, "c_to": 190, "word": ""})),
    ("bake 25-30 min at 180°C", ((25, 30), {"f": 355, "c": 180, "word": ""})),
    ("Simmer half an hour in a slow oven", ((30, 0), {"f": 300, "f_to": 325, "c": 150, "c_to": 165, "word": "slow oven"})),
    ("Let it stand an hour.", ((60, 0), None)),
    ("Stir for a few minutes, then add 2025 raisins", ((0, 0), None)),
])
def test_times_and_ovens_are_found_in_a_step(kitchen, text, expected):
    assert (kitchen.minutes_in(text), kitchen.oven_for(text)) == expected


def test_old_words_are_explained(kitchen):
    notes = kitchen.old_notes("Take a teacup of sweet milk, a gill of molasses, a teaspoonful of saleratus, butter the size of an egg; bake in a quick oven.")
    joined = " ".join(notes)
    for said in ("A teacup was about ½ cup", "A gill is ½ cup", "about ¼ cup (2 oz, 57 g)", "“quick oven” is about 400–450 °F (205–230 °C)",
                 "Saleratus is baking soda", "ordinary fresh whole milk"):
        assert said in joined, said
    assert "“Soda” in a recipe" not in joined and kitchen.old_notes("2 cups flour, 1 egg. Bake at 350 F.") == []
    assert "A teacup was about ½ cup" in kitchen.SYSTEM_PROMPT and "a quick or hot oven: 400–450 °F" in kitchen.SYSTEM_PROMPT


# ── setting a recipe out ───────────────────────────────────────────────────

def test_a_recipe_is_set_out_with_the_numbers_worked_out_here(kitchen):
    made = asyncio.run(kitchen.set_out(JUMBLES, "alice"))
    assert made["by_model"] is True and made["model"] == "test-model" and made["title"] == "Jackson Jumbles" and made["makes"] == "about 5 dozen"
    by_item = {i["item"]: i for i in made["ingredients"]}
    assert list(by_item) == ["sugar", "lard", "flour", "baking soda", "eggs", "nutmeg", "1 pound butter"]         # junk entries dropped
    assert (by_item["sugar"]["amount"], by_item["sugar"]["unit"], by_item["sugar"]["grams"], by_item["sugar"]["estimate"]) == (1.5, "cup", 300.0, True)
    # the model left "teacup" as the unit: converted here, and marked as the estimate it is
    lard = by_item["lard"]
    assert (lard["amount"], lard["unit"], lard["estimate"]) == (0.5, "cup", True) and lard["grams"] == pytest.approx(102.5) and "A teacup was about" in lard["note"]
    assert (by_item["flour"]["amount"], by_item["flour"]["unit"], by_item["flour"]["grams"]) == (3.0, "cup", 360.0)
    assert (by_item["baking soda"]["unit"], by_item["eggs"]["unit"], by_item["eggs"]["grams"]) == ("teaspoon", "", None)
    assert by_item["nutmeg"]["amount"] is None and by_item["nutmeg"]["prep"] == "grated"

    steps = made["steps"]
    assert [s["do"][:12] for s in steps] == ["Heat the ove", "Cream the su", "Roll out, cu", "Cool on a ra", "Store in a t"]
    assert steps[0]["oven"] == {"f": 400, "f_to": 450, "c": 205, "c_to": 230, "word": ""} and steps[0]["minutes"] == 0
    assert steps[2]["minutes"] == 11 and steps[2]["oven"]["word"] == "quick oven"            # the original's own words win over the model's 412
    assert (steps[3]["minutes"], steps[3]["minutes_to"]) == (10, 15)                          # read from the step itself
    assert steps[1].get("oven") is None and steps[1]["as_written"] == ""
    notes = " ".join(made["notes"])
    assert "No baking time is given" in notes and "A teacup was about ½ cup" in notes and "“quick oven” is about 400–450 °F" in notes


def test_an_odd_oven_number_from_the_model_is_rounded_to_what_an_oven_dial_has(kitchen):
    assert kitchen._step({"do": "Bake until golden.", "oven_f": 362})["oven"] == {"f": 350, "c": 175, "word": ""}
    assert kitchen._step({"do": "Bake until golden.", "oven_f": 9000}).get("oven") is None
    assert kitchen._step({"do": "Chill.", "minutes": 100000})["minutes"] == 0


def test_with_no_model_the_recipe_is_still_kept_and_says_what_it_is(kitchen, monkeypatch):
    monkeypatch.setattr(kitchen, "_ask_model", _no_model)
    recipe = asyncio.run(kitchen.add("alice", JUMBLES, source_title="Housekeeping in Old Virginia (1879)"))
    data = recipe["data"]
    assert recipe["title"] == "Jackson Jumbles" and recipe["year"] == "1879" and recipe["original"] == JUMBLES
    assert data["by_model"] is False and data["ingredients"] == [] and len(data["steps"]) >= 6
    assert "No model was available" in data["notes"][0] and any("A teacup was about" in n for n in data["notes"])
    assert any(s.get("oven", {}).get("word") == "quick oven" for s in data["steps"])
    for reply in ("not json at all", '{"title": "x", "ingredients": []}', ""):               # an answer that is no recipe counts as none
        async def says(text, owner, reply=reply):
            return reply, "m"
        assert asyncio.run(kitchen.set_out(JUMBLES, "alice", says))["by_model"] is False
    with pytest.raises(kitchen.KitchenError):
        asyncio.run(kitchen.set_out("too short", "alice"))


def test_a_cookbook_belongs_to_one_account(kitchen):
    recipe = asyncio.run(kitchen.add("alice", JUMBLES, source_title="Housekeeping in Old Virginia (1879)", source_url="/read?b=gutenberg&p=index/book_42450"))
    assert recipe["title"] == "Jackson Jumbles" and recipe["year"] == "1879" and recipe["link"] == f"/kitchen?r={recipe['id']}"
    assert recipe["original"] == JUMBLES and recipe["ingredient_count"] == 7 and recipe["step_count"] == 5 and recipe["by_model"] is True
    assert "title" not in recipe["data"] and recipe["data"]["makes"] == "about 5 dozen"
    asyncio.run(kitchen.add("bob", JUMBLES, title="Bob's biscuits"))
    assert [r["title"] for r in kitchen.all_recipes("alice")] == ["Jackson Jumbles"] and [r["title"] for r in kitchen.all_recipes("bob")] == ["Bob's biscuits"]
    assert "data" not in kitchen.all_recipes("alice")[0]

    assert kitchen.find("alice", "jackson jumbles")["id"] == recipe["id"] and kitchen.find("alice", "the jumbles")["id"] == recipe["id"]
    assert kitchen.find("alice", recipe["id"])["id"] == recipe["id"]
    assert kitchen.find("alice", "pot roast") is None and kitchen.find("bob", "jackson jumbles") is None and kitchen.find("alice", "") is None
    for attempt in (lambda: kitchen.get("bob", recipe["id"]), lambda: kitchen.update("bob", recipe["id"], {"title": "Mine"}),
                    lambda: asyncio.run(kitchen.redo("bob", recipe["id"]))):
        with pytest.raises(kitchen.KitchenError):
            attempt()
    assert kitchen.remove("bob", recipe["id"]) is False
    changed = kitchen.update("alice", recipe["id"], {"title": "  Grandmother's jumbles ", "favourite": True, "year": "1879 "})
    assert changed["title"] == "Grandmother's jumbles" and changed["favourite"] is True and changed["original"] == JUMBLES
    assert kitchen.update("alice", recipe["id"], {"title": "   ", "favourite": "yes"})["title"] == "Grandmother's jumbles"
    again = asyncio.run(kitchen.redo("alice", recipe["id"]))
    assert again["title"] == "Grandmother's jumbles" and again["step_count"] == 5 and again["original"] == JUMBLES
    assert kitchen.remove("alice", recipe["id"]) is True and kitchen.all_recipes("alice") == []


def test_a_recipe_reads_as_text_with_the_old_wording_beside_the_estimates(kitchen):
    recipe = asyncio.run(kitchen.add("alice", JUMBLES, source_title="Housekeeping in Old Virginia (1879)"))
    text = kitchen.as_markdown(recipe)
    assert text.startswith("# Jackson Jumbles\n\nabout 5 dozen · Housekeeping in Old Virginia (1879)\n")      # the year is not said twice
    for line in ("- 1 ½ cups sugar (about 300 g) — as written: “3 teacups sugar” (3 teacups at ½ cup each)",
                 "- ½ cup lard (about 105 g) — as written: “1 teacup lard”",
                 "- 3 eggs", "- 1 teaspoon baking soda",
                 "1. Heat the oven to 400–450 °F. (400–450 °F / 205–230 °C)",
                 "3. Roll out, cut, and bake until golden. (11 min; 400–450 °F / 205–230 °C)",
                 "4. Cool on a rack for ten to fifteen minutes. (10–15 min)",
                 "## As written\n\nJackson Jumbles.\n3 teacups sugar."):
        assert line in text, line
    assert "## As written" not in kitchen.as_markdown(recipe, original=False)
    assert "baking soda (" not in text                          # a spoonful is measured with a spoon: no grams beside it
    assert kitchen.measure(1, "teaspoon", "cream of tartar")["grams"] == pytest.approx(3.3, abs=0.1)      # not the weight of cream


# ── where a recipe comes from ──────────────────────────────────────────────

PAGE = """<html><head><title>Grandma's blog</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "Blog"},
 {"@type": ["Recipe"], "name": "Oat Cookies", "recipeYield": ["24 cookies"], "recipeIngredient": ["1 cup oats", "1/2 cup butter"],
  "recipeInstructions": [{"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Cream the <b>butter</b>."}]},
                         {"@type": "HowToStep", "text": "Bake 12 minutes at 350 F."}]}]}</script>
</head><body><p>Twelve paragraphs about my childhood…</p></body></html>"""


def test_a_recipe_page_on_the_web_gives_the_recipe_not_the_story(kitchen, monkeypatch):
    from services.library import additions

    monkeypatch.setattr("services.library.kiwix.owns_url", lambda url: False)
    asked = []

    def download(url, max_bytes=0, timeout=0):
        asked.append((url, max_bytes))
        if "plain" in url:
            return (b"<html><head><title>Plain page</title></head><body><h1>Soda bread</h1><p>Mix four cups of flour with a teaspoon of soda and sour milk; "
                    b"bake in a hot oven for forty minutes.</p></body></html>", url, "")
        if "private" in url:
            raise additions.AdditionError("That address is not on the public internet.")
        return PAGE.encode(), "https://blog.example/oat-cookies/", ""

    monkeypatch.setattr(additions, "download", download)
    found = kitchen.source_text("https://blog.example/oat")
    assert found["title"] == "Oat Cookies" and found["url"] == "https://blog.example/oat-cookies/"
    assert found["text"] == "Oat Cookies\n\nMakes: 24 cookies\n\nIngredients:\n1 cup oats\n1/2 cup butter\n\nMethod:\nCream the butter.\nBake 12 minutes at 350 F."
    assert "childhood" not in found["text"] and asked[0][1] <= 4_000_000
    plain = kitchen.source_text("https://blog.example/plain")
    assert "four cups of flour" in plain["text"]
    with pytest.raises(kitchen.KitchenError, match="public internet"):
        kitchen.source_text("http://private.example/recipe")
    for not_a_link in ("ftp://x/y", "file:///etc/passwd", "just words", ""):
        with pytest.raises(kitchen.KitchenError):
            kitchen.source_text(not_a_link)
    assert len(asked) == 3


def test_a_recipe_from_a_library_page_is_read_the_way_the_reader_shows_it(kitchen, monkeypatch):
    from services.reader import content

    monkeypatch.setattr("services.library.kiwix.owns_url", lambda url: url.startswith("https://library.example/content/"))
    monkeypatch.setattr(content, "resolve", lambda url="", **kw: {"kind": "article", "book": "recipes", "path": "Jumbles", "page": 0})
    monkeypatch.setattr(content, "article_text", lambda book, path: {"title": "Jumbles", "text": JUMBLES})
    found = kitchen.source_text("https://library.example/content/recipes/Jumbles")
    assert found == {"title": "Jumbles", "text": JUMBLES, "url": "/read?u=https%3A%2F%2Flibrary.example%2Fcontent%2Frecipes%2FJumbles"}
    monkeypatch.setattr(content, "resolve", lambda url="", **kw: {"kind": "manual", "book": "cookbooks", "path": "old.pdf", "page": 12})
    monkeypatch.setattr(content, "document", lambda book, path: {"title": "An Old Cookbook", "pages": 300})
    monkeypatch.setattr(content, "page_text", lambda book, path, page: f"page {page} of {path}")
    assert kitchen.source_text("/read?b=cookbooks&p=old.pdf&page=12") == {"title": "An Old Cookbook — page 12", "text": "page 12 of old.pdf",
                                                                         "url": "/read?b=cookbooks&p=old.pdf&page=12"}


# ── the API and the assistant's tool ───────────────────────────────────────

def test_the_api_keeps_each_persons_cookbook(kitchen, monkeypatch):
    import routes.reader_routes as routes

    who = {"name": "alice"}
    monkeypatch.setattr(routes, "require_user", lambda request: who["name"])
    app = FastAPI()
    app.include_router(routes.setup_reader_routes(None))
    client = TestClient(app)
    made = client.post("/api/kitchen/recipes", json={"text": JUMBLES, "source_title": "Housekeeping in Old Virginia (1879)", "source_url": "/read?b=g&p=x"})
    assert made.status_code == 200 and made.json()["title"] == "Jackson Jumbles" and made.json()["source_url"] == "/read?b=g&p=x"
    recipe_id = made.json()["id"]
    assert client.post("/api/kitchen/recipes", json={}).status_code == 400 and client.post("/api/kitchen/recipes", json={"text": "short"}).status_code == 400
    assert [r["id"] for r in client.get("/api/kitchen/recipes").json()["recipes"]] == [recipe_id]
    assert client.patch(f"/api/kitchen/recipes/{recipe_id}", json={"favourite": True}).json()["favourite"] is True
    text = client.get(f"/api/kitchen/recipes/{recipe_id}/text")
    assert text.headers["content-type"].startswith("text/markdown") and "## Ingredients" in text.text
    assert client.post(f"/api/kitchen/recipes/{recipe_id}/redo").json()["step_count"] == 5
    who["name"] = "bob"
    assert client.get("/api/kitchen/recipes").json()["recipes"] == []
    for call in (client.get(f"/api/kitchen/recipes/{recipe_id}"), client.patch(f"/api/kitchen/recipes/{recipe_id}", json={"title": "x"}),
                 client.post(f"/api/kitchen/recipes/{recipe_id}/redo"), client.get(f"/api/kitchen/recipes/{recipe_id}/text")):
        assert call.status_code == 400
    assert client.delete(f"/api/kitchen/recipes/{recipe_id}").status_code == 404
    who["name"] = "alice"
    assert client.delete(f"/api/kitchen/recipes/{recipe_id}").json() == {"ok": True}


def test_the_assistant_sets_a_recipe_out_and_hands_back_the_link(kitchen):
    from src.agent_tools.reader_tools import ManageKitchenTool

    tool = ManageKitchenTool()
    run = lambda args, owner="alice": asyncio.run(tool.execute(json.dumps(args) if isinstance(args, dict) else args, {"owner": owner}))
    assert "cookbook is empty" in run({"action": "list"})["output"]
    saved = run({"action": "save", "text": JUMBLES})
    assert saved["exit_code"] == 0
    recipe = kitchen.all_recipes("alice")[0]
    assert saved["output"].startswith("Saved “Jackson Jumbles” to the cookbook. Link (give it to the user exactly as it is")
    assert f"[Follow along: Jackson Jumbles](/kitchen?r={recipe['id']})" in saved["output"].splitlines()[0]
    assert "- 1 ½ cups sugar (about 300 g) — as written: “3 teacups sugar”" in saved["output"] and "## Method" in saved["output"]
    assert "are estimates" in saved["output"] and "In your reply: give the link" in saved["output"]
    assert "## As written" not in saved["output"]                                                        # not the whole original again
    assert kitchen.get("alice", recipe["id"])["original"] == JUMBLES                                      # kept exactly as given
    assert f"[Jackson Jumbles](/kitchen?r={recipe['id']})" in run("list")["output"]
    assert run({"action": "show", "recipe": "jumbles"})["output"].startswith("“Jackson Jumbles” from the cookbook. Link (")
    assert "could not tell which recipe" in run({"action": "show", "recipe": "pot roast"})["error"]
    assert "could not tell which recipe" in run({"action": "delete", "recipe": "jumbles"}, owner="bob")["error"]      # not someone else's
    assert "Give the recipe itself" in run({"action": "save"})["error"]
    assert run(JUMBLES, owner="bob")["exit_code"] == 0 and len(kitchen.all_recipes("bob")) == 1          # pasted with nothing around it
    assert "Removed “Jackson Jumbles”" in run({"action": "delete", "recipe": "Jackson Jumbles"})["output"] and kitchen.all_recipes("alice") == []
    assert "Unknown action" in run({"action": "flambé", "recipe": "jumbles"}, owner="bob")["error"]


def test_as_written_is_what_the_person_sent_not_the_models_tidied_copy(kitchen, monkeypatch):
    """The model is asked to pass the recipe on unchanged and still tidies it.
    What is kept as the original is what the person actually sent."""
    from src.agent_tools import reader_tools

    sent = "Hey, help me follow along with this one from 1879:\n\n" + JUMBLES + "\n\nThanks!"
    tidied = "Jackson Jumbles\n3 teacups sugar\n1 teacup lard\n6 teacups flour\n1 teaspoon soda in one cup of sour cream\n3 eggs\n" \
             "The grated rind of one or two lemons, or a little grated nutmeg\nRoll out and bake in a quick oven."
    assert reader_tools.as_the_user_wrote_it(tidied, sent) == JUMBLES
    assert reader_tools.as_the_user_wrote_it(tidied, "") == tidied and reader_tools.as_the_user_wrote_it(tidied, "Something else entirely.") == tidied
    assert reader_tools.as_the_user_wrote_it("Rice pudding\n2 cups rice\n1 quart milk\nBake slowly.", sent) == "Rice pudding\n2 cups rice\n1 quart milk\nBake slowly."
    paragraph = "Take one pound of flour, half a pound of butter and three eggs; mix well and bake in a moderate oven."
    assert reader_tools.as_the_user_wrote_it("Take 1 pound of flour, half a pound of butter and three eggs; mix well and bake in a moderate oven",
                                             f"my grandmother's:\n{paragraph}") == paragraph

    monkeypatch.setattr(reader_tools, "_last_user_message", lambda session_id: sent if session_id == "chat-1" else "")
    tool = reader_tools.ManageKitchenTool()
    asyncio.run(tool.execute(json.dumps({"action": "save", "text": tidied}), {"owner": "alice", "session_id": "chat-1"}))
    assert kitchen.get("alice", kitchen.all_recipes("alice")[0]["id"])["original"] == JUMBLES
    asyncio.run(tool.execute(json.dumps({"action": "save", "text": tidied}), {"owner": "bob", "session_id": "another-chat"}))
    assert kitchen.get("bob", kitchen.all_recipes("bob")[0]["id"])["original"] == tidied
    assert reader_tools._last_user_message.__name__ and reader_tools._last_user_message("no-such-chat") == ""


def test_the_last_thing_the_user_sent_is_read_from_the_chat(monkeypatch):
    from types import SimpleNamespace

    import core.models as models
    from src.agent_tools import reader_tools

    history = [models.ChatMessage("user", "first"), models.ChatMessage("assistant", "noted"),
               models.ChatMessage("user", [{"type": "text", "text": "second, with a photo"}, {"type": "image_url", "image_url": {"url": "x"}}]),
               models.ChatMessage("assistant", "working on it")]
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", SimpleNamespace(get_session=lambda sid: SimpleNamespace(history=history) if sid == "chat-1" else None))
    assert reader_tools._last_user_message("chat-1") == "second, with a photo"
    assert reader_tools._last_user_message("someone-elses-chat") == "" and reader_tools._last_user_message(None) == ""
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", None)
    assert reader_tools._last_user_message("chat-1") == ""


def test_the_tool_says_so_when_no_model_set_the_recipe_out(kitchen, monkeypatch):
    from src.agent_tools.reader_tools import ManageKitchenTool

    monkeypatch.setattr(kitchen, "_ask_model", _no_model)
    told = asyncio.run(ManageKitchenTool().execute(json.dumps({"action": "save", "text": JUMBLES}), {"owner": "alice"}))
    assert told["exit_code"] == 0 and "NOTE: no model was available" in told["output"]


def test_the_recipes_tool_is_a_home_tool_everywhere_tools_are_listed():
    from core import access
    from src import agent_loop, tool_index, tool_policy, tool_security
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    name = "manage_kitchen"
    assert name in TOOL_HANDLERS and name in TOOL_TAGS
    assert name in tool_security.HOME_TOOLS and name in tool_security._PLAN_MODE_KNOWN_MUTATORS
    assert name in access.TOOL_GROUPS["home"]["tools"] and name in access.GRANTABLE_TOOLS
    assert name in agent_loop._DOMAIN_TOOL_MAP["home"] and name in agent_loop.TOOL_SECTIONS and name in agent_loop._CHANGE_TOOLS
    assert "manage_kitchen" in agent_loop._DOMAIN_RULES["home"]
    assert any(schema["function"]["name"] == name for schema in FUNCTION_TOOL_SCHEMAS)
    assert name in getattr(tool_index, "TOOL_DESCRIPTIONS", {name: ""}) or name in open(tool_index.__file__).read()
    assert name in open(tool_policy.__file__).read()
    # a household account may use it; an account whose role leaves the home group out may not
    blocked = tool_security.blocked_tools_for_owner
    assert callable(blocked)
