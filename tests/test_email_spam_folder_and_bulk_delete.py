"""Asked to clear the spam, the assistant must not empty the inbox.

What happened, on a Gmail mailbox: the assistant asked the mail server for the folder "Spam", then
"Junk", then "spam", and was told each time that there is no such folder — Gmail's is "[Gmail]/Spam".
It then listed the newest two hundred messages of the INBOX, moved all of them to Trash in one call,
and reported that the spam had been cleared.

Two things are checked here:

  * a folder asked for by what it is ("Spam", "Junk", "Trash", "Archive") opens the provider's own
    folder for it, for reading and for bulk changes alike;
  * deleting more than twenty messages in one call does nothing until the person has been told how
    many and which, and has said yes.

The mailbox, the folders and the numbers are made up.
"""
import asyncio

import pytest

pytest.importorskip("mcp")

import mcp_servers.email_server as es


class Gmail:
    """A mail server that, like Gmail, keeps spam in "[Gmail]/Spam" and has no folder called "Spam"."""

    FOLDERS = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
    ]

    def __init__(self):
        self.opened, self.calls = [], []

    def list(self):
        return "OK", self.FOLDERS

    def select(self, folder, readonly=False):
        name = folder.strip('"')
        known = {es._folder_name_from_list_line(line) for line in self.FOLDERS}
        if name not in known:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.opened.append((name, readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "FETCH":
            return "OK", [b"1 (UID 501)", b"2 (UID 502)", b"3 (UID 503)"]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("EXPUNGE",))

    def logout(self):
        pass


def test_spam_opens_the_providers_own_spam_folder():
    for asked in ("Spam", "Junk", "spam"):
        conn = Gmail()
        assert es._select_folder(conn, asked, readonly=True) == "[Gmail]/Spam", asked
        assert conn.opened == [("[Gmail]/Spam", True)]
    conn = Gmail()
    assert es._select_folder(conn, "Trash") == "[Gmail]/Trash" and es._select_folder(conn, "Archive") == "[Gmail]/All Mail"
    assert es._select_folder(Gmail(), "INBOX") == "INBOX"
    assert es._select_folder(Gmail(), "Receipts") == ""               # a folder that is not there, and is no kind of folder, is not guessed at


def test_listing_the_spam_folder_reads_spam_not_the_inbox(monkeypatch):
    conn = Gmail()
    monkeypatch.setattr(es, "_fixture_list_emails", lambda *a, **k: None)
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)
    try:
        es._list_emails("Spam", max_results=5)
    except Exception:
        pass                                                          # what it does with the made-up messages is not the point
    assert conn.opened and conn.opened[0] == ("[Gmail]/Spam", True) and ("INBOX", True) not in conn.opened


def test_a_bulk_change_in_the_spam_folder_happens_in_the_spam_folder(monkeypatch):
    conn = Gmail()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)
    assert es._bulk_move(["501", "502", "503"], "Spam", "Trash", role="trash") == 3
    assert conn.opened == [("[Gmail]/Spam", False)]
    assert ("MOVE", b"501,502,503", '"[Gmail]/Trash"') in conn.calls
    # and when the folder really is not there nothing is touched, rather than whatever folder was open
    conn = Gmail()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)
    assert es._bulk_move(["501"], "Receipts", "Trash", role="trash") == 0 and conn.calls == []
    assert es._bulk_set_flag(["501"], "Receipts", "\\Seen") == 0 and conn.calls == []


def test_a_few_messages_are_deleted_and_many_wait_for_the_person():
    assert es.BULK_DELETE_ASK_ABOVE == 20
    assert es._bulk_delete_needs_asking(20, None) == ""
    held = es._bulk_delete_needs_asking(200, None)
    assert held.startswith("Nothing was deleted.") and "200 messages" in held and "confirmed_count: 200" in held
    assert es._bulk_delete_needs_asking(200, 200) == "" and es._bulk_delete_needs_asking(200, "200") == ""
    assert es._bulk_delete_needs_asking(200, 150) and es._bulk_delete_needs_asking(200, True) and es._bulk_delete_needs_asking(200, "yes")


def _call(arguments):
    return asyncio.run(es.call_tool("bulk_email", arguments))[0].text


def test_two_hundred_messages_are_not_deleted_on_the_first_call(monkeypatch):
    moved = []
    monkeypatch.setattr(es, "_bulk_move", lambda uids, folder, dest, account=None, role="": moved.append((len(uids), folder, role)) or len(uids))
    monkeypatch.setattr(es, "_bulk_set_flag", lambda uids, folder, flag, add=True, account=None: moved.append((len(uids), folder, flag)) or len(uids))
    monkeypatch.setattr(es, "_load_config", lambda account=None: {"trash_folder": "Trash", "archive_folder": "Archive"})
    # Never read the accounts of the machine the tests run on.
    monkeypatch.setattr(es, "_read_accounts_from_db", lambda *a, **k: [])
    uids = [str(n) for n in range(200)]

    said = _call({"action": "delete", "uids": uids})
    assert said.startswith("Nothing was deleted.") and moved == []
    assert _call({"action": "delete", "uids": uids, "permanent": True}).startswith("Nothing was deleted.") and moved == []
    assert _call({"action": "delete", "uids": uids, "confirmed_count": 50}).startswith("Nothing was deleted.") and moved == []

    assert "200 email(s) moved to Trash" in _call({"action": "delete", "uids": uids, "confirmed_count": 200})
    assert moved == [(200, "INBOX", "trash")]

    moved.clear()
    assert "5 email(s) moved to Trash" in _call({"action": "delete", "uids": uids[:5]})          # a handful needs no asking
    assert "200 email(s) marked read" in _call({"action": "mark_read", "uids": uids})            # and nothing else is held back
    assert moved == [(5, "INBOX", "trash"), (200, "INBOX", "\\Seen")]


def test_the_tool_says_so_where_the_model_reads_about_it():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "bulk_email")
    assert "confirmed_count" in schema["parameters"]["properties"]
    for words in ("never treat INBOX messages as spam", "more than 20 messages"):
        assert words in schema["description"], words
    listed = asyncio.run(es.list_tools())
    tool = next(t for t in listed if t.name == "bulk_email")
    assert "confirmed_count" in tool.inputSchema["properties"] and "never treat INBOX messages as spam" in tool.description
