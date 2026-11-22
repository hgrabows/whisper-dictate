import json
import os
import stat
import sys
import time

import pytest

import whisper_server
from whisper_server import (
    WhisperServerManager,
    WhisperServerUnavailable,
    parse_verbose_json,
)

FAKE_SERVER = r'''#!{python}
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
with open({argv_log!r}, "w") as fh:
    json.dump(args, fh)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        lang = "de" if b'name="language"\r\n\r\nde' in body else "?"
        out = {{"text": " Hallo Welt.", "segments": [
            {{"start": 0.0, "end": 1.0, "text": " Hallo"}},
            {{"start": 1.0, "end": 2.0, "text": " Welt."}},
            {{"start": 2.0, "end": 2.5, "text": "  "}}], "language": lang}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.fixture
def fake_server(tmp_path):
    argv_log = tmp_path / "argv.json"
    script = tmp_path / "whisper-server"
    script.write_text(FAKE_SERVER.format(python=sys.executable, argv_log=str(argv_log)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, argv_log


def _wait_ready(manager, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager._ready.is_set():
            return
        time.sleep(0.05)
    raise AssertionError("fake whisper-server did not become ready")


def test_parse_verbose_json_keeps_timed_non_empty_segments() -> None:
    segs = parse_verbose_json(json.dumps({
        "text": "a b",
        "segments": [{"start": 0, "end": 1.5, "text": " a "}, {"start": 2, "end": 3, "text": ""}],
    }))
    assert [(s.start_sec, s.end_sec, s.text) for s in segs] == [(0.0, 1.5, "a")]


def test_parse_verbose_json_rejoins_words_split_across_segments() -> None:
    segs = parse_verbose_json(json.dumps({"segments": [
        {"start": 0, "end": 1, "text": " die Über"},
        {"start": 1, "end": 2, "text": "arbeitung mit"},
        {"start": 2, "end": 3, "text": " einem LLM."},
    ]}))
    assert [s.text for s in segs] == ["die Überarbeitung mit", "einem LLM."]
    assert (segs[0].start_sec, segs[0].end_sec) == (0.0, 2.0)


def test_parse_verbose_json_falls_back_to_text_and_reports_errors() -> None:
    segs = parse_verbose_json('{"text": " nur  Text ", "duration": 2.0}')
    assert [(s.start_sec, s.end_sec, s.text) for s in segs] == [(0.0, 2.0, "nur Text")]
    with pytest.raises(WhisperServerUnavailable):
        parse_verbose_json('{"error": "failed to read audio"}')


def test_transcribe_without_running_server_is_unavailable(tmp_path) -> None:
    manager = WhisperServerManager("/nonexistent/whisper-server", tmp_path / "pid")
    manager.start("model.bin", ["-mc", "0"])  # binary missing -> stays off
    with pytest.raises(WhisperServerUnavailable):
        manager.transcribe(tmp_path / "a.wav", "model.bin", ["-mc", "0"], "de")


def test_server_lifecycle_request_and_model_switch(tmp_path, fake_server) -> None:
    script, argv_log = fake_server
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF....WAVE")
    manager = WhisperServerManager(str(script), tmp_path / "whisper-server.pid")
    try:
        manager.start("model-a.bin", ["-mc", "0", "-sns"])
        _wait_ready(manager)

        args = json.loads(argv_log.read_text())
        assert args[:2] == ["-m", "model-a.bin"]
        assert args[args.index("--host") + 1] == "127.0.0.1"
        # Speed flag first, user args last so they win.
        assert args[-4:] == ["-nlp", "-mc", "0", "-sns"]
        assert (tmp_path / "whisper-server.pid").read_text().strip() == str(manager._proc.pid)

        segs = manager.transcribe(wav, "model-a.bin", ["-mc", "0", "-sns"], "de")
        assert [s.text for s in segs] == ["Hallo", "Welt."]

        # A request for another model must not be served by the old process.
        with pytest.raises(WhisperServerUnavailable):
            manager.transcribe(wav, "model-b.bin", ["-mc", "0", "-sns"], "de")

        first_proc = manager._proc
        manager.start("model-a.bin", ["-mc", "0", "-sns"])  # same config: no restart
        assert manager._proc is first_proc
    finally:
        manager.stop()
    assert first_proc.poll() is not None
    assert not (tmp_path / "whisper-server.pid").exists()


def test_watchdog_stops_server_when_terminated(tmp_path, fake_server) -> None:
    script, argv_log = fake_server
    manager = WhisperServerManager(str(script), tmp_path / "pid")
    try:
        manager.start("m.bin", [])
        _wait_ready(manager)
        port = manager._port
    finally:
        manager.stop()
    # The real server child (not just the watchdog) must be gone: port closed.
    import socket

    deadline = time.time() + 5
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    raise AssertionError("whisper-server child still listening after stop()")


def test_crashed_server_is_restarted(tmp_path, fake_server) -> None:
    script, _ = fake_server
    manager = WhisperServerManager(str(script), tmp_path / "pid")
    try:
        manager.start("m.bin", [])
        _wait_ready(manager)
        old = manager._proc
        old_port = manager._port
        old.kill()  # watchdog SIGKILLed: its server child must not survive a restart
        old.wait()
        manager.restart_if_crashed()
        import socket

        with socket.socket() as s:
            assert s.connect_ex(("127.0.0.1", old_port)) != 0
        assert manager._proc is not old
        _wait_ready(manager)
        assert manager.transcribe(tmp_path / "pid", "m.bin", [], None)
    finally:
        manager.stop()


def test_dictate_uses_server_segments_with_tail_filter(monkeypatch, tmp_path) -> None:
    import whisper_dictate as app

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    seen = {}

    def fake_transcribe(path, model, args, language):
        seen["call"] = (path, model, language)
        return [whisper_server.ServerSegment(0.0, 1.0, "Hallo"), whisper_server.ServerSegment(1.0, 2.0, "Welt.")]

    monkeypatch.setattr(app, "MODEL", "/x/ggml-large-v3-turbo-q5_0.bin")
    monkeypatch.setattr(app, "WHISPER_SERVER_ENABLED", True)
    monkeypatch.setattr(app.WHISPER_SERVER, "transcribe", fake_transcribe)
    assert app.transcribe(str(wav)) == "Hallo Welt."
    assert seen["call"][1] == "/x/ggml-large-v3-turbo-q5_0.bin"


def test_dictate_falls_back_to_cli_when_server_unavailable(monkeypatch, tmp_path) -> None:
    import whisper_dictate as app

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")

    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"m")

    def unavailable(*_a, **_k):
        raise WhisperServerUnavailable("still loading")

    monkeypatch.setattr(app, "MODEL", str(model))
    monkeypatch.setattr(app, "WHISPER_SERVER_ENABLED", True)
    monkeypatch.setattr(app.WHISPER_SERVER, "transcribe", unavailable)
    monkeypatch.setattr(
        app.subprocess,
        "check_output",
        lambda cmd, **kw: "[00:00:00.000 --> 00:00:01.000]  Per CLI.\n",
    )
    assert app.transcribe(str(wav)) == "Per CLI."
