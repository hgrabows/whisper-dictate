# WhisperDictate

*[English version](README.md)*

Lokales Diktieren für macOS. Zweimal **Fn** tippen, sprechen, wieder zweimal
**Fn** tippen, und der Text steht in der App, in der du gerade schreibst:
Mail, Editor, Browser, Terminal.

- **Läuft auf deinem Mac.** Die Spracherkennung nutzt
  [whisper.cpp](https://github.com/ggml-org/whisper.cpp) oder
  [Cactus Whistle](https://huggingface.co/Cactus-Compute/whistle). Deine
  Aufnahmen verlassen den Rechner nicht.
- **Schnell.** Das Modell bleibt in einem `whisper-server` im Hintergrund
  geladen. Ein typisches Diktat ist 0,5–1,2 s nach dem Sprechen fertig
  (large-v3-turbo, Apple Silicon).
- **Gesprochene Satzzeichen und Befehle** auf Deutsch und Englisch: „Komma“,
  „neue Zeile“, „lösche das“, „drücke Enter“ …
- **Optionale LLM-Überarbeitung**: Aus einem groben Diktat wird eine fertige
  E-Mail, ein Brief oder Quellcode, mit Claude, ChatGPT, Gemini, DeepSeek,
  einem beliebigen OpenAI-kompatiblen Server oder einem lokalen Modell.
  Standardmäßig aus.
- **Oberfläche auf Deutsch oder Englisch.**

> Status: privates Projekt, täglich auf Apple Silicon im Einsatz. Rechne mit
> Ecken und Kanten.

## Installation

**Voraussetzung:** macOS auf Apple Silicon mit [Homebrew](https://brew.sh).
Getestet auf einem M1 Max mit macOS 26.6, whisper.cpp 1.8.0, SoX 14.4.2 und
uv 0.11.

1. **Werkzeuge installieren:**

   ```bash
   brew install uv sox whisper-cpp
   ```

   - `uv` verwaltet Python und alle Python-Pakete. Python 3.12 lädt es selbst;
     eine eigene Python-Installation brauchst du nicht.
   - `sox` nimmt das Mikrofon auf.
   - `whisper-cpp` liefert die Spracherkennung (`whisper-cli` und
     `whisper-server`).

2. **Code holen und Python-Pakete installieren:**

   ```bash
   git clone https://github.com/hgrabows/whisper-dictate.git
   cd whisper-dictate
   uv sync
   ```

3. **Starten:**

   ```bash
   uv run python whisper_dictate.py
   ```

   Das Einstellungsfenster öffnet sich.

4. **Sprachmodell laden:** Beim ersten Start bietet die App an,
   **large-v3-turbo** (574 MB) zu laden. Klicke **Ja**; sie prüft die
   Prüfsumme und schaltet das Modell ein. Später geht das auch im Menü
   **Whisper-Modell** (siehe [Sprachmodelle](#sprachmodelle)). Fehlt `sox` oder
   `whisper-cpp`, sagt die App das und zeigt den passenden `brew install`-Befehl.

5. **macOS-Rechte erteilen:** Unter *Systemeinstellungen → Datenschutz &
   Sicherheit* braucht das Programm, aus dem du WhisperDictate startest
   (Terminal, iTerm, VS Code …), diese Rechte:
   - **Mikrofon**: macOS fragt beim ersten Diktat nach.
   - **Bedienungshilfen**: für den Fn-Doppeltipp und das Einfügen des Texts.
   - **Eingabeüberwachung**: für den globalen Hotkey.

   Die letzten beiden musst du von Hand einschalten. Der Knopf
   **Berechtigungen prüfen…** im Einstellungsfenster öffnet die richtigen
   Seiten. Danach die App neu starten.

6. **Fn-Taste für die App freigeben:** Sonst reagiert macOS selbst auf Fn.
   - *Systemeinstellungen → Tastatur*: **„Taste 🌐 drücken für“** auf
     **„Nichts tun“** stellen. Sonst öffnet Fn die Emoji-Auswahl oder wechselt
     die Eingabequelle.
   - Falls du die macOS-Diktierfunktion nutzt: unter *Systemeinstellungen →
     Tastatur → Diktieren* den **Kurzbefehl** auf etwas anderes als „🌐 zweimal
     drücken“ stellen. Sonst startet ein Fn-Doppeltipp zusätzlich das
     Apple-Diktat.

**Was die App selbst herunterlädt:**
- Die Sprachmodelle, die du im Menü wählst.
- Bei Whistle einmalig dessen Engine (`libcactus`, feste Version und
  Prüfsumme) nach `~/.cache/cactus/`.

Sonst nichts. Beim ersten Start legt sie `config.json`, `credentials.yaml` und
`current_prompts.json` aus den Vorlagen im Repo an. Diese Dateien enthalten
deine Einstellungen und werden von git ignoriert.

### Optional: lokales LLM

Für die LLM-Überarbeitung ohne Cloud-Dienst installierst du das Extra. Dabei
wird `llama-cpp-python` kompiliert; das braucht die Xcode-Kommandozeilentools
(`xcode-select --install`) und dauert einige Minuten:

```bash
uv sync --extra local-llm
```

Danach im Fenster **LLM** ein GGUF-Modell herunterladen. Siehe
[LLM-Überarbeitung](#llm-überarbeitung).

### Optional: beim Anmelden starten

Setze im Einstellungsfenster das Häkchen **Beim Anmelden automatisch
starten**. Ab der nächsten Anmeldung startet macOS die App aus diesem Ordner
(mit dem Python aus `.venv`, ohne Terminalfenster). Häkchen entfernen löscht den
Eintrag wieder.

Ohne Terminal prüft macOS die Rechte des **Python-Programms** selbst. Die App
zeigt seinen Pfad direkt nach dem Häkchen an und legt ihn in die
Zwischenablage; er sieht etwa so aus:
`~/.local/share/uv/python/cpython-3.12.…/bin/python3.12`. Füge ihn unter
*Systemeinstellungen → Datenschutz & Sicherheit* bei **Bedienungshilfen** und
**Eingabeüberwachung** hinzu („+“, dann ⌘⇧G und den Pfad einfügen). Installiert
`uv` später ein neueres Python, ändert sich der Pfad, und du vergibst die Rechte
neu.

Protokolle des Autostarts: `~/Library/Logs/WhisperDictate/`.

## Bedienung

1. Den Cursor dorthin setzen, wo der Text hin soll.
2. Zweimal **Fn** tippen (oder **Aufnahme starten** klicken). Ein Ton bestätigt
   den Start.
3. Sprechen.
4. Wieder zweimal **Fn** tippen. Der Text wird am Cursor eingefügt.

Die Zwischenablage wird nach dem Einfügen wiederhergestellt.

**Was die App sonst noch kann:** ein Diktat als formelle E-Mail oder Brief
umschreiben, Code erzeugen, übersetzen, kopierten Text per Sprache bearbeiten
(„fasse das zusammen“), Hintergrund-Kontext nutzen und eigene Prompts. Die
**[Bedienungsanleitung](docs/manual.de.md)** erklärt alle Modi und Fenster.

### Gesprochene Satzzeichen

Sprich das Satzzeichen als Wort, es wird zum Zeichen. Die Regeln richten sich
nach der Erkennungssprache (`WHISPER_LANGUAGE`): deutsche Regeln bei `de`,
englische bei `en`, beide bei `auto`.

| Deutsch | Englisch | Ergebnis |
|---|---|---|
| Komma | comma | `,` |
| Punkt | period, full stop | `.` |
| Fragezeichen | question mark | `?` |
| Ausrufezeichen | exclamation mark / point | `!` |
| Doppelpunkt | colon | `:` |
| Semikolon | semicolon | `;` |
| neue Zeile | new line | Zeilenumbruch |
| neuer Absatz | new paragraph | Leerzeile |
| Anführungszeichen auf … zu | open quote … close quote | `"…"` |
| Klammer auf … zu | open paren … close paren | `(…)` |

Normale Sprache bleibt unverändert: „Das ist der Punkt.“ bleibt Text.

### Sprachbefehle

Sprich den Befehl als ganzes Diktat:

| Deutsch | Englisch | Wirkung |
|---|---|---|
| „lösche das“ | "delete that", "scratch that" | löscht den gerade eingefügten Text |
| „rückgängig“ | "undo" | macht das letzte Einfügen rückgängig (Cmd+Z) |
| „drücke Enter“ | "press enter" | drückt die Eingabetaste |

Im Modus „Kein Post-Processing“ funktioniert „Enter“ auch am Ende eines
Diktats („… bis morgen, Enter“). Die anderen Modi drücken nach dem Einfügen nie
die Eingabetaste, damit ein umformulierter Text nicht versehentlich abgeschickt
wird.

### Sprache der Oberfläche

Die Oberfläche folgt der macOS-Sprache (Deutsch, wenn Deutsch deine erste
Sprache ist, sonst Englisch). Umstellen im Einstellungsfenster oder mit
`"UI_LANGUAGE": "de"` / `"en"` in `config.json`; gilt nach einem Neustart.

## Sprachmodelle

Das Menü **Whisper-Modell** listet alle Modelle aus `whisper_models.py`. Ein
noch fehlendes Modell ist mit `⬇ <Größe> laden…` markiert. Wählst du es, lädt
die App es herunter und schaltet es ein. **Modelle verwalten…** öffnet ein
Fenster zum Herunterladen, Verwenden, Entfernen und Abbrechen.

| Key | Modell | Größe | Ø Zeit* | Hinweis |
|---|---|---|---|---|
| `large-turbo` | OpenAI large-v3-turbo, q5_0 | 574 MB | 0,9 s | **empfohlen**: Qualität wie large, schnellstes whisper.cpp-Modell |
| `whistle` | [Cactus Whistle](https://huggingface.co/Cactus-Compute/whistle) (Apache 2.0) | 17 MB | 0,03–0,5 s** | sehr schnell, aber **deutlich schwächere Erkennung**: Fachbegriffe und englische Wörter im deutschen Diktat oft falsch. Nicht als erstes Modell geeignet |
| `de-turbo` | large-v3-turbo, deutsch feinjustiert ([cstr/primeline](https://huggingface.co/cstr/whisper-large-v3-turbo-german-ggml)), q5_0 | 574 MB | 1,0 s | nur Deutsch; schreibt immer „ss“ statt „ß“ |
| `large` | OpenAI large-v3 | 3,1 GB | 2,1 s | mehrsprachig, langsam |
| `medium` | OpenAI medium | 1,5 GB | 1,3 s | kleiner, älter |

\* Gemessen mit fünf echten deutschen Diktaten (5–25 s, viele englische
Fachbegriffe), whisper.cpp 1.8.0, Apple Silicon, vor Einführung von
`whisper-server`. Nur eine Stichprobe. `medium`, `large` und `large-turbo`
lieferten vergleichbare Qualität mit jeweils 1–2 kleinen Fehlern.

\*\* Whistle: gemessen mit sechs Diktaten (2–42 s) auf einem M1 Max; turbo
brauchte für dieselben Aufnahmen 0,8–1,6 s. Whistle läuft direkt im
App-Prozess (Paket `cactus-compute`, kommt mit `uv sync`) und kann en, de, fr,
es, it, nl und pl. Die App schaltet die Cactus-Telemetrie ab.

- Downloads landen in `models/` im Repo-Ordner. Sie werden in eine
  `.part`-Datei gestreamt und gegen Größe und SHA-256 aus dem Katalog geprüft,
  bevor sie die Zieldatei ersetzen. Ein kaputter Download zerstört also nie ein
  funktionierendes Modell.
- Die Auswahl wird als `WHISPER_MODEL_SIZE` in `config.json` gespeichert; auf
  der Kommandozeile `--model-size large-turbo`.
- Fehlt das gewählte Modell, nimmt die App ein vorhandenes in der Reihenfolge
  `medium → large-turbo → large → de-turbo → whistle`.
- Turbo-Modelle brauchen whisper.cpp ≥ 1.7.

### whisper-server

Die App lädt whisper.cpp-Modelle nicht bei jedem Diktat neu. Sie startet
einmal `whisper-server` und hält das Modell geladen.
- Der Server ist nur auf `127.0.0.1` erreichbar, an einem freien Port.
- Das spart ~0,4 s pro Diktat: 0,5–1,2 s statt 0,9–1,7 s mit large-v3-turbo.
- Ein kleiner Wächter-Prozess beendet den Server, sobald die App endet, auch
  nach einem Absturz.
- Solange der Server noch lädt oder falls er ausfällt, nimmt die App
  automatisch `whisper-cli`.
- Abschalten mit `"WHISPER_SERVER": "0"` in `config.json` oder der
  Umgebungsvariable `WHISPER_SERVER=0`. Ein anderes Programm stellst du mit
  `WHISPER_SERVER_CMD` ein.
- Kosten: Das aktive Modell bleibt im Speicher, bei turbo ~600 MB.

## LLM-Überarbeitung

Standardmäßig aus: Neue Installationen starten im Modus „Post-Processing ohne KI“ (nur
gesprochene Satzzeichen und Bereinigung). Wählst du im Einstellungsfenster
einen Modus (zum Beispiel „Formell umformulieren“, „Brief erstellen“,
„Quellcode erzeugen“), überarbeitet ein LLM das Transkript vor dem Einfügen.
Modi und Prompts stehen in `current_prompts.json`; siehe
[docs/prompts.de.md](docs/prompts.de.md).

### Anbieter wählen

Öffne **LLM (Post-Processing) → Anbieter & API-Keys…**, wähle einen Anbieter,
füge deinen API-Key ein und klicke **Speichern & testen**. Die App prüft den
Key, lädt die Modellliste des Anbieters und schaltet auf ihn um. Ein anderes
Modell wählst du aus der Liste oder tippst seinen Namen ein. Eingerichtete
Anbieter wechselst du danach direkt im Menü **LLM (Post-Processing)**, Modi im
Menü **Prompts**.

| Anbieter | Anbindung | Was du einträgst |
|---|---|---|
| **Claude** (Anthropic) | offizielles Python-SDK `anthropic` | API-Key; Standardmodell Claude Opus 5.5 (Sonnet 5.5 und Haiku 5.5 sind schneller und günstiger) |
| **ChatGPT** (OpenAI) | OpenAI-Chat-API | API-Key |
| **Gemini** (Google) | Googles OpenAI-kompatibler Endpunkt | API-Key |
| **DeepSeek** | OpenAI-kompatibel | API-Key |
| **Eigener Anbieter** | jeder OpenAI-kompatible Server (OpenRouter, LM Studio, vLLM, ein Firmen-Gateway …) | Adresse, z. B. `https://host/v1`, und einen API-Key, falls der Server einen verlangt |
| **Lokal** | GGUF-Modell über `llama-cpp-python` auf deinem Mac | nichts; Extra installieren (siehe [Optional: lokales LLM](#optional-lokales-llm)) und im selben Fenster ein Modell laden. Vorlagen: Phi-4-mini, Qwen2.5 1.5B/3B, Phi-3.5-mini, SmolLM2 1.7B |

- **Keys**, die du im Fenster einträgst, liegen im **macOS-Schlüsselbund**
  (Dienst „WhisperDictate“), nicht in einer Datei. **Key entfernen** löscht
  ihn dort. Keys werden nie geloggt.
- Mit einem Cloud-Anbieter geht der **Text des Transkripts** an diesen
  Anbieter, die Aufnahme nicht.
- Bei Claude fragt die App mit niedrigem Aufwand („effort: low“) an, weil das
  Bereinigen eines Diktats eine einfache Aufgabe ist. Bei Claude Opus 5.5 ist
  außerdem Anthropics serverseitiger Fallback aktiv: Lehnt das Modell eine
  harmlose Anfrage fälschlich ab, übernimmt ein anderes Modell.

**Keys aus einer Datei oder Umgebungsvariable** funktionieren weiter.
Reihenfolge: Schlüsselbund, dann `credentials.yaml`, dann `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY`. In `credentials.yaml`
kannst du außerdem weitere OpenAI-kompatible Anbieter eintragen; sie erscheinen
dann in der Liste:

```yaml
providers:
  openrouter:
    api_url: "https://openrouter.ai/api/v1/chat/completions"
    api_key_file: "~/.config/openrouter.key"   # oder: api_key: "..."
    models:
      - "openrouter/free"
    default_model: "openrouter/free"
```

`api_key_file` liest den Key aus einer eigenen Datei (relative Pfade gelten
neben `credentials.yaml`, `~` geht). Ein Anbieter namens `google` in
`credentials.yaml` wird für Gemini verwendet.

## Konfiguration

Einstellungen stehen in `config.json` (angelegt aus `config.template.json`).
Die meisten gibt es auch im Einstellungsfenster. Gleichnamige
Umgebungsvariablen haben Vorrang vor der Datei.

| Key | Standard | Bedeutung |
|---|---|---|
| `UI_LANGUAGE` | `auto` | Sprache der Oberfläche: `auto`, `de`, `en` |
| `WHISPER_LANGUAGE` | Sprache der Oberfläche | Erkennungssprache (`de`, `en`, `auto` …) |
| `LLM_OUTPUT_LANGUAGE` | Sprache der Oberfläche | Sprache, in der das LLM antwortet |
| `AUTO_CORRECT_MODE` | `-5` | Modus: `-5` ohne KI, `0` ohne Bearbeitung, `1`… Prompt-Modi |
| `WHISPER_MODEL_SIZE` | `medium` | Key des Sprachmodells, siehe Tabelle oben |
| `WHISPER_ARGS` | `-mc 0 -sns` | Zusätzliche whisper.cpp-Argumente (kein Segment-Kontext, Nicht-Sprach-Tokens unterdrücken: weniger wiederholte Halluzinationen) |
| `WHISPER_SERVER` | `1` | Modell in `whisper-server` geladen halten |
| `WHISPER_TAIL_FILTER` | `1` | Typische Whisper-Halluzinationen nach langer Stille entfernen |
| `POST_PROCESSING_PROVIDER` | `local` | LLM-Anbieter: `local`, `claude`, `openai`, `gemini`, `deepseek`, `custom` oder ein Name aus `credentials.yaml` |
| `CUSTOM_LLM_URL` | – | Adresse des eigenen OpenAI-kompatiblen Anbieters |
| `SHOW_SETTINGS_GUI` | `1` | Einstellungsfenster beim Start zeigen |
| `PASTE_RESTORE_CLIPBOARD` | `1` | Zwischenablage nach dem Einfügen wiederherstellen |
| `DOUBLE_TAP_WINDOW` | `0.6` | Max. Sekunden zwischen den beiden Fn-Taps (nur Umgebungsvariable) |

Kommandozeilen-Optionen: `uv run python whisper_dictate.py --help`.

## Datenschutz

- Aufnahmen landen in `tmp/` im Repo-Ordner und werden lokal erkannt. Alte
  Aufnahmen löscht die App selbst (behält die letzten 100, höchstens 14 Tage;
  `WHISPER_MAX_RECORDING_COUNT`, `WHISPER_MAX_RECORDING_AGE_DAYS`).
- Es wird nichts verschickt, außer du richtest einen API-Anbieter für die
  LLM-Überarbeitung ein.
- „Persönliche Daten“ (Name, Adresse, E-Mail, Telefon) stehen nur in deiner
  lokalen `config.json`. Die App schreibt damit deinen Namen richtig und füllt
  die Signatur im Brief-Modus.

## Fehlersuche

- **Fn-Doppeltipp tut nichts**: Bedienungshilfen und Eingabeüberwachung für das
  Programm prüfen, aus dem du WhisperDictate startest, dann neu starten.
  Prüfen, ob die Fn-Taste frei ist (Installationsschritt 6). Details:
  [docs/hotkey.de.md](docs/hotkey.de.md).
- **Text wird nicht eingefügt**: Manche Apps (Passwortfelder, Terminals mit
  „Sichere Tastatureingabe“) blockieren synthetische Tastendrücke. Der Text
  liegt trotzdem in der Zwischenablage; Cmd+V drücken. Hintergrund:
  [docs/text-insertion.md](docs/text-insertion.md) (englisch).
- **„whisper-server nicht gefunden“**: kommt mit `brew install whisper-cpp`;
  bis dahin nimmt die App `whisper-cli`.

## Entwicklung

```bash
uv sync
uv run python -m pytest -q
```

Siehe [CONTRIBUTING.md](CONTRIBUTING.md) (englisch).

## Lizenz

[MIT mit Commons Clause](LICENSE): Jeder darf die App herunterladen, nutzen,
ändern und weitergeben, privat und beruflich, auch in Firmen. Ohne eigene
Lizenz nicht erlaubt: sie zu verkaufen oder ein bezahltes Produkt bzw. einen
bezahlten Dienst anzubieten, dessen Wert ganz oder wesentlich auf ihr beruht
(auch bezahltes Hosting oder bezahlter Support). Frag dafür über ein
GitHub-Issue an. Maßgeblich ist der englische Lizenztext.

Modellgewichte werden von ihren Originalquellen
geladen und behalten ihre eigenen Lizenzen (OpenAI Whisper: MIT; Cactus
Whistle: Apache 2.0; LLM-Vorlagen: siehe
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
