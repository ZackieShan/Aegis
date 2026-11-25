"""The translator: text, long text and photos into another language, on this machine.

What must hold:
  * a language can be named the way people name it, and a word inside a
    sentence is never mistaken for a language code;
  * the language of a text is told by looking where that is safe, and asked
    of the model where it is not;
  * the request to the model names both languages and asks for the
    translation only; a long text goes paragraph by paragraph and comes back
    with its paragraphs where they were;
  * the dedicated model is used when it is there, the chat model when it is
    not — except for a picture, which only the dedicated model can read;
  * what a person translated is theirs alone, and theirs to forget.

Every sentence here is made up, and no test needs a model: the engine is a
stand-in that answers in a recognisable way.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import translate
from services.translate import core, detect, engine, history, languages

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent


# ── naming a language ──────────────────────────────────────────────────────

@pytest.mark.parametrize("said, code", [
    ("es", "es"), ("Spanish", "es"), ("español", "es"), ("ESPAÑOL", "es"), ("the Spanish language", "es"),
    ("zh-TW", "zh-Hant"), ("zh_CN", "zh-Hans"), ("Mandarin", "zh-Hans"), ("Chinese (Traditional)", "zh-Hant"),
    ("pt-BR", "pt-BR"), ("pt_br", "pt-BR"), ("Brazilian Portuguese", "pt-BR"), ("farsi", "fa"), ("Tagalog", "tl"),
    ("Haitian Creole", "ht"), ("en-AU", "en"), ("jp", "ja"), ("It is Spanish.", "es"), ("This is French", "fr"),
    ("Klingon", ""), ("", ""), ("in to", ""),
])
def test_a_language_is_named_the_way_people_name_it(said, code):
    assert languages.resolve(said) == code


def test_a_little_word_in_a_sentence_is_not_a_language_code():
    """"is", "it", "no", "so" and "to" are Icelandic, Italian, Norwegian, Somali and Tongan — as codes."""
    assert languages.resolve("is") == "is" and languages.resolve("no") == "no"
    for word in ("is", "it", "no", "so", "to", "es"):
        assert languages.resolve(word, codes=False) == "", word
    assert languages.resolve("it is so", codes=False) == "" and languages.resolve("no it is not") == ""
    assert languages.resolve("Norwegian", codes=False) == "no"


def test_the_picker_lists_every_language_once_common_ones_first():
    listed = languages.listing()
    codes = [entry["code"] for entry in listed]
    assert len(codes) == len(set(codes)) > 160 and codes[:3] == ["en", "es", "fr"]
    assert [entry["common"] for entry in listed[:len(languages.COMMON)]] == [True] * len(languages.COMMON)
    assert all(entry["name"] for entry in listed) and "zh" not in codes          # Chinese is offered as its two scripts
    assert languages.name("pt-BR") == "Portuguese (Brazil)" and languages.plain_name("pt-BR") == "Portuguese"
    assert languages.name("xx") == "" and languages.name("en-AU") == "English"


# ── telling a language by looking ──────────────────────────────────────────

@pytest.mark.parametrize("code, text", [
    ("en", "Where is the nearest pharmacy? My daughter has a fever and we need medicine tonight."),
    ("es", "El puente está cerrado por la crecida del río; tome el desvío por el camino viejo hasta el pueblo."),
    ("fr", "Faites bouillir l'eau pendant une minute avant de la boire et gardez-la dans un récipient propre."),
    ("de", "Bitte bewahren Sie die Batterien trocken auf und prüfen Sie einmal im Monat den Ladezustand."),
    ("it", "Il treno per la città parte dal binario quattro e non si ferma nelle stazioni più piccole."),
    ("pt", "Não se esqueça de fechar a porta do celeiro, porque os animais saem durante a noite."),
    ("pl", "Proszę nie wychodzić na lód, ponieważ jest jeszcze zbyt cienki i może się załamać."),
    ("tr", "Bu köprü kapalı olduğu için eski yoldan gitmeniz gerekiyor, çok daha uzun sürecek."),
    ("ru", "Осторожно: лёд на озере ещё тонкий, не выходите на него с детьми."),
    ("uk", "Будь ласка, покажіть мені дорогу до лікарні, бо моїй мамі потрібна допомога."),
    ("ar", "أين أقرب مستشفى؟ نحن بحاجة إلى طبيب في هذه الليلة."),
    ("fa", "لطفاً به من بگویید که ایستگاه قطار کجاست، چون باید پیش از ظهر به شهر برسم."),
    ("he", "איפה בית המרקחת הקרוב? אנחנו צריכים תרופה הלילה."),
    ("hi", "कृपया मुझे बताइए कि अस्पताल कहाँ है, मेरी बेटी को बुखार है।"),
    ("ja", "この電車は十分後に四番線から出発します。"),
    ("zh-Hans", "请在这里写下您的地址和电话号码，我们会尽快联系您。"),
    ("zh-Hant", "請在這裡寫下您的地址和電話號碼，我們會盡快聯絡您。"),
    ("ko", "가장 가까운 약국이 어디에 있습니까? 오늘 밤에 약이 필요합니다."),
    ("th", "ร้านขายยาที่ใกล้ที่สุดอยู่ที่ไหน"),
    ("el", "Πού είναι το πλησιέστερο φαρμακείο; Χρειαζόμαστε φάρμακα απόψε."),
])
def test_a_sentence_says_what_language_it_is(code, text):
    found, sure = detect.detect(text)
    assert found == code and sure >= 0.35, (found, sure)


def test_a_word_or_two_is_not_trusted():
    for text in ("Hola", "ok", "Danke", "12345 !!!", ""):
        assert detect.detect(text)[1] < 0.35, text


# ── the words of the request, and long text ────────────────────────────────

def test_the_request_names_both_languages_and_asks_for_the_translation_only():
    asked = core.text_prompt("  Good morning.  ", "en", "pt-BR")
    assert asked.startswith("You are a professional English (en) to Portuguese (pt-BR) translator.")
    assert "Produce only the Portuguese translation, without any additional explanations or commentary." in asked
    assert asked.endswith("into Portuguese:\n\n\nGood morning.")
    unknown = core.text_prompt("Hola", "", "en")
    assert unknown.startswith("You are a professional translator into English (en).") and unknown.endswith("into English:\n\n\nHola")
    picture = core.image_prompt("es", "en")
    assert "translate the Spanish text in the provided image into English" in picture and "Ignore symbols" in picture
    assert "translate the text in the provided image into English" in core.image_prompt("", "en")


def test_a_long_text_goes_paragraph_by_paragraph_and_keeps_its_shape():
    pieces = core.parts("One.\r\n\r\nTwo line a\nTwo line b\n\n\n\nThree.\n")
    assert pieces == [("One.", "\n\n"), ("Two line a\nTwo line b", "\n\n"), ("Three.", "")]
    long = ("This sentence is one of many in a paragraph with no breaks. " * 60).strip()
    cut = core.parts(long, limit=500)
    assert len(cut) > 5 and all(len(piece) <= 500 for piece, _gap in cut)
    assert all(piece.endswith(".") for piece, _gap in cut)                       # cut at sentence ends
    assert " ".join(piece for piece, _gap in cut) == long and cut[-1][1] == ""
    endless = "word " * 400
    assert all(len(piece) <= 300 for piece, _gap in core.parts(endless, limit=300))
    assert core.parts("") == [] and core.parts("\n\n  \n") == []


# ── translating, with a stand-in for the model ─────────────────────────────

class StandIn:
    """Answers like a translator: the text after the last blank lines, tagged
    with the language asked for. Remembers what it was asked."""

    def __init__(self, who="translator"):
        self.asked = []
        self.who = who
        self.language = "Spanish"            # its answer to "which language is this"
        self.echo_when_source_named = False
        self.fail = None

    async def __call__(self, messages, max_tokens=1200, timeout=180.0, owner=None, needs_sight=False):
        content = messages[0]["content"]
        prompt = content if isinstance(content, str) else next(part["text"] for part in content if part["type"] == "text")
        self.asked.append({"prompt": prompt, "sight": needs_sight, "owner": owner, "picture": not isinstance(content, str)})
        if self.fail:
            raise self.fail
        if prompt.startswith("Which language"):
            return self.language, self.who
        if self.echo_when_source_named and " to " in prompt.split("translator")[0]:
            return prompt, self.who
        if not isinstance(content, str):
            return "DANGER\nDo not drink.", self.who
        target = prompt.split("translation,")[0].rsplit("the ", 1)[1].strip()
        return f"[{target}] {prompt.rsplit(chr(10) * 3, 1)[1]}", self.who


@pytest.fixture
def model(monkeypatch):
    stand_in = StandIn()
    monkeypatch.setattr(engine, "ask", stand_in)
    return stand_in


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_sentence_is_translated_and_says_how_its_language_was_told(model):
    result = run(translate.translate_text("Where is the nearest pharmacy? My daughter has a fever.", "Spanish", owner="zed"))
    assert result["text"] == "[Spanish] Where is the nearest pharmacy? My daughter has a fever."
    assert (result["source"], result["source_name"], result["target"], result["target_name"]) == ("en", "English", "es", "Spanish")
    assert (result["detected"], result["parts"], result["engine"], result["same_language"]) == ("detected", 1, "translator", False)
    assert len(model.asked) == 1 and model.asked[0]["owner"] == "zed"            # looked, did not ask
    assert model.asked[0]["prompt"].startswith("You are a professional English (en) to Spanish (es) translator.")


def test_a_word_or_two_is_asked_about_and_a_given_language_is_taken_as_given(model):
    short = run(translate.translate_text("Hola", "en"))
    assert (short["source"], short["detected"], short["text"]) == ("es", "asked", "[English] Hola")
    assert model.asked[0]["prompt"].startswith("Which language") and "Hola" in model.asked[0]["prompt"]
    model.asked.clear()
    given = run(translate.translate_text("Hola", "English", source="Spanish"))
    assert (given["source"], given["detected"]) == ("es", "") and len(model.asked) == 1
    model.asked.clear()
    model.language = "I am not sure."                                             # the model cannot say either
    unknown = run(translate.translate_text("Zxqv", "fr"))
    assert unknown["source"] == "" and unknown["source_name"] == "" and unknown["text"] == "[French] Zxqv"
    assert model.asked[-1]["prompt"].startswith("You are a professional translator into French (fr).")


def test_a_text_of_several_paragraphs_comes_back_with_its_paragraphs(model):
    seen = []

    async def on_part(number, of, text):
        seen.append((number, of, text))

    result = run(translate.translate_text("First paragraph here.\n\nSecond one,\nwith two lines.\n\nThird.", "de", "en", on_part=on_part))
    assert result["text"] == "[German] First paragraph here.\n\n[German] Second one,\nwith two lines.\n\n[German] Third."
    assert result["parts"] == 3 and [(n, of) for n, of, _text in seen] == [(1, 3), (2, 3), (3, 3)]
    assert seen[0][2] == "[German] First paragraph here.\n\n" and seen[2][2] == "[German] Third."


def test_instructions_handed_back_are_not_a_translation(model):
    model.echo_when_source_named = True
    result = run(translate.translate_text("Hola amigo", "en", source="is"))       # told the wrong language
    assert result["text"] == "[English] Hola amigo" and len(model.asked) == 2    # asked again without one
    assert model.asked[1]["prompt"].startswith("You are a professional translator into English (en).")
    model.echo_when_source_named = False

    async def always_echo(messages, **_):
        return messages[0]["content"], "translator"

    import services.translate.engine as real
    original = real.ask
    real.ask = always_echo
    try:
        with pytest.raises(translate.TranslateError, match="gave no translation"):
            run(translate.translate_text("Hola amigo", "en", source="es"))
    finally:
        real.ask = original


@pytest.mark.parametrize("text, target, source, said", [
    ("", "es", "auto", "nothing to translate"), ("   \n ", "es", "auto", "nothing to translate"),
    ("Hello", "Klingon", "auto", "not a language I can translate into"),
    ("Hello", "es", "Elvish", "not a language I can translate from"),
    ("x" * 20001, "es", "en", "20,001 characters"),
])
def test_what_cannot_be_translated_is_refused_in_plain_words(model, text, target, source, said):
    with pytest.raises(translate.TranslateError, match=said):
        run(translate.translate_text(text, target, source))
    assert model.asked == []


def test_text_already_in_the_language_is_left_alone(model):
    result = run(translate.translate_text("Good evening.", "en", "en"))
    assert result["same_language"] and result["text"] == "Good evening." and result["parts"] == 0 and model.asked == []
    # two ways of writing Chinese are two languages here
    assert not run(translate.translate_text("请写下地址。", "zh-Hant", "zh-Hans"))["same_language"]


def test_a_model_answer_is_tidied():
    assert core._tidy('  "Buenos días."  ') == "Buenos días."
    assert core._tidy("Translation: Buenos días.") == "Buenos días."
    assert core._tidy("<think>hmm</think>Buenos días.") == "Buenos días."
    assert core._tidy('He said "no" and left.') == 'He said "no" and left.'      # quotes inside are the text's own


def test_a_photo_goes_to_the_model_that_can_see(model):
    picture = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    result = run(translate.translate_image(picture, "image/png; charset=binary", "English", owner="zed"))
    assert result["text"] == "DANGER\nDo not drink." and result["target"] == "en" and result["source"] == ""
    asked = model.asked[0]
    assert asked["sight"] and asked["picture"] and "text in the provided image into English" in asked["prompt"]
    for data, mime, said in ((b"x", "application/pdf", "not a picture"), (b"", "image/png", "empty"),
                             (b"0" * (core.MAX_IMAGE_BYTES + 1), "image/jpeg", "over 12 MB")):
        with pytest.raises(translate.TranslateError, match=said):
            run(translate.translate_image(data, mime, "en"))
    model.fail = engine.EngineError("Reading a picture needs the translation model, and it is not answering on this server.")
    with pytest.raises(translate.TranslateError, match="needs the translation model"):
        run(translate.translate_image(picture, "image/png", "en"))


# ── who answers ────────────────────────────────────────────────────────────

class FakeServer:
    """The model server, for ``engine``: answers, or is missing the model, or is away."""

    calls = []
    status = 200
    body = {"choices": [{"message": {"content": "Hola."}}]}
    error = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).calls.append((url, json))
        if type(self).error:
            raise type(self).error

        class Response:
            status_code = type(self).status
            text = __import__("json").dumps(type(self).body)

            @staticmethod
            def json():
                return type(self).body

        return Response()


@pytest.fixture
def server(monkeypatch):
    import httpx

    FakeServer.calls, FakeServer.status, FakeServer.error = [], 200, None
    FakeServer.body = {"choices": [{"message": {"content": "Hola."}}]}
    monkeypatch.setattr(httpx, "AsyncClient", FakeServer)
    monkeypatch.delenv("AEGIS_TRANSLATE_URL", raising=False)
    monkeypatch.setattr(engine, "alias", lambda: "translate")
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda owner=None: [("http://chat", "chat-model", {})])
    stood_in = []

    async def chat_model(candidates, messages, **options):
        stood_in.append(messages)
        return "Hola (from the chat model)."

    monkeypatch.setattr("src.llm_core.llm_call_async_with_fallback", chat_model)
    engine.reset()
    yield type("Server", (), {"fake": FakeServer, "stood_in": stood_in})
    engine.reset()


MESSAGE = [{"role": "user", "content": "Translate: Hello."}]


def test_the_dedicated_model_answers_when_it_is_there(server):
    answer, who = run(engine.ask(MESSAGE, owner="zed"))
    assert (answer, who) == ("Hola.", "translator") and server.stood_in == []
    url, sent = server.fake.calls[0]
    assert url == "http://127.0.0.1:9090/v1/chat/completions"
    assert sent["model"] == "translate" and sent["temperature"] == 0 and sent["messages"] == MESSAGE and sent["stream"] is False


def test_without_the_dedicated_model_the_chat_model_stands_in_and_is_not_asked_twice(server):
    server.fake.status, server.fake.body = 404, {"error": "could not find a model called translate"}
    answer, who = run(engine.ask(MESSAGE))
    assert (answer, who) == ("Hola (from the chat model).", "chat-model") and engine.dedicated_known_missing()
    run(engine.ask(MESSAGE))
    assert len(server.fake.calls) == 1 and len(server.stood_in) == 2             # the missing model is left alone for a while
    engine.reset()
    assert not engine.dedicated_known_missing()


def test_a_model_server_that_is_away_does_not_stop_a_translation(server):
    import httpx

    server.fake.error = httpx.ConnectError("refused")
    assert run(engine.ask(MESSAGE))[1] == "chat-model"
    engine.reset()
    server.fake.error, server.fake.status, server.fake.body = None, 500, {"error": "boom"}
    assert run(engine.ask(MESSAGE))[1] == "chat-model" and not engine.dedicated_known_missing()   # a hiccup, not an absence
    server.fake.status, server.fake.body = 200, {"choices": [{"message": {"content": "   "}}]}
    assert run(engine.ask(MESSAGE))[1] == "chat-model"                                           # an empty answer is no answer


def test_only_the_dedicated_model_is_asked_to_read_a_picture(server):
    server.fake.status, server.fake.body = 404, {"error": "model not found"}
    with pytest.raises(engine.EngineError, match="needs the translation model"):
        run(engine.ask(MESSAGE, needs_sight=True))
    assert server.stood_in == []


def test_the_dedicated_model_can_be_switched_off_or_pointed_elsewhere(server, monkeypatch):
    monkeypatch.setattr(engine, "alias", lambda: "")
    assert run(engine.ask(MESSAGE))[1] == "chat-model" and server.fake.calls == []
    monkeypatch.setattr(engine, "alias", lambda: "my-translator")
    monkeypatch.setenv("AEGIS_TRANSLATE_URL", "http://elsewhere:8000/v1/chat/completions")
    engine.reset()
    run(engine.ask(MESSAGE))
    assert server.fake.calls[0][0] == "http://elsewhere:8000/v1/chat/completions" and server.fake.calls[0][1]["model"] == "my-translator"


def test_no_model_at_all_is_said_plainly(server, monkeypatch):
    server.fake.status, server.fake.body = 404, {"error": "model not found"}
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda owner=None: [])
    with pytest.raises(engine.EngineError, match="No model is set up"):
        run(engine.ask(MESSAGE))


# ── what was translated lately ─────────────────────────────────────────────

@pytest.fixture
def kept(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'translate.db'}")
    history.Translation.__table__.create(bind=db)
    monkeypatch.setattr(history, "SessionLocal", sessionmaker(bind=db))
    return history


def test_a_persons_translations_are_theirs_alone_and_theirs_to_forget(kept):
    first = kept.add("zed", kind="text", source="en", target="es", text="Good morning.", result="Buenos días.")
    kept.add("zed", kind="photo", source="", target="en", text="", result="DANGER")
    kept.add("ada", kind="text", source="en", target="fr", text="Thank you.", result="Merci.")
    mine = kept.recent("zed")
    assert [(item["kind"], item["target_name"], item["result"]) for item in mine] == [("photo", "English", "DANGER"), ("text", "Spanish", "Buenos días.")]
    assert mine[1]["source_name"] == "English" and mine[1]["text"] == "Good morning." and mine[1]["at"] > 1_700_000_000
    assert [item["result"] for item in kept.recent("ada")] == ["Merci."] and kept.recent(None) == []
    assert kept.remove("ada", first["id"]) is False and kept.remove("zed", first["id"]) is True     # not another person's
    assert kept.clear("zed") == 1 and kept.recent("zed") == [] and len(kept.recent("ada")) == 1


def test_only_the_latest_are_kept(kept, monkeypatch):
    monkeypatch.setattr(kept, "KEEP", 3)
    for number in range(5):
        kept.add("zed", kind="text", source="en", target="es", text=f"line {number}", result=f"línea {number}")
    assert len(kept.recent("zed", 50)) == 3
    assert len(kept.add("zed", kind="text", source="en", target="es", text="x" * 9000, result="y")["text"]) == kept.MAX_TEXT


# ── the page's routes ──────────────────────────────────────────────────────

@pytest.fixture
def client(model, kept, monkeypatch):
    import routes.translate_routes as routes

    who = {"user": "zed"}

    def fake_user(request):
        if not who["user"]:
            raise HTTPException(401, "Sign in")
        return who["user"]

    monkeypatch.setattr(routes, "require_user", fake_user)
    monkeypatch.setattr(engine, "dedicated_known_missing", lambda: False)
    app = FastAPI()
    app.include_router(routes.setup_translate_routes())
    test_client = TestClient(app)
    test_client.who = who
    return test_client


def test_translating_through_the_api_and_keeping_or_not_keeping_it(client, kept):
    listed = client.get("/api/translate/languages").json()
    assert listed["languages"][0] == {"code": "en", "name": "English", "native": "English", "common": True}
    assert listed["max_chars"] == 20000 and listed["dedicated"] is True
    done = client.post("/api/translate", json={"text": "The water is safe after boiling.", "to": "Spanish"}).json()
    assert done["text"] == "[Spanish] The water is safe after boiling." and done["source"] == "en" and done["id"]
    quiet = client.post("/api/translate", json={"text": "Do not keep this one, please.", "to": "fr", "from": "en", "keep": False}).json()
    assert quiet["text"].startswith("[French]") and "id" not in quiet
    same = client.post("/api/translate", json={"text": "Good evening.", "to": "en", "from": "en"}).json()
    assert same["same_language"] and "id" not in same                              # nothing was translated: nothing to keep
    assert [item["result"] for item in client.get("/api/translate/history").json()["items"]] == ["[Spanish] The water is safe after boiling."]
    refused = client.post("/api/translate", json={"text": "Hello", "to": "Klingon"})
    assert refused.status_code == 400 and "not a language" in refused.json()["detail"]
    assert client.post("/api/translate", json={"text": "", "to": "es"}).status_code == 400
    assert client.delete(f"/api/translate/history/{done['id']}").json() == {"ok": True}
    assert client.delete(f"/api/translate/history/{done['id']}").status_code == 404
    assert client.delete("/api/translate/history").json() == {"removed": 0}


def test_a_long_text_arrives_a_paragraph_at_a_time(client):
    response = client.post("/api/translate/stream", json={"text": "One here.\n\nTwo here.", "to": "de", "from": "en"})
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert lines[0] == {"part": 1, "of": 2, "text": "[German] One here.\n\n"} and lines[1]["part"] == 2
    assert lines[2]["done"] is True and lines[2]["text"] == "[German] One here.\n\n[German] Two here." and lines[2]["id"]
    wrong = client.post("/api/translate/stream", json={"text": "Hello", "to": "Klingon"})
    assert [json.loads(line) for line in wrong.text.splitlines() if line.strip()][-1]["error"].startswith("“Klingon” is not a language")


def test_a_photo_through_the_api(client):
    picture = ("sign.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")
    done = client.post("/api/translate/photo", files={"file": picture}, data={"to": "English", "from": "auto"}).json()
    assert done["text"] == "DANGER\nDo not drink." and done["id"]
    kept_item = client.get("/api/translate/history").json()["items"][0]
    assert (kept_item["kind"], kept_item["text"], kept_item["result"]) == ("photo", "", "DANGER\nDo not drink.")   # the words, not the picture
    refused = client.post("/api/translate/photo", files={"file": ("notes.txt", b"hello", "text/plain")}, data={"to": "en"})
    assert refused.status_code == 400 and "not a picture" in refused.json()["detail"]


def test_the_routes_need_a_signed_in_person(client):
    client.who["user"] = ""
    for method, path in (("get", "/api/translate/languages"), ("get", "/api/translate/history"), ("delete", "/api/translate/history"),
                         ("delete", "/api/translate/history/abc")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/translate", json={"text": "Hello", "to": "es"}).status_code == 401
    assert client.post("/api/translate/stream", json={"text": "Hello", "to": "es"}).status_code == 401
    assert client.post("/api/translate/photo", files={"file": ("a.png", b"x", "image/png")}, data={"to": "es"}).status_code == 401


# ── the assistant's tool ───────────────────────────────────────────────────

def test_the_translate_tool_gives_the_translation_and_says_which_languages(model):
    from src.agent_tools import TOOL_HANDLERS

    def tool(args):
        return asyncio.run(TOOL_HANDLERS["translate"](json.dumps(args) if isinstance(args, dict) else args, {"owner": "zed"}))

    done = tool({"text": "Where is the nearest pharmacy? My daughter has a fever.", "to": "Spanish"})
    card = done.pop("card")                                  # the same answer as a card, for a screen that draws them
    assert done == {"output": "English (language detected) → Spanish:\n[Spanish] Where is the nearest pharmacy? My daughter has a fever.",
                    "exit_code": 0}
    assert card["type"] == "translation" and (card["from"], card["from_code"], card["to"]) == ("English", "en", "Spanish")
    assert card["source"] == "Where is the nearest pharmacy? My daughter has a fever." and card["text"].startswith("[Spanish] Where is")
    assert tool({"text": "Guten Morgen.", "to": "en", "from": "German"})["output"].startswith("German → English:\n")
    assert "already in English" in tool({"text": "Good evening.", "to": "en", "from": "en"})["output"]
    for bad in ({"text": "", "to": "es"}, {"text": "Hello"}, "translate this please", ""):
        assert tool(bad)["exit_code"] == 1
    assert "not a language" in tool({"text": "Hello", "to": "Klingon"})["error"]


def test_the_translator_is_wired_in_everywhere_it_must_be():
    from src import tool_policy, tool_security
    from src.agent_loop import _CHANGE_TOOLS
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_index import ToolIndex
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert "translate" in TOOL_HANDLERS and "translate" in TOOL_TAGS and "translate" not in _CHANGE_TOOLS
    schema = {entry["function"]["name"]: entry["function"] for entry in FUNCTION_TOOL_SCHEMAS}["translate"]
    assert schema["parameters"]["required"] == ["text", "to"] and set(schema["parameters"]["properties"]) == {"text", "to", "from"}
    assert "translate" in tool_security.PLAN_MODE_READONLY_TOOLS and "translate" not in tool_security.NON_ADMIN_BLOCKED_TOOLS
    assert '"translate"' in open(tool_policy.__file__, encoding="utf-8").read()
    index = ToolIndex.__new__(ToolIndex)
    index.retrieve = lambda query, k=8: []
    for said in ("translate this into French", "how do you say thank you in Japanese", "what does this say"):
        assert "translate" in index.get_tools_for_query(said, always_include=set()), said
    assert "translate" not in index.get_tools_for_query("what is the weather like", always_include=set())

    served = (REPO / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/translate")' in served and "setup_translate_routes()" in served
    for name in ("static/translate.html", "static/lite/translate.js", "static/lite/translate.css"):
        assert (REPO / name).is_file(), name
    assert 'id="tool-translate-link" href="/translate"' in (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "location.href = '/translate'" in (REPO / "static/lite/home.js").read_text(encoding="utf-8")
    assert "\n  translate: {" in (REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    icons = (REPO / "static/lite/lite.js").read_text(encoding="utf-8")
    assert all(f"  {name}: '" in icons or f", {name}: '" in icons for name in ("mic", "swap", "stop", "globe"))
