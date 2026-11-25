"""Looking one kind of thing up in the place that keeps it, instead of asking the model's memory.

    news        recent articles on a subject, each with its date
    places      shops, restaurants and services near home or a named town
    medicines   a medicine's official US label: uses, dose, side effects, warnings, interactions
    papers      published studies on a question
    wolfram     exact maths and curated facts from Wolfram|Alpha

Each module has one ``answer(...)`` that returns the text for the assistant and the list of sources for
the screen, and raises ``Problem`` (``services.connections``) with words fit for a person when it cannot.
Which outside service each one asks, and whether it needs a key, is in ``services/connections/catalog.py``.
"""

from services.connections.http import Problem

__all__ = ["Problem"]
