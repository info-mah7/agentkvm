"""The vision half: one capture loop, several consumers.

One capture loop, several consumers. It exists because more than one agent needs
to see the same screen, and because the high-rate work belongs on this side of
any tool-call boundary: a 60 fps sensor behind a JSON-RPC request/response is a
firehose through a keyhole.

Two kinds of tap, deliberately:

  * the STREAM  - /stream (multipart MJPEG) and /frame.jpg. Universal on purpose:
                  ``cv2.VideoCapture("http://host:9101/stream")`` just works, as
                  do ffmpeg and any browser, so an existing consumer attaches by
                  changing a URL.
  * the QUERIES - /changed, /wait, /roi.jpg. These answer *questions* and return
                  bytes-not-megabytes, so a deliberating agent can ask "did
                  anything happen" without pulling a frame through its context.

Capture is MJPEG passthrough: the card already emits JPEG, so the stream tap
never decodes. Only the query endpoints decode, and only the region asked for.

This runs on the machine driving, NOT on the KVM board. The board is a
single-core SoC that is already encode-bound and is the majority of round-trip
latency; adding a decode-and-diff there would slow the leg that is already
slowest. And where the comparison happens has no effect on what an agent pays
in image tokens - that saving comes from not sending a picture to a model,
which is decided here either way.

Dependencies: python3 + ffmpeg. Pillow is optional and only enables the query
endpoints - the stream works without it, which keeps this deployable anywhere.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:                                    # optional: only the query tap needs it
    from PIL import Image
except Exception:                       # noqa: BLE001
    Image = None
try:
    import numpy as np                  # optional: makes the compare O(n) in C
except Exception:                       # noqa: BLE001
    np = None

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def resolve_device(pattern: str, wait: float = 0.0) -> str | None:
    """Resolve a stable /dev/v4l/by-id path, optionally waiting for it.

    Never address /dev/videoN directly: the numbers move. This card went from
    video6 to a fresh pair within a minute of being plugged in, and its USB
    device number climbed 025 -> 095 while I was working - it re-enumerates
    constantly on some hosts - a property of the machine and its hub, not a
    one-off.

    Absence is therefore normal and temporary, not fatal: return None and let
    the caller retry rather than exiting and needing a supervisor to restart us.
    """
    if pattern.startswith(("/dev/video", "http://", "https://")):
        return pattern      # explicit node or an HTTP MJPEG source: use as-is
    deadline = time.monotonic() + wait
    while True:
        hits = sorted(glob.glob(f"/dev/v4l/by-id/*{pattern}*index0"))
        if hits:
            return os.path.realpath(hits[0])
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _espkvm_headers(url: str) -> "str | None":
    """For an espkvm ``/stream`` URL, log in and return an ffmpeg ``-headers``
    cookie string. The device's own certificate is self-signed, so TLS
    verification is off here: the alternative is asking every user to install a
    CA before they can see a picture. Creds via AMI_ESPKVM_USER/PASS."""
    import re
    import ssl
    import urllib.request
    m = re.match(r"(https?://[^/]+)", url)
    if not m:
        return None
    base = m.group(1)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    user = os.environ.get("AMI_ESPKVM_USER", "admin")
    pw = os.environ.get("AMI_ESPKVM_PASS", "")
    try:
        with urllib.request.urlopen(base + "/api/v1/auth/session", timeout=8, context=ctx) as r:
            if not json.loads(r.read()).get("required", True):
                return ""            # login disabled: no cookie needed, and none is an error
        body = json.dumps({"user": user, "password": pw}).encode()
        req = urllib.request.Request(base + "/api/v1/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            cookies = [c.split(";", 1)[0] for c in r.headers.get_all("Set-Cookie") or []]
        return f"Cookie: {'; '.join(cookies)}\r\n" if cookies else None
    except Exception:                # noqa: BLE001 - stream just fails, watchdog retries
        return None


def _nanokvm_headers(url: str) -> "str | None":
    """For a NanoKVM /api/stream URL, log in and return an ffmpeg ``-headers``
    cookie string. The stream is auth-gated (token lasts ~31 days); we mint a
    fresh one on every (re)spawn so a long-running organ never carries a stale
    one. Creds via env AMI_NANOKVM_USER/PASS (default admin/password)."""
    import re
    import urllib.request
    m = re.match(r"(https?://[^/]+)", url)
    if not m:
        return None
    base = m.group(1)
    user = os.environ.get("AMI_NANOKVM_USER", "admin")
    pw = os.environ.get("AMI_NANOKVM_PASS", "password")
    key = "nanokvm-sipeed-2024"          # the fixed passphrase NanoKVM's web login uses (see NOTICE)
    try:
        enc = subprocess.check_output(
            ["openssl", "enc", "-aes-256-cbc", "-md", "md5", "-pass", f"pass:{key}",
             "-a", "-A"], input=pw.encode(), stderr=subprocess.DEVNULL).decode().strip()
        enc = enc.replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")
        body = json.dumps({"username": user, "password": enc}).encode()
        req = urllib.request.Request(base + "/api/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            tok = (json.loads(r.read()).get("data") or {}).get("token")
        return f"Cookie: nano-kvm-token={tok}\r\n" if tok else None
    except Exception:                    # noqa: BLE001 - stream just fails, watchdog retries
        return None


class Organ:
    """The capture loop. Holds exactly one frame: the newest."""

    def __init__(self, pattern: str, width: int, height: int, fps: int):
        self.pattern = pattern
        self.device = resolve_device(pattern) or "(waiting)"
        self.width, self.height, self.fps = width, height, fps
        # The MiraBox ignores the ffmpeg input -framerate and always delivers
        # 60 fps MJPEG, so throttle on the publish side instead: drop frames
        # that arrive sooner than 1/fps after the last published one. -c:v copy
        # stays, so per-frame JPEG quality is untouched - this only thins the
        # rate that reaches clients. fps<=0 disables the gate.
        self._min_gap = (1.0 / fps) if fps and fps > 0 else 0.0
        self._last_pub = 0.0
        self.lock = threading.Condition()
        self.jpeg: bytes | None = None
        self.seq = 0
        self.last_gap = 0.0
        self.t_frame = 0.0
        self.started = time.monotonic()
        self.frames = 0
        self._recent: list[float] = []
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._proc_started = 0.0
        self._empty_starts = 0
        threading.Thread(target=self._loop, name="capture", daemon=True).start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()

    def _spawn(self):
        if str(self.device).startswith("http"):
            # HTTP MJPEG source. Passthrough copy - the source already emits
            # JPEG, so the stream tap never decodes. A fresh auth cookie is
            # minted per spawn rather than held, so a long-running organ never
            # carries a stale one.
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            # Which login to attempt is decided by the URL shape, because that
            # is the only thing we know about a device we have not talked to.
            hdr = (_espkvm_headers(self.device) if self.device.rstrip("/").endswith("/stream")
                   else _nanokvm_headers(self.device))
            if hdr:
                cmd += ["-headers", hdr]
            # The device signs its own certificate, so verification would fail
            # against exactly the device the user asked for by address.
            if self.device.startswith("https://"):
                cmd += ["-tls_verify", "0"]
            cmd += ["-i", self.device, "-c:v", "copy", "-f", "mpjpeg", "-"]
            return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        ifmt = os.environ.get("AMI_INPUT_FORMAT", "yuyv422")
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "v4l2", "-input_format", ifmt,
             "-video_size", f"{self.width}x{self.height}", "-framerate", str(self.fps),
             "-i", self.device]
            # -c:v copy when the sensor already produces JPEG. The 2026-08-11
            # replacement MiraBox advertises MJPEG but delivers 0 frames on it
            # (ffmpeg spins at 100% CPU); its YUYV path works, so that variant
            # re-encodes to JPEG here. AMI_INPUT_FORMAT=mjpeg restores copy.
            + (["-c:v", "copy"] if ifmt == "mjpeg" else
               ["-c:v", "mjpeg", "-q:v", "4"])
            + ["-f", "mpjpeg", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _loop(self):
        while not self._stop.is_set():
            # Re-resolve every time: a re-enumeration moves the node, so the
            # path that worked a minute ago may now point at nothing.
            dev = resolve_device(self.pattern, wait=10.0)
            if dev is None:
                continue
            self.device = dev
            proc = self._spawn()
            self._proc, self._proc_started = proc, time.monotonic()
            frames_at_spawn = self.frames
            buf = b""
            try:
                while not self._stop.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        i = buf.find(SOI)
                        j = buf.find(EOI, i + 2) if i >= 0 else -1
                        if i < 0 or j < 0:
                            break
                        frame = buf[i:j + 2]
                        buf = buf[j + 2:]
                        # Rate gate: keep the frame only if enough time has
                        # passed since the last published one. The 2 ms slack
                        # stops 60->30 from slipping to 20 on capture jitter.
                        now = time.monotonic()
                        if now - self._last_pub >= self._min_gap - 0.002:
                            self._last_pub = now
                            self._publish(frame)
                    if len(buf) > 16_000_000:       # desync guard
                        buf = b""
            finally:
                proc.kill()
                self._proc = None
            if not self._stop.is_set():
                # Back off only when a start produced NOTHING. A card that was
                # working and dropped out deserves an immediate retry; one that
                # cannot hand over a single frame deserves a widening pause,
                # otherwise the respawn loop becomes the load that keeps it down.
                if self.frames > frames_at_spawn:
                    self._empty_starts = 0
                else:
                    self._empty_starts += 1
                time.sleep(min(30.0, 2.0 ** min(self._empty_starts, 5)))

    #: A capture child can sit at 100% CPU forever without ever emitting a
    #: frame - the MiraBox did exactly that for two hours on 2026-08-11 (ffmpeg
    #: `wchar: 0`, /status honestly reporting `"stale": true`, systemd still
    #: `active`). Nothing above recovers it: a child that never exits never
    #: closes the pipe, so the read() in _loop blocks forever and the respawn
    #: underneath it is unreachable. Killing the child is what makes that
    #: existing recovery path run, so the watchdog only has to do that much.
    #:
    #: Generous on purpose: this must never fire on a slow start or a source
    #: that briefly drops (an HDMI mode change blanks the signal for seconds).
    #: It is a deadlock breaker, not a rate gate - STALE_AFTER_S already tells
    #: clients about the short gaps.
    #:
    #: Two thresholds, because the two cases need opposite temperaments. Once
    #: frames have been flowing, silence is a genuine stall and 10 s is already
    #: ~180 missed frames. But a child that has not produced its FIRST frame yet
    #: may simply be slow to open the card, and killing it at 10 s guarantees
    #: the next one is killed at 10 s too: a livelock that looks exactly like
    #: recovery in the journal. Observed 2026-08-11 - after SIGKILLing the child
    #: mid-stream, the card needed longer than 10 s to hand over a first frame
    #: and the watchdog kill-looped every ~12 s for five and a half minutes,
    #: PREVENTING the recovery it exists to cause. Hence the wider start grace,
    #: plus the backoff in _loop so repeated empty starts stop hammering.
    DEAD_AFTER_S = 10.0
    START_GRACE_S = 30.0

    def _watchdog(self):
        while not self._stop.wait(1.0):
            proc, since = self._proc, self._proc_started
            if proc is None or proc.poll() is not None:
                continue                            # no child, or already dead
            # Measure from the spawn, not just the last frame: a child that
            # never produced a first frame has t_frame from a previous life.
            produced = self.t_frame > since
            quiet = time.monotonic() - max(since, self.t_frame)
            if quiet >= (self.DEAD_AFTER_S if produced else self.START_GRACE_S):
                what = "no frame" if produced else "no first frame"
                print(f"agentkvm vision: {what} for {quiet:.1f}s on {self.device}"
                      f" - killing capture to force a respawn", flush=True)
                proc.kill()

    def _publish(self, jpeg: bytes):
        now = time.monotonic()
        with self.lock:
            # The frame arriving after a blackout carries its full width. This
            # is the only moment that number exists; sampling age later sees a
            # frame that has just arrived and reads ~0 by construction.
            self.last_gap = (now - self.t_frame) if self.t_frame else 0.0
            self.jpeg, self.seq, self.t_frame = jpeg, self.seq + 1, now
            self.frames += 1
            self._recent.append(now)
            if len(self._recent) > 60:
                del self._recent[:30]
            self.lock.notify_all()

    def latest(self, after: int = -1, timeout: float = 5.0):
        """Newest frame, optionally blocking until one newer than ``after``."""
        deadline = time.monotonic() + timeout
        with self.lock:
            while self.jpeg is None or self.seq <= after:
                if not self.lock.wait(max(0.0, deadline - time.monotonic())):
                    return None, self.seq
            return self.jpeg, self.seq

    #: No frame for this long means the pipe is dead rather than the screen
    #: being still. At 60 fps a frame arrives every ~17 ms, so two seconds is
    #: ~120 missed frames - far outside jitter, well inside a human's patience.
    STALE_AFTER_S = 1.0

    def frame_age(self) -> float:
        """Seconds since a frame last ARRIVED - not since one last changed."""
        with self.lock:
            if self.t_frame is None:
                return float("inf")
            return time.monotonic() - self.t_frame

    def fps_now(self) -> float:
        with self.lock:
            r = list(self._recent)
        return 0.0 if len(r) < 2 else (len(r) - 1) / (r[-1] - r[0])


DECODE_SCALE = 4        # draft-mode divisor used by the query endpoints


def crop_gray(jpeg: bytes, roi, scale: int = DECODE_SCALE):
    """Decode only what a question needs.

    JPEG draft mode decodes at 1/scale straight out of the DCT, which is several
    times cheaper than a full decode and plenty for "did this region change".
    """
    im = Image.open(io.BytesIO(jpeg))
    ow, oh = im.size
    im.draft("L", (ow // scale, oh // scale))
    im = im.convert("L")
    # draft() only honours 1/1, 1/2, 1/4, 1/8, so the achieved scale is not
    # necessarily the one asked for. Derive it from the result rather than
    # assuming it, or every ROI lands in the wrong place.
    fx, fy = im.width / ow, im.height / oh
    if roi:
        x, y, w, h = roi
        left, top = int(x * fx), int(y * fy)
        im = im.crop((left, top,
                      max(left + 1, int((x + w) * fx)),
                      max(top + 1, int((y + h) * fy))))
    return im


def changed_px(a, b, thr: int) -> int:
    """Count pixels differing by more than ``thr``."""
    if a.size != b.size:
        return 1 << 30
    if np is not None:
        return int((np.abs(np.asarray(a, np.int16) - np.asarray(b, np.int16)) > thr).sum())
    # Fallback keeps the service usable without numpy, but a pure-Python compare
    # over a 1080p frame is ~100x slower - fine for small ROIs only.
    return sum(1 for x, y in zip(a.tobytes(), b.tobytes()) if abs(x - y) > thr)


INDEX = """<!doctype html>
<meta charset="utf-8"><title>agentkvm vision</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root { color-scheme: dark }
 body { margin:0; background:#111; color:#ddd;
        font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }
 header { padding:.6rem .9rem; background:#191919; border-bottom:1px solid #2a2a2a;
          display:flex; gap:1.2rem; align-items:baseline; flex-wrap:wrap }
 h1 { font-size:14px; margin:0; font-weight:600; letter-spacing:.04em }
 #stat { color:#8a8a8a }
 a { color:#6aa9ff; text-decoration:none } a:hover { text-decoration:underline }
 .wrap { padding:.9rem }
 img { display:block; width:100%; height:auto; max-width:1920px;
       background:#000; border:1px solid #2a2a2a }
 nav { margin-top:.7rem; color:#6a6a6a } nav a { margin-right:1rem }
</style>
<header>
  <h1>ami-vision</h1>
  <span id="stat">connecting…</span>
</header>
<div class="wrap">
  <img src="/stream" alt="live capture">
  <nav>
    <a href="/frame.jpg">frame.jpg</a>
    <a href="/status">status</a>
    <a href="/changed?roi=0,0,400,200">changed?roi=…</a>
    <a href="/wait?roi=0,0,400,200&timeout=5">wait?roi=…</a>
    <a href="/roi.jpg?roi=0,0,400,200">roi.jpg?roi=…</a>
  </nav>
</div>
<script>
async function tick() {
  try {
    const s = await (await fetch('/status', {cache:'no-store'})).json();
    document.getElementById('stat').textContent =
      `${s.width}x${s.height}  ${s.fps.toFixed(1)} fps  seq ${s.seq}  ` +
      `${(s.bytes_last/1024).toFixed(0)} KB/frame  up ${Math.floor(s.uptime_s)}s  ` +
      `${s.device}`;
  } catch (e) { document.getElementById('stat').textContent = 'status unavailable'; }
}
tick(); setInterval(tick, 2000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    organ: Organ = None            # set on the server class
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):     # quiet; systemd captures what matters
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _roi(self, q):
        if "roi" not in q:
            return None
        roi = tuple(int(v) for v in q["roi"][0].split(","))
        if len(roi) != 4:
            raise ValueError("roi is x,y,w,h")
        return roi

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        o = self.organ
        try:
            if u.path in ("/", "/index.html"):
                # A human opening the root should see the picture, not a 404
                # listing paths. The stream is an <img> because
                # multipart/x-mixed-replace is what browsers render natively -
                # no JS, no player, no dependencies.
                body = INDEX.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)

            if u.path == "/status":
                jpeg, seq = o.jpeg, o.seq
                return self._json({
                    "device": o.device, "seq": seq,
                    "width": o.width, "height": o.height,
                    "fps": round(o.fps_now(), 2), "frames": o.frames,
                    "bytes_last": len(jpeg) if jpeg else 0,
                    "uptime_s": round(time.monotonic() - o.started, 1),
                    "frame_age_s": round(o.frame_age(), 3),
                    "stale": o.frame_age() > o.STALE_AFTER_S,
                    "queries": Image is not None,
                })

            if u.path == "/frame.jpg":
                after = int(q.get("after", [-1])[0])
                jpeg, seq = o.latest(after=after, timeout=float(q.get("timeout", [5])[0]))
                if jpeg is None:
                    return self._json({"error": "no frame"}, 503)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Frame-Seq", str(seq))
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                return self.wfile.write(jpeg)

            if u.path == "/stream":
                # A browser NAVIGATING here gets one frozen frame: Chromium
                # dropped top-level multipart/x-mixed-replace, and it only
                # animates inside an <img>. Send people to the page that
                # embeds it rather than letting them conclude the stream is
                # broken. Programmatic clients (cv2, ffmpeg, curl) do not ask
                # for text/html, so they are unaffected.
                if "text/html" in self.headers.get("Accept", ""):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                # No length and no chunked encoding: under HTTP/1.1 that framing
                # is ambiguous, so say outright that the body ends when the
                # connection does.
                self.send_header("Connection", "close")
                self.end_headers()
                last = -1
                while True:
                    jpeg, last = o.latest(after=last, timeout=10.0)
                    if jpeg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(jpeg)).encode()
                                     + b"\r\n\r\n" + jpeg + b"\r\n")

            if u.path in ("/changed", "/wait"):
                if Image is None:
                    return self._json({"error": "Pillow not installed"}, 501)
                roi = self._roi(q)
                thr = int(q.get("thr", [24])[0])
                # 'min' is given in FULL-RESOLUTION pixels, because that is the
                # only unit a caller can reason about - they know the screen, not
                # our decode scale. Convert to decoded pixels here: area shrinks
                # by scale**2, so a 4x draft makes 16 screen pixels into 1.
                # Getting this wrong is silent: the first run missed a cursor
                # because 40 was compared against a 24-pixel change.
                # Reference points at 1080p: text caret ~36, mouse cursor ~380.
                min_full = int(q.get("min", [150])[0])
                minpx = max(1, min_full // (DECODE_SCALE * DECODE_SCALE))
                timeout = float(q.get("timeout", [5])[0])
                base_jpeg, base_seq = o.latest(timeout=2.0)
                if base_jpeg is None:
                    return self._json({"error": "no frame"}, 503)
                base = crop_gray(base_jpeg, roi)
                deadline = time.monotonic() + timeout
                seq = base_seq
                worst_gap = 0.0
                #: Did we ever manage to compare a frame against the baseline?
                #: The deadline expiring after N good comparisons is an ANSWER
                #: ("did not change"); never getting a frame at all is not.
                #: Both arrive here as latest() -> None, so they must be told
                #: apart explicitly or every timeout reads as a broken capture.
                measured = 0
                last_n = None
                while True:
                    jpeg, seq = o.latest(after=seq, timeout=max(0.0, deadline - time.monotonic()))
                    worst_gap = max(worst_gap, o.last_gap, o.frame_age())
                    if jpeg is None:
                        # No NEW frame arrived before the deadline. That is a
                        # capture-side condition, not an answer about the
                        # screen -- reporting it as changed=false conflates
                        # "did not move" with "could not measure", and a caller
                        # cannot tell the difference. Measure the newest frame
                        # we do have against the baseline and say plainly that
                        # the wait was inconclusive.
                        cur, _cseq = o.latest(timeout=0.0)
                        px = last_n
                        if cur is not None:
                            try:
                                px = changed_px(base, crop_gray(cur, roi), thr)
                                measured += 1
                            except Exception:
                                pass
                        if measured:
                            # We compared real frames and none differed enough.
                            # That is a measurement, so report it as one.
                            return self._json({"changed": bool(px is not None and px >= minpx),
                                               "seq": seq, "px": px,
                                               "waited_s": round(timeout, 3),
                                               "frame_age_s": round(o.frame_age(), 3),
                                               "worst_gap_s": round(max(worst_gap, o.frame_age()), 3),
                                               "stale": max(worst_gap, o.frame_age()) > o.STALE_AFTER_S})
                        return self._json({"changed": False,
                                           "inconclusive": True,
                                           "reason": "no_frame_to_compare",
                                           "seq": seq,
                                           "px": px,
                                           "waited_s": round(timeout, 3),
                                           "frame_age_s": round(o.frame_age(), 3),
                                           "worst_gap_s": round(max(worst_gap, o.frame_age()), 3),
                                           "stale": max(worst_gap, o.frame_age()) > o.STALE_AFTER_S})
                    n = changed_px(base, crop_gray(jpeg, roi), thr)
                    measured += 1
                    last_n = n
                    if n >= minpx:
                        return self._json({"changed": True, "seq": seq, "px": n,
                                           "px_full_est": n * DECODE_SCALE * DECODE_SCALE,
                                           "frame_age_s": round(o.frame_age(), 3),
                                           "worst_gap_s": round(worst_gap, 3),
                                           "stale": worst_gap > o.STALE_AFTER_S,
                                           "waited_s": round(timeout - max(0.0, deadline - time.monotonic()), 3)})
                    if u.path == "/changed" or time.monotonic() >= deadline:
                        return self._json({"changed": False, "seq": seq, "px": n,
                                           "frame_age_s": round(o.frame_age(), 3),
                                           "worst_gap_s": round(max(worst_gap, o.frame_age()), 3),
                                           "stale": max(worst_gap, o.frame_age()) > o.STALE_AFTER_S})

            if u.path == "/roi.jpg":
                if Image is None:
                    return self._json({"error": "Pillow not installed"}, 501)
                roi = self._roi(q)
                jpeg, seq = o.latest(timeout=3.0)
                if jpeg is None:
                    return self._json({"error": "no frame"}, 503)   # as /frame.jpg does
                im = Image.open(io.BytesIO(jpeg))
                if roi:
                    x, y, w, h = roi
                    im = im.crop((x, y, x + w, y + h))
                out = io.BytesIO(); im.convert("RGB").save(out, "JPEG", quality=88)
                body = out.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Frame-Seq", str(seq))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)

            return self._json({"error": "not found",
                               "paths": ["/", "/status", "/frame.jpg", "/stream",
                                         "/changed", "/wait", "/roi.jpg"]}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return                                  # consumer went away mid-stream
        except ValueError as e:
            # A malformed parameter (roi=a,b, after=x, thr=...) is the caller's
            # mistake: say so, rather than dropping the connection on a traceback.
            return self._json({"error": f"bad query: {e}"}, 400)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentkvm vision")
    ap.add_argument("--device", required=True,
                    help="an MJPEG URL (https://espkvm.local/stream, "
                         "http://nanokvm/api/stream/mjpeg), a "
                         "/dev/v4l/by-id substring, or an explicit /dev/videoN")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args(argv)

    print(f"agentkvm vision: {a.device} {a.width}x{a.height}@{a.fps} -> http://{a.bind}:{a.port}",
          flush=True)
    Handler.organ = Organ(a.device, a.width, a.height, a.fps)
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
