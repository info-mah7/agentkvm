"""Transport for espkvm firmware - HDMI in and USB HID out on one ESP32-P4.

The flagship path: one board you flash, nothing plugged into the driving
machine. Video arrives over a WebSocket as JPEG, control leaves over a second
WebSocket as fixed binary reports.

Deliberately does NOT decode video. The frames are already JPEG and every
consumer here wants bytes - a screenshot to hand a model, or a region to
compare. Decoding to a numpy array would drag in OpenCV to produce something
that gets re-encoded a moment later.

Wire protocol (little-endian), as the firmware defines it:

  WS /video   subscribe with a single 0x01 byte; each binary message is a
              12-byte header followed by the payload
                off 0  u8  magic 0x4b ('K')
                off 1  u8  type  1=JPEG 2=H.264
                off 2  u16 width      off 4  u16 height
                off 6  u32 sequence   off 10 u16 pts (device ms, wraps ~65 s)

  WS /ws      0x01 abs   buttons, x:u16 0..32767, y:u16, wheel:i8, pan:i8
              0x02 rel   buttons, dx:i16, dy:i16, wheel:i8, pan:i8
              0x03 kbd   modifier, six key usages
              0x04 cons  usage:u16
              0x05 release-all      0x06 ping      0x07 take-control
              device->client: 0x81 status(flags,leds) 0x82 pong 0x83 control

Both channels need a session cookie from POST /api/v1/auth/login. The device
releases everything held when the socket closes, so a dropped connection
cannot strand a key down on the target - the one safety property this
transport gets for free that the daemon has to implement itself.

Control is fire-and-forget with no acknowledgement, on purpose: an ack is a
round trip, and you do not read with your hands.
"""
from __future__ import annotations

import asyncio
import json
import struct
import threading
import urllib.error
import urllib.request

from .client import ABS_MAX, KvmError

VIDEO_MAGIC = 0x4B
VIDEO_HDR = 12
JPEG = 1

MOUSE_ABS, MOUSE_REL, KEYBOARD, CONSUMER, RELEASE_ALL, PING, TAKEOVER = range(1, 8)


def _clamp(v, lo, hi) -> int:
    return max(lo, min(hi, int(round(v))))


# --- pure wire codecs: no device needed to test these ----------------------
def enc_mouse_abs(buttons: int, x: int, y: int, wheel: int = 0, pan: int = 0) -> bytes:
    return struct.pack("<BBHHbb", MOUSE_ABS, buttons & 0xFF,
                       _clamp(x, 0, ABS_MAX), _clamp(y, 0, ABS_MAX),
                       _clamp(wheel, -127, 127), _clamp(pan, -127, 127))


def enc_mouse_rel(buttons: int, dx: int, dy: int, wheel: int = 0, pan: int = 0) -> bytes:
    return struct.pack("<BBhhbb", MOUSE_REL, buttons & 0xFF,
                       _clamp(dx, -32768, 32767), _clamp(dy, -32768, 32767),
                       _clamp(wheel, -127, 127), _clamp(pan, -127, 127))


def enc_keyboard(modifier: int, keys) -> bytes:
    k = list(keys)[:6]
    k += [0] * (6 - len(k))
    return struct.pack("<BB6B", KEYBOARD, modifier & 0xFF, *[x & 0xFF for x in k])


def enc_consumer(usage: int) -> bytes:
    return struct.pack("<BH", CONSUMER, usage & 0xFFFF)


def parse_video_header(msg: bytes) -> tuple[int, int, int, int, int]:
    """(type, width, height, seq, pts) from a /video frame. ValueError on bad magic."""
    if len(msg) < VIDEO_HDR or msg[0] != VIDEO_MAGIC:
        raise ValueError("not an espkvm video frame")
    _magic, ftype, w, h, seq, pts = struct.unpack_from("<BBHHIH", msg, 0)
    return ftype, w, h, seq, pts


class EspKvm:
    """Same verb surface as `Kvm`, so everything above this is unchanged.

    One background thread owns an asyncio loop and both sockets; the public
    methods are ordinary blocking calls, because the callers are a CLI and an
    MCP tool handler and neither wants to be async.
    """

    CPS = 11.0
    JITTER = 0.25

    def __init__(self, host: str, password: str | None = None, user: str = "admin",
                 port: int | None = None, tls: bool = False, timeout: float = 10.0):
        self.scheme = "https" if tls else "http"
        self.host = host if ":" in host or port is None else f"{host}:{port}"
        self.base = f"{self.scheme}://{self.host}"
        self.user, self.password, self.timeout = user, password, timeout
        self._cookie: str | None = None
        self._frame: bytes | None = None
        self._seq = 0
        self._lock = threading.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ctrl = None
        self._stop = threading.Event()
        self._err: str | None = None

    # --- session -----------------------------------------------------------
    def _login(self) -> None:
        try:
            with urllib.request.urlopen(f"{self.base}/api/v1/auth/session", timeout=self.timeout) as r:
                if not json.loads(r.read()).get("required", True):
                    return                                  # login disabled on the device
        except urllib.error.URLError as e:
            raise KvmError(f"espkvm at {self.host}: {e.reason}") from None
        if self.password is None:
            raise KvmError("device requires a login and no password was given")
        body = json.dumps({"user": self.user, "password": self.password}).encode()
        req = urllib.request.Request(f"{self.base}/api/v1/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                # The session cookie is HttpOnly, so it has to be read off the
                # header rather than from any cookie jar.
                cookies = [c.split(";", 1)[0] for c in r.headers.get_all("Set-Cookie") or []]
        except urllib.error.HTTPError as e:
            raise KvmError(f"espkvm login failed ({e.code})") from None
        if not cookies:
            raise KvmError("espkvm login returned no session cookie")
        self._cookie = "; ".join(cookies)

    def connect(self) -> None:
        if self._loop is not None:
            return
        self._err = None        # a failure from an earlier attempt is not this one's
        self._login()
        ready = threading.Event()
        threading.Thread(target=self._pump, args=(ready,), name="espkvm", daemon=True).start()
        if not ready.wait(self.timeout):
            raise KvmError(f"espkvm: sockets did not come up ({self._err or 'timeout'})")
        if self._err:
            raise KvmError(f"espkvm: {self._err}")

    def _pump(self, ready: threading.Event) -> None:
        try:
            import websockets
        except ImportError:
            self._err = "the espkvm extra is not installed (pipx install 'agentkvm[espkvm]')"
            ready.set()
            return

        async def run():
            ws_base = self.base.replace("http", "ws", 1)
            hdr = {"Cookie": self._cookie} if self._cookie else {}
            try:
                # websockets renamed this argument; accept either.
                try:
                    ctrl = await websockets.connect(f"{ws_base}/ws", additional_headers=hdr, max_size=None)
                    video = await websockets.connect(f"{ws_base}/video", additional_headers=hdr, max_size=None)
                except TypeError:
                    ctrl = await websockets.connect(f"{ws_base}/ws", extra_headers=hdr, max_size=None)
                    video = await websockets.connect(f"{ws_base}/video", extra_headers=hdr, max_size=None)
            except Exception as e:                              # noqa: BLE001
                self._err = str(e)
                self._loop = None       # not connected: the next connect() tries again
                ready.set()
                return
            self._ctrl = ctrl
            # Claim the single control session up front, or the device drops
            # our input while someone's browser holds it.
            await ctrl.send(bytes([TAKEOVER]))
            await video.send(bytes([JPEG]))                     # subscribe
            ready.set()
            try:
                async for msg in video:
                    if not isinstance(msg, (bytes, bytearray)) or len(msg) <= VIDEO_HDR:
                        continue
                    try:
                        ftype, _w, _h, seq, _pts = parse_video_header(msg)
                    except ValueError:
                        continue
                    if ftype != JPEG:
                        continue        # H.264 is for viewers; we want bytes to send on
                    with self._lock:
                        self._frame, self._seq = bytes(msg[VIDEO_HDR:]), seq
                        self._lock.notify_all()
            except Exception:                                   # noqa: BLE001
                pass
            # The stream ended (the device dropped us, or rebooted). Mark the
            # session gone so the next connect() builds a new one, rather than
            # leaving a dead control socket that every send fails on.
            self._ctrl = None
            self._loop = None

        loop = asyncio.new_event_loop()
        self._loop = loop       # published before run(): _send schedules onto it while it streams
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

    def _send(self, data: bytes) -> None:
        self.connect()
        if self._ctrl is None or self._loop is None:
            raise KvmError("espkvm: control socket is not open")
        asyncio.run_coroutine_threadsafe(self._ctrl.send(data), self._loop).result(self.timeout)

    # --- the verb surface, identical to Kvm --------------------------------
    def move(self, fx: float, fy: float, buttons: int = 0) -> None:
        self._send(enc_mouse_abs(buttons, fx * ABS_MAX, fy * ABS_MAX))

    def click(self, fx: float, fy: float, button: int = 1, double: bool = False) -> None:
        self.move(fx, fy)
        for _ in range(2 if double else 1):
            self.move(fx, fy, buttons=button)
            self.move(fx, fy, buttons=0)

    def scroll(self, ticks: int) -> None:
        self._send(enc_mouse_abs(0, 0, 0, wheel=_clamp(ticks, -127, 127)))

    def tap(self, modifier: int = 0, keys=None, hold_ms: int = 0) -> None:
        # No device-side tap primitive here, so press and release are two
        # messages on one already-open socket. That is microseconds apart, not
        # the hundreds of milliseconds a per-command HTTP round trip would cost
        # - which is what makes a target's auto-repeat fire.
        self._send(enc_keyboard(modifier, keys or []))
        self._send(enc_keyboard(0, []))

    def release(self) -> None:
        self._send(bytes([RELEASE_ALL]))

    def type(self, text: str, cps: float = 0.0, layout=None) -> int:
        import random
        import time

        from .keys import to_usage
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

    def screenshot(self, timeout: float = 5.0) -> bytes:
        """The newest JPEG off the video socket. No decode, no re-encode."""
        self.connect()
        with self._lock:
            if self._frame is None and not self._lock.wait_for(lambda: self._frame is not None, timeout):
                raise KvmError("espkvm: no video frame arrived - is an HDMI source connected?")
            return self._frame

    def healthz(self) -> dict:
        self.connect()
        with self._lock:
            return {"ok": self._frame is not None, "seq": self._seq}
