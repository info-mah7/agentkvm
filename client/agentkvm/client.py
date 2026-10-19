"""HTTP client for agentkvmd.

The verbs are thin, with one exception: typing. A per-character pace and an
accurate count of what was delivered are the difference between a demo and
something you can leave running.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

ABS_MAX = 32767


class KvmError(RuntimeError):
    pass


class Kvm:
    #: Characters per second. Fast enough to be usable, slow enough that the
    #: target's input queue keeps up: a burst loses characters with no error.
    CPS = 11.0
    #: Fraction of the gap to jitter, so a long string does not beat against
    #: whatever else polls on the target at a fixed rate.
    JITTER = 0.25

    def __init__(self, host: str, key: str, port: int = 9200, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.key = key
        self.timeout = timeout

    # --- transport ---
    def _post(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            raise KvmError(f"{path}: HTTP {e.code} {detail}") from None
        except urllib.error.URLError as e:
            raise KvmError(f"{path}: {e.reason}") from None

    def screenshot(self) -> bytes:
        req = urllib.request.Request(
            self.base + "/v1/screenshot",
            headers={"Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.URLError as e:
            raise KvmError(f"screenshot: {e}") from None

    def healthz(self) -> dict:
        with urllib.request.urlopen(self.base + "/healthz", timeout=self.timeout) as r:
            return json.loads(r.read())

    # --- pointer ---
    def move(self, fx: float, fy: float, buttons: int = 0) -> None:
        """Move to a fraction of the screen, 0..1 on each axis.

        Fractions, not pixels: nothing between the caller and the gadget needs
        to know the target's resolution, and a screenshot the caller measured
        is in the same units as the move it then makes.
        """
        self._post("/v1/mouse", {"buttons": buttons,
                                 "x": _frac(fx), "y": _frac(fy)})

    def click(self, fx: float, fy: float, button: int = 1, double: bool = False) -> None:
        self.move(fx, fy)
        for _ in range(2 if double else 1):
            self.move(fx, fy, buttons=button)
            self.move(fx, fy, buttons=0)

    def scroll(self, ticks: int) -> None:
        self._post("/v1/wheel", {"dy": max(-127, min(127, ticks))})

    # --- keyboard ---
    def tap(self, modifier: int = 0, keys: list[int] | None = None, hold_ms: int = 0) -> None:
        """One press and release, bounded on the device."""
        body = {"modifier": modifier, "keys": keys or []}
        if hold_ms:
            body["hold_ms"] = hold_ms
        self._post("/v1/tap", body)

    def release(self) -> None:
        self._post("/v1/release")

    def type(self, text: str, cps: float = 0.0, layout=None) -> int:
        """Type text. Returns the number of characters ACTUALLY sent.

        Characters this layout cannot produce are skipped, and the count says
        so. Reporting the requested length is how a silently dropped character
        becomes a mystery three steps later.
        """
        from .keys import to_usage  # local import: keeps the table out of __init__

        layout = layout or to_usage
        gap = 1.0 / max(cps or self.CPS, 0.5)
        sent = 0
        for ch in text:
            got = layout(ch)
            if got is None:
                continue
            mod, usage = got
            self.tap(mod, [usage])
            sent += 1
            time.sleep(gap * (1 + random.uniform(-self.JITTER, self.JITTER)))
        return sent


def _frac(v: float) -> int:
    """A fraction of the screen as a gadget coordinate, clamped.

    Out-of-range coordinates are refused by the firmware rather than clamped,
    so a caller that computed 1.0001 from a screenshot would otherwise see its
    move silently do nothing.
    """
    return max(0, min(ABS_MAX, round(float(v) * ABS_MAX)))


class Eyes:
    """Client for the vision organ - the query half.

    Every method here answers a QUESTION and returns a number or a boolean.
    None of them return a picture, which is the point: an agent can follow a
    screen for a minute at zero image-token cost and pay for a look only when
    something has actually happened.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9101, timeout: float = 30.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def _get(self, path: str, **params) -> dict:
        from urllib.parse import urlencode
        url = f"{self.base}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            raise KvmError(f"vision organ: {e}") from None

    def status(self) -> dict:
        return self._get("/status")

    def changed(self, roi: str | None = None, min_px: int = 150) -> dict:
        """Has this region changed since the last call? Returns counts, not pixels."""
        return self._get("/changed", roi=roi, min=min_px)

    def wait(self, roi: str | None = None, timeout: float = 5.0, min_px: int = 150) -> dict:
        """Block until the region changes, or the timeout expires.

        `min_px` is in FULL-RESOLUTION pixels, because that is the only unit a
        caller can reason about. Reference points at 1080p: a text caret is
        about 36 px, a mouse cursor about 380. A threshold near 100 will
        false-fail on a cursor that genuinely moved.
        """
        return self._get("/wait", roi=roi, timeout=timeout, min=min_px)

    def roi(self, roi: str) -> bytes:
        """A crop, as JPEG. Cheap to look at: a 400x200 region is ~120 image
        tokens against ~2700 for a 1080p screen."""
        from urllib.parse import urlencode
        url = f"{self.base}/roi.jpg?{urlencode({'roi': roi})}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.URLError as e:
            raise KvmError(f"vision organ: {e}") from None
