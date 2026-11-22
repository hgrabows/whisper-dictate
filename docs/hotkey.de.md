# Hotkey: Doppel-Fn

*[English version](hotkey.md)*

WhisperDictate startet und stoppt die Aufnahme über einen **Doppel-Tap auf die
Fn-Taste**: Fn schnell zweimal antippen (innerhalb von ca. 0,6 s).

## Wie die Erkennung funktioniert

Die App belauscht den globalen Tastatur-Event-Strom (CGEvent HID-Tap) und zählt
die **steigenden Flanken** der physischen Fn-Taste. Zwei solche Flanken
innerhalb eines Zeitfensters (Default 0,6 s, konfigurierbar über
`DOUBLE_TAP_WINDOW`) gelten als Doppel-Tap.

Härtung in mehreren Schichten (jede blockt eine andere Klasse von Fehlauslösern):

1. **Keycode-Gate** — ein `flagsChanged`-Event zählt nur, wenn es von der
   physischen Fn-Taste (**Keycode 63**) stammt. Das ist entscheidend: Das
   `SecondaryFn`-Bit wird auch von Pfeil-/Navigationstasten mitgetragen und
   reitet auf den `flagsChanged`-Events anderer Modifier mit. Ohne dieses Gate
   würden Tab, Pfeiltasten, Cmd+Tab oder ein Makro, das Modifier toggelt, als
   Fn-Taps durchgehen.
2. **Mindestabstand (~40 ms)** — die zwei Taps müssen mindestens `min_gap`
   auseinanderliegen. Ein menschlicher Doppel-Tap liegt bei ~80–250 ms; ein
   Makro-*Flacker* toggelt das Flag in deutlich unter einer Millisekunde.
   Alles schneller als `min_gap` ist ein Maschinen-Artefakt, kein Tap.
3. **Flankenerkennung** — nur die *steigende* Flanke (aus → an) zählt, damit
   Halten nicht wiederholt auslöst.
4. **Cooldown + Reset** — nach einem Treffer wird der Zustand zurückgesetzt und
   ~0,3 s lang werden weitere Taps verschluckt. Ein "stale reset" recovert
   zusätzlich von verschluckten Fn-Up-Events.

Diagnose: Die ersten ~40 `flagsChanged`-Events werden mit ihrem Keycode ins Log
geschrieben (`🔎 flagsChanged keycode=… fn_bit=…`). Falls Fn+Fn auf exotischer
Hardware nicht mehr reagiert, zeigt das Log, welchen Keycode die Fn-Taste dort
sendet (dann muss `FN_KEYCODE` in `key_handler.py` angepasst werden).

## Früheres Randproblem: Fn + Buchstabe / Tab / Cmd+Tab löste fälschlich aus (gelöst)

In Setups mit **externen Makro-Tools** (Karabiner-Elements, BetterTouchTool,
Hyperkey o.ä.) und bei bestimmten Navigationstasten konnte die Aufnahme starten,
ohne dass Fn doppelt getippt wurde — z.B. beim Auslösen eines `Fn+P`-Makros,
bei `Tab`, Pfeiltasten oder `Cmd+Tab`.

**Ursache:** Der Detektor zählte früher *jede* steigende Flanke des
Fn-Modifier-**Bits**, egal welche Taste das Event ausgelöst hatte. Zwei Quellen
erzeugten Phantom-Flanken:

1. Das `SecondaryFn`-Bit wird von macOS auch für Pfeil-/Navigationstasten
   gesetzt und von anderen Modifier-`flagsChanged`-Events mitgetragen — diese
   sahen aus wie Fn-Taps.
2. Makro-Tools **flackern** beim Remapping den Fn-Modifier (aus → an → aus) in
   sub-Millisekunden. Zwei solche Flanken im 0,6-s-Fenster = falscher
   „Doppel-Tap".

**Behoben** durch die oben beschriebenen Schichten **Keycode-Gate** (nur die
physische Fn-Taste, Keycode 63) und **Mindestabstand** (Flacker unter ~40 ms
zählt nicht). Echtes Fn+Fn passiert beide Filter; Fn+\<Buchstabe\>, Tab,
Pfeiltasten und Cmd+Tab werden zuverlässig ignoriert.

### Weitere Optionen

- **GUI-Button** „Aufnahme starten/stoppen" — umgeht die Fn-Erkennung ganz.
- **Detektor-Fenster anpassen**: `DOUBLE_TAP_WINDOW` (Default 0,6 s) als
  Umgebungsvariable.
- **Fallback Ctrl**: Auf Macs ohne Fn-kompatible Tastatur erkennt die App
  alternativ einen Doppel-Tap auf Ctrl (automatischer Fallback).

> Hinweis: Ein noch früherer Versuch, jeden `keyDown` als „Abbruchsignal" für
> einen anstehenden Tap zu werten, verschluckte in Makro-Setups auch echte
> Doppel-Taps und wurde verworfen. Der jetzige Keycode-/Gap-Ansatz braucht
> keine `keyDown`-Heuristik.
