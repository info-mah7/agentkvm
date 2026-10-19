"""Runnable checks. No hardware, no network: a fake daemon in-process."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentkvm.client import Kvm, KvmError, _frac
from agentkvm.keys import parse_chord, to_usage

SEEN = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer k":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"no"}'); return
        n = int(self.headers.get("Content-Length", 0))
        SEEN.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(b'{"ok":true}')


@pytest.fixture
def kvm():
    SEEN.clear()
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Kvm("127.0.0.1", "k", port=srv.server_port)
    srv.shutdown()


def test_move_sends_fractions_as_gadget_coordinates(kvm):
    kvm.move(0.5, 0.25)
    assert SEEN == [("/v1/mouse", {"buttons": 0, "x": 16384, "y": 8192})]


def test_out_of_range_is_clamped_not_dropped():
    # The firmware refuses out-of-range coordinates rather than clamping, so a
    # caller that computed 1.0001 from a screenshot would silently do nothing.
    assert _frac(1.5) == 32767
    assert _frac(-0.2) == 0


def test_click_releases_the_button(kvm):
    kvm.click(0.1, 0.1)
    buttons = [b["buttons"] for _, b in SEEN]
    assert buttons[-1] == 0, "a click that never releases leaves the button down"


def test_type_counts_only_what_it_could_send(kvm):
    # A layout cannot produce every character; the count must say so rather
    # than reporting the requested length.
    n = kvm.type("abé", cps=1000)
    assert n == 2
    assert len(SEEN) == 2


def test_type_uses_shift_for_capitals(kvm):
    kvm.type("A", cps=1000)
    _, body = SEEN[0]
    assert body["modifier"] == 0x02 and body["keys"] == [0x04]


def test_unknown_key_in_chord_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError):
        parse_chord("ctrl+nosuchkey")


def test_chords():
    assert parse_chord("ctrl+alt+delete") == (0x05, [0x4C])
    assert parse_chord("win") == (0x08, [])


def test_bad_key_is_reported(kvm):
    kvm.key = "wrong"
    with pytest.raises(KvmError):
        kvm.move(0.5, 0.5)


def test_layout_round_trip():
    assert to_usage("a") == (0, 0x04)
    assert to_usage("?") == (0x02, 0x38)
    assert to_usage("€") is None


def test_mcp_refuses_to_act_without_a_recent_look(monkeypatch):
    """The MCP interface must not drive a screen it has not seen.

    This is the property that distinguishes a closed-loop KVM from a blind HID
    injector, so it is worth a test rather than a paragraph.
    """
    import agentkvm.mcp as m

    calls = []

    class FakeTool:
        def __init__(self):
            self.fns = {}

        def tool(self):
            def deco(fn):
                self.fns[fn.__name__] = fn
                return fn
            return deco

        def run(self):
            calls.append("run")

    fake = FakeTool()
    monkeypatch.setattr(m, "load_entry", lambda h: ("host", {"key": "k"}))
    monkeypatch.setattr(m, "connect", lambda h: type(
        "K", (), {"screenshot": lambda s: b"\xff\xd8shot",
                  "type": lambda s, t: len(t),
                  "click": lambda s, *a, **kw: None,
                  "release": lambda s: None})())

    import sys
    import types
    class FakeImage:
        def __init__(self, data=None, format=None):
            self.data, self.format = data, format

    mod = types.ModuleType("mcp.server.mcpserver")
    mod.MCPServer = lambda name: fake
    mod.Image = FakeImage
    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.mcpserver"] = mod

    m.serve(None)

    with pytest.raises(ValueError, match="look at the screen"):
        fake.fns["type_text"]("rm -rf /")

    # release_all is the recovery verb and must work blind
    assert fake.fns["release_all"]() == "released"

    fake.fns["look"]()
    assert "sent" in fake.fns["type_text"]("ok")


# --- the eyes: questions, not pictures ---

class FakeOrgan(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.path, None))
        if self.path.startswith("/roi.jpg"):
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.end_headers(); self.wfile.write(b"\xff\xd8crop"); return
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(b'{"changed": true, "px": 900}')


@pytest.fixture
def eyes():
    from agentkvm.client import Eyes
    SEEN.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeOrgan)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Eyes("127.0.0.1", port=srv.server_port)
    srv.shutdown()


def test_wait_passes_the_threshold_in_full_resolution_pixels(eyes):
    eyes.wait("10,20,300,200", timeout=2.0, min_px=90)
    path = SEEN[0][0]
    assert "roi=10%2C20%2C300%2C200" in path
    assert "min=90" in path and "timeout=2.0" in path


def test_wait_without_a_roi_omits_it_rather_than_sending_none(eyes):
    # "roi=None" would be parsed as a region and match nothing.
    eyes.wait(None)
    assert "roi=" not in SEEN[0][0]


def test_roi_returns_bytes_not_json(eyes):
    assert eyes.roi("0,0,10,10").startswith(b"\xff\xd8")


def test_organ_is_importable_only_with_the_extra():
    # The client must stay dependency-free; the organ is allowed to need Pillow,
    # but importing the package must not drag it in.
    import importlib
    import agentkvm
    assert "agentkvm.vision" not in str(importlib.import_module("agentkvm").__dict__)


def test_organ_query_path_decodes_and_compares():
    """The cheap-question path end to end: draft-decode a crop, count changed px."""
    Image = pytest.importorskip("PIL.Image", reason="needs the [vision] extra")
    import io

    from agentkvm.vision.organ import changed_px, crop_gray

    def jpg(box=None):
        im = Image.new("RGB", (320, 240), (0, 0, 0))
        if box:
            im.paste((255, 255, 255), box)
        b = io.BytesIO()
        im.save(b, "JPEG")
        return b.getvalue()

    roi = (0, 0, 160, 240)
    base = crop_gray(jpg(), roi)
    assert changed_px(base, crop_gray(jpg(), roi), 20) == 0, "a still screen must read as unchanged"
    assert changed_px(base, crop_gray(jpg((10, 10, 120, 200)), roi), 20) > 100


def test_look_returns_an_image_not_base64_text(monkeypatch):
    """A screenshot handed back as a base64 STRING bills as ~56,000 text tokens
    instead of ~2,700 image tokens - twenty times the cost, for bytes no model
    can read as a picture. This is the single most expensive mistake this
    interface can make, so it is pinned."""
    import sys
    import types

    import agentkvm.mcp as m

    class FakeTool:
        def __init__(self):
            self.fns = {}

        def tool(self):
            def deco(fn):
                self.fns[fn.__name__] = fn
                return fn
            return deco

        def run(self):
            pass

    class FakeImage:
        def __init__(self, data=None, format=None):
            self.data, self.format = data, format

    fake = FakeTool()
    mod = types.ModuleType("mcp.server.mcpserver")
    mod.MCPServer = lambda name: fake
    mod.Image = FakeImage
    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.mcpserver"] = mod

    monkeypatch.setattr(m, "load_entry", lambda h: ("host", {"key": "k"}))
    monkeypatch.setattr(m, "connect", lambda h: type(
        "K", (), {"screenshot": lambda s: b"\xff\xd8jpegbytes"})())
    m.serve(None)

    out = fake.fns["look"]()
    assert isinstance(out, FakeImage), f"look() must return an Image, got {type(out).__name__}"
    assert out.data == b"\xff\xd8jpegbytes" and out.format == "jpeg"


# --- espkvm wire codecs: exact bytes, no device needed ---

def test_espkvm_abs_mouse_is_little_endian_and_clamped():
    from agentkvm.espkvm import enc_mouse_abs
    assert enc_mouse_abs(1, 0x1234, 99999) == bytes([0x01, 0x01, 0x34, 0x12, 0xff, 0x7f, 0, 0])


def test_espkvm_keyboard_report_pads_to_six_keys():
    from agentkvm.espkvm import enc_keyboard
    assert enc_keyboard(0x02, [0x04]) == bytes([0x03, 0x02, 0x04, 0, 0, 0, 0, 0])
    # More than six is a caller error, but truncating quietly beats raising in
    # the middle of a keystroke: the report simply cannot carry them.
    assert len(enc_keyboard(0, list(range(10)))) == 8


def test_espkvm_video_header_round_trip():
    import struct

    from agentkvm.espkvm import parse_video_header
    hdr = struct.pack("<BBHHIH", 0x4B, 1, 1920, 1080, 42, 999)
    assert parse_video_header(hdr + b"payload") == (1, 1920, 1080, 42, 999)


def test_espkvm_rejects_a_frame_that_is_not_one():
    import pytest as _pytest

    from agentkvm.espkvm import parse_video_header
    with _pytest.raises(ValueError):
        parse_video_header(b"\x00" * 12)
