import time
import threading
from typing import Callable, Optional

try:
    from Quartz import (
        CGEventTapCreate,
        kCGHIDEventTap,
        kCGSessionEventTap,
        kCGAnnotatedSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionListenOnly,
        CGEventMaskBit,
        CGEventTapEnable,
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFMachPortCreateRunLoopSource,
        CGEventGetFlags,
        CGEventGetIntegerValueField,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGKeyboardEventAutorepeat,
        kCGKeyboardEventKeycode,
    )
    import Quartz

    kCGEventTapDisabledByTimeout = getattr(Quartz, "kCGEventTapDisabledByTimeout", None)
    kCGEventTapDisabledByUserInput = getattr(Quartz, "kCGEventTapDisabledByUserInput", None)
    HAS_QUARTZ = True
    QUARTZ_IMPORT_ERROR = ""
except Exception as e:
    HAS_QUARTZ = False
    QUARTZ_IMPORT_ERROR = f"{type(e).__name__}: {e}"
    Quartz = None  # type: ignore
    CGEventTapCreate = None  # type: ignore
    kCGSessionEventTap = None  # type: ignore
    kCGHIDEventTap = None  # type: ignore
    kCGAnnotatedSessionEventTap = None  # type: ignore
    kCGHeadInsertEventTap = None  # type: ignore
    kCGEventTapOptionListenOnly = None  # type: ignore
    CGEventMaskBit = None  # type: ignore
    CGEventTapEnable = None  # type: ignore
    CFRunLoopAddSource = None  # type: ignore
    CFRunLoopGetCurrent = None  # type: ignore
    CFMachPortCreateRunLoopSource = None  # type: ignore
    CGEventGetFlags = None  # type: ignore
    CGEventGetIntegerValueField = None  # type: ignore
    kCGEventFlagsChanged = None  # type: ignore
    kCGEventKeyDown = None  # type: ignore
    kCGEventTapDisabledByTimeout = None  # type: ignore
    kCGEventTapDisabledByUserInput = None  # type: ignore
    kCGKeyboardEventAutorepeat = None  # type: ignore
    kCGKeyboardEventKeycode = None  # type: ignore


# Physical key codes for the modifier keys we watch via flagsChanged. macOS
# reports the keycode of the key that toggled, even on a flagsChanged event.
FN_KEYCODE = 63             # kVK_Function (the physical Fn / Globe key)
CONTROL_KEYCODES = (59, 62)  # left / right Control


class DoubleTapDetector:
    """Detects a "tap twice quickly" on a modifier key.

    Hardening over the naive version, in layers — each blocks a different class
    of false trigger seen with macro tools (Karabiner etc.) and macOS quirks:

      1. Keycode gate — a flagsChanged event only counts if it was produced by
         the physical watched key (e.g. Fn = keycode 63). Crucial: the
         SecondaryFn flag is *also* carried by arrow / navigation keys and rides
         along on other modifiers' flagsChanged events, so without this gate
         pressing Tab, arrows, Cmd+Tab or a macro that toggles modifiers would
         masquerade as Fn taps.
      2. Minimum gap — the two taps must be at least `min_gap` apart. A human
         double-tap is ~80–250 ms; a macro's modifier *flicker* toggles the
         flag in well under a millisecond. Anything faster than `min_gap` is a
         machine artefact, not a tap.
      3. Edge detection — only the *rising* edge of the watched flag (off → on)
         counts, so holding the key doesn't re-fire.
      4. Reset on success + cooldown — after a double-tap fires, state resets and
         further taps are swallowed for `cooldown` seconds.
    """

    def __init__(
        self,
        flag_mask_primary,
        window: float,
        label: str = "Fn",
        cooldown: float = 0.3,
        expected_keycodes=None,
        min_gap: float = 0.04,
    ):
        self.flag_mask_primary = flag_mask_primary
        self.window = window
        self.label = label
        self.cooldown = cooldown
        # tuple of accepted physical keycodes, or None to skip the gate
        self.expected_keycodes = tuple(expected_keycodes) if expected_keycodes else None
        self.min_gap = min_gap
        self.last_tap_time = 0.0
        self._flag_was_set = False
        self._cooldown_until = 0.0
        self._last_event_time = 0.0
        self._stale_reset_sec = 1.5

    def reset(self) -> None:
        self.last_tap_time = 0.0
        self._flag_was_set = False
        self._cooldown_until = 0.0
        self._last_event_time = 0.0

    def register_tap_and_check(self, flags, keycode=None):
        now = time.time()

        # (1) Keycode gate: ignore flagsChanged events not produced by the
        # physical watched key. These never touch our edge state, so a Shift /
        # Cmd toggle or an arrow key carrying the Fn flag can't desync us.
        if self.expected_keycodes is not None:
            if keycode is None or int(keycode) not in self.expected_keycodes:
                return False

        # If we did not see (matching) modifier events for a while, clear stale
        # edge state — recovers from a dropped key-up edge.
        if self._last_event_time > 0.0 and (now - self._last_event_time) > self._stale_reset_sec:
            self._flag_was_set = False
        self._last_event_time = now
        flag_is_set = bool(flags & self.flag_mask_primary)
        prev_was_set = self._flag_was_set
        self._flag_was_set = flag_is_set

        # (3) Only the rising edge (off → on) counts as a tap.
        if not flag_is_set or prev_was_set:
            return False

        # Ignore taps inside the post-detection cooldown without updating
        # last_tap_time, so the first tap after the cooldown starts fresh.
        if now < self._cooldown_until:
            return False

        prev_tap_time = self.last_tap_time
        self.last_tap_time = now

        # (2) Need a previous tap inside the window AND no closer than min_gap.
        if prev_tap_time > 0.0:
            gap = now - prev_tap_time
            if self.min_gap <= gap <= self.window:
                self.last_tap_time = 0.0
                self._cooldown_until = now + self.cooldown
                return True
            # Too fast (machine flicker) or too slow: the current edge becomes
            # the new anchor (already stored above) and we wait for a real tap.
        return False


CTRL_X_KEYCODE = 7


def _create_event_tap(mask, callback):
    """Create the first available event tap, preferring HID for Fn reliability."""
    tap_locations = [
        ("hid", getattr(Quartz, "kCGHIDEventTap", kCGHIDEventTap)),
        ("session", getattr(Quartz, "kCGSessionEventTap", kCGSessionEventTap)),
        ("annotated", getattr(Quartz, "kCGAnnotatedSessionEventTap", kCGAnnotatedSessionEventTap)),
    ]
    for name, location in tap_locations:
        if location is None:
            continue
        try:
            tap = CGEventTapCreate(
                location,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
                mask,
                callback,
                None,
            )
        except Exception:
            tap = None
        if tap:
            print(f"⌨️ EventTap location: {name}")
            return tap
    return None


_flagschanged_debug_remaining = 40


def _maybe_debug_flagschanged(keycode, flags) -> None:
    """Log the first handful of flagsChanged keycodes so we can confirm the
    physical Fn key reports keycode 63 on this hardware. Self-limiting, then
    silent — keeps the log usable while still diagnosable if Fn ever breaks."""
    global _flagschanged_debug_remaining
    if _flagschanged_debug_remaining <= 0:
        return
    _flagschanged_debug_remaining -= 1
    try:
        fn_mask = getattr(Quartz, "kCGEventFlagMaskSecondaryFn", 0) or 0
        fn_set = bool(flags & fn_mask) if fn_mask else False
        print(f"🔎 flagsChanged keycode={keycode} fn_bit={'1' if fn_set else '0'}")
    except Exception:
        pass


def _is_ctrl_x_keydown(event) -> bool:
    try:
        is_repeat = bool(CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat))
        if is_repeat:
            return False
        keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
        if keycode != CTRL_X_KEYCODE:
            return False
        flags = CGEventGetFlags(event)
        ctrl_mask = getattr(Quartz, "kCGEventFlagMaskControl", 0)
        # Plain Ctrl+X only: Ctrl+Shift+X, Ctrl+Option+X, Ctrl+Cmd+X belong
        # to other apps' shortcuts and must not trigger the burst delete.
        other_mask = (
            getattr(Quartz, "kCGEventFlagMaskShift", 0)
            | getattr(Quartz, "kCGEventFlagMaskAlternate", 0)
            | getattr(Quartz, "kCGEventFlagMaskCommand", 0)
        )
        return bool(flags & ctrl_mask) and not (flags & other_mask)
    except Exception:
        return False


def get_default_detector(double_tap_window: float) -> Optional[DoubleTapDetector]:
    if not HAS_QUARTZ:
        return None
    fn_mask = getattr(Quartz, "kCGEventFlagMaskSecondaryFn", None)
    ctrl_mask = getattr(Quartz, "kCGEventFlagMaskControl", None)
    if fn_mask is not None:
        return DoubleTapDetector(
            fn_mask, double_tap_window, label="Fn",
            expected_keycodes=(FN_KEYCODE,),
        )
    if ctrl_mask is not None:
        return DoubleTapDetector(
            ctrl_mask, double_tap_window, label="Control",
            expected_keycodes=CONTROL_KEYCODES,
        )
    return None


def listen_for_double_tap(
    detector: DoubleTapDetector,
    on_double_tap: Callable[[str], None],
    on_ctrl_x: Optional[Callable[[], None]] = None,
):
    """Install an event tap and block the run loop, invoking on_double_tap on each double tap."""
    if not HAS_QUARTZ or detector is None:
        print("❌ Event tap unavailable; exiting.")
        return

    tap_ref = {"tap": None}
    fallback_ctrl_detector = None
    ctrl_mask = getattr(Quartz, "kCGEventFlagMaskControl", None)
    if detector is not None and detector.label == "Fn" and ctrl_mask is not None:
        fallback_ctrl_detector = DoubleTapDetector(
            ctrl_mask,
            detector.window,
            label="Control",
            cooldown=detector.cooldown,
            expected_keycodes=CONTROL_KEYCODES,
        )

    disabled_types = {evt for evt in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput) if evt is not None}

    def event_callback(proxy, event_type, event, refcon):
        try:
            if disabled_types and event_type in disabled_types:
                tap = tap_ref.get("tap")
                if tap is not None:
                    CGEventTapEnable(tap, True)
                detector.reset()
                if fallback_ctrl_detector is not None:
                    fallback_ctrl_detector.reset()
                reason = "timeout" if event_type == kCGEventTapDisabledByTimeout else "user input"
                print(f"⚠️ Event tap disabled ({reason}) – re-enabled.")
            elif event_type == kCGEventFlagsChanged and detector:
                flags = CGEventGetFlags(event)
                keycode = None
                try:
                    keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
                except Exception:
                    keycode = None
                _maybe_debug_flagschanged(keycode, flags)
                if detector.register_tap_and_check(flags, keycode):
                    on_double_tap(detector.label)
                elif fallback_ctrl_detector is not None and fallback_ctrl_detector.register_tap_and_check(flags, keycode):
                    on_double_tap(fallback_ctrl_detector.label)
            elif event_type == kCGEventKeyDown and on_ctrl_x is not None:
                if _is_ctrl_x_keydown(event):
                    on_ctrl_x()
        except Exception as e:
            print("Event error:", e)
        return event

    mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
    tap = _create_event_tap(mask, event_callback)
    if not tap:
        print("❌ Could not create event tap. Check Accessibility permissions.")
        return
    tap_ref["tap"] = tap
    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    try:
        from CoreFoundation import CFRunLoopRun

        print("🟢 Ready. Double-tap Fn (or Control) to record. Ctrl+C to quit.")
        CFRunLoopRun()
    except KeyboardInterrupt:
        pass


def listen_for_double_tap_stoppable(
    detector: DoubleTapDetector,
    on_double_tap: Callable[[str], None],
    stop_event: threading.Event,
    poll_interval_sec: float = 0.25,
    on_ctrl_x: Optional[Callable[[], None]] = None,
):
    """Install an event tap and run until stop_event is set."""
    if not HAS_QUARTZ or detector is None:
        print("❌ Event tap unavailable; exiting.")
        return

    tap_ref = {"tap": None}
    fallback_ctrl_detector = None
    ctrl_mask = getattr(Quartz, "kCGEventFlagMaskControl", None)
    if detector is not None and detector.label == "Fn" and ctrl_mask is not None:
        fallback_ctrl_detector = DoubleTapDetector(
            ctrl_mask,
            detector.window,
            label="Control",
            cooldown=detector.cooldown,
            expected_keycodes=CONTROL_KEYCODES,
        )

    disabled_types = {evt for evt in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput) if evt is not None}

    def event_callback(proxy, event_type, event, refcon):
        try:
            if disabled_types and event_type in disabled_types:
                tap = tap_ref.get("tap")
                if tap is not None:
                    CGEventTapEnable(tap, True)
                detector.reset()
                if fallback_ctrl_detector is not None:
                    fallback_ctrl_detector.reset()
                reason = "timeout" if event_type == kCGEventTapDisabledByTimeout else "user input"
                print(f"⚠️ Event tap disabled ({reason}) – re-enabled.")
            elif event_type == kCGEventFlagsChanged and detector:
                flags = CGEventGetFlags(event)
                keycode = None
                try:
                    keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
                except Exception:
                    keycode = None
                _maybe_debug_flagschanged(keycode, flags)
                if detector.register_tap_and_check(flags, keycode):
                    on_double_tap(detector.label)
                elif fallback_ctrl_detector is not None and fallback_ctrl_detector.register_tap_and_check(flags, keycode):
                    on_double_tap(fallback_ctrl_detector.label)
            elif event_type == kCGEventKeyDown and on_ctrl_x is not None:
                if _is_ctrl_x_keydown(event):
                    on_ctrl_x()
        except Exception as e:
            print("Event error:", e)
        return event

    mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
    tap = _create_event_tap(mask, event_callback)
    if not tap:
        print("❌ Could not create event tap. Check Accessibility permissions.")
        return
    tap_ref["tap"] = tap
    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)

    from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

    print("🟢 Ready. Double-tap Fn (or Control) to record. Close the GUI to quit.")
    while not stop_event.is_set():
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, poll_interval_sec, True)
