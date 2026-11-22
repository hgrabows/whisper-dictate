# WhisperDictate

*[Deutsche Version](README.de.md)*

Local dictation for macOS. Double-tap **Fn**, speak, double-tap **Fn** again,
and the text appears in whatever app you are typing in: mail, editor, browser,
terminal.

- **Runs on your Mac.** Speech recognition uses [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
  or [Cactus Whistle](https://huggingface.co/Cactus-Compute/whistle). Your
  audio never leaves the machine.
- **Fast.** The model stays loaded in a background `whisper-server`; a typical
  dictation is ready about 0.5–1.2 s after you stop speaking (large-v3-turbo,
  Apple Silicon).
- **Spoken punctuation and commands** in English and German: "comma",
  "new line", "delete that", "press enter" …
- **Optional LLM rewrite**: turn a rough dictation into a polished email,
  letter or code with Claude, ChatGPT, Gemini, DeepSeek, any OpenAI-compatible
  server or a local model. Off by default.
- **Interface in English or German.**

> Status: personal project, used daily on Apple Silicon. Expect rough edges.

## Installation

**Requirements:** macOS on Apple Silicon with [Homebrew](https://brew.sh).
Tested on an M1 Max with macOS 26.6, whisper.cpp 1.8.0, SoX 14.4.2 and uv 0.11.

1. **Install the tools:**

   ```bash
   brew install uv sox whisper-cpp
   ```

   - `uv` manages Python and all Python packages. It downloads Python 3.12 by
     itself; you don't need your own Python installation.
   - `sox` records the microphone.
   - `whisper-cpp` provides speech recognition (`whisper-cli` and
     `whisper-server`).

2. **Get the code and install the Python packages:**

   ```bash
   git clone https://github.com/hgrabows/whisper-dictate.git
   cd whisper-dictate
   uv sync
   ```

3. **Start:**

   ```bash
   uv run python whisper_dictate.py
   ```

   The settings window opens.

4. **Download the speech model:** on first start the app offers to download
   **large-v3-turbo** (574 MB). Click **Yes**; it verifies the checksum and
   switches to the model. You can also pick it later in the **Whisper model**
   menu (see [Speech models](#speech-models)). If `sox` or `whisper-cpp` is
   missing, the app says so and shows the `brew install` command.

5. **Grant macOS permissions:** under *System Settings → Privacy & Security*,
   the app you start WhisperDictate from (Terminal, iTerm, VS Code …) needs:
   - **Microphone**: macOS asks on the first dictation.
   - **Accessibility**: for the Fn double tap and for pasting the text.
   - **Input Monitoring**: for the global hotkey.

   You have to switch on the last two by hand; the button **Check permissions…**
   in the settings window opens the right panes. Restart the app afterwards.

6. **Free the Fn key from macOS:** otherwise macOS reacts to Fn itself.
   - *System Settings → Keyboard*: set **"Press 🌐 key to"** to **"Do
     Nothing"**. Otherwise Fn opens the emoji picker or switches the input
     source.
   - If you use macOS Dictation: under *System Settings → Keyboard →
     Dictation*, set its **shortcut** to something other than "Press 🌐 twice".
     Otherwise a Fn double tap also starts Apple's dictation.

**What the app downloads by itself:**
- The speech models you pick in the menu.
- For Whistle, once: its engine (`libcactus`, pinned version and checksum) into
  `~/.cache/cactus/`.

Nothing else. On first start it creates `config.json`, `credentials.yaml` and
`current_prompts.json` from the templates in the repo; these files hold your
settings and are ignored by git.

### Optional: local LLM

For LLM post-processing without any cloud service, install the extra (this
compiles `llama-cpp-python`, needs the Xcode Command Line Tools via
`xcode-select --install` and takes a few minutes):

```bash
uv sync --extra local-llm
```

Then download a GGUF model in the **LLM** window. See
[LLM post-processing](#llm-post-processing).

### Optional: start at login

Tick **Start automatically at login** in the settings window. From the next
login, macOS starts the app from this folder (with the project's `.venv`
Python, no terminal window). Untick it to remove the entry.

Without a terminal, macOS checks the permissions of the **Python program**
itself. The app shows its path right after you tick the box and copies it to
the clipboard; it looks like
`~/.local/share/uv/python/cpython-3.12.…/bin/python3.12`. Under *System
Settings → Privacy & Security*, add it to **Accessibility** and **Input
Monitoring** ("+", then ⌘⇧G and paste the path). If `uv` later installs a newer
Python, the path changes and you grant the permissions again.

Logs of autostarted runs: `~/Library/Logs/WhisperDictate/`.

## Usage

1. Put the cursor where the text should go.
2. Double-tap **Fn** (or click **Start recording**). A sound confirms the start.
3. Speak.
4. Double-tap **Fn** again. The text is pasted at the cursor.

Your clipboard is restored after pasting.

**What else it can do:** rewrite a dictation as a formal email or a letter,
generate code, translate, edit copied text by voice ("summarize this"), use
background context, and your own prompts. The **[user guide](docs/manual.md)**
explains all modes and windows.

### Spoken punctuation

Say the punctuation mark as a word; it becomes the character. The rules follow
the recognition language (`WHISPER_LANGUAGE`): English rules for `en`, German
rules for `de`, both for `auto`.

| English | German | Result |
|---|---|---|
| comma | Komma | `,` |
| period, full stop | Punkt | `.` |
| question mark | Fragezeichen | `?` |
| exclamation mark / point | Ausrufezeichen | `!` |
| colon | Doppelpunkt | `:` |
| semicolon | Semikolon | `;` |
| new line | neue Zeile | line break |
| new paragraph | neuer Absatz | blank line |
| open quote … close quote | Anführungszeichen auf … zu | `"…"` |
| open paren … close paren | Klammer auf … zu | `(…)` |

Normal speech is left alone: "that's the point" or "a period of two weeks" stay
text.

### Voice commands

Say the command as the whole dictation:

| English | German | Effect |
|---|---|---|
| "delete that", "scratch that" | „lösche das“ | deletes the text that was just pasted |
| "undo" | „rückgängig“ | undoes the last paste (Cmd+Z) |
| "press enter" | „drücke Enter“ | presses Return |

In the mode "No post-processing", "press enter" also works at the end of a
dictation ("… see you tomorrow, press enter"). The other modes never press
Return after pasting, so a rewritten text is never sent by accident.

### Interface language

The interface follows the macOS language (German if German is your first
language, English otherwise). Change it in the settings window or with
`"UI_LANGUAGE": "en"` / `"de"` in `config.json`; it applies after a restart.

## Speech models

The **Whisper model** menu lists all models from `whisper_models.py`. A model
that is not installed yet is marked `⬇ <size> download…`; pick it and the app
downloads it and switches to it. **Manage models…** opens a window to
download, use, remove or cancel.

| Key | Model | Size | Avg. time* | Notes |
|---|---|---|---|---|
| `large-turbo` | OpenAI large-v3-turbo, q5_0 | 574 MB | 0.9 s | **recommended**: quality like large, fastest whisper.cpp model |
| `whistle` | [Cactus Whistle](https://huggingface.co/Cactus-Compute/whistle) (Apache 2.0) | 17 MB | 0.03–0.5 s** | very fast, but **clearly weaker recognition**: technical terms and English words in German speech often come out wrong. Not a good first model |
| `de-turbo` | large-v3-turbo fine-tuned for German ([cstr/primeline](https://huggingface.co/cstr/whisper-large-v3-turbo-german-ggml)), q5_0 | 574 MB | 1.0 s | German only; always writes "ss" instead of "ß" |
| `large` | OpenAI large-v3 | 3.1 GB | 2.1 s | multilingual, slow |
| `medium` | OpenAI medium | 1.5 GB | 1.3 s | smaller, older |

\* Measured with five real German dictations (5–25 s, many English technical
terms), whisper.cpp 1.8.0, Apple Silicon, before `whisper-server` was added.
Only a small sample. `medium`, `large` and `large-turbo` gave comparable
quality with 1–2 small errors each.

\*\* Whistle: measured with six dictations (2–42 s) on an M1 Max; turbo took
0.8–1.6 s on the same clips. Whistle runs inside the app process (package
`cactus-compute`, installed by `uv sync`) and supports en, de, fr, es, it, nl
and pl. The app switches off Cactus telemetry.

- Downloads go to `models/` in the repo folder. They are streamed into a
  `.part` file and checked against the size and SHA-256 from the catalog before
  they replace the target file, so a broken download never destroys a working
  model.
- The choice is saved as `WHISPER_MODEL_SIZE` in `config.json`; on the command
  line use `--model-size large-turbo`.
- If the chosen model is missing, the app falls back to an installed one in the
  order `medium → large-turbo → large → de-turbo → whistle`.
- Turbo models need whisper.cpp ≥ 1.7.

### whisper-server

The app does not reload whisper.cpp models for every dictation. It starts
`whisper-server` once and keeps the model loaded.
- The server listens only on `127.0.0.1`, on a free port.
- This saves ~0.4 s per dictation: 0.5–1.2 s instead of 0.9–1.7 s with
  large-v3-turbo.
- A small watchdog process stops the server when the app exits, even after a
  crash.
- While the server is still loading, or if it fails, the app uses
  `whisper-cli` automatically.
- Switch it off with `"WHISPER_SERVER": "0"` in `config.json` or the
  environment variable `WHISPER_SERVER=0`. Point `WHISPER_SERVER_CMD` at a
  different binary if needed.
- Cost: the active model stays in memory, ~600 MB for turbo.

## LLM post-processing

Off by default: new installs start in the mode "Post-processing without AI" (spoken
punctuation and cleanup only). Pick a mode in the settings window (for example "Formal
rewrite", "Write a letter", "Generate source code") and the transcript is
rewritten by an LLM before it is pasted. Modes and prompts live in
`current_prompts.json`; see [docs/prompts.md](docs/prompts.md).

### Choosing a provider

Open **LLM (Post-Processing) → Providers & API keys…**, pick a provider, paste your
API key and click **Save & test**. The app checks the key, loads the provider's
model list and switches to it. Pick another model from the list or type a
model name. Providers you have set up can then be switched directly in the
**LLM (Post-Processing)** menu, modes in the **Prompts** menu.

| Provider | Connection | You enter |
|---|---|---|
| **Claude** (Anthropic) | official `anthropic` Python SDK | API key; default model Claude Opus 5.5 (Sonnet 5.5 and Haiku 5.5 are faster and cheaper) |
| **ChatGPT** (OpenAI) | OpenAI chat API | API key |
| **Gemini** (Google) | Google's OpenAI-compatible endpoint | API key |
| **DeepSeek** | OpenAI-compatible | API key |
| **Custom** | any OpenAI-compatible server (OpenRouter, LM Studio, vLLM, a company gateway …) | address, e.g. `https://host/v1`, and an API key if the server needs one |
| **Local** | GGUF model via `llama-cpp-python` on your Mac | nothing; install the extra (see [Optional: local LLM](#optional-local-llm)) and download a model in the same window. Presets: Phi-4-mini, Qwen2.5 1.5B/3B, Phi-3.5-mini, SmolLM2 1.7B |

- **Keys** entered in the window are stored in the **macOS Keychain**
  (service "WhisperDictate"), not in a file. **Remove key** deletes it there.
  Keys are never logged.
- With a cloud provider, the **transcript text** is sent to that provider; the
  audio is not.
- For Claude, the app asks for low effort (dictation cleanup is a simple task)
  and, on Claude Opus 5.5, enables Anthropic's server-side fallback, so a
  rare false safety refusal is retried on another model instead of failing.

**Keys from a file or the environment** keep working. Order: Keychain, then
`credentials.yaml`, then `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY` / `DEEPSEEK_API_KEY`. In `credentials.yaml` you can also add
further OpenAI-compatible providers; they show up in the list:

```yaml
providers:
  openrouter:
    api_url: "https://openrouter.ai/api/v1/chat/completions"
    api_key_file: "~/.config/openrouter.key"   # or: api_key: "..."
    models:
      - "openrouter/free"
    default_model: "openrouter/free"
```

`api_key_file` reads the key from a separate file (relative paths are resolved
next to `credentials.yaml`, `~` works). A provider named `google` in
`credentials.yaml` is used for Gemini.

## Configuration

Settings live in `config.json` (created from `config.template.json`). Most are
also available in the settings window. Environment variables with the same name
override the file.

| Key | Default | Meaning |
|---|---|---|
| `UI_LANGUAGE` | `auto` | Interface language: `auto`, `en`, `de` |
| `WHISPER_LANGUAGE` | interface language | Recognition language (`en`, `de`, `auto` …) |
| `LLM_OUTPUT_LANGUAGE` | interface language | Language the LLM answers in |
| `AUTO_CORRECT_MODE` | `-5` | Mode: `-5` without AI, `0` no post-processing, `1`… prompt modes |
| `WHISPER_MODEL_SIZE` | `medium` | Speech model key, see table above |
| `WHISPER_ARGS` | `-mc 0 -sns` | Extra whisper.cpp arguments (no segment context, suppress non-speech tokens: fewer repeated hallucinations) |
| `WHISPER_SERVER` | `1` | Keep the model loaded in `whisper-server` |
| `WHISPER_TAIL_FILTER` | `1` | Drop typical Whisper hallucinations after long silences |
| `POST_PROCESSING_PROVIDER` | `local` | LLM provider: `local`, `claude`, `openai`, `gemini`, `deepseek`, `custom` or a name from `credentials.yaml` |
| `CUSTOM_LLM_URL` | – | Address of the custom OpenAI-compatible provider |
| `SHOW_SETTINGS_GUI` | `1` | Show the settings window on start |
| `PASTE_RESTORE_CLIPBOARD` | `1` | Restore the clipboard after pasting |
| `DOUBLE_TAP_WINDOW` | `0.6` | Max. seconds between the two Fn taps (env only) |

Run `uv run python whisper_dictate.py --help` for command-line options.

## Privacy

- Audio is recorded to `tmp/` in the repo folder and transcribed locally.
  Old recordings are deleted automatically (keeps the last 100, at most 14
  days; `WHISPER_MAX_RECORDING_COUNT`, `WHISPER_MAX_RECORDING_AGE_DAYS`).
- Nothing is sent anywhere unless you configure an API provider for LLM
  post-processing.
- "Personal data" (name, address, email, phone) is stored only in your local
  `config.json`. It is used to spell your name correctly and for the signature
  of the letter mode.

## Troubleshooting

- **Fn double tap does nothing**: check Accessibility and Input Monitoring for
  the app you start WhisperDictate from, then restart it. Check that the Fn
  key is free (installation step 6). Details:
  [docs/hotkey.md](docs/hotkey.md).
- **Text is not pasted**: some apps (password fields, terminals with "Secure
  Keyboard Entry") block synthetic keystrokes. The text is still on the
  clipboard; press Cmd+V. Background: [docs/text-insertion.md](docs/text-insertion.md).
- **"whisper-server not found"**: `brew install whisper-cpp` provides it; the
  app falls back to `whisper-cli` meanwhile.

## Development

```bash
uv sync
uv run python -m pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT with Commons Clause](LICENSE): anyone may download, use, modify and
share it, privately and at work, inside companies too. Not allowed without a
separate license: selling it, or offering a paid product or service whose
value comes entirely or substantially from it (including paid hosting or
support). For that, ask via a GitHub issue.

Model weights are downloaded from their upstream sources and keep their own
licenses (OpenAI Whisper: MIT; Cactus Whistle:
Apache 2.0; LLM presets: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
