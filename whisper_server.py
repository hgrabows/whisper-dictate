"""Keep a whisper.cpp model loaded in a local `whisper-server` process.

`whisper-cli` loads the model and initialises Metal on every dictation
(~0.4 s on Apple Silicon). `whisper-server` ships in the same Homebrew formula
(`brew install whisper-cpp`), so the app starts it once in the background,
bound to 127.0.0.1 on a free port, and sends each WAV to it over HTTP.

Every failure (binary missing, server not ready yet, crash, HTTP error) is
reported as WhisperServerUnavailable so the caller can fall back to
`whisper-cli`; dictation never depends on the server.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

HOST = "127.0.0.1"
# -nlp: without it every verbose_json request also scores all languages,
# which cost ~0.45 s per dictation (measured 1.13 s -> 0.70 s on M1 Max).
# Decoding stays at the server default (greedy): beam 5/5 was slower and once
# appended an invented "Bis zum nächsten Mal." to a real dictation.
# User-supplied WHISPER_ARGS come later on the command line and win.
DEFAULT_SERVER_ARGS: tuple[str, ...] = ("-nlp",)

# The server runs under this tiny watchdog: it stops the server when it is
# terminated itself or when the app disappears without cleanup (os._exit,
# SIGKILL, crash), so no 600 MB model is left loaded in an orphan process.
_WATCHDOG = r"""
import os, signal, subprocess, sys, time
parent = int(sys.argv[1])
child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)
def stop(*_):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(3)
        except Exception:
            child.kill()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while child.poll() is None:
    if os.getppid() != parent:
        stop()
    time.sleep(1)
sys.exit(child.returncode or 1)
"""


class WhisperServerUnavailable(RuntimeError):
    """The server cannot serve this request; use whisper-cli instead."""


@dataclass(frozen=True)
class ServerSegment:
    start_sec: float
    end_sec: float
    text: str


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def parse_verbose_json(payload: bytes | str) -> list[ServerSegment]:
    data = json.loads(payload)
    if isinstance(data, dict) and data.get("error"):
        raise WhisperServerUnavailable(f"whisper-server: {data['error']}")
    segments: list[ServerSegment] = []
    for seg in (data.get("segments") or []) if isinstance(data, dict) else []:
        raw = str(seg.get("text") or "")
        text = raw.strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        if segments and not raw[:1].isspace():
            # The server sometimes splits inside a word ("Über" + "arbeitung");
            # a segment without leading whitespace continues the previous one.
            prev = segments[-1]
            segments[-1] = ServerSegment(prev.start_sec, max(prev.end_sec, end), prev.text + text)
            continue
        segments.append(ServerSegment(start_sec=start, end_sec=end, text=text))
    if not segments and isinstance(data, dict) and str(data.get("text") or "").strip():
        text = " ".join(str(data["text"]).split())
        segments.append(ServerSegment(0.0, float(data.get("duration") or 0.0), text))
    return segments


def _multipart(fields: dict[str, str], file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
    )
    body = b"".join(parts) + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _group_members(pgid: int) -> list[int]:
    """PIDs in process group `pgid` that belong to our whisper-server launch.

    Signals go to verified members only: once a group is empty its id can be
    reused by an unrelated program, so a blind killpg() would be unsafe.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,command="], capture_output=True, text=True, timeout=2
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pgid) and "whisper-server" in parts[2]:
            pids.append(int(parts[0]))
    return pids


def _kill_group(pgid: int, sig: int) -> None:
    for pid in _group_members(pgid):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _is_our_server(pid: int) -> bool:
    try:
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2
        ).stdout
    except Exception:
        return False
    return "whisper-server" in cmd and f"--host {HOST}" in cmd


class WhisperServerManager:
    def __init__(
        self,
        server_cmd: str,
        pid_file: Path,
        *,
        startup_timeout: float = 60.0,
        request_timeout: float = 120.0,
    ) -> None:
        self.server_cmd = server_cmd
        self.pid_file = Path(pid_file)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._config: Optional[tuple[str, tuple[str, ...]]] = None
        self._ready = threading.Event()
        self._failed_config: Optional[tuple[str, tuple[str, ...]]] = None

    # -- lifecycle -----------------------------------------------------
    def available(self) -> bool:
        return bool(shutil.which(self.server_cmd) or Path(self.server_cmd).is_file())

    def _cleanup_stale(self) -> None:
        """Kill a server left behind by a previous run that was killed hard."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except Exception:
            return
        if pid != os.getpid() and _is_our_server(pid):
            _kill_group(pid, signal.SIGTERM)
            print(f"🧹 Alten whisper-server beendet (pid {pid}).")
        try:
            self.pid_file.unlink()
        except Exception:
            pass

    def start(self, model_path: str, extra_args: Sequence[str] = ()) -> None:
        """(Re)start the server for `model_path` unless it already serves it.

        Returns immediately; the model loads in the background and requests
        fall back to whisper-cli until it is ready.
        """
        config = (str(model_path), tuple(extra_args))
        with self._lock:
            if self._config == config and self._proc is not None and self._proc.poll() is None:
                return
            self._stop_locked()
            if self._failed_config == config:
                return
            if not self.available():
                print(f"ℹ️ whisper-server not found ({self.server_cmd}) – using whisper-cli.")
                self._failed_config = config
                return
            self._cleanup_stale()
            port = find_free_port()
            cmd = [
                self.server_cmd,
                "-m", str(model_path),
                "--host", HOST,
                "--port", str(port),
                *DEFAULT_SERVER_ARGS,
                *extra_args,
            ]
            try:
                # A frozen (PyInstaller) build has no usable `python -c`.
                launch = cmd if getattr(sys, "frozen", False) else [
                    sys.executable, "-I", "-c", _WATCHDOG, str(os.getpid()), *cmd
                ]
                # Own process group: stopping kills watchdog *and* server even
                # if the watchdog itself was killed hard.
                proc = subprocess.Popen(
                    launch,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                print(f"⚠️ whisper-server could not start: {e} – using whisper-cli.")
                self._failed_config = config
                return
            self._proc, self._port, self._config = proc, port, config
            self._ready.clear()
            try:
                self.pid_file.parent.mkdir(parents=True, exist_ok=True)
                self.pid_file.write_text(str(proc.pid))
            except Exception:
                pass
        threading.Thread(target=self._wait_ready, args=(proc, port, config), daemon=True).start()

    def _wait_ready(self, proc: subprocess.Popen, port: int, config) -> None:
        started = time.time()
        deadline = started + self.startup_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=1):
                    pass
            except urllib.error.HTTPError:
                pass  # any HTTP answer means the server is listening
            except Exception:
                time.sleep(0.2)
                continue
            with self._lock:
                if self._proc is proc:
                    self._ready.set()
            print(f"⚡ whisper-server bereit ({time.time() - started:.1f}s, Port {port})")
            return
        with self._lock:
            if self._proc is proc:
                print("⚠️ whisper-server did not come up – using whisper-cli.")
                self._failed_config = config
                self._stop_locked()

    def restart_if_crashed(self) -> None:
        """Restart a server that died while in use (no-op if never started)."""
        with self._lock:
            config, proc = self._config, self._proc
            if config is None or proc is None or proc.poll() is None:
                return
            print("⚠️ whisper-server wurde beendet – starte neu.")
        self.start(config[0], config[1])

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._port = None
        self._config = None
        self._ready.clear()
        if proc is not None:
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except Exception:
                _kill_group(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
            # The group may outlive the watchdog (e.g. it was SIGKILLed).
            _kill_group(proc.pid, signal.SIGKILL)
        if proc is not None:
            try:
                if self.pid_file.read_text().strip() == str(proc.pid):
                    self.pid_file.unlink()
            except Exception:
                pass

    # -- inference -----------------------------------------------------
    def transcribe(
        self,
        wav_path: str | Path,
        model_path: str,
        extra_args: Sequence[str],
        language: Optional[str],
    ) -> list[ServerSegment]:
        with self._lock:
            config = (str(model_path), tuple(extra_args))
            proc, port = self._proc, self._port
            if self._config != config or proc is None or port is None or proc.poll() is not None:
                raise WhisperServerUnavailable("whisper-server is not running for this model")
            if not self._ready.is_set():
                raise WhisperServerUnavailable("whisper-server is still loading")
        fields = {"response_format": "verbose_json", "temperature": "0.0"}
        if language:
            fields["language"] = language
        body, content_type = _multipart(fields, Path(wav_path).read_bytes())
        req = urllib.request.Request(
            f"http://{HOST}:{port}/inference",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                payload = resp.read()
        except Exception as e:
            raise WhisperServerUnavailable(f"whisper-server Anfrage fehlgeschlagen: {e}") from e
        try:
            return parse_verbose_json(payload)
        except WhisperServerUnavailable:
            raise
        except Exception as e:
            raise WhisperServerUnavailable(f"whisper-server Antwort unlesbar: {e}") from e
