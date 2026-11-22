# Text insertion: synthetic keystrokes (Quartz CGEvent)

Transcribed text is inserted by putting it on the clipboard (`NSPasteboard`) and
synthesising a `Cmd+V` keystroke via **Quartz `CGEvent`**. The "LLM working"
heartbeat and the delete/undo/enter shortcuts use the same `CGEvent` path.

**Why not AppleEvents / `osascript "System Events"`:** driving System Events
needs the macOS *Automation* permission. An ad-hoc signed, un-notarised bundle
never gets the Automation consent dialog — every attempt just fails with
`-1743 (errAEEventNotPermitted)`. `CGEvent` synthesis only needs the
*Accessibility* grant the app already requires for hotkey detection, so it works
without notarisation.

**Private event source — do not remove.** All synthetic events are created from
a `CGEventSourceCreate(kCGEventSourceStatePrivate)` source, and their flags are
set explicitly on *every* event (`CGEventSetFlags`, `0` or `Command`). This is
load-bearing: when `Fn+Fn` is double-tapped quickly, macOS can coalesce the
`Fn` key-up, leaving the global modifier state with `Fn` stuck "on". A synthetic
event created from the *shared* state then inherits that stuck `Fn` — `Cmd+V`
silently becomes `Fn+Cmd+V` (no paste) and heartbeat characters become chords
(invisible). The whole dictation cycle then produces nothing, intermittently.
A private-state source has its own modifier table and is immune to this. See
`_cg_source()` / `_cg_post_key()` / `_cg_type_char()` in `whisper_dictate.py`.

If a future change makes paste intermittently insert nothing while manual
`Cmd+V` still works, suspect modifier-flag contamination of synthetic events
first.
