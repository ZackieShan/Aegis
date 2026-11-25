"""What an account leaves behind when it is deleted or renamed.

The lines that must hold: erasing one account's data removes all of it and
nothing of anyone else's (nor anything shared); a new account never picks up
a deleted account's data by accident, because the name stays reserved until
that data is erased or deliberately handed on; a renamed account keeps its
memory.
"""
import contextlib
import datetime
import importlib
import json
import sys
import threading
import types
from pathlib import Path

import pytest
from sqlalchemy import Boolean, DateTime, Float, Integer, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from tests.helpers.import_state import clear_module


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throw-away server: its own database (with the real schema and
    foreign keys enforced) and data folder."""
    import core.database as cdb
    import services.home  # noqa: F401  registers the tracker and board tables
    from services import account_data

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    for name, folder in (("DATA_DIR", ""), ("GENERATED_IMAGES_DIR", "generated_images"), ("DEEP_RESEARCH_DIR", "deep_research"),
                         ("SKILLS_DIR", "skills"), ("UPLOAD_DIR", "uploads")):
        target = tmp_path / folder if folder else tmp_path
        target.mkdir(exist_ok=True)
        monkeypatch.setattr(f"src.constants.{name}", str(target))
    import routes.personal_routes as personal
    monkeypatch.setattr(personal, "UPLOADS_DIR", str(tmp_path / "personal_uploads"))
    import routes.prefs_routes as prefs
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    from core import access
    monkeypatch.setattr(access, "_data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("AEGIS_ACCOUNT_LOG_DISABLED", raising=False)

    counter = iter(range(1, 10_000))

    def put(table_name, **values):
        """Insert a row, inventing a value for every column that needs one."""
        table = cdb.Base.metadata.tables[table_name]
        row = dict(values)
        for column in table.c:
            if column.name in row or column.nullable or column.default is not None or column.server_default is not None:
                continue
            if column.primary_key and isinstance(column.type, Integer):
                continue                                         # the database numbers it
            n = next(counter)
            kind = column.type
            row[column.name] = (False if isinstance(kind, Boolean) else n if isinstance(kind, Integer)
                                else 0.0 if isinstance(kind, Float) else datetime.datetime(2026, 1, 1)
                                if isinstance(kind, DateTime) else f"{table_name}-{column.name}-{n}")
        with factory() as db:
            key = db.execute(table.insert().values(**row)).inserted_primary_key
            db.commit()
        return row.get("id", key[0] if key else None)

    def count(table_name, where=None):
        table = cdb.Base.metadata.tables[table_name]
        query = select(func.count()).select_from(table)
        if where is not None:
            query = query.where(where(table))
        with factory() as db:
            return db.execute(query).scalar()

    with factory() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1     # the order of deletion is really tested
    return types.SimpleNamespace(data=account_data, put=put, count=count, db=factory, tables=cdb.Base.metadata.tables,
                                 root=tmp_path)


def _memory(root):
    from src.memory import MemoryManager
    return MemoryManager(str(root))


def _fill(store, owner):
    """Give an account a bit of everything the database holds."""
    chat = store.put("sessions", id=f"chat-{owner}", owner=owner)
    for _ in range(2):
        store.put("chat_messages", session_id=chat)
    store.put("notes", owner=owner)
    store.put("home_trackers", owner=owner)
    store.put("home_tracker_entries", owner=owner)
    store.put("home_boards", owner=owner)
    store.put("home_pins", owner=owner)
    calendar = store.put("calendars", id=f"cal-{owner}", owner=owner)
    store.put("calendar_events", calendar_id=calendar)
    task = store.put("scheduled_tasks", id=f"task-{owner}", owner=owner)
    store.put("task_runs", task_id=task)
    document = store.put("documents", id=f"doc-{owner}", owner=owner, session_id=chat)
    store.put("document_versions", document_id=document)
    store.put("gallery_images", id=f"img-{owner}", owner=owner, filename=f"{owner}.png", session_id=chat)
    return {"chat": chat, "task": task, "document": document}


# ── the database ───────────────────────────────────────────────────────────

def test_erasing_removes_every_row_of_the_account_and_nobody_elses(store):
    alice = _fill(store, "alice")
    _fill(store, "bob")
    # Bob's things that merely point at Alice's: they must survive, detached.
    store.put("documents", id="doc-bob-in-alices-chat", owner="bob", session_id=alice["chat"])
    store.put("scheduled_tasks", id="task-bob-after-alices", owner="bob", then_task_id=alice["task"])
    # Shared rows have no owner.
    store.put("notes", owner=None)
    store.put("model_endpoints", owner=None)

    before = {name: store.count(name) for name in store.tables}
    result = store.data._erase_rows("alice")

    assert result["images"] == ["alice.png"]
    for name, table in store.tables.items():
        if "owner" in table.c:
            assert store.count(name, lambda t: func.lower(t.c.owner) == "alice") == 0, name
    assert store.count("chat_messages") == 2 and store.count("calendar_events") == 1          # Bob's
    assert store.count("task_runs") == 1 and store.count("document_versions") == 1
    assert store.count("notes") == 2 and store.count("model_endpoints") == 1                   # Bob's and the shared one
    assert store.count("documents", lambda t: t.c.owner == "bob") == 2
    assert store.count("scheduled_tasks", lambda t: t.c.owner == "bob") == 2
    with store.db() as db:
        documents, tasks = store.tables["documents"], store.tables["scheduled_tasks"]
        assert db.execute(select(documents.c.session_id).where(documents.c.id == "doc-bob-in-alices-chat")).scalar() is None
        assert db.execute(select(tasks.c.then_task_id).where(tasks.c.id == "task-bob-after-alices")).scalar() is None
    assert result["rows"]["sessions"] == 1 and result["rows"]["chat_messages"] == 2
    assert sum(result["rows"].values()) == sum(before.values()) - sum(store.count(name) for name in store.tables)


def test_account_names_match_exactly_and_an_empty_name_is_refused(store):
    _fill(store, "al")
    _fill(store, "alice")
    store.put("notes", owner="")                                   # how a server without accounts stores things
    store.put("notes", owner="Alice")                              # older rows may differ in case: still hers
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            store.data.erase(bad)
        with pytest.raises(ValueError):
            store.data.leftover(bad)
    store.data._erase_rows("alice")
    assert store.count("notes", lambda t: t.c.owner == "al") == 1 and store.count("notes", lambda t: t.c.owner == "") == 1
    assert store.count("notes", lambda t: func.lower(t.c.owner) == "alice") == 0
    assert store.count("sessions") == 1


def test_every_table_is_either_someones_or_known_to_be_shared(store):
    """A new table has to be covered by account deletion: it records whose a
    row is, or hangs off a table that does, or is listed here as shared."""
    shared = {"mcp_servers", "webhooks"}
    nobodys = {name for name, table in store.tables.items() if store.data._their_rows(table, "x") is None}
    assert nobodys == shared


# ── everything, across the stores ──────────────────────────────────────────

def _everything(store, owner, memory):
    from services.home import boards
    from services.memory import mind, profile, store as mind_store
    from routes.personal_routes import _personal_upload_dir_for_owner

    _fill(store, owner)
    memory.save(memory.load_all() + [{"id": f"m-{owner}", "text": f"{owner} keeps bees", "owner": owner, "category": "interest"}])
    mind.add_notes(memory, owner, "Beekeeping", ["Four hives"], kind="interest", keywords=["bees"])
    profile.profile_path(owner, memory).parent.mkdir(parents=True, exist_ok=True)
    profile.profile_path(owner, memory).write_text(json.dumps({"owner": owner, "summary": "Keeps bees"}))
    pins = boards.pins_dir(owner)
    pins.mkdir(parents=True)
    (pins / "pin.jpg").write_bytes(b"jpg")
    (store.root / "generated_images" / f"{owner}.png").write_bytes(b"png")
    (store.root / "deep_research" / f"{owner}.json").write_text(json.dumps({"owner": owner, "report": "…"}))
    skill = store.root / "skills" / "general" / f"{owner}-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {owner}-skill\nowner: {owner}\n---\nBody that says owner: someone-else\n")
    folder = Path(_personal_upload_dir_for_owner(owner))
    (folder / "notes.txt").write_text("mine")
    upload = store.root / "uploads" / f"{owner}-file"
    upload.write_bytes(b"data")
    index_path = store.root / "uploads" / "uploads.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    index[f"{owner}:hash"] = {"id": f"{owner}-file", "path": str(upload), "owner": owner, "hash": "hash"}
    index_path.write_text(json.dumps(index))
    return mind_store.mind_dir(memory, owner)


def _uploads(store):
    from src.upload_handler import UploadHandler
    handler = UploadHandler.__new__(UploadHandler)                 # without the file-type detector and the rest
    handler.upload_dir = str(store.root / "uploads")
    handler._index_lock = threading.Lock()
    handler._index_cache, handler._index_mtime = None, 0.0
    return handler


def test_erase_leaves_nothing_of_the_account_and_everything_of_the_other(store):
    memory = _memory(store.root)
    alice_mind = _everything(store, "alice", memory)
    bob_mind = _everything(store, "bob", memory)
    (store.root / "skills" / "general" / "shared-skill").mkdir()
    (store.root / "skills" / "general" / "shared-skill" / "SKILL.md").write_text("---\nname: shared\n---\nNo owner.\n")
    (store.root / "skills" / "_usage.json").write_text(json.dumps({"alice::alice-skill": 3, "bob::bob-skill": 1, "plain": 2}))

    held = types.SimpleNamespace(sessions={"chat-alice": types.SimpleNamespace(owner="alice"),
                                           "chat-bob": types.SimpleNamespace(owner="bob")})
    left = store.data.leftover("alice", memory_manager=memory)
    assert left["chats"] == 1 and left["memories"] == 1 and left["files"] >= 4 and left["total"] > 20
    bob_before = store.data.leftover("bob", memory_manager=memory)

    with pytest.raises(ValueError):                                # never while the account still exists
        store.data.erase("alice", accounts=["alice", "bob"], memory_manager=memory)

    result = store.data.erase("alice", accounts=["bob"], memory_manager=memory, session_manager=held,
                              upload_handler=_uploads(store))
    assert result["failed"] == []
    assert store.data.leftover("alice", memory_manager=memory)["total"] == 0
    assert store.data.leftover("bob", memory_manager=memory) == bob_before
    assert list(held.sessions) == ["chat-bob"]
    assert [m["owner"] for m in memory.load_all()] == ["bob"]
    assert not alice_mind.exists() and bob_mind.is_dir()
    root = store.root
    assert not (root / "generated_images" / "alice.png").exists() and (root / "generated_images" / "bob.png").exists()
    assert not (root / "deep_research" / "alice.json").exists() and (root / "deep_research" / "bob.json").exists()
    assert not (root / "skills" / "general" / "alice-skill").exists() and (root / "skills" / "general" / "bob-skill").is_dir()
    assert (root / "skills" / "general" / "shared-skill" / "SKILL.md").is_file()
    assert json.loads((root / "skills" / "_usage.json").read_text()) == {"bob::bob-skill": 1, "plain": 2}
    assert not (root / "uploads" / "alice-file").exists() and (root / "uploads" / "bob-file").exists()
    assert list(json.loads((root / "uploads" / "uploads.json").read_text())) == ["bob:hash"]
    assert not (root / "personal_uploads" / "alice").exists() and (root / "personal_uploads" / "bob" / "notes.txt").is_file()
    assert not (root / "home" / "pins" / "alice").exists() and (root / "home" / "pins" / "bob" / "pin.jpg").is_file()
    assert result["removed"]["memories"] == 1 and result["removed"]["sessions"] == 1 and result["removed"]["uploads"] == 1


def test_a_store_that_cannot_be_cleared_is_reported_and_the_rest_still_goes(store):
    memory = _memory(store.root)
    _everything(store, "alice", memory)
    result = store.data.erase("alice", memory_manager=memory, upload_handler=None)      # no upload store to ask
    assert result["failed"] == ["uploads"]
    left = store.data.leftover("alice", memory_manager=memory)
    assert left["total"] == 1 and left["files"] == 1 and left["chats"] == 0 and left["memories"] == 0


def test_a_folder_shared_with_a_live_account_is_left_alone(store):
    """Two names can land on one folder ("a b" and "a_b"): what is on disk
    under it may be the other, living account's."""
    from services.home import boards
    memory = _memory(store.root)
    folder = boards.pins_dir("a b")
    assert folder == boards.pins_dir("a_b")
    folder.mkdir(parents=True)
    (folder / "pin.jpg").write_bytes(b"jpg")
    store.data.erase("a b", accounts=["a_b"], memory_manager=memory)
    assert (folder / "pin.jpg").is_file()
    store.data.erase("a b", accounts=["someone"], memory_manager=memory)
    assert not folder.exists()


def test_uploads_are_removed_with_their_files_and_only_inside_the_upload_folder(store):
    handler = _uploads(store)
    uploads = store.root / "uploads"
    outside = store.root / "precious.txt"
    outside.write_text("not an upload")
    (uploads / "one").write_bytes(b"1")
    (uploads / "two").write_bytes(b"2")
    (uploads / "uploads.json").write_text(json.dumps({
        "alice:h1": {"id": "one", "path": str(uploads / "one"), "owner": "Alice"},
        "alice:h2": {"id": "evil", "path": str(outside), "owner": "alice"},          # a row pointing out of the folder
        "bob:h1": {"id": "two", "path": str(uploads / "two"), "owner": "bob"},
        "h3": {"id": "three", "path": str(uploads / "three")},                       # nobody's
    }))
    assert handler.remove_owner("") == 0 and handler.remove_owner("nobody") == 0
    assert handler.remove_owner("alice") == 2
    assert not (uploads / "one").exists() and (uploads / "two").exists() and outside.read_text() == "not an upload"
    assert sorted(json.loads((uploads / "uploads.json").read_text())) == ["bob:h1", "h3"]


# ── renaming ───────────────────────────────────────────────────────────────

def test_a_renamed_account_keeps_its_memory_files_pictures_devices_and_sign_ins(store, monkeypatch):
    from core import access
    from services import push
    from services.home import boards
    from services.memory import mind, profile, store as mind_store

    monkeypatch.delenv("AEGIS_PUSH_DISABLED", raising=False)
    memory = _memory(store.root)
    mind.add_notes(memory, "robin", "Garden", ["Planted beans"], kind="home", keywords=["garden"])
    profile.profile_path("robin", memory).parent.mkdir(parents=True, exist_ok=True)
    profile.profile_path("robin", memory).write_text(json.dumps({"owner": "robin", "summary": "Gardens"}))
    boards.pins_dir("robin").mkdir(parents=True)
    (boards.pins_dir("robin") / "pin.jpg").write_bytes(b"jpg")
    (store.root / "push_subscriptions.json").write_text(json.dumps({"robin": [{"endpoint": "https://fcm.googleapis.com/x"}]}))
    access.note_sign_in("robin")

    moved = store.data.move_files("robin", "robyn", memory_manager=memory)
    assert set(moved) == {"profile", "memory files", "board pictures", "devices", "sign-ins"}
    assert [t["name"] for t in mind.list_topics(memory, "robyn")] == ["Garden"] and mind.list_topics(memory, "robin") == []
    assert json.loads(profile.profile_path("robyn", memory).read_text())["owner"] == "robyn"
    assert not profile.profile_path("robin", memory).exists() and not mind_store.mind_dir(memory, "robin").exists()
    assert (boards.pins_dir("robyn") / "pin.jpg").is_file() and not boards.pins_dir("robin").exists()
    assert len(push.devices("robyn")) == 1 and push.devices("robin") == []
    assert "robyn" in access.sign_in_state() and "robin" not in access.sign_in_state()

    # Never over what is already under the new name.
    mind.add_notes(memory, "sam", "Cars", ["Old truck"], kind="interest", keywords=["truck"])
    assert "memory files" not in store.data.move_files("sam", "robyn", memory_manager=memory)
    assert [t["name"] for t in mind.list_topics(memory, "robyn")] == ["Garden"]
    assert [t["name"] for t in mind.list_topics(memory, "sam")] == ["Cars"]


# ── the account store: a deleted account's name stays reserved ─────────────

@pytest.fixture
def accounts(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    core = sys.modules.get("core")
    if core is None:
        core = types.ModuleType("core")
        monkeypatch.setitem(sys.modules, "core", core)
    core.__path__ = [str(root / "core")]
    clear_module("core.auth")
    auth_mod = importlib.import_module("core.auth")
    _token_store(tmp_path, monkeypatch)
    manager = auth_mod.AuthManager(str(tmp_path / "auth.json"))
    manager.create_user("zed", "admin-password", is_admin=True)
    manager.create_user("mara", "mara-password")
    return auth_mod, manager


def _token_store(tmp_path, monkeypatch):
    """Deleting an account revokes its API tokens first: give that a database."""
    import core.database as cdb
    engine = create_engine(f"sqlite:///{tmp_path / 'tokens.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def session():
        db = factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr(cdb, "get_db_session", session)


def test_the_name_of_a_deleted_account_cannot_be_taken_until_it_is_freed(accounts, tmp_path, monkeypatch):
    auth_mod, manager = accounts
    manager.create_user("nina", "nina-password")
    assert manager.delete_user("mara", "zed")
    assert list(manager.removed()) == ["mara"] and manager.removed()["mara"]["by"] == "zed"
    assert manager.create_user("mara", "another-password") is False            # sign-up, the old console, anything
    assert manager.rename_user("nina", "mara", "zed") is False                  # nor by renaming someone into it
    assert "mara" not in manager.users and "nina" in manager.users
    # It survives a restart.
    again = auth_mod.AuthManager(str(tmp_path / "auth.json"))
    assert list(again.removed()) == ["mara"] and again.create_user("mara", "another-password") is False
    assert manager.note_removed("mara", erase_failed=["uploads"]) and manager.removed()["mara"]["erase_failed"] == ["uploads"]
    assert manager.release_removed("mara") and manager.release_removed("mara") is False
    assert manager.create_user("mara", "another-password") and manager.removed() == {}


def test_a_password_longer_than_the_hash_reads_neither_fails_nor_locks_anyone_out(accounts):
    auth_mod, manager = accounts
    long_one = "correct horse battery staple " * 4                             # 116 characters
    assert manager.create_user("lena", long_one)                               # was a server error
    assert manager.verify_password("lena", long_one)
    assert manager.verify_password("lena", "x" * 200) is False                 # and so was this, at sign-in
    assert manager.verify_password("lena", long_one[:72] + "anything")         # only the first 72 bytes are read…
    assert manager.verify_password("lena", long_one[:71]) is False             # …and all of those count
    assert manager.admin_set_password("lena", "é" * 60) and manager.verify_password("lena", "é" * 60)
    assert auth_mod._verify_password("whatever", "not-a-hash") is False


def test_a_permission_only_takes_the_kind_of_value_it_holds(accounts):
    auth_mod, manager = accounts
    assert auth_mod.privilege_problems({"can_use_browser": "no", "max_messages_per_day": -5, "made_up": 1,
                                        "can_use_agent": False, "allowed_models": ["a"]}) == ["can_use_browser", "max_messages_per_day"]
    assert auth_mod.privilege_problems({"max_messages_per_day": "lots"}) == ["max_messages_per_day"]
    assert auth_mod.privilege_problems({"max_messages_per_day": True}) == ["max_messages_per_day"]
    assert auth_mod.privilege_problems({"max_messages_per_day": 40.0, "can_use_browser": True}) == []
    before = manager.get_privileges("mara")
    manager.set_privileges("mara", {"can_use_browser": "no", "max_messages_per_day": -5})      # ignored, not stored
    assert manager.get_privileges("mara") == before and manager.privilege_overrides("mara") == {}
    manager.set_privileges("mara", {"can_use_browser": False, "max_messages_per_day": 40.0})
    assert manager.privilege_overrides("mara") == {"can_use_browser": False, "max_messages_per_day": 40}


# ── the console: delete, keep or erase, free the name ──────────────────────

@pytest.fixture
def console(store, accounts, monkeypatch):
    from fastapi import HTTPException  # noqa: F401  (tests below use it)
    import routes.account_routes as routes

    auth_mod, manager = accounts
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")      # for speed
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "get_current_user", lambda request: "zed")
    memory = _memory(store.root)
    sessions = types.SimpleNamespace(sessions={}, get_sessions_for_user=lambda user: {})
    router = routes.setup_account_routes(manager, sessions, memory)
    state = types.SimpleNamespace(upload_handler=_uploads(store), personal_docs_manager=None,
                                  invalidate_token_cache=lambda: None)
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state), cookies={}, headers={},
                                    client=types.SimpleNamespace(host="127.0.0.1"))

    def call(method, path, **kwargs):
        endpoint = next(r.endpoint for r in router.routes if r.path == path and method in r.methods)
        return endpoint(request=request, **kwargs)

    return types.SimpleNamespace(call=call, manager=manager, memory=memory, store=store, state=state,
                                 left=lambda name: store.data.leftover(name, memory_manager=memory))


USER = "/api/admin/users/{username}"


def test_a_deleted_accounts_data_stays_out_of_reach_until_it_is_erased(console, monkeypatch):
    from fastapi import HTTPException
    from core import access
    from services import push

    monkeypatch.delenv("AEGIS_PUSH_DISABLED", raising=False)
    _everything(console.store, "mara", console.memory)
    (console.store.root / "push_subscriptions.json").write_text(json.dumps({"mara": [{"endpoint": "https://fcm.googleapis.com/x"}]}))

    deleted = console.call("DELETE", USER, username="mara", data="keep")
    assert deleted["name_free"] is False and deleted["left"]["chats"] == 1 and deleted["left"]["memories"] == 1
    assert push.devices("mara") == []                          # her phone stops getting this name's notifications at once
    overview = console.call("GET", "/api/admin/overview")
    assert [(r["username"], r["by"], r["left"]["chats"]) for r in overview["removed"]] == [("mara", "zed", 1)]
    assert "keeps bees" not in json.dumps(overview)            # how much, never what

    with pytest.raises(HTTPException) as refused:
        console.call("POST", "/api/admin/users", body={"username": "mara", "password": "a-new-password"})
    assert refused.value.status_code == 409 and "Deleted accounts" in refused.value.detail

    erased = console.call("POST", "/api/admin/removed/{username}/erase", username="mara")
    assert erased["name_free"] is True and erased["failed"] == [] and erased["removed"]["sessions"] == 1
    assert console.call("GET", "/api/admin/overview")["removed"] == []
    console.call("POST", "/api/admin/users", body={"username": "mara", "password": "a-new-password"})
    assert console.left("mara")["total"] == 0                   # the new account starts with nothing
    events = [e["event"] for e in access.read_audit(50)]
    assert events[:3] == ["account_created", "data_erased", "account_deleted"]


def test_delete_and_erase_in_one_step_frees_the_name(console):
    _everything(console.store, "mara", console.memory)
    done = console.call("DELETE", USER, username="mara", data="erase")
    assert done["name_free"] is True and done["failed"] == [] and done["removed"]["memories"] == 1
    assert console.manager.removed() == {} and console.left("mara")["total"] == 0


def test_an_account_that_stored_nothing_leaves_no_reserved_name(console):
    done = console.call("DELETE", USER, username="mara", data="keep")
    assert done["name_free"] is True and console.manager.removed() == {}
    assert console.call("POST", "/api/admin/users", body={"username": "mara", "password": "a-new-password"})["ok"]


def test_what_could_not_be_erased_keeps_the_name_reserved(console):
    from fastapi import HTTPException
    _everything(console.store, "mara", console.memory)
    request_state = console.state
    request_state.upload_handler = None                                         # the upload store is not there
    done = console.call("DELETE", USER, username="mara", data="erase")
    assert done["name_free"] is False and done["failed"] == ["uploads"]
    removed = console.call("GET", "/api/admin/overview")["removed"]
    assert removed[0]["erase_failed"] == ["uploads"] and removed[0]["left"]["total"] == 1
    with pytest.raises(HTTPException):
        console.call("POST", "/api/admin/users", body={"username": "mara", "password": "a-new-password"})
    request_state.upload_handler = _uploads(console.store)
    assert console.call("POST", "/api/admin/removed/{username}/erase", username="mara")["name_free"] is True


def test_freeing_a_name_without_erasing_hands_the_data_to_the_next_account_on_purpose(console):
    from core import access
    _everything(console.store, "mara", console.memory)
    console.call("DELETE", USER, username="mara", data="keep")
    assert console.call("DELETE", "/api/admin/removed/{username}", username="mara")["ok"]
    console.call("POST", "/api/admin/users", body={"username": "mara", "password": "a-new-password"})
    assert console.left("mara")["memories"] == 1 and console.left("mara")["chats"] == 1
    assert "name_freed" in [e["event"] for e in access.read_audit(50)]


def test_only_what_a_deleted_account_left_can_be_erased(console):
    from fastapi import HTTPException
    _everything(console.store, "mara", console.memory)
    for name in ("zed", "mara", "nobody", ""):                                  # accounts that exist, and no account at all
        with pytest.raises(HTTPException) as refused:
            console.call("POST", "/api/admin/removed/{username}/erase", username=name)
        assert refused.value.status_code == 404
    with pytest.raises(HTTPException) as refused:
        console.call("DELETE", "/api/admin/removed/{username}", username="mara")
    assert refused.value.status_code == 404
    with pytest.raises(HTTPException) as refused:
        console.call("DELETE", USER, username="mara", data="everything")
    assert refused.value.status_code == 400 and "mara" in console.manager.users
    assert console.left("mara")["memories"] == 1
