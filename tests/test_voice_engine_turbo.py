"""The voice-cloning engine can speak with a faster model, and falls back when it cannot.

The engine script (``scripts/engine-templates/chatterbox-server.py``, installed as the engine's
``server.py``) speaks recorded voices with Chatterbox. It can now use Chatterbox Turbo as well, and on a
processor it runs Turbo's token loop a row at a time in half precision, which is what makes a recorded
voice quicker to make than to say.

What is checked here:

  which model
    - "auto" is the faster model only when its files are already on the machine; nothing is fetched to decide
    - a model asked for by name is the model used
    - if the faster model will not load, the standard one speaks instead
  a voice
    - each model keeps what it prepared from a recording in a file of its own, beside the recording
    - a recording that is re-recorded is prepared again
    - a recording too short for the faster model is made long enough, and the copy is thrown away
    - deleting a voice in the app removes what every engine prepared from it
  how loud
    - every piece is brought to the same loudness (the faster model speaks half as loud as the standard one)
    - never turned up until it clips, silence stays silence, and it can be switched off
  the speed-up (run only where PyTorch is installed: the engine's own environment, not the app's)
    - one row gives the same answer as the exact arithmetic, to within half precision
    - more than one row is computed exactly as before
    - a value too big for half precision is computed exactly instead of turning into noise
    - the change is kept only where it measures quicker and agrees

Every name and voice in here is made up.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import types
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "scripts" / "engine-templates" / "chatterbox-server.py"
SOURCE = TEMPLATE.read_text(encoding="utf-8")


def _wav(seconds: float, rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(b"\x10\x00" * int(rate * seconds))
    return out.getvalue()


@pytest.fixture
def engine(tmp_path):
    """The engine script, loaded as a module, with a voices folder of its own."""
    before = os.environ.get("TQDM_DISABLE")
    spec = importlib.util.spec_from_file_location("chatterbox_server_under_test", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    voices = tmp_path / "voices"
    voices.mkdir()
    module._args = argparse.Namespace(voices_dir=str(voices), model="auto", exact=False, loudness=-22.0)
    module.voices = voices
    yield module
    if before is None:
        os.environ.pop("TQDM_DISABLE", None)
    else:
        os.environ["TQDM_DISABLE"] = before


# ── which model ────────────────────────────────────────────────────────────

def test_auto_is_the_faster_model_only_when_it_is_already_here(engine):
    assert engine.which_model("auto", True) == "turbo"
    assert engine.which_model("auto", False) == "standard"
    assert engine.which_model("", True) == "turbo" and engine.which_model(None, False) == "standard"


def test_a_model_asked_for_by_name_is_the_model_used(engine):
    assert engine.which_model("standard", True) == "standard"
    assert engine.which_model("turbo", False) == "turbo"            # asked for by name: it is fetched on first use
    assert engine.which_model("Turbo", False) == "turbo"


def test_nothing_is_fetched_to_decide(engine, tmp_path, monkeypatch):
    asked = []
    folder = tmp_path / "snapshot"
    folder.mkdir()

    def snapshot_download(**kwargs):
        asked.append(kwargs)
        return str(folder)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    assert engine.turbo_files() is None                              # the folder is there, the model's files are not
    for name in engine.TURBO_FILES[:-1]:
        (folder / name).write_bytes(b"x")
    assert engine.turbo_files() is None                              # one is still missing
    (folder / engine.TURBO_FILES[-1]).write_bytes(b"x")
    assert engine.turbo_files() == folder
    assert asked and all(call["local_files_only"] is True and call["repo_id"] == "ResembleAI/chatterbox-turbo" for call in asked)

    def not_here(**kwargs):
        raise RuntimeError("not in the cache")

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=not_here))
    assert engine.turbo_files() is None


class _Prepared:
    def __init__(self, source="made"):
        self.source = source

    def save(self, path):
        Path(path).write_bytes(b"prepared")

    def to(self, device):
        return self

    @classmethod
    def load(cls, path, map_location=None):
        return cls("loaded")


class _Model:
    device = "cpu"
    sr = 24000

    def __init__(self):
        self.conds = None
        self.prepared = []

    def prepare_conditionals(self, path, **how):
        self.prepared.append((Path(path).name, how))
        self.conds = _Prepared()


def _stand_ins(monkeypatch, cuda=False):
    """Stand-ins for the packages the engine imports when it loads a model (none are in the app's environment)."""
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: cuda)))
    made = []

    class ChatterboxTTS:
        @classmethod
        def from_pretrained(cls, device="cpu"):
            made.append(("standard", device))
            return _Model()

    monkeypatch.setitem(sys.modules, "chatterbox", types.ModuleType("chatterbox"))
    monkeypatch.setitem(sys.modules, "chatterbox.tts", types.SimpleNamespace(ChatterboxTTS=ChatterboxTTS, Conditionals=_Prepared))
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", types.SimpleNamespace(Conditionals=_Prepared))
    return made


def test_the_faster_model_is_loaded_when_it_is_here(engine, monkeypatch, tmp_path):
    made = _stand_ins(monkeypatch)
    monkeypatch.setattr(engine, "turbo_files", lambda: tmp_path)
    monkeypatch.setattr(engine, "_load_turbo", lambda device: made.append(("turbo", device)) or _Model())
    engine._get_model()
    assert made == [("turbo", "cpu")] and engine._model_kind == "turbo"
    engine._get_model()
    assert made == [("turbo", "cpu")]                                 # loaded once


def test_the_standard_model_speaks_when_the_faster_one_is_not_here(engine, monkeypatch):
    made = _stand_ins(monkeypatch)
    monkeypatch.setattr(engine, "turbo_files", lambda: None)
    engine._get_model()
    assert made == [("standard", "cpu")] and engine._model_kind == "standard"


def test_the_standard_model_speaks_if_the_faster_one_will_not_load(engine, monkeypatch, tmp_path):
    made = _stand_ins(monkeypatch)
    monkeypatch.setattr(engine, "turbo_files", lambda: tmp_path)

    def broken(device):
        raise RuntimeError("a file is damaged")

    monkeypatch.setattr(engine, "_load_turbo", broken)
    engine._get_model()
    assert made == [("standard", "cpu")] and engine._model_kind == "standard"


def test_a_model_chosen_by_name_wins_over_what_is_on_the_machine(engine, monkeypatch, tmp_path):
    made = _stand_ins(monkeypatch)
    monkeypatch.setattr(engine, "turbo_files", lambda: tmp_path)
    engine._args.model = "standard"
    engine._get_model()
    assert made == [("standard", "cpu")] and engine._model_kind == "standard"


# ── a voice ────────────────────────────────────────────────────────────────

def test_what_an_engine_prepared_is_not_a_voice(engine):
    (engine.voices / "robin.wav").write_bytes(_wav(6))
    (engine.voices / "ada.mp3").write_bytes(b"not really an mp3")
    for suffix in (".voice.pt", ".turbo.pt", ".qwen3.pt"):
        (engine.voices / f"robin.wav{suffix}").write_bytes(b"prepared")
    (engine.voices / "voices.json").write_text("{}")
    assert sorted(engine.list_voices()) == ["ada", "robin"]


def test_each_model_keeps_what_it_prepared_in_a_file_of_its_own(engine, monkeypatch):
    _stand_ins(monkeypatch)
    sample = engine.voices / "robin.wav"
    sample.write_bytes(_wav(6))
    assert engine.BAKED_SUFFIX["standard"] != engine.BAKED_SUFFIX["turbo"]

    engine._model_kind = "standard"
    standard = _Model()
    engine._prepare_voice(standard, "robin", sample)
    assert standard.prepared == [("robin.wav", {"exaggeration": 0.5})]
    assert (engine.voices / "robin.wav.voice.pt").exists() and not (engine.voices / "robin.wav.turbo.pt").exists()

    engine._model_kind, engine._active_voice = "turbo", None
    turbo = _Model()
    engine._prepare_voice(turbo, "robin", sample)
    assert turbo.prepared == [("robin.wav", {})]                      # the faster model has no "exaggeration" to set
    assert (engine.voices / "robin.wav.turbo.pt").exists()


def test_a_prepared_voice_is_used_again_and_a_new_recording_is_prepared_again(engine, monkeypatch):
    _stand_ins(monkeypatch)
    sample = engine.voices / "robin.wav"
    sample.write_bytes(_wav(6))
    engine._model_kind = "turbo"
    first = _Model()
    engine._prepare_voice(first, "robin", sample)
    assert len(first.prepared) == 1

    engine._active_voice = None                                      # as after the engine has been restarted
    again = _Model()
    engine._prepare_voice(again, "robin", sample)
    assert again.prepared == [] and again.conds.source == "loaded"

    baked = engine.voices / "robin.wav.turbo.pt"
    older = baked.stat().st_mtime - 60
    os.utime(baked, (older, older))                                  # the recording is now newer than what was prepared
    engine._active_voice = None
    renewed = _Model()
    engine._prepare_voice(renewed, "robin", sample)
    assert len(renewed.prepared) == 1


def test_a_recording_too_short_for_the_faster_model_is_made_long_enough(engine, monkeypatch):
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("numpy")
    short = engine.voices / "pip.wav"
    short.write_bytes(_wav(3.0))
    longer = engine._long_enough_for_turbo(short)
    try:
        assert longer != str(short)
        info = sf.info(longer)
        assert info.frames / info.samplerate >= engine.MIN_TURBO_SAMPLE_SECONDS
    finally:
        os.unlink(longer)
    enough = engine.voices / "robin.wav"
    enough.write_bytes(_wav(6.0))
    assert engine._long_enough_for_turbo(enough) == str(enough)
    not_sound = engine.voices / "odd.wav"
    not_sound.write_bytes(b"this is not sound")
    assert engine._long_enough_for_turbo(not_sound) == str(not_sound)       # left for the model to refuse


def test_the_longer_copy_of_a_short_recording_is_thrown_away(engine, monkeypatch):
    pytest.importorskip("soundfile")
    _stand_ins(monkeypatch)
    short = engine.voices / "pip.wav"
    short.write_bytes(_wav(3.0))
    engine._model_kind = "turbo"
    seen = []

    class Looking(_Model):
        def prepare_conditionals(self, path, **how):
            seen.append((path, Path(path).exists()))
            super().prepare_conditionals(path, **how)

    engine._prepare_voice(Looking(), "pip", short)
    assert seen and seen[0][0] != str(short) and seen[0][1] is True
    assert not Path(seen[0][0]).exists()
    assert (engine.voices / "pip.wav.turbo.pt").exists()


def test_the_app_removes_what_every_engine_prepared_from_a_voice(engine):
    import routes.tts_routes as tts_routes
    for suffix in engine.BAKED_SUFFIX.values():
        assert suffix in tts_routes._BAKED_SUFFIXES
    other_engine = (REPO / "scripts" / "engine-templates" / "qwen3tts-server.py").read_text(encoding="utf-8")
    assert '".qwen3.pt"' in other_engine and ".qwen3.pt" in tts_routes._BAKED_SUFFIXES


def test_the_engine_says_which_model_it_speaks_with(engine):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    said = TestClient(engine.build_app()).get("/health").json()
    assert said["ok"] is True and said["model"] is None and said["row_at_a_time_layers"] == 0      # nothing loaded yet
    engine._model_kind = "turbo"
    assert TestClient(engine.build_app()).get("/health").json()["model"] == "turbo"


def test_the_script_takes_the_model_and_the_exact_switch_and_keeps_the_log_quiet():
    assert 'choices=("auto", "standard", "turbo"), default="auto"' in SOURCE
    assert '"--exact"' in SOURCE
    assert 'os.environ.setdefault("TQDM_DISABLE", "1")' in SOURCE
    # the model is never fetched just because the script was started with its defaults
    assert "local_files_only=True" in SOURCE


# ── how loud ───────────────────────────────────────────────────────────────

def _speechlike(np, seconds=3.0, rate=24000, size=0.05, seed=1):
    """Bursts of noise with pauses between them, about as loud as ``size`` (a stand-in for speech)."""
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    x = rng.standard_normal(n) * size
    t = np.arange(n) / rate
    x *= (np.sin(2 * np.pi * 1.5 * t) > -0.3)                       # three bursts every two seconds
    return x.astype(np.float32)


def test_every_piece_is_brought_to_the_same_loudness(engine):
    np = pytest.importorskip("numpy")
    quiet, loud = _speechlike(np, size=0.03), _speechlike(np, size=0.25, seed=2)
    before = engine.loudness_of(quiet, 24000), engine.loudness_of(loud, 24000)
    assert before[1] - before[0] > 10                                # one is much louder than the other
    made = [engine.level(clip, 24000, -22.0) for clip in (quiet, loud)]
    after = [engine.loudness_of(clip, 24000) for clip, _ in made]
    assert all(abs(value - -22.0) < 0.5 for value in after)           # and now they are alike
    assert made[0][1] > 0 > made[1][1]                               # the quiet one turned up, the loud one down
    assert all(clip.dtype == np.float32 and len(clip) == len(quiet) for clip, _ in made)


def test_a_piece_is_never_turned_up_until_it_clips(engine):
    np = pytest.importorskip("numpy")
    clip = _speechlike(np, size=0.01)
    clip[1000] = 0.5                                                 # one sharp moment in a quiet piece
    levelled, gain_db = engine.level(clip, 24000, -16.0)
    assert float(np.abs(levelled).max()) <= engine.PEAK_CEILING + 1e-6
    assert 0 < gain_db < 6.0                                         # held back by that moment, not by the target
    hot = _speechlike(np, size=0.3)
    hot = np.clip(hot, -1.0, 1.0)
    assert float(np.abs(engine.level(hot, 24000, -10.0)[0]).max()) <= engine.PEAK_CEILING + 1e-6


def test_silence_stays_silence_and_levelling_can_be_switched_off(engine):
    np = pytest.importorskip("numpy")
    nothing = np.zeros(24000, dtype=np.float32)
    assert engine.loudness_of(nothing, 24000) is None
    levelled, gain_db = engine.level(nothing, 24000, -22.0)
    assert gain_db == 0.0 and not levelled.any()
    faint = _speechlike(np, size=0.00002)                             # barely above nothing: not turned into hiss
    assert engine.level(faint, 24000, -22.0)[1] <= engine.MAX_GAIN_DB + 1e-6
    clip = _speechlike(np, size=0.02)
    for off in (None, 0, 0.0, 3.0):
        same, gain_db = engine.level(clip, 24000, off)
        assert gain_db == 0.0 and np.array_equal(same, clip)
    short = _speechlike(np, seconds=0.2, size=0.02)                  # too short for the meter: judged by its plain level
    assert engine.loudness_of(short, 24000) is not None and engine.level(short, 24000, -22.0)[1] > 0
    assert '"--loudness", type=float, default=-22.0' in SOURCE


# ── the speed-up (needs PyTorch) ───────────────────────────────────────────

class _Layer:
    """What the speed-up needs of GPT-2's Conv1D: ``nf``, ``weight`` (inputs x outputs) and ``bias``."""

    def __init__(self, torch, inputs, outputs, scale=0.02):
        self.nf = outputs
        self.weight = torch.nn.Parameter(torch.randn(inputs, outputs) * scale)
        self.bias = torch.nn.Parameter(torch.randn(outputs) * 0.1)


def test_one_row_gives_the_same_answer_to_within_half_precision(engine):
    torch = pytest.importorskip("torch")
    torch.manual_seed(3)
    layer = _Layer(torch, 256, 768)
    fast = engine.make_row_at_a_time()(layer)
    with torch.no_grad():
        for shape in ((1, 1, 256), (1, 256)):
            row = torch.randn(*shape)
            want = torch.addmm(layer.bias, row.reshape(-1, 256), layer.weight).view(*shape[:-1], 768)
            got = fast(row)
            assert got.shape == want.shape and got.dtype == want.dtype
            assert float((got - want).abs().max() / want.abs().max()) < 5e-3
    assert fast.exact_rows == 0


def test_more_than_one_row_is_computed_exactly_as_before(engine):
    torch = pytest.importorskip("torch")
    torch.manual_seed(4)
    layer = _Layer(torch, 128, 384)
    fast = engine.make_row_at_a_time()(layer)
    with torch.no_grad():
        for shape in ((1, 7, 128), (2, 1, 128), (3, 128)):
            rows = torch.randn(*shape)
            want = torch.addmm(layer.bias, rows.reshape(-1, 128), layer.weight).view(*shape[:-1], 384)
            assert torch.equal(fast(rows), want)


def test_a_value_too_big_for_half_precision_is_computed_exactly(engine):
    torch = pytest.importorskip("torch")
    torch.manual_seed(5)
    layer = _Layer(torch, 64, 96)
    fast = engine.make_row_at_a_time()(layer)
    with torch.no_grad():
        row = torch.randn(1, 1, 64)
        row[0, 0, 5] = 3.0e6                                          # half precision stops at 65,504
        want = torch.addmm(layer.bias, row.reshape(-1, 64), layer.weight).view(1, 1, 96)
        got = fast(row)
    assert bool(torch.isfinite(got).all()) and torch.equal(got, want)
    assert fast.exact_rows == 1


def test_the_change_is_kept_only_where_it_is_quicker_and_agrees(engine, monkeypatch):
    torch = pytest.importorskip("torch")
    torch.manual_seed(6)
    layer = _Layer(torch, 256, 512)
    monkeypatch.setattr(engine, "ROW_SPEEDUP_NEEDED", 0.0)
    keep, speedup, error, line = engine.try_row_at_a_time(layer, rounds=3)
    assert keep is True and speedup > 0 and error < 5e-3 and "kept" in line
    monkeypatch.setattr(engine, "ROW_SPEEDUP_NEEDED", 1.0e9)          # nothing is that much quicker
    keep, _, _, line = engine.try_row_at_a_time(layer, rounds=3)
    assert keep is False and "not kept" in line
    monkeypatch.setattr(engine, "ROW_SPEEDUP_NEEDED", 0.0)
    monkeypatch.setattr(engine, "ROW_ERROR_ALLOWED", 0.0)             # nor does half precision agree exactly
    assert engine.try_row_at_a_time(layer, rounds=3)[0] is False


def test_every_layer_of_the_token_model_is_changed_or_none_is(engine, monkeypatch):
    torch = pytest.importorskip("torch")
    Conv1D = pytest.importorskip("transformers.pytorch_utils").Conv1D

    def model():
        block = torch.nn.Module()
        block.c_attn, block.c_proj = Conv1D(96, 32), Conv1D(32, 96)
        tfmr = torch.nn.Module()
        tfmr.h = torch.nn.ModuleList([block])
        return types.SimpleNamespace(t3=types.SimpleNamespace(tfmr=tfmr))

    monkeypatch.setattr(engine, "ROW_SPEEDUP_NEEDED", 0.0)
    changed = model()
    count, line = engine.speed_up_token_loop(changed)
    assert count == 2 and "kept" in line
    assert all(type(m).__name__ == "RowAtATime" for m in (changed.t3.tfmr.h[0].c_attn, changed.t3.tfmr.h[0].c_proj))
    assert engine.rows_redone_exactly(changed) == 0
    with torch.no_grad():
        row = torch.randn(1, 1, 32)
        want = torch.addmm(changed.t3.tfmr.h[0].c_attn.bias, row.reshape(-1, 32), changed.t3.tfmr.h[0].c_attn.weight)
        assert float((changed.t3.tfmr.h[0].c_attn(row).reshape(-1) - want.reshape(-1)).abs().max()) < 1e-2

    monkeypatch.setattr(engine, "ROW_SPEEDUP_NEEDED", 1.0e9)
    left = model()
    count, line = engine.speed_up_token_loop(left)
    assert count == 0 and "not kept" in line
    assert isinstance(left.t3.tfmr.h[0].c_attn, Conv1D)

    empty = types.SimpleNamespace(t3=types.SimpleNamespace(tfmr=torch.nn.Module()))
    assert engine.speed_up_token_loop(empty)[0] == 0
