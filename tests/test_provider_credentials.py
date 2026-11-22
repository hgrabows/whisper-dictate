import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def load_credentials():
    # Isolate the credentials helpers from macOS and dictation startup effects.
    source_path = Path(__file__).resolve().parents[1] / "whisper_dictate.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    helpers = {
        "_load_yaml_config",
        "_normalize_provider_models",
        "_load_provider_api_key",
        "_load_provider_credentials",
    }
    module = ast.Module(
        body=[
            node for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name in helpers
        ],
        type_ignores=[],
    )
    namespace = {"Path": Path, "Any": Any, "re": re, "yaml": yaml}
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["_load_provider_credentials"]


def write_credentials(path: Path, **key_settings) -> Path:
    provider = {
        "api_url": "https://api.deepseek.com/chat/completions",
        "models": ["deepseek-flash"],
        "default_model": "deepseek-flash",
        **key_settings,
    }
    path.write_text(
        yaml.safe_dump({"providers": {"deepseek": provider}}),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "key_text, expected",
    [
        ("  sk-test-secret  \n", "sk-test-secret"),
        ("Deepseek\n\nAPI key: sk-test-secret\n---\n", "sk-test-secret"),
        ("\ufeffsk-test-secret\n", "sk-test-secret"),
        ("plain-test-token\n", "plain-test-token"),
    ],
)
def test_credentials_load_key_file(load_credentials, tmp_path, key_text, expected):
    key_path = tmp_path / "key.txt"
    key_path.write_text(key_text, encoding="utf-8")
    path = write_credentials(
        tmp_path / "credentials.yaml", api_key_file=str(key_path)
    )

    provider = load_credentials(path)["deepseek"]

    assert provider["api_key"] == expected
    assert provider["models"] == ["deepseek-flash"]
    assert provider["default_model"] == "deepseek-flash"


def test_credentials_resolve_relative_key_file(load_credentials, tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "key.txt").write_text("sk-relative-secret\n", encoding="utf-8")
    path = write_credentials(config_dir / "credentials.yaml", api_key_file="key.txt")
    monkeypatch.chdir(tmp_path)

    assert load_credentials(path)["deepseek"]["api_key"] == "sk-relative-secret"


def test_credentials_expand_home_key_file(load_credentials, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "key.txt").write_text("sk-home-secret\n", encoding="utf-8")
    path = write_credentials(
        tmp_path / "credentials.yaml", api_key_file="~/key.txt"
    )

    assert load_credentials(path)["deepseek"]["api_key"] == "sk-home-secret"


def test_inline_key_takes_precedence(load_credentials, tmp_path, capsys):
    path = write_credentials(
        tmp_path / "credentials.yaml",
        api_key="inline-secret",
        api_key_file="missing.txt",
    )

    assert load_credentials(path)["deepseek"]["api_key"] == "inline-secret"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "key_text",
    [None, "", "invalid multiline\ncontents\n", "sk-first-secret\nsk-second-secret\n"],
)
def test_invalid_key_file_does_not_expose_content(
    load_credentials, tmp_path, capsys, key_text
):
    key_path = tmp_path / "key.txt"
    if key_text is not None:
        key_path.write_text(key_text, encoding="utf-8")
    path = write_credentials(
        tmp_path / "credentials.yaml", api_key_file=str(key_path)
    )

    assert load_credentials(path)["deepseek"]["api_key"] == ""
    output = capsys.readouterr().out
    assert "API key file" in output
    assert "sk-first-secret" not in output
    assert "sk-second-secret" not in output
    assert "invalid multiline" not in output


def test_credentials_without_key_settings(load_credentials, tmp_path):
    path = write_credentials(tmp_path / "credentials.yaml")

    assert load_credentials(path)["deepseek"]["api_key"] == ""
