import json

import pytest

import i18n
from whisper_models import WHISPER_MODEL_CATALOG


@pytest.fixture
def restore_ui_language():
    previous = i18n.ui_language()
    yield
    i18n.set_ui_language(previous)


def test_catalog_labels_follow_ui_language(restore_ui_language) -> None:
    spec = WHISPER_MODEL_CATALOG["large-turbo"]
    i18n.set_ui_language("de")
    assert spec.label == "large-v3-turbo (empfohlen)"
    assert "Qualität" in spec.description
    i18n.set_ui_language("en")
    assert spec.label == "large-v3-turbo (recommended)"
    assert "quality" in spec.description
    for item in WHISPER_MODEL_CATALOG.values():
        assert item.label and item.description


def test_ui_language_setting_env_wins_over_config(tmp_path, monkeypatch) -> None:
    from whisper_dictate import _read_ui_language_setting

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"UI_LANGUAGE": "de"}), encoding="utf-8")
    monkeypatch.delenv("UI_LANGUAGE", raising=False)
    assert _read_ui_language_setting(config) == "de"
    monkeypatch.setenv("UI_LANGUAGE", "EN")
    assert _read_ui_language_setting(config) == "en"
    monkeypatch.setenv("UI_LANGUAGE", "fr")
    assert _read_ui_language_setting(config) == "auto"
    monkeypatch.delenv("UI_LANGUAGE")
    assert _read_ui_language_setting(tmp_path / "missing.json") == "auto"


@pytest.mark.parametrize(
    "utterance",
    ["Enter", "drücke Enter", "Drücke die Entertaste.", "Eingabetaste",
     "enter key", "Press enter.", "Hit the Enter key!", "press return", "hit the return key"],
)
def test_enter_command_accepts_german_and_english(utterance) -> None:
    from whisper_dictate import is_enter_command

    assert is_enter_command(utterance)


@pytest.mark.parametrize("utterance", ["return", "return key", "enter the room", "press", "Please return the book"])
def test_enter_command_ignores_normal_dictation(utterance) -> None:
    from whisper_dictate import is_enter_command, strip_trailing_enter_command

    assert not is_enter_command(utterance)
    if utterance == "Please return the book":
        assert strip_trailing_enter_command(utterance) == (utterance, False)


def test_trailing_english_enter_is_stripped() -> None:
    from whisper_dictate import strip_trailing_enter_command

    assert strip_trailing_enter_command("Write this. Press enter.") == ("Write this.", True)
    assert strip_trailing_enter_command("Hallo Welt Enter") == ("Hallo Welt", True)


def test_delete_and_undo_commands_in_both_languages() -> None:
    from whisper_dictate import is_delete_command, is_undo_command

    for phrase in ("Löschen!", "lösche das", "Delete that.", "delete", "Scratch that"):
        assert is_delete_command(phrase), phrase
    for phrase in ("Rückgängig.", "mach das rückgängig", "Undo", "undo that"):
        assert is_undo_command(phrase), phrase
    for phrase in ("delete the file", "undo the changes", "das ist gelöscht"):
        assert not is_delete_command(phrase) and not is_undo_command(phrase), phrase


def test_prompt_template_follows_ui_language_with_german_fallback(tmp_path, monkeypatch, restore_ui_language) -> None:
    import whisper_dictate as dictate

    monkeypatch.setattr(dictate, "_candidate_resource_paths", lambda name: [tmp_path / name])
    (tmp_path / "prompt.json").write_text("{}", encoding="utf-8")

    i18n.set_ui_language("en")
    assert dictate._prompt_template_name() == "prompt.json"  # English file missing
    (tmp_path / "prompt.en.json").write_text("{}", encoding="utf-8")
    assert dictate._prompt_template_name() == "prompt.en.json"
    i18n.set_ui_language("de")
    assert dictate._prompt_template_name() == "prompt.json"
