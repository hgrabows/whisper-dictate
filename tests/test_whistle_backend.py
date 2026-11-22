import os
import sys
import types

import whistle_backend


def test_whistle_language_maps_supported_codes_and_falls_back_to_auto() -> None:
    assert whistle_backend.whistle_language("de") == "de"
    assert whistle_backend.whistle_language(" EN ") == "en"
    assert whistle_backend.whistle_language("ja") is None
    assert whistle_backend.whistle_language("auto") is None
    assert whistle_backend.whistle_language(None) is None


def test_transcribe_uses_one_cached_model_with_telemetry_off(monkeypatch, tmp_path) -> None:
    created = []
    calls = []

    class FakeWhistle:
        def __init__(self, weights):
            created.append(weights)

        def transcribe(self, audio, language=None, keywords=None):
            calls.append((audio, language, keywords))
            return {"text": "  Hallo   Welt. ", "language": "de"}

    monkeypatch.setitem(sys.modules, "cactus", types.SimpleNamespace(Whistle=FakeWhistle))
    monkeypatch.setenv("CACTUS_TELEMETRY", "1")
    monkeypatch.setattr(whistle_backend, "_models", {})
    weights = tmp_path / "whistle.cact"
    weights.write_bytes(b"x")

    first = whistle_backend.transcribe("a.wav", weights, "de", keywords=["Main Actor", " "])
    second = whistle_backend.transcribe("b.wav", weights, "auto")

    assert first == "Hallo Welt."
    assert second == "Hallo Welt."
    assert created == [str(weights.resolve())]
    assert calls == [("a.wav", "de", ["Main Actor"]), ("b.wav", None, None)]
    assert os.environ["CACTUS_TELEMETRY"] == "0"


def test_dictate_routes_whistle_models_by_filename() -> None:
    from whisper_dictate import _model_engine

    assert _model_engine("/x/models/whistle.cact") == "whistle"
    assert _model_engine("/x/models/ggml-large-v3-turbo-q5_0.bin") == "whisper.cpp"
    assert _model_engine("/x/custom.bin") == "whisper.cpp"
