"""Cloud LLM providers for post-processing: presets, API keys, model lists, Claude.

Presets: Claude (Anthropic SDK), ChatGPT, Gemini, DeepSeek and a custom
OpenAI-compatible endpoint. API keys entered in the settings window are stored
in the macOS Keychain (via `keyring`); keys from `credentials.yaml` or from the
environment keep working.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from i18n import tr

KEYCHAIN_SERVICE = "WhisperDictate"

KIND_ANTHROPIC = "anthropic"
KIND_OPENAI = "openai"  # any OpenAI-compatible /chat/completions endpoint


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label_de: str
    label_en: str
    kind: str
    api_url: str  # chat endpoint; empty for Claude (SDK) and the custom provider
    default_model: str
    env_key: str  # environment variable that may hold the API key
    credentials_aliases: tuple[str, ...] = ()  # other names in credentials.yaml

    @property
    def label(self) -> str:
        return tr(self.label_de, self.label_en)


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "claude": ProviderPreset(
        key="claude",
        label_de="Claude (Anthropic)",
        label_en="Claude (Anthropic)",
        kind=KIND_ANTHROPIC,
        api_url="",
        default_model="claude-opus-5-5",
        env_key="ANTHROPIC_API_KEY",
        credentials_aliases=("anthropic",),
    ),
    "openai": ProviderPreset(
        key="openai",
        label_de="ChatGPT (OpenAI)",
        label_en="ChatGPT (OpenAI)",
        kind=KIND_OPENAI,
        api_url="https://api.openai.com/v1/chat/completions",
        default_model="",
        env_key="OPENAI_API_KEY",
    ),
    "gemini": ProviderPreset(
        key="gemini",
        label_de="Gemini (Google)",
        label_en="Gemini (Google)",
        kind=KIND_OPENAI,
        api_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        default_model="",
        env_key="GEMINI_API_KEY",
        credentials_aliases=("google",),
    ),
    "deepseek": ProviderPreset(
        key="deepseek",
        label_de="DeepSeek",
        label_en="DeepSeek",
        kind=KIND_OPENAI,
        api_url="https://api.deepseek.com/chat/completions",
        default_model="",
        env_key="DEEPSEEK_API_KEY",
    ),
    "custom": ProviderPreset(
        key="custom",
        label_de="Eigener Anbieter (OpenAI-kompatibel)",
        label_en="Custom provider (OpenAI-compatible)",
        kind=KIND_OPENAI,
        api_url="",
        default_model="",
        env_key="",
    ),
}

# Claude models that accept the server-side refusal fallback (`fallbacks`).
_CLAUDE_FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
_CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Environment values as they were when the app started; the app itself later
# writes OPENAI_API_KEY etc. for the active provider.
_ENV_AT_START = {
    preset.key: (os.environ.get(preset.env_key) or "").strip()
    for preset in PROVIDER_PRESETS.values()
    if preset.env_key
}


class ProviderError(RuntimeError):
    """A provider call failed; the message is shown to the user."""


def preset_for(provider_key: str) -> Optional[ProviderPreset]:
    return PROVIDER_PRESETS.get((provider_key or "").strip().lower())


def preset_for_credentials_name(name: str) -> Optional[ProviderPreset]:
    """Map a provider name from credentials.yaml ("google") to its preset ("gemini")."""
    key = (name or "").strip().lower()
    if key in PROVIDER_PRESETS:
        return PROVIDER_PRESETS[key]
    for preset in PROVIDER_PRESETS.values():
        if key in preset.credentials_aliases:
            return preset
    return None


def env_api_key(provider_key: str) -> str:
    return _ENV_AT_START.get((provider_key or "").strip().lower(), "")


# ---- Keychain ---------------------------------------------------------------

def _keyring():
    try:
        import keyring

        return keyring
    except Exception:
        return None


def get_stored_key(provider_key: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return (kr.get_password(KEYCHAIN_SERVICE, provider_key) or "").strip()
    except Exception as error:
        print(f"Keychain read failed ({provider_key}): {type(error).__name__}")
        return ""


def store_key(provider_key: str, api_key: str) -> None:
    kr = _keyring()
    if kr is None:
        raise ProviderError(tr(
            "Schlüsselbund nicht verfügbar (Paket keyring fehlt).",
            "Keychain not available (package keyring is missing).",
        ))
    try:
        kr.set_password(KEYCHAIN_SERVICE, provider_key, api_key.strip())
    except Exception as error:
        raise ProviderError(tr(
            f"Key konnte nicht im Schlüsselbund gespeichert werden: {type(error).__name__}",
            f"Could not save the key in the Keychain: {type(error).__name__}",
        )) from error


def delete_stored_key(provider_key: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYCHAIN_SERVICE, provider_key)
    except Exception:
        pass


# ---- HTTP helpers (OpenAI-compatible) ---------------------------------------

def _ssl_context() -> Optional[ssl.SSLContext]:
    ca_bundle = os.environ.get("LLM_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    try:
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def models_url_from_chat_url(chat_url: str) -> str:
    """https://host/v1/chat/completions -> https://host/v1/models"""
    url = (chat_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    if url.endswith("/completions"):
        return url[: -len("/completions")] + "/models"
    return url + "/models"


def normalize_chat_url(url: str) -> str:
    """Accept a base URL ("https://host/v1") as well as the full chat endpoint."""
    value = (url or "").strip().rstrip("/")
    if not value:
        return ""
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


def _http_error_message(error: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        body = error.read().decode("utf-8", "replace").strip()
        payload = json.loads(body) if body else {}
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or "").strip()
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            inner = payload[0].get("error")
            if isinstance(inner, dict):
                detail = str(inner.get("message") or "").strip()
        if not detail:
            detail = body[:200]
    except Exception:
        detail = ""
    if error.code in (401, 403):
        return tr(
            f"API-Key abgelehnt (HTTP {error.code}). {detail}".strip(),
            f"API key rejected (HTTP {error.code}). {detail}".strip(),
        )
    return f"HTTP {error.code}: {detail}".strip()


_OPENAI_NON_CHAT_MARKERS = (
    "audio", "realtime", "transcribe", "tts", "image", "embedding", "moderation",
    "search", "whisper", "dall-e", "davinci", "babbage", "computer-use", "sora",
)


def _filter_chat_models(provider_key: str, model_ids: list[str]) -> list[str]:
    ids = [m.removeprefix("models/") for m in model_ids if m]
    if provider_key == "openai":
        ids = [
            m for m in ids
            if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))
            and not any(marker in m for marker in _OPENAI_NON_CHAT_MARKERS)
        ]
    elif provider_key == "gemini":
        ids = [
            m for m in ids
            if m.startswith("gemini") and not any(marker in m for marker in _OPENAI_NON_CHAT_MARKERS)
        ]
    seen: set[str] = set()
    out: list[str] = []
    for m in ids:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _list_openai_compatible_models(provider_key: str, chat_url: str, api_key: str, timeout: float) -> list[str]:
    if not chat_url:
        raise ProviderError(tr("Adresse (URL) fehlt.", "Address (URL) is missing."))
    url = models_url_from_chat_url(chat_url)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        ctx = _ssl_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ProviderError(_http_error_message(error)) from error
    except urllib.error.URLError as error:
        raise ProviderError(tr(
            f"Keine Verbindung zu {url}: {error.reason}",
            f"Could not connect to {url}: {error.reason}",
        )) from error
    except Exception as error:
        raise ProviderError(f"{type(error).__name__}: {error}") from error
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ids = [str(item.get("id") or item.get("name") or "").strip() for item in items if isinstance(item, dict)]
    return _filter_chat_models(provider_key, ids)


# ---- Claude (Anthropic SDK) -------------------------------------------------

def _anthropic_client(api_key: str, timeout: float):
    try:
        import anthropic
    except Exception as error:
        raise ProviderError(tr(
            "Python-Paket 'anthropic' fehlt (uv sync ausführen).",
            "Python package 'anthropic' is missing (run uv sync).",
        )) from error
    if not api_key:
        raise ProviderError(tr("API-Key fehlt.", "API key is missing."))
    return anthropic, anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)


def _anthropic_error_message(anthropic, error: Exception) -> str:
    if isinstance(error, anthropic.AuthenticationError):
        return tr("API-Key abgelehnt (HTTP 401).", "API key rejected (HTTP 401).")
    if isinstance(error, anthropic.PermissionDeniedError):
        return tr("Keine Berechtigung (HTTP 403).", "Permission denied (HTTP 403).")
    if isinstance(error, anthropic.NotFoundError):
        return tr("Modell nicht gefunden (HTTP 404).", "Model not found (HTTP 404).")
    if isinstance(error, anthropic.RateLimitError):
        return tr("Rate-Limit erreicht (HTTP 429).", "Rate limit reached (HTTP 429).")
    if isinstance(error, anthropic.APIStatusError):
        return f"HTTP {error.status_code}: {error.message}"
    if isinstance(error, anthropic.APITimeoutError):
        return tr("Zeitüberschreitung.", "Request timed out.")
    if isinstance(error, anthropic.APIConnectionError):
        return tr("Keine Verbindung zur Anthropic-API.", "Could not connect to the Anthropic API.")
    return f"{type(error).__name__}: {error}"


def _list_claude_models(api_key: str, timeout: float) -> list[str]:
    anthropic, client = _anthropic_client(api_key, timeout)
    try:
        return [model.id for model in client.models.list()]
    except anthropic.AnthropicError as error:
        raise ProviderError(_anthropic_error_message(anthropic, error)) from error


def claude_chat(
    system: str,
    user_text: str,
    *,
    model: str,
    api_key: str,
    timeout: float = 30.0,
    max_tokens: int = 16000,
) -> str:
    """One Messages API call; returns the reply text. Raises ProviderError."""
    anthropic, client = _anthropic_client(api_key, timeout)
    model_id = (model or "").strip() or PROVIDER_PRESETS["claude"].default_model
    params = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
        # Dictation cleanup is a simple, latency-sensitive task.
        "output_config": {"effort": "low"},
    }

    def _send(request: dict):
        if model_id in _CLAUDE_FALLBACK_MODELS:
            # On a safety refusal the API retries on a fallback model in the same call.
            return client.beta.messages.create(
                **request, betas=[_CLAUDE_FALLBACK_BETA], fallbacks="default"
            )
        return client.messages.create(**request)

    try:
        try:
            response = _send(params)
        except anthropic.BadRequestError as error:
            # Older Claude models do not accept `effort`.
            if "effort" not in str(error.message).lower():
                raise
            params.pop("output_config", None)
            response = _send(params)
    except anthropic.AnthropicError as error:
        raise ProviderError(_anthropic_error_message(anthropic, error)) from error

    if response.stop_reason == "refusal":
        raise ProviderError(tr("Claude hat die Anfrage abgelehnt.", "Claude declined the request."))
    return "".join(block.text for block in response.content if block.type == "text").strip()


def check_claude_model(model: str, api_key: str, timeout: float = 6.0) -> None:
    """Free check that the key works and the model exists. Raises ProviderError."""
    anthropic, client = _anthropic_client(api_key, timeout)
    try:
        client.models.retrieve(model)
    except anthropic.AnthropicError as error:
        raise ProviderError(_anthropic_error_message(anthropic, error)) from error


# ---- Public entry points ----------------------------------------------------

def list_models(provider_key: str, *, api_key: str, chat_url: str = "", timeout: float = 10.0) -> list[str]:
    """Models the provider offers for this key. Raises ProviderError."""
    preset = preset_for(provider_key)
    if preset is not None and preset.kind == KIND_ANTHROPIC:
        return _list_claude_models(api_key, timeout)
    url = chat_url or (preset.api_url if preset else "")
    if preset is not None and preset.key != "custom" and not api_key:
        raise ProviderError(tr("API-Key fehlt.", "API key is missing."))
    return _list_openai_compatible_models((provider_key or "").strip().lower(), url, api_key, timeout)


def pick_default_model(provider_key: str, models: list[str], current: str = "") -> str:
    if current and current in models:
        return current
    preset = preset_for(provider_key)
    if preset and preset.default_model and preset.default_model in models:
        return preset.default_model
    return models[0] if models else (current or (preset.default_model if preset else ""))
