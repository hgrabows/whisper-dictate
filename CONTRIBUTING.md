# Contributing

Thanks for your interest! Bug reports and pull requests are welcome.

## Setup

```bash
brew install uv sox whisper-cpp
uv sync                      # add --extra local-llm for local LLM support
uv run python -m pytest -q
uv run python whisper_dictate.py
```

## Project layout

| File | Purpose |
|---|---|
| `whisper_dictate.py` | Main app: hotkey, recording, transcription, paste, settings window |
| `key_handler.py` | Fn double-tap detection (Quartz event tap) |
| `whisper_server.py` | Keeps a whisper.cpp model loaded in `whisper-server` |
| `whistle_backend.py` | In-process Cactus Whistle model |
| `whisper_models.py` | Speech model catalog and verified downloads |
| `post_processing.py` | Spoken punctuation, cleanup, LLM prompts |
| `local_llm.py` | Local GGUF models via `llama-cpp-python` |
| `llm_providers.py` | Cloud LLM presets (Claude, ChatGPT, Gemini, DeepSeek, custom), Keychain keys, model lists, Claude calls |
| `i18n.py` | Interface language (`tr("Deutsch", "English")`) |
| `prompt.en.json`, `prompt.json` | Default LLM modes (English, German) |
| `docs/manual.md`, `docs/manual.de.md` | User guide; update it when you change the GUI or a mode |
| `config.template.json`, `credentials.template.yaml` | Templates for the local, git-ignored config files |

## Guidelines

- PEP 8, 4 spaces, `snake_case`; `UPPER_SNAKE_CASE` for config keys.
- Prefer `pathlib.Path`.
- Every user-visible string in the GUI needs both languages:
  `tr("Deutscher Text", "English text")`. Console output and code comments
  are English.
- Tests go under `tests/` (`test_*.py`). Describe manual checks you did (hotkey,
  recording, transcription) in the pull request.
- In the pull request, mention your macOS version, the whisper.cpp version and
  any new or changed config keys.
- Never commit `config.json`, `credentials.yaml`, `current_prompts.json`,
  models or anything in `tmp/`: they hold personal data, API keys or
  recordings and are git-ignored.
