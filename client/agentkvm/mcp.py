"""Serve the KVM to an agent over MCP (stdio).

The verbs only. How to USE them - look, model, act open-loop, verify once -
lives in the skill, because that is the part an agent gets wrong.
"""
from __future__ import annotations

import json
import time

from .client import Eyes
from .install import connect, load_entry
from .keys import parse_chord

#: How long a look stays fresh enough to act on. Long enough that a normal
#: look-act-verify rhythm never trips it, short enough that an agent cannot act
#: on a screen it saw minutes ago.
LOOK_TTL_S = 60.0


def serve(host: str | None = None, eyes_host: str | None = None) -> None:
    # The SDK renamed FastMCP to MCPServer in v2 and moved the module. Accept
    # both: a user's client pins whichever it pins, and failing to start over an
    # import path is a bad first experience.
    try:
        from mcp.server.mcpserver import Image, MCPServer as Server   # SDK >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server, Image   # SDK 1.x
        except ImportError:  # pragma: no cover - depends on an optional extra
            raise SystemExit("the MCP server needs the extra: pipx install './client[mcp]'")

    host, _entry = load_entry(host)
    kvm = connect(host)
    eyes = Eyes(eyes_host) if eyes_host else None
    app = Server("agentkvm")

    # Acting requires having looked. This is what separates a KVM from a HID
    # injector: a rubber ducky types into a machine it cannot see, and its whole
    # method is that nobody is watching. Requiring a recent frame makes blind
    # injection impossible THROUGH THIS INTERFACE, and makes an agent's actions
    # a response to an observed screen rather than an assumption about one.
    #
    # It is a guardrail, not an access control: the daemon's HTTP API is right
    # there and enforces nothing of the sort. It constrains the agent, not an
    # attacker who has the key.
    state = {"looked_at": 0.0}

    def _require_look(what: str) -> None:
        age = time.monotonic() - state["looked_at"]
        if state["looked_at"] == 0.0 or age > LOOK_TTL_S:
            raise ValueError(
                f"look at the screen before you {what}: this interface will not act on a "
                f"machine it has not seen ("
                + ("no look yet" if not state["looked_at"] else f"last look {age:.0f}s ago")
                + "). Call look(), decide from what is actually there, then act.")

    @app.tool()
    def look() -> Image:
        """Take a screenshot of the target's screen.

        Look before acting and once after. The screen is the only source of
        truth about what the target actually did.
        """
        shot = kvm.screenshot()
        state["looked_at"] = time.monotonic()
        # Return an Image, never base64 text. A 1080p JPEG billed as an image is
        # ~2,700 visual tokens; the same bytes handed back as a base64 STRING is
        # ~56,000 text tokens - twenty times the cost, and it fills the context
        # with characters no model can read as a picture.
        return Image(data=shot, format="jpeg")

    @app.tool()
    def click(x: float, y: float, double: bool = False) -> str:
        """Click at a fraction of the screen: x and y are 0..1, origin top-left.

        Fractions, not pixels, so a position measured on a screenshot is the
        same number you pass here regardless of resolution.
        """
        _require_look("click")
        kvm.click(x, y, double=double)
        return f"clicked ({x:.3f}, {y:.3f})"

    @app.tool()
    def move(x: float, y: float) -> str:
        """Move the pointer to a fraction of the screen without clicking."""
        _require_look("move the pointer")
        kvm.move(x, y)
        return f"moved to ({x:.3f}, {y:.3f})"

    @app.tool()
    def type_text(text: str) -> str:
        """Type text at a human pace. Returns how many characters were SENT.

        Assumes the TARGET uses a US keyboard layout. On any other layout some
        characters will arrive wrong or not at all; the count says how many
        were sent, not whether they were right. Use `press` for keys that must
        be exact. A count lower than the text length means this layout cannot
        produce some character; the rest still landed.
        """
        _require_look("type")
        n = kvm.type(text)
        return f"sent {n} of {len(text)} characters"

    @app.tool()
    def press(chord: str) -> str:
        """Press a chord and release it, e.g. 'enter', 'ctrl+c', 'ctrl+alt+del'.

        The press and release happen on the KVM microseconds apart, so a held
        key cannot stretch past the target's auto-repeat threshold.
        """
        _require_look("press keys")
        mods, keys = parse_chord(chord)
        kvm.tap(mods, keys)
        return f"pressed {chord}"

    @app.tool()
    def scroll(ticks: int) -> str:
        """Scroll the wheel. Negative scrolls down."""
        _require_look("scroll")
        kvm.scroll(ticks)
        return f"scrolled {ticks}"

    if eyes is not None:
        # These cost NO image tokens. A full-screen look is ~2700 visual
        # tokens; asking whether anything changed is a number. Poll with these,
        # look once when the answer is yes.
        @app.tool()
        def wait_for_change(roi: str = "", timeout: float = 5.0, min_px: int = 150) -> str:
            """Wait until the screen changes. Returns a description, not a picture.

            `roi` is "x,y,w,h" in full-resolution pixels, or empty for the whole
            screen. `min_px` is how many pixels must differ to count: a text
            caret is about 36 at 1080p, a mouse cursor about 380, so a threshold
            near 100 will miss a cursor that really moved.

            Use this instead of looking repeatedly to see whether something has
            finished. When it reports a change, look once.
            """
            r = eyes.wait(roi or None, timeout=timeout, min_px=min_px)
            return json.dumps(r)

        @app.tool()
        def look_at(roi: str) -> Image:
            """Screenshot ONE region: "x,y,w,h" in full-resolution pixels.

            Far cheaper than look(): a 400x200 crop is roughly 120 image tokens
            against about 2700 for a whole 1080p screen. Prefer this whenever
            you know where on the screen the answer is.
            """
            shot = eyes.roi(roi)
            state["looked_at"] = time.monotonic()
            return Image(data=shot, format="jpeg")

    @app.tool()
    def release_all() -> str:
        """Release every key and button. Use this if the target starts repeating.

        Deliberately usable without looking: it is the recovery verb, and
        needing a screenshot first would make a stuck key harder to clear.
        """
        kvm.release()
        return "released"

    app.run()
