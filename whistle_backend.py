"""In-process speech recognition with Cactus Whistle (fast, 17 MB model).

whisper.cpp runs as a subprocess per dictation; Whistle instead runs inside
this process through the `cactus-compute` package and keeps its model loaded,
so a dictation is transcribed in tens of milliseconds.

The model file itself is downloaded and SHA-256 verified by whisper_models
like every other speech model. On first use `cactus` fetches its native engine
(libcactus, pinned version and checksum) from Hugging Face into ~/.cache/cactus.
Cactus telemetry is switched off before the package is imported.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

# Languages the Whistle model can transcribe. Anything else falls back to
# Whistle's automatic language detection.
WHISTLE_LANGUAGES: tuple[str, ...] = ("en", "de", "fr", "es", "it", "nl", "pl")

_lock = threading.Lock()
_models: dict[str, Any] = {}


def _disable_telemetry() -> None:
    # Must happen before `cactus` is imported/used: it reads these on each event.
    os.environ["CACTUS_TELEMETRY"] = "0"
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def is_available() -> bool:
    """True when the `cactus-compute` package can be imported."""
    _disable_telemetry()
    try:
        import cactus  # noqa: F401
    except Exception:
        return False
    return True


def whistle_language(language: Optional[str]) -> Optional[str]:
    """Map the app's Whisper language setting to a Whistle language (None = auto)."""
    code = (language or "").strip().lower()
    return code if code in WHISTLE_LANGUAGES else None


def _model(weights: Path) -> Any:
    key = str(Path(weights).resolve())
    with _lock:
        model = _models.get(key)
        if model is None:
            _disable_telemetry()
            from cactus import Whistle

            model = Whistle(weights=key)
            _models.clear()  # only one Whistle model is ever active
            _models[key] = model
        return model


def preload(weights: Path) -> None:
    """Load engine + model ahead of the first dictation (may download libcactus)."""
    _model(weights)


def transcribe(
    wav_path: str | Path,
    weights: Path,
    language: Optional[str] = None,
    keywords: Optional[Iterable[str]] = None,
) -> str:
    """Transcribe a 16 kHz WAV file and return the text (whitespace-normalized)."""
    model = _model(weights)
    words = [w for w in (keywords or ()) if str(w).strip()] or None
    result = model.transcribe(str(wav_path), language=whistle_language(language), keywords=words)
    return " ".join(str(result.get("text") or "").split())
