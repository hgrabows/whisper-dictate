"""Tiny UI-language layer: German or English, picked once at startup.

Usage: ``tr("Deutscher Text", "English text")`` returns the string for the
active UI language. Keeping both strings at the call site avoids a separate
catalog that drifts out of sync with the code.

The language comes from ``UI_LANGUAGE`` (environment or config.json):
``"de"``, ``"en"`` or ``"auto"`` (default: follow the macOS system language,
English for anything other than German). Changing it takes effect after a
restart.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

SUPPORTED_UI_LANGUAGES = ("de", "en")
DEFAULT_UI_LANGUAGE = "en"

_ui_language: Optional[str] = None


def detect_system_language() -> str:
    """Return "de" if German is the preferred macOS language, else "en"."""
    try:
        out = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        match = re.search(r'"?([A-Za-z]{2})[-_A-Za-z]*"?', out.replace("(", " "))
        if match:
            code = match.group(1).lower()
            return code if code in SUPPORTED_UI_LANGUAGES else DEFAULT_UI_LANGUAGE
    except Exception:
        pass
    env_locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if env_locale.lower().startswith("de"):
        return "de"
    return DEFAULT_UI_LANGUAGE


def resolve_ui_language(raw: Optional[str]) -> str:
    value = (raw or "").strip().lower()
    if value in SUPPORTED_UI_LANGUAGES:
        return value
    return detect_system_language()


def set_ui_language(raw: Optional[str]) -> str:
    global _ui_language
    _ui_language = resolve_ui_language(raw)
    return _ui_language


def ui_language() -> str:
    if _ui_language is None:
        return set_ui_language(os.environ.get("UI_LANGUAGE"))
    return _ui_language


def tr(de: str, en: str) -> str:
    """Pick the German or English variant for the active UI language."""
    return de if ui_language() == "de" else en


def prompt_template_filename(language: Optional[str] = None) -> str:
    """Shipped prompt template for a language: German or English."""
    return "prompt.json" if (language or ui_language()) == "de" else "prompt.en.json"
