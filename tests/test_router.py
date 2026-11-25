"""Auto: how a turn is answered — a plain answer or the agent loop, how hard
to think, and (only where the administrator said so) which model.

The promises under test:
  * tools are never taken away from a message that might need one — only
    plain conversation takes the quick lane;
  * a turn the person put in plain chat is never moved to the agent;
  * "ok" is small talk after a statement and an instruction after a question;
  * Thinking set by hand is left alone;
  * a model is swapped only when named in settings and served by the endpoint.
"""
import pytest

from services import router
from services.router import Turn, decide


def route(message, **turn):
    turn.setdefault("library_ready", True)
    return decide(Turn(message=message, **turn))


# ── the quick lane ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "hi", "Hello!", "hey there", "good morning Aegis", "thanks!", "Thank you so much", "thx", "how are you?", "lol", "bye", "you're the best",
    "write me a haiku about autumn", "give me 5 names for a sourdough starter", "tell me a joke", "brainstorm ideas for a rainy afternoon",
    "come up with some ideas for a birthday surprise", "tell me a fun fact", "suggest a name for a black kitten",
    "can you write a toast for a retirement party", "please write a short poem about the sea",
    "rewrite this to sound friendlier: Your payment is late and we need it now.",
    "translate this: where is the nearest pharmacy and is it open",
    "fix the typos: teh quick brwon fox jumsp over the lazy dog",
    "summarize this: The email said the calendar event moved to Friday and the list of tasks changed.",
    "translate that to Spanish", "make it shorter", "rephrase your last answer",
])
def test_plain_conversation_is_answered_directly_and_briefly(message):
    found = route(message)
    assert (found.path, found.effort, found.label) == ("chat", "low", "quick"), found.reasons
    assert "plain conversation" in found.reasons[0]


def test_translation_of_words_at_hand_is_quick_and_other_translation_goes_to_the_translator():
    """Text that is in the message (or just above it) is reworked by the chat
    model directly; a question about another language uses the translate tool."""
    for message in ("translate that to Spanish", "translate this: where is the nearest pharmacy and is it open",
                    "translate it into French please"):
        assert route(message).path == "chat", message
    from services.router import _hint_tools

    for message in ("how do you say thank you in Japanese", "what does this say in English", "I need a translator for a letter"):
        found = route(message)
        assert found.path == "agent" and "translate" in _hint_tools(message), (message, found.reasons)


@pytest.mark.parametrize("message, why", [
    # the person's own things, and things to be done
    ("add milk and eggs to my shopping list", "tool"), ("what's on my list?", "tool"), ("what do I have coming up?", "tool"),
    ("remind me to call the vet tomorrow at 3", "action"), ("write an email to my landlord about the heating", "action"),
    ("rewrite my email: hello there how are you today friend", ""), ("thanks, now add bread to my list", ""),
    ("hi, what's on my calendar today?", ""),
    # questions: the library or the web may hold the answer
    ("how do I purify water with bleach?", ""), ("what is the capital of France?", "question"), ("explain how a heat pump works", ""),
    ("tell me about the Battle of Hastings", ""), ("is this mushroom edible", ""),
    ("give me tips for sharpening a knife", ""), ("suggest options for heating a cabin", ""), ("give me ideas for treating a wasp sting", ""),
    # the world as it is now
    ("what's the weather tomorrow?", ""), ("any news on the storm", ""), ("I had a rough day today", "date"),
    # links, files, things kept in the app
    ("check https://example.com and tell me the title", "link"), ("summarize the file notes.txt", ""),
    ("summarize chapter 3 of the survival manual", ""), ("summarize this: https://example.com/article", ""),
    ("draw a picture of a red fox in snow", ""), ("make a 5 second video of waves", ""), ("generate a song about cats", ""),
    ("write a python script that prints hello", ""), ("build me a web app that tracks workouts", ""),
    ("write a poem and save it as a note", ""), ("translate this document", ""),
    # carrying on
    ("continue", "carry on"), ("do it", "carry on"), ("go ahead", "carry on"), ("yes please do", "carry on"), ("try again", "carry on"),
    # anything not plainly conversation
    ("That makes sense, I think I'll go with the second option then.", "not plainly"), ("hmm", ""), ("", ""),
])
def test_anything_that_might_need_a_tool_keeps_its_tools(message, why):
    found = route(message)
    assert found.path == "agent", (message, found.reasons)
    assert found.reasons and found.reasons[0].startswith("tools kept:")
    if why:
        assert why in found.reasons[0], found.reasons


@pytest.mark.parametrize("turn, why", [
    ({"code_mode": True}, "Code mode"), ({"has_attachments": True}, "attached"), ({"search_on": True}, "web search"),
    ({"special": True}, "document"),
])
def test_the_state_of_the_turn_keeps_tools_whatever_the_words(turn, why):
    for message in ("thanks!", "write me a haiku about autumn"):
        found = route(message, **turn)
        assert found.path == "agent" and why in found.reasons[0]


def test_on_the_family_screen_tools_are_kept_for_everything_but_a_hello():
    """There the tools are what the assistant is for, whatever the words — but a hello or a thank-you
    needs none, and must not wait for them."""
    for message in ("write me a haiku about autumn", "milk and eggs", "ok", "what do you mean"):
        found = route(message, home_account=True)
        assert found.path == "agent" and "home screen" in found.reasons[0], message
    for message in ("hi", "thanks!", "good morning", "thank you so much"):
        found = route(message, home_account=True)
        assert found.path == "chat" and found.label == "quick", message
    assert route("thanks!", home_account=True, has_attachments=True).path == "agent"      # a photo came with it


@pytest.mark.parametrize("message", [
    "any news on the storm", "what's the latest on the election", "who won the game last night", "how much is a 2x4 at the lumber yard",
    "is the hardware store open now", "search the web for a replacement gasket", "look it up", "google the model number",
    "check https://example.com and tell me the title", "best cordless drill reviews", "what is the exchange rate for euros",
    "when does the new season come out", "pharmacy near me", "what happened in 2026",
])
def test_the_web_is_pressed_on_the_assistant_only_for_what_is_dated_or_a_lookup(message):
    assert router.wants_web(message), message


@pytest.mark.parametrize("message", [
    "how do I purify water with bleach?", "what is the capital of France?", "explain how a heat pump works", "tell me about the Battle of Hastings",
    "add milk to my shopping list", "remind me to call the dentist tomorrow at 10", "how many cups is 2 litres", "write me a haiku about autumn",
    "what's the weather tomorrow?", "when does it get dark tonight", "how do you say thank you in Japanese", "thanks!", "",
])
def test_settled_knowledge_and_the_households_own_things_do_not_call_for_the_web(message):
    assert not router.wants_web(message), message


def test_ok_is_small_talk_after_a_statement_and_an_instruction_after_a_question():
    assert route("ok", last_reply="A heat pump moves heat rather than making it.").path == "chat"
    assert route("ok").path == "chat"
    asked = route("ok", last_reply="I found three old backups. Shall I delete them?")
    assert asked.path == "agent" and "answers the assistant" in asked.reasons[0]
    assert route("yes", last_reply="Done — the list has three items now.", last_used_tools=True).path == "agent"
    assert route("no", last_reply="Want me to add it to the calendar?").path == "agent"
    # "why?" is about the reply above it — unless that reply was the result of work
    assert route("why?", last_reply="Because warm air holds more water.").path == "chat"
    assert route("what do you mean?", last_reply="I added it.", last_used_tools=True).path == "agent"
    assert route("why?").path == "agent"                       # nothing above to be about


def test_a_turn_in_plain_chat_is_never_moved_to_the_agent():
    for message in ("add milk to my list", "what's the weather tomorrow?", "check https://example.com", "do it", "thanks!"):
        assert route(message, mode="chat").path == "chat"
    assert route("thanks!", mode="chat").effort == "low" and route("write me a limerick about goats", mode="chat").label == "quick"
    assert route("what is the boiling point of water at altitude?", mode="chat").effort is None
    assert route("ok", mode="chat", last_reply="Shall I go on?").effort is None


# ── effort ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message, effort", [
    ("add milk and eggs to my shopping list", "low"), ("remind me to call the vet tomorrow at 3", "low"), ("show me my boards", "low"),
    ("log 2 glasses of water", "low"), ("turn on web search", "low"), ("what's on my packing list", "low"), ("mark the laundry as done", "low"),
    ("show me how to build a raised garden bed", None), ("read this and tell me what you think of the plan", None),
    ("open the pod bay doors and explain why that line is famous", None), ("save the whales — why does it matter", None),
    ("how do I purify water with bleach?", None), ("what is 17*23?", None), ("what is 15% of 80", None),
    ("build me a web app that tracks workouts", None), ("search for the best dutch oven", None),
    ("think step by step: a train leaves at 3pm going 60 mph, another at 4pm going 80", "high"),
    ("compare the pros and cons of propane and wood heat for a cabin", "high"),
    ("debug why the script crashes with a KeyError when the list is empty", "high"),
    ("why does my sourdough fail to rise in winter?", "high"), ("analyze this contract clause for risks", "high"),
    ("be thorough: what should I check before buying a used truck?", "high"),
    ("what is it? how old is it? is it safe to eat?", "high"),
    ("add " + "a very long item " * 12 + "to my list", None),
])
def test_effort_follows_the_message(message, effort):
    found = route(message)
    assert found.effort == effort, (message, found.reasons)
    assert found.label == {"low": "quick", "high": "careful", None: ""}[effort]


def test_a_lot_to_take_in_gets_care_unless_it_is_only_to_be_reworded():
    long_text = "The committee met on the hill and argued about the well. " * 40
    assert route("What should we do about this? " + long_text).effort == "high"
    reworded = route("rewrite this to be shorter: " + long_text)
    assert (reworded.path, reworded.effort) == ("chat", "low")


def test_code_mode_thinks_harder_about_real_work_and_never_rushes():
    assert route("rename the variable", code_mode=True).effort is None
    assert route("refactor this module to use dataclasses", code_mode=True).effort == "high"
    assert route("add a retry around the upload call and make the timeout configurable in the settings, then run the tests and fix "
                 "whatever fails, and commit when everything is green again and tell me what changed and what is left to do",
                 code_mode=True).effort == "high"
    assert route("add milk to the list", code_mode=True).effort is None


def test_thinking_set_by_hand_is_left_alone():
    for chosen in (True, False):
        for message in ("thanks!", "think step by step about this proof", "add milk to my list"):
            assert route(message, think_set=chosen).effort is None
    assert route("thanks!", think_set=False).path == "chat"            # the lane is still chosen


# ── the model ──────────────────────────────────────────────────────────────

def test_a_model_is_swapped_only_when_named_and_served():
    named = {"code": "coder-80b", "deep": "big-thinker", "quick": "small-fast"}
    served = ["main-120b", "coder-80b", "small-fast"]
    base = {"auto_models": named, "available_models": served, "current_model": "main-120b"}
    assert route("refactor this module to use dataclasses", code_mode=True, **base).model == "coder-80b"
    assert route("thanks!", **base).model == "small-fast"
    assert route("how do I purify water?", **base).model is None                       # ordinary turn: stays
    assert route("analyze this plan for risks", **base).model is None                  # named, but the endpoint does not serve it
    assert route("thanks!", **{**base, "available_models": None}).model is None        # not known to be served
    assert route("thanks!", **{**base, "current_model": "small-fast"}).model is None   # already on it
    assert route("thanks!", auto_models={}, available_models=served).model is None     # nothing named: nothing swapped
    assert route("thanks!", think_set=False, **base).model is None                     # effort chosen by hand: no "quick" model either
    swapped = route("thanks!", **base)
    assert swapped.moved and swapped.as_event() == {"path": "chat", "effort": "low", "model": "small-fast", "label": "quick",
                                                    "why": swapped.as_event()["why"]}
    assert "small-fast" in swapped.as_event()["why"]
    plain = route("how do I purify water?")
    assert not plain.moved and plain.as_event()["effort"] == "default"


def test_settings_switch_it_off_and_name_the_models(monkeypatch):
    import src.settings as settings

    stored = {}
    monkeypatch.setattr(settings, "load_settings", lambda: dict(stored))
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner, default=None: stored.get(f"{owner}:{key}", default))
    assert router.enabled("alice") is True and router.named_models() == {}
    stored["auto_route"] = False
    assert router.enabled("alice") is False
    stored["alice:auto_route"] = "on"                                                  # a person's own choice wins
    assert router.enabled("alice") is True and router.enabled("bob") is False
    stored["auto_models"] = {"code": "coder-80b", "deep": "", "poetry": "bard", "quick": 7}
    assert router.named_models() == {"code": "coder-80b", "quick": "7"}
    stored["auto_models"] = ["not", "a", "mapping"]
    assert router.named_models() == {}
    monkeypatch.setattr(settings, "load_settings", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert router.enabled("alice") is True and router.named_models() == {}             # unreadable settings: Auto stays on, swaps nothing


def test_served_models_reads_the_endpoint_list_and_never_asks_the_endpoint(tmp_path, monkeypatch):
    import core.database as cdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'router.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    db = factory()
    db.add(cdb.ModelEndpoint(id="swap", name="llama-swap", base_url="http://127.0.0.1:9090/v1/", is_enabled=True,
                             cached_models='["main-120b", "coder-80b"]', pinned_models='["pinned-one"]'))
    db.add(cdb.ModelEndpoint(id="off", name="off", base_url="http://127.0.0.1:1234/v1", is_enabled=False, cached_models='["x"]'))
    db.add(cdb.ModelEndpoint(id="odd", name="odd", base_url="http://127.0.0.1:5555/v1", is_enabled=True, cached_models="not json"))
    db.commit()
    db.close()
    monkeypatch.setattr("httpx.get", lambda *a, **k: pytest.fail("must not ask the endpoint"))
    assert router.served_models("http://127.0.0.1:9090/v1/chat/completions") == ["main-120b", "coder-80b", "pinned-one"]
    assert router.served_models("http://127.0.0.1:1234/v1/chat/completions") is None       # a disabled endpoint is not "known"
    assert router.served_models("http://127.0.0.1:5555/v1/chat/completions") == []
    assert router.served_models("http://elsewhere:8000/v1/chat/completions") is None and router.served_models("") is None


# ── asking for Deep Research by name ───────────────────────────────────────
# A research job reads dozens of pages and takes minutes. The chat may start
# one only when the person asked for it in so many words.

@pytest.mark.parametrize("message", [
    "Do deep research on the history of the safety bicycle", "deep research: best budget 3D printers 2026",
    "Research the best solar generators for home backup", "can you research how heat pumps perform in cold climates",
    "Please do some research on raised bed gardening in zone 6", "I need you to research local well-drilling regulations",
    "run a deep dive on sodium-ion batteries", "write me a detailed research report on the causes of the 2008 financial crisis",
    "Could you please research which wood stoves qualify for the tax credit?", "deep research this",
    "hey aegis, research meshtastic radios for off-grid comms", "do a deep research job on water purification methods",
    "Start research on the best trail cameras", "let's research sourdough hydration levels",
    "go research tiny home zoning laws in Ohio", "Deep-dive into ham radio licensing for me",
    "research no-till gardening for clay soil", "research and compare the best e-bikes under $2000",
    "Research whether creatine is safe long term", "do research into what causes tinnitus",
])
def test_a_research_job_asked_for_by_name(message):
    assert router.asks_for_research(message), message
    assert router.may_start_research(message, "agent")
    # ...and such a turn is never moved to the quick lane, where there are no tools
    assert route(message, mode="agent").path == "agent"


@pytest.mark.parametrize("message", [
    "Deep research still a function just behind a slash command", "what does the research say about creatine?",
    "how do I do research on my family tree?", "open my research on solar generators", "delete the research about printers",
    "is deep research still available?", "my research paper is due friday", "what is deep research",
    "show me the last research report", "research shows that sleep matters, right?", "summarize that research for me",
    "add 'research wood stoves' to my to-do list", "what's the best budget 3D printer?", "tell me about the safety bicycle",
    "where is the research button", "how does deep research on this app work", "the research library is empty",
    "who did the research on penicillin?", "hi", "can you look into why my wifi is slow", "research mode off",
    "Research has shown mixed results", "deep research doesn't work on my phone", "research is hard",
    "Deep research should be easier to find", "remind me to research wood stoves tomorrow", "",
])
def test_talk_about_research_is_not_a_request_for_a_job(message):
    assert not router.asks_for_research(message), message
    assert not router.may_start_research(message, "agent")


def test_research_starts_only_where_there_are_tools():
    ask = "do deep research on rainwater harvesting"
    assert router.may_start_research(ask, "agent")
    assert not router.may_start_research(ask, "chat")                    # plain chat: no tools at all
    assert not router.may_start_research(ask, "agent", code_mode=True)   # Code mode is for the coding agent
    assert not router.asks_for_research("x" * 3000 + " do deep research on this")   # a pasted wall of text is not a command
