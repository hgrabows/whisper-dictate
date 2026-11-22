# Prompts and LLM modes

*[Deutsche Version](prompts.de.md)*

After speech recognition, WhisperDictate can optionally rewrite the transcript
with an LLM. The **mode** decides which prompt is used.

## Files

| File | Role |
|------|------|
| `prompt.en.json` | English template (in the repo). |
| `prompt.json` | German template (in the repo). |
| `current_prompts.json` | Active prompts. Created on first start from the template that matches the interface language (`UI_LANGUAGE`). Not tracked by git. |

If you delete `current_prompts.json`, the app recreates it from the template on
the next start. **Prompts → Reset** in the settings window does the same.

## Structure of a mode

```json
{
  "modes": {
    "4": {
      "label": "Write a letter (ready to send)",
      "add_signature": true,
      "prompt": "You are a writing assistant. …"
    }
  }
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `label` | yes | Name shown in the GUI |
| `prompt` | yes | System prompt for the LLM |
| `add_signature` | no | see below |
| `skip_output_language` | no | see below |

**Mode numbers are arbitrary** and carry no program logic; any special
behaviour is controlled only by the flags. Keep the numbers contiguous
(1, 2, 3, …) so the list in the GUI looks tidy.

Two modes are reserved and do *not* appear in the JSON:
`0` = "No post-processing", `-5` = "Post-processing without AI".

## Options (flags)

### `add_signature: true`

The mode appends the **signature / contact block** (address, email, phone from
"Personal data") below the name, like the signature feature of a mail client.
In detail:

- The LLM is told to put the contact details below the name.
- After the LLM, the contact block is enforced deterministically (in case the
  LLM forgot or changed it).
- If no LLM provider is configured at all, a fallback letter layout
  (salutation, text, closing, name, contact block) is produced.

### `skip_output_language: true`

The global setting "Language (output / LLM)" (`LLM_OUTPUT_LANGUAGE`) is **not**
appended to the prompt for this mode. Useful for a translation mode, for
example: "Translate into German" combined with "Answer in English" would be a
contradiction.

Without the flag, the output language is applied as usual.

## Placeholders in the prompt text

These placeholders are replaced in the `prompt` text:

| Placeholder | Content |
|-------------|---------|
| `{name}` | Canonical name from "Personal data" |
| `{default_salutation}` | Default salutation (`LETTER_SALUTATION`) |
| `{closing}` | Closing phrase (`LETTER_CLOSING`) |
| `{contact_block}` | Contact block from the profile fields |
