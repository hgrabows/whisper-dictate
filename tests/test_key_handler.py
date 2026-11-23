"""Ctrl+X burst-delete shortcut: only plain Ctrl+X triggers it."""

import pytest

import key_handler

Quartz = pytest.importorskip("Quartz")

CTRL = Quartz.kCGEventFlagMaskControl
SHIFT = Quartz.kCGEventFlagMaskShift
OPTION = Quartz.kCGEventFlagMaskAlternate
CMD = Quartz.kCGEventFlagMaskCommand


def _key_event(keycode: int, flags: int):
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventSetFlags(event, flags)
    return event


@pytest.mark.parametrize(
    ("keycode", "flags", "expected"),
    [
        (key_handler.CTRL_X_KEYCODE, CTRL, True),
        (key_handler.CTRL_X_KEYCODE, 0, False),
        (key_handler.CTRL_X_KEYCODE, CMD, False),
        (key_handler.CTRL_X_KEYCODE, CTRL | SHIFT, False),
        (key_handler.CTRL_X_KEYCODE, CTRL | OPTION, False),
        (key_handler.CTRL_X_KEYCODE, CTRL | CMD, False),
        (8, CTRL, False),  # Ctrl+C
    ],
)
def test_only_plain_ctrl_x_triggers(keycode, flags, expected) -> None:
    assert key_handler._is_ctrl_x_keydown(_key_event(keycode, flags)) is expected


def test_autorepeat_does_not_trigger() -> None:
    event = _key_event(key_handler.CTRL_X_KEYCODE, CTRL)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat, 1)
    assert key_handler._is_ctrl_x_keydown(event) is False
