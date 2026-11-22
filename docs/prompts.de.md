# Prompt-Handling

*[English version](prompts.md)*

WhisperDictate verarbeitet das Transkript nach der Spracherkennung optional mit
einem LLM. Welcher Prompt dabei verwendet wird, bestimmt der **Modus**.

## Dateien

| Datei | Rolle |
|-------|-------|
| `prompt.json` | Deutsche Vorlage (im Repo). |
| `prompt.en.json` | Englische Vorlage (im Repo). Beim ersten Start wird die Vorlage passend zur Sprache der Oberfläche (`UI_LANGUAGE`) kopiert. |
| `current_prompts.json` | Aktive Prompts, von git ignoriert. |

Wird die aktive Datei gelöscht, erzeugt die App sie beim nächsten Start neu aus
der Vorlage.

## Aufbau eines Modus

```json
{
  "modes": {
    "4": {
      "label": "Brief erstellen (sendefertig)",
      "add_signature": true,
      "prompt": "Du bist ein deutscher Schreibassistent. …"
    }
  }
}
```

| Feld | Pflicht | Bedeutung |
|------|---------|-----------|
| `label` | ja | Anzeigename in der GUI |
| `prompt` | ja | Der System-Prompt für das LLM |
| `add_signature` | nein | siehe unten |
| `skip_output_language` | nein | siehe unten |

Die **Modus-Nummern sind frei wählbar** und tragen keine Programm-Semantik mehr
— jedes Sonderverhalten wird ausschließlich über die Flags gesteuert. Nummern
sollten lückenlos sein (1, 2, 3, …), damit die Liste in der GUI sauber aussieht.

Zwei Modi sind fest reserviert und stehen *nicht* in der JSON:
`0` = „Kein Post-Processing", `-5` = „Post-Processing ohne KI".

## Optionen (Flags)

### `add_signature: true`

Der Modus hängt die **Signatur / den Kontaktblock** (Adresse, E-Mail, Telefon
aus den „Persönliche Daten") unter den Namen — analog zur Signatur-Funktion in
Mail-Programmen. Konkret:

- Der LLM erhält die Anweisung, die Kontaktangaben unter den Namen zu setzen.
- Nach dem LLM wird der Kontaktblock deterministisch erzwungen (falls der LLM
  ihn vergisst oder verändert).
- Ist gar kein LLM-Provider konfiguriert, wird ein Fallback-Brief-Layout
  (Anrede, Text, Grußformel, Name, Kontaktblock) erzeugt.

### `skip_output_language: true`

Die globale Einstellung „Sprache (Ausgabe / LLM)" (`LLM_OUTPUT_LANGUAGE`) wird
für diesen Modus **nicht** an den Prompt gehängt. Sinnvoll z.B. für einen
Übersetzungs-Modus: „Übersetze ins Englische" und gleichzeitig
„Antwortsprache: Deutsch" wäre ein Widerspruch.

Ohne das Flag wird die Ausgabesprache normal angewendet.

## Platzhalter im Prompt-Text

Im `prompt`-Text werden folgende Platzhalter ersetzt:

| Platzhalter | Inhalt |
|-------------|--------|
| `{name}` | Kanonischer Name aus den „Persönliche Daten" |
| `{default_salutation}` | Standard-Anrede (`LETTER_SALUTATION`) |
| `{closing}` | Grußformel (`LETTER_CLOSING`) |
| `{contact_block}` | Kontaktblock aus den Profil-Feldern |
