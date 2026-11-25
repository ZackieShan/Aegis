"""Home: the everyday things a household keeps — lists and plans, reminders,
trackers and boards — for the simple home screen and for the assistant.

``lists`` and ``reminders`` are stored as notes (checklists, and notes with a
due time), so they also show up in the main app's Notes and ride on its
reminder delivery. ``trackers`` and ``boards`` have tables of their own.
Everything is per account. See ``docs/home.md``.
"""

from . import boards, common, lists, reminders, trackers
from .common import HomeError

__all__ = ["boards", "common", "lists", "reminders", "trackers", "HomeError"]
