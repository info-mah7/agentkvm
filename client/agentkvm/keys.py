"""US-layout character to HID usage. Written from the USB HID Usage Tables
(section 10, Keyboard/Keypad page), not taken from any device firmware.
"""
from __future__ import annotations

MOD_SHIFT = 0x02

# Named keys a caller refers to by word.
NAMED = {
    "enter": 0x28, "return": 0x28, "escape": 0x29, "esc": 0x29,
    "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "delete": 0x4C,
    "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pagedown": 0x4E,
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F,
    "insert": 0x49, "capslock": 0x39, "printscreen": 0x46,
    **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)},
}

MODIFIERS = {"ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04,
             "win": 0x08, "gui": 0x08, "meta": 0x08, "cmd": 0x08,
             "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rwin": 0x80}

_UNSHIFTED = {
    **{chr(c): 0x04 + c - ord("a") for c in range(ord("a"), ord("z") + 1)},
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "\n": 0x28, "\t": 0x2B, " ": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}
_SHIFTED = {
    "!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22,
    "^": 0x23, "&": 0x24, "*": 0x25, "(": 0x26, ")": 0x27,
    "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31,
    ":": 0x33, '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37, "?": 0x38,
    **{chr(c).upper(): 0x04 + c - ord("a") for c in range(ord("a"), ord("z") + 1)},
}


def to_usage(ch: str) -> tuple[int, int] | None:
    """(modifier, usage) for one character, or None if this layout cannot type it."""
    if ch in _UNSHIFTED:
        return 0, _UNSHIFTED[ch]
    if ch in _SHIFTED:
        return MOD_SHIFT, _SHIFTED[ch]
    return None


def parse_chord(spec: str) -> tuple[int, list[int]]:
    """'ctrl+shift+esc' -> (modifier bitmask, [usages]).

    A modifier-only chord is legal: 'win' on its own is a real thing to press.
    """
    parts = [p for p in spec.strip().lower().replace(" ", "+").split("+") if p]
    if not parts:
        raise ValueError("empty chord")
    mods, keys = 0, []
    for p in parts:
        if p in MODIFIERS:
            mods |= MODIFIERS[p]
        elif p in NAMED:
            keys.append(NAMED[p])
        else:
            got = to_usage(p) if len(p) == 1 else None
            if got is None:
                raise ValueError(f"unknown key {p!r} in chord {spec!r}")
            m, u = got
            mods |= m
            keys.append(u)
    if len(keys) > 6:
        raise ValueError("a boot keyboard report carries at most six keys")
    return mods, keys
