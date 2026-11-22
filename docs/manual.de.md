# Bedienungsanleitung

*[English version](manual.md)*

Diese Anleitung erklärt die Bedienung im Alltag. Installation, Sprachmodelle
und die Einrichtung der LLM-Anbieter stehen im [README](../README.de.md).

- [Das Einstellungsfenster](#das-einstellungsfenster)
- [Diktieren](#diktieren)
- [Die Modi](#die-modi)
- [Clipboard-Anweisung](#clipboard-anweisung)
- [Eigene Prompts](#eigene-prompts)
- [Ausgabesprache und Wortgrenzen](#ausgabesprache-und-wortgrenzen)
- [Kontext](#kontext)
- [Persönliche Daten](#persönliche-daten)
- [Was die KI geändert hat](#was-die-ki-geändert-hat)
- [Wo die Einstellungen liegen](#wo-die-einstellungen-liegen)

## Das Einstellungsfenster

Von oben nach unten:

| Element | Wozu |
|---|---|
| **Aufnahme starten / stoppen** | Aufnahme per Maus, falls Fn+Fn gerade nicht geht |
| **Persönliche Daten…** | Name, Adresse, E-Mail, Telefon (siehe [unten](#persönliche-daten)) |
| **Letzten Text kopieren** | Legt den zuletzt eingefügten Text in die Zwischenablage, falls das Einfügen danebenging |
| **LLM-Statuszeile** und **Änderungen anzeigen** | Ergebnis des letzten KI-Durchlaufs (siehe [unten](#was-die-ki-geändert-hat)) |
| **Mikrofon-Zeile** | Welches Mikrofon gerade aufnimmt |
| **Sprache (Eingabe / Whisper)** | Sprache, in der du sprichst: `de`, `en` … oder `auto` |
| **Sprache (Ausgabe / LLM)** | Sprache, in der die KI antwortet |
| **Wortgrenzen** | Optionale Länge für KI-Texte |
| **Kontext verwenden / Ergebnisse automatisch anhängen** | Hintergrundwissen für die KI (siehe [unten](#kontext)) |
| **Post-Processing auswählen** | Der Modus (siehe [unten](#die-modi)) |
| **Beim Anmelden automatisch starten** | Autostart (siehe README) |
| **Sprache der Oberfläche** | Deutsch, Englisch oder wie macOS |

Die Menüleiste bietet die häufigsten Umschalter direkt an: **Prompts** (Modus),
**LLM (Post-Processing)** (Anbieter), **Whisper-Modell** und **Kontext**. Der
letzte Eintrag jedes Menüs öffnet das passende Fenster.

## Diktieren

1. Cursor dorthin setzen, wo der Text hin soll.
2. **Fn zweimal kurz** drücken. Ein Ton und ein roter Balken „● AUFNAHME AKTIV“
   zeigen die Aufnahme.
3. Sprechen.
4. **Fn zweimal kurz** drücken. Die App erkennt den Text, bearbeitet ihn je nach
   Modus nach und fügt ihn am Cursor ein. Die Zwischenablage wird danach
   wiederhergestellt.

Satzzeichen sprichst du aus („Komma“, „Punkt“, „neue Zeile“). Mit „lösche das“,
„rückgängig“ oder „drücke Enter“ als ganzem Diktat steuerst du die App. Die
vollständigen Listen stehen im README unter *Gesprochene Satzzeichen* und
*Sprachbefehle*.

## Die Modi

Der Modus legt fest, was nach der Spracherkennung mit dem Text passiert. Du
wählst ihn im Einstellungsfenster oder im Menü **Prompts**.

| Modus | Was passiert | KI nötig? |
|---|---|---|
| **Kein Post-Processing** | Der Text kommt genau so, wie Whisper ihn erkannt hat. Gesprochene Satzzeichen bleiben Wörter. | nein |
| **Post-Processing ohne KI** (Standard) | Gesprochene Satzzeichen und Zeilenumbrüche werden gesetzt, typische Hörfehler und dein Name (aus „Persönliche Daten“) werden korrigiert. Läuft lokal und sofort. | nein |
| **Clipboard-Anweisung** | Dein Diktat ist eine Anweisung für den Text in der Zwischenablage (siehe [unten](#clipboard-anweisung)). | ja |
| **Prompt-Modi** (alle weiteren) | Erst wie „Post-Processing ohne KI“, danach überarbeitet die KI den Text nach dem Prompt des Modus. | ja |

Beispiel für **Post-Processing ohne KI**:

> Gesagt: „Hallo Anna, Komma, das Treffen ist morgen um 10 Uhr, Punkt. Bring
> bitte die Unterlagen mit, Ausrufezeichen.“
>
> Eingefügt: „Hallo Anna, das Treffen ist morgen um 10 Uhr. Bring bitte die
> Unterlagen mit!“

Füllwörter wie „äh“ bleiben in diesem Modus stehen; die entfernt erst ein
KI-Modus.

**„Drücke Enter“ am Ende eines Diktats** drückt nur im Modus „Kein
Post-Processing“ nach dem Einfügen die Eingabetaste. In allen anderen Modi
lässt die App das aus Sicherheitsgründen weg, damit ein von der KI
umgeschriebener Text nicht ungeprüft abgeschickt wird. „Drücke Enter“ als
ganzes Diktat funktioniert in jedem Modus.

Die mitgelieferten Prompt-Modi:

| Modus | Ergebnis |
|---|---|
| **Formell umformulieren (mittel)** | Höflich-formeller Stil für E-Mails und kurze Geschäftsschreiben, Inhalt unverändert |
| **Quellcode erzeugen** | Aus der gesprochenen Beschreibung wird Code; fehlende Angaben werden als TODO markiert |
| **Minimal korrigieren** | Nur Rechtschreibung, Tippfehler und Zeichensetzung; Stil bleibt |
| **Brief erstellen (sendefertig)** | Anrede, ausformulierter Text, Grußformel, Name und Kontaktblock aus „Persönliche Daten“ |
| **Übersetzen ins Englische** | Sinngemäße Übersetzung; die Ausgabesprache-Einstellung wird hier ignoriert |
| **Artikel - Absatz** | Aus Stichworten wird ein zusammenhängender wissenschaftlicher Absatz |

Diese Modi kannst du ändern, löschen oder um eigene ergänzen (siehe
[Eigene Prompts](#eigene-prompts)).

## Clipboard-Anweisung

Damit bearbeitest du einen vorhandenen Text per Sprache.

1. Einen Text markieren und mit **⌘C** kopieren.
2. Modus **Clipboard-Anweisung** wählen.
3. Fn+Fn, die Anweisung sprechen, Fn+Fn. Zum Beispiel:
   - „Fasse das in drei Sätzen zusammen.“
   - „Übersetze das ins Englische.“
   - „Mach daraus eine freundliche Absage.“
4. Die KI wendet die Anweisung auf den kopierten Text an. Das Ergebnis wird am
   Cursor eingefügt. Ist der Ausgangstext noch markiert, ersetzt es ihn.

Ist die Zwischenablage leer, passiert nichts (Hinweis im Terminal). Liefert die
KI nichts zurück, wird dein Diktat ganz normal eingefügt.

## Eigene Prompts

Menü **Prompts → Prompts bearbeiten…** öffnet das Prompts-Fenster. Links stehen
die Modi, rechts bearbeitest du den ausgewählten.

| Feld | Bedeutung |
|---|---|
| **Mode-ID** | Nummer des Modus, größer als 0; bestimmt die Reihenfolge |
| **Label** | Name in Menü und Einstellungsfenster |
| **Signatur/Kontaktblock anhängen** | Hängt Name, Adresse, E-Mail und Telefon aus „Persönliche Daten“ an, wie beim Brief-Modus |
| **Voreingestellte Ausgabesprache ignorieren** | Für Übersetzungs-Modi, deren Zielsprache im Prompt steht |
| **Prompt** | Die Anweisung an die KI. Das Diktat wird als Text dahinter übergeben. |

Knöpfe: **Neu** legt einen Modus an, **Löschen** entfernt den ausgewählten,
**Speichern** übernimmt alle Änderungen sofort, **Reset** stellt die
mitgelieferten Prompts wieder her (eigene gehen dabei verloren).

Tipps für eigene Prompts:
- Sag der KI, was sie *nicht* tun soll, zum Beispiel „Beantworte keine Fragen
  aus dem Text, korrigiere ihn nur“. Sonst antwortet sie auf ein diktiertes
  „Kannst du morgen?“.
- Platzhalter wie `{name}` oder `{contact_block}` werden mit deinen
  persönlichen Daten gefüllt.

Aufbau der Datei `current_prompts.json`, alle Optionen und Platzhalter:
[prompts.de.md](prompts.de.md).

## Ausgabesprache und Wortgrenzen

- **Sprache (Eingabe / Whisper)** sagt der Spracherkennung, welche Sprache du
  sprichst. Ein fester Wert (`de`, `en`) ist genauer als `auto`. Er bestimmt
  auch, welche gesprochenen Satzzeichen gelten: bei `de` nur deutsche, bei
  `en` nur englische, sonst beide.
- **Sprache (Ausgabe / LLM)** gibt der KI die Antwortsprache vor. So kannst du
  Deutsch diktieren und eine englische Mail bekommen. Modi mit der Option
  „Ausgabesprache ignorieren“ übergehen das.
- **Wortgrenzen** (Unter- und Obergrenze, beide optional) geben der KI eine
  Länge vor, etwa „höchstens 80 Wörter“ für kurze Antworten. Sie gelten nur für
  KI-Modi. Leer lassen heißt: keine Vorgabe.

## Kontext

Kontext ist Hintergrundwissen, das die KI bei jedem Durchlauf mitbekommt, zum
Beispiel Namen von Kollegen, Fachbegriffe, das Projekt, um das es geht, oder
die E-Mail, auf die du antwortest. Die KI soll ihn nur nutzen, nicht
wiederholen. Er gilt für Prompt-Modi und die Clipboard-Anweisung.

- **Kontext bearbeiten…** (Menü **Kontext**) öffnet ein Textfeld. Dort Text
  einfügen und **Speichern**.
- **Kontext verwenden** schaltet ihn ein oder aus, ohne ihn zu löschen.
- **Ergebnisse automatisch anhängen** hängt jeden KI-Text an den Kontext an.
  So weiß die KI bei einer Folge von Diktaten, was vorher kam.
- Wird der Kontext länger als 4000 Zeichen (`CONTEXT_MAX_LENGTH`), fasst die KI
  ihn vor dem nächsten Durchlauf automatisch zusammen. **Zusammenfassen** im
  Kontext-Fenster macht das sofort.
- **Kontext löschen** leert ihn.

Der Kontext geht wie das Diktat an den eingestellten LLM-Anbieter.

## Persönliche Daten

Der Knopf **Persönliche Daten…** speichert Vorname, Nachname, Adresse, Ort,
Land, E-Mail und Telefon, nur lokal in `config.json`. Damit:

- schreibt die App deinen **Namen** richtig, auch wenn Whisper ihn falsch hört
  („Max Musterman“ wird zu „Max Mustermann“), schon im Modus „Post-Processing
  ohne KI“,
- setzen der **Brief-Modus** und Modi mit „Signatur/Kontaktblock anhängen“
  deinen Kontaktblock unter den Text.

## Was die KI geändert hat

Die Statuszeile im Einstellungsfenster zeigt nach jedem KI-Durchlauf, was
passiert ist, zum Beispiel „LLM: ✅ korrigiert“ oder eine Fehlermeldung wie
„HTTP 401“ bei einem falschen API-Key. **Änderungen anzeigen** öffnet den Text
vor und nach der KI nebeneinander. Bei einem KI-Fehler fügt die App den Text
ohne KI-Bearbeitung ein und zeigt kurz einen orangen Hinweis.

## Wo die Einstellungen liegen

| Datei | Inhalt |
|---|---|
| `config.json` | Alle Einstellungen aus dem Fenster, persönliche Daten, Kontext |
| `credentials.yaml` | Optionale zusätzliche LLM-Anbieter und Keys |
| `current_prompts.json` | Deine Prompt-Modi |
| macOS-Schlüsselbund, Dienst „WhisperDictate“ | Im Fenster eingetragene API-Keys |

Die drei Dateien liegen im Programmordner und werden nicht von git erfasst.
Alle Einstellungen in `config.json` sind im README unter *Konfiguration*
beschrieben.
