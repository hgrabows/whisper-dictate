"""Run the test suite against a throw-away app-support directory.

Importing whisper_dictate creates config.json, credentials.yaml and
current_prompts.json from the templates and copies their values into the
environment. Pointing APP_SUPPORT_DIR at a temporary directory (before any
test module imports the app) keeps a developer's real settings, personal data
and API keys out of the tests, and makes results independent of them.
"""

import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="whisperdictate-tests-")
os.environ["APP_SUPPORT_DIR"] = _SANDBOX
os.environ["WHISPER_NO_REEXEC"] = "1"
# Never touch the developer's macOS Keychain from tests.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
for _key in (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_PROVIDER",
    "LLM_PROVIDER_NAME",
    "WHISPER_CONFIG_PATH",
    "WHISPER_CREDENTIALS_PATH",
    "LOCAL_MODELS_REGISTRY",
    "WHISPER_PROMPTS_TEMPLATE_PATH",
    "WHISPER_CURRENT_PROMPTS_PATH",
    "WHISPER_PROMPTS_PATH",
    "WHISPER_TMP_DIR",
):
    os.environ.pop(_key, None)
