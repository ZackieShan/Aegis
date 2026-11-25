"""The daily review: once a day, how did the assistant do, and what would make it better?

Every night the server reads the day's chats, rates each exchange (a request and what the assistant did
about it) from 1 to 5, names what went wrong in words from a fixed list, and writes a report for the
administrators: a score for the day and for each kind of assistant, the things that went wrong and how
often, and a short list of what to change.

    collect   the day's exchanges, read from the chat records (no model, nothing changed)
    signals   what the records themselves show: a tool that failed, an answer stopped or asked for again,
              a slow reply, a question a look-up tool exists for that was answered without it
    judge     the rating — the house's own model reads each exchange against a fixed rubric
    report    the day put together, kept in the data folder, and shaped for whoever is reading it
    run       the whole job, in steps that can be interrupted and picked up again

Everything happens on this server, with its own model; nothing is sent anywhere. What is kept about a chat
depends on whose it is: an administrator's own chats keep a short excerpt and the reviewer's note, shown only
to that administrator; every other account's chats are kept as figures and categories only — never what was
said. An administrator can leave any account out altogether. See ``docs/daily-review.md``.
"""

from .signals import CODES

__all__ = ["CODES"]
