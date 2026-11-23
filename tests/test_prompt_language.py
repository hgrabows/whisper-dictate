"""Built-in prompt modes follow the interface language; edited prompts stay."""

import json
import shutil
from pathlib import Path

import whisper_dictate as app

ROOT = Path(__file__).resolve().parent.parent
DE = ROOT / "prompt.json"
EN = ROOT / "prompt.en.json"


def _labels(path: Path) -> list[str]:
    modes = json.loads(path.read_text(encoding="utf-8"))["modes"]
    return [entry["label"] for entry in modes.values()]


def test_untouched_german_prompts_switch_to_english(tmp_path) -> None:
    current = tmp_path / "current_prompts.json"
    shutil.copy(DE, current)
    assert app.swap_untouched_prompts_to_language(current, [EN], [DE])
    assert _labels(current) == _labels(EN)
    # Already in the wanted language: nothing more to do.
    assert not app.swap_untouched_prompts_to_language(current, [EN], [DE])


def test_untouched_english_prompts_switch_to_german(tmp_path) -> None:
    current = tmp_path / "current_prompts.json"
    shutil.copy(EN, current)
    assert app.swap_untouched_prompts_to_language(current, [DE], [EN])
    assert _labels(current) == _labels(DE)


def test_builtin_modes_switch_but_own_and_edited_modes_stay(tmp_path) -> None:
    current = tmp_path / "current_prompts.json"
    payload = json.loads(DE.read_text(encoding="utf-8"))
    own = {"label": "Medium korrigieren", "prompt": "Korrigiere."}
    payload["modes"]["7"] = own
    payload["modes"]["3"]["prompt"] += "\nZusatz."
    edited = payload["modes"]["3"]
    current.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    assert app.swap_untouched_prompts_to_language(current, [EN], [DE])
    modes = json.loads(current.read_text(encoding="utf-8"))["modes"]
    en_modes = json.loads(EN.read_text(encoding="utf-8"))["modes"]
    assert modes["1"] == en_modes["1"] and modes["6"] == en_modes["6"]
    assert modes["3"] == edited
    assert modes["7"] == own


def test_nothing_to_switch_leaves_file_alone(tmp_path) -> None:
    current = tmp_path / "current_prompts.json"
    original = json.dumps({"modes": {"9": {"label": "Eigen", "prompt": "x"}}}, indent=2)
    current.write_text(original, encoding="utf-8")
    assert not app.swap_untouched_prompts_to_language(current, [EN], [DE])
    assert current.read_text(encoding="utf-8") == original


def test_shipped_templates_have_the_same_modes() -> None:
    de = json.loads(DE.read_text(encoding="utf-8"))["modes"]
    en = json.loads(EN.read_text(encoding="utf-8"))["modes"]
    assert de.keys() == en.keys()


def test_manual_url_follows_interface_language(monkeypatch) -> None:
    monkeypatch.setattr(app, "ui_language", lambda: "de")
    assert app.manual_url().endswith("/docs/manual.de.md")
    monkeypatch.setattr(app, "ui_language", lambda: "en")
    assert app.manual_url() == "https://github.com/hgrabows/whisper-dictate/blob/main/docs/manual.md"
