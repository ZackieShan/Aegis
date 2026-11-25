"""Answers read aloud, in a voice the person chose — their own, if they recorded one.

What was there before: a cloned voice in the Studio and an engine to speak it, but no speaker under
an answer (the app asked whether the house's saved engine could speak, and on a server without
Kokoro it could not, although the cloned voice could), one voice for the whole house set by the
administrator, and a whole answer sent off to a slow engine in one piece.

What is checked here:

  whose voice
    - a recorded voice belongs to whoever recorded it and reads only to them until they share it
    - a voice from before owners were kept is the administrators'
    - each person chooses their own reading voice and whether answers are read by themselves
    - nobody is read to in a voice that is not theirs to use, by any route
  the reading
    - an answer is cut into pieces of about a sentence and made a piece at a time
    - the reading starts when enough is ready not to run dry; a voice that keeps up starts at once
    - the pace of each voice is measured from the pieces actually made
    - making speech never holds up the rest of the app
  the screens
    - a speaker under every answer, a switch to read every answer, the voice in Settings
    - the Studio's Voice Lab: "Read to me in this voice", and sharing
    - the family screen: the same reader, its own sheet, and a line that says how the voice is getting on

Every name, voice and sentence in here is made up.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
READ_ALOUD_JS = (REPO / "static" / "js" / "readAloud.js").read_text(encoding="utf-8")
TTS_AI_JS = (REPO / "static" / "js" / "tts-ai.js").read_text(encoding="utf-8")
CHAT_JS = (REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
RENDERER_JS = (REPO / "static" / "js" / "chatRenderer.js").read_text(encoding="utf-8")
STUDIO_JS = (REPO / "static" / "js" / "studioMusic.js").read_text(encoding="utf-8")
PAGE = (REPO / "static" / "index.html").read_text(encoding="utf-8")
STYLE = (REPO / "static" / "style.css").read_text(encoding="utf-8")
HOME_JS = (REPO / "static" / "lite" / "home.js").read_text(encoding="utf-8")
SPEECH_JS = (REPO / "static" / "lite" / "speech.js").read_text(encoding="utf-8")
ROUTES = (REPO / "routes" / "tts_routes.py").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _wav(seconds: float) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(1)
        sound.setframerate(8000)
        sound.writeframes(b"\x80" * int(8000 * seconds))
    return out.getvalue()


class House:
    """A voice service for a house with two recorded voices and an engine that speaks them."""

    def __init__(self, settings=None, built_in=None, works=False, delay=0.0):
        self.settings = {"tts_enabled": True, "tts_provider": "local", "tts_model": "tts-1", "tts_voice": "robin", "tts_speed": "1"}
        self.settings.update(settings or {})
        self.built_in = built_in or []
        self.works = works                       # the house's own engine (not the cloning one) can speak
        self.delay = delay
        self.asked = []

    def _load_settings(self):
        return dict(self.settings)

    @property
    def available(self):
        return self.works and self.settings.get("tts_enabled") is not False and self.settings["tts_provider"] != "disabled"

    def is_routable_cloned_voice(self, voice):
        from services.tts.tts_service import cloned_voice_catalog
        if self.settings.get("tts_enabled") is False or self.settings["tts_provider"] == "disabled":
            return False
        return any(v["id"] == voice for v in cloned_voice_catalog())

    def list_voices(self, provider=None):
        return list(self.built_in)

    def synthesize(self, text, use_cache=True, voice=None):
        self.asked.append((text, voice))
        if self.delay:
            time.sleep(self.delay)
        return _wav(2.0)

    def synthesize_to_base64(self, text, voice=None):
        return "UklGRg=="

    def clear_cache(self):
        pass

    def get_stats(self):
        return {}


@pytest.fixture
def house(tmp_path, monkeypatch):
    """A data folder of its own: two recorded voices, nobody's preferences, no pace measured yet."""
    voices = tmp_path / "voices"
    voices.mkdir()
    for name in ("robin", "ada", "old-timer"):
        (voices / f"{name}.wav").write_bytes(_wav(4.0))
    monkeypatch.setattr("src.constants.VOICES_DIR", str(voices))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("routes.prefs_routes.PREFS_FILE", str(tmp_path / "prefs.json"))
    from services.tts import reading
    reading.claim("robin", "sam")                       # Sam recorded Robin and keeps it to himself
    reading.claim("ada", "dana", shared=True)           # Dana recorded Ada and shared it
    return House()                                      # "old-timer" has no record: from before owners were kept


# ── whose voice ────────────────────────────────────────────────────────────

def test_a_recorded_voice_is_its_makers_until_they_share_it(house):
    from services.tts import reading
    assert reading.may_use("robin", "sam", False) and not reading.may_use("robin", "dana", False) and not reading.may_use("robin", "mia", False)
    assert not reading.may_use("robin", "boss", True)                    # not even an administrator is read to in it
    assert reading.may_use("ada", "mia", False) and reading.may_use("ada", "dana", False)
    reading.share("robin", True)
    assert reading.may_use("robin", "mia", False)
    reading.share("robin", False)
    assert not reading.may_use("robin", "mia", False)


def test_a_voice_from_before_owners_were_kept_is_the_administrators(house):
    from services.tts import reading
    assert reading.record("old-timer") == {}
    assert reading.may_use("old-timer", "boss", True) and not reading.may_use("old-timer", "mia", False)
    assert reading.may_change("old-timer", "boss", True) and not reading.may_change("old-timer", "mia", False)


def test_only_its_owner_or_an_administrator_changes_a_voice(house):
    from services.tts import reading
    assert reading.may_change("robin", "sam", False) and not reading.may_change("robin", "dana", False) and reading.may_change("robin", "boss", True)
    reading.forget("robin")
    assert reading.record("robin") == {}


def test_with_no_accounts_every_voice_is_the_one_persons(house):
    from services.tts import reading
    assert reading.may_use("robin", None, False) and reading.may_use("robin", "", False) and reading.may_change("ada", None, False)


def test_each_person_sees_their_own_voices_first_then_the_shared_ones(house):
    from services.tts import reading
    assert [(v["id"], v["mine"], v["shared"]) for v in reading.cloned_voices("sam", False)] == [("robin", True, False), ("ada", False, True)]
    assert [(v["id"], v["mine"]) for v in reading.cloned_voices("mia", False)] == [("ada", False)]
    assert [(v["id"], v["mine"]) for v in reading.cloned_voices("boss", True)] == [("old-timer", True), ("ada", False)]
    assert all(v["can_change"] for v in reading.cloned_voices("boss", True))
    assert [v["can_change"] for v in reading.cloned_voices("mia", False)] == [False]


# ── each person's choice ───────────────────────────────────────────────────

def test_the_choice_is_kept_with_the_person(house):
    from services.tts import reading
    assert reading.prefs("mia") == {"voice": "", "auto": False}
    assert reading.save_prefs("mia", voice="ada") == {"voice": "ada", "auto": False}
    assert reading.save_prefs("mia", auto=True) == {"voice": "ada", "auto": True}            # one does not undo the other
    assert reading.prefs("sam") == {"voice": "", "auto": False}                              # and it is hers alone
    kept = json.loads((Path(__import__("routes.prefs_routes", fromlist=["x"]).PREFS_FILE)).read_text())
    assert kept["_users"]["mia"]["reading"] == {"voice": "ada", "auto": True}


def test_what_a_person_can_be_read_to_in(house):
    from services.tts import reading
    assert [c["id"] for c in reading.choices(house, "mia", False)] == ["device", "ada"]
    assert [c["id"] for c in reading.choices(house, "sam", False)] == ["device", "robin", "ada"]
    assert reading.choices(house, "sam", False)[1]["pace"] == reading.DEFAULT_PACE and reading.choices(house, "sam", False)[0]["pace"] is None
    # an engine with voices of its own adds them; a recording is never listed twice
    assert [c["id"] for c in reading.choices(House(built_in=[{"id": "af_heart", "label": "Heart"}, {"id": "ada", "label": "ada"}], works=True), "mia", False)] == ["device", "ada", "af_heart"]
    # speech switched off for the whole house leaves a device's own voice and nothing else
    for off in ({"tts_enabled": False}, {"tts_provider": "disabled"}):
        assert [c["id"] for c in reading.choices(House(settings=off), "sam", False)] == ["device"]


def test_the_voice_that_reads_to_someone(house):
    from services.tts import reading
    assert reading.house_voice(house) == "robin"
    assert reading.resolve(house, "sam", False) == "robin"                   # the house's voice, which is his
    assert reading.resolve(house, "mia", False) == "device"                  # the house's voice is not hers to use
    reading.save_prefs("mia", voice="ada")
    assert reading.resolve(house, "mia", False) == "ada"
    reading.save_prefs("sam", voice="device")
    assert reading.resolve(house, "sam", False) == "device"                  # he prefers the phone's own voice
    assert reading.resolve(house, "sam", False, asked="ada") == "ada"        # a preview of another he may use
    assert reading.resolve(house, "mia", False, asked="robin") == "ada"      # asking for one she may not changes nothing
    reading.share("ada", False)
    assert reading.resolve(house, "mia", False) == "device"                  # taken back: her choice no longer applies


# ── the pace ───────────────────────────────────────────────────────────────

def test_the_pace_of_a_voice_is_measured_from_what_was_made(house):
    from services.tts import reading
    assert reading.pace_for("ada") == reading.DEFAULT_PACE == 0.55
    reading.note_pace("ada", 4.0, 17.0)                                      # the first piece: the engine had to load
    assert reading.pace_for("ada") == 0.55                                   # one timing does not make it look twice as slow
    for _ in range(5):
        reading.note_pace("ada", 8.0, 16.0)
    assert reading.pace_for("ada") == 0.5                                    # the middle of what was measured
    reading.note_pace("ada", 0.4, 0.2)                                       # too short to say anything
    reading.note_pace("ada", 9.0, 0.01)                                      # came back at once: it was already made
    assert reading._paces()["ada"] == [0.235, 0.5, 0.5, 0.5, 0.5, 0.5]
    for _ in range(12):
        reading.note_pace("ada", 9.0, 5.0)                                   # a faster engine was installed
    assert reading.pace_for("ada") == 1.8 and len(reading._paces()["ada"]) == reading.PACE_SAMPLES
    assert reading.speech_seconds(_wav(3.0)) == 3.0 and reading.speech_seconds(b"ID3 not a wav") == 0.0
    # a file from when one number was kept is still read
    (Path(__import__("src.constants", fromlist=["x"]).DATA_DIR) / "tts_pace.json").write_text('{"robin": 0.29}')
    assert reading._paces() == {"robin": [0.29]} and reading.pace_for("robin") == 0.55


def test_a_voice_not_timed_yet_is_expected_to_go_as_the_others_of_its_kind_do(house):
    from services.tts import reading
    for took in (4.8, 5.0, 5.2, 5.0, 4.9):
        reading.note_pace("ada", 10.0, took)                                     # Ada's engine makes two seconds of speech in one
    assert reading.pace_for("ada") == 2.0
    assert reading.pace_for("robin") == 2.0                                      # the same engine makes Robin: no reason to expect less
    assert reading.pace_for("af_heart") == reading.DEFAULT_PACE                  # a built-in voice is another engine altogether
    reading.note_pace("robin", 10.0, 20.0)                                       # one slow piece (the engine was being loaded)
    assert reading.pace_for("robin") == 2.0
    for _ in range(4):
        reading.note_pace("robin", 10.0, 20.0)                                   # five of them: Robin really is slow
    assert reading.pace_for("robin") == 0.5
    reading.note_pace("af_heart", 10.0, 2.0)
    assert reading.pace_for("af_bella") == 5.0                                   # another built-in voice goes as that one does
    assert reading.pace_for("old-timer") == 1.21                                 # a recording: between Ada's last three and Robin's


def test_a_deleted_voice_takes_its_timings_with_it(house):
    from services.tts import reading
    for _ in range(5):
        reading.note_pace("robin", 10.0, 20.0)
    reading._spoke_at["robin"] = time.time()
    reading.forget("robin")
    assert "robin" not in reading._paces() and "robin" not in reading._spoke_at
    assert reading.record("robin") == {}


def test_one_piece_is_made_and_timed(house):
    from services.tts import reading
    slow = House(delay=0.6)
    made = reading.say(slow, "  Keep flour\n in a tin.  ", "ada")
    assert made["audio"][:4] == b"RIFF" and made["seconds"] == 2.0 and made["took"] >= 0.6 and made["fresh"] is True
    assert slow.asked == [("Keep flour in a tin.", "ada")]
    assert 2.5 < reading._paces()["ada"][0] < 3.5                            # two seconds of speech in about 0.6: kept
    quick = reading.say(House(), "Keep flour in a tin.", "robin")
    assert quick["fresh"] is False and "robin" not in reading._paces()       # nothing learned from a piece that was already made
    assert reading.say(house, "   ", "ada")["audio"] is None
    long = House()
    reading.say(long, "word " * 400, "ada")
    assert len(long.asked[0][0]) <= reading.PIECE_CHARS


# ── the routes ─────────────────────────────────────────────────────────────

class People:
    is_configured = True

    def is_admin(self, name):
        return name == "boss"

    def get_privileges(self, name):
        return {"can_generate_images": name != "mia"}                         # Mia has the family screen: no Studio


@pytest.fixture
def client(house):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes.tts_routes import setup_tts_routes

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user") or None
        return await call_next(request)

    app.state.auth_manager = People()
    app.include_router(setup_tts_routes(house))
    test = TestClient(app)
    test.house = house
    return test


def _as(name):
    return {"x-user": name}


def test_asking_what_one_can_be_read_to_in(client):
    mine = client.get("/api/tts/reading", headers=_as("mia")).json()
    assert [v["id"] for v in mine["voices"]] == ["device", "ada"] and mine["reads_in"] == "device" and mine["voice"] == "" and mine["auto"] is False
    assert mine["house"] == "robin"
    sams = client.get("/api/tts/reading", headers=_as("sam")).json()
    assert sams["reads_in"] == "robin" and [v["id"] for v in sams["voices"]] == ["device", "robin", "ada"]


def test_choosing_a_voice_and_being_refused_one_that_is_not_yours(client):
    refused = client.put("/api/tts/reading", json={"voice": "robin"}, headers=_as("mia"))
    assert refused.status_code == 403 and "not yours" in refused.json()["detail"]
    assert client.put("/api/tts/reading", json={"voice": "ada"}, headers=_as("mia")).json() == {"voice": "ada", "auto": False, "reads_in": "ada"}
    assert client.put("/api/tts/reading", json={"auto": True}, headers=_as("mia")).json() == {"voice": "ada", "auto": True, "reads_in": "ada"}
    assert client.put("/api/tts/reading", json={"voice": "device"}, headers=_as("mia")).json()["reads_in"] == "device"
    assert client.put("/api/tts/reading", json={"voice": ""}, headers=_as("sam")).json()["reads_in"] == "robin"       # "" is the house's voice


def test_a_piece_of_speech_in_ones_own_voice(client):
    client.put("/api/tts/reading", json={"voice": "ada"}, headers=_as("mia"))
    said = client.post("/api/tts/say", json={"text": "Keep flour in a tin."}, headers=_as("mia"))
    assert said.status_code == 200 and said.headers["content-type"] == "audio/wav" and said.content[:4] == b"RIFF"
    assert said.headers["x-speech-seconds"] == "2.0" and said.headers["x-voice"] == "ada" and float(said.headers["x-pace"]) > 0
    assert client.house.asked[-1] == ("Keep flour in a tin.", "ada")
    assert client.post("/api/tts/say", json={"text": "Hello.", "voice": "ada"}, headers=_as("sam")).headers["x-voice"] == "ada"     # a preview


def test_nobody_is_read_to_in_a_voice_that_is_not_theirs(client):
    asked_before = len(client.house.asked)
    assert client.post("/api/tts/say", json={"text": "Hello.", "voice": "robin"}, headers=_as("mia")).status_code == 403
    # with nothing of her own chosen, the screen is told to use the device's voice: the house's voice is Sam's
    nothing = client.post("/api/tts/say", json={"text": "Hello."}, headers=_as("mia"))
    assert nothing.status_code == 409 and nothing.json()["detail"]["code"] == "device"
    # the older endpoint, which reads in the house's saved voice, holds to the same
    assert client.post("/api/tts/synthesize", json={"text": "Hello."}, headers=_as("mia")).status_code == 503
    assert client.post("/api/tts/synthesize", json={"text": "Hello.", "voice": "robin"}, headers=_as("mia")).status_code == 403
    assert len(client.house.asked) == asked_before                               # and nothing was made
    assert client.post("/api/tts/synthesize", json={"text": "Hello."}, headers=_as("sam")).status_code == 200
    assert client.house.asked[-1] == ("Hello.", None)                            # the house's voice, for the one whose voice it is


def test_the_studios_list_sharing_and_deleting(client):
    assert [(v["id"], v["mine"], v["shared"], v["can_change"]) for v in client.get("/api/tts/my-voices", headers=_as("sam")).json()["voices"]] == [
        ("robin", True, False, True), ("ada", False, True, False)]
    assert client.put("/api/tts/my-voices/robin", json={"shared": True}, headers=_as("dana")).status_code == 403
    assert client.delete("/api/tts/my-voices/robin", headers=_as("dana")).status_code == 403
    assert client.put("/api/tts/my-voices/robin", json={"shared": True}, headers=_as("sam")).json() == {"ok": True, "voice": "robin", "shared": True}
    assert "robin" in [v["id"] for v in client.get("/api/tts/reading", headers=_as("mia")).json()["voices"]]
    assert client.put("/api/tts/my-voices/nobody", json={"shared": True}, headers=_as("sam")).status_code == 404
    # an administrator taking charge of a voice from before owners were kept becomes its owner
    assert client.put("/api/tts/my-voices/old-timer", json={"shared": False}, headers=_as("boss")).status_code == 200
    from services.tts import reading
    assert reading.record("old-timer")["owner"] == "boss"
    # deleting takes the record with it
    assert client.delete("/api/tts/my-voices/robin", headers=_as("sam")).json() == {"ok": True}
    assert reading.record("robin") == {} and "robin" not in [v["id"] for v in client.get("/api/tts/my-voices", headers=_as("sam")).json()["voices"]]
    assert client.get("/api/tts/my-voices", headers=_as("mia")).status_code == 403          # the Studio is not hers


def test_the_catalogue_lists_a_recording_only_for_those_who_may_use_it(client):
    client.house.built_in = [{"id": "robin", "label": "robin"}, {"id": "ada", "label": "ada"}, {"id": "af_heart", "label": "Heart"}]
    assert [v["id"] for v in client.get("/api/tts/voices", headers=_as("mia")).json()["voices"]] == ["ada", "af_heart"]
    assert [v["id"] for v in client.get("/api/tts/voices", headers=_as("sam")).json()["voices"]] == ["robin", "ada", "af_heart"]


def test_a_new_recording_is_its_makers_and_cannot_take_anothers_name(client, monkeypatch):
    import routes.tts_routes as tts_routes

    def fake_convert(data, ext, dest):
        Path(dest).write_bytes(_wav(5.0))
        return 5.0

    monkeypatch.setattr(tts_routes, "_convert_to_wav", fake_convert)
    sample = {"file": ("sample.wav", _wav(5.0) + b"\x00" * 2000, "audio/wav")}
    made = client.post("/api/tts/my-voices", data={"name": "Juniper"}, files=sample, headers=_as("dana"))
    assert made.status_code == 200 and made.json()["voice"] == "juniper"
    from services.tts import reading
    assert reading.record("juniper")["owner"] == "dana" and reading.record("juniper")["shared"] is False
    assert not reading.may_use("juniper", "sam", False)
    taken = client.post("/api/tts/my-voices", data={"name": "Juniper"}, files=sample, headers=_as("sam"))
    assert taken.status_code == 403 and "someone else" in taken.json()["detail"]
    assert client.post("/api/tts/my-voices", data={"name": "Juniper"}, files=sample, headers=_as("dana")).status_code == 200      # her own, re-recorded


def test_deleting_or_re_recording_a_voice_removes_what_the_engines_prepared_from_it(client, monkeypatch):
    import routes.tts_routes as tts_routes
    from src import constants

    voices = Path(constants.VOICES_DIR)
    for name in ("robin", "ada"):
        for suffix in tts_routes._BAKED_SUFFIXES:
            (voices / f"{name}.wav{suffix}").write_bytes(b"prepared from the old recording")
    assert client.delete("/api/tts/my-voices/robin", headers=_as("sam")).json() == {"ok": True}
    assert [p.name for p in voices.glob("robin*")] == []                         # nothing of the voice is left behind

    def fake_convert(data, ext, dest):
        Path(dest).write_bytes(_wav(5.0))
        return 5.0

    monkeypatch.setattr(tts_routes, "_convert_to_wav", fake_convert)
    sample = {"file": ("sample.wav", _wav(5.0) + b"\x00" * 2000, "audio/wav")}
    assert client.post("/api/tts/my-voices", data={"name": "Ada"}, files=sample, headers=_as("dana")).status_code == 200
    assert sorted(p.name for p in voices.glob("ada*")) == ["ada.wav"]             # each engine prepares it again from the new one


# ── having the voice ready ─────────────────────────────────────────────────

def test_a_recorded_voice_is_got_ready_while_the_answer_is_thought_about(house):
    from services.tts import reading
    reading._spoke_at.clear()
    assert reading.warm(house, "robin", wait=True) is True
    assert house.asked == [(reading.WARM_WORDS, "robin")]
    assert reading.pace_for("robin") == reading.DEFAULT_PACE                     # a warm-up is mostly loading: it is not timed
    assert reading.warm(house, "robin", wait=True) is False                      # it spoke a moment ago: still loaded
    assert len(house.asked) == 1
    reading._spoke_at["robin"] -= reading.WARM_FOR + 1                            # some minutes later it has been put away again
    assert reading.warm(house, "robin", wait=True) is True and len(house.asked) == 2


def test_a_voice_that_has_just_read_something_is_not_warmed(house):
    from services.tts import reading
    reading._spoke_at.clear()
    house.delay = 0.6                                                            # long enough to count as freshly made
    assert reading.say(house, "Keep flour in a tin.", "ada")["fresh"] is True
    house.delay = 0.0
    assert reading.warm(house, "ada", wait=True) is False
    assert [voice for _, voice in house.asked] == ["ada"]


def test_only_a_recording_is_warmed(house):
    from services.tts import reading
    reading._spoke_at.clear()
    house.works, house.built_in = True, [{"id": "af_heart", "label": "Heart"}]     # a built-in voice may be a service that charges
    assert reading.warm(house, "af_heart", wait=True) is False
    assert reading.warm(house, reading.DEVICE, wait=True) is False
    assert reading.warm(house, "", wait=True) is False
    assert reading.warm(house, "nobody", wait=True) is False
    house.settings["tts_enabled"] = False                                       # speech switched off for the whole server
    assert reading.warm(house, "robin", wait=True) is False
    assert house.asked == []


def test_one_warm_up_at_a_time_and_a_failure_is_not_remembered(house):
    import threading
    from services.tts import reading
    reading._spoke_at.clear()
    gate, begun = threading.Event(), threading.Event()
    real = house.synthesize

    def slow(text, use_cache=True, voice=None):
        begun.set()
        gate.wait(5)
        return real(text, use_cache, voice)

    house.synthesize = slow
    assert reading.warm(house, "robin") is True
    assert begun.wait(5)
    assert reading.warm(house, "robin") is False                                 # already being warmed
    gate.set()
    for _ in range(100):
        if "robin" not in reading._warming:
            break
        time.sleep(0.02)
    assert "robin" not in reading._warming and len(house.asked) == 1

    reading._spoke_at.clear()
    house.synthesize = lambda text, use_cache=True, voice=None: None             # the engine is not there
    assert reading.warm(house, "robin", wait=True) is True
    assert "robin" not in reading._spoke_at                                      # so the next question tries again


def test_the_screen_asks_for_the_voice_as_the_question_is_sent(client):
    from services.tts import reading
    reading._spoke_at.clear()
    assert client.post("/api/tts/warm", headers=_as("mia")).json() == {"warming": False}       # she is read to by her device
    assert client.house.asked == []
    assert client.post("/api/tts/warm", headers=_as("sam")).json() == {"warming": True}        # the house's voice is his recording
    for _ in range(100):
        if "robin" not in reading._warming:
            break
        time.sleep(0.02)
    assert client.house.asked == [(reading.WARM_WORDS, "robin")]
    assert client.post("/api/tts/warm", headers=_as("sam")).json() == {"warming": False}

    # the shared reader asks once a minute at most, and never for a device's own voice
    assert "export async function warm()" in READ_ALOUD_JS and "'/api/tts/warm'" in READ_ALOUD_JS
    warm = READ_ALOUD_JS.split("export async function warm()")[1].split("\n}\n")[0]
    assert "mine.reads_in === 'device'" in warm and "warmedAt < 60000" in warm
    # the full app: when answers are read by themselves, as the answer starts to arrive
    start = TTS_AI_JS.split("streamingStart() {")[1].split("}\n")[0]
    assert start.index("!this.autoPlay) return") < start.index("readAloud.warm()")
    # the family screen: when answers are read by themselves, or the question was spoken
    assert "if (S.reading.auto || S.spoke) readAloudLib.warm();" in HOME_JS
    assert "{ readAloudLib.unlock(); readAloudLib.warm(); }" in HOME_JS


def test_the_wait_is_told_from_what_the_voice_has_managed_before():
    # Before its first piece arrives a reading has measured nothing; it goes by the pace kept for the voice,
    # not by a fixed guess (which made every reading begin by saying "about 40 s").
    assert "learn(mine, voice)" in READ_ALOUD_JS
    assert "this.learn(mine, mine.reads_in)" in READ_ALOUD_JS and "this.learn(mine, this.asked)" in READ_ALOUD_JS
    assert "pace: this.pace || this.known" in READ_ALOUD_JS


@needs_node
def test_a_voice_known_to_keep_up_says_it_will_start_in_seconds(tmp_path):
    script = tmp_path / "wait.mjs"
    script.write_text(f"""
import {{ startsIn }} from {json.dumps(str(REPO / 'static' / 'js' / 'readAloud.js'))};
const pieces = [7, 10, 10, 10, 10, 10];                         // seconds of speech still to be made
console.log(JSON.stringify({{
  keepsUp: startsIn({{ pace: 2.0, ready: 0, pieces }}),
  slow: startsIn({{ pace: 0.55, ready: 0, pieces }}),
  unknown: startsIn({{ pace: 0, ready: 0, pieces }}),
}}));
""", encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    assert out["keepsUp"] <= 4                                    # the first piece, and it can begin
    assert out["slow"] >= 30 and out["unknown"] >= 30             # these wait for most of the answer (up to the cap)


def test_the_clip_that_lets_a_phone_sound_later_is_one_the_apps_own_policy_allows():
    # The app's content policy lets sound come from the app and from blob: addresses. The silent clip played
    # inside a tap was a data: address: refused, so it unlocked nothing (found with the sound player's calls logged).
    policy = (REPO / "core" / "middleware.py").read_text(encoding="utf-8")
    assert "media-src 'self' blob:; " in policy
    assert "data:audio" not in READ_ALOUD_JS
    assert "function silence()" in READ_ALOUD_JS and "URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))" in READ_ALOUD_JS
    # and the clip is a real, short, silent WAV
    import base64
    clip = base64.b64decode(READ_ALOUD_JS.split("const SILENCE = '")[1].split("'")[0], validate=True)
    with wave.open(io.BytesIO(clip)) as sound:
        assert 0 < sound.getnframes() / sound.getframerate() <= 0.1
        assert set(sound.readframes(sound.getnframes())) == {0x80}                # 8-bit silence
    # a reading that has already sounded is not handed the clip: its ending would pass for the end of a piece
    unlock = READ_ALOUD_JS.split("export function unlock() {")[1].split("\n}\n")[0]
    assert unlock.index("current.started") < unlock.index("audio.src = silence()")


def test_the_family_screen_keeps_the_speaker_and_the_line_through_a_new_drawing():
    # The conversation is drawn again now and then; each drawing gives the answer a new speaker button.
    start = HOME_JS.split("function readAloud(message, button) {")[1].split("\n}\n")[0]
    assert "message.node.querySelector('.say')" in start                         # the one on the screen now
    assert "message.saying = line;" in start and "if (message.saying === line) message.saying = null;" in start
    assert "if (message.saying) node.append(message.saying);" in HOME_JS         # a new drawing carries the line along
    assert "if (button && button.isConnected) clear(button, icon('speaker', 'sm'))" not in HOME_JS


def test_making_speech_never_holds_up_the_rest_of_the_app():
    # A slow engine takes seconds over a piece. Run on the event loop, every other request waited for it.
    assert "made = await asyncio.to_thread(reading.say, tts_service, body.text, voice)" in ROUTES
    assert "audio_data = await asyncio.to_thread(tts_service.synthesize, request.text, True, voice or None)" in ROUTES
    assert "await asyncio.to_thread(tts_service.synthesize_to_base64, request.text, voice or None)" in ROUTES
    assert "= tts_service.synthesize(" not in ROUTES


def test_the_house_counts_as_able_to_speak_when_its_recorded_voice_can():
    source = (REPO / "services" / "tts" / "tts_service.py").read_text(encoding="utf-8")
    assert 'is_available = (self.available or self.is_routable_cloned_voice(settings["tts_voice"])) and tts_enabled' in source


# ── the reading itself ─────────────────────────────────────────────────────

def _node(script):
    out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=REPO, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


@needs_node
def test_what_is_said_has_no_markup_code_addresses_or_thinking():
    got = _node("""
        const { sayable } = await import('./static/js/readAloud.js');
        console.log(JSON.stringify([
          sayable('## Storing flour\\n\\nKeep it **cool and dry**. Use a tin, e.g. a biscuit tin.\\n\\n```bash\\nrm x\\n```\\nSee [the guide](https://example.org/a) or https://example.org/b.'),
          sayable('<think time="3.1">Plan the answer.</think>Use a tin.'),
          sayable('<think>Still planning'),
          sayable('| Kind | Keeps |\\n|---|---|\\n| White | 1 year |'),
          sayable('Half written ```python\\nprint(1'),
        ]));
    """)
    assert got[0] == "Storing flour. Keep it cool and dry. Use a tin, e.g. a biscuit tin. See the guide or."
    assert got[1] == "Use a tin." and got[2] == ""
    assert got[3] == "Kind Keeps. White 1 year" and got[4] == "Half written"


@needs_node
def test_an_answer_is_cut_into_pieces_of_about_a_sentence():
    got = _node("""
        const { pieces } = await import('./static/js/readAloud.js');
        const answer = 'Store flour in an airtight container, such as a food-grade plastic bucket with a gasket lid or a glass jar, to keep out moisture, pests and odors. '
          + 'Keep it cool. Keep it dark. Dr. Lee suggests glass. For storage beyond a year, add oxygen absorbers to the sealed container, or freeze the flour for a few days first to kill any insect eggs. Done!';
        const cut = pieces(answer);
        const long = pieces('word '.repeat(150).trim() + '.');
        const held = pieces('The first sentence is long enough to stand as a piece of its own without waiting. The second is still being writ', { whole: false });
        const short = pieces('Short start. Still being writ', { whole: false });
        console.log(JSON.stringify({ lengths: cut.pieces.map((p) => p.length), joined: cut.pieces.join(' ') === answer, first: cut.pieces[0], doctor: cut.pieces.some((p) => p.includes('Dr. Lee suggests glass.')),
          long: long.pieces.map((p) => p.length), held, short }));
    """)
    assert got["joined"] is True and max(got["lengths"]) <= 170 and len(got["lengths"]) == 3
    assert got["first"].endswith(",") and len(got["first"]) <= 120            # the long opening sentence is cut at a comma: a short first piece
    assert got["doctor"] is True                                              # "Dr." does not end a sentence
    assert min(got["lengths"]) >= 25                                          # "Done!" rides with the piece before it
    assert max(got["long"]) <= 170 and len(got["long"]) >= 4                  # one endless sentence still comes in pieces
    assert got["held"]["pieces"] == ["The first sentence is long enough to stand as a piece of its own without waiting."]
    assert got["short"] == {"pieces": [], "used": 0}                          # too little to be worth a piece yet: it waits for more


@needs_node
def test_the_reading_starts_when_it_will_not_run_dry():
    got = _node("""
        const { needed, plan, startsIn, describe } = await import('./static/js/readAloud.js');
        const r = (n) => Math.round(n * 10) / 10;
        console.log(JSON.stringify({
          // a piece can be played only once it is wholly made: one piece of 7.5 s at this pace takes 13.6 s
          need: [needed({ pace: 0.55, pieces: [7.5] }), needed({ pace: 0.5, pieces: [5, 5, 5, 5] }), needed({ pace: 2, pieces: [5, 5] }), needed({ pace: 0.5, pieces: [] }), needed({ pace: 0.55, pieces: [7.5], progress: 6 })].map(r),
          nothing: plan({ pace: 0.5, ready: 0, pieces: [5] }),
          tooSoon: plan({ pace: 0.55, ready: 8, pieces: [7.5] }),
          enough: plan({ pace: 0.55, ready: 14, pieces: [7.5] }),
          keepsUp: [plan({ pace: 2, ready: 3, pieces: [5, 5] }), plan({ pace: 2, ready: 2, pieces: [5, 5] })],
          allMade: plan({ pace: 0.5, ready: 3, pieces: [] }),
          moreComing: plan({ pace: 0.5, ready: 3, pieces: [], more: true }),
          capFar: plan({ pace: 0.55, ready: 9, pieces: [9, 9, 9, 9, 9], waitedMs: 40000 }),
          capNear: plan({ pace: 0.55, ready: 25, pieces: [9, 9, 9], waitedMs: 40000 }),
          capOver: plan({ pace: 0.55, ready: 25, pieces: [9, 9, 9], waitedMs: 48500 }),
          wait: [startsIn({ pace: 0.5, ready: 0, pieces: [5, 5, 5, 5] }), startsIn({ pace: 0.55, ready: 8, pieces: [7.5] }), startsIn({ pace: 2, ready: 0, pieces: [6, 6, 6] }),
                 startsIn({ pace: 0.5, ready: 0, pieces: Array(40).fill(6) }), startsIn({ pace: 0.5, ready: 10, pieces: [5, 5], progress: 5 })].map(r),
          words: [describe({ phase: 'preparing', kind: 'server', ready: 2, total: 7, startsIn: 9 }), describe({ phase: 'preparing', kind: 'server', ready: 0, total: 1, startsIn: 0 }),
                  describe({ phase: 'preparing', kind: 'device' }), describe({ phase: 'waiting' }), describe({ phase: 'playing' }), describe({ phase: 'failed', message: 'No voice.' })],
        }));
    """)
    assert got["need"] == [13.6, 25, 2.5, 0, 7.6]
    assert (got["nothing"], got["tooSoon"], got["enough"]) == ("wait", "wait", "start")
    assert got["keepsUp"] == ["start", "wait"]                                # a voice that keeps up waits only for its next piece
    assert got["allMade"] == "start" and got["moreComing"] == "start"         # nothing left to make (so far): read what there is
    assert got["capFar"] == "start"                                           # past the cap and far from unbroken: start, and pause where it must
    assert got["capNear"] == "wait" and got["capOver"] == "start"             # past the cap but seconds from unbroken: those seconds are worth it, once
    # four pieces of 5 s at half pace: the last is made at 40 s and reached 15 s into the reading, so the reading starts at 25
    assert got["wait"] == [25, 5.6, 3, 40, 0]
    assert got["words"] == ["Getting the voice ready (2 of 7), about 9 s", "Getting the voice ready", "Starting", "Catching up", "Reading aloud", "No voice."]


@needs_node
def test_what_settings_says_about_a_voice():
    got = _node("""
        const { noteFor } = await import('./static/js/readingSettings.js');
        console.log(JSON.stringify([noteFor({ kind: 'device' }), noteFor({ kind: 'recorded', mine: true, pace: 0.55 }), noteFor({ kind: 'recorded', mine: false, owner: 'dana', pace: 1.8 }), noteFor(null)]));
    """)
    assert got[0].startswith("Starts at once.")
    assert got[1].startswith("Your recording.") and "about 18 s for every 10 s of speech" in got[1] and "A second listen is instant." in got[1]
    assert got[2].startswith("Recorded by dana.") and "keeps up with itself" in got[2]
    assert got[3] == ""


def test_the_reader_makes_pieces_in_order_and_falls_back_to_the_device():
    assert "fetch('/api/tts/say'" in READ_ALOUD_JS and "this.queue.find((p) => p.state === 'todo')" in READ_ALOUD_JS
    assert "if (answer && answer.status === 409) { this.toDevice(); return; }" in READ_ALOUD_JS       # nothing on the server for this person
    assert "if (!this.started && await deviceCanSpeak(this.lang)) { this.toDevice(); return; }" in READ_ALOUD_JS
    assert "export function unlock()" in READ_ALOUD_JS and "audio.src = silence()" in READ_ALOUD_JS     # a tap lets later sound start by itself
    assert "made.set(key, { url: piece.url, seconds: piece.seconds });" in READ_ALOUD_JS              # a second listen is instant
    assert "stopAll();\n  const reader = new Reader(options);" in READ_ALOUD_JS                       # one reading at a time


# ── the full app ───────────────────────────────────────────────────────────

def test_every_answer_has_a_speaker():
    assert "import { addAITTSButton, readAloudButton } from './tts-ai.js';" in RENDERER_JS
    at = RENDERER_JS.index("const [speaker, speakerStatus] = readAloudButton(() => copyMessageText(msgElement));")
    assert at < RENDERER_JS.index("visible.forEach(a => _addBtn(a, actions));")                # first in the row, and not one of the three "recent" ones
    assert ".ai-tts-button { display: none;" in STYLE and "body.can-read-aloud .ai-tts-button { display: inline-flex; }" in STYLE
    assert "document.body.classList.toggle('can-read-aloud', this.available);" in TTS_AI_JS
    assert "readAloud.unlock();" in TTS_AI_JS and "manager.enqueue(text, button);" in TTS_AI_JS
    assert "button.innerHTML = want === 'stop' ? ICON_STOP : MARK_HTML;" in TTS_AI_JS          # preparing shows the moving mark, playing a stop
    assert "import readAloud from './readAloud.js';" in TTS_AI_JS and "/api/tts/synthesize" not in TTS_AI_JS


def test_answers_can_be_read_by_themselves_as_they_are_written():
    item = PAGE[PAGE.index('id="overflow-tts-btn"') - 400:PAGE.index('id="overflow-tts-btn"') + 700]
    assert "Read answers aloud" in item and "hidden" not in item.split('id="overflow-tts-btn"')[1][:60]
    assert "#overflow-tts-btn { display: none; }" in STYLE and "body.can-read-aloud #overflow-tts-btn { display: flex; }" in STYLE
    assert "await mgr.setAuto(!mgr.autoPlay);" in (REPO / "static" / "app.js").read_text(encoding="utf-8")
    assert "await readAloud.chooseReading({ auto: !!on });" in TTS_AI_JS                       # kept with the account, not the browser
    # the answer's text is fed in as it is written, a step at a time, and finished with the last step's words
    assert "window.aiTTSManager.streamingUpdate(roundText);" in CHAT_JS and "window.aiTTSManager.streamingNewRound();" in CHAT_JS
    assert "window.aiTTSManager.streamingEnd(roundText);" in CHAT_JS
    assert "this._streaming = true;                     // after _begin" in TTS_AI_JS         # set first, starting the reading switched it off again
    assert "_streamSentencesSent" not in CHAT_JS


def test_the_voice_is_chosen_in_settings_by_everyone():
    card = PAGE[PAGE.index('id="reading-card"'):PAGE.index('id="reading-card"') + 1900]
    assert "Reading aloud" in card and 'id="reading-voice"' in card and 'id="reading-preview"' in card and 'id="reading-auto"' in card
    assert "data-admin-only" not in card
    assert PAGE.index('data-settings-panel="appearance"') < PAGE.index('id="reading-card"') < PAGE.index('data-settings-panel="shortcuts"')
    settings = (REPO / "static" / "js" / "readingSettings.js").read_text(encoding="utf-8")
    assert "await readAloud.chooseReading({ voice: select.value });" in settings and "document.addEventListener('aegis-voices-changed', () => fill(true));" in settings


def test_the_voice_lab_reads_to_you_in_a_voice_and_shares_it():
    assert "import readAloud from './readAloud.js';" in STUDIO_JS
    assert "'Read to me in this voice'" in STUDIO_JS and "await readAloud.chooseReading({ voice: name });" in STUDIO_JS
    assert "cur.tts_voice = name;" not in STUDIO_JS                                              # it used to change the voice for the whole house
    assert "shared ? 'Stop sharing' : 'Share with everyone at home'" in STUDIO_JS and "body: JSON.stringify({ shared: !shared })" in STUDIO_JS
    assert "'yours · only you can use it'" in STUDIO_JS and "if (canChange) {" in STUDIO_JS


# ── the family screen ──────────────────────────────────────────────────────

def test_the_family_screen_reads_with_the_same_reader():
    assert "import readAloud from '../js/readAloud.js';" in SPEECH_JS and "readAloud.read(String(text || '').slice(0, 6000)" in SPEECH_JS
    assert "/api/tts/synthesize" not in SPEECH_JS and "SpeechSynthesisUtterance" not in SPEECH_JS
    # words in another language are the phone's to say, where it has a voice for it
    assert "otherLanguage && await readAloud.deviceCanSpeak(lang) ? 'device' : ''" in SPEECH_JS
    assert "speak(card.text, { lang: card.to_code, otherLanguage: true })" in HOME_JS


def test_the_family_screen_has_its_own_sheet_and_says_how_the_voice_is_getting_on():
    assert "function readingSheet()" in HOME_JS and "row('speaker', 'Reading aloud: the voice, and whether by itself'" in HOME_JS
    sheet = HOME_JS[HOME_JS.index("function readingSheet()"):HOME_JS.index("function readingSheet()") + 4200]
    for part in ("readAloudLib.chooseReading({ voice: voice.id })", "readAloudLib.chooseReading({ auto: !mine.auto })", "'Hear it'", "`Read every answer aloud: ${mine.auto ? 'on' : 'off'}`"):
        assert part in sheet, part
    assert "const line = h('div', { class: 'saying', role: 'status', hidden: true }, working.mark(), h('span', {}));" in HOME_JS
    assert "if ((aloud || S.reading.auto) && !reply.error && !reply.speaking) readAloud(reply" in HOME_JS       # after a spoken question, or always if chosen
    assert HOME_JS.count("readAloudLib.unlock();") >= 4                                                         # every tap that leads to sound
    css = (REPO / "static" / "lite" / "home.css").read_text(encoding="utf-8")
    assert ".saying[hidden] { display: none; }" in css and ".voice-choice.on {" in css
