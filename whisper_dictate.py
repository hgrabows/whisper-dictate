#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
macOS: Double-tap Fn (fallback: Control) to toggle recording with sox,
then transcribe with whisper.cpp and paste text into the frontmost app.

Requirements:
  - brew install sox
  - brew install whisper-cpp  (provides `whisper-cli`)
Permissions:
  - System Settings → Privacy & Security:
      Accessibility + Input Monitoring for your terminal / Python
  - Microphone access (on first recording)
"""

import argparse
import audioop
from dataclasses import dataclass
import json
import importlib.util
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse
import urllib.request
import yaml

from key_handler import HAS_QUARTZ, QUARTZ_IMPORT_ERROR, get_default_detector, listen_for_double_tap
from key_handler import listen_for_double_tap_stoppable
import llm_providers
import whistle_backend
from whisper_server import WhisperServerManager, WhisperServerUnavailable
from whisper_models import (
    ENGINE_WHISTLE,
    WHISPER_FALLBACK_ORDER,
    WHISPER_MODEL_CATALOG,
    DownloadCancelled,
    download_whisper_model,
    format_size,
)
from i18n import prompt_template_filename, resolve_ui_language, set_ui_language, tr, ui_language

def _ensure_tcl_tk_library_paths() -> None:
    """Point Tk at its script libraries when Python cannot find them itself.

    uv's standalone CPython builds look for init.tcl only next to the venv and
    at a build-time path, so ``tkinter.Tk()`` fails with "Can't find a usable
    init.tcl" in a plain ``uv run``. The libraries ship in the base
    interpreter's ``lib/tcl8.x`` and ``lib/tk8.x``.
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return
    lib_dir = Path(sys.base_prefix) / "lib"
    for env_key, prefix in (("TCL_LIBRARY", "tcl8."), ("TK_LIBRARY", "tk8.")):
        if os.environ.get(env_key):
            continue
        candidates = sorted(
            p for p in lib_dir.glob(prefix + "*") if (p / ("init.tcl" if prefix == "tcl8." else "tk.tcl")).is_file()
        )
        if candidates:
            os.environ[env_key] = str(candidates[-1])


_ensure_tcl_tk_library_paths()

APP_NAME = "WhisperDictate"
MENU_APP_NAME = "Whisper"
MY_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
RUNTIME_BASE_DIR = Path(getattr(sys, "_MEIPASS", MY_DIR))
if not RUNTIME_BASE_DIR.exists():
    RUNTIME_BASE_DIR = MY_DIR


def _resolve_path_setting(raw_value: str, default_path: Path, base_dir: Optional[Path] = None) -> Path:
    raw = (raw_value or "").strip()
    if not raw:
        return default_path
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    anchor = base_dir or Path.cwd()
    return (anchor / path).resolve()


def _default_app_support_dir() -> Path:
    # Keep source-tree defaults in local development; use Application Support in bundled apps.
    if not IS_FROZEN:
        return MY_DIR
    return (Path.home() / "Library" / "Application Support" / APP_NAME).resolve()


def _candidate_resource_paths(filename: str) -> list[Path]:
    return [
        RUNTIME_BASE_DIR / filename,
        MY_DIR / filename,
    ]


def _copy_template_if_missing(target: Path, source_candidates: list[Path], fallback_text: str = "") -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    for src in source_candidates:
        if src.exists():
            shutil.copy2(src, target)
            return
    target.write_text(fallback_text, encoding="utf-8")


def _first_existing_path(candidates: list[Path]) -> Optional[Path]:
    for src in candidates:
        if src.exists():
            return src
    return None


def _sync_file_from_candidates(
    target: Path,
    source_candidates: list[Path],
    *,
    fallback_text: str = "",
) -> None:
    """Keep target in sync with the first available source candidate."""
    src = _first_existing_path(source_candidates)
    target.parent.mkdir(parents=True, exist_ok=True)
    if src is None:
        if not target.exists() and fallback_text:
            target.write_text(fallback_text, encoding="utf-8")
        return
    try:
        same = target.exists() and target.read_bytes() == src.read_bytes()
    except Exception:
        same = False
    if same:
        return
    shutil.copy2(src, target)


def _resolve_existing_executable(candidates: list[Path]) -> str:
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            if mode & 0o111:
                return str(path)
    return ""


APP_SUPPORT_DIR = _resolve_path_setting(os.environ.get("APP_SUPPORT_DIR", ""), _default_app_support_dir())
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = _resolve_path_setting(os.environ.get("WHISPER_CONFIG_PATH", ""), APP_SUPPORT_DIR / "config.json", APP_SUPPORT_DIR)
CREDENTIALS_PATH = _resolve_path_setting(os.environ.get("WHISPER_CREDENTIALS_PATH", ""), APP_SUPPORT_DIR / "credentials.yaml", APP_SUPPORT_DIR)
LOCAL_MODELS_REGISTRY_PATH = _resolve_path_setting(os.environ.get("LOCAL_MODELS_REGISTRY", ""), APP_SUPPORT_DIR / "local_models.json", APP_SUPPORT_DIR)

_copy_template_if_missing(
    CONFIG_PATH,
    _candidate_resource_paths("config.template.json") + _candidate_resource_paths("config.json"),
    fallback_text="{}\n",
)

UI_LANGUAGE_CHOICES = ("auto", "de", "en")


def _read_ui_language_setting(config_path: Path) -> str:
    """Configured UI language: env UI_LANGUAGE wins over config.json; default "auto"."""
    raw = (os.environ.get("UI_LANGUAGE") or "").strip()
    if not raw:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            raw = str(data.get("UI_LANGUAGE") or "").strip()
    raw = raw.lower()
    return raw if raw in UI_LANGUAGE_CHOICES else "auto"


# Resolve the UI language right after config.json exists: the prompt template
# below and every tr() call depend on it. Changing it requires a restart.
UI_LANGUAGE_SETTING = _read_ui_language_setting(CONFIG_PATH)
set_ui_language(UI_LANGUAGE_SETTING)


def _prompt_template_name() -> str:
    """Shipped prompt template for the UI language (German prompt.json as fallback)."""
    name = prompt_template_filename()
    if any(path.exists() for path in _candidate_resource_paths(name)):
        return name
    return "prompt.json"


PROMPT_TEMPLATE_NAME = _prompt_template_name()


def _prompt_template_candidates() -> list[Path]:
    candidates = _candidate_resource_paths(PROMPT_TEMPLATE_NAME)
    if PROMPT_TEMPLATE_NAME != "prompt.json":
        candidates += _candidate_resource_paths("prompt.json")
    return candidates


PROMPTS_TEMPLATE_PATH = _resolve_path_setting(
    os.environ.get("WHISPER_PROMPTS_TEMPLATE_PATH", ""),
    APP_SUPPORT_DIR / PROMPT_TEMPLATE_NAME,
    APP_SUPPORT_DIR,
)
CURRENT_PROMPTS_PATH = _resolve_path_setting(
    os.environ.get("WHISPER_CURRENT_PROMPTS_PATH", os.environ.get("WHISPER_PROMPTS_PATH", "")),
    APP_SUPPORT_DIR / "current_prompts.json",
    APP_SUPPORT_DIR,
)

_copy_template_if_missing(
    CREDENTIALS_PATH,
    _candidate_resource_paths("credentials.template.yaml") + _candidate_resource_paths("credentials.yaml"),
    fallback_text="providers:\n  local:\n    models: []\n",
)
_copy_template_if_missing(
    PROMPTS_TEMPLATE_PATH,
    _prompt_template_candidates(),
    fallback_text='{"modes": {}}\n',
)
_copy_template_if_missing(
    CURRENT_PROMPTS_PATH,
    [PROMPTS_TEMPLATE_PATH] + _candidate_resource_paths("current_prompts.json") + _prompt_template_candidates(),
    fallback_text='{"modes": {}}\n',
)


def swap_untouched_prompts_to_language(
    current_path: Path, wanted: list[Path], other: list[Path]
) -> bool:
    """Switch the built-in prompt modes to the template of the UI language.

    Mode by mode: a mode is replaced only when it is still an unchanged copy
    of the other language's shipped mode with the same number. Modes the
    user added or edited are never touched.
    """
    def _load(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) and isinstance(data.get("modes"), dict) else None

    current = _load(current_path)
    source = next((p for p in wanted if p.is_file()), None)
    wanted_data = _load(source) if source is not None else None
    other_data = next((d for d in (_load(p) for p in other if p.is_file()) if d is not None), None)
    if current is None or wanted_data is None or other_data is None:
        return False

    if current == other_data:
        # Untouched copy: take the template verbatim (keeps its formatting).
        current_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"🌐 Built-in prompt modes switched to {source.name} (interface language).")
        return True

    switched = []
    modes = current["modes"]
    for key, entry in list(modes.items()):
        if key in wanted_data["modes"] and other_data["modes"].get(key) == entry:
            modes[key] = wanted_data["modes"][key]
            switched.append(key)
    if not switched:
        return False
    current_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"🌐 Prompt modes {', '.join(switched)} switched to {source.name} (interface language); own modes kept.")
    return True


if not os.environ.get("WHISPER_CURRENT_PROMPTS_PATH") and not os.environ.get("WHISPER_PROMPTS_PATH"):
    _other_prompt_template = "prompt.json" if PROMPT_TEMPLATE_NAME == "prompt.en.json" else "prompt.en.json"
    swap_untouched_prompts_to_language(
        CURRENT_PROMPTS_PATH,
        _candidate_resource_paths(PROMPT_TEMPLATE_NAME),
        _candidate_resource_paths(_other_prompt_template),
    )

_copy_template_if_missing(
    LOCAL_MODELS_REGISTRY_PATH,
    _candidate_resource_paths("local_models.json"),
    fallback_text='{"models": []}\n',
)

# In packaged app runs, prompts must match shipped defaults to avoid drift
# between development and release behavior. User-specific files (credentials,
# model registry, tmp, etc.) remain persistent and are not overwritten.
if IS_FROZEN and (os.environ.get("WHISPER_SYNC_PROMPTS_FROM_BUNDLE", "1").strip().lower() in {"1", "true", "yes", "on"}):
    _sync_file_from_candidates(
        PROMPTS_TEMPLATE_PATH,
        _prompt_template_candidates(),
        fallback_text='{"modes": {}}\n',
    )
    _sync_file_from_candidates(
        CURRENT_PROMPTS_PATH,
        _candidate_resource_paths("current_prompts.json") + _prompt_template_candidates(),
        fallback_text='{"modes": {}}\n',
    )

os.environ.setdefault("APP_SUPPORT_DIR", str(APP_SUPPORT_DIR))
os.environ.setdefault("WHISPER_CONFIG_PATH", str(CONFIG_PATH))
os.environ.setdefault("WHISPER_CREDENTIALS_PATH", str(CREDENTIALS_PATH))
os.environ.setdefault("LOCAL_MODELS_REGISTRY", str(LOCAL_MODELS_REGISTRY_PATH))
os.environ.setdefault("WHISPER_PROMPTS_TEMPLATE_PATH", str(PROMPTS_TEMPLATE_PATH))
os.environ.setdefault("WHISPER_CURRENT_PROMPTS_PATH", str(CURRENT_PROMPTS_PATH))
os.environ["WHISPER_PROMPTS_PATH"] = str(CURRENT_PROMPTS_PATH)


def _set_macos_dock_icon() -> None:
    """Set a custom Dock icon for source runs (e.g. `uv run`) on macOS."""
    if sys.platform != "darwin":
        return

    try:
        from AppKit import NSApplication, NSImage
    except Exception:
        return

    env_icon = (os.environ.get("WHISPER_DOCK_ICON") or "").strip()
    candidate_paths: list[Path] = []
    if env_icon:
        candidate_paths.append(_resolve_path_setting(env_icon, APP_SUPPORT_DIR / "app_icon.icns", APP_SUPPORT_DIR))
    candidate_paths.extend(
        [
            APP_SUPPORT_DIR / "app_icon.icns",
            RUNTIME_BASE_DIR / "app_icon.icns",
            RUNTIME_BASE_DIR / "icon-windowed.icns",
            MY_DIR / "packaging" / "app_icon.icns",
        ]
    )

    app = NSApplication.sharedApplication()
    seen: set[Path] = set()
    for icon_path in candidate_paths:
        try:
            icon_resolved = icon_path.resolve()
        except Exception:
            icon_resolved = icon_path
        if icon_resolved in seen:
            continue
        seen.add(icon_resolved)
        if not icon_resolved.exists():
            continue
        try:
            image = NSImage.alloc().initWithContentsOfFile_(str(icon_resolved))
            if image and image.isValid():
                app.setApplicationIconImage_(image)
                return
        except Exception:
            continue


def _set_macos_menu_app_name() -> None:
    """Set macOS menu bar app name for source runs (avoid showing python/python3)."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle, NSProcessInfo
    except Exception:
        return

    desired = (os.environ.get("WHISPER_MENU_APP_NAME") or MENU_APP_NAME or APP_NAME).strip()
    if not desired:
        return

    try:
        NSProcessInfo.processInfo().setProcessName_(desired)
    except Exception:
        pass

    # Best-effort: also patch bundle display keys if mutable.
    try:
        bundle = NSBundle.mainBundle()
        info = bundle.infoDictionary() if bundle is not None else None
        if info is not None:
            info["CFBundleName"] = desired
            info["CFBundleDisplayName"] = desired
    except Exception:
        pass


def _load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_yaml_config(path: Path, warn_name: str = "") -> dict:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        if warn_name:
            print(f"⚠️ {warn_name} could not be read ({path.name}): {e}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_yaml_config(path: Path, payload: dict) -> None:
    data = payload if isinstance(payload, dict) else {}
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def _cfg_str(cfg: dict, key: str) -> str:
    val = cfg.get(key)
    if val is None:
        return ""
    return str(val).strip()


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    raw = _cfg_str(cfg, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_optional_word_limit(raw: Any) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _word_limit_to_entry_value(limit: Optional[int]) -> str:
    return "" if limit is None else str(limit)


def _normalize_provider_models(raw_models: Any) -> list[str]:
    if isinstance(raw_models, str):
        model = raw_models.strip()
        return [model] if model else []
    if not isinstance(raw_models, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_models:
        model = ""
        if isinstance(item, str):
            model = item.strip()
        elif isinstance(item, dict):
            model = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        out.append(model)
    return out


def _load_provider_api_key(data: dict, credentials_path: Path) -> str:
    api_key = str(data.get("api_key") or "").strip()
    if api_key:
        return api_key
    raw_path = str(data.get("api_key_file") or "").strip()
    if not raw_path:
        return ""
    key_path = Path(raw_path).expanduser()
    if not key_path.is_absolute():
        key_path = credentials_path.parent / key_path
    try:
        key_text = key_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as error:
        print(f"API key file could not be read ({key_path.name}): {type(error).__name__}")
        return ""

    # Support annotated key files as well as plain, single-line tokens.
    candidates = re.findall(r"\bsk-[A-Za-z0-9_-]+", key_text)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates and key_text and not any(char.isspace() for char in key_text):
        return key_text
    print(f"API key file is empty, invalid or contains multiple keys ({key_path.name})")
    return ""


def _load_provider_credentials(path: Path) -> dict:
    payload = _load_yaml_config(path, warn_name="Credentials YAML")
    if not payload:
        return {}
    providers_raw = payload.get("providers")
    if not isinstance(providers_raw, dict):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for provider, data in providers_raw.items():
        if not isinstance(data, dict):
            continue
        provider_key = str(provider or "").strip().lower()
        if not provider_key:
            continue
        api_url = str(data.get("api_url") or "").strip()
        api_key = _load_provider_api_key(data, path)
        models = _normalize_provider_models(data.get("models"))
        if not models:
            single_model = str(data.get("model") or "").strip()
            if single_model:
                models = [single_model]
        default_model = str(data.get("default_model") or "").strip()
        inline_key = str(data.get("api_key") or "").strip()
        out[provider_key] = {
            "api_url": api_url,
            "api_key": api_key,
            # Shown in the LLM window; the key itself is never displayed.
            "api_key_file": "" if inline_key else str(data.get("api_key_file") or "").strip(),
            "models": models,
            "default_model": default_model,
        }
    return out


def llm_local_runtime_available() -> bool:
    """True when llama-cpp-python (extra `local-llm`) is installed."""
    try:
        return importlib.util.find_spec("llama_cpp") is not None
    except Exception:
        return False


def normalize_llm_provider_key(name: str) -> str:
    """Config/credentials name -> provider key ("google" -> "gemini")."""
    key = (name or "").strip().lower()
    if key == "academiccloud":
        return "academic"
    preset = llm_providers.preset_for_credentials_name(key)
    return preset.key if preset else key


def _credentials_for_provider(provider_key: str, creds: dict) -> dict:
    entry = creds.get(provider_key)
    if isinstance(entry, dict):
        return entry
    preset = llm_providers.preset_for(provider_key)
    for alias in (preset.credentials_aliases if preset else ()):
        entry = creds.get(alias)
        if isinstance(entry, dict):
            return entry
    return {}


def provider_runtime_meta(provider_key: str, cfg: dict, creds: dict) -> dict[str, Any]:
    """Where a cloud provider's URL, key and models come from.

    Key order: macOS Keychain (entered in the settings window), then
    credentials.yaml (`api_key` / `api_key_file`), then the environment.
    """
    key = normalize_llm_provider_key(provider_key)
    preset = llm_providers.preset_for(key)
    entry = _credentials_for_provider(key, creds)
    api_url = str(entry.get("api_url") or "").strip()
    if not api_url and key == "custom":
        api_url = llm_providers.normalize_chat_url(_cfg_str(cfg, "CUSTOM_LLM_URL"))
    if not api_url and preset is not None:
        api_url = preset.api_url

    api_key = llm_providers.get_stored_key(key)
    key_source = "keychain" if api_key else ""
    if not api_key:
        api_key = str(entry.get("api_key") or "").strip()
        key_source = "credentials" if api_key else ""
    if not api_key:
        api_key = llm_providers.env_api_key(key)
        key_source = "env" if api_key else ""

    models = entry.get("models") if isinstance(entry.get("models"), list) else []
    return {
        "key": key,
        "kind": preset.kind if preset else llm_providers.KIND_OPENAI,
        "api_url": api_url,
        "api_key": api_key,
        "key_source": key_source,
        "key_file": str(entry.get("api_key_file") or "") if key_source == "credentials" else "",
        "models": [str(m).strip() for m in models if str(m).strip()],
        "default_model": str(entry.get("default_model") or "").strip() or (preset.default_model if preset else ""),
    }


def remembered_llm_model(cfg: dict, provider_key: str) -> str:
    by_provider = cfg.get("LLM_MODEL_BY_PROVIDER")
    if isinstance(by_provider, dict):
        value = str(by_provider.get(provider_key) or "").strip()
        if value:
            return value
    return ""


def choose_llm_model(meta: dict, preferred: str) -> str:
    models = meta.get("models") or []
    default_model = meta.get("default_model") or ""
    if preferred and (not models or preferred in models):
        return preferred
    if default_model and (not models or default_model in models):
        return default_model
    return models[0] if models else ""


def llm_runtime_env(meta: dict, model: str) -> dict[str, str]:
    """Environment variables post_processing reads for this provider."""
    if meta.get("kind") == llm_providers.KIND_ANTHROPIC:
        return {
            "LLM_PROVIDER": "anthropic",
            "LLM_PROVIDER_NAME": meta["key"],
            "ANTHROPIC_API_KEY": meta.get("api_key") or "",
            "ANTHROPIC_MODEL": model,
        }
    return {
        "LLM_PROVIDER": "openai",
        "LLM_PROVIDER_NAME": meta["key"],
        "OPENAI_BASE_URL": meta.get("api_url") or "",
        "OPENAI_API_KEY": meta.get("api_key") or "",
        "OPENAI_MODEL": model,
    }


def _apply_provider_credentials(cfg: dict, creds: dict) -> None:
    provider = _cfg_str(cfg, "POST_PROCESSING_PROVIDER").lower()
    if not provider:
        return

    provider_key = normalize_llm_provider_key(provider)
    provider_creds = creds.get(provider_key, {})
    configured_model = _cfg_str(cfg, "POST_PROCESSING_MODEL")

    # ollama stays on the native path and does not use API keys.
    if provider_key == "ollama":
        os.environ.setdefault("LLM_PROVIDER", "ollama")
        if configured_model:
            os.environ.setdefault("OLLAMA_MODEL", configured_model)
        ollama_url = _cfg_str(cfg, "OLLAMA_URL")
        if ollama_url:
            os.environ.setdefault("OLLAMA_URL", ollama_url)
        return

    # local uses llama.cpp bindings directly in-process.
    if provider_key == "local":
        os.environ.setdefault("LLM_PROVIDER", "local")
        local_default_from_creds = str(provider_creds.get("default_model") or "").strip() if isinstance(provider_creds, dict) else ""
        local_model_id = configured_model or _cfg_str(cfg, "LOCAL_MODEL_ID") or local_default_from_creds or default_local_model_id()
        if local_model_id:
            os.environ.setdefault("LOCAL_MODEL_ID", local_model_id)
        local_models_dir = _cfg_str(cfg, "LOCAL_MODELS_DIR")
        if local_models_dir:
            os.environ.setdefault("LOCAL_MODELS_DIR", local_models_dir)
        for key in ("LOCAL_CTX_SIZE", "LOCAL_THREADS", "LOCAL_GPU_LAYERS", "LOCAL_MAX_TOKENS", "LOCAL_TIMEOUT_SEC"):
            val = _cfg_str(cfg, key)
            if val:
                os.environ.setdefault(key, val)
        return

    if llm_providers.preset_for(provider_key) is not None or _credentials_for_provider(provider_key, creds):
        meta = provider_runtime_meta(provider_key, cfg, creds)
        model = choose_llm_model(meta, remembered_llm_model(cfg, provider_key) or configured_model)
        for env_key, value in llm_runtime_env(meta, model).items():
            if value:
                os.environ.setdefault(env_key, value)
        return

    # Fallback: allow direct env-style config values in config.json.
    if provider_key in {"openai", "openrouter", "google", "academic"}:
        os.environ.setdefault("LLM_PROVIDER", "openai")
    else:
        os.environ.setdefault("LLM_PROVIDER", provider_key)
    openai_base = _cfg_str(cfg, "OPENAI_BASE_URL")
    if openai_base:
        os.environ.setdefault("OPENAI_BASE_URL", openai_base)
    openai_model = configured_model or _cfg_str(cfg, "OPENAI_MODEL")
    if openai_model:
        os.environ.setdefault("OPENAI_MODEL", openai_model)


def _apply_config_env(cfg: dict, creds: dict) -> None:
    _apply_provider_credentials(cfg, creds)

    whisper_model_size = _cfg_str(cfg, "WHISPER_MODEL_SIZE").lower()
    if whisper_model_size in WHISPER_MODEL_CATALOG:
        os.environ.setdefault("WHISPER_MODEL_SIZE", whisper_model_size)
    whisper_lang = _cfg_str(cfg, "WHISPER_LANGUAGE")
    if whisper_lang:
        os.environ.setdefault("WHISPER_LANGUAGE", whisper_lang)
    whisper_args = _cfg_str(cfg, "WHISPER_ARGS")
    if whisper_args:
        os.environ.setdefault("WHISPER_ARGS", whisper_args)
    output_lang = _cfg_str(cfg, "LLM_OUTPUT_LANGUAGE")
    if output_lang:
        os.environ.setdefault("LLM_OUTPUT_LANGUAGE", output_lang)
    signature = _cfg_str(cfg, "LETTER_SIGNATURE_LINE")
    if signature:
        os.environ.setdefault("LETTER_SIGNATURE_LINE", signature)

    # Structured personal-data profile (see the personal-data dialog).
    for key in (
        "PROFILE_FIRST_NAME", "PROFILE_LAST_NAME", "PROFILE_STREET",
        "PROFILE_ZIP", "PROFILE_CITY", "PROFILE_COUNTRY",
        "PROFILE_EMAIL", "PROFILE_PHONE",
    ):
        val = _cfg_str(cfg, key)
        if val:
            os.environ.setdefault(key, val)
    # Migrate a legacy NORM_NAME into the structured first/last name fields.
    if not os.environ.get("PROFILE_FIRST_NAME") and not os.environ.get("PROFILE_LAST_NAME"):
        legacy = _cfg_str(cfg, "NORM_NAME").strip()
        if legacy:
            parts = legacy.split()
            os.environ.setdefault("PROFILE_FIRST_NAME", " ".join(parts[:-1]) or parts[0])
            if len(parts) > 1:
                os.environ.setdefault("PROFILE_LAST_NAME", parts[-1])

    # Keep local runtime settings available even if provider is switched at runtime.
    for key in (
        "LOCAL_MODELS_DIR",
        "LOCAL_CTX_SIZE",
        "LOCAL_THREADS",
        "LOCAL_GPU_LAYERS",
        "LOCAL_MAX_TOKENS",
        "LOCAL_TIMEOUT_SEC",
        "LLM_REQUEST_TIMEOUT_SEC",
        "CONTEXT_SUMMARIZE_TIMEOUT_SEC",
    ):
        val = _cfg_str(cfg, key)
        if val:
            os.environ.setdefault(key, val)


def _update_config_key(path: Path, key: str, value: str) -> None:
    cfg = _load_json_config(path)
    cfg[key] = value
    _save_json_config(path, cfg)


def _local_models_dir_path() -> Path:
    raw = (
        os.environ.get("LOCAL_MODELS_DIR")
        or _cfg_str(_CONFIG, "LOCAL_MODELS_DIR")
        or "models"
    ).strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (APP_SUPPORT_DIR / path)


def _normalize_local_model_registry_models(raw_models: Any) -> list[dict[str, Any]]:
    if isinstance(raw_models, str):
        raw_models = [raw_models]
    if not isinstance(raw_models, list):
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            model_id = item.strip()
            label = model_id
            filename = ""
            tier = ""
            default = False
            download_url = ""
            license_name = ""
            bundle = False
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            label = str(item.get("label") or model_id).strip()
            filename = str(item.get("filename") or item.get("file") or "").strip()
            tier = str(item.get("tier") or "").strip()
            default = bool(item.get("default", False))
            download_url = str(item.get("download_url") or item.get("url") or "").strip()
            license_name = str(item.get("license") or item.get("license_name") or "").strip()
            bundle = bool(item.get("bundle", False))
        else:
            continue

        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        out.append(
            {
                "id": model_id,
                "label": label or model_id,
                "filename": filename,
                "tier": tier,
                "default": default,
                "download_url": download_url,
                "license": license_name,
                "bundle": bundle,
            }
        )
    return out


def _load_local_models_registry_from_credentials(path: Path = CREDENTIALS_PATH) -> dict:
    payload = _load_yaml_config(path)
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        return {"models": []}
    local = providers.get("local")
    if not isinstance(local, dict):
        return {"models": []}
    models = _normalize_local_model_registry_models(local.get("models"))
    return {"models": models}


def _load_local_models_registry(path: Path = LOCAL_MODELS_REGISTRY_PATH) -> dict:
    creds_data = _load_local_models_registry_from_credentials(CREDENTIALS_PATH)
    creds_models = creds_data.get("models")
    if isinstance(creds_models, list) and creds_models:
        return {"models": creds_models}

    data = _load_json_config(path)
    if not isinstance(data, dict):
        data = {}
    data["models"] = _normalize_local_model_registry_models(data.get("models"))
    return data


def _seed_local_models_into_app_support() -> None:
    """Copy bundled local GGUF models into APP_SUPPORT_DIR if missing.

    For packaged .app runs we want local post-processing to work out-of-the-box.
    The model registry defines file names; if a target file is missing under the
    configured LOCAL_MODELS_DIR, copy it from bundled resource locations.
    """
    try:
        model_dir = _local_models_dir_path()
        model_dir.mkdir(parents=True, exist_ok=True)
        payload = _load_local_models_registry()
        items = payload.get("models") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "").strip()
            if not filename:
                continue
            dest = model_dir / filename
            if dest.exists():
                continue
            src_candidates = [
                RUNTIME_BASE_DIR / "models" / filename,
                MY_DIR / "models" / filename,
                RUNTIME_BASE_DIR / filename,
                MY_DIR / filename,
            ]
            for src in src_candidates:
                if not src.exists() or not src.is_file():
                    continue
                try:
                    shutil.copy2(src, dest)
                    print(f"📦 Local model seeded: {dest}")
                except Exception as e:
                    print(f"⚠️ Local model could not be copied ({src} -> {dest}): {e}")
                break
    except Exception as e:
        print(f"⚠️ Seeding local models failed: {e}")


def _derive_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    filename = Path(unquote(parsed.path or "")).name
    if filename:
        return filename
    query = parse_qs(parsed.query or "")
    for key in ("filename", "file", "name"):
        values = query.get(key) or []
        if values:
            candidate = Path(unquote(values[0])).name
            if candidate:
                return candidate
    return ""


def _slugify_model_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def _slugify_model_id_from_filename(filename: str) -> str:
    stem = Path(filename).name
    if stem.lower().endswith(".gguf"):
        stem = stem[:-5]
    return _slugify_model_id(stem)


def _registry_conflict_message(model_id: str, filename: str, path: Path = LOCAL_MODELS_REGISTRY_PATH) -> Optional[str]:
    payload = _load_local_models_registry(path)
    wanted_id = (model_id or "").strip().lower()
    wanted_filename = (filename or "").strip().lower()
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        existing_id = str(item.get("id") or "").strip().lower()
        existing_filename = str(item.get("filename") or "").strip().lower()
        if wanted_id and existing_id == wanted_id:
            return tr(f"Modell-ID existiert bereits: {model_id}", f"Model ID already exists: {model_id}")
        if wanted_filename and existing_filename == wanted_filename:
            return tr(f"Dateiname ist bereits registriert: {filename}", f"File name is already registered: {filename}")
    return None


def _registry_model_id_for_filename(filename: str, path: Path = LOCAL_MODELS_REGISTRY_PATH) -> Optional[str]:
    """Return the id of a registry model that already uses this .gguf filename,
    or None. Lets the downloader fetch a known/preset model's file without
    trying to add a duplicate catalog entry."""
    wanted = (filename or "").strip().lower()
    if not wanted:
        return None
    payload = _load_local_models_registry(path)
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("filename") or "").strip().lower() == wanted:
            return str(item.get("id") or "").strip() or None
    return None


def _append_local_model_registry_entry(
    model_id: str,
    filename: str,
    label: str,
    tier: str = "downloaded",
    path: Path = LOCAL_MODELS_REGISTRY_PATH,
) -> None:
    conflict = _registry_conflict_message(model_id, filename, path=path)
    if conflict:
        raise ValueError(conflict)
    model_entry = {
        "id": model_id,
        "label": label,
        "filename": filename,
        "tier": tier,
        "default": False,
        "download_url": "",
        "license": "",
        "bundle": False,
    }

    credentials_payload = _load_yaml_config(CREDENTIALS_PATH)
    providers = credentials_payload.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        credentials_payload["providers"] = providers
    local = providers.get("local")
    if not isinstance(local, dict):
        local = {}
        providers["local"] = local
    models_raw = local.get("models")
    if isinstance(models_raw, list):
        models = list(models_raw)
    elif isinstance(models_raw, str):
        models = [models_raw]
    else:
        models = []
    models.append(model_entry)
    local["models"] = models
    if not str(local.get("default_model") or "").strip():
        local["default_model"] = model_id
    _save_yaml_config(CREDENTIALS_PATH, credentials_payload)


def _set_local_default_model_in_credentials(model_id: str, path: Path = CREDENTIALS_PATH) -> None:
    wanted = str(model_id or "").strip()
    if not wanted:
        return

    payload = _load_yaml_config(path)
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        payload["providers"] = providers

    local = providers.get("local")
    if not isinstance(local, dict):
        local = {}
        providers["local"] = local

    local["default_model"] = wanted

    models = local.get("models")
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            if not item_id:
                continue
            item["default"] = (item_id == wanted)

    _save_yaml_config(path, payload)


from local_llm import available_local_models, default_local_model_id, is_local_model_downloaded

_CONFIG = _load_json_config(CONFIG_PATH)
_CREDENTIALS = _load_provider_credentials(CREDENTIALS_PATH)
_apply_config_env(_CONFIG, _CREDENTIALS)
# Unconfigured languages follow the UI language. post_processing reads
# WHISPER_LANGUAGE at call time to pick German or English spoken punctuation,
# so it must always hold the effective transcription language.
if not (os.environ.get("WHISPER_LANGUAGE") or "").strip():
    os.environ["WHISPER_LANGUAGE"] = ui_language()
if "LLM_OUTPUT_LANGUAGE" not in os.environ and "LLM_OUTPUT_LANGUAGE" not in _CONFIG:
    # An explicit empty value stays empty (= no output-language instruction).
    os.environ["LLM_OUTPUT_LANGUAGE"] = ui_language()
_seed_local_models_into_app_support()

from post_processing import (
    apply_spoken_instruction_to_text,
    get_auto_mode_labels,
    get_last_llm_status,
    is_llm_enabled,
    is_llm_mode,
    maybe_llm_refine,
    post_llm_cleanup,
    post_process,
    reload_prompt_modes,
    summarize_context_with_llm,
    startup_llm_selftest,
)

# ---- Configuration ---------------------------------------------------------

DEFAULT_TMP_DIR = APP_SUPPORT_DIR / "tmp"
TMP_DIR = _resolve_path_setting(os.environ.get("WHISPER_TMP_DIR", ""), DEFAULT_TMP_DIR, APP_SUPPORT_DIR)
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("WHISPER_TMP_DIR", str(TMP_DIR))


def _parse_int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _parse_float_setting(
    raw: Any,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _parse_cli_args(raw: str, default: str = "") -> list[str]:
    text = (raw or "").strip() or (default or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError as e:
        print(f"⚠️ Invalid CLI arguments '{text}': {e}")
        if default and text != default:
            try:
                return shlex.split(default)
            except ValueError:
                return []
        return []


# Retention for recordings: keep the newest N files OR everything younger than
# M days, whichever set is larger. 0 disables that axis.
MAX_RECORDING_COUNT = _parse_int_env("WHISPER_MAX_RECORDING_COUNT", 100, minimum=0)
MAX_RECORDING_AGE_DAYS = _parse_int_env("WHISPER_MAX_RECORDING_AGE_DAYS", 14, minimum=0)


def _resolve_model_file(filename: str) -> Path:
    explicit = (os.environ.get("WHISPER_MODEL_PATH") or "").strip()
    if explicit:
        return _resolve_path_setting(explicit, APP_SUPPORT_DIR / filename, APP_SUPPORT_DIR)

    for candidate in [
        APP_SUPPORT_DIR / filename,
        APP_SUPPORT_DIR / "models" / filename,
        RUNTIME_BASE_DIR / filename,
        MY_DIR / filename,
    ]:
        if candidate.exists():
            return candidate
    return APP_SUPPORT_DIR / filename


def _default_whisper_cmd() -> str:
    bundled = _resolve_existing_executable(
        [
            RUNTIME_BASE_DIR / "bin" / "whisper-cli",
            MY_DIR / "bin" / "whisper-cli",
        ]
    )
    if bundled:
        return bundled
    found = shutil.which("whisper-cli")
    if found:
        return found
    return "/opt/homebrew/bin/whisper-cli"


def _default_sox_cmd() -> str:
    bundled = _resolve_existing_executable(
        [
            RUNTIME_BASE_DIR / "bin" / "rec",
            RUNTIME_BASE_DIR / "bin" / "sox",
            MY_DIR / "bin" / "rec",
            MY_DIR / "bin" / "sox",
        ]
    )
    if bundled:
        return bundled
    found = shutil.which("rec")
    if found:
        return found
    found = shutil.which("sox")
    if found:
        return found
    return "rec"

# Whisper model configuration (catalog key → local file). Downloads land in
# WHISPER_MODELS_DOWNLOAD_DIR, which _resolve_model_file() already searches
# before the files bundled inside the .app.
WHISPER_MODELS_DOWNLOAD_DIR = APP_SUPPORT_DIR / "models"
MODEL_FILES = {key: _resolve_model_file(spec.filename) for key, spec in WHISPER_MODEL_CATALOG.items()}


def _whisper_model_installed(key: str) -> bool:
    path = MODEL_FILES.get(key)
    if path is None:
        return False
    if Path(path).is_file():
        return True
    # The file may have appeared since startup (e.g. a download that finished
    # after its window was closed) — re-resolve before reporting it missing.
    spec = WHISPER_MODEL_CATALOG.get(key)
    if spec is None:
        return False
    MODEL_FILES[key] = _resolve_model_file(spec.filename)
    return Path(MODEL_FILES[key]).is_file()


def _model_engine(model_path: str | Path) -> str:
    """Speech engine for a model file: catalog entry by filename, else whisper.cpp."""
    name = Path(model_path).name
    for spec in WHISPER_MODEL_CATALOG.values():
        if spec.filename == name:
            return spec.engine
    return "whistle" if name.endswith(".cact") else "whisper.cpp"


def _preload_whistle_async(model_path: str | Path) -> None:
    """Load Whistle in the background so the first dictation is instant."""
    if _model_engine(model_path) != ENGINE_WHISTLE or not Path(model_path).is_file():
        return

    def _worker() -> None:
        started = time.time()
        try:
            whistle_backend.preload(Path(model_path))
        except Exception as e:
            print(f"⚠️ Whistle could not be loaded: {e}")
            return
        print(f"⚡ Whistle ready ({time.time() - started:.1f}s)")

    threading.Thread(target=_worker, daemon=True).start()


def _prepare_speech_backend(model_path: str | Path) -> None:
    """Warm up whatever engine `model_path` needs; stop what it doesn't."""
    if _model_engine(model_path) == ENGINE_WHISTLE:
        WHISPER_SERVER.stop()
        _preload_whistle_async(model_path)
        return
    if WHISPER_SERVER_ENABLED and Path(model_path).is_file():
        WHISPER_SERVER.start(str(model_path), WHISPER_ARGS)
    else:
        WHISPER_SERVER.stop()


def _resolve_model_size_or_fallback(requested_size: str, *, warn: bool = True) -> str:
    requested = (requested_size or "").strip().lower()
    if requested not in MODEL_FILES:
        requested = "medium"
    requested_path = Path(MODEL_FILES[requested])
    if requested_path.exists():
        return requested

    for fallback in WHISPER_FALLBACK_ORDER:
        candidate = Path(MODEL_FILES[fallback])
        if candidate.exists():
            if warn:
                print(
                    f"⚠️ Whisper model '{requested}' is missing ({requested_path}). "
                    f"Falling back to '{fallback}' ({candidate})."
                )
            return fallback

    # Keep requested size if nothing exists; downstream diagnostics will still
    # report model=missing.
    return requested


DEFAULT_MODEL_SIZE = _resolve_model_size_or_fallback(os.environ.get("WHISPER_MODEL_SIZE", "medium"))
MODEL = str(MODEL_FILES[DEFAULT_MODEL_SIZE])
os.environ["WHISPER_MODEL_SIZE"] = DEFAULT_MODEL_SIZE
# Defaults to the UI language (see above); override with env/config WHISPER_LANGUAGE.
LANGUAGE = os.environ.get("WHISPER_LANGUAGE") or ui_language()
SOX_CMD = os.environ.get("SOX_CMD", _default_sox_cmd())
WHISPER_CMD = os.environ.get("WHISPER_CMD", _default_whisper_cmd())


def _command_available(cmd: str) -> bool:
    return bool(shutil.which(cmd)) or Path(cmd).is_file()


def missing_external_tools(model_engine: str) -> list[tuple[str, str]]:
    """(command, Homebrew package) pairs the current setup needs but cannot find."""
    missing: list[tuple[str, str]] = []
    if not _command_available(SOX_CMD):
        missing.append((SOX_CMD, "sox"))
    if model_engine != ENGINE_WHISTLE and not _command_available(WHISPER_CMD):
        missing.append((WHISPER_CMD, "whisper-cpp"))
    return missing


def missing_tools_message(missing: list[tuple[str, str]]) -> str:
    commands = ", ".join(f"`{Path(cmd).name}`" for cmd, _pkg in missing)
    packages = " ".join(dict.fromkeys(pkg for _cmd, pkg in missing))
    return tr(
        f"Nicht gefunden: {commands}. Installieren mit:\n\n    brew install {packages}\n\nDanach die App neu starten.",
        f"Not found: {commands}. Install with:\n\n    brew install {packages}\n\nThen restart the app.",
    )


def no_speech_model_message() -> str:
    return tr(
        "Kein Sprachmodell installiert. Im Menü „Whisper-Modell“ eines laden, "
        "empfohlen: large-v3-turbo.",
        "No speech model installed. Download one from the \"Whisper model\" menu; "
        "recommended: large-v3-turbo.",
    )
WHISPER_ARGS = _parse_cli_args(os.environ.get("WHISPER_ARGS", ""), default="-mc 0")
# Keep the whisper.cpp model loaded in a local whisper-server (same Homebrew
# formula as whisper-cli). WHISPER_SERVER=0 disables it; whisper-cli remains
# the fallback either way.
_whisper_server_raw = os.environ.get("WHISPER_SERVER")
if _whisper_server_raw is None:
    _whisper_server_raw = _cfg_str(_CONFIG, "WHISPER_SERVER")
WHISPER_SERVER_ENABLED = str(_whisper_server_raw or "1").strip().lower() in {"1", "true", "yes", "on"}
WHISPER_SERVER_CMD = os.environ.get("WHISPER_SERVER_CMD") or str(
    Path(WHISPER_CMD).with_name("whisper-server")
    if Path(WHISPER_CMD).with_name("whisper-server").is_file()
    else (shutil.which("whisper-server") or "whisper-server")
)
WHISPER_SERVER = WhisperServerManager(WHISPER_SERVER_CMD, APP_SUPPORT_DIR / "whisper-server.pid")
_whisper_tail_filter_raw = os.environ.get("WHISPER_TAIL_FILTER")
if _whisper_tail_filter_raw is None:
    _whisper_tail_filter_raw = _cfg_str(_CONFIG, "WHISPER_TAIL_FILTER")
WHISPER_TAIL_FILTER = str(_whisper_tail_filter_raw or "1").strip().lower() in {"1", "true", "yes", "on"}
_whisper_tail_gap_raw = os.environ.get("WHISPER_TAIL_GAP_SEC")
if _whisper_tail_gap_raw is None:
    _whisper_tail_gap_raw = _cfg_str(_CONFIG, "WHISPER_TAIL_GAP_SEC")
WHISPER_TAIL_GAP_SEC = _parse_float_setting(_whisper_tail_gap_raw, 8.0, minimum=1.0, maximum=60.0)
_whisper_tail_ratio_raw = os.environ.get("WHISPER_TAIL_START_RATIO")
if _whisper_tail_ratio_raw is None:
    _whisper_tail_ratio_raw = _cfg_str(_CONFIG, "WHISPER_TAIL_START_RATIO")
WHISPER_TAIL_START_RATIO = _parse_float_setting(_whisper_tail_ratio_raw, 0.55, minimum=0.0, maximum=1.0)
os.environ.setdefault("SOX_CMD", SOX_CMD)
os.environ.setdefault("WHISPER_CMD", WHISPER_CMD)
SAMPLE_ARGS = os.environ.get("SOX_ARGS", "-q -r 16000 -b 16 -c 1")  # mono 16kHz
AUDIO_INPUT_REFRESH_INTERVAL_MS = max(1000, min(60000, int(os.environ.get("AUDIO_INPUT_REFRESH_INTERVAL_MS", "5000"))))
DOUBLE_TAP_WINDOW = float(os.environ.get("DOUBLE_TAP_WINDOW", "0.6"))  # seconds
MAX_RECORDING_SECONDS = float(os.environ.get("MAX_RECORDING_SECONDS", "600"))  # auto-stop after 10 min by default
CTRL_X_DELETE_COUNT = int(
    os.environ.get(
        "CTRL_X_DELETE_COUNT",
        str(_cfg_int(_CONFIG, "CTRL_X_DELETE_COUNT", _cfg_int(_CONFIG, "BACKSPACE_BURST_DELETE_COUNT", 30))),
    )
)
NO_AI_MODE = -5
CLIPBOARD_INSTRUCTION_MODE = -10
CLIPBOARD_INSTRUCTION_LABEL = tr("Clipboard-Anweisung", "Clipboard instruction")
RESERVED_PROMPT_MODE_IDS = {0}


# Env wins, then config.json (the template starts new users on -5 "Post-processing without AI",
# since no LLM is set up yet), then mode 3 for older configs without the key.
_auto_correct_raw = os.environ.get("AUTO_CORRECT_MODE") or _cfg_str(_CONFIG, "AUTO_CORRECT_MODE") or "3"
try:
    AUTO_CORRECT_MODE = int(_auto_correct_raw)
except ValueError:
    AUTO_CORRECT_MODE = 3
os.environ["AUTO_CORRECT_MODE"] = str(AUTO_CORRECT_MODE)


AUTO_MODE_LABELS: dict[int, str] = {}


def _refresh_auto_mode_labels() -> dict[int, str]:
    global AUTO_MODE_LABELS
    labels = get_auto_mode_labels()
    # Built-in non-LLM modes: label them in the UI language.
    if 0 in labels:
        labels[0] = tr("Kein Post-Processing", "No post-processing")
    if NO_AI_MODE in labels:
        labels[NO_AI_MODE] = tr("Post-Processing ohne KI", "Post-processing without AI")
    for mode_key, label_value in list(labels.items()):
        if mode_key == CLIPBOARD_INSTRUCTION_MODE:
            continue
        if "clipboard" in str(label_value or "").strip().lower():
            labels.pop(mode_key, None)
    labels[CLIPBOARD_INSTRUCTION_MODE] = CLIPBOARD_INSTRUCTION_LABEL
    AUTO_MODE_LABELS = dict(sorted(labels.items(), key=lambda item: item[0]))
    return AUTO_MODE_LABELS


_refresh_auto_mode_labels()
CONTEXT_MAX_LENGTH = int(
    os.environ.get(
        "CONTEXT_MAX_LENGTH",
        str(_cfg_int(_CONFIG, "CONTEXT_MAX_LENGTH", 4000)),
    )
)
if CONTEXT_MAX_LENGTH < 200:
    CONTEXT_MAX_LENGTH = 200
POST_PROCESSING_CONTEXT = os.environ.get("POST_PROCESSING_CONTEXT", _cfg_str(_CONFIG, "POST_PROCESSING_CONTEXT"))
_auto_context_raw = os.environ.get("AUTO_CONTEXT_APPEND")
if _auto_context_raw is None:
    _auto_context_raw = _cfg_str(_CONFIG, "AUTO_CONTEXT_APPEND")
AUTO_CONTEXT_APPEND = str(_auto_context_raw or "0").strip().lower() in {"1", "true", "yes", "on"}
_context_processing_raw = os.environ.get("CONTEXT_PROCESSING_ENABLED")
if _context_processing_raw is None:
    _context_processing_raw = _cfg_str(_CONFIG, "CONTEXT_PROCESSING_ENABLED")
CONTEXT_PROCESSING_ENABLED = str(_context_processing_raw or "1").strip().lower() in {"1", "true", "yes", "on"}
WORD_LIMIT_MIN = _parse_optional_word_limit(
    os.environ.get("POST_PROCESSING_WORD_MIN", _cfg_str(_CONFIG, "POST_PROCESSING_WORD_MIN"))
)
WORD_LIMIT_MAX = _parse_optional_word_limit(
    os.environ.get("POST_PROCESSING_WORD_MAX", _cfg_str(_CONFIG, "POST_PROCESSING_WORD_MAX"))
)
if WORD_LIMIT_MIN is not None and WORD_LIMIT_MAX is not None and WORD_LIMIT_MAX <= WORD_LIMIT_MIN:
    print("⚠️ Invalid word limits (maximum <= minimum). Ignoring the maximum.")
    WORD_LIMIT_MAX = None
os.environ["POST_PROCESSING_CONTEXT"] = POST_PROCESSING_CONTEXT
os.environ["CONTEXT_MAX_LENGTH"] = str(CONTEXT_MAX_LENGTH)
os.environ["AUTO_CONTEXT_APPEND"] = "1" if AUTO_CONTEXT_APPEND else "0"
os.environ["CONTEXT_PROCESSING_ENABLED"] = "1" if CONTEXT_PROCESSING_ENABLED else "0"
os.environ["POST_PROCESSING_WORD_MIN"] = str(WORD_LIMIT_MIN if WORD_LIMIT_MIN is not None else -1)
os.environ["POST_PROCESSING_WORD_MAX"] = str(WORD_LIMIT_MAX if WORD_LIMIT_MAX is not None else -1)
CONTEXT_LOCK = threading.Lock()
CONTEXT_SUMMARIZE_LOCK = threading.Lock()
CONTEXT_SUMMARIZING_DIALOG = None

# Runtime-overridable canonical name (used for prompts/signature enforcement).
# Empty when no name is configured; name normalisation is then skipped.
CURRENT_NAME_CANONICAL = (os.environ.get("NORM_NAME") or "").strip()

# Detector created early so we can report availability
detector = get_default_detector(DOUBLE_TAP_WINDOW)


def _llm_used_for_mode(mode: int) -> bool:
    if mode == CLIPBOARD_INSTRUCTION_MODE:
        return is_llm_enabled()
    return is_llm_mode(mode) and is_llm_enabled()


PROJECT_URL = "https://github.com/hgrabows/whisper-dictate"


def manual_url() -> str:
    """User guide on GitHub in the interface language."""
    name = "manual.de.md" if ui_language() == "de" else "manual.md"
    return f"{PROJECT_URL}/blob/main/docs/{name}"


def open_url(url: str) -> None:
    try:
        subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ Could not open {url}: {e}")


def _ordered_mode_options() -> list[tuple[int, str]]:
    opts = []
    for special in (0, NO_AI_MODE, CLIPBOARD_INSTRUCTION_MODE):
        if special in AUTO_MODE_LABELS:
            opts.append((special, AUTO_MODE_LABELS[special]))
    for mode in sorted(m for m in AUTO_MODE_LABELS if m not in {0, NO_AI_MODE, CLIPBOARD_INSTRUCTION_MODE}):
        opts.append((mode, AUTO_MODE_LABELS[mode]))
    return opts


def _parse_auto_correct_arg(raw: str) -> int:
    try:
        mode = int(str(raw).strip())
    except Exception as e:
        raise argparse.ArgumentTypeError(str(e))
    return mode


def _llm_startup_status_summary() -> tuple[str, str, str, str]:
    selected_provider = (_cfg_str(_CONFIG, "POST_PROCESSING_PROVIDER") or "not set").strip().lower()
    runtime_provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    runtime_provider_name = (os.environ.get("LLM_PROVIDER_NAME") or runtime_provider or selected_provider).strip().lower()

    if runtime_provider == "local" or runtime_provider_name == "local":
        model = (
            os.environ.get("LOCAL_MODEL_ID")
            or os.environ.get("POST_PROCESSING_MODEL")
            or _cfg_str(_CONFIG, "LOCAL_MODEL_ID")
            or _cfg_str(_CONFIG, "POST_PROCESSING_MODEL")
            or "not set"
        )
        source = "local"
    elif runtime_provider == "ollama":
        model = os.environ.get("OLLAMA_MODEL") or _cfg_str(_CONFIG, "POST_PROCESSING_MODEL") or "not set"
        source = "ollama"
    elif runtime_provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL") or "not set"
        source = "anthropic"
    else:
        model = os.environ.get("OPENAI_MODEL") or _cfg_str(_CONFIG, "POST_PROCESSING_MODEL") or "not set"
        source = "credentials/openai-compatible"

    available_credential_providers = ",".join(sorted(_CREDENTIALS.keys())) if _CREDENTIALS else "-"
    return selected_provider, runtime_provider_name, str(model).strip(), available_credential_providers + f" | source={source}"


def _show_settings_gui() -> bool:
    env_raw = os.environ.get("SHOW_SETTINGS_GUI")
    if env_raw is not None:
        return str(env_raw).strip().lower() in {"1", "true", "yes", "on"}
    cfg_raw = _cfg_str(_CONFIG, "SHOW_SETTINGS_GUI")
    if not cfg_raw:
        return True
    return str(cfg_raw).strip().lower() in {"1", "true", "yes", "on"}


def get_name_canonical() -> str:
    first = (os.environ.get("PROFILE_FIRST_NAME") or "").strip()
    last = (os.environ.get("PROFILE_LAST_NAME") or "").strip()
    name = " ".join(x for x in (first, last) if x).strip()
    return name or CURRENT_NAME_CANONICAL


def _get_post_processing_context() -> str:
    with CONTEXT_LOCK:
        return POST_PROCESSING_CONTEXT or ""


def _set_post_processing_context(value: str, persist: bool = True) -> None:
    global POST_PROCESSING_CONTEXT
    normalized = str(value or "").replace("\r\n", "\n")
    with CONTEXT_LOCK:
        POST_PROCESSING_CONTEXT = normalized
    os.environ["POST_PROCESSING_CONTEXT"] = normalized
    if persist:
        _update_config_key(CONFIG_PATH, "POST_PROCESSING_CONTEXT", normalized)


def _is_auto_context_enabled() -> bool:
    return bool(AUTO_CONTEXT_APPEND)


def _set_auto_context_enabled(enabled: bool, persist: bool = True) -> None:
    global AUTO_CONTEXT_APPEND
    AUTO_CONTEXT_APPEND = bool(enabled)
    os.environ["AUTO_CONTEXT_APPEND"] = "1" if AUTO_CONTEXT_APPEND else "0"
    if persist:
        _update_config_key(CONFIG_PATH, "AUTO_CONTEXT_APPEND", 1 if AUTO_CONTEXT_APPEND else 0)


def _is_context_processing_enabled() -> bool:
    return bool(CONTEXT_PROCESSING_ENABLED)


def _set_context_processing_enabled(enabled: bool, persist: bool = True) -> None:
    global CONTEXT_PROCESSING_ENABLED
    CONTEXT_PROCESSING_ENABLED = bool(enabled)
    os.environ["CONTEXT_PROCESSING_ENABLED"] = "1" if CONTEXT_PROCESSING_ENABLED else "0"
    if persist:
        _update_config_key(CONFIG_PATH, "CONTEXT_PROCESSING_ENABLED", 1 if CONTEXT_PROCESSING_ENABLED else 0)


def _get_word_limits() -> tuple[Optional[int], Optional[int]]:
    return WORD_LIMIT_MIN, WORD_LIMIT_MAX


def _set_word_limits(min_words: Optional[int], max_words: Optional[int], persist: bool = True) -> tuple[bool, str]:
    global WORD_LIMIT_MIN, WORD_LIMIT_MAX

    min_norm = min_words if isinstance(min_words, int) and min_words > 0 else None
    max_norm = max_words if isinstance(max_words, int) and max_words > 0 else None

    if min_norm is not None and max_norm is not None and max_norm <= min_norm:
        return False, tr("Obergrenze muss größer als Untergrenze sein.", "The maximum must be greater than the minimum.")

    WORD_LIMIT_MIN = min_norm
    WORD_LIMIT_MAX = max_norm
    os.environ["POST_PROCESSING_WORD_MIN"] = str(min_norm if min_norm is not None else -1)
    os.environ["POST_PROCESSING_WORD_MAX"] = str(max_norm if max_norm is not None else -1)

    if persist:
        _update_config_key(CONFIG_PATH, "POST_PROCESSING_WORD_MIN", min_norm if min_norm is not None else -1)
        _update_config_key(CONFIG_PATH, "POST_PROCESSING_WORD_MAX", max_norm if max_norm is not None else -1)
    return True, ""


def _append_post_processing_output_to_context(output_text: str) -> None:
    if not _is_auto_context_enabled():
        return
    snippet = (output_text or "").strip()
    if not snippet:
        return
    current = _get_post_processing_context().strip()
    merged = snippet if not current else f"{current}\n\n{snippet}"
    _set_post_processing_context(merged, persist=True)


def _set_context_summarizing_dialog_visible(visible: bool) -> None:
    root = SETTINGS_GUI_ROOT
    if root is None:
        if visible:
            print("context summarizing")
        return

    def _toggle() -> None:
        global CONTEXT_SUMMARIZING_DIALOG
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            CONTEXT_SUMMARIZING_DIALOG = None
            return

        if visible:
            if CONTEXT_SUMMARIZING_DIALOG is not None:
                try:
                    if CONTEXT_SUMMARIZING_DIALOG.winfo_exists():
                        CONTEXT_SUMMARIZING_DIALOG.lift()
                        return
                except Exception:
                    CONTEXT_SUMMARIZING_DIALOG = None
            dlg = tk.Toplevel(root)
            CONTEXT_SUMMARIZING_DIALOG = dlg
            dlg.title("context summarizing")
            dlg.resizable(False, False)
            ttk.Label(dlg, text="context summarizing").grid(row=0, column=0, padx=20, pady=16)
            try:
                dlg.transient(root)
                dlg.grab_set()
            except Exception:
                pass
        else:
            if CONTEXT_SUMMARIZING_DIALOG is None:
                return
            try:
                if CONTEXT_SUMMARIZING_DIALOG.winfo_exists():
                    CONTEXT_SUMMARIZING_DIALOG.destroy()
            except Exception:
                pass
            CONTEXT_SUMMARIZING_DIALOG = None

    try:
        root.after(0, _toggle)
    except Exception:
        if visible:
            print("context summarizing")


def _notify_context_summarize_failure(message: str, interactive: bool = False) -> None:
    msg = tr(f"Kontext-Zusammenfassung fehlgeschlagen: {message}", f"Context summary failed: {message}")
    print(f"⚠️ {msg}")
    if not interactive:
        return
    root = SETTINGS_GUI_ROOT
    if root is None:
        return

    def _show_warning() -> None:
        try:
            from tkinter import messagebox
            messagebox.showwarning(tr("Kontext", "Context"), msg, parent=root)
        except Exception:
            pass

    try:
        root.after(0, _show_warning)
    except Exception:
        pass


def _context_summarize_reason_suffix() -> str:
    reason = (os.environ.get("CONTEXT_SUMMARIZE_LAST_ERROR") or "").strip()
    if not reason:
        return ""
    return tr(f" Grund: {reason}", f" Reason: {reason}")


def _summarize_context_now(
    name_canonical: Optional[str] = None,
    interactive: bool = False,
    target_max_length: Optional[int] = None,
    require_actual_summary: bool = False,
    accept_max_length: Optional[int] = None,
) -> str:
    effective_limit = CONTEXT_MAX_LENGTH
    if target_max_length is not None:
        try:
            requested = int(target_max_length)
        except Exception:
            requested = CONTEXT_MAX_LENGTH
        requested = max(1, requested)
        effective_limit = min(CONTEXT_MAX_LENGTH, requested)
    acceptance_limit = effective_limit
    if accept_max_length is not None:
        try:
            requested_accept = int(accept_max_length)
        except Exception:
            requested_accept = effective_limit
        requested_accept = max(1, requested_accept)
        acceptance_limit = min(CONTEXT_MAX_LENGTH, max(effective_limit, requested_accept))

    context = _get_post_processing_context()
    if not context:
        os.environ["POST_PROCESSING_CONTEXT"] = ""
        return ""
    if len(context) <= effective_limit:
        os.environ["POST_PROCESSING_CONTEXT"] = context
        return context

    canonical = (name_canonical or get_name_canonical() or "").strip()
    with CONTEXT_SUMMARIZE_LOCK:
        latest = _get_post_processing_context()
        if len(latest) <= effective_limit:
            os.environ["POST_PROCESSING_CONTEXT"] = latest
            return latest

        _set_context_summarizing_dialog_visible(True)
        try:
            summarized = summarize_context_with_llm(
                latest,
                effective_limit,
                name_canonical=canonical,
                allow_hard_truncate=not require_actual_summary,
            )
        finally:
            _set_context_summarizing_dialog_visible(False)

        cleaned = (summarized or "").strip()
        if not cleaned:
            cleaned = latest

        likely_prefix_cut = (
            len(latest) > effective_limit
            and len(cleaned) < len(latest)
            and latest.startswith(cleaned)
        )

        if require_actual_summary and (cleaned == latest or likely_prefix_cut):
            if cleaned == latest:
                _notify_context_summarize_failure(
                    tr(
                        "LLM hat keine verwertbare Zusammenfassung geliefert; Kontext bleibt unverändert.",
                        "The LLM returned no usable summary; context left unchanged.",
                    )
                    + _context_summarize_reason_suffix(),
                    interactive=interactive,
                )
            else:
                _notify_context_summarize_failure(
                    tr(
                        "LLM hat den Kontext vermutlich nur abgeschnitten statt zusammenzufassen; Kontext bleibt unverändert.",
                        "The LLM probably just truncated the context instead of summarizing it; context left unchanged.",
                    )
                    + _context_summarize_reason_suffix(),
                    interactive=interactive,
                )
            os.environ["POST_PROCESSING_CONTEXT"] = latest
            return latest

        if require_actual_summary and len(cleaned) > acceptance_limit:
            _notify_context_summarize_failure(
                tr(
                    "Zusammenfassung ist zu lang; Kontext bleibt unverändert.",
                    "The summary is too long; context left unchanged.",
                )
                + _context_summarize_reason_suffix(),
                interactive=interactive,
            )
            os.environ["POST_PROCESSING_CONTEXT"] = latest
            return latest

        if len(cleaned) > effective_limit:
            if cleaned == latest:
                _notify_context_summarize_failure(
                    tr(
                        "LLM hat keine verwertbare Zusammenfassung geliefert; Kontext wurde gekürzt.",
                        "The LLM returned no usable summary; context was truncated.",
                    )
                    + _context_summarize_reason_suffix(),
                    interactive=interactive,
                )
            cleaned = cleaned[:effective_limit].rstrip()
        elif cleaned == latest and len(latest) > effective_limit:
            _notify_context_summarize_failure(
                tr(
                    "LLM hat keine verwertbare Zusammenfassung geliefert; Kontext bleibt unverändert.",
                    "The LLM returned no usable summary; context left unchanged.",
                )
                + _context_summarize_reason_suffix(),
                interactive=interactive,
            )
        elif likely_prefix_cut:
            _notify_context_summarize_failure(
                tr(
                    "LLM hat den Kontext vermutlich nur abgeschnitten statt zusammenzufassen.",
                    "The LLM probably just truncated the context instead of summarizing it.",
                )
                + _context_summarize_reason_suffix(),
                interactive=interactive,
            )

        _set_post_processing_context(cleaned, persist=True)
        return cleaned


def _prepare_context_for_post_processing(name_canonical: Optional[str] = None) -> str:
    if not _is_context_processing_enabled():
        os.environ["POST_PROCESSING_CONTEXT"] = ""
        return ""
    return _summarize_context_now(name_canonical=name_canonical)


def _maybe_reexec_into_venv():
    """Prefer project .venv runtime to avoid missing optional modules."""
    if IS_FROZEN:
        return
    if os.environ.get("WHISPER_NO_REEXEC"):
        return

    venv_dir = MY_DIR / ".venv"
    venv_py = venv_dir / "bin" / "python"
    if not venv_py.exists():
        return

    try:
        if Path(sys.prefix).resolve() == venv_dir.resolve():
            return
    except Exception:
        if str(sys.prefix) == str(venv_dir):
            return

    missing = []
    for mod in ("tkinter", "Quartz", "llama_cpp"):
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    if not missing:
        return

    print(f"♻️ Re-exec in .venv (missing modules: {', '.join(missing)})", flush=True)
    os.environ["WHISPER_NO_REEXEC"] = "1"
    os.execv(str(venv_py), [str(venv_py), *sys.argv])


def play_start_sound():
    """Play a short 'start recording' tone."""
    subprocess.Popen(
        [AFPLAY, "/System/Library/Sounds/Pop.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def play_stop_sound():
    """Play a short 'stop recording' tone."""
    subprocess.Popen(
        [AFPLAY, "/System/Library/Sounds/Glass.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def _flatten_audio_profiler_items(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        out.append(payload)
        items = payload.get("_items")
        if isinstance(items, list):
            for item in items:
                out.extend(_flatten_audio_profiler_items(item))
    elif isinstance(payload, list):
        for item in payload:
            out.extend(_flatten_audio_profiler_items(item))
    return out


def _humanize_audio_transport(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    known = {
        "coreaudio_device_type_builtin": "Built-in",
        "coreaudio_device_type_bluetooth": "Bluetooth",
        "coreaudio_device_type_usb": "USB",
        "coreaudio_device_type_hdmi": "HDMI",
        "coreaudio_device_type_airplay": "AirPlay",
    }
    if raw in known:
        return known[raw]
    raw = raw.removeprefix("coreaudio_device_type_")
    raw = raw.replace("_", " ").strip()
    return raw.title() if raw else ""


def get_current_audio_input_info() -> dict[str, str]:
    unknown = tr("Unbekannt", "Unknown")
    fallback = {"name": unknown, "source": "", "transport": "", "label": unknown}
    if sys.platform != "darwin":
        return fallback
    try:
        raw = subprocess.check_output(
            ["system_profiler", "SPAudioDataType", "-json"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        payload = json.loads(raw)
    except Exception:
        return fallback

    devices = _flatten_audio_profiler_items(payload.get("SPAudioDataType", []))
    selected: Optional[dict[str, Any]] = None
    for item in devices:
        if str(item.get("coreaudio_default_audio_input_device") or "").strip().lower() == "spaudio_yes":
            selected = item
            break
    if selected is None:
        return fallback

    name = str(selected.get("_name") or "").strip() or tr("Unbekannt", "Unknown")
    source = str(selected.get("coreaudio_input_source") or "").strip()
    transport = _humanize_audio_transport(str(selected.get("coreaudio_device_transport") or ""))

    label = name
    if source and source.lower() != name.lower():
        label = f"{label} / {source}"
    if transport:
        label = f"{label} ({transport})"

    return {
        "name": name,
        "source": source,
        "transport": transport,
        "label": label,
    }


# ---- Recorder state --------------------------------------------------------
class Recorder:
    def __init__(self):
        self.proc = None
        self.wav_path = None
        self.lock = threading.Lock()
        self.timer = None
        self.started_at = None
        self.on_auto_stop = None

    def start(self):
        with self.lock:
            if self.proc is not None:
                return
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            fd, path = tempfile.mkstemp(prefix="whisper-", suffix=".wav", dir=str(TMP_DIR))
            os.close(fd)
            self.wav_path = path
            args = [SOX_CMD] + shlex.split(SAMPLE_ARGS) + [self.wav_path]
            try:
                self.proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                self.proc = None
                print(f"⚠️ {missing_tools_message([(SOX_CMD, 'sox')])}")
                return
            self.started_at = time.time()
            if MAX_RECORDING_SECONDS > 0:
                if self.timer:
                    self.timer.cancel()
                self.timer = threading.Timer(MAX_RECORDING_SECONDS, self._auto_timeout)
                self.timer.daemon = True
                self.timer.start()
            play_start_sound()  # 🔔 Play start tone
            print(f"🎙️  Recording started → {self.wav_path}")

    def stop(self):
        with self.lock:
            if self.proc is None:
                return None
            print("🛑 Stopping recording…")
            play_stop_sound()  # 🔔 Play stop tone
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                print("⚠️ SoX did not react to SIGINT within 1.5s — terminating.")
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    print("⚠️ SoX did not react to terminate either — sending SIGKILL.")
                    try:
                        self.proc.kill()
                    except Exception as e:
                        print(f"⚠️ SoX kill failed: {e}")
                except Exception as e:
                    print(f"⚠️ SoX terminate failed: {e}")
            except Exception as e:
                print(f"⚠️ Stopping the recording: unexpected error ({e}) — terminating.")
                try:
                    self.proc.terminate()
                except Exception:
                    pass

            self.proc = None
            self.started_at = None
            if self.timer:
                self.timer.cancel()
                self.timer = None
            path = self.wav_path
            self.wav_path = None
            return path

    def _auto_timeout(self):
        cb = self.on_auto_stop
        if cb:
            cb()


recorder = Recorder()

# Track last pasted text length for deletion
LAST_PASTED_LEN = 0
PASTE_DEDUP_WINDOW_MS = max(0, int(os.environ.get("PASTE_DEDUP_WINDOW_MS", "1200")))
_paste_restore_raw = os.environ.get("PASTE_RESTORE_CLIPBOARD")
if _paste_restore_raw is None:
    _paste_restore_raw = _cfg_str(_CONFIG, "PASTE_RESTORE_CLIPBOARD")
PASTE_RESTORE_CLIPBOARD = str(_paste_restore_raw or "1").strip().lower() in {"1", "true", "yes", "on"}
_paste_restore_delay_raw = os.environ.get("PASTE_CLIPBOARD_RESTORE_DELAY_MS")
if _paste_restore_delay_raw is None:
    _paste_restore_delay_raw = _cfg_str(_CONFIG, "PASTE_CLIPBOARD_RESTORE_DELAY_MS")
try:
    # 120ms proved too short in some apps; allow paste target more time to read NSPasteboard.
    PASTE_CLIPBOARD_RESTORE_DELAY_MS = max(0, int(str(_paste_restore_delay_raw or "900").strip()))
except (TypeError, ValueError):
    PASTE_CLIPBOARD_RESTORE_DELAY_MS = 900
os.environ["PASTE_RESTORE_CLIPBOARD"] = "1" if PASTE_RESTORE_CLIPBOARD else "0"
os.environ["PASTE_CLIPBOARD_RESTORE_DELAY_MS"] = str(PASTE_CLIPBOARD_RESTORE_DELAY_MS)
PASTE_LOCK = threading.Lock()
LAST_PASTE_TEXT = ""
LAST_PASTE_TS = 0.0
LAST_PASTE_SUPPRESSED_COUNT = 0
# Last fully processed dictation text — used by the "copy" button as a manual
# fallback when auto-paste landed nowhere useful (e.g. focus changed).
LAST_RECOGNIZED_TEXT = ""
# Last LLM rewrite snapshot for the GUI "show changes" popup.
LAST_LLM_CHANGE_LOCK = threading.Lock()
LAST_LLM_CHANGE: dict[str, Any] = {
    "timestamp": 0.0,
    "changed": False,
    "original": "",
    "refined": "",
}
# True while the current recording was started via the GUI "start recording"
# button. For that fallback flow the dictated text is left on the clipboard
# (no restore) so the user can paste it manually with Cmd+V.
_RECORDING_VIA_BUTTON = False
SETTINGS_GUI_ROOT = None
FOCUS_RESTORE_DELAY_MS = int(os.environ.get("FOCUS_RESTORE_DELAY_MS", "70"))
APP_PID = os.getpid()

# Absolute paths — a PyInstaller .app bundle does not reliably resolve bare
# command names via PATH, so subprocess calls must use the full path.
AFPLAY = "/usr/bin/afplay"
LAUNCHCTL = "/bin/launchctl"

# ---- Synthetic input + focus via Quartz/AppKit -----------------------------
# Keystrokes are synthesised with Quartz CGEvent and focus is handled with
# AppKit, instead of driving "System Events" through AppleEvents. CGEvent only
# needs the Accessibility grant the app already requires for hotkey detection;
# AppleEvents automation additionally needs an Automation grant that macOS will
# not prompt for in an unsigned, un-notarised bundle.

_VK_V = 9        # macOS virtual key codes
_VK_Z = 6
_VK_DELETE = 51
_VK_RETURN = 36


def _secure_input_active() -> Optional[bool]:
    """True if macOS Secure Keyboard Entry is enabled by some app. While active,
    macOS silently drops ALL synthetic keystrokes (paste, heartbeat) but allows
    physical input. None if the check itself fails."""
    try:
        import ctypes
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        return None


_CG_SOURCE = "uninitialised"


def _cg_source():
    """A private-state CGEvent source. Events created from it use an isolated
    modifier-flag table, so a physically held/stuck modifier (notably Fn after a
    fast double-tap, where macOS may coalesce the Fn key-up) cannot contaminate
    synthesised keystrokes. Without this, a stuck Fn turns a synthetic Cmd+V
    into Fn+Cmd+V and the paste silently does nothing."""
    global _CG_SOURCE
    if _CG_SOURCE == "uninitialised":
        try:
            from Quartz import CGEventSourceCreate, kCGEventSourceStatePrivate
            _CG_SOURCE = CGEventSourceCreate(kCGEventSourceStatePrivate)
        except Exception:
            _CG_SOURCE = None
    return _CG_SOURCE


def _cg_post_key(keycode: int, *, command: bool = False) -> bool:
    """Synthesise a single key press (down+up) via CGEvent."""
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
            kCGHIDEventTap, kCGEventFlagMaskCommand,
        )
    except Exception as e:
        print(f"⚠️ CGEvent not available: {e}")
        return False
    src = _cg_source()
    down = CGEventCreateKeyboardEvent(src, keycode, True)
    up = CGEventCreateKeyboardEvent(src, keycode, False)
    if down is None or up is None:
        return False
    # Set flags explicitly on every event — never inherit ambient modifier
    # state. command -> Command only; otherwise clear all modifiers.
    flags = kCGEventFlagMaskCommand if command else 0
    CGEventSetFlags(down, flags)
    CGEventSetFlags(up, flags)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)
    return True


def _cg_post_key_repeat(keycode: int, repeat: int, *, command: bool = False) -> bool:
    if repeat <= 0:
        return True
    for _ in range(repeat):
        if not _cg_post_key(keycode, command=command):
            return False
    return True


def _cg_keyboard_available() -> bool:
    try:
        from Quartz import CGEventCreateKeyboardEvent, CGEventPost  # noqa: F401
        return True
    except Exception:
        return False


def _appkit_pasteboard_available() -> bool:
    try:
        from AppKit import NSPasteboard  # noqa: F401
        return True
    except Exception:
        return False


def _appkit_focus_available() -> bool:
    try:
        from AppKit import NSWorkspace, NSRunningApplication  # noqa: F401
        return True
    except Exception:
        return False


def _cg_type_char(ch: str) -> bool:
    """Synthesise typing a single literal character via CGEvent."""
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
            CGEventPost, CGEventSetFlags, kCGHIDEventTap,
        )
    except Exception:
        return False
    src = _cg_source()
    down = CGEventCreateKeyboardEvent(src, 0, True)
    up = CGEventCreateKeyboardEvent(src, 0, False)
    if down is None or up is None:
        return False
    # Clear modifiers so a stuck Fn cannot turn the character into a chord.
    CGEventSetFlags(down, 0)
    CGEventSetFlags(up, 0)
    CGEventKeyboardSetUnicodeString(down, len(ch), ch)
    CGEventKeyboardSetUnicodeString(up, len(ch), ch)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)
    return True


def _update_last_llm_change(original: str, refined: str) -> None:
    original_text = str(original or "")
    refined_text = str(refined or "")
    changed = refined_text.strip() != original_text.strip()
    payload = {
        "timestamp": time.time(),
        "changed": changed,
        "original": original_text,
        "refined": refined_text,
    }
    with LAST_LLM_CHANGE_LOCK:
        LAST_LLM_CHANGE.update(payload)


def get_last_llm_change() -> dict[str, Any]:
    with LAST_LLM_CHANGE_LOCK:
        return dict(LAST_LLM_CHANGE)


def _set_clipboard(text: str) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))
    except Exception as e:
        print(f"⚠️ Setting the clipboard failed: {e}")
        return False


def _get_clipboard() -> Optional[str]:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
        pb = NSPasteboard.generalPasteboard()
        val = pb.stringForType_(NSPasteboardTypeString)
        return str(val) if val is not None else None
    except Exception:
        return None


def _capture_clipboard_snapshot() -> Optional[list[dict[str, bytes]]]:
    try:
        from AppKit import NSPasteboard
        pb = NSPasteboard.generalPasteboard()
        items = pb.pasteboardItems() or []
        snapshot: list[dict[str, bytes]] = []
        for item in items:
            entry: dict[str, bytes] = {}
            for pb_type in (item.types() or []):
                payload = item.dataForType_(pb_type)
                if payload is None:
                    continue
                entry[str(pb_type)] = bytes(payload)
            snapshot.append(entry)
        return snapshot
    except Exception:
        return None


def _restore_clipboard_snapshot(snapshot: list[dict[str, bytes]]) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardItem
        from Foundation import NSData
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        if not snapshot:
            return True
        items = []
        for entry in snapshot:
            item = NSPasteboardItem.alloc().init()
            has_data = False
            for pb_type, payload in entry.items():
                nsdata = NSData.dataWithBytes_length_(payload, len(payload))
                if item.setData_forType_(nsdata, pb_type):
                    has_data = True
            if has_data:
                items.append(item)
        if not items:
            return True
        return bool(pb.writeObjects_(items))
    except Exception:
        return False


@dataclass(frozen=True)
class FocusTarget:
    app_name: str
    pid: int
    window_title: str = ""


LAST_FOCUS_TARGET: Optional[FocusTarget] = None
LAST_FOCUS_TARGET_LOCK = threading.Lock()


def _set_last_focus_target(target: Optional[FocusTarget]) -> None:
    global LAST_FOCUS_TARGET
    if target is None:
        return
    if target.pid == APP_PID:
        return
    with LAST_FOCUS_TARGET_LOCK:
        LAST_FOCUS_TARGET = target


def _get_last_focus_target() -> Optional[FocusTarget]:
    with LAST_FOCUS_TARGET_LOCK:
        return LAST_FOCUS_TARGET


def _get_frontmost_focus_target() -> Optional[FocusTarget]:
    try:
        from AppKit import NSWorkspace
    except Exception:
        return None
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None
    if app is None:
        return None
    try:
        pid = int(app.processIdentifier())
    except Exception:
        return None
    app_name = str(app.localizedName() or app.bundleIdentifier() or "").strip()
    if not app_name:
        app_name = f"pid:{pid}"
    return FocusTarget(app_name=app_name, pid=pid, window_title="")


def _reactivate_focus_target(target: Optional[FocusTarget], delay_ms: int = 0) -> None:
    if not target:
        return
    if target.pid == APP_PID:
        return

    def _activate_target() -> None:
        if delay_ms > 0:
            time.sleep(max(0, delay_ms) / 1000.0)
        try:
            import AppKit
            running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(target.pid)
            if running is None:
                return
            options = int(getattr(AppKit, "NSApplicationActivateIgnoringOtherApps", 0))
            options |= int(getattr(AppKit, "NSApplicationActivateAllWindows", 0))
            running.activateWithOptions_(options)
        except Exception:
            pass

    threading.Thread(target=_activate_target, daemon=True).start()


def _hide_settings_gui_window() -> None:
    root = SETTINGS_GUI_ROOT
    if root is None:
        return

    front_target = _get_frontmost_focus_target()
    if front_target is not None and front_target.pid != APP_PID:
        _set_last_focus_target(front_target)
    else:
        # Do not fall back to a stale target (e.g. Finder) when Whisper itself
        # is frontmost. In that case we have no reliable external focus target.
        front_target = None

    def _hide():
        try:
            if root.winfo_exists():
                state = str(root.state())
                if state in {"withdrawn", "iconic"} or not root.winfo_viewable():
                    _reactivate_focus_target(front_target, delay_ms=FOCUS_RESTORE_DELAY_MS)
                    return
                # Keep a Dock handle on macOS so the app can be brought back to front.
                if sys.platform == "darwin":
                    root.iconify()
                else:
                    root.withdraw()
        except Exception:
            pass
        _reactivate_focus_target(front_target, delay_ms=FOCUS_RESTORE_DELAY_MS)

    try:
        root.after(0, _hide)
    except Exception:
        pass


# ---- Whisper STT -----------------------------------------------------------
WHISPER_SEGMENT_RE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})\]\s*(.*)$"
)


@dataclass
class WhisperSegment:
    start_sec: float
    end_sec: float
    text: str


def _hhmmss_ms_to_seconds(hh: str, mm: str, ss: str, ms: str) -> float:
    return (
        int(hh) * 3600
        + int(mm) * 60
        + int(ss)
        + int(ms) / 1000.0
    )


def _parse_whisper_segments(lines: list[str]) -> list[WhisperSegment]:
    segments: list[WhisperSegment] = []
    for line in lines:
        m = WHISPER_SEGMENT_RE.match(line.strip())
        if not m:
            continue
        start_sec = _hhmmss_ms_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end_sec = _hhmmss_ms_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        text = (m.group(9) or "").strip()
        if not text:
            continue
        segments.append(WhisperSegment(start_sec=start_sec, end_sec=end_sec, text=text))
    return segments


def _wav_duration_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if frames <= 0 or rate <= 0:
                return 0.0
            return frames / float(rate)
    except Exception:
        return 0.0


def _wav_peak_and_rms(path: str) -> tuple[float, float]:
    """Return normalized peak and RMS amplitude in [0, 1]."""
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            width = wf.getsampwidth()
            if not frames or width <= 0:
                return 0.0, 0.0
            peak = float(audioop.max(frames, width))
            rms = float(audioop.rms(frames, width))
            full_scale = float((1 << (8 * width - 1)) - 1)
            if full_scale <= 0:
                return 0.0, 0.0
            return peak / full_scale, rms / full_scale
    except Exception:
        return 0.0, 0.0


def _filter_suspicious_tail_segments(
    segments: list[WhisperSegment],
    audio_duration_sec: float,
) -> list[WhisperSegment]:
    if not WHISPER_TAIL_FILTER or len(segments) < 2:
        return segments

    first_large_gap_index: Optional[int] = None
    gap_sec = 0.0
    for idx in range(1, len(segments)):
        gap = segments[idx].start_sec - segments[idx - 1].end_sec
        if gap >= WHISPER_TAIL_GAP_SEC:
            first_large_gap_index = idx
            gap_sec = gap
            break

    if first_large_gap_index is None:
        return segments

    tail_start_sec = segments[first_large_gap_index].start_sec
    if audio_duration_sec > 0:
        late_threshold_sec = audio_duration_sec * WHISPER_TAIL_START_RATIO
        if tail_start_sec < late_threshold_sec:
            return segments

    kept = segments[:first_large_gap_index]
    dropped = segments[first_large_gap_index:]
    kept_text = " ".join(seg.text for seg in kept).strip()
    dropped_text = " ".join(seg.text for seg in dropped).strip()
    if len(kept_text) < 6 or not dropped_text:
        return segments

    print(
        f"🧹 Dropping suspicious Whisper tail after {gap_sec:.1f}s silence "
        f"(tail_start={tail_start_sec:.1f}s, audio={audio_duration_sec:.1f}s)."
    )
    print(f"🧹 Dropped tail text: {dropped_text}")
    return kept


def _join_filtered_segments(segments: list[WhisperSegment], wav_path: str) -> str:
    if not segments:
        return ""
    filtered = _filter_suspicious_tail_segments(segments, _wav_duration_seconds(wav_path))
    if not filtered:
        # Safety: never turn a non-empty Whisper decode into an empty
        # transcript solely due to filtering. Empty text means no paste.
        filtered = segments
    return " ".join(seg.text for seg in filtered).strip()


def _transcribe_whistle(path: str) -> str:
    print(f"⚡ Transcribing with Whistle: {path} (language={LANGUAGE})")
    started = time.time()
    try:
        text = _run_with_whisper_keyboard_heartbeat(
            lambda: whistle_backend.transcribe(path, Path(MODEL), LANGUAGE)
        )
    except Exception as e:
        print(f"Whistle error: {e}")
        return ""
    print(f"✅ Transcribed ({time.time() - started:.2f}s):", text)
    return text


def transcribe(path):
    if not path or not Path(path).exists():
        return ""
    if _model_engine(MODEL) == ENGINE_WHISTLE:
        return _transcribe_whistle(path)
    if WHISPER_SERVER_ENABLED:
        started = time.time()
        try:
            server_segments = _run_with_whisper_keyboard_heartbeat(
                lambda: WHISPER_SERVER.transcribe(path, MODEL, WHISPER_ARGS, LANGUAGE)
            )
        except WhisperServerUnavailable as e:
            print(f"ℹ️ {e} – using whisper-cli.")
            WHISPER_SERVER.restart_if_crashed()
        else:
            segments = [
                WhisperSegment(start_sec=s.start_sec, end_sec=s.end_sec, text=s.text)
                for s in server_segments
            ]
            text = _join_filtered_segments(segments, path)
            print(f"✅ Transcribed via whisper-server ({time.time() - started:.2f}s):", text)
            return text
    if not Path(MODEL).is_file():
        print(f"⚠️ {no_speech_model_message()}")
        return ""
    print(f"🧠 Transcribing with Whisper: {path} (language={LANGUAGE})")
    try:
        cmd = [WHISPER_CMD, "-m", MODEL]
        if WHISPER_ARGS:
            cmd += WHISPER_ARGS
        cmd += ["-f", path]
        if LANGUAGE:
            cmd += ["-l", LANGUAGE]
        out = _run_with_whisper_keyboard_heartbeat(
            lambda: subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        )
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        transcript_lines = [ln for ln in lines if ln.startswith('[')]
        segments = _parse_whisper_segments(transcript_lines)
        if segments:
            text = _join_filtered_segments(segments, path)
        elif transcript_lines:
            text = " ".join([ln.split('] ', 1)[-1] for ln in transcript_lines]).strip()
        else:
            text = ""
        print("✅ Transcribed:", text)
        return text
    except subprocess.CalledProcessError as e:
        print("Whisper error:\n", e.output)
        return ""
    except FileNotFoundError:
        print(f"⚠️ {missing_tools_message([(WHISPER_CMD, 'whisper-cpp')])}")
        return ""


def save_raw_transcript(wav_path: str, raw_text: str):
    """Persist the unprocessed recognition next to the WAV for debugging."""
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        base = Path(wav_path).stem
        out_path = TMP_DIR / f"{base}.raw.txt"
        out_path.write_text(raw_text or "", encoding="utf-8")
        print(f"💾 Raw transcript saved to {out_path}")
    except Exception as e:
        print(f"⚠️ Could not save raw transcript: {e}")


def list_recent_wavs(limit: int = 5):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    wavs = sorted(TMP_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    return wavs[:limit]


def prune_old_recordings(
    max_count: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> int:
    """Remove old recording artefacts (whisper-*.wav and matching .raw.txt).

    Retention policy: a file is kept if EITHER
      - it is among the newest `max_count` files (by mtime), OR
      - its mtime is within the last `max_age_days` days.
    A value of 0 for either knob disables that axis (0 count = keep none by
    that rule; 0 days = keep none by age). Returns the number of WAVs deleted.

    Cleanup failures are swallowed so a misbehaving filesystem can never
    take the dictation pipeline down.
    """
    count_limit = MAX_RECORDING_COUNT if max_count is None else max(0, max_count)
    age_limit_days = MAX_RECORDING_AGE_DAYS if max_age_days is None else max(0, max_age_days)

    # Both knobs at 0 → no retention at all (delete everything). Both unset
    # (huge values) → keep everything. We just compute the cutoff sets.
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        wavs = list(TMP_DIR.glob("whisper-*.wav"))
    except OSError:
        return 0
    if not wavs:
        return 0

    try:
        wavs_with_mtime = [(p, p.stat().st_mtime) for p in wavs]
    except OSError:
        return 0
    wavs_with_mtime.sort(key=lambda t: t[1], reverse=True)

    now = time.time()
    age_cutoff = now - age_limit_days * 86400.0 if age_limit_days > 0 else None

    keep: set[Path] = set()
    if count_limit > 0:
        keep.update(p for p, _ in wavs_with_mtime[:count_limit])
    if age_cutoff is not None:
        keep.update(p for p, mt in wavs_with_mtime if mt >= age_cutoff)

    deleted = 0
    for p, _ in wavs_with_mtime:
        if p in keep:
            continue
        # Delete the WAV and its companion .raw.txt (same stem).
        try:
            p.unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            print(f"⚠️ Could not delete old recording ({p.name}): {e}")
            continue
        raw_txt = p.parent / f"{p.stem}.raw.txt"
        try:
            raw_txt.unlink(missing_ok=True)
        except OSError:
            pass

    if deleted:
        print(
            f"🧹 Recording cleanup: removed {deleted} old WAV(s) "
            f"(limit: {count_limit} newest, {age_limit_days} days)."
        )
    return deleted


def debug_transcribe(paths):
    for p in paths:
        if not p.exists():
            print(f"❌ {p} not found")
            continue
        print(f"\n=== {p} ===")
        raw = transcribe(str(p))
        canonical = get_name_canonical()
        processed = raw if AUTO_CORRECT_MODE == 0 else post_process(raw, canonical)
        if _llm_used_for_mode(AUTO_CORRECT_MODE):
            _prepare_context_for_post_processing(canonical)
        refined = maybe_llm_refine(processed, canonical, AUTO_CORRECT_MODE)
        final_text = (
            processed
            if AUTO_CORRECT_MODE == 0
            else (post_llm_cleanup(refined, canonical, AUTO_CORRECT_MODE) if refined else post_llm_cleanup(processed, canonical, AUTO_CORRECT_MODE))
        )
        print(f"📝 Raw: {raw}")
        if AUTO_CORRECT_MODE == 0:
            print("🧽 Post-processed: (skipped; mode 0)")
        else:
            print(f"🧽 Post-processed: {processed}")
        if refined != processed:
            print(f"🧹 LLM refined: {refined}")
        else:
            print("🧹 LLM refined: (no change)")
        if final_text != (refined or processed):
            print(f"✨ Final after cleanup: {final_text}")


# Labels are resolved at import time, after set_ui_language() ran above.
PROFILE_FIELDS = [
    ("PROFILE_FIRST_NAME", tr("Vorname", "First name")),
    ("PROFILE_LAST_NAME", tr("Nachname", "Last name")),
    ("PROFILE_STREET", tr("Adresse (Straße, Nr.)", "Address (street, no.)")),
    ("PROFILE_ZIP", tr("Postleitzahl", "Postal code")),
    ("PROFILE_CITY", tr("Ort", "City")),
    ("PROFILE_COUNTRY", tr("Land", "Country")),
    ("PROFILE_EMAIL", tr("E-Mail", "Email")),
    ("PROFILE_PHONE", tr("Telefon", "Phone")),
]


def _show_profile_dialog(root) -> None:
    """Modal dialog to edit the stored personal data. Saved values drive the
    deterministic fuzzy correction of speech-to-text mishears and fill the
    letter contact block."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return

    dlg = tk.Toplevel(root)
    dlg.title(tr("WhisperDictate – Persönliche Daten", "WhisperDictate – Personal data"))
    dlg.resizable(False, False)
    try:
        dlg.transient(root)
        dlg.grab_set()
    except Exception:
        pass

    body = ttk.Frame(dlg, padding=16)
    body.grid(row=0, column=0, sticky="nsew")
    body.columnconfigure(1, weight=1)

    ttk.Label(
        body,
        text=tr(
            "Einmal hinterlegen — diese Daten korrigieren Diktier-Verhörer\n"
            "(Name, E-Mail, Ort …) automatisch und füllen den Kontaktblock\n"
            "im Brief-Modus.",
            "Enter once — this data automatically corrects misheard dictation\n"
            "(name, email, city …) and fills the contact block\n"
            "in letter mode.",
        ),
        justify="left",
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

    entries: dict = {}
    for i, (key, label) in enumerate(PROFILE_FIELDS):
        default = os.environ.get(key, "")
        if key == "PROFILE_COUNTRY" and not default:
            default = tr("Deutschland", "")
        var = tk.StringVar(value=default)
        entries[key] = var
        ttk.Label(body, text=f"{label}:").grid(
            row=i + 1, column=0, sticky="w", pady=3, padx=(0, 10)
        )
        ttk.Entry(body, textvariable=var, width=36).grid(
            row=i + 1, column=1, sticky="we", pady=3
        )

    status_var = tk.StringVar(value="")
    ttk.Label(body, textvariable=status_var, foreground="#777").grid(
        row=len(PROFILE_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0)
    )

    btns = ttk.Frame(body)
    btns.grid(row=len(PROFILE_FIELDS) + 2, column=0, columnspan=2, sticky="e", pady=(12, 0))

    def _on_save() -> None:
        for key, _label in PROFILE_FIELDS:
            val = (entries[key].get() or "").strip()
            if val:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
            try:
                _update_config_key(CONFIG_PATH, key, val)
            except Exception as e:
                status_var.set(tr(f"Speichern fehlgeschlagen: {e}", f"Saving failed: {e}"))
                return
        try:
            dlg.grab_release()
        except Exception:
            pass
        dlg.destroy()

    def _on_cancel() -> None:
        try:
            dlg.grab_release()
        except Exception:
            pass
        dlg.destroy()

    ttk.Button(btns, text=tr("Abbrechen", "Cancel"), command=_on_cancel).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(btns, text=tr("Speichern", "Save"), command=_on_save).grid(row=0, column=1)


def _show_onboarding_dialog(root) -> None:
    """Modal first-run dialog that walks the user through the four required
    macOS privacy permissions. Marks onboarding complete only when the user
    clicks 'Done'; 'Later' leaves the marker absent so it reappears next
    launch.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return

    dlg = tk.Toplevel(root)
    dlg.title(tr("WhisperDictate – Erste Einrichtung", "WhisperDictate – First-time setup"))
    dlg.resizable(False, False)
    try:
        dlg.transient(root)
        dlg.grab_set()
    except Exception:
        pass

    body = ttk.Frame(dlg, padding=16)
    body.grid(row=0, column=0, sticky="nsew")
    body.columnconfigure(0, weight=1)

    ttk.Label(
        body,
        text=tr("Willkommen bei WhisperDictate", "Welcome to WhisperDictate"),
        font=("Helvetica", 14, "bold"),
    ).grid(row=0, column=0, sticky="w", pady=(0, 6))

    ttk.Label(
        body,
        text=tr(
            "Damit Diktieren per Doppel-Fn funktioniert, braucht macOS drei "
            "Berechtigungen.\nKlicke auf die Buttons unten und aktiviere "
            "WhisperDictate in der jeweiligen Liste.",
            "For dictation via double-Fn to work, macOS needs three "
            "permissions.\nClick the buttons below and enable "
            "WhisperDictate in each list.",
        ),
        justify="left",
        wraplength=460,
    ).grid(row=1, column=0, sticky="w", pady=(0, 12))

    status_var = tk.StringVar(value="")

    def _format_status(value: Optional[bool]) -> str:
        if value is True:
            return tr("✅ erlaubt", "✅ allowed")
        if value is False:
            return tr("❌ noch nicht aktiviert", "❌ not enabled yet")
        return tr("❓ Status unbekannt", "❓ status unknown")

    def _format_input_status(value: Optional[bool]) -> str:
        # Input Monitoring checks can report false negatives on macOS even when
        # hotkeys already work, so avoid a hard red "not activated" status.
        if value is True:
            return tr("✅ erlaubt", "✅ allowed")
        return tr("⚠️ manuell prüfen", "⚠️ check manually")

    accessibility_status_var = tk.StringVar(value=_format_status(_accessibility_is_trusted()))
    input_status_var = tk.StringVar(value=_format_input_status(_input_monitoring_is_trusted()))
    # Was Accessibility already granted when the dialog opened? If it gets
    # granted *during* onboarding, the running event tap is stale and the app
    # must relaunch for it to take effect.
    _acc_ok_at_open = _accessibility_is_trusted() is True

    # Restart-after-grant flow. macOS applies a freshly granted permission only
    # to a NEW process (and often won't even report it live). We therefore arm
    # a watch when the user opens the Accessibility pane and offer a relaunch
    # once the app regains focus (back from System Settings) — plus an explicit
    # button as a reliable fallback.
    _restart_state = {"armed": False, "left": False}

    def _offer_restart() -> None:
        _restart_state["armed"] = False
        try:
            from tkinter import messagebox
            do_it = messagebox.askyesno(
                tr("Neu starten?", "Restart?"),
                tr(
                    "Damit die erteilten Berechtigungen wirken, muss WhisperDictate "
                    "einmal neu starten.\n\nJetzt neu starten?",
                    "WhisperDictate needs to restart once for the granted "
                    "permissions to take effect.\n\nRestart now?",
                ),
                parent=dlg,
            )
        except Exception:
            do_it = False
        if do_it:
            onboarding_mark_complete()
            if _relaunch_app():
                os._exit(0)

    def _open_pane(pane_name: str) -> None:
        if pane_name == "accessibility" and not _acc_ok_at_open:
            _restart_state["armed"] = True
            _restart_state["left"] = False
        open_system_settings_pane(pane_name)

    def _on_focus_out(_e=None) -> None:
        # Only count the dialog's own (app-level) focus loss, not child-widget
        # focus shifts inside the dialog — avoids a premature prompt.
        if _restart_state["armed"] and (_e is None or _e.widget is dlg):
            _restart_state["left"] = True

    def _on_focus_in(_e=None) -> None:
        # Returned from System Settings after opening the Accessibility pane.
        if _restart_state["armed"] and _restart_state["left"] and (_e is None or _e.widget is dlg):
            _offer_restart()

    def _refresh_status() -> None:
        accessibility_status_var.set(_format_status(_accessibility_is_trusted()))
        input_status_var.set(_format_input_status(_input_monitoring_is_trusted()))
        if not _acc_ok_at_open:
            status_var.set(tr(
                "Nach dem Erteilen: WhisperDictate neu starten — Knopf unten oder beim Zurückkommen.",
                "After granting: restart WhisperDictate — button below or when you come back.",
            ))
        else:
            status_var.set(tr("Status aktualisiert.", "Status updated."))

    perms_frame = ttk.Frame(body)
    perms_frame.grid(row=2, column=0, sticky="we")
    perms_frame.columnconfigure(1, weight=1)

    rows = [
        (tr("Mikrofon", "Microphone"), "microphone",
            tr("macOS fragt beim ersten Diktat (Fn+Fn) automatisch nach der Erlaubnis. Hier musst du nichts tun.",
               "macOS asks for permission automatically on the first dictation (Fn+Fn). Nothing to do here.")),
        (tr("Bedienungshilfen", "Accessibility"), "accessibility",
            tr("Für die Fn-Erkennung und das Einfügen von Text. Bitte manuell aktivieren — "
               "deckt zugleich die Eingabeüberwachung mit ab.",
               "For Fn detection and inserting text. Please enable manually — "
               "this also covers Input Monitoring.")),
        (tr("Eingabeüberwachung", "Input Monitoring"), "input",
            tr("Für die Fn-Erkennung. Wird durch die Bedienungshilfen mit abgedeckt — "
               "normalerweise keine separate Aktion nötig.",
               "For Fn detection. Covered by Accessibility — "
               "usually no separate action needed.")),
    ]
    for i, (label_text, pane, hint) in enumerate(rows):
        ttk.Label(perms_frame, text=label_text, font=("Helvetica", 12, "bold")).grid(
            row=i * 2, column=0, sticky="w", pady=(6, 0)
        )
        if pane == "accessibility":
            ttk.Label(perms_frame, textvariable=accessibility_status_var).grid(
                row=i * 2, column=1, sticky="e", pady=(6, 0)
            )
        elif pane == "input":
            ttk.Label(perms_frame, textvariable=input_status_var).grid(
                row=i * 2, column=1, sticky="e", pady=(6, 0)
            )
        ttk.Label(perms_frame, text=hint, foreground="#555", wraplength=320, justify="left").grid(
            row=i * 2 + 1, column=0, columnspan=1, sticky="w"
        )
        if pane == "microphone":
            ttk.Label(perms_frame, text=tr("✔ automatisch", "✔ automatic"), foreground="#777").grid(
                row=i * 2 + 1, column=1, sticky="e", padx=(8, 0)
            )
            continue
        if pane == "input":
            # Covered by Accessibility — the status above flips to ✅ once
            # Accessibility is granted. No button: the app does not need to
            # appear in the Input-Monitoring list separately.
            continue
        ttk.Button(
            perms_frame,
            text=tr("System-Einstellungen öffnen", "Open System Settings"),
            command=lambda p=pane: _open_pane(p),
        ).grid(row=i * 2 + 1, column=1, sticky="e", padx=(8, 0))

    # Auto-offer a restart when the app regains focus after the user visited the
    # Accessibility settings.
    dlg.bind("<FocusOut>", _on_focus_out)
    dlg.bind("<FocusIn>", _on_focus_in)

    ttk.Label(body, textvariable=status_var, foreground="#777").grid(
        row=3, column=0, sticky="w", pady=(12, 0)
    )

    btns = ttk.Frame(body)
    btns.grid(row=4, column=0, sticky="e", pady=(12, 0))

    if not _acc_ok_at_open:
        ttk.Button(btns, text=tr("Neu starten", "Restart"), command=_offer_restart).grid(
            row=0, column=3, padx=(8, 0)
        )

    def _on_done() -> None:
        onboarding_mark_complete()
        # If the app started WITHOUT Accessibility, a freshly granted permission
        # only takes effect in a new process — and macOS often doesn't even
        # report the grant live (so we can't detect it reliably). Therefore offer
        # the relaunch whenever the app launched ungranted, regardless of the
        # live status.
        if not _acc_ok_at_open:
            try:
                from tkinter import messagebox
                relaunch = messagebox.askyesno(
                    tr("Neu starten?", "Restart?"),
                    tr(
                        "Falls du gerade Bedienungshilfen (oder Eingabeüberwachung) erteilt "
                        "hast, muss WhisperDictate einmal neu starten, damit die Fn-Erkennung "
                        "wirkt.\n\nJetzt neu starten?",
                        "If you just granted Accessibility (or Input Monitoring), "
                        "WhisperDictate needs to restart once for Fn detection "
                        "to work.\n\nRestart now?",
                    ),
                    parent=dlg,
                )
            except Exception:
                relaunch = False
            if relaunch and _relaunch_app():
                os._exit(0)
        try:
            dlg.grab_release()
        except Exception:
            pass
        dlg.destroy()

    def _on_later() -> None:
        try:
            dlg.grab_release()
        except Exception:
            pass
        dlg.destroy()

    ttk.Button(btns, text=tr("Status neu prüfen", "Check status again"), command=_refresh_status).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(btns, text=tr("Später", "Later"), command=_on_later).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(btns, text=tr("Fertig", "Done"), command=_on_done).grid(row=0, column=2)

    dlg.protocol("WM_DELETE_WINDOW", _on_later)

    # Center on parent.
    try:
        dlg.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = root.winfo_y() + 80
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:
        pass

    try:
        dlg.wait_window()
    except Exception:
        pass


def run_mode_selector_gui(stop_event: threading.Event) -> bool:
    """Small GUI to select post-processing mode. Closing it exits the app."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as e:
        print(f"⚠️ GUI not available (tkinter import failed): {e}")
        return False

    global SETTINGS_GUI_ROOT

    _set_macos_menu_app_name()
    root = tk.Tk()
    _set_macos_dock_icon()
    SETTINGS_GUI_ROOT = root
    root.title("Whisper – Post-Processing")
    root.resizable(False, False)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    root.rowconfigure(1, weight=0)
    base_title = "Whisper – Post-Processing"
    show_settings_gui = _show_settings_gui()

    def show_settings_window() -> None:
        try:
            if not root.winfo_exists():
                return
            root.deiconify()
            root.lift()
            try:
                root.focus_force()
            except Exception:
                pass
        except Exception:
            pass

    try:
        # macOS: triggered when the user clicks the app icon in the Dock.
        root.createcommand("::tk::mac::ReopenApplication", lambda: (show_settings_window(), ""))
    except Exception:
        pass

    # Optional always-on-top recording overlay so active recording is impossible to miss.
    # Keep recording overlay opt-in for hidden GUI, but allow LLM error alerts
    # independently so users still see auth/connectivity failures.
    overlay_raw = os.environ.get("RECORDING_OVERLAY")
    recording_overlay_enabled = (
        show_settings_gui if overlay_raw is None
        else overlay_raw.strip().lower() in {"1", "true", "yes", "on"}
    )
    llm_alert_overlay_enabled = (os.environ.get("LLM_ALERT_OVERLAY", "1").strip().lower() in {"1", "true", "yes", "on"})
    llm_alert_duration_sec = _parse_float_setting(
        os.environ.get("LLM_ALERT_OVERLAY_DURATION_SEC"),
        default=6.0,
        minimum=1.5,
        maximum=20.0,
    )
    overlay_enabled = recording_overlay_enabled or llm_alert_overlay_enabled
    overlay = None
    overlay_label = None
    overlay_visible = False
    overlay_mode = "hidden"  # hidden | recording | llm_alert
    llm_alert_active_until = 0.0
    llm_alert_pending_text = ""
    llm_alert_pending_ts = 0.0
    llm_alert_last_enqueued_ts = 0.0
    llm_alert_last_shown_ts = 0.0
    if overlay_enabled:
        overlay = tk.Toplevel(root)
        overlay.withdraw()
        overlay.overrideredirect(True)
        try:
            # macOS: show overlay without taking keyboard focus from the active app.
            root.tk.call("::tk::unsupported::MacWindowStyle", "style", overlay._w, "help", "noActivates")
        except Exception:
            pass
        try:
            overlay.attributes("-topmost", True)
        except Exception:
            pass
        try:
            overlay.attributes("-alpha", 0.95)
        except Exception:
            pass
        overlay.configure(bg="#B00020")
        overlay_label = tk.Label(
            overlay,
            text=tr("● AUFNAHME AKTIV", "● RECORDING"),
            fg="white",
            bg="#B00020",
            font=("Helvetica", 16, "bold"),
            padx=20,
            pady=10,
            anchor="center",
            justify="center",
            wraplength=300,
        )
        overlay_label.pack()
        try:
            sw = root.winfo_screenwidth()
            width, height = 360, 54
            x = max(0, (sw - width) // 2)
            y = 18
            overlay.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    fallback_mode = 3 if 3 in AUTO_MODE_LABELS else (next(iter(sorted(AUTO_MODE_LABELS.keys()))) if AUTO_MODE_LABELS else 0)
    initial = AUTO_CORRECT_MODE if AUTO_CORRECT_MODE in AUTO_MODE_LABELS else fallback_mode
    mode_var = tk.IntVar(value=initial)
    input_lang_var = tk.StringVar(value=os.environ.get("WHISPER_LANGUAGE", LANGUAGE))
    output_lang_var = tk.StringVar(value=os.environ.get("LLM_OUTPUT_LANGUAGE", ""))
    audio_input_status_var = tk.StringVar(value=tr("Input: wird ermittelt…", "Input: detecting…"))
    llm_runtime_status_var = tk.StringVar(value=tr("LLM: bereit", "LLM: ready"))
    llm_runtime_status_cache_ts = 0.0
    llm_change_cache_ts = 0.0
    current_word_min, current_word_max = _get_word_limits()
    word_min_var = tk.StringVar(value=_word_limit_to_entry_value(current_word_min))
    word_max_var = tk.StringVar(value=_word_limit_to_entry_value(current_word_max))
    word_limits_status_var = tk.StringVar(value="")
    local_model_map: dict[str, str] = {}
    local_model_ids: list[str] = []
    credential_provider_map: dict[str, dict[str, Any]] = {}
    provider_choices: list[str] = []
    download_preset_map: dict[str, str] = {}
    download_preset_labels: list[str] = []

    def _refresh_local_model_catalog() -> None:
        local_model_map.clear()
        for item in available_local_models():
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            label = str(item.get("label") or model_id).strip()
            local_model_map[model_id] = label
        local_model_ids[:] = list(local_model_map.keys())

    def _refresh_download_presets() -> None:
        download_preset_map.clear()
        download_preset_labels.clear()
        payload = _load_local_models_registry()
        items = payload.get("models") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("download_url") or item.get("url") or "").strip()
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            model_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or item.get("id") or "").strip()
            filename = str(item.get("filename") or "").strip() or _derive_filename_from_url(url)
            if not label:
                label = filename or url
            present = bool(model_id) and is_local_model_downloaded(model_id)
            mark = "✓" if present else "⬇"
            base = f"{label} [{filename}]" if filename else label
            display = f"{mark} {base}"
            if display in download_preset_map:
                continue
            download_preset_map[display] = url
            download_preset_labels.append(display)

    def apply_download_preset(*_) -> None:
        selected = (download_preset_var.get() or "").strip()
        if not selected:
            return
        url = (download_preset_map.get(selected) or "").strip()
        if url:
            download_url_var.set(url)

    # Models a provider reported after "Save & test", kept for this session.
    fetched_provider_models: dict[str, list[str]] = {}
    provider_labels: dict[str, str] = {}

    def _refresh_credential_provider_catalog() -> None:
        """Cloud providers: the presets plus any extra provider in credentials.yaml."""
        credential_provider_map.clear()
        cfg = _load_json_config(CONFIG_PATH)
        keys = list(llm_providers.PROVIDER_PRESETS.keys())
        for name in sorted(_CREDENTIALS.keys()):
            key = normalize_llm_provider_key(name)
            if key not in ("local", "ollama") and key not in keys:
                keys.append(key)
        for key in keys:
            credential_provider_map[key] = provider_runtime_meta(key, cfg, _CREDENTIALS)

    def _provider_display_label(key: str) -> str:
        if key == "local":
            return tr("Lokal (auf diesem Mac)", "Local (on this Mac)")
        if key == "ollama":
            return "Ollama"
        preset = llm_providers.preset_for(key)
        if preset is not None:
            return preset.label
        return f"{key} (credentials.yaml)"

    def _refresh_provider_choices() -> None:
        choices = ["local"] + list(credential_provider_map.keys())
        if (os.environ.get("LLM_PROVIDER") or "").strip().lower() == "ollama":
            choices.append("ollama")
        provider_choices[:] = choices
        provider_labels.clear()
        for key in choices:
            provider_labels[key] = _provider_display_label(key)

    _refresh_local_model_catalog()
    _refresh_credential_provider_catalog()
    _refresh_provider_choices()

    configured_provider = normalize_llm_provider_key(
        os.environ.get("LLM_PROVIDER_NAME")
        or _cfg_str(_CONFIG, "POST_PROCESSING_PROVIDER")
        or "local"
    )
    if configured_provider not in provider_choices:
        configured_provider = "local"
    provider_select_var = tk.StringVar(value=configured_provider)
    provider_label_var = tk.StringVar(value=provider_labels.get(configured_provider, configured_provider))
    api_key_var = tk.StringVar(value="")
    api_status_var = tk.StringVar(value="")
    cloud_note_var = tk.StringVar(value="")
    custom_url_var = tk.StringVar(value=_cfg_str(_CONFIG, "CUSTOM_LLM_URL"))

    def _detect_current_whisper_model_size() -> str:
        current_model = Path(MODEL)
        for size_key, candidate in MODEL_FILES.items():
            try:
                if current_model.resolve() == Path(candidate).resolve():
                    return size_key
            except Exception:
                if str(current_model) == str(candidate):
                    return size_key
        env_size = (os.environ.get("WHISPER_MODEL_SIZE") or "").strip().lower()
        if env_size in MODEL_FILES:
            return _resolve_model_size_or_fallback(env_size, warn=False)
        return _resolve_model_size_or_fallback(DEFAULT_MODEL_SIZE, warn=False)

    local_creds_default_model = str((_CREDENTIALS.get("local") or {}).get("default_model") or "").strip() if isinstance(_CREDENTIALS, dict) else ""
    default_local_model = (
        os.environ.get("POST_PROCESSING_MODEL")
        or os.environ.get("LOCAL_MODEL_ID")
        or _cfg_str(_CONFIG, "POST_PROCESSING_MODEL")
        or _cfg_str(_CONFIG, "LOCAL_MODEL_ID")
        or local_creds_default_model
        or default_local_model_id()
    )
    if not default_local_model and local_model_ids:
        default_local_model = local_model_ids[0]
    model_select_var = tk.StringVar(value="")
    download_url_var = tk.StringVar(value="")
    download_preset_var = tk.StringVar(value="")
    download_name_var = tk.StringVar(value="")
    download_status_var = tk.StringVar(value="")
    context_status_var = tk.StringVar(value="")
    whisper_model_size_var = tk.StringVar(value=_detect_current_whisper_model_size())
    auto_context_var = tk.BooleanVar(value=_is_auto_context_enabled())
    context_processing_var = tk.BooleanVar(value=_is_context_processing_enabled())
    model_combo = None
    provider_combo = None
    cloud_frame = None
    local_frame = None
    custom_url_widgets: list[Any] = []
    test_button = None
    remove_key_button = None
    download_url_entry = None
    download_preset_combo = None
    download_name_entry = None
    download_button = None
    download_in_progress = False
    context_window = None
    context_text = None
    provider_window = None
    prompts_window = None
    prompts_listbox = None
    prompts_text_widget = None
    prompts_entries: dict[int, dict[str, str]] = {}
    prompts_selected_mode: Optional[int] = None
    prompts_mode_var = tk.StringVar(value="")
    prompts_label_var = tk.StringVar(value="")
    prompts_add_signature_var = tk.BooleanVar(value=False)
    prompts_skip_output_lang_var = tk.BooleanVar(value=False)
    prompts_status_var = tk.StringVar(value="")
    mode_options_frame = None
    close_button = None
    llm_change_button = None
    llm_change_window = None
    audio_input_refresh_in_progress = False

    def _normalize_prompt_entries(payload: Any) -> dict[int, dict[str, str]]:
        if not isinstance(payload, dict):
            return {}
        modes_raw = payload.get("modes", payload)
        if not isinstance(modes_raw, dict):
            return {}
        out: dict[int, dict[str, str]] = {}
        for key, value in modes_raw.items():
            try:
                mode = int(str(key).strip())
            except Exception:
                continue
            if mode <= 0 or mode in RESERVED_PROMPT_MODE_IDS:
                continue
            add_signature = False
            skip_output_language = False
            if isinstance(value, dict):
                label = str(value.get("label") or "").strip()
                prompt = str(value.get("prompt") or value.get("template") or "").strip()
                add_signature = bool(value.get("add_signature"))
                skip_output_language = bool(value.get("skip_output_language"))
            else:
                label = ""
                prompt = str(value or "").strip()
            if not label:
                label = f"Mode {mode}"
            if not prompt:
                continue
            out[mode] = {
                "label": label,
                "prompt": prompt,
                "add_signature": add_signature,
                "skip_output_language": skip_output_language,
            }
        return dict(sorted(out.items(), key=lambda item: item[0]))

    def _prompt_entries_to_payload(entries: dict[int, dict[str, str]]) -> dict:
        modes: dict[str, dict] = {}
        for mode, data in sorted(entries.items(), key=lambda item: item[0]):
            entry: dict = {
                "label": str(data.get("label") or "").strip() or f"Mode {mode}",
                "prompt": str(data.get("prompt") or "").strip(),
            }
            if data.get("add_signature"):
                entry["add_signature"] = True
            if data.get("skip_output_language"):
                entry["skip_output_language"] = True
            modes[str(mode)] = entry
        return {"modes": modes}

    def _load_current_prompt_entries() -> dict[int, dict[str, str]]:
        _copy_template_if_missing(
            CURRENT_PROMPTS_PATH,
            [PROMPTS_TEMPLATE_PATH] + _candidate_resource_paths("current_prompts.json") + _prompt_template_candidates(),
            fallback_text='{"modes": {}}\n',
        )
        payload = _load_json_config(CURRENT_PROMPTS_PATH)
        normalized = _normalize_prompt_entries(payload)
        if normalized:
            return normalized
        fallback = reload_prompt_modes(CURRENT_PROMPTS_PATH)
        return {
            int(mode): {
                "label": str(data.get("label") or "").strip() or f"Mode {mode}",
                "prompt": str(data.get("prompt") or "").strip(),
                "add_signature": bool(data.get("add_signature")),
                "skip_output_language": bool(data.get("skip_output_language")),
            }
            for mode, data in sorted(fallback.items(), key=lambda item: item[0])
            if str(data.get("prompt") or "").strip()
        }

    def _save_current_prompt_entries(entries: dict[int, dict[str, str]]) -> tuple[bool, str]:
        if not entries:
            return False, tr("Mindestens ein Prompt muss vorhanden sein.", "At least one prompt is required.")
        normalized: dict[int, dict[str, str]] = {}
        for mode, data in sorted(entries.items(), key=lambda item: item[0]):
            if mode <= 0:
                return False, tr("Prompt-ID muss > 0 sein.", "Prompt ID must be > 0.")
            if mode in RESERVED_PROMPT_MODE_IDS:
                return False, tr(f"Prompt-ID {mode} ist reserviert.", f"Prompt ID {mode} is reserved.")
            label = str(data.get("label") or "").strip()
            prompt = str(data.get("prompt") or "").strip()
            if not label:
                return False, tr(f"Prompt {mode}: Label fehlt.", f"Prompt {mode}: label is missing.")
            if not prompt:
                return False, tr(f"Prompt {mode}: Prompt-Text fehlt.", f"Prompt {mode}: prompt text is missing.")
            normalized[mode] = {
                "label": label,
                "prompt": prompt,
                "add_signature": bool(data.get("add_signature")),
                "skip_output_language": bool(data.get("skip_output_language")),
            }
        try:
            CURRENT_PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            CURRENT_PROMPTS_PATH.write_text(
                json.dumps(_prompt_entries_to_payload(normalized), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            return False, tr(f"Speichern fehlgeschlagen: {e}", f"Saving failed: {e}")
        return True, ""

    def _reload_prompt_modes_runtime() -> None:
        reload_prompt_modes(CURRENT_PROMPTS_PATH)
        _refresh_auto_mode_labels()
        _refresh_mode_options_ui()

    def _refresh_context_status() -> None:
        context_len = len(_get_post_processing_context())
        context_status_var.set(tr(
            f"Kontext: {context_len} Zeichen (max {CONTEXT_MAX_LENGTH})",
            f"Context: {context_len} characters (max {CONTEXT_MAX_LENGTH})",
        ))

    def _refresh_audio_input_status_async() -> None:
        nonlocal audio_input_refresh_in_progress
        if audio_input_refresh_in_progress:
            return
        audio_input_refresh_in_progress = True

        def _worker() -> None:
            info = get_current_audio_input_info()

            def _apply() -> None:
                nonlocal audio_input_refresh_in_progress
                audio_input_status_var.set(f"Input: {info['label']}")
                audio_input_refresh_in_progress = False
                try:
                    if root.winfo_exists():
                        root.after(AUDIO_INPUT_REFRESH_INTERVAL_MS, _refresh_audio_input_status_async)
                except Exception:
                    pass

            try:
                if root.winfo_exists():
                    root.after(0, _apply)
            except Exception:
                audio_input_refresh_in_progress = False

        threading.Thread(target=_worker, daemon=True).start()

    def apply_auto_context_toggle(*_) -> None:
        _set_auto_context_enabled(bool(auto_context_var.get()), persist=True)

    def apply_context_processing_toggle(*_) -> None:
        _set_context_processing_enabled(bool(context_processing_var.get()), persist=True)

    def open_context_window() -> None:
        nonlocal context_window, context_text
        if context_window is not None:
            try:
                if context_window.winfo_exists():
                    context_window.deiconify()
                    context_window.lift()
                    return
            except Exception:
                context_window = None
                context_text = None

        context_window = tk.Toplevel(root)
        context_window.title(tr("Kontext", "Context"))
        context_window.geometry("720x460")
        try:
            context_window.transient(root)
        except Exception:
            pass

        body = ttk.Frame(context_window, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        context_window.rowconfigure(0, weight=1)
        context_window.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        body.columnconfigure(0, weight=1)

        controls = ttk.Frame(body)
        controls.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(controls, textvariable=context_status_var).grid(row=0, column=0, sticky="w")

        ttk.Label(
            body,
            text=tr(
                f"Kontext für Post-Processing (automatische Zusammenfassung ab {CONTEXT_MAX_LENGTH} Zeichen):",
                f"Context for post-processing (summarized automatically above {CONTEXT_MAX_LENGTH} characters):",
            ),
        ).grid(row=1, column=0, sticky="w")

        text_frame = ttk.Frame(body)
        text_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 8))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        context_text = tk.Text(text_frame, wrap="word", width=90, height=20)
        context_text.grid(row=0, column=0, sticky="nsew")
        context_text.insert("1.0", _get_post_processing_context())
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=context_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        context_text.configure(yscrollcommand=scroll.set)

        def _save_context() -> None:
            if context_text is None:
                return
            value = context_text.get("1.0", "end-1c")
            _set_post_processing_context(value, persist=True)
            _refresh_context_status()

        summarize_button = None
        summarize_in_progress = False

        def _set_summarize_state(in_progress: bool) -> None:
            nonlocal summarize_in_progress
            summarize_in_progress = in_progress
            if summarize_button is not None:
                try:
                    summarize_button.configure(state="disabled" if in_progress else "normal")
                except Exception:
                    pass

        def _summarize_context_manual() -> None:
            if context_text is None or summarize_in_progress:
                return
            value = context_text.get("1.0", "end-1c")
            _set_post_processing_context(value, persist=True)
            _refresh_context_status()
            current_len = len(value)
            target_limit = min(CONTEXT_MAX_LENGTH, max(1, current_len // 2))
            acceptance_limit = min(CONTEXT_MAX_LENGTH, max(target_limit, int(current_len * 0.6)))
            _set_summarize_state(True)

            def _worker() -> None:
                try:
                    summarized = _summarize_context_now(
                        name_canonical=get_name_canonical(),
                        interactive=True,
                        target_max_length=target_limit,
                        accept_max_length=acceptance_limit,
                        require_actual_summary=True,
                    )
                except Exception:
                    summarized = _get_post_processing_context()

                def _apply_result() -> None:
                    if context_text is not None:
                        try:
                            context_text.delete("1.0", "end")
                            context_text.insert("1.0", summarized)
                        except Exception:
                            pass
                    _refresh_context_status()
                    _set_summarize_state(False)

                try:
                    root.after(0, _apply_result)
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        def _delete_context() -> None:
            if context_text is not None:
                try:
                    context_text.delete("1.0", "end")
                except Exception:
                    pass
            _set_post_processing_context("", persist=True)
            _refresh_context_status()

        def _close_context_window() -> None:
            nonlocal context_window, context_text
            try:
                if context_window is not None and context_window.winfo_exists():
                    context_window.destroy()
            except Exception:
                pass
            context_window = None
            context_text = None

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, sticky="e")
        summarize_button = ttk.Button(btns, text=tr("Zusammenfassen", "Summarize"), command=_summarize_context_manual)
        summarize_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text=tr("Speichern", "Save"), command=_save_context).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text=tr("Kontext löschen", "Delete context"), command=_delete_context).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(btns, text=tr("Schließen", "Close"), command=_close_context_window).grid(row=0, column=3)
        context_window.protocol("WM_DELETE_WINDOW", _close_context_window)

    _refresh_context_status()

    def _model_options_for_provider(provider_name: str) -> list[str]:
        provider = (provider_name or "").strip().lower()
        if provider == "local":
            # Only models whose .gguf file is actually present are selectable.
            return [mid for mid in local_model_ids if is_local_model_downloaded(mid)]
        if provider in fetched_provider_models:
            return list(fetched_provider_models[provider])
        meta = credential_provider_map.get(provider, {})
        models = list(meta.get("models") or [])
        for extra in (remembered_llm_model(_load_json_config(CONFIG_PATH), provider), meta.get("default_model")):
            if extra and extra not in models:
                models.append(extra)
        return models

    def _default_model_for_provider(provider_name: str) -> str:
        provider = (provider_name or "").strip().lower()
        configured_model = (_cfg_str(_CONFIG, "POST_PROCESSING_MODEL") or "").strip()
        if provider == "local":
            if default_local_model and default_local_model in local_model_ids:
                return default_local_model
            if configured_model and configured_model in local_model_ids:
                return configured_model
            return local_model_ids[0] if local_model_ids else ""
        meta = dict(credential_provider_map.get(provider, {}))
        meta["models"] = _model_options_for_provider(provider)
        return choose_llm_model(meta, remembered_llm_model(_load_json_config(CONFIG_PATH), provider))

    def _provider_model_text(provider_runtime_name: str, model: str) -> str:
        provider = (provider_runtime_name or "").strip().lower()
        selected_model = (model or "").strip()
        if not selected_model:
            return ""
        if provider == "local":
            label = local_model_map.get(selected_model)
            return f"{selected_model} - {label}" if label else selected_model
        return selected_model

    provider_var = tk.StringVar(value="")

    def _set_provider_info_text() -> None:
        provider = (provider_select_var.get() or "").strip().lower()
        label = provider_labels.get(provider, provider)
        model_text = _provider_model_text(provider, model_select_var.get())
        provider_var.set(label if not model_text else f"{label}: {model_text}")

    def _set_download_controls_enabled(enabled: bool) -> None:
        if download_button is not None:
            try:
                download_button.configure(state="normal" if enabled and not download_in_progress else "disabled")
            except Exception:
                pass
        if download_preset_combo is not None:
            try:
                download_preset_combo.configure(state="readonly" if enabled and not download_in_progress else "disabled")
            except Exception:
                pass
        if download_url_entry is not None:
            try:
                download_url_entry.configure(state="normal" if enabled and not download_in_progress else "disabled")
            except Exception:
                pass
        if download_name_entry is not None:
            try:
                download_name_entry.configure(state="normal" if enabled and not download_in_progress else "disabled")
            except Exception:
                pass

    def _set_download_status(message: str) -> None:
        download_status_var.set(message)

    def _safe_root_after(callback) -> None:
        try:
            root.after(0, callback)
        except Exception:
            pass

    def _refresh_model_widget(prefer_id: Optional[str] = None) -> None:
        _refresh_local_model_catalog()
        _refresh_download_presets()
        provider = (provider_select_var.get() or "").strip().lower()
        choices = _model_options_for_provider(provider)
        selected = (prefer_id or model_select_var.get() or "").strip()
        if not selected:
            selected = _default_model_for_provider(provider)
        if selected and choices and selected not in choices:
            selected = choices[0]
        if selected:
            model_select_var.set(selected)
        if model_combo is not None:
            try:
                model_combo.configure(values=choices)
            except Exception:
                pass
        if download_preset_combo is not None:
            try:
                download_preset_combo.configure(values=download_preset_labels)
            except Exception:
                pass
        if download_preset_labels:
            current_preset = (download_preset_var.get() or "").strip()
            if current_preset not in download_preset_map:
                current_preset = download_preset_labels[0]
                download_preset_var.set(current_preset)
            if not (download_url_var.get() or "").strip():
                preset_url = (download_preset_map.get(current_preset) or "").strip()
                if preset_url:
                    download_url_var.set(preset_url)
        else:
            download_preset_var.set("")
        _set_download_controls_enabled(provider == "local")
        _set_provider_info_text()

    def _apply_runtime_provider_model(provider_name: str, model_name: str) -> None:
        provider = (provider_name or "").strip().lower()
        model = (model_name or "").strip()
        os.environ["POST_PROCESSING_PROVIDER"] = provider
        if provider == "local":
            os.environ["LLM_PROVIDER"] = "local"
            os.environ["LLM_PROVIDER_NAME"] = "local"
            if model:
                os.environ["LOCAL_MODEL_ID"] = model
                os.environ["POST_PROCESSING_MODEL"] = model
            return
        meta = credential_provider_map.get(provider)
        if not meta:
            return
        model_to_use = model or _default_model_for_provider(provider)
        for env_key, value in llm_runtime_env(meta, model_to_use).items():
            os.environ[env_key] = value
        if model_to_use:
            os.environ["POST_PROCESSING_MODEL"] = model_to_use

    def apply_mode(*_):
        global AUTO_CORRECT_MODE
        try:
            AUTO_CORRECT_MODE = int(mode_var.get())
        except Exception:
            return
        os.environ["AUTO_CORRECT_MODE"] = str(AUTO_CORRECT_MODE)
        _update_config_key(CONFIG_PATH, "AUTO_CORRECT_MODE", str(AUTO_CORRECT_MODE))

    def _refresh_mode_options_ui() -> None:
        if mode_options_frame is None:
            return
        try:
            for child in mode_options_frame.winfo_children():
                child.destroy()
        except Exception:
            pass

        options = _ordered_mode_options()
        allowed_modes = [val for val, _label in options]
        current_mode = int(mode_var.get()) if str(mode_var.get()).strip() else 0
        if allowed_modes and current_mode not in allowed_modes:
            fallback_mode = 3 if 3 in allowed_modes else allowed_modes[0]
            mode_var.set(fallback_mode)
            apply_mode()

        for idx, (val, label) in enumerate(options):
            ttk.Radiobutton(
                mode_options_frame,
                text=label,
                value=val,
                variable=mode_var,
                command=apply_mode,
            ).grid(row=idx, column=0, sticky="w", pady=2)

    def apply_input_language(*_):
        global LANGUAGE
        val = (input_lang_var.get() or "").strip()
        LANGUAGE = val
        if val:
            os.environ["WHISPER_LANGUAGE"] = val
            _update_config_key(CONFIG_PATH, "WHISPER_LANGUAGE", val)
        else:
            os.environ.pop("WHISPER_LANGUAGE", None)
            _update_config_key(CONFIG_PATH, "WHISPER_LANGUAGE", "")

    def apply_output_language(*_):
        val = (output_lang_var.get() or "").strip()
        if val:
            os.environ["LLM_OUTPUT_LANGUAGE"] = val
            _update_config_key(CONFIG_PATH, "LLM_OUTPUT_LANGUAGE", val)
        else:
            os.environ.pop("LLM_OUTPUT_LANGUAGE", None)
            _update_config_key(CONFIG_PATH, "LLM_OUTPUT_LANGUAGE", "")

    whisper_download_state: dict[str, Any] = {"key": None, "cancel": None}
    whisper_download_status_var = tk.StringVar(value="")
    whisper_download_progress_var = tk.DoubleVar(value=0.0)
    whisper_models_window = None
    whisper_model_rows: dict[str, dict[str, Any]] = {}
    whisper_cancel_button = None

    def _whisper_menu_label(key: str) -> str:
        spec = WHISPER_MODEL_CATALOG[key]
        if _whisper_model_installed(key):
            return spec.label
        size = format_size(spec.size_bytes)
        return tr(f"{spec.label}  ⬇ {size} laden…", f"{spec.label}  ⬇ download {size}…")

    def _rebuild_whisper_model_menu() -> None:
        try:
            whisper_model_menu.delete(0, "end")
        except Exception:
            return
        for key in WHISPER_MODEL_CATALOG:
            whisper_model_menu.add_radiobutton(
                label=_whisper_menu_label(key),
                variable=whisper_model_size_var,
                value=key,
                command=apply_whisper_model_size,
            )
        whisper_model_menu.add_separator()
        whisper_model_menu.add_command(label=tr("Modelle verwalten…", "Manage models…"), command=open_whisper_models_window)

    def _whisper_model_removable(key: str) -> bool:
        path = Path(MODEL_FILES[key])
        if not path.is_file():
            return False
        try:
            return path.resolve().is_relative_to(APP_SUPPORT_DIR.resolve())
        except Exception:
            return False

    def _refresh_whisper_model_views() -> None:
        _rebuild_whisper_model_menu()
        active = _detect_current_whisper_model_size()
        busy = whisper_download_state["key"] is not None
        for key, row in whisper_model_rows.items():
            installed = _whisper_model_installed(key)
            if whisper_download_state["key"] == key:
                status = tr("lädt…", "downloading…")
            elif installed and key == active:
                status = tr("✓ aktiv", "✓ active")
            elif installed:
                status = tr("✓ installiert", "✓ installed")
            else:
                status = tr("nicht installiert", "not installed")
            try:
                row["status"].configure(text=status)
                row["download"].configure(
                    text=tr("Erneut laden", "Download again") if installed else tr("Herunterladen", "Download"),
                    state="disabled" if busy else "normal",
                )
                row["use"].configure(state="normal" if installed and key != active else "disabled")
                row["remove"].configure(
                    state="normal" if (not busy and key != active and _whisper_model_removable(key)) else "disabled"
                )
            except Exception:
                pass
        if whisper_cancel_button is not None:
            try:
                whisper_cancel_button.configure(state="normal" if busy else "disabled")
            except Exception:
                pass

    def _activate_whisper_model(key: str) -> None:
        whisper_model_size_var.set(key)
        apply_whisper_model_size()

    def start_whisper_model_download(
        key: str, *, activate_on_success: bool = False, warn_missing_tools: bool = True
    ) -> None:
        if whisper_download_state["key"] is not None:
            messagebox.showinfo(
                tr("Whisper-Modell", "Whisper model"),
                tr("Es läuft bereits ein Download.", "A download is already running."),
            )
            return
        spec = WHISPER_MODEL_CATALOG[key]
        if warn_missing_tools and spec.engine != ENGINE_WHISTLE and not _command_available(WHISPER_CMD):
            if not messagebox.askyesno(
                tr("Whisper-Modell", "Whisper model"),
                missing_tools_message([(WHISPER_CMD, "whisper-cpp")])
                + tr(
                    "\n\nModell trotzdem schon herunterladen?",
                    "\n\nDownload the model anyway?",
                ),
            ):
                return
        if _whisper_model_installed(key) and not messagebox.askyesno(
            tr("Whisper-Modell", "Whisper model"),
            tr(
                f"{spec.label} ist bereits installiert.\nTrotzdem neu herunterladen ({format_size(spec.size_bytes)})?",
                f"{spec.label} is already installed.\nDownload it again anyway ({format_size(spec.size_bytes)})?",
            ),
        ):
            return

        cancel_event = threading.Event()
        whisper_download_state["key"] = key
        whisper_download_state["cancel"] = cancel_event
        whisper_download_progress_var.set(0.0)
        whisper_download_status_var.set(tr(f"Download gestartet: {spec.label}", f"Download started: {spec.label}"))
        _refresh_whisper_model_views()

        last_ui_update = [0.0]

        def _on_progress(done: int, total: int) -> None:
            now = time.time()
            if total <= 0 or (now - last_ui_update[0]) < 0.3:
                return
            last_ui_update[0] = now
            pct = min(100.0, done * 100.0 / total)

            def _update(p=pct, d=done, t=total) -> None:
                whisper_download_progress_var.set(p)
                whisper_download_status_var.set(
                    f"{spec.label}: {p:.0f}% ({format_size(d)} / {format_size(t)})"
                )

            _safe_root_after(_update)

        def _worker() -> None:
            try:
                path = download_whisper_model(
                    spec,
                    WHISPER_MODELS_DOWNLOAD_DIR,
                    progress_cb=_on_progress,
                    cancel_event=cancel_event,
                )
            except DownloadCancelled:
                outcome, detail = "cancelled", ""
            except Exception as e:
                outcome, detail = "error", str(e)
            else:
                outcome, detail = "ok", str(path)

            def _finish() -> None:
                whisper_download_state["key"] = None
                whisper_download_state["cancel"] = None
                if outcome == "ok":
                    MODEL_FILES[key] = Path(detail)
                    whisper_download_progress_var.set(100.0)
                    whisper_download_status_var.set(tr(
                        f"Fertig: {spec.label} installiert und geprüft.",
                        f"Done: {spec.label} installed and verified.",
                    ))
                    print(f"✅ Whisper model downloaded: {key} -> {detail}")
                    if activate_on_success:
                        _activate_whisper_model(key)
                elif outcome == "cancelled":
                    whisper_download_progress_var.set(0.0)
                    whisper_download_status_var.set(tr(f"Download abgebrochen: {spec.label}", f"Download cancelled: {spec.label}"))
                else:
                    whisper_download_progress_var.set(0.0)
                    whisper_download_status_var.set(tr(f"Download fehlgeschlagen: {detail}", f"Download failed: {detail}"))
                    print(f"⚠️ Whisper model download failed ({key}): {detail}")
                _refresh_whisper_model_views()

            _safe_root_after(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def cancel_whisper_model_download() -> None:
        cancel_event = whisper_download_state.get("cancel")
        if cancel_event is not None:
            cancel_event.set()
            whisper_download_status_var.set(tr("Download wird abgebrochen…", "Cancelling download…"))

    def remove_whisper_model(key: str) -> None:
        spec = WHISPER_MODEL_CATALOG[key]
        if key == _detect_current_whisper_model_size():
            messagebox.showinfo(
                tr("Whisper-Modell", "Whisper model"),
                tr(
                    "Das aktive Modell kann nicht entfernt werden. Bitte erst ein anderes wählen.",
                    "The active model cannot be removed. Please select another one first.",
                ),
            )
            return
        if not _whisper_model_removable(key):
            return
        path = Path(MODEL_FILES[key])
        if not messagebox.askyesno(
            tr("Whisper-Modell entfernen", "Remove Whisper model"),
            tr(
                f"{spec.label} löschen?\n{path}\n\nGibt {format_size(path.stat().st_size)} frei. Kann jederzeit neu geladen werden.",
                f"Delete {spec.label}?\n{path}\n\nFrees {format_size(path.stat().st_size)}. Can be downloaded again at any time.",
            ),
        ):
            return
        try:
            path.unlink()
        except Exception as e:
            whisper_download_status_var.set(tr(f"Entfernen fehlgeschlagen: {e}", f"Removing failed: {e}"))
            return
        MODEL_FILES[key] = _resolve_model_file(spec.filename)
        whisper_download_status_var.set(tr(f"Entfernt: {spec.label}", f"Removed: {spec.label}"))
        _refresh_whisper_model_views()

    def open_whisper_models_window() -> None:
        nonlocal whisper_models_window, whisper_cancel_button
        if whisper_models_window is not None:
            try:
                if whisper_models_window.winfo_exists():
                    whisper_models_window.deiconify()
                    whisper_models_window.lift()
                    _refresh_whisper_model_views()
                    return
            except Exception:
                pass

        win = tk.Toplevel(root)
        whisper_models_window = win
        win.title(tr("Whisper-Modelle", "Whisper models"))
        try:
            win.transient(root)
        except Exception:
            pass

        body = ttk.Frame(win, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        whisper_model_rows.clear()
        for row_idx, (key, spec) in enumerate(WHISPER_MODEL_CATALOG.items()):
            frame = ttk.Frame(body, padding=(0, 4))
            frame.grid(row=row_idx, column=0, sticky="ew")
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=spec.label, font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, sticky="w")
            ttk.Label(frame, text=f"{spec.description} · {format_size(spec.size_bytes)}").grid(
                row=1, column=0, sticky="w"
            )
            status_label = ttk.Label(frame, text="", width=16)
            status_label.grid(row=0, column=1, rowspan=2, padx=(8, 4))
            download_btn = ttk.Button(
                frame, text=tr("Herunterladen", "Download"), command=lambda k=key: start_whisper_model_download(k)
            )
            download_btn.grid(row=0, column=2, rowspan=2, padx=2)
            use_btn = ttk.Button(frame, text=tr("Verwenden", "Use"), command=lambda k=key: _activate_whisper_model(k))
            use_btn.grid(row=0, column=3, rowspan=2, padx=2)
            remove_btn = ttk.Button(frame, text=tr("Entfernen", "Remove"), command=lambda k=key: remove_whisper_model(k))
            remove_btn.grid(row=0, column=4, rowspan=2, padx=2)
            whisper_model_rows[key] = {
                "status": status_label,
                "download": download_btn,
                "use": use_btn,
                "remove": remove_btn,
            }

        footer = ttk.Frame(body, padding=(0, 10, 0, 0))
        footer.grid(row=len(WHISPER_MODEL_CATALOG), column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Progressbar(
            footer, variable=whisper_download_progress_var, maximum=100.0, mode="determinate"
        ).grid(row=0, column=0, sticky="ew")
        whisper_cancel_button = ttk.Button(footer, text=tr("Abbrechen", "Cancel"), command=cancel_whisper_model_download)
        whisper_cancel_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(footer, textvariable=whisper_download_status_var, wraplength=560).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            footer,
            text=tr(f"Speicherort: {WHISPER_MODELS_DOWNLOAD_DIR}", f"Location: {WHISPER_MODELS_DOWNLOAD_DIR}"),
            foreground="gray",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def _on_close() -> None:
            nonlocal whisper_models_window, whisper_cancel_button
            whisper_model_rows.clear()
            whisper_cancel_button = None
            whisper_models_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        _refresh_whisper_model_views()

    def apply_whisper_model_size(*_) -> None:
        global MODEL
        requested_size = (whisper_model_size_var.get() or "").strip().lower()
        if requested_size in WHISPER_MODEL_CATALOG and not _whisper_model_installed(requested_size):
            # Radiobutton already moved; snap back to the model actually in use
            # until the requested one is downloaded.
            whisper_model_size_var.set(_detect_current_whisper_model_size())
            spec = WHISPER_MODEL_CATALOG[requested_size]
            if messagebox.askyesno(
                tr("Whisper-Modell herunterladen", "Download Whisper model"),
                tr(
                    f"{spec.label} ist noch nicht installiert.\n\n"
                    f"{spec.description}\nGröße: {format_size(spec.size_bytes)}\n\n"
                    "Jetzt herunterladen und danach aktivieren?",
                    f"{spec.label} is not installed yet.\n\n"
                    f"{spec.description}\nSize: {format_size(spec.size_bytes)}\n\n"
                    "Download it now and activate it afterwards?",
                ),
            ):
                open_whisper_models_window()
                start_whisper_model_download(requested_size, activate_on_success=True)
            return
        effective_size = _resolve_model_size_or_fallback(requested_size)
        if effective_size != requested_size:
            whisper_model_size_var.set(effective_size)
        model_path = MODEL_FILES[effective_size]
        MODEL = str(model_path)
        os.environ["WHISPER_MODEL_SIZE"] = effective_size
        _prepare_speech_backend(MODEL)
        _update_config_key(CONFIG_PATH, "WHISPER_MODEL_SIZE", effective_size)
        print(
            f"🧠 Whisper model selected via GUI: requested={requested_size or '(empty)'} "
            f"effective={effective_size} path={MODEL} exists={'yes' if Path(MODEL).exists() else 'no'}"
        )
        if whisper_model_rows:
            active_label = WHISPER_MODEL_CATALOG[effective_size].label
            whisper_download_status_var.set(tr(f"Aktiv: {active_label}", f"Active: {active_label}"))
        _refresh_whisper_model_views()

    def _parse_word_limit_input(raw: str) -> tuple[bool, Optional[int]]:
        text = (raw or "").strip()
        if not text:
            return True, None
        try:
            value = int(text)
        except Exception:
            return False, None
        if value in {0, -1}:
            return True, None
        if value < 0:
            return False, None
        if value < 1:
            return False, None
        return True, value

    def apply_word_limits(*_) -> None:
        min_ok, min_words = _parse_word_limit_input(word_min_var.get())
        max_ok, max_words = _parse_word_limit_input(word_max_var.get())
        if not min_ok or not max_ok:
            word_limits_status_var.set(tr(
                "Wortgrenzen: ganzzahlige Werte >=1 oder leer/0/-1.",
                "Word limits: whole numbers >= 1, or empty/0/-1.",
            ))
            return
        ok, msg = _set_word_limits(min_words, max_words, persist=True)
        if not ok:
            word_limits_status_var.set(msg)
            return
        word_min_var.set(_word_limit_to_entry_value(min_words))
        word_max_var.set(_word_limit_to_entry_value(max_words))
        word_limits_status_var.set("")

    def _key_status_text(provider: str) -> str:
        meta = credential_provider_map.get(provider) or {}
        source = meta.get("key_source") or ""
        if source == "keychain":
            return tr("✓ API-Key im macOS-Schlüsselbund gespeichert", "✓ API key stored in the macOS Keychain")
        if source == "credentials":
            key_file = meta.get("key_file") or ""
            if key_file:
                return tr(
                    f"✓ API-Key aus der Datei {key_file} (eingetragen in credentials.yaml)",
                    f"✓ API key from the file {key_file} (set in credentials.yaml)",
                )
            return tr("✓ API-Key steht direkt in credentials.yaml", "✓ API key is set directly in credentials.yaml")
        if source == "env":
            return tr("✓ API-Key aus Umgebungsvariable", "✓ API key from environment variable")
        if provider == "custom":
            return tr("Adresse und ggf. API-Key eintragen, dann testen.", "Enter the address and, if needed, an API key, then test.")
        return tr("Noch kein API-Key – eintragen und testen.", "No API key yet – enter it and test.")

    def _refresh_cloud_controls() -> None:
        provider = (provider_select_var.get() or "").strip().lower()
        is_cloud = provider in credential_provider_map
        if cloud_frame is not None:
            try:
                if is_cloud:
                    cloud_frame.grid()
                else:
                    cloud_frame.grid_remove()
            except Exception:
                pass
        if local_frame is not None:
            try:
                if provider == "local":
                    local_frame.grid()
                else:
                    local_frame.grid_remove()
            except Exception:
                pass
        if custom_url_widgets:
            for widget in custom_url_widgets:
                try:
                    if provider == "custom":
                        widget.grid()
                    else:
                        widget.grid_remove()
                except Exception:
                    pass
        if is_cloud:
            api_status_var.set(_key_status_text(provider))
            source = (credential_provider_map.get(provider) or {}).get("key_source") or ""
            note = tr(
                "Der diktierte Text (nicht die Aufnahme) wird zur Überarbeitung an diesen Anbieter gesendet.",
                "The dictated text (not the recording) is sent to this provider for rewriting.",
            )
            if source in ("credentials", "env"):
                note += " " + tr(
                    "Ein hier eingetragener Key wird im macOS-Schlüsselbund gespeichert und hat dann Vorrang.",
                    "A key entered here is stored in the macOS Keychain and then takes precedence.",
                )
            else:
                note += " " + tr(
                    "Ein hier eingetragener Key wird im macOS-Schlüsselbund gespeichert.",
                    "A key entered here is stored in the macOS Keychain.",
                )
            cloud_note_var.set(note)
            if remove_key_button is not None:
                try:
                    remove_key_button.configure(state="normal" if source == "keychain" else "disabled")
                except Exception:
                    pass

    def apply_provider_label_selection(*_) -> None:
        label = (provider_label_var.get() or "").strip()
        provider = next((key for key, text in provider_labels.items() if text == label), "local")
        provider_select_var.set(provider)
        apply_provider_selection()

    def apply_provider_selection(*_) -> None:
        provider = (provider_select_var.get() or "").strip().lower()
        if provider not in provider_choices:
            provider = "local"
            provider_select_var.set(provider)
        provider_label_var.set(provider_labels.get(provider, provider))
        api_key_var.set("")
        _update_config_key(CONFIG_PATH, "POST_PROCESSING_PROVIDER", provider)
        model_select_var.set("")
        _refresh_model_widget(prefer_id=_default_model_for_provider(provider))
        _refresh_cloud_controls()
        apply_model_selection()

    def apply_model_selection(*_) -> None:
        nonlocal default_local_model
        provider = (provider_select_var.get() or "").strip().lower()
        model = (model_select_var.get() or "").strip()
        options = _model_options_for_provider(provider)
        if provider == "local" and options and model not in options:
            model = options[0]
            model_select_var.set(model)
        if not model:
            _apply_runtime_provider_model(provider, "")
            _set_provider_info_text()
            return
        if provider == "local":
            os.environ["LOCAL_MODEL_ID"] = model
            default_local_model = model
            _set_local_default_model_in_credentials(model)
        else:
            cfg = _load_json_config(CONFIG_PATH)
            by_provider = cfg.get("LLM_MODEL_BY_PROVIDER")
            by_provider = dict(by_provider) if isinstance(by_provider, dict) else {}
            if by_provider.get(provider) != model:
                by_provider[provider] = model
                _update_config_key(CONFIG_PATH, "LLM_MODEL_BY_PROVIDER", by_provider)
        _update_config_key(CONFIG_PATH, "POST_PROCESSING_MODEL", model)
        _apply_runtime_provider_model(provider, model)
        _set_provider_info_text()

    def save_and_test_provider(*_) -> None:
        provider = (provider_select_var.get() or "").strip().lower()
        if provider not in credential_provider_map:
            return
        entered_key = (api_key_var.get() or "").strip()
        try:
            if provider == "custom":
                url = llm_providers.normalize_chat_url(custom_url_var.get())
                if not url.startswith(("http://", "https://")):
                    api_status_var.set(tr(
                        "✗ Bitte eine Adresse mit http:// oder https:// eintragen.",
                        "✗ Please enter an address starting with http:// or https://.",
                    ))
                    return
                _update_config_key(CONFIG_PATH, "CUSTOM_LLM_URL", url)
                _CONFIG["CUSTOM_LLM_URL"] = url
            if entered_key:
                llm_providers.store_key(provider, entered_key)
                api_key_var.set("")
        except llm_providers.ProviderError as error:
            api_status_var.set(f"✗ {error}")
            return
        _refresh_credential_provider_catalog()
        meta = credential_provider_map.get(provider) or {}
        api_status_var.set(tr("Verbinde …", "Connecting …"))
        if test_button is not None:
            try:
                test_button.configure(state="disabled")
            except Exception:
                pass

        def _worker() -> None:
            try:
                models = llm_providers.list_models(
                    provider, api_key=meta.get("api_key") or "", chat_url=meta.get("api_url") or ""
                )
                error_text = ""
            except llm_providers.ProviderError as error:
                models, error_text = [], str(error)

            def _done() -> None:
                if test_button is not None:
                    try:
                        test_button.configure(state="normal")
                    except Exception:
                        pass
                if (provider_select_var.get() or "").strip().lower() != provider:
                    return
                if error_text:
                    api_status_var.set(f"✗ {error_text}")
                    return
                if models:
                    fetched_provider_models[provider] = models
                current = (model_select_var.get() or "").strip()
                chosen = llm_providers.pick_default_model(provider, models, current) if models else current
                _refresh_model_widget(prefer_id=chosen)
                apply_model_selection()
                api_status_var.set(tr(
                    f"✓ Verbunden – {len(models)} Modelle gefunden",
                    f"✓ Connected – {len(models)} models found",
                ))

            _safe_root_after(_done)

        threading.Thread(target=_worker, daemon=True).start()

    def remove_provider_key(*_) -> None:
        provider = (provider_select_var.get() or "").strip().lower()
        if provider not in credential_provider_map:
            return
        llm_providers.delete_stored_key(provider)
        fetched_provider_models.pop(provider, None)
        _refresh_credential_provider_catalog()
        _apply_runtime_provider_model(provider, (model_select_var.get() or "").strip())
        _refresh_cloud_controls()

    def start_model_download(*_) -> None:
        nonlocal download_in_progress
        if download_in_progress:
            return
        url = (download_url_var.get() or "").strip()
        if not url:
            _set_download_status(tr("Download fehlgeschlagen: URL fehlt.", "Download failed: URL is missing."))
            return
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            _set_download_status(tr(
                "Download fehlgeschlagen: Nur http/https URLs sind erlaubt.",
                "Download failed: only http/https URLs are allowed.",
            ))
            return

        filename = _derive_filename_from_url(url)
        if not filename or not filename.lower().endswith(".gguf"):
            _set_download_status(tr(
                "Download fehlgeschlagen: URL muss auf eine .gguf-Datei zeigen.",
                "Download failed: the URL must point to a .gguf file.",
            ))
            return
        filename = Path(filename).name
        custom_name = (download_name_var.get() or "").strip()

        # If this .gguf is already a catalog model (e.g. a built-in preset),
        # just fetch its file — do not add a second registry entry.
        existing_id = _registry_model_id_for_filename(filename)
        if existing_id:
            model_id = existing_id
            model_label = ""
            append_entry = False
        else:
            append_entry = True
            # Optional custom name drives id + label; empty -> derive from file.
            if custom_name:
                model_id = _slugify_model_id(custom_name)
                model_label = custom_name
            else:
                model_id = _slugify_model_id_from_filename(filename)
                model_label = f"{Path(filename).stem} (downloaded)"
            if not model_id:
                _set_download_status(tr(
                    "Download fehlgeschlagen: Modell-ID konnte nicht abgeleitet werden.",
                    "Download failed: could not derive a model ID.",
                ))
                return
            conflict = _registry_conflict_message(model_id, filename)
            if conflict:
                _set_download_status(tr(f"Download abgebrochen: {conflict}", f"Download cancelled: {conflict}"))
                return

        models_dir = _local_models_dir_path()
        target_path = models_dir / filename
        part_path = models_dir / f".{filename}.part"

        if target_path.exists():
            if not messagebox.askyesno(
                tr("Modell erneut herunterladen", "Download model again"),
                tr(
                    f"Die Datei „{filename}“ ist bereits vorhanden.\n\n"
                    "Erneut herunterladen und überschreiben?\n"
                    "(Sinnvoll, falls der vorige Download unvollständig oder fehlerhaft war.)",
                    f"The file “{filename}” already exists.\n\n"
                    "Download it again and overwrite it?\n"
                    "(Useful if the previous download was incomplete or corrupt.)",
                ),
            ):
                _set_download_status(tr("Download abgebrochen.", "Download cancelled."))
                return

        download_in_progress = True
        _set_download_controls_enabled(False)
        _set_download_status(tr(f"Download gestartet: {filename}", f"Download started: {filename}"))

        def _worker() -> None:
            nonlocal download_in_progress
            try:
                models_dir.mkdir(parents=True, exist_ok=True)
                if part_path.exists():
                    part_path.unlink()

                req = urllib.request.Request(url, headers={"User-Agent": "WhisperDictate/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, part_path.open("wb") as fout:
                    total = int(resp.headers.get("Content-Length", "0") or "0")
                    downloaded = 0
                    last_update = 0.0
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if total > 0 and (now - last_update) >= 0.5:
                            pct = (downloaded * 100) // total
                            _safe_root_after(lambda p=pct, name=filename: _set_download_status(tr(f"Download läuft: {name} ({p}%)", f"Downloading: {name} ({p}%)")))
                            last_update = now

                part_path.replace(target_path)
                if append_entry:
                    _append_local_model_registry_entry(
                        model_id=model_id,
                        filename=filename,
                        label=model_label,
                    )

                def _on_success() -> None:
                    nonlocal download_in_progress
                    download_in_progress = False
                    download_name_var.set("")
                    if (provider_select_var.get() or "").strip().lower() == "local":
                        _refresh_model_widget(prefer_id=model_id)
                        apply_model_selection()
                    else:
                        _refresh_local_model_catalog()
                        _set_download_controls_enabled(False)
                    _set_download_status(tr(f"Download fertig: {filename} -> {model_id}", f"Download finished: {filename} -> {model_id}"))

                _safe_root_after(_on_success)
            except Exception as e:
                err_msg = str(e)
                try:
                    if part_path.exists():
                        part_path.unlink()
                except Exception:
                    pass

                def _on_error() -> None:
                    nonlocal download_in_progress
                    download_in_progress = False
                    _set_download_controls_enabled((provider_select_var.get() or "").strip().lower() == "local")
                    _set_download_status(tr(f"Download fehlgeschlagen: {err_msg}", f"Download failed: {err_msg}"))

                _safe_root_after(_on_error)

        threading.Thread(target=_worker, daemon=True).start()

    def open_provider_window() -> None:
        nonlocal provider_window, provider_combo, model_combo, download_url_entry, download_preset_combo, download_name_entry, download_button
        nonlocal cloud_frame, local_frame, test_button, remove_key_button
        if provider_window is not None:
            try:
                if provider_window.winfo_exists():
                    provider_window.deiconify()
                    provider_window.lift()
                    return
            except Exception:
                provider_window = None

        provider_window = tk.Toplevel(root)
        provider_window.title(tr("LLM (Überarbeitung)", "LLM (post-processing)"))
        provider_window.geometry("640x560")
        try:
            provider_window.transient(root)
        except Exception:
            pass

        body = ttk.Frame(provider_window, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        provider_window.rowconfigure(0, weight=1)
        provider_window.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text=tr("Anbieter:", "Provider:")).grid(row=0, column=0, sticky="w")
        provider_combo = ttk.Combobox(body, textvariable=provider_label_var, values=[], width=48, state="readonly")
        provider_combo.grid(row=1, column=0, sticky="we", pady=(2, 10))
        provider_combo.bind("<<ComboboxSelected>>", apply_provider_label_selection)

        # Cloud providers: address (custom only), API key, test, privacy note.
        cloud_frame = ttk.Frame(body)
        cloud_frame.grid(row=2, column=0, sticky="we")
        cloud_frame.columnconfigure(0, weight=1)
        custom_url_widgets.clear()
        url_label = ttk.Label(cloud_frame, text=tr(
            "Adresse (OpenAI-kompatibel, z. B. https://host/v1):",
            "Address (OpenAI-compatible, e.g. https://host/v1):",
        ))
        url_label.grid(row=0, column=0, columnspan=3, sticky="w")
        url_entry = ttk.Entry(cloud_frame, textvariable=custom_url_var, width=48)
        url_entry.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 8))
        custom_url_widgets.extend([url_label, url_entry])

        ttk.Label(cloud_frame, text=tr("API-Key:", "API key:")).grid(row=2, column=0, columnspan=3, sticky="w")
        key_entry = ttk.Entry(cloud_frame, textvariable=api_key_var, show="•", width=36)
        key_entry.grid(row=3, column=0, sticky="we", pady=(2, 4))
        key_entry.bind("<Return>", save_and_test_provider)
        test_button = ttk.Button(cloud_frame, text=tr("Speichern & testen", "Save & test"), command=save_and_test_provider)
        test_button.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(2, 4))
        remove_key_button = ttk.Button(cloud_frame, text=tr("Key entfernen", "Remove key"), command=remove_provider_key)
        remove_key_button.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(2, 4))
        ttk.Label(cloud_frame, textvariable=api_status_var, wraplength=600).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            cloud_frame,
            textvariable=cloud_note_var,
            wraplength=600,
            foreground="gray",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(body, text=tr("Aktiver LLM/Modell:", "Active LLM/model:")).grid(row=3, column=0, sticky="w")
        ttk.Label(body, textvariable=provider_var).grid(row=4, column=0, sticky="w", pady=(2, 10))

        ttk.Label(body, text=tr("Modell (Liste oder eigener Name):", "Model (pick or type a name):")).grid(row=5, column=0, sticky="w")
        model_combo = ttk.Combobox(body, textvariable=model_select_var, values=[], width=48, state="normal")
        model_combo.grid(row=6, column=0, sticky="we", pady=(2, 10))
        model_combo.bind("<<ComboboxSelected>>", apply_model_selection)
        model_combo.bind("<Return>", apply_model_selection)
        model_combo.bind("<FocusOut>", apply_model_selection)

        # Local GGUF models: download controls.
        local_frame = ttk.Frame(body)
        local_frame.grid(row=7, column=0, sticky="we")
        local_frame.columnconfigure(0, weight=1)
        ttk.Label(local_frame, text=tr("Download-Preset:", "Download preset:")).grid(row=0, column=0, sticky="w")
        download_preset_combo = ttk.Combobox(local_frame, textvariable=download_preset_var, values=download_preset_labels, width=48, state="readonly")
        download_preset_combo.grid(row=1, column=0, sticky="we", pady=(2, 10))
        download_preset_combo.bind("<<ComboboxSelected>>", apply_download_preset)

        ttk.Label(local_frame, text="Download URL (.gguf):").grid(row=2, column=0, sticky="w")
        download_url_entry = ttk.Entry(local_frame, textvariable=download_url_var, width=48)
        download_url_entry.grid(row=3, column=0, sticky="we", pady=(2, 6))
        download_url_entry.bind("<Return>", start_model_download)

        ttk.Label(local_frame, text=tr("Eigener Modell-Name (optional):", "Custom model name (optional):")).grid(row=4, column=0, sticky="w")
        download_name_entry = ttk.Entry(local_frame, textvariable=download_name_var, width=48)
        download_name_entry.grid(row=5, column=0, sticky="we", pady=(2, 6))
        download_name_entry.bind("<Return>", start_model_download)

        dl_btn_row = ttk.Frame(local_frame)
        dl_btn_row.grid(row=6, column=0, sticky="we")
        download_button = ttk.Button(dl_btn_row, text=tr("Modell herunterladen", "Download model"), command=start_model_download)
        download_button.grid(row=0, column=0, sticky="w")
        ttk.Label(dl_btn_row, textvariable=download_status_var).grid(row=0, column=1, sticky="w", padx=(10, 0))
        if not llm_local_runtime_available():
            ttk.Label(
                local_frame,
                text=tr(
                    "Für lokale Modelle einmalig ausführen: uv sync --extra local-llm",
                    "For local models, run once: uv sync --extra local-llm",
                ),
                wraplength=600,
                foreground="gray",
            ).grid(row=7, column=0, sticky="w", pady=(8, 0))

        def _close_provider_window() -> None:
            nonlocal provider_window, provider_combo, model_combo, download_url_entry, download_preset_combo, download_name_entry, download_button
            nonlocal cloud_frame, local_frame, test_button, remove_key_button
            try:
                if provider_window is not None and provider_window.winfo_exists():
                    provider_window.destroy()
            except Exception:
                pass
            provider_window = None
            provider_combo = None
            model_combo = None
            download_url_entry = None
            download_preset_combo = None
            download_name_entry = None
            download_button = None
            cloud_frame = None
            local_frame = None
            test_button = None
            remove_key_button = None
            custom_url_widgets.clear()

        provider_window.protocol("WM_DELETE_WINDOW", _close_provider_window)

        _refresh_credential_provider_catalog()
        _refresh_provider_choices()
        try:
            provider_combo.configure(values=[provider_labels[key] for key in provider_choices])
        except Exception:
            pass
        provider_label_var.set(provider_labels.get((provider_select_var.get() or "").strip().lower(), ""))
        _refresh_model_widget(prefer_id=_default_model_for_provider((provider_select_var.get() or "").strip().lower()))
        apply_download_preset()
        _refresh_cloud_controls()
        _set_provider_info_text()

    def open_prompts_window() -> None:
        nonlocal prompts_window, prompts_listbox, prompts_text_widget, prompts_entries, prompts_selected_mode
        if prompts_window is not None:
            try:
                if prompts_window.winfo_exists():
                    prompts_window.deiconify()
                    prompts_window.lift()
                    return
            except Exception:
                prompts_window = None

        prompts_window = tk.Toplevel(root)
        prompts_window.title("Prompts")
        prompts_window.geometry("860x560")
        try:
            prompts_window.transient(root)
        except Exception:
            pass

        prompts_entries = _load_current_prompt_entries()
        prompts_selected_mode = None
        prompts_mode_var.set("")
        prompts_label_var.set("")
        prompts_status_var.set("")

        body = ttk.Frame(prompts_window, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        prompts_window.rowconfigure(0, weight=1)
        prompts_window.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        ttk.Label(body, text="Prompts (current_prompts.json):").grid(row=0, column=0, sticky="w")
        list_frame = ttk.Frame(body)
        list_frame.grid(row=1, column=0, sticky="ns", padx=(0, 12))
        prompts_listbox = tk.Listbox(list_frame, width=32, exportselection=False)
        prompts_listbox.grid(row=0, column=0, sticky="ns")
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=prompts_listbox.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        prompts_listbox.configure(yscrollcommand=list_scroll.set)

        editor = ttk.Frame(body)
        editor.grid(row=1, column=1, sticky="nsew")
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(3, weight=1)

        ttk.Label(editor, text=tr("Mode-ID:", "Mode ID:")).grid(row=0, column=0, sticky="w")
        mode_entry = ttk.Entry(editor, textvariable=prompts_mode_var, width=10)
        mode_entry.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=(0, 8))

        ttk.Label(editor, text="Label:").grid(row=1, column=0, sticky="w")
        label_entry = ttk.Entry(editor, textvariable=prompts_label_var)
        label_entry.grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(0, 8))

        ttk.Label(editor, text=tr("Optionen:", "Options:")).grid(row=2, column=0, sticky="nw")
        options_frame = ttk.Frame(editor)
        options_frame.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(0, 8))
        ttk.Checkbutton(
            options_frame,
            text=tr("Signatur/Kontaktblock anhängen (add_signature)", "Append signature/contact block (add_signature)"),
            variable=prompts_add_signature_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            options_frame,
            text=tr(
                "Voreingestellte Ausgabesprache ignorieren (skip_output_language)",
                "Ignore the configured output language (skip_output_language)",
            ),
            variable=prompts_skip_output_lang_var,
        ).grid(row=1, column=0, sticky="w")

        ttk.Label(editor, text="Prompt:").grid(row=3, column=0, sticky="nw")
        prompts_text_widget = tk.Text(editor, wrap="word", width=72, height=18)
        prompts_text_widget.grid(row=3, column=1, sticky="nsew", padx=(6, 0))
        text_scroll = ttk.Scrollbar(editor, orient="vertical", command=prompts_text_widget.yview)
        text_scroll.grid(row=3, column=2, sticky="ns")
        prompts_text_widget.configure(yscrollcommand=text_scroll.set)

        ttk.Label(body, textvariable=prompts_status_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 6))

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e")

        def _refresh_prompts_list(select_mode: Optional[int] = None) -> None:
            if prompts_listbox is None:
                return
            modes = sorted(prompts_entries.keys())
            try:
                prompts_listbox.delete(0, "end")
            except Exception:
                return
            for mode in modes:
                label = str((prompts_entries.get(mode) or {}).get("label") or f"Mode {mode}")
                prompts_listbox.insert("end", f"{mode}: {label}")

            target_mode = select_mode if select_mode is not None else prompts_selected_mode
            if target_mode in prompts_entries:
                idx = modes.index(target_mode)
                prompts_listbox.selection_clear(0, "end")
                prompts_listbox.selection_set(idx)
                prompts_listbox.activate(idx)

        def _load_editor_from_mode(mode: Optional[int]) -> None:
            if mode is None or mode not in prompts_entries:
                prompts_mode_var.set("")
                prompts_label_var.set("")
                prompts_add_signature_var.set(False)
                prompts_skip_output_lang_var.set(False)
                if prompts_text_widget is not None:
                    prompts_text_widget.delete("1.0", "end")
                return
            entry = prompts_entries.get(mode) or {}
            prompts_mode_var.set(str(mode))
            prompts_label_var.set(str(entry.get("label") or ""))
            prompts_add_signature_var.set(bool(entry.get("add_signature")))
            prompts_skip_output_lang_var.set(bool(entry.get("skip_output_language")))
            if prompts_text_widget is not None:
                prompts_text_widget.delete("1.0", "end")
                prompts_text_widget.insert("1.0", str(entry.get("prompt") or ""))

        def _store_editor_into_entries(show_error: bool = True, refresh_list: bool = True) -> bool:
            nonlocal prompts_selected_mode
            if prompts_selected_mode is None:
                return True
            mode_raw = (prompts_mode_var.get() or "").strip()
            label = (prompts_label_var.get() or "").strip()
            prompt = (prompts_text_widget.get("1.0", "end-1c") if prompts_text_widget is not None else "").strip()
            try:
                mode_value = int(mode_raw)
            except Exception:
                if show_error:
                    prompts_status_var.set(tr("Ungültige Mode-ID.", "Invalid mode ID."))
                return False
            if mode_value <= 0:
                if show_error:
                    prompts_status_var.set(tr("Mode-ID muss > 0 sein.", "Mode ID must be > 0."))
                return False
            if mode_value in RESERVED_PROMPT_MODE_IDS:
                if show_error:
                    prompts_status_var.set(tr(f"Mode-ID {mode_value} ist reserviert.", f"Mode ID {mode_value} is reserved."))
                return False
            if not label:
                if show_error:
                    prompts_status_var.set(tr("Label darf nicht leer sein.", "Label must not be empty."))
                return False
            if not prompt:
                if show_error:
                    prompts_status_var.set(tr("Prompt darf nicht leer sein.", "Prompt must not be empty."))
                return False
            if mode_value != prompts_selected_mode and mode_value in prompts_entries:
                if show_error:
                    prompts_status_var.set(tr(f"Mode-ID {mode_value} existiert bereits.", f"Mode ID {mode_value} already exists."))
                return False
            prompts_entries.pop(prompts_selected_mode, None)
            prompts_entries[mode_value] = {
                "label": label,
                "prompt": prompt,
                "add_signature": bool(prompts_add_signature_var.get()),
                "skip_output_language": bool(prompts_skip_output_lang_var.get()),
            }
            prompts_selected_mode = mode_value
            if refresh_list:
                _refresh_prompts_list(select_mode=mode_value)
            return True

        def _select_mode(mode: Optional[int]) -> None:
            nonlocal prompts_selected_mode
            prompts_selected_mode = mode
            _load_editor_from_mode(mode)

        def _on_select_prompt(_event=None) -> None:
            nonlocal prompts_selected_mode
            if prompts_listbox is None:
                return
            selection = prompts_listbox.curselection()
            if not selection:
                return
            modes = sorted(prompts_entries.keys())
            idx = int(selection[0])
            if idx < 0 or idx >= len(modes):
                return
            _store_editor_into_entries(show_error=False, refresh_list=False)
            mode = modes[idx]
            prompts_selected_mode = mode
            _load_editor_from_mode(mode)
            _refresh_prompts_list(select_mode=mode)
            prompts_status_var.set("")

        def _new_prompt() -> None:
            if not _store_editor_into_entries(show_error=True):
                return
            next_mode = max(prompts_entries.keys(), default=0) + 1
            prompts_entries[next_mode] = {
                "label": f"Mode {next_mode}",
                "prompt": tr("Beschreibe hier den Prompt.", "Describe the prompt here."),
            }
            _select_mode(next_mode)
            _refresh_prompts_list(select_mode=next_mode)
            prompts_status_var.set("")

        def _delete_prompt() -> None:
            nonlocal prompts_selected_mode
            if prompts_selected_mode is None or prompts_selected_mode not in prompts_entries:
                prompts_status_var.set(tr("Bitte zuerst einen Prompt auswählen.", "Please select a prompt first."))
                return
            if not messagebox.askyesno(
                tr("Prompt löschen", "Delete prompt"),
                tr(f"Prompt {prompts_selected_mode} wirklich löschen?", f"Really delete prompt {prompts_selected_mode}?"),
            ):
                return
            prompts_entries.pop(prompts_selected_mode, None)
            next_mode = sorted(prompts_entries.keys())[0] if prompts_entries else None
            _select_mode(next_mode)
            _refresh_prompts_list(select_mode=next_mode)
            prompts_status_var.set("")

        def _save_prompts() -> None:
            if not _store_editor_into_entries(show_error=True):
                return
            ok, msg = _save_current_prompt_entries(prompts_entries)
            if not ok:
                prompts_status_var.set(msg)
                return
            _reload_prompt_modes_runtime()
            prompts_status_var.set(tr("Prompts gespeichert und neu geladen.", "Prompts saved and reloaded."))

        def _reset_prompts() -> None:
            nonlocal prompts_entries, prompts_selected_mode
            template_name = PROMPTS_TEMPLATE_PATH.name
            if not messagebox.askyesno(
                tr("Prompts zurücksetzen", "Reset prompts"),
                tr(
                    f"current_prompts.json aus {template_name} neu erstellen?",
                    f"Recreate current_prompts.json from {template_name}?",
                ),
            ):
                return
            try:
                CURRENT_PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROMPTS_TEMPLATE_PATH, CURRENT_PROMPTS_PATH)
            except Exception as e:
                prompts_status_var.set(tr(f"Reset fehlgeschlagen: {e}", f"Reset failed: {e}"))
                return
            prompts_entries = _load_current_prompt_entries()
            prompts_selected_mode = sorted(prompts_entries.keys())[0] if prompts_entries else None
            _select_mode(prompts_selected_mode)
            _refresh_prompts_list(select_mode=prompts_selected_mode)
            _reload_prompt_modes_runtime()
            prompts_status_var.set(tr("Prompts zurückgesetzt und neu geladen.", "Prompts reset and reloaded."))

        def _close_prompts_window() -> None:
            nonlocal prompts_window, prompts_listbox, prompts_text_widget, prompts_selected_mode
            try:
                if prompts_window is not None and prompts_window.winfo_exists():
                    prompts_window.destroy()
            except Exception:
                pass
            prompts_window = None
            prompts_listbox = None
            prompts_text_widget = None
            prompts_selected_mode = None

        ttk.Button(buttons, text=tr("Neu", "New"), command=_new_prompt).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text=tr("Löschen", "Delete"), command=_delete_prompt).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text=tr("Speichern", "Save"), command=_save_prompts).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(buttons, text="Reset", command=_reset_prompts).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(buttons, text=tr("Schließen", "Close"), command=_close_prompts_window).grid(row=0, column=4)

        prompts_listbox.bind("<<ListboxSelect>>", _on_select_prompt)
        mode_entry.bind("<Return>", lambda _e: _save_prompts())
        label_entry.bind("<Return>", lambda _e: _save_prompts())
        prompts_window.protocol("WM_DELETE_WINDOW", _close_prompts_window)

        prompts_selected_mode = sorted(prompts_entries.keys())[0] if prompts_entries else None
        _select_mode(prompts_selected_mode)
        _refresh_prompts_list(select_mode=prompts_selected_mode)

    def on_close():
        global SETTINGS_GUI_ROOT
        nonlocal context_window, context_text, provider_window, provider_combo, model_combo, download_url_entry, download_preset_combo, download_name_entry, download_button
        nonlocal prompts_window, prompts_listbox, prompts_text_widget, prompts_selected_mode, llm_change_window
        stop_event.set()
        try:
            if overlay is not None and overlay.winfo_exists():
                overlay.destroy()
        except Exception:
            pass
        try:
            if context_window is not None and context_window.winfo_exists():
                context_window.destroy()
        except Exception:
            pass
        try:
            if provider_window is not None and provider_window.winfo_exists():
                provider_window.destroy()
        except Exception:
            pass
        try:
            if prompts_window is not None and prompts_window.winfo_exists():
                prompts_window.destroy()
        except Exception:
            pass
        try:
            if llm_change_window is not None and llm_change_window.winfo_exists():
                llm_change_window.destroy()
        except Exception:
            pass
        context_window = None
        context_text = None
        provider_window = None
        provider_combo = None
        model_combo = None
        download_url_entry = None
        download_preset_combo = None
        download_name_entry = None
        download_button = None
        prompts_window = None
        prompts_listbox = None
        prompts_text_widget = None
        prompts_selected_mode = None
        llm_change_window = None
        try:
            path = recorder.stop()
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            SETTINGS_GUI_ROOT = None
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)

    # Every menu works like "Whisper model": the choices first, the window last.
    # macOS menu bar entries cannot open a window directly; they must be menus.
    menu_bar = tk.Menu(root)

    def _rebuild_prompts_menu() -> None:
        prompts_menu.delete(0, "end")
        for value, label in _ordered_mode_options():
            prompts_menu.add_radiobutton(label=label, value=value, variable=mode_var, command=apply_mode)
        prompts_menu.add_separator()
        prompts_menu.add_command(label=tr("Prompts bearbeiten…", "Edit prompts…"), command=open_prompts_window)

    def _provider_ready_for_menu(key: str) -> bool:
        if key == "local":
            return True
        if key == "ollama":
            return True
        meta = credential_provider_map.get(key) or {}
        if key == "custom":
            return bool(meta.get("api_url"))
        return bool(meta.get("key_source"))

    def _rebuild_provider_menu() -> None:
        provider_menu.delete(0, "end")
        _refresh_credential_provider_catalog()
        _refresh_provider_choices()
        current = (provider_select_var.get() or "").strip().lower()
        for key in provider_choices:
            if key != current and not _provider_ready_for_menu(key):
                continue
            if key == current:
                model = (model_select_var.get() or "").strip()
            else:
                model = _default_model_for_provider(key)
            label = provider_labels.get(key, key)
            if key == "local":
                model_text = local_model_map.get(model, model)
            else:
                model_text = _provider_model_text(key, model)
            provider_menu.add_radiobutton(
                label=f"{label}: {model_text}" if model_text else label,
                value=key,
                variable=provider_select_var,
                command=apply_provider_selection,
            )
        provider_menu.add_separator()
        provider_menu.add_command(label=tr("Anbieter & API-Keys…", "Providers & API keys…"), command=open_provider_window)

    def _rebuild_context_menu() -> None:
        context_menu.delete(0, "end")
        context_processing_var.set(_is_context_processing_enabled())
        auto_context_var.set(_is_auto_context_enabled())
        context_menu.add_checkbutton(
            label=tr("Kontext verwenden", "Use context"),
            variable=context_processing_var,
            command=apply_context_processing_toggle,
        )
        context_menu.add_checkbutton(
            label=tr("Ergebnisse automatisch anhängen", "Append results automatically"),
            variable=auto_context_var,
            command=apply_auto_context_toggle,
        )
        context_menu.add_separator()
        context_menu.add_command(label=tr("Kontext bearbeiten…", "Edit context…"), command=open_context_window)

    prompts_menu = tk.Menu(menu_bar, tearoff=0, postcommand=_rebuild_prompts_menu)
    _rebuild_prompts_menu()
    menu_bar.add_cascade(label="Prompts", menu=prompts_menu)
    provider_menu = tk.Menu(menu_bar, tearoff=0, postcommand=_rebuild_provider_menu)
    _rebuild_provider_menu()
    menu_bar.add_cascade(label="LLM (Post-Processing)", menu=provider_menu)
    whisper_model_menu = tk.Menu(menu_bar, tearoff=0)
    _rebuild_whisper_model_menu()
    menu_bar.add_cascade(label=tr("Whisper-Modell", "Whisper model"), menu=whisper_model_menu)
    context_menu = tk.Menu(menu_bar, tearoff=0, postcommand=_rebuild_context_menu)
    _rebuild_context_menu()
    menu_bar.add_cascade(label=tr("Kontext", "Context"), menu=context_menu)
    # name="help" makes Tk use it as the native macOS Help menu (last place,
    # with the system search field).
    help_menu = tk.Menu(menu_bar, tearoff=0, name="help")
    help_menu.add_command(label=tr("Bedienungsanleitung", "User guide"), command=lambda: open_url(manual_url()))
    help_menu.add_command(label=tr("Projektseite auf GitHub", "Project page on GitHub"), command=lambda: open_url(PROJECT_URL))
    menu_bar.add_cascade(label=tr("Hilfe", "Help"), menu=help_menu)
    try:
        root.configure(menu=menu_bar)
    except Exception:
        pass

    def _normalize_llm_overlay_message(status: dict) -> str:
        code = status.get("http_status")
        if isinstance(code, int):
            return f"⚠ LLM {code}"
        err = str(status.get("error_type") or "").strip().lower()
        if err == "no_provider":
            return "⚠ no LLM"
        if err == "missing_key":
            return "⚠ no LLM key"
        if err == "missing_model":
            return "⚠ no LLM model"
        if err == "ssl":
            return "⚠ LLM SSL"
        return "⚠ no LLM"

    def _enqueue_llm_overlay_alert(status: dict) -> None:
        nonlocal llm_alert_pending_text, llm_alert_pending_ts, llm_alert_last_enqueued_ts
        if not llm_alert_overlay_enabled:
            return
        ts = float(status.get("timestamp") or 0.0)
        if ts <= 0.0 or ts <= llm_alert_last_enqueued_ts:
            return
        llm_alert_last_enqueued_ts = ts
        llm_alert_pending_ts = ts
        llm_alert_pending_text = _normalize_llm_overlay_message(status)

    def _set_overlay_appearance(mode: str, text: str) -> None:
        if overlay is None or overlay_label is None:
            return
        if mode == "recording":
            bg = "#B00020"
            fg = "white"
        elif mode == "llm_alert":
            bg = "#B35A00"
            fg = "white"
        else:
            bg = "#222222"
            fg = "white"
        overlay.configure(bg=bg)
        overlay_label.configure(bg=bg, fg=fg, text=text)

    def _format_llm_runtime_status(status: dict) -> str:
        state = str(status.get("state") or "").strip().lower()
        provider = str(status.get("provider") or "").strip().upper()
        message = str(status.get("message") or "").strip()
        code = status.get("http_status")
        if state == "probe":
            if message:
                return f"LLM: {message}"
            return "LLM: unknown"
        if state == "error":
            if isinstance(code, int):
                return f"LLM: ❌ {provider or 'PROVIDER'} HTTP {code}"
            return f"LLM: ❌ {message or tr('Verbindung/Authentifizierung fehlgeschlagen', 'connection/authentication failed')}"
        if state == "ok":
            changed = status.get("changed")
            if changed is True:
                return tr(f"LLM: ✅ korrigiert ({provider or 'AKTIV'})", f"LLM: ✅ corrected ({provider or 'ACTIVE'})")
            return tr(f"LLM: ✓ keine Änderung ({provider or 'AKTIV'})", f"LLM: ✓ no change ({provider or 'ACTIVE'})")
        if state == "disabled":
            return tr("LLM: aus (deaktiviert)", "LLM: off (disabled)")
        if state in {"skip", "fallback"}:
            return f"LLM: {message or tr('übersprungen', 'skipped')}"
        return tr("LLM: bereit", "LLM: ready")

    def _refresh_llm_runtime_status_label() -> None:
        nonlocal llm_runtime_status_cache_ts
        try:
            status = get_last_llm_status()
            ts = float(status.get("timestamp") or 0.0)
            if ts != llm_runtime_status_cache_ts:
                llm_runtime_status_cache_ts = ts
                llm_runtime_status_var.set(_format_llm_runtime_status(status))
                if str(status.get("state") or "").strip().lower() == "error":
                    _enqueue_llm_overlay_alert(status)
        except Exception:
            pass
        try:
            root.after(700, _refresh_llm_runtime_status_label)
        except Exception:
            pass

    def _refresh_llm_change_button() -> None:
        nonlocal llm_change_cache_ts
        if llm_change_button is None:
            return
        try:
            snapshot = get_last_llm_change()
            ts = float(snapshot.get("timestamp") or 0.0)
            if ts != llm_change_cache_ts:
                llm_change_cache_ts = ts
                if bool(snapshot.get("changed")):
                    llm_change_button.state(["!disabled"])
                else:
                    llm_change_button.state(["disabled"])
        except Exception:
            pass
        try:
            root.after(700, _refresh_llm_change_button)
        except Exception:
            pass

    def _show_llm_change_window() -> None:
        nonlocal llm_change_window
        snapshot = get_last_llm_change()
        if not bool(snapshot.get("changed")):
            try:
                messagebox.showinfo(
                    tr("LLM-Änderungen", "LLM changes"),
                    tr(
                        "Für den letzten Durchlauf wurde keine LLM-Änderung erkannt.",
                        "The LLM made no changes in the last run.",
                    ),
                )
            except Exception:
                pass
            return
        if llm_change_window is not None:
            try:
                if llm_change_window.winfo_exists():
                    llm_change_window.deiconify()
                    llm_change_window.lift()
                    return
            except Exception:
                llm_change_window = None

        llm_change_window = tk.Toplevel(root)
        llm_change_window.title(tr("LLM-Änderungen", "LLM changes"))
        llm_change_window.geometry("860x560")
        try:
            llm_change_window.transient(root)
        except Exception:
            pass
        body = ttk.Frame(llm_change_window, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        llm_change_window.rowconfigure(0, weight=1)
        llm_change_window.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)

        original_text = str(snapshot.get("original") or "")
        refined_text = str(snapshot.get("refined") or "")

        ttk.Label(body, text=tr("Original (vor LLM):", "Original (before LLM):")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        original_box = tk.Text(body, wrap="word", height=10)
        original_box.grid(row=1, column=0, sticky="nsew")
        original_box.insert("1.0", original_text)
        original_box.configure(state="disabled")

        ttk.Label(body, text=tr("LLM-Ausgabe (nach LLM):", "LLM output (after LLM):")).grid(row=2, column=0, sticky="w", pady=(10, 4))
        refined_box = tk.Text(body, wrap="word", height=10)
        refined_box.grid(row=3, column=0, sticky="nsew")
        refined_box.insert("1.0", refined_text)
        refined_box.configure(state="disabled")

        ttk.Button(body, text=tr("Schließen", "Close"), command=llm_change_window.destroy).grid(row=4, column=0, sticky="e", pady=(12, 0))

        def _on_close_llm_change() -> None:
            nonlocal llm_change_window
            try:
                llm_change_window.destroy()
            except Exception:
                pass
            llm_change_window = None

        llm_change_window.protocol("WM_DELETE_WINDOW", _on_close_llm_change)

    frame = ttk.Frame(root, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)

    footer = ttk.Frame(root, padding=(12, 0, 12, 12))
    footer.grid(row=1, column=0, sticky="ew")
    footer.columnconfigure(0, weight=1)

    def _toggle_recording_button() -> None:
        # Manual start/stop fallback for when Fn+Fn double-tap is unavailable.
        global _RECORDING_VIA_BUTTON
        if recorder.proc is None:
            try:
                recorder.start()
                _RECORDING_VIA_BUTTON = True
            except Exception as e:
                print(f"⚠️ Starting the recording (button) failed: {e}")
        else:
            threading.Thread(target=stop_and_process, args=("manual",), daemon=True).start()

    def _copy_last_text() -> None:
        # Manual fallback when auto-paste landed nowhere useful.
        text = (LAST_RECOGNIZED_TEXT or "").strip()
        if not text:
            print("📋 No recognized text to copy.")
            return
        if _set_clipboard(text):
            print(f"📋 Copied the last text to the clipboard ({len(text)} characters).")

    button_bar = ttk.Frame(frame)
    button_bar.grid(row=0, column=0, sticky="w", pady=(0, 10))
    record_btn = ttk.Button(
        button_bar,
        text=tr("Aufnahme starten", "Start recording"),
        command=_toggle_recording_button,
    )
    record_btn.grid(row=0, column=0, padx=(0, 6))
    ttk.Button(
        button_bar,
        text=tr("Persönliche Daten…", "Personal data…"),
        command=lambda: _show_profile_dialog(root),
    ).grid(row=0, column=1, padx=(0, 6))
    ttk.Button(
        button_bar,
        text=tr("Letzten Text kopieren", "Copy last text"),
        command=_copy_last_text,
    ).grid(row=0, column=2)

    llm_status_row = ttk.Frame(frame)
    llm_status_row.grid(row=2, column=0, sticky="we", pady=(0, 8))
    llm_status_row.columnconfigure(0, weight=1)
    ttk.Label(llm_status_row, textvariable=llm_runtime_status_var, foreground="#555").grid(row=0, column=0, sticky="w")
    llm_change_button = ttk.Button(llm_status_row, text=tr("Änderungen anzeigen", "Show changes"), command=_show_llm_change_window)
    llm_change_button.grid(row=0, column=1, sticky="e")
    llm_change_button.state(["disabled"])

    ttk.Label(frame, textvariable=audio_input_status_var).grid(row=4, column=0, sticky="w", pady=(0, 10))

    ttk.Label(frame, text=tr("Sprache (Eingabe / Whisper):", "Language (input / Whisper):")).grid(row=5, column=0, sticky="w")
    input_lang_entry = ttk.Entry(frame, textvariable=input_lang_var, width=34)
    input_lang_entry.grid(row=6, column=0, sticky="we", pady=(2, 10))
    input_lang_entry.bind("<Return>", apply_input_language)
    input_lang_entry.bind("<FocusOut>", apply_input_language)

    ttk.Label(frame, text=tr("Sprache (Ausgabe / LLM):", "Language (output / LLM):")).grid(row=7, column=0, sticky="w")
    output_lang_entry = ttk.Entry(frame, textvariable=output_lang_var, width=34)
    output_lang_entry.grid(row=8, column=0, sticky="we", pady=(2, 10))
    output_lang_entry.bind("<Return>", apply_output_language)
    output_lang_entry.bind("<FocusOut>", apply_output_language)

    ttk.Label(frame, text=tr("Wortgrenzen (Post-Processing, optional):", "Word limits (post-processing, optional):")).grid(row=9, column=0, sticky="w")
    word_limits_frame = ttk.Frame(frame)
    word_limits_frame.grid(row=10, column=0, sticky="we", pady=(2, 4))
    ttk.Label(word_limits_frame, text=tr("Untergrenze:", "Minimum:")).grid(row=0, column=0, sticky="w")
    word_min_entry = ttk.Entry(word_limits_frame, textvariable=word_min_var, width=8)
    word_min_entry.grid(row=0, column=1, sticky="w", padx=(6, 12))
    word_min_entry.bind("<Return>", apply_word_limits)
    word_min_entry.bind("<FocusOut>", apply_word_limits)
    ttk.Label(word_limits_frame, text=tr("Obergrenze:", "Maximum:")).grid(row=0, column=2, sticky="w")
    word_max_entry = ttk.Entry(word_limits_frame, textvariable=word_max_var, width=8)
    word_max_entry.grid(row=0, column=3, sticky="w", padx=(6, 0))
    word_max_entry.bind("<Return>", apply_word_limits)
    word_max_entry.bind("<FocusOut>", apply_word_limits)

    ttk.Label(frame, textvariable=word_limits_status_var, foreground="#B00020").grid(row=11, column=0, sticky="w", pady=(0, 6))

    context_processing_var.set(_is_context_processing_enabled())
    auto_context_var.set(_is_auto_context_enabled())
    context_toggles = ttk.Frame(frame)
    context_toggles.grid(row=12, column=0, sticky="w", pady=(0, 10))
    ttk.Checkbutton(context_toggles, text=tr("Kontext verwenden", "Use context"), variable=context_processing_var, command=apply_context_processing_toggle).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(context_toggles, text=tr("Ergebnisse automatisch anhängen", "Append results automatically"), variable=auto_context_var, command=apply_auto_context_toggle).grid(row=0, column=1, sticky="w", padx=(12, 0))

    ttk.Label(frame, text=tr("Post-Processing auswählen:", "Select post-processing:")).grid(row=13, column=0, sticky="w")
    mode_options_frame = ttk.Frame(frame)
    mode_options_frame.grid(row=14, column=0, sticky="w")

    # ---- Autostart toggle ----
    autostart_frame = ttk.Frame(frame)
    autostart_frame.grid(row=15, column=0, sticky="we", pady=(12, 0))
    autostart_frame.columnconfigure(0, weight=1)
    autostart_var = tk.BooleanVar(value=autostart_is_enabled())
    autostart_status_var = tk.StringVar(value="")

    def _apply_autostart_toggle() -> None:
        want_enabled = bool(autostart_var.get())
        if want_enabled:
            ok, msg = autostart_enable()
        else:
            ok, msg = autostart_disable()
        if not ok:
            # Rollback the checkbox state on failure so the UI reflects reality.
            autostart_var.set(autostart_is_enabled())
            autostart_status_var.set(f"⚠️ {msg}")
            try:
                messagebox.showwarning("Autostart", msg)
            except Exception:
                pass
        else:
            autostart_status_var.set(msg)
            permission_binary = autostart_permission_binary()
            if want_enabled and permission_binary is not None:
                # Source run: at login macOS checks the permissions of this
                # Python binary, not of the terminal the app runs in now.
                try:
                    root.clipboard_clear()
                    root.clipboard_append(str(permission_binary))
                except Exception:
                    pass
                try:
                    messagebox.showinfo(
                        tr("Autostart: Rechte vergeben", "Autostart: grant permissions"),
                        tr(
                            "Beim Anmelden startet macOS die App ohne Terminal. Dann braucht "
                            "dieses Python-Programm die Rechte „Bedienungshilfen“ und "
                            "„Eingabeüberwachung“:\n\n"
                            f"{permission_binary}\n\n"
                            "Der Pfad ist in der Zwischenablage. In den Systemeinstellungen "
                            "jeweils auf „+“ klicken, ⌘⇧G drücken, Pfad einfügen und hinzufügen.",
                            "At login macOS starts the app without a terminal. This Python "
                            "program then needs the \"Accessibility\" and \"Input Monitoring\" "
                            "permissions:\n\n"
                            f"{permission_binary}\n\n"
                            "The path is on the clipboard. In System Settings, click \"+\" in "
                            "each list, press ⌘⇧G, paste the path and add it.",
                        ),
                        parent=root,
                    )
                except Exception:
                    pass
                open_system_settings_pane("accessibility")
            if not want_enabled:
                # On disable, open System Settings → Login Items so the user can
                # verify / remove the entry there (macOS may keep showing it).
                try:
                    subprocess.Popen(
                        ["/usr/bin/open",
                         "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass

    autostart_check = ttk.Checkbutton(
        autostart_frame,
        text=tr("Beim Anmelden automatisch starten", "Start automatically at login"),
        variable=autostart_var,
        command=_apply_autostart_toggle,
    )
    autostart_check.grid(row=0, column=0, sticky="w")
    ttk.Button(
        autostart_frame,
        text=tr("Berechtigungen prüfen…", "Check permissions…"),
        command=lambda: _show_onboarding_dialog(root),
    ).grid(row=0, column=1, sticky="e", padx=(12, 0))
    ttk.Label(autostart_frame, textvariable=autostart_status_var, foreground="#555").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
    )

    # ---- Interface language (applies after a restart) ----
    ui_lang_frame = ttk.Frame(frame)
    ui_lang_frame.grid(row=16, column=0, sticky="we", pady=(10, 0))
    ui_lang_options = [("auto", "Auto"), ("de", "Deutsch"), ("en", "English")]
    ui_lang_var = tk.StringVar(value=dict(ui_lang_options).get(UI_LANGUAGE_SETTING, "Auto"))
    ui_lang_status_var = tk.StringVar(value="")
    ttk.Label(ui_lang_frame, text=tr("Sprache der Oberfläche:", "Interface language:")).grid(
        row=0, column=0, sticky="w"
    )
    ui_lang_combo = ttk.Combobox(
        ui_lang_frame,
        textvariable=ui_lang_var,
        values=[label for _code, label in ui_lang_options],
        width=10,
        state="readonly",
    )
    ui_lang_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
    ttk.Label(ui_lang_frame, textvariable=ui_lang_status_var, foreground="#555").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
    )
    if (os.environ.get("UI_LANGUAGE") or "").strip():
        ui_lang_status_var.set(tr(
            "Hinweis: Die Umgebungsvariable UI_LANGUAGE hat Vorrang.",
            "Note: the UI_LANGUAGE environment variable takes precedence.",
        ))

    def _apply_ui_language(*_) -> None:
        global UI_LANGUAGE_SETTING
        selected = ui_lang_var.get()
        code = next((c for c, label in ui_lang_options if label == selected), "auto")
        if code == UI_LANGUAGE_SETTING:
            return
        UI_LANGUAGE_SETTING = code
        try:
            _update_config_key(CONFIG_PATH, "UI_LANGUAGE", code)
        except Exception as e:
            ui_lang_status_var.set(tr(f"Speichern fehlgeschlagen: {e}", f"Saving failed: {e}"))
            return
        if resolve_ui_language(code) == ui_language():
            ui_lang_status_var.set("")
            return
        ui_lang_status_var.set(tr("Wird nach einem Neustart wirksam.", "Takes effect after a restart."))
        if not IS_FROZEN:
            return
        try:
            restart_now = messagebox.askyesno(
                tr("Neu starten?", "Restart?"),
                tr(
                    "Die neue Sprache wird nach einem Neustart wirksam.\n\nJetzt neu starten?",
                    "The new language takes effect after a restart.\n\nRestart now?",
                ),
                parent=root,
            )
        except Exception:
            restart_now = False
        if restart_now and _relaunch_app():
            os._exit(0)

    ui_lang_combo.bind("<<ComboboxSelected>>", _apply_ui_language)

    close_button = ttk.Button(footer, text=tr("Beenden", "Quit"), command=on_close)
    close_button.grid(row=0, column=0, sticky="e")
    _refresh_mode_options_ui()

    # First-run onboarding: walk the user through privacy permissions before
    # we let the GUI hide itself. We force-show the settings window for the
    # duration of the dialog so the modal Toplevel has a visible parent.
    needs_onboarding = onboarding_should_run()
    if needs_onboarding:
        show_settings_window()
        try:
            root.update_idletasks()
        except Exception:
            pass
        # First run with Accessibility missing: pop ONLY the native macOS
        # Accessibility access prompt (it registers the app and has its
        # own "Open System Settings" button). The custom onboarding dialog stays
        # available via "Check permissions…"; showing both at once produced
        # two windows with identical buttons.
        open_system_settings_pane("accessibility")

    if show_settings_gui:
        show_settings_window()
    else:
        try:
            # On macOS, prefer minimize over withdraw to keep the Dock icon available.
            if sys.platform == "darwin":
                root.iconify()
            else:
                root.withdraw()
        except Exception:
            pass

    def _is_recording_active() -> bool:
        with recorder.lock:
            return recorder.proc is not None

    def refresh_recording_overlay():
        nonlocal overlay_visible, overlay_mode, llm_alert_active_until, llm_alert_pending_text
        nonlocal llm_alert_pending_ts, llm_alert_last_shown_ts
        active = _is_recording_active()
        now = time.monotonic()
        if show_settings_gui and root.winfo_viewable():
            root.title(base_title)
        try:
            record_btn.configure(
                text=tr("Aufnahme stoppen", "Stop recording") if active else tr("Aufnahme starten", "Start recording")
            )
        except Exception:
            pass
        if overlay is not None:
            try:
                if active:
                    if recording_overlay_enabled:
                        if not overlay_visible:
                            front_target = _get_last_focus_target()
                            if front_target is None:
                                candidate = _get_frontmost_focus_target()
                                if candidate is not None and candidate.pid != APP_PID:
                                    front_target = candidate
                            if front_target is not None and front_target.pid != APP_PID:
                                _set_last_focus_target(front_target)
                            _set_overlay_appearance("recording", tr("● AUFNAHME AKTIV", "● RECORDING"))
                            overlay.deiconify()
                            overlay.lift()
                            overlay.attributes("-topmost", True)
                            overlay_visible = True
                            overlay_mode = "recording"
                            _reactivate_focus_target(front_target, delay_ms=FOCUS_RESTORE_DELAY_MS)
                        else:
                            if overlay_mode != "recording":
                                _set_overlay_appearance("recording", tr("● AUFNAHME AKTIV", "● RECORDING"))
                                overlay_mode = "recording"
                            # Keep the banner above the active app without activating it.
                            overlay.lift()
                            overlay.attributes("-topmost", True)
                    elif overlay_visible:
                        # Recording has priority; hide non-recording overlay while active.
                        overlay.withdraw()
                        overlay_visible = False
                        overlay_mode = "hidden"
                else:
                    if (
                        llm_alert_overlay_enabled
                        and llm_alert_pending_text
                        and llm_alert_pending_ts > llm_alert_last_shown_ts
                    ):
                        _set_overlay_appearance("llm_alert", llm_alert_pending_text)
                        overlay.deiconify()
                        overlay.lift()
                        overlay.attributes("-topmost", True)
                        overlay_visible = True
                        overlay_mode = "llm_alert"
                        llm_alert_active_until = now + llm_alert_duration_sec
                        llm_alert_last_shown_ts = llm_alert_pending_ts
                        llm_alert_pending_text = ""
                        llm_alert_pending_ts = 0.0
                    elif overlay_mode == "llm_alert" and overlay_visible:
                        if now >= llm_alert_active_until:
                            front_target = _get_last_focus_target()
                            overlay.withdraw()
                            overlay_visible = False
                            overlay_mode = "hidden"
                            _reactivate_focus_target(front_target, delay_ms=FOCUS_RESTORE_DELAY_MS)
                        else:
                            overlay.lift()
                            overlay.attributes("-topmost", True)
                    elif overlay_visible:
                        front_target = _get_last_focus_target()
                        overlay.withdraw()
                        overlay_visible = False
                        overlay_mode = "hidden"
                        _reactivate_focus_target(front_target, delay_ms=FOCUS_RESTORE_DELAY_MS)
            except Exception:
                pass
        root.after(250, refresh_recording_overlay)

    def _first_run_checks() -> None:
        """Missing Homebrew tools or no speech model: tell the user what to do."""
        missing = missing_external_tools(_model_engine(MODEL))
        any_model = any(_whisper_model_installed(key) for key in WHISPER_MODEL_CATALOG)
        cfg = _load_json_config(CONFIG_PATH)
        offer_model = not any_model and str(cfg.get("FIRST_RUN_MODEL_OFFERED") or "") != "1"
        if not missing and not offer_model:
            return
        show_settings_window()
        if missing:
            messagebox.showwarning(
                tr("Programme fehlen", "Programs missing"),
                missing_tools_message(missing),
                parent=root,
            )
        if offer_model:
            _update_config_key(CONFIG_PATH, "FIRST_RUN_MODEL_OFFERED", "1")
            spec = WHISPER_MODEL_CATALOG["large-turbo"]
            if messagebox.askyesno(
                tr("Sprachmodell laden?", "Download a speech model?"),
                tr(
                    "Es ist noch kein Sprachmodell installiert.\n\n"
                    f"Jetzt {spec.label} laden ({format_size(spec.size_bytes)})? "
                    "Die App prüft die Datei und schaltet das Modell danach ein.\n\n"
                    "Andere Modelle gibt es im Menü „Whisper-Modell“.",
                    "No speech model is installed yet.\n\n"
                    f"Download {spec.label} now ({format_size(spec.size_bytes)})? "
                    "The app verifies the file and switches to the model afterwards.\n\n"
                    "Other models are in the \"Whisper model\" menu.",
                ),
                parent=root,
            ):
                open_whisper_models_window()
                start_whisper_model_download("large-turbo", activate_on_success=True, warn_missing_tools=False)

    try:
        root.after(1500 if needs_onboarding else 600, _first_run_checks)
    except Exception:
        pass

    apply_mode()
    apply_input_language()
    apply_output_language()
    apply_word_limits()
    _refresh_model_widget()
    apply_provider_selection()
    _refresh_audio_input_status_async()
    refresh_recording_overlay()
    _refresh_llm_runtime_status_label()
    _refresh_llm_change_button()

    # If the app started WITHOUT Accessibility, the keyboard event tap was not
    # created (to avoid the Input-Monitoring prompt). Poll for the grant and
    # relaunch once it lands, so Fn+Fn starts working without a manual restart.
    if IS_FROZEN and _accessibility_is_trusted() is not True:
        def _poll_for_accessibility_grant() -> None:
            if _accessibility_is_trusted() is True:
                if _relaunch_app():
                    os._exit(0)
                return
            try:
                root.after(1500, _poll_for_accessibility_grant)
            except Exception:
                pass
        try:
            root.after(2000, _poll_for_accessibility_grant)
        except Exception:
            pass

    try:
        root.mainloop()
    except KeyboardInterrupt:
        on_close()
    return True


def is_gui_available() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


# ---- Paste into frontmost app ---------------------------------------------
def read_clipboard_text() -> str:
    if sys.platform != "darwin":
        return ""
    text = _get_clipboard()
    return text if text is not None else ""


def paste_into_front_app(text: str):
    global LAST_PASTED_LEN, LAST_PASTE_TEXT, LAST_PASTE_TS, LAST_PASTE_SUPPRESSED_COUNT
    global LAST_RECOGNIZED_TEXT
    if not text:
        return
    # Safety: remove trailing whitespace so hidden final linebreaks cannot execute commands in terminals.
    safe_text = text.rstrip()
    if not safe_text:
        return
    # Remember the processed text even if the paste below lands nowhere useful,
    # so the GUI "copy" button can recover it.
    LAST_RECOGNIZED_TEXT = safe_text

    with PASTE_LOCK:
        target = _get_frontmost_focus_target()
        target_name = target.app_name if target is not None else "(unknown)"
        target_pid = target.pid if target is not None else -1
        now = time.monotonic()
        if (
            PASTE_DEDUP_WINDOW_MS > 0
            and safe_text == LAST_PASTE_TEXT
            and (now - LAST_PASTE_TS) * 1000.0 <= PASTE_DEDUP_WINDOW_MS
        ):
            LAST_PASTE_SUPPRESSED_COUNT += 1
            LAST_PASTE_TS = now
            print(f"🛡️ Suppressing duplicate paste x{LAST_PASTE_SUPPRESSED_COUNT} (window={PASTE_DEDUP_WINDOW_MS}ms)")
            return

        # Button-started recordings keep the dictated text on the clipboard so
        # the user can paste it manually; restore only for the Fn+Fn flow.
        restore_clipboard = PASTE_RESTORE_CLIPBOARD and not _RECORDING_VIA_BUTTON
        clipboard_snapshot = _capture_clipboard_snapshot() if restore_clipboard else None
        previous_text = _get_clipboard() if restore_clipboard and clipboard_snapshot is None else None
        if not _set_clipboard(safe_text):
            print("⚠️ Paste failed: could not set the clipboard.")
            return
        secure = _secure_input_active()
        print(
            f"📋 Paste attempt: target={target_name} pid={target_pid} len={len(safe_text)} "
            f"restore={'on' if restore_clipboard else 'off'} delay={PASTE_CLIPBOARD_RESTORE_DELAY_MS}ms "
            f"secure_input={secure}"
        )
        if secure is True:
            print(
                "⚠️ Secure Keyboard Entry is active — macOS blocks synthetic "
                "keystrokes. The text is on the clipboard; press Cmd+V "
                "manually, or disable Secure Keyboard Entry in the target app."
            )
        if not _cg_post_key(_VK_V, command=True):
            print("⚠️ Paste failed: CGEvent synthesis unavailable or blocked.")
            if restore_clipboard:
                if clipboard_snapshot is not None:
                    _restore_clipboard_snapshot(clipboard_snapshot)
                elif previous_text is not None:
                    _set_clipboard(previous_text)
            return
        if restore_clipboard:
            delay_sec = max(0, PASTE_CLIPBOARD_RESTORE_DELAY_MS) / 1000.0
            if delay_sec > 0:
                time.sleep(delay_sec)
            restored = False
            if clipboard_snapshot is not None:
                restored = _restore_clipboard_snapshot(clipboard_snapshot)
            elif previous_text is not None:
                restored = _set_clipboard(previous_text)
            if not restored:
                print("⚠️ Could not fully restore the clipboard after pasting.")
        LAST_PASTED_LEN = len(safe_text)
        LAST_PASTE_TEXT = safe_text
        LAST_PASTE_TS = now
        LAST_PASTE_SUPPRESSED_COUNT = 0


def delete_last_paste(count: Optional[int] = None):
    """Send backspaces to delete the last pasted text.

    Serialised against paste_into_front_app() via PASTE_LOCK so that a Ctrl+X
    right after a paste cannot read a stale LAST_PASTED_LEN.
    """
    global LAST_PASTED_LEN
    with PASTE_LOCK:
        n = count if (count is not None and count > 0) else LAST_PASTED_LEN
        if not n:
            return
        if not _cg_post_key_repeat(_VK_DELETE, n):
            print("⚠️ Delete failed: CGEvent synthesis unavailable or blocked.")
            return
        LAST_PASTED_LEN = 0


def _normalize_command_text(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = t.replace('-', ' ')
    t = re.sub(r'^["“”‚‘`\']+|["“”‚‘`\']+$', '', t)
    t = re.sub(r'[\s\.!?;:,…]+$', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t


def _try_clipboard_instruction_flow(spoken_text: str) -> bool:
    if AUTO_CORRECT_MODE != CLIPBOARD_INSTRUCTION_MODE:
        return False

    instruction = (spoken_text or "").strip()
    if not instruction:
        print("📋 Clipboard workflow: no instruction recognized.")
        return True

    clipboard_text = read_clipboard_text().strip()
    if not clipboard_text:
        print("📋 Clipboard workflow: clipboard is empty.")
        return True

    if not is_llm_enabled():
        print("📋 Clipboard workflow: LLM is not available for this mode.")
        return True

    canonical = get_name_canonical()
    include_context = _is_context_processing_enabled()
    if include_context:
        _prepare_context_for_post_processing(canonical)

    def _apply_instruction() -> str:
        return apply_spoken_instruction_to_text(
            source_text=clipboard_text,
            spoken_instruction=instruction,
            name_canonical=canonical,
            include_context=include_context,
        )

    rewritten = _run_with_llm_keyboard_heartbeat(_apply_instruction)
    final_text = post_llm_cleanup(rewritten, canonical, None) if rewritten else ""
    if not final_text:
        print("⚠️ Clipboard transformation returned no text; using normal dictation.")
        return False

    print(f"📋 Clipboard workflow: {len(clipboard_text)} characters of source text + spoken instruction")
    paste_into_front_app(final_text)
    _append_post_processing_output_to_context(final_text)
    return True


# Voice commands only fire when they are the whole utterance (after
# _normalize_command_text), so normal dictation is never misread. German and
# English are accepted regardless of the transcription language.
DELETE_COMMANDS = frozenset({
    # German
    "löschen",
    "alles löschen",
    "text löschen",
    "das löschen",
    "lösche",
    "lösche das",
    # English
    "delete",
    "delete that",
    "delete this",
    "delete it",
    "delete all",
    "delete everything",
    "delete text",
    "delete the text",
    "scratch that",
})

UNDO_COMMANDS = frozenset({
    # German
    "rückgängig",
    "mach rückgängig",
    "rückgängig machen",
    "mach das rückgängig",
    # English
    "undo",
    "undo that",
    "undo this",
    "undo it",
})


def is_delete_command(text: str) -> bool:
    return _normalize_command_text(text) in DELETE_COMMANDS


def undo_last_action(times: Optional[int] = None):
    """Trigger Cmd+Z one or multiple times (default: once)."""
    n = times if (times is not None and times > 0) else 1
    if not _cg_post_key_repeat(_VK_Z, n, command=True):
        print("⚠️ Undo failed: CGEvent synthesis unavailable or blocked.")


def is_undo_command(text: str) -> bool:
    return _normalize_command_text(text) in UNDO_COMMANDS


def get_frontmost_app_name() -> Optional[str]:
    target = _get_frontmost_focus_target()
    if target is None:
        return None
    return target.app_name or None


def app_prefers_backspace(app_name: Optional[str]) -> bool:
    if not app_name:
        return False
    name = app_name.lower()
    terminal_like = (
        "terminal" in name or
        "iterm" in name or
        "warp" in name or
        "alacritty" in name or
        "kitty" in name
    )
    return terminal_like


def undo_or_delete_last():
    app = get_frontmost_app_name()
    if app_prefers_backspace(app):
        delete_last_paste()
    else:
        undo_last_action()


def press_enter(times: Optional[int] = None):
    """Press Enter (Return) key one or more times."""
    n = times if (times is not None and times > 0) else 1
    print(f"↩️  Pressing Enter x{n}")
    if not _cg_post_key_repeat(_VK_RETURN, n):
        print("⚠️ Enter failed: CGEvent synthesis unavailable or blocked.")


def _is_llm_keyboard_heartbeat_enabled() -> bool:
    raw = os.environ.get("LLM_KEYBOARD_HEARTBEAT")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_whisper_keyboard_heartbeat_enabled() -> bool:
    raw = os.environ.get("WHISPER_KEYBOARD_HEARTBEAT")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _llm_keyboard_heartbeat_interval_sec() -> float:
    raw = (os.environ.get("LLM_KEYBOARD_HEARTBEAT_INTERVAL_SEC") or "").strip()
    if not raw:
        return 0.7
    try:
        value = float(raw)
    except Exception:
        return 0.7
    return max(0.2, min(5.0, value))


def _llm_keyboard_heartbeat_sequence() -> list[str]:
    raw = (os.environ.get("LLM_KEYBOARD_HEARTBEAT_SEQUENCE") or "*+-+").strip()
    seq = [ch for ch in raw if not ch.isspace()]
    return seq or ["*", "+", "-", "+"]


def _llm_keyboard_heartbeat_visible_ms() -> int:
    raw = (os.environ.get("LLM_KEYBOARD_HEARTBEAT_VISIBLE_MS") or "").strip()
    if not raw:
        return 90
    try:
        value = int(raw)
    except Exception:
        return 90
    return max(10, min(2000, value))


def _send_keyboard_heartbeat_pulse(ch: str) -> None:
    if sys.platform != "darwin":
        return
    key = (ch or "*")[0]
    if key in {'"', "'"}:
        key = "*"
    if not _cg_type_char(key):
        return
    delay_sec = _llm_keyboard_heartbeat_visible_ms() / 1000.0
    if delay_sec > 0:
        time.sleep(delay_sec)
    _cg_post_key(_VK_DELETE)


def _run_with_keyboard_heartbeat(fn: Callable[[], str], *, enabled: bool, announce: Optional[str] = None) -> str:
    if not enabled or sys.platform != "darwin":
        return fn()

    stop_event = threading.Event()
    seq = _llm_keyboard_heartbeat_sequence()
    interval_sec = _llm_keyboard_heartbeat_interval_sec()
    visible_ms = _llm_keyboard_heartbeat_visible_ms()
    if announce:
        print(f"💓 {announce} keyboard heartbeat active: seq={''.join(seq)} interval={interval_sec:.2f}s visible_ms={visible_ms}")

    def _worker() -> None:
        idx = 0
        while not stop_event.is_set():
            _send_keyboard_heartbeat_pulse(seq[idx % len(seq)])
            idx += 1
            if stop_event.wait(interval_sec):
                break

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop_event.set()
        # Hard barrier: heartbeat must be fully stopped before caller can paste output.
        t.join()


def _run_with_llm_keyboard_heartbeat(fn: Callable[[], str]) -> str:
    return _run_with_keyboard_heartbeat(
        fn,
        enabled=_is_llm_keyboard_heartbeat_enabled(),
        announce="LLM",
    )


def _run_with_whisper_keyboard_heartbeat(fn: Callable[[], str]) -> str:
    return _run_with_keyboard_heartbeat(
        fn,
        enabled=_is_whisper_keyboard_heartbeat_enabled(),
    )


# Enter command phrases. German: "(drücke) (die) Enter(taste)", "Eingabetaste".
# English: "enter (key)", "press/hit (the) enter (key)", "press/hit (the) return
# (key)" - bare "return" is never a command because it is a common word.
# The whole pattern is one group because ENTER_SPLIT_RE and
# strip_trailing_enter_command() embed it between other expressions.
ENTER_PHRASE_RE = re.compile(
    r'(?:'
    r'(?:(?:dr(?:ü|u)cke(?:n)?\s+)?(?:die\s+)?|\b(?:press|hit)\s+(?:the\s+)?)'
    r'(?:\benter\b(?:\s*(?:taste|key))?|\beingabe\b\s*(?:taste|key)|\b(?:entertaste|eingabetaste)\b)'
    r'|\b(?:press|hit)\s+(?:the\s+)?return\b(?:\s+key\b)?'
    r')',
    re.IGNORECASE
)

# Inside a sentence a bare "enter" is an ordinary English verb ("please enter
# your name"), so outside German dictation an inline/trailing Enter command
# needs a verb ("press/hit enter", "drücke Enter") or "key"/"Taste".
ENTER_PHRASE_STRICT_RE = re.compile(
    r'(?:'
    r'(?:\bdr(?:ü|u)cke(?:n)?\s+(?:die\s+)?|\b(?:press|hit)\s+(?:the\s+)?)'
    r'(?:\benter\b(?:\s*(?:taste|key))?|\beingabe\b\s*(?:taste|key)|\b(?:entertaste|eingabetaste)\b)'
    r'|\benter\s*(?:taste|key)\b|\beingabe\s*(?:taste|key)\b|\b(?:entertaste|eingabetaste)\b'
    r'|\b(?:press|hit)\s+(?:the\s+)?return\b(?:\s+key\b)?'
    r')',
    re.IGNORECASE
)

_ENTER_LEAD = r'[\s\"“”‚‘\'\`\(\[\{]*'
_ENTER_TRAIL = r'[\s\.!?;:,…\"“”‚‘\'\`)\]]*'
ENTER_SPLIT_RE = re.compile(_ENTER_LEAD + ENTER_PHRASE_RE.pattern + _ENTER_TRAIL, re.IGNORECASE)
ENTER_SPLIT_STRICT_RE = re.compile(_ENTER_LEAD + ENTER_PHRASE_STRICT_RE.pattern + _ENTER_TRAIL, re.IGNORECASE)


def _inline_enter_phrase_re() -> re.Pattern:
    lang = (os.environ.get("WHISPER_LANGUAGE") or LANGUAGE or "").strip().lower()
    return ENTER_PHRASE_RE if lang == "de" else ENTER_PHRASE_STRICT_RE


def _inline_enter_split_re() -> re.Pattern:
    return ENTER_SPLIT_RE if _inline_enter_phrase_re() is ENTER_PHRASE_RE else ENTER_SPLIT_STRICT_RE


def is_enter_command(text: str) -> bool:
    t = _normalize_command_text(text)
    return bool(ENTER_PHRASE_RE.fullmatch(t))


def strip_trailing_enter_command(text: str):
    if not text:
        return text, False
    m = re.search(
        _inline_enter_phrase_re().pattern + _ENTER_TRAIL + r'$',
        text,
        re.IGNORECASE
    )
    if not m:
        return text, False
    return text[:m.start()].rstrip(), True


def _dispatch_enter_deferred(n: int):
    if n <= 0:
        return
    delay_ms = int(os.environ.get("ENTER_DELAY_MS", "80")) if _llm_used_for_mode(AUTO_CORRECT_MODE) else 0
    if delay_ms > 0:
        print(f"⏳ Waiting {delay_ms}ms before Enter x{n} (LLM gating)")
        time.sleep(delay_ms / 1000.0)
    press_enter(n)


def _allow_enter_after_paste() -> bool:
    # Hard safety rule: never synthesize Enter after a post-processed paste.
    return AUTO_CORRECT_MODE == 0


def _paste_postprocessed(segment: str):
    if not segment:
        return
    if AUTO_CORRECT_MODE == 0:
        _update_last_llm_change(segment, segment)
        paste_into_front_app(segment.strip())
        return
    canonical = get_name_canonical()
    if _llm_used_for_mode(AUTO_CORRECT_MODE):
        _prepare_context_for_post_processing(canonical)
    raw_out = post_process(segment, canonical)
    if _llm_used_for_mode(AUTO_CORRECT_MODE):
        refined = _run_with_llm_keyboard_heartbeat(lambda: maybe_llm_refine(raw_out, canonical, AUTO_CORRECT_MODE))
    else:
        refined = maybe_llm_refine(raw_out, canonical, AUTO_CORRECT_MODE)
    _update_last_llm_change(raw_out, refined)
    print(f"🧽 Post-processed: {raw_out}")
    if refined != raw_out:
        print(f"🧹 LLM refined: {refined}")
    final_text = post_llm_cleanup(refined, canonical, AUTO_CORRECT_MODE) if refined else post_llm_cleanup(raw_out, canonical, AUTO_CORRECT_MODE)
    if final_text:
        paste_into_front_app(final_text)
        _append_post_processing_output_to_context(final_text)


def handle_inline_enter_commands(text: str) -> bool:
    if not text:
        return False
    pos = 0
    enter_count = 0
    segments: list[str] = []
    for m in _inline_enter_split_re().finditer(text):
        seg = text[pos:m.start()].strip()
        if seg:
            segments.append(seg)
        enter_count += 1
        pos = m.end()
    if enter_count == 0:
        return False
    tail = text[pos:].strip()
    if tail:
        segments.append(tail)
    merged = " ".join(segments).strip()
    if merged:
        _paste_postprocessed(merged)
    print(f"↩️  Enter command detected (inline) x{enter_count}")
    if _allow_enter_after_paste():
        _dispatch_enter_deferred(enter_count)
    else:
        print(f"🛡️ Suppressing Enter x{enter_count} after post-processing")
    return True


# ---- Stop & process pipeline ----------------------------------------------
def stop_and_process(reason: str = "manual"):
    if reason == "timeout":
        print(f"⏱️ Auto-stop: reached {MAX_RECORDING_SECONDS:.0f}s limit.")
    path = recorder.stop()
    if not path:
        return
    try:
        peak, rms = _wav_peak_and_rms(path)
        print(f"🎚️ WAV level: peak={peak:.6f} rms={rms:.6f}")
        if peak <= 0.001:
            print(
                "⚠️ Microphone level extremely low (near silence). "
                "Please check the microphone permission and input source."
            )
        raw_text = transcribe(path)
        save_raw_transcript(path, raw_text)
        try:
            print(f"📝 Transcript: {raw_text}")
        except Exception:
            pass
        if is_undo_command(raw_text):
            undo_or_delete_last()
        elif is_delete_command(raw_text):
            delete_last_paste()
        elif is_enter_command(raw_text):
            print("↩️  Enter command detected (single)")
            press_enter()
        else:
            if _try_clipboard_instruction_flow(raw_text):
                return
            if not handle_inline_enter_commands(raw_text):
                base_text, wants_enter = strip_trailing_enter_command(raw_text)
                _paste_postprocessed(base_text)
                if wants_enter:
                    if _allow_enter_after_paste():
                        print("↩️  Enter command detected (trailing)")
                        press_enter()
                    else:
                        print("🛡️ Suppressing trailing Enter after post-processing")
    finally:
        try:
            prune_old_recordings()
        except Exception as e:
            print(f"⚠️ Recording cleanup failed: {e}")


# auto-stop callback wiring (installed after definition)
recorder.on_auto_stop = lambda: stop_and_process("timeout")


# ---- Double-tap handling ---------------------------------------------------
def handle_double_tap(label: str):
    # Run in a background thread so the event-tap callback returns immediately.
    # stop_and_process() transcribes for several seconds; blocking the callback
    # that long makes macOS disable the event tap (kCGEventTapDisabledByTimeout),
    # which silently drops the next Fn+Fn after a STOP.
    threading.Thread(
        target=_handle_double_tap_impl, args=(label,), daemon=True
    ).start()


def _handle_double_tap_impl(label: str):
    global _RECORDING_VIA_BUTTON
    manage_focus = SETTINGS_GUI_ROOT is not None
    if manage_focus:
        front_target = _get_frontmost_focus_target()
        if front_target is not None and front_target.pid != APP_PID:
            _set_last_focus_target(front_target)
        _hide_settings_gui_window()
    if recorder.proc is None:
        print(f"🔑 Detected double {label} → START")
        _RECORDING_VIA_BUTTON = False
        recorder.start()
        if manage_focus:
            for delay_ms in (0, FOCUS_RESTORE_DELAY_MS, 250):
                _reactivate_focus_target(_get_last_focus_target(), delay_ms=delay_ms)
        return

    print(f"🔑 Detected double {label} → STOP")
    if manage_focus:
        current_target = _get_frontmost_focus_target()
        if current_target is not None and current_target.pid != APP_PID:
            _set_last_focus_target(current_target)
        else:
            _reactivate_focus_target(_get_last_focus_target(), delay_ms=FOCUS_RESTORE_DELAY_MS)
    stop_and_process("manual")


def handle_ctrl_x_shortcut() -> None:
    if CTRL_X_DELETE_COUNT <= 0:
        return
    print(f"⌃X detected → Backspace x{CTRL_X_DELETE_COUNT}")
    delete_last_paste(CTRL_X_DELETE_COUNT)


# ---- Single-instance lock --------------------------------------------------
SINGLE_INSTANCE_LOCK_PATH = APP_SUPPORT_DIR / "whisper_dictate.lock"
_SINGLE_INSTANCE_LOCK_FH = None  # keep the fd open for the lifetime of the process


def _read_lockfile_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        pid = int(raw.split()[0])
    except (ValueError, IndexError):
        return None
    return pid if pid > 0 else None


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it; treat as alive.
        return True
    except OSError:
        return False
    return True


def acquire_single_instance_lock() -> bool:
    """Return True if this process became the sole instance.

    Acquires an exclusive non-blocking fcntl.flock on a file under APP_SUPPORT_DIR.
    The file descriptor is parked in a module-level global so the lock survives
    until the process exits (the kernel releases it automatically on exit).
    If another instance already holds the lock, prints a hint and returns False.
    """
    global _SINGLE_INSTANCE_LOCK_FH
    try:
        import fcntl
    except ImportError:
        # Non-POSIX platform; skip the guard rather than block startup.
        return True

    try:
        SINGLE_INSTANCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(SINGLE_INSTANCE_LOCK_PATH, "a+")
    except OSError as e:
        print(f"⚠️ Could not open the lock file ({SINGLE_INSTANCE_LOCK_PATH}): {e}")
        return True  # fail open: don't block startup if the FS misbehaves

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        other_pid = _read_lockfile_pid(SINGLE_INSTANCE_LOCK_PATH)
        if other_pid is not None and _process_is_alive(other_pid):
            print(
                f"❌ Another instance of whisper_dictate.py is already running "
                f"(PID {other_pid}). Exiting this instance so Fn+Fn "
                f"does not trigger twice."
            )
            print(
                "Hint: stop all of them with 'pkill -f whisper_dictate.py' "
                "and start exactly one again."
            )
        else:
            print(
                "❌ The lock file is held, but no running PID was found. "
                f"Please check the lock file: {SINGLE_INSTANCE_LOCK_PATH}"
            )
        try:
            fh.close()
        except Exception:
            pass
        return False

    # We hold the lock. Write our PID for diagnostics in the lock file.
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass

    _SINGLE_INSTANCE_LOCK_FH = fh

    import atexit

    def _release_lock() -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        try:
            SINGLE_INSTANCE_LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_release_lock)
    return True


# ---- Autostart (LaunchAgent) management ------------------------------------
DEFAULT_LAUNCH_AGENT_LABEL = "io.github.whisperdictate"


def _launch_agent_label() -> str:
    """LaunchAgent label: env override, else the app bundle's CFBundleIdentifier."""
    override = (os.environ.get("WHISPER_LAUNCH_AGENT_LABEL") or "").strip()
    if override:
        return override
    if IS_FROZEN:
        try:
            import plistlib

            exe = Path(sys.executable).resolve()
            bundle = next((p for p in exe.parents if p.suffix == ".app"), None)
            if bundle is not None:
                with (bundle / "Contents" / "Info.plist").open("rb") as fh:
                    ident = str(plistlib.load(fh).get("CFBundleIdentifier") or "").strip()
                if ident:
                    return ident
        except Exception:
            pass
        try:
            from Foundation import NSBundle

            ident = str(NSBundle.mainBundle().bundleIdentifier() or "").strip()
            if ident:
                return ident
        except Exception:
            pass
    return DEFAULT_LAUNCH_AGENT_LABEL


AUTOSTART_LABEL = _launch_agent_label()


def _autostart_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AUTOSTART_LABEL}.plist"


# launchd starts agents with a minimal PATH; sox and whisper-cpp live in Homebrew.
_AUTOSTART_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _source_run_python() -> Path:
    """Python the LaunchAgent uses for a source checkout: the project .venv."""
    venv_python = MY_DIR / ".venv" / "bin" / "python"
    return venv_python if venv_python.exists() else Path(sys.executable)


def autostart_permission_binary() -> Optional[Path]:
    """Program macOS asks permissions for when the LaunchAgent starts a source run.

    The .venv python is a symlink; privacy permissions belong to the real binary.
    """
    if IS_FROZEN:
        return None
    try:
        return _source_run_python().resolve()
    except OSError:
        return None


def _autostart_program_target() -> Optional[tuple[list[str], str]]:
    """Resolve (program_arguments, working_dir) for the LaunchAgent.

    Installed .app: Contents/MacOS/WhisperDictate inside the bundle.
    Source checkout (`uv run`): the project's .venv python with this script,
    so login does not depend on uv.
    """
    if not IS_FROZEN:
        script = Path(__file__).resolve()
        return ([str(_source_run_python()), str(script)], str(script.parent))
    exe = Path(sys.executable)
    bundle: Optional[Path] = None
    for candidate in [exe, *exe.parents]:
        if candidate.suffix == ".app":
            bundle = candidate
            break
    if bundle is None:
        return None
    program_path = bundle / "Contents" / "MacOS" / "WhisperDictate"
    if not program_path.exists():
        return None
    return ([str(program_path)], str(bundle))


def autostart_is_enabled() -> bool:
    return _autostart_plist_path().exists()


def autostart_enable() -> tuple[bool, str]:
    """Write the LaunchAgent plist and bootstrap it. Returns (success, message)."""
    target = _autostart_program_target()
    if target is None:
        return (False, tr("Autostart: Programmpfad der App nicht gefunden.", "Autostart: could not find the app's program path."))
    program_args, working_dir = target

    plist_path = _autostart_plist_path()
    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (False, tr(f"LaunchAgents-Ordner nicht erreichbar: {e}", f"LaunchAgents folder not accessible: {e}"))

    log_dir = Path.home() / "Library" / "Logs" / "WhisperDictate"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        import plistlib
    except ImportError:
        return (False, tr("plistlib nicht verfügbar.", "plistlib not available."))

    payload = {
        "Label": AUTOSTART_LABEL,
        "ProgramArguments": program_args,
        "WorkingDirectory": working_dir,
        "EnvironmentVariables": {"PATH": _AUTOSTART_PATH},
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": ["Aqua"],
        "StandardOutPath": str(log_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd.stderr.log"),
    }
    try:
        with plist_path.open("wb") as fh:
            plistlib.dump(payload, fh, sort_keys=False)
    except OSError as e:
        return (False, tr(f"plist konnte nicht geschrieben werden: {e}", f"Could not write the plist: {e}"))

    uid = os.getuid()
    domain = f"gui/{uid}"
    if not IS_FROZEN:
        # Source run: launchd picks the plist up at the next login. Bootstrapping
        # now would start a second instance next to the one already running.
        return (True, tr("Autostart aktiviert (ab der nächsten Anmeldung).", "Autostart enabled (from the next login)."))
    try:
        # Boot out a previous version first; ignore failure (no prior install).
        subprocess.run(
            [LAUNCHCTL, "bootout", domain, str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            [LAUNCHCTL, "bootstrap", domain, str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return (False, tr(f"launchctl bootstrap fehlgeschlagen: {err}", f"launchctl bootstrap failed: {err}"))
        subprocess.run(
            [LAUNCHCTL, "enable", f"{domain}/{AUTOSTART_LABEL}"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return (False, tr(f"launchctl-Aufruf fehlgeschlagen: {e}", f"launchctl call failed: {e}"))
    except Exception as e:
        return (False, tr(f"Unerwarteter Fehler beim launchctl-Aufruf: {e}", f"Unexpected error calling launchctl: {e}"))

    return (True, tr("Autostart aktiviert.", "Autostart enabled."))


def autostart_disable() -> tuple[bool, str]:
    plist_path = _autostart_plist_path()
    if not plist_path.exists():
        return (True, tr("Autostart war nicht aktiviert.", "Autostart was not enabled."))
    uid = os.getuid()
    domain = f"gui/{uid}"
    try:
        subprocess.run(
            [LAUNCHCTL, "bootout", domain, str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    try:
        plist_path.unlink(missing_ok=True)
    except OSError as e:
        return (False, tr(f"plist konnte nicht gelöscht werden: {e}", f"Could not delete the plist: {e}"))
    return (True, tr("Autostart deaktiviert.", "Autostart disabled."))


# ---- First-run onboarding --------------------------------------------------
ONBOARDING_MARKER_PATH = APP_SUPPORT_DIR / ".onboarding_complete"


def onboarding_should_run() -> bool:
    """Run onboarding on first launch, or whenever the critical permission
    (Accessibility) is missing — e.g. after a reinstall with a
    new signature or a TCC reset, where the marker may still exist but the grant
    is gone. Keying off the live permission (not just the marker) avoids the
    stuck state: no onboarding shown, yet no permission granted."""
    if not IS_FROZEN:
        # Dev runs never need onboarding — the developer knows the score.
        return False
    if _accessibility_is_trusted() is True:
        # All set (Accessibility also covers Input Monitoring). Don't nag again.
        if not ONBOARDING_MARKER_PATH.exists():
            onboarding_mark_complete()
        return False
    # Accessibility missing → show onboarding regardless of the marker.
    return True


def onboarding_mark_complete() -> None:
    try:
        ONBOARDING_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        ONBOARDING_MARKER_PATH.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    except OSError:
        pass


def _accessibility_is_trusted() -> Optional[bool]:
    """Return True if Accessibility is granted, False if not, None if we can't
    tell."""
    if not HAS_QUARTZ:
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def _input_monitoring_is_trusted() -> Optional[bool]:
    """Return True if Input Monitoring is granted, False if
    denied, None if unknown. Uses IOKit's IOHIDCheckAccess via ctypes."""
    try:
        import ctypes
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        # kIOHIDRequestTypeListenEvent = 1; 0=granted, 1=denied, 2=unknown
        status = iokit.IOHIDCheckAccess(1)
        if status == 0:
            return True
        if status == 1:
            return False
        return None
    except Exception:
        return None


def _request_input_monitoring_access() -> Optional[bool]:
    """Trigger the macOS Input Monitoring consent dialog and
    register the app in the list, via IOKit's IOHIDRequestAccess. Counterpart to
    AXIsProcessTrustedWithOptions for Accessibility. Returns True/False/None."""
    try:
        import ctypes
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDRequestAccess.restype = ctypes.c_bool
        iokit.IOHIDRequestAccess.argtypes = [ctypes.c_int]
        # kIOHIDRequestTypeListenEvent = 1
        return bool(iokit.IOHIDRequestAccess(1))
    except Exception as e:
        print(f"⚠️ IOHIDRequestAccess failed: {e}")
        return None


def _relaunch_app() -> bool:
    """Relaunch the bundled .app and signal the caller to exit. macOS only
    applies a freshly granted Accessibility permission to a *new* process, so
    after onboarding the running event tap is stale. A small waiter reopens the
    bundle once this process has exited (releasing the single-instance lock).
    No-op (returns False) outside the frozen app."""
    if not IS_FROZEN:
        return False
    try:
        exe = Path(sys.executable).resolve()
        app_path = next((p for p in exe.parents if p.suffix == ".app"), None)
        if app_path is None:
            return False
        pid = os.getpid()
        script = (
            f'while /bin/kill -0 {pid} 2>/dev/null; do sleep 0.2; done; '
            f'/usr/bin/open "{app_path}"'
        )
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        print(f"⚠️ Relaunch failed: {e}")
        return False


_ACC_PROMPT_SHOWN = False


def open_system_settings_pane(pane: str) -> None:
    """Open the relevant privacy settings for a permission."""
    global _ACC_PROMPT_SHOWN
    if pane == "accessibility":
        # First click: the native AX dialog registers the app in the
        # Accessibility list AND offers its own "Open System Settings" button —
        # so we do NOT also open the list URL (that would pop a second window).
        # Later clicks (dialog won't reappear once decided): open the list
        # directly so the user can still get there, e.g. after a denial.
        if not _ACC_PROMPT_SHOWN:
            _ACC_PROMPT_SHOWN = True
            try:
                from ApplicationServices import (
                    AXIsProcessTrustedWithOptions,
                    kAXTrustedCheckOptionPrompt,
                )
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
                return
            except Exception as e:
                print(f"⚠️ AXIsProcessTrustedWithOptions failed: {e}")
        # fall through to opening the list

    urls = {
        "microphone":    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "input":         "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    }
    url = urls.get(pane)
    if not url:
        return
    try:
        subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ Could not open System Settings ({pane}): {e}")


# ---- Main ------------------------------------------------------------------
def _init_log_file() -> None:
    """Redirect stdout/stderr to a log file. A windowed .app bundle has no
    console, so without this all diagnostics (paste errors, transcripts) are
    lost. Truncated per launch to avoid unbounded growth."""
    if not IS_FROZEN:
        return
    try:
        APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        log_path = APP_SUPPORT_DIR / "whisperdictate.log"
        fh = open(log_path, "w", buffering=1, encoding="utf-8")
        sys.stdout = fh
        sys.stderr = fh
        print(f"=== WhisperDictate Log — {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    except Exception:
        pass


def main():
    global AUTO_CORRECT_MODE

    _init_log_file()
    _maybe_reexec_into_venv()

    class _Formatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    mode_lines = ["Auto-correct modes:"]
    for mode, label in _ordered_mode_options():
        mode_lines.append(f"  {mode} = {label}")

    parser = argparse.ArgumentParser(
        description="Whisper dictate helper",
        formatter_class=_Formatter,
        epilog="\n".join(mode_lines),
    )
    env_default = os.environ.get("AUTO_CORRECT_MODE")
    try:
        env_default_int = int(env_default) if env_default is not None else AUTO_CORRECT_MODE
    except ValueError:
        env_default_int = AUTO_CORRECT_MODE
    allowed_modes = [mode for mode, _ in _ordered_mode_options()]
    if env_default_int not in allowed_modes:
        env_default_int = AUTO_CORRECT_MODE if AUTO_CORRECT_MODE in allowed_modes else (3 if 3 in allowed_modes else allowed_modes[0])
    parser.add_argument(
        "--auto-correct",
        type=_parse_auto_correct_arg,
        choices=allowed_modes,
        default=env_default_int,
        help="Post-processing mode (prompt modes + fixed special modes such as clipboard instruction, see below)",
    )
    parser.add_argument(
        "--letter-salutation",
        default=os.environ.get("LETTER_SALUTATION", ""),
        help="Default salutation for letter mode 4 (if the dictation contains none)",
    )
    parser.add_argument(
        "--letter-closing",
        default=os.environ.get("LETTER_CLOSING", ""),
        help="Closing phrase for letter mode 4 (optional; otherwise the prompt decides)",
    )
    parser.add_argument(
        "--letter-subject",
        default=os.environ.get("LETTER_SUBJECT", ""),
        help="Optional subject (used in the fallback layout; the LLM may use it as well)",
    )
    parser.add_argument(
        "--letter-signature-line",
        default=os.environ.get("LETTER_SIGNATURE_LINE", ""),
        help="Contact line for letter mode 4 (placed below the name, optional)",
    )
    parser.add_argument(
        "--model-size",
        choices=sorted(MODEL_FILES.keys()),
        default=DEFAULT_MODEL_SIZE,
        help=(
            "Speech model (large-turbo=recommended, whistle=very fast, large, "
            "medium=bundled, de-turbo=German fine-tune, writes ss instead of ß)"
        ),
    )
    parser.add_argument(
        "--list-recent-wavs",
        action="store_true",
        help="List the 5 most recent WAV files in the temp directory and exit",
    )
    parser.add_argument(
        "--transcribe-wav",
        type=Path,
        help="Transcribe a WAV file and exit (debug)",
    )
    parser.add_argument(
        "--transcribe-latest",
        action="store_true",
        help="Transcribe the newest WAV from the temp directory and exit (debug)",
    )
    args = parser.parse_args()
    AUTO_CORRECT_MODE = args.auto_correct
    if args.letter_salutation:
        os.environ["LETTER_SALUTATION"] = args.letter_salutation
    if args.letter_closing:
        os.environ["LETTER_CLOSING"] = args.letter_closing
    if args.letter_subject:
        os.environ["LETTER_SUBJECT"] = args.letter_subject
    if args.letter_signature_line:
        os.environ["LETTER_SIGNATURE_LINE"] = args.letter_signature_line
        _update_config_key(CONFIG_PATH, "LETTER_SIGNATURE_LINE", args.letter_signature_line)

    # Configure Whisper model path based on CLI size selection
    global MODEL
    requested_size = getattr(args, "model_size", DEFAULT_MODEL_SIZE)
    size = _resolve_model_size_or_fallback(requested_size)
    explicit_model_path = (os.environ.get("WHISPER_MODEL_PATH") or "").strip()
    if explicit_model_path:
        MODEL = str(_resolve_path_setting(explicit_model_path, MODEL_FILES[size], APP_SUPPORT_DIR))
    else:
        MODEL = str(MODEL_FILES[size])
    os.environ["WHISPER_MODEL_SIZE"] = size

    # Debug: list or transcribe wavs and exit
    if args.list_recent_wavs:
        wavs = list_recent_wavs(5)
        if wavs:
            print("Recent WAV files:")
            for p in wavs:
                stat = p.stat()
                print(f"- {p} | {stat.st_size} bytes | mtime={time.ctime(stat.st_mtime)}")
        else:
            print("No WAV files found.")
        if not args.transcribe_wav and not args.transcribe_latest:
            return
    if args.transcribe_wav or args.transcribe_latest:
        paths = []
        if args.transcribe_wav:
            paths.append(args.transcribe_wav)
        if args.transcribe_latest:
            latest = list_recent_wavs(1)
            if latest:
                paths.append(latest[0])
            else:
                print("No WAV files found for --transcribe-latest.")
        if paths:
            debug_transcribe([Path(p) for p in paths])
        return

    # Single-instance guard: prevent two listeners from grabbing the same
    # Fn+Fn event and double-triggering recording start/stop.
    if not acquire_single_instance_lock():
        sys.exit(1)

    # Only the instance holding the lock may (re)start the speech backend:
    # starting whisper-server also cleans up a stale server from a crashed run.
    _prepare_speech_backend(MODEL)
    import atexit

    atexit.register(WHISPER_SERVER.stop)

    # One-shot retention sweep on startup so stale recordings from previous
    # runs don't accumulate forever.
    try:
        prune_old_recordings()
    except Exception as e:
        print(f"⚠️ Recording cleanup at startup failed: {e}")

    if AUTO_CORRECT_MODE in {0, NO_AI_MODE}:
        print(f"🧹 LLM auto-correct: OFF (auto-correct mode {AUTO_CORRECT_MODE})")
    else:
        startup_llm_selftest()

    try:
        sox_path = shutil.which(SOX_CMD)
        whisper_path = shutil.which(WHISPER_CMD)
        model_path = Path(MODEL)
        model_exists = model_path.exists()
        enter_delay = int(os.environ.get("ENTER_DELAY_MS", "80")) if _llm_used_for_mode(AUTO_CORRECT_MODE) else 0
        afplay_path = AFPLAY if os.path.exists(AFPLAY) else None
        audio_input = get_current_audio_input_info()
        cg_ready = _cg_keyboard_available()
        pb_ready = _appkit_pasteboard_available()
        focus_ready = _appkit_focus_available()

        print(f"🎤 SoX: cmd={SOX_CMD} found={'yes' if sox_path else 'no'}")
        print(f"🎚️ Input: {audio_input['label']}")
        whisper_args_display = " ".join(WHISPER_ARGS) if WHISPER_ARGS else "(none)"
        print(
            f"🧠 Whisper: cmd={WHISPER_CMD} found={'yes' if whisper_path else 'no'} | args={whisper_args_display} "
            f"| model_size={size} | engine={_model_engine(model_path)} "
            f"| model_path={model_path} | model={'ok' if model_exists else 'missing'} "
            f"| server={'on' if WHISPER_SERVER_ENABLED else 'off'} "
            f"({WHISPER_SERVER_CMD}, found={'yes' if WHISPER_SERVER.available() else 'no'})"
        )
        missing_tools = missing_external_tools(_model_engine(model_path))
        if missing_tools:
            print(f"⚠️ {missing_tools_message(missing_tools)}")
        if not model_exists:
            print(f"⚠️ {no_speech_model_message()}")
        print(f"🐍 Python: {sys.executable}")
        print(f"🌐 Language (Whisper): {LANGUAGE} | UI: {ui_language()} (UI_LANGUAGE={UI_LANGUAGE_SETTING})")
        if detector is not None:
            print(f"⌨️ Hotkey: double-tap {detector.label} | window={DOUBLE_TAP_WINDOW:.2f}s")
        else:
            quartz_detail = f" ({QUARTZ_IMPORT_ERROR})" if not HAS_QUARTZ and QUARTZ_IMPORT_ERROR else ""
            print(f"⌨️ Hotkey: Quartz/detector not available{quartz_detail}")
        auto_stop_status = "off" if MAX_RECORDING_SECONDS <= 0 else f"{MAX_RECORDING_SECONDS:.0f}s limit"
        print(f"⏱️ Auto-Stop: {auto_stop_status}")
        print(f"🪟 GUI: {'visible' if _show_settings_gui() else 'hidden (SHOW_SETTINGS_GUI=0)'}")
        print(f"🧹 LLM auto-correct mode: {AUTO_MODE_LABELS.get(AUTO_CORRECT_MODE, AUTO_CORRECT_MODE)}")
        selected_provider, active_provider, active_model, provider_detail = _llm_startup_status_summary()
        print(f"🔌 LLM Provider: selected={selected_provider} | active={active_provider}")
        print(f"🧠 LLM model: {active_model}")
        print(f"🗂️ Credential providers: {provider_detail}")
        print(f"💾 Recording path: {TMP_DIR}")
        print(f"↩️ Enter gating: {'on' if _llm_used_for_mode(AUTO_CORRECT_MODE) else 'off'} | delay={enter_delay}ms")
        print(f"⌃X-Shortcut: delete={CTRL_X_DELETE_COUNT}")
        print(
            f"📋 Paste: cgevent={'ok' if cg_ready else 'missing'} | pasteboard={'ok' if pb_ready else 'missing'} "
            f"| focus={'ok' if focus_ready else 'missing'} | afplay={'ok' if afplay_path else 'missing'} "
            f"| dedupe_window={PASTE_DEDUP_WINDOW_MS}ms | restore_clipboard={'on' if PASTE_RESTORE_CLIPBOARD else 'off'} "
            f"| restore_delay={PASTE_CLIPBOARD_RESTORE_DELAY_MS}ms"
        )
        accessibility_ok = _accessibility_is_trusted()
        acc_label = {True: "yes", False: "NO", None: "unknown"}.get(accessibility_ok, "unknown")
        print(f"🔐 Accessibility (keystroke synthesis): {acc_label}")
        if accessibility_ok is not True:
            print("   ⚠️ Without Accessibility, macOS silently drops synthetic keystrokes")
            print("      — no paste, no heartbeat. Please grant it in onboarding.")
            # No native prompt here: the onboarding dialog handles the request.
            # Firing AXIsProcessTrustedWithOptions at startup would pop a second
            # window next to the onboarding.

        input_ok = _input_monitoring_is_trusted()
        in_label = {True: "yes", False: "NO", None: "unknown"}.get(input_ok, "unknown")
        print(f"🔐 Input Monitoring (Fn detection): {in_label}")
        # No separate request here: it is covered by the Accessibility grant,
        # and firing a second TCC prompt at startup competes with (and gets
        # swallowed by) the Accessibility prompt.
    except Exception:
        pass

    if not HAS_QUARTZ or detector is None:
        print("⚠️  Quartz / Fn detection not available.")
        print("Tip: Start/stop manually by calling recorder.start()/stop().")
        return

    # Only create the keyboard event tap when Accessibility is granted. Creating
    # it without permission triggers the macOS "Input Monitoring" prompt — but
    # the tap can't work anyway, and Accessibility already covers Input
    # Monitoring. Deferring it means the first run shows ONLY the native
    # Accessibility prompt. The GUI's restart-poll relaunches once granted.
    accessibility_ok = _accessibility_is_trusted() is True

    if _show_settings_gui():
        stop_event = threading.Event()
        listener_thread = None
        if accessibility_ok:
            listener_thread = threading.Thread(
                target=listen_for_double_tap_stoppable,
                args=(detector, handle_double_tap, stop_event),
                kwargs={
                    "on_ctrl_x": handle_ctrl_x_shortcut,
                },
                daemon=True,
            )
            listener_thread.start()
            print(f"✅ Listening for double-tap of {detector.label} (window {DOUBLE_TAP_WINDOW:.2f}s). The GUI hides on the first hotkey; quit with Ctrl+C.")
        else:
            print("⏸️ Fn detection paused until Accessibility is granted (the app then restarts automatically).")
        gui_ok = run_mode_selector_gui(stop_event)
        stop_event.set()
        if listener_thread is not None:
            listener_thread.join(timeout=1.5)
        if gui_ok:
            return
        print("⚠️  GUI could not be started. Falling back to no-GUI mode. Ctrl+C to quit.")

    if not accessibility_ok:
        print("⏸️ Fn detection inactive: please grant Accessibility and restart WhisperDictate.")
        return

    print(f"✅ Listening for double-tap of {detector.label} (window {DOUBLE_TAP_WINDOW:.2f}s). Ctrl+C to quit.")
    listen_for_double_tap(
        detector,
        handle_double_tap,
        on_ctrl_x=handle_ctrl_x_shortcut,
    )


if __name__ == "__main__":
    main()
