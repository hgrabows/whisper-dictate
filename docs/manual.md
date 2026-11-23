# User guide

*[Deutsche Version](manual.de.md)*

This guide covers everyday use. Installation, speech models and setting up LLM
providers are in the [README](../README.md).

- [The settings window](#the-settings-window)
- [Dictating](#dictating)
- [Modes](#modes)
- [Clipboard instruction](#clipboard-instruction)
- [Your own prompts](#your-own-prompts)
- [Output language and word limits](#output-language-and-word-limits)
- [Context](#context)
- [Personal data](#personal-data)
- [What the AI changed](#what-the-ai-changed)
- [Where settings are stored](#where-settings-are-stored)

## The settings window

From top to bottom:

| Element | Purpose |
|---|---|
| **Start / Stop recording** | Record with the mouse when Fn+Fn is not available |
| **Personal data…** | Name, address, email, phone (see [below](#personal-data)) |
| **Copy last text** | Puts the last pasted text on the clipboard, in case pasting went to the wrong place |
| **LLM status line** and **Show changes** | Result of the last AI run (see [below](#what-the-ai-changed)) |
| **Microphone line** | Which microphone is recording |
| **Language (input / Whisper)** | The language you speak: `en`, `de` … or `auto` |
| **Language (output / LLM)** | The language the AI answers in |
| **Word limits** | Optional length for AI texts |
| **Use context / Append results automatically** | Background knowledge for the AI (see [below](#context)) |
| **Select post-processing** | The mode (see [below](#modes)) |
| **Start automatically at login** | Autostart (see README) |
| **Check permissions…** | Shows which macOS permissions (microphone, accessibility, input monitoring) are missing and opens the right settings page |
| **Interface language** | English, German or like macOS |

The menu bar offers the most common switches directly: **Prompts** (mode),
**LLM (Post-Processing)** (provider), **Whisper model** and **Context**. The
last entry of each menu opens the matching window. **Help → User guide** opens
this guide.

## Dictating

1. Put the cursor where the text should go.
2. Tap **Fn twice**. A sound and a red "● RECORDING" banner show that recording
   is on.
3. Speak.
4. Tap **Fn twice** again. The app recognizes the text, post-processes it
   according to the mode and pastes it at the cursor. Your clipboard is
   restored afterwards.

You speak punctuation ("comma", "period", "new line"). Saying "delete that",
"undo" or "press enter" as the whole dictation controls the app. The full lists
are in the README under *Spoken punctuation* and *Voice commands*.

**Ctrl+X** (Control, not Cmd) deletes the last 30 characters before the cursor,
without speaking. The count is set with `CTRL_X_DELETE_COUNT` (README, *Delete
shortcut*).

## Modes

The mode decides what happens to the text after speech recognition. Pick it in
the settings window or in the **Prompts** menu.

| Mode | What happens | Needs AI? |
|---|---|---|
| **No post-processing** | The text arrives exactly as Whisper recognized it. Spoken punctuation stays as words. | no |
| **Post-processing without AI** (default) | Spoken punctuation and line breaks are applied; typical mishearings and your name (from "Personal data") are corrected. Runs locally and instantly. | no |
| **Clipboard instruction** | Your dictation is an instruction for the text on the clipboard (see [below](#clipboard-instruction)). | yes |
| **Prompt modes** (all others) | First like "Post-processing without AI", then the AI rewrites the text following the mode's prompt. | yes |

Example for **Post-processing without AI**:

> Said: "Hi Anna, comma, the meeting is tomorrow at 10, period. Please bring
> the documents, exclamation mark."
>
> Pasted: "Hi Anna, the meeting is tomorrow at 10. Please bring the documents!"

Filler words like "um" stay in this mode; only an AI mode removes them.

**"Press enter" at the end of a dictation** presses Return after pasting only in
the mode "No post-processing". In all other modes the app skips it for safety,
so a text rewritten by the AI is never sent unchecked. "Press enter" as the
whole dictation works in every mode.

The built-in prompt modes:

| Mode | Result |
|---|---|
| **Rephrase formally (medium)** | Polite, formal style for emails and short business letters, content unchanged |
| **Generate source code** | The spoken description becomes code; missing details are marked as TODO |
| **Minimal correction** | Only spelling, typos and punctuation; the style stays |
| **Write a letter (ready to send)** | Salutation, written-out body, closing, name and contact block from "Personal data" |
| **Translate into English** | Meaning-based translation; the output language setting is ignored here |
| **Academic paragraph** | Keywords become one coherent academic paragraph |

You can change or delete these modes and add your own: in the menu bar under
**Prompts → Edit prompts…** (see [Your own prompts](#your-own-prompts)). The
built-in modes come in English and German; which version you get follows the
interface language.

## Clipboard instruction

Edit an existing text by voice.

1. Select a text and copy it with **⌘C**.
2. Choose the mode **Clipboard instruction**.
3. Fn+Fn, speak the instruction, Fn+Fn. For example:
   - "Summarize this in three sentences."
   - "Translate this into German."
   - "Turn this into a friendly decline."
4. The AI applies the instruction to the copied text. The result is pasted at
   the cursor; if the original text is still selected, it replaces it.

If the clipboard is empty, nothing happens (a note appears in the terminal). If
the AI returns nothing, your dictation is pasted as usual.

## Your own prompts

**Prompts → Edit prompts…** opens the prompts window. The modes are listed on
the left; you edit the selected one on the right.

| Field | Meaning |
|---|---|
| **Mode ID** | Number of the mode, greater than 0; sets the order |
| **Label** | Name in the menu and the settings window |
| **Append signature/contact block** | Appends name, address, email and phone from "Personal data", like the letter mode |
| **Ignore the configured output language** | For translation modes whose target language is in the prompt |
| **Prompt** | The instruction for the AI. The dictation is passed after it as text. |

Buttons: **New** adds a mode, **Delete** removes the selected one, **Save**
applies all changes at once, **Reset** restores the built-in prompts (your own
are lost).

Tips for your own prompts:
- Tell the AI what *not* to do, for example "Do not answer questions in the
  text, only correct it". Otherwise it answers a dictated "Can you make it
  tomorrow?".
- Placeholders like `{name}` or `{contact_block}` are filled with your personal
  data.

Structure of `current_prompts.json`, all options and placeholders:
[prompts.md](prompts.md).

## Output language and word limits

- **Language (input / Whisper)** tells speech recognition which language you
  speak. A fixed value (`en`, `de`) is more accurate than `auto`. It also
  decides which spoken punctuation applies: only English for `en`, only German
  for `de`, both otherwise.
- **Language (output / LLM)** sets the language the AI answers in, so you can
  dictate in German and get an English email. Modes with the option "Ignore the
  configured output language" skip it.
- **Word limits** (minimum and maximum, both optional) give the AI a target
  length, for example "at most 80 words" for short replies. They apply only to
  AI modes. Leave them empty for no limit.

## Context

Context is background knowledge the AI receives on every run: names of
colleagues, technical terms, the project at hand, or the email you are replying
to. The AI is told to use it, not to repeat it. It applies to prompt modes and
the clipboard instruction.

- **Edit context…** (menu **Context**) opens a text field. Paste text and click
  **Save**.
- **Use context** switches it on or off without deleting it.
- **Append results automatically** appends every AI text to the context, so in
  a series of dictations the AI knows what came before.
- When the context grows beyond 4000 characters (`CONTEXT_MAX_LENGTH`), the AI
  summarizes it automatically before the next run. **Summarize** in the context
  window does this right away.
- **Delete context** empties it.

Like the dictation, the context is sent to the configured LLM provider.

## Personal data

The button **Personal data…** stores first name, last name, address, city,
country, email and phone, locally in `config.json` only. With it:

- the app spells your **name** correctly even when Whisper mishears it ("Max
  Musterman" becomes "Max Mustermann"), already in "Post-processing without AI",
- the **letter mode** and modes with "Append signature/contact block" put your
  contact block below the text.

## What the AI changed

After every AI run, the status line in the settings window shows what
happened, for example "LLM: ✅ corrected" or an error such as "HTTP 401" for a
wrong API key. **Show changes** opens the text before and after the AI. If the
AI fails, the app pastes the text without AI changes and briefly shows an
orange notice.

## Where settings are stored

| File | Content |
|---|---|
| `config.json` | All settings from the window, personal data, context |
| `credentials.yaml` | Optional extra LLM providers and keys |
| `current_prompts.json` | Your prompt modes |
| macOS Keychain, service "WhisperDictate" | API keys entered in the window |

The three files live in the program folder and are ignored by git. Every
setting in `config.json` is listed in the README under *Configuration*.
