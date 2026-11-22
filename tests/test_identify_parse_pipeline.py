"""Photo identification: parsing, candidate handling, reference notes and the
pipeline's branches. The classifier, the vision model and the library are
stubbed — what is under test is how their outputs are combined.
"""
import io

import pytest

from services import identify
from services.identify import classifier, reference, vision


def _classified(top_score=0.8, genus_score=0.98, kingdom="Fungi", cls="Agaricomycetes"):
    return {
        "model": "bioclip-test",
        "species": [
            {"kingdom": kingdom, "class": cls, "family": "Amanitaceae", "genus": "Amanita",
             "species": "Amanita phalloides", "common_name": "Death cap", "score": top_score},
            {"kingdom": kingdom, "class": cls, "genus": "Amanita", "species": "Amanita subjunquillea", "score": 0.04},
            {"kingdom": kingdom, "class": cls, "genus": "Amanita", "species": "Amanita virosa", "score": 0.02},
        ],
        "genus": [{"genus": "Amanita", "family": "Amanitaceae", "score": genus_score}],
        "family": [{"family": "Amanitaceae", "score": 0.98}],
    }


@pytest.fixture
def photo(tmp_path):
    from PIL import Image

    path = tmp_path / "photo.png"
    Image.new("RGB", (3000, 2000), (40, 120, 60)).save(path)
    return str(path)


# ── parsing the vision model's reply ───────────────────────────────────────

def test_parse_analysis_handles_fences_reasoning_and_prose():
    reply = (
        "<think>It might be a mushroom {not json}</think>Here you go:\n```json\n"
        '{"category": "Mushroom", "subject": "greenish cap with volva", "features": ["white gills", "", 7, "sac-like volva"],'
        ' "best_guess": {"common_name": "Death cap", "scientific_name": "Amanita  phalloides", "rank": "Species", "confidence": "HIGH"},'
        ' "alternatives": [{"common_name": "", "scientific_name": "Amanita virosa"}, {"common_name": "", "scientific_name": ""}, "junk"],'
        ' "text_in_image": "", "need_more": ["underside"]}\n```\nHope that helps.'
    )
    out = identify.parse_analysis(reply)
    assert out["parsed"] and out["category"] == "mushroom"
    assert out["features"] == ["white gills", "sac-like volva"]
    assert out["best_guess"] == {"common_name": "Death cap", "scientific_name": "Amanita phalloides",
                                 "rank": "species", "confidence": "high"}
    assert out["alternatives"] == [{"common_name": "", "scientific_name": "Amanita virosa"}]
    assert out["need_more"] == ["underside"]


@pytest.mark.parametrize("reply", ["", "I cannot tell what this is.", '{"category": "mushroom", "subject": "cut off', "[1, 2]"])
def test_parse_analysis_never_fails(reply):
    out = identify.parse_analysis(reply)
    assert out["category"] == "unclear" and out["parsed"] is False
    assert out["best_guess"]["confidence"] == "low" and out["alternatives"] == []


def test_unknown_category_and_confidence_fall_back():
    out = identify.parse_analysis('{"category": "alien", "best_guess": {"confidence": "certain"}}')
    assert out["category"] == "unclear" and out["best_guess"]["confidence"] == "low"


# ── candidates ─────────────────────────────────────────────────────────────

def test_lookup_names_orders_and_cleans():
    analysis = identify.parse_analysis(
        '{"category": "mushroom", "best_guess": {"scientific_name": "Cortinarius sp."},'
        ' "alternatives": [{"scientific_name": "Galerina marginata"}, {"common_name": "Sulphur tuft"}]}')
    weak = _classified(top_score=0.04, genus_score=0.31)
    assert identify.lookup_names(analysis, weak) == ["Cortinarius", "Galerina marginata", "Sulphur tuft"]
    strong = _classified(top_score=0.8)
    names = identify.lookup_names(analysis, strong)
    assert names[:2] == ["Cortinarius", "Amanita phalloides"] and len(names) <= 4
    assert identify.lookup_names(identify.parse_analysis(""), None) == []


def test_fast_path_only_when_the_classifier_is_confident():
    assert identify.analysis_from_classifier(None) is None
    assert identify.analysis_from_classifier(_classified(top_score=0.3, genus_score=0.6)) is None
    sure = identify.analysis_from_classifier(_classified(top_score=0.8))
    assert sure["category"] == "mushroom" and sure["from_classifier"]
    assert sure["best_guess"] == {"common_name": "Death cap", "scientific_name": "Amanita phalloides",
                                  "rank": "species", "confidence": "high"}
    assert [a["scientific_name"] for a in sure["alternatives"]] == ["Amanita subjunquillea", "Amanita virosa"]
    genus_only = identify.analysis_from_classifier(_classified(top_score=0.2, genus_score=0.95))
    assert genus_only["best_guess"]["rank"] == "genus" and genus_only["best_guess"]["scientific_name"] == "Amanita"
    assert genus_only["best_guess"]["common_name"] == ""
    spider = identify.analysis_from_classifier(_classified(top_score=0.7, kingdom="Animalia", cls="Arachnida"))
    assert spider["category"] == "spider"
    assert identify.analysis_from_classifier(_classified(top_score=0.9, kingdom="Bacteria")) is None


def test_classifier_summary_and_strength():
    text = classifier.summarize(_classified(top_score=0.77), k=2)
    assert text.splitlines()[0] == "Species: Amanita phalloides (Death cap) 0.77; Amanita subjunquillea 0.04"
    assert "Genus: Amanita 0.98" in text and "Family: Amanitaceae 0.98" in text
    assert classifier.summarize(None) == ""
    assert [classifier.strength(s) for s in (0.7, 0.3, 0.1, 0.01)] == ["strong", "moderate", "weak", "very weak"]


def test_classifier_absent_is_not_an_error(monkeypatch):
    monkeypatch.setattr(classifier, "_settings", lambda: {"species_id_enabled": False})
    assert classifier.classify(b"jpeg") is None

    monkeypatch.setattr(classifier, "_settings", lambda: {})

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(classifier.httpx, "Client", _Client)
    assert classifier.classify(b"jpeg") is None


# ── reference notes ────────────────────────────────────────────────────────

_ARTICLE = """<html><head><title>Amanita phalloides</title></head><body><div id="mw-content-text">
<table class="infobox"><tr><td><img src="./_assets_/abc/Gills_icon.png"></td></tr>
<tr><td><img src="./_assets_/abc/Amanita_phalloides_1.JPG"></td></tr>
<tr><td><img src="./_assets_/abc/White_spore_print_icon.png"></td><td>Spore print is white</td></tr>
<tr><td><img src="./_assets_/abc/Deadly_icon.png"></td><td>Edibility is deadly</td></tr></table>
<p>Amanita phalloides, commonly known as the death cap, is a deadly poisonous basidiomycete fungus, one of many in the
genus Amanita. Widely distributed across Europe, it forms ectomycorrhizas with various broadleaved trees.</p>
<h2>Taxonomy</h2><p>First described by Vaillant in 1727.</p>
<h2>Similarity to edible species</h2><p>It resembles the paddy straw mushroom and Caesar's mushroom.</p>
<h2>Toxicity</h2><p>Half a cap contains enough amatoxin to kill an adult human.</p>
<h2>References</h2><p>Many.</p></div></body></html>"""


def test_notes_pick_field_marks_lookalikes_and_toxicity():
    info = reference.notes_from_article(_ARTICLE, budget=1500)
    assert info["title"] == "Amanita phalloides"
    notes = info["notes"]
    assert notes.startswith("Field marks: ") and "Edibility is deadly" in notes and "Spore print is white" in notes
    assert "commonly known as the death cap" in notes
    assert "Similarity to edible species: It resembles the paddy straw mushroom" in notes
    assert "Toxicity: Half a cap" in notes
    assert "Vaillant" not in notes and "Many." not in notes
    assert info["image"].endswith("Amanita_phalloides_1.JPG")          # the icon was skipped


def test_lookup_falls_back_to_the_genus(monkeypatch):
    from services.library import kiwix

    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: [
        {"id": "wikipedia_en_medicine_maxi", "articles": 70_000},
        {"id": "wikipedia_en_all_maxi", "articles": 7_000_000}])
    monkeypatch.setattr(kiwix, "public_url", lambda path: "https://lib" + path)
    monkeypatch.setattr(kiwix, "suggest", lambda book, term, count=5: [])
    fetched = []

    def fake_fetch(url, max_bytes=0):
        fetched.append(url)
        return ("text/html", _ARTICLE.encode()) if url.endswith("/Amanita") else None

    monkeypatch.setattr(kiwix, "fetch", fake_fetch)
    found = reference.lookup("Amanita arocheae")
    assert fetched == ["https://lib/content/wikipedia_en_all_maxi/Amanita_arocheae",
                       "https://lib/content/wikipedia_en_all_maxi/Amanita"]
    assert found["is_genus_fallback"] and found["matched"] == "Amanita"
    assert found["image_path"] == "/content/wikipedia_en_all_maxi/_assets_/abc/Amanita_phalloides_1.JPG"

    monkeypatch.setattr(kiwix, "available", lambda force=False: False)
    assert reference.lookup("Amanita phalloides") is None


# ── vision helper ──────────────────────────────────────────────────────────

def test_prepare_image_caps_the_long_edge(photo):
    from PIL import Image

    data = vision.prepare_image(photo)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG" and max(image.size) == vision.MAX_SIDE


def test_identify_model_is_tried_before_the_general_vision_model(monkeypatch):
    import src.ai_interaction as ai
    import src.document_processor as dp

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"identify_model": "fast-vl", "vision_model": "slow-vl"})
    monkeypatch.setattr(ai, "_resolve_model", lambda spec, owner=None: (f"http://127.0.0.1:9090/v1/chat/completions", spec, {}))
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda configured, owner=None: ("http://127.0.0.1:9090/v1/chat/completions", configured, {}))
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_vision_fallback_candidates", lambda owner=None: [])
    assert [model for _url, model, _headers in vision._candidates(None)] == ["fast-vl", "slow-vl"]

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": False})
    with pytest.raises(vision.VisionUnavailable):
        vision._candidates(None)


def test_local_call_switches_thinking_off_and_asks_for_json(monkeypatch):
    sent = []

    class _Resp:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload

        def raise_for_status(self):
            assert self.status_code == 200

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None):
            sent.append(dict(json))                    # the caller reuses and edits its payload
            if "response_format" in json:
                return _Resp(400, {})                      # an engine without JSON mode
            return _Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(vision.httpx, "Client", _Client)
    text = vision._local_call("http://x/v1/chat/completions", "m", [{"role": "user", "content": "hi"}],
                              {"Authorization": "Bearer k"}, 50, 0.2, 30, False, True)
    assert text == '{"ok": true}'
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False} and sent[0]["think"] is False
    assert sent[0]["response_format"] == {"type": "json_object"} and "response_format" not in sent[1]


# ── pipeline ───────────────────────────────────────────────────────────────

def _wire(monkeypatch, classified, replies, lookups=None):
    calls = []

    def fake_vision(image, prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return {"text": replies[len(calls) - 1], "model": "test-vl"}

    monkeypatch.setattr(vision, "warm_up", lambda owner=None: None)
    monkeypatch.setattr(vision, "vision_call", fake_vision)
    monkeypatch.setattr(classifier, "classify", lambda image, k=5, timeout=0: classified)
    monkeypatch.setattr(reference, "lookup", lambda name, budget=1900: (lookups or {}).get(name))
    return calls


def _ref(title, image=True):
    return {"query": title, "matched": title, "is_genus_fallback": False, "title": title,
            "url": f"https://lib/content/w/{title.replace(' ', '_')}", "notes": f"Field marks: notes about {title}",
            "image_path": f"/content/w/_assets_/{title[:3]}.jpg" if image else ""}


def test_confident_classifier_skips_the_look_pass(monkeypatch, photo):
    calls = _wire(monkeypatch, _classified(top_score=0.8), ["**Most likely:** Death cap"],
                  {"Amanita phalloides": _ref("Amanita phalloides"), "Amanita": _ref("Amanita", image=False)})
    out = identify._identify_sync(photo, "under oaks", None)
    assert len(calls) == 1                                           # write-up only
    prompt = calls[0]["prompt"]
    assert "[1] Amanita phalloides" in prompt and "Field marks: notes about Amanita phalloides" in prompt
    assert "Amanita phalloides (Death cap) 0.80" in prompt
    assert 'The person who took the photo adds: "under oaks"' in prompt
    assert out["category"] == "mushroom" and out["best_guess"]["scientific_name"] == "Amanita phalloides"
    assert out["sources"][0] == {"url": "https://lib/content/w/Amanita_phalloides", "title": "Amanita phalloides"}
    assert out["images"] == [{"title": "Amanita phalloides", "url": "/api/reference/asset?p=%2Fcontent%2Fw%2F_assets_%2FAma.jpg"}]
    assert out["markdown"].startswith("**Most likely:** Death cap")
    assert "![Amanita phalloides — reference photo](/api/reference/asset?p=" in out["markdown"]
    assert "Species classifier scores (0–1): Amanita phalloides 0.80" in out["markdown"]
    assert "look_s" not in out["timings"]


def test_unsure_classifier_gets_a_look_pass_and_candidates_as_hints(monkeypatch, photo):
    look = ('{"category": "mushroom", "subject": "small brown mushrooms", "features": ["brown caps"],'
            ' "best_guess": {"common_name": "webcap", "scientific_name": "Cortinarius", "rank": "genus", "confidence": "medium"},'
            ' "alternatives": [{"common_name": "funeral bell", "scientific_name": "Galerina marginata"}]}')
    calls = _wire(monkeypatch, _classified(top_score=0.04, genus_score=0.31), [look, "<think>hm</think>**Most likely:** Webcap"],
                  {"Cortinarius": _ref("Cortinarius"), "Galerina marginata": _ref("Galerina marginata")})
    out = identify._identify_sync(photo, "", None)
    assert len(calls) == 2 and calls[0]["json_mode"] is True
    assert "A species classifier trained on the whole tree of life" in calls[0]["prompt"]
    assert "Could also be: funeral bell / Galerina marginata" in calls[1]["prompt"]
    assert [s["title"] for s in out["sources"]] == ["Cortinarius", "Galerina marginata"]
    assert out["markdown"].startswith("**Most likely:** Webcap")      # reasoning stripped
    assert out["best_guess"]["rank"] == "genus"


def test_text_photo_is_transcribed_without_species_noise(monkeypatch, photo):
    noise = _classified(top_score=0.02, genus_score=0.02)
    calls = _wire(monkeypatch, noise, ['{"category": "text", "subject": "a label"}', "LOT 22B\n\nSummary: a label."])
    out = identify._identify_sync(photo, "", None)
    assert out["category"] == "text" and len(calls) == 2
    assert calls[1]["prompt"].startswith("Transcribe all legible text")
    assert out["markdown"] == "LOT 22B\n\nSummary: a label."          # no classifier line, no images
    assert out["sources"] == [] and out["images"] == []


def test_works_without_classifier_or_library(monkeypatch, photo):
    look = '{"category": "plant", "subject": "three-leaved vine", "best_guess": {"scientific_name": "Toxicodendron radicans"}}'
    calls = _wire(monkeypatch, None, [look, "**Most likely:** Poison ivy"])
    out = identify._identify_sync(photo, "", None)
    assert "species classifier" not in calls[0]["prompt"].lower()
    assert "the offline library has no article for these candidates" in calls[1]["prompt"]
    assert out["classifier"] is None and "classifier scores" not in out["markdown"]


def test_object_photo_uses_the_plain_description_prompt(monkeypatch, photo):
    calls = _wire(monkeypatch, None, ['{"category": "object", "subject": "a carburetor"}', "**What it is:** a carburetor"])
    out = identify._identify_sync(photo, "", None)
    assert calls[1]["prompt"].startswith("Describe and identify the subject")
    assert out["markdown"] == "**What it is:** a carburetor"


def test_failures_become_identify_errors(monkeypatch, photo, tmp_path):
    def no_vision(*a, **k):
        raise vision.VisionUnavailable("No vision model is configured (Settings → Vision).")

    monkeypatch.setattr(vision, "warm_up", lambda owner=None: None)
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: None)
    monkeypatch.setattr(vision, "vision_call", no_vision)
    with pytest.raises(identify.IdentifyError) as err:
        identify._identify_sync(photo, "", None)
    assert err.value.status == 503 and "vision model" in str(err.value).lower()

    bad = tmp_path / "notes.txt"
    bad.write_text("not an image")
    with pytest.raises(identify.IdentifyError) as err:
        identify._identify_sync(str(bad), "", None)
    assert err.value.status == 400
