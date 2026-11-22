import pytest

import whisper_dictate as app


@pytest.mark.parametrize("lang", ["de", "en", "auto"])
def test_whole_utterance_enter_is_always_a_command(monkeypatch, lang) -> None:
    monkeypatch.setenv("WHISPER_LANGUAGE", lang)
    for text in ("Enter.", "drücke Enter", "press enter", "enter key", "Eingabetaste"):
        assert app.is_enter_command(text), text
    assert not app.is_enter_command("return")


def test_english_verb_enter_inside_sentence_stays_text(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_LANGUAGE", "en")
    assert app.strip_trailing_enter_command("Please enter your name.") == ("Please enter your name.", False)
    assert app.strip_trailing_enter_command("Then you enter.") == ("Then you enter.", False)
    assert not list(app._inline_enter_split_re().finditer("please enter your name"))
    assert app.strip_trailing_enter_command("Send it, press enter.") == ("Send it,", True)
    assert app.strip_trailing_enter_command("Send it, enter key.") == ("Send it,", True)


def test_german_keeps_bare_enter_inside_dictation(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_LANGUAGE", "de")
    assert app.strip_trailing_enter_command("Schick das ab, Enter.") == ("Schick das ab,", True)
    assert len(list(app._inline_enter_split_re().finditer("Zeile eins Enter Zeile zwei"))) == 1
