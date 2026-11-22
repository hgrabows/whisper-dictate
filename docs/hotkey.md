# Hotkey: double-tap Fn

*[Deutsche Version](hotkey.de.md)*

WhisperDictate starts and stops recording with a **double tap on the Fn key**:
tap Fn twice quickly (within about 0.6 s).

## How detection works

The app listens to the global keyboard event stream (CGEvent HID tap) and counts
**rising edges** of the physical Fn key. Two such edges within a time window
(default 0.6 s, configurable via `DOUBLE_TAP_WINDOW`) count as a double tap.

Detection is hardened in several layers, each blocking a different class of
false triggers:

1. **Keycode gate**: a `flagsChanged` event only counts if it comes from the
   physical Fn key (**keycode 63**). This matters because the `SecondaryFn` bit
   is also carried by arrow/navigation keys and rides along on the
   `flagsChanged` events of other modifiers. Without this gate, Tab, arrow
   keys, Cmd+Tab or a macro that toggles modifiers would pass as Fn taps.
2. **Minimum gap (~40 ms)**: the two taps must be at least `min_gap` apart. A
   human double tap takes ~80–250 ms; a macro *flicker* toggles the flag in well
   under a millisecond. Anything faster than `min_gap` is a machine artefact,
   not a tap.
3. **Edge detection**: only the *rising* edge (off → on) counts, so holding Fn
   does not trigger repeatedly.
4. **Cooldown + reset**: after a hit the state is reset and further taps are
   ignored for ~0.3 s. A "stale reset" also recovers from swallowed Fn-up
   events.

Diagnostics: the first ~40 `flagsChanged` events are logged with their keycode
(`🔎 flagsChanged keycode=… fn_bit=…`). If Fn+Fn stops reacting on unusual
hardware, the log shows which keycode the Fn key sends there (then adjust
`FN_KEYCODE` in `key_handler.py`).

## Former edge case: Fn + letter / Tab / Cmd+Tab triggered recording (fixed)

With **external macro tools** (Karabiner-Elements, BetterTouchTool, Hyperkey
and similar) and with certain navigation keys, recording could start without a
real Fn double tap, for example when firing an `Fn+P` macro, or on `Tab`, arrow
keys or `Cmd+Tab`.

**Cause:** the detector used to count *every* rising edge of the Fn modifier
**bit**, regardless of which key produced the event. Two sources produced
phantom edges:

1. macOS also sets the `SecondaryFn` bit for arrow/navigation keys, and other
   modifier `flagsChanged` events carry it along; these looked like Fn taps.
2. Macro tools **flicker** the Fn modifier (off → on → off) within
   sub-milliseconds while remapping. Two such edges in the 0.6 s window looked
   like a double tap.

**Fixed** by the **keycode gate** (physical Fn key only, keycode 63) and the
**minimum gap** (flicker under ~40 ms does not count) described above. A real
Fn+Fn passes both filters; Fn+\<letter\>, Tab, arrow keys and Cmd+Tab are
reliably ignored.

### Other options

- **GUI button** "Start recording / Stop recording" bypasses Fn detection
  entirely.
- **Adjust the detection window**: set `DOUBLE_TAP_WINDOW` (default 0.6 s) as an
  environment variable.
- **Ctrl fallback**: on Macs without an Fn-compatible keyboard the app falls
  back to detecting a double tap on Ctrl.

> Note: an earlier attempt to treat every `keyDown` as a "cancel" signal for a
> pending tap also swallowed real double taps in macro setups and was dropped.
> The current keycode/gap approach needs no `keyDown` heuristic.
