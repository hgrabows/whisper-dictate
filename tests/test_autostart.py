"""Autostart (LaunchAgent) for source runs, written into a temporary home."""

import plistlib
from pathlib import Path

import whisper_dictate as app


def test_source_run_autostart_writes_plist_without_starting_now(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(app, "IS_FROZEN", False)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: calls.append(a))

    ok, _msg = app.autostart_enable()
    assert ok
    assert app.autostart_is_enabled()
    # No launchctl bootstrap: a second instance must not start right now.
    assert calls == []

    plist_path = tmp_path / "Library" / "LaunchAgents" / f"{app.AUTOSTART_LABEL}.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    python, script = payload["ProgramArguments"]
    assert Path(script).name == "whisper_dictate.py"
    assert Path(python).name.startswith("python")
    assert payload["WorkingDirectory"] == str(Path(script).parent)
    assert payload["RunAtLoad"] is True
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"]

    ok, _msg = app.autostart_disable()
    assert ok
    assert not plist_path.exists()


def test_permission_binary_is_the_real_python_for_source_runs(monkeypatch) -> None:
    monkeypatch.setattr(app, "IS_FROZEN", False)
    binary = app.autostart_permission_binary()
    assert binary is not None and binary.is_file()
    assert not binary.is_symlink()
    monkeypatch.setattr(app, "IS_FROZEN", True)
    assert app.autostart_permission_binary() is None
