"""Cloud LLM providers: presets, key order, model lists and the Claude call."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import whisper_dictate as app
import llm_providers
import post_processing


# ---- URLs and model lists ----------------------------------------------------

@pytest.mark.parametrize(
    ("chat_url", "models_url"),
    [
        ("https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1/models"),
        ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com/models"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "https://generativelanguage.googleapis.com/v1beta/openai/models",
        ),
        ("https://host/v1/", "https://host/v1/models"),
    ],
)
def test_models_url_from_chat_url(chat_url, models_url):
    assert llm_providers.models_url_from_chat_url(chat_url) == models_url


def test_normalize_chat_url_accepts_base_or_full_endpoint():
    assert llm_providers.normalize_chat_url("https://host/v1") == "https://host/v1/chat/completions"
    assert llm_providers.normalize_chat_url("https://host/v1/chat/completions/") == "https://host/v1/chat/completions"
    assert llm_providers.normalize_chat_url("  ") == ""


def test_openai_model_list_keeps_only_chat_models():
    ids = ["gpt-5.2", "gpt-4o-audio-preview", "text-embedding-3-small", "whisper-1", "o3", "dall-e-3", "gpt-5.2"]
    assert llm_providers._filter_chat_models("openai", ids) == ["gpt-5.2", "o3"]


def test_gemini_model_list_strips_prefix_and_drops_non_gemini():
    ids = ["models/gemini-2.5-flash", "models/embedding-001", "models/gemini-2.5-flash-image"]
    assert llm_providers._filter_chat_models("gemini", ids) == ["gemini-2.5-flash"]


def test_pick_default_model_prefers_current_then_preset_default():
    models = ["claude-haiku-5-5", "claude-opus-5-5"]
    assert llm_providers.pick_default_model("claude", models, "claude-haiku-5-5") == "claude-haiku-5-5"
    assert llm_providers.pick_default_model("claude", models, "gone") == "claude-opus-5-5"
    assert llm_providers.pick_default_model("deepseek", ["deepseek-chat"], "") == "deepseek-chat"


def test_credentials_alias_google_maps_to_gemini():
    assert llm_providers.preset_for_credentials_name("google").key == "gemini"
    assert app.normalize_llm_provider_key("Google") == "gemini"
    assert app.normalize_llm_provider_key("openrouter") == "openrouter"


# ---- Key order and runtime environment ---------------------------------------

def test_key_order_keychain_then_credentials_then_env(monkeypatch):
    creds = {"google": {"api_url": "https://example.test/chat/completions", "api_key": "from-yaml", "models": ["gemini-x"]}}
    monkeypatch.setattr(llm_providers, "_ENV_AT_START", {"gemini": "from-env"})

    monkeypatch.setattr(llm_providers, "get_stored_key", lambda key: "from-keychain")
    meta = app.provider_runtime_meta("gemini", {}, creds)
    assert (meta["api_key"], meta["key_source"]) == ("from-keychain", "keychain")
    assert meta["api_url"] == "https://example.test/chat/completions"
    assert meta["models"] == ["gemini-x"]

    monkeypatch.setattr(llm_providers, "get_stored_key", lambda key: "")
    meta = app.provider_runtime_meta("gemini", {}, creds)
    assert (meta["api_key"], meta["key_source"]) == ("from-yaml", "credentials")

    meta = app.provider_runtime_meta("gemini", {}, {})
    assert (meta["api_key"], meta["key_source"]) == ("from-env", "env")
    assert meta["api_url"] == llm_providers.PROVIDER_PRESETS["gemini"].api_url


def test_custom_provider_reads_url_from_config(monkeypatch):
    monkeypatch.setattr(llm_providers, "get_stored_key", lambda key: "")
    meta = app.provider_runtime_meta("custom", {"CUSTOM_LLM_URL": "https://my.host/v1"}, {})
    assert meta["api_url"] == "https://my.host/v1/chat/completions"
    assert meta["kind"] == llm_providers.KIND_OPENAI


def test_runtime_env_for_claude_and_openai_compatible():
    claude = {"key": "claude", "kind": llm_providers.KIND_ANTHROPIC, "api_key": "k"}
    assert app.llm_runtime_env(claude, "claude-opus-5-5") == {
        "LLM_PROVIDER": "anthropic",
        "LLM_PROVIDER_NAME": "claude",
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_MODEL": "claude-opus-5-5",
    }
    deepseek = {"key": "deepseek", "kind": llm_providers.KIND_OPENAI, "api_key": "k", "api_url": "https://u"}
    env = app.llm_runtime_env(deepseek, "deepseek-chat")
    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_BASE_URL"] == "https://u"
    assert env["OPENAI_MODEL"] == "deepseek-chat"


def test_choose_model_prefers_remembered_then_default():
    meta = {"models": ["a", "b"], "default_model": "b"}
    assert app.choose_llm_model(meta, "a") == "a"
    assert app.choose_llm_model(meta, "zzz") == "b"
    assert app.choose_llm_model({"models": [], "default_model": "claude-opus-5-5"}, "") == "claude-opus-5-5"
    assert app.remembered_llm_model({"LLM_MODEL_BY_PROVIDER": {"claude": "claude-haiku-5-5"}}, "claude") == "claude-haiku-5-5"


# ---- Claude call ----------------------------------------------------------------

def _response(text="Fertig.", stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class _FakeMessages:
    def __init__(self, replies):
        self.calls = []
        self._replies = list(replies)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _fake_client(monkeypatch, replies, beta_replies=None):
    client = SimpleNamespace(
        messages=_FakeMessages(replies),
        beta=SimpleNamespace(messages=_FakeMessages(beta_replies or [])),
    )
    monkeypatch.setattr(llm_providers, "_anthropic_client", lambda api_key, timeout: (anthropic, client))
    return client


def _bad_request(message):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request)
    return anthropic.BadRequestError(message, response=response, body=None)


def test_claude_opus_uses_server_side_fallback_and_low_effort(monkeypatch):
    client = _fake_client(monkeypatch, [], beta_replies=[_response("Hallo Welt.")])
    out = llm_providers.claude_chat("sys", "hallo welt", model="claude-opus-5-5", api_key="k")
    assert out == "Hallo Welt."
    call = client.beta.messages.calls[0]
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "low"}
    assert "temperature" not in call
    assert client.messages.calls == []


def test_claude_older_model_retries_without_effort(monkeypatch):
    client = _fake_client(monkeypatch, [_bad_request("effort is not supported"), _response("ok")])
    assert llm_providers.claude_chat("sys", "x", model="claude-haiku-4-5", api_key="k") == "ok"
    assert "output_config" in client.messages.calls[0]
    assert "output_config" not in client.messages.calls[1]


def test_claude_refusal_raises_provider_error(monkeypatch):
    _fake_client(monkeypatch, [_response("", stop_reason="refusal")])
    with pytest.raises(llm_providers.ProviderError):
        llm_providers.claude_chat("sys", "x", model="claude-haiku-5-5", api_key="k")


def test_post_processing_routes_to_claude(monkeypatch):
    seen = {}

    def _fake_chat(system, user_text, **kwargs):
        seen.update(kwargs, user_text=user_text)
        return "Das ist ein Test."

    monkeypatch.setattr(llm_providers, "claude_chat", _fake_chat)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("LLM_REWRITE", "1")
    out = post_processing.maybe_llm_refine("das ist ein test", mode=3)
    assert out == "Das ist ein Test."
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["api_key"] == "test-key"
    assert post_processing.get_last_llm_status().get("provider") == "claude"


def test_post_processing_claude_without_key_keeps_text(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_REWRITE", "1")
    assert post_processing.maybe_llm_refine("das ist ein test", mode=3) == "das ist ein test"
    assert post_processing.get_last_llm_status().get("error_type") == "missing_key"


# ---- Missing tools -----------------------------------------------------------

def test_missing_tools_message_names_brew_packages(monkeypatch):
    monkeypatch.setattr(app, "_command_available", lambda cmd: False)
    missing = app.missing_external_tools(app.ENGINE_WHISTLE)
    assert [pkg for _cmd, pkg in missing] == ["sox"]
    missing = app.missing_external_tools("whisper.cpp")
    assert [pkg for _cmd, pkg in missing] == ["sox", "whisper-cpp"]
    assert "brew install sox whisper-cpp" in app.missing_tools_message(missing)
