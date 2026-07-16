from src.action_intents import classify_tool_intent, message_needs_tools


def test_calendar_entry_request_promotes_to_agent():
    assert message_needs_tools("Can you add an entry to my calendar?")
    intent = classify_tool_intent("Can you add an entry to my calendar?")
    assert intent.needs_tools
    assert intent.category == "calendar"


def test_calendar_imperative_variants_promote_to_agent():
    assert message_needs_tools("add lunch with Sam to my calendar tomorrow at noon")
    assert message_needs_tools("schedule a call with Mina next Friday")
    assert message_needs_tools("put dentist appointment on my calendar")
    assert message_needs_tools("Alright. Recreate that same appointment")
    assert message_needs_tools("Okay delete that doctor appointment from the calendar")
    assert message_needs_tools("have another go at adding a test entry to the calendar")
    assert message_needs_tools(
        "Okay so you should be able to create that calendar event for tomorrow at 1:30 p.m. right for me to go to the hardware store"
    )
    assert message_needs_tools(
        "make it an appointment at 12pm for me to visit the doctor it's tomorrow the 2nd of June 2026"
    )


def test_calendar_read_requests_promote_to_agent():
    assert message_needs_tools("What upcoming events do I have?")
    assert message_needs_tools("Can you show my next appointments?")
    assert message_needs_tools("Do I have upcoming Taekwondo classes this week?")
    assert message_needs_tools("What's on my calendar tomorrow?")
    assert message_needs_tools("When is my next meeting?")


def test_note_todo_and_reminder_actions_promote_to_agent():
    assert message_needs_tools("add milk to my todo list")
    assert message_needs_tools("take a note that the server needs checking")
    assert message_needs_tools("set a reminder to call Pat at 4pm")


def test_email_and_ui_actions_promote_to_agent():
    assert message_needs_tools("reply to that email")
    assert message_needs_tools("mark those emails as read")
    assert message_needs_tools("open my calendar")
    assert message_needs_tools("turn off web search")


def test_research_action_promotes_to_agent():
    assert message_needs_tools("research cost effective local models")
    assert message_needs_tools("can you look into GPU hosting options")


def test_explicit_web_search_promotes_to_agent():
    assert message_needs_tools("use web search and find a recipe for chocolate chip cookies")
    assert message_needs_tools("do a web search for the best chocolate chip cookies")
    assert message_needs_tools("search the web for current RTX 3090 prices")
    assert classify_tool_intent("use web search and find a recipe").category == "web"


def test_explanatory_calendar_questions_stay_plain_chat():
    assert not message_needs_tools("How do I add an entry to my calendar?")
    assert not message_needs_tools("What about the built-in Aegis calendar, is that linked to email?")
    assert not message_needs_tools("Can you explain how calendar reminders work?")
    intent = classify_tool_intent("How do I add an entry to my calendar?")
    assert not intent.needs_tools
    assert intent.reason == "explanatory feature question"


def test_router_reports_non_calendar_categories():
    assert classify_tool_intent("reply to that email").category == "email"
    assert classify_tool_intent("open my calendar").category == "ui"
    assert classify_tool_intent("research cost effective local models").category == "research"


def test_current_events_question_routes_to_web_not_calendar():
    # Regression 2026-07-16: "what ... this week ... summary of EVENTS"
    # matched the calendar lookup pattern, so a war-news question escalated
    # as a calendar turn and flailed against manage_calendar/manage_notes.
    # The curly apostrophe is the dictated/phone-keyboard form of the real
    # message — it must not break matching.
    msg = (
        "What’s going on with the war and Iran and the war in Ukraine this "
        "week give me a high-level summary of events and their impact for "
        "each conflict include Lebanon Turkey Gaza Israel as well"
    )
    intent = classify_tool_intent(msg)
    assert intent.needs_tools
    assert intent.category == "web"
    assert classify_tool_intent("What's happening in Gaza right now?").category == "web"


def test_bare_whats_going_on_stays_plain_chat():
    # No topic preposition — as likely to mean the user's own week as the
    # news, so it must not auto-escalate.
    assert not message_needs_tools("What's going on this week?")


def test_dictated_curly_apostrophe_calendar_question_still_matches():
    intent = classify_tool_intent("What’s on my calendar tomorrow?")
    assert intent.needs_tools
    assert intent.category == "calendar"


def test_calendar_lookup_with_modifier_words_still_matches():
    # The timeframe→noun tightening must still admit a determiner plus a
    # couple of modifier words ("next THREE meetings", "upcoming WORK
    # meetings"), or common lookups silently fall to plain chat.
    for msg in (
        "Show my upcoming work meetings",
        "What are my next three meetings?",
        "List today's team meetings",
        "Show me this week's taekwondo classes",
        "Check my upcoming dentist appointments",
    ):
        intent = classify_tool_intent(msg)
        assert intent.needs_tools and intent.category == "calendar", msg


def test_current_events_pattern_avoids_personal_and_mixed_topics():
    # Personal topics must not escalate as web — category "web" triggers the
    # tool clampdown in chat_routes, which would disable the very calendar/
    # notes tools a schedule question needs.
    assert not message_needs_tools("What's going on with my schedule this week?")
    # Conversational check-ins whose preposition and time word live in
    # different sentences must stay plain chat.
    assert not message_needs_tools("What's going on with you? I've been stressed lately")
    # Mixed messages containing an explicit shell request keep their shell
    # routing — the current-events pattern is evaluated last.
    intent = classify_tool_intent(
        "What's going on with the server right now? Can you run htop on prod"
    )
    assert intent.category == "shell"
