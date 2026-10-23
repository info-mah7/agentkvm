"""Walk the chain and name the broken link.

board -> daemon -> gadget -> target -> capture. Every one of them fails as
"it doesn't type", and the cost of not distinguishing them is an afternoon.
Each check reports what it saw and, when it fails, what to do about it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import Kvm, KvmError
from .install import InstallError, load_entry, resolve_host, ssh


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        line = f"{mark} {self.name:<22} {self.detail}"
        return line if self.ok or not self.fix else f"{line}\n{'':<25}→ {self.fix}"


def run_espkvm(host: str, entry: dict) -> list[Check]:
    """The espkvm chain: reachable -> login -> control socket -> video.

    Shorter than the daemon's, because nothing was installed on the board and
    there is no ssh, no key file and no gadget to face the wrong way. What can
    break is the network, the password, someone else holding control, and
    whether an HDMI source is actually plugged in.
    """
    from .espkvm import EspKvm

    checks: list[Check] = []
    kvm = EspKvm(host, password=entry.get("password"), user=entry.get("user", "admin"),
                 tls=entry.get("tls", False))
    try:
        kvm._login()
        checks.append(Check("device reachable", True, f"{host}, login accepted"))
    except KvmError as e:
        checks.append(Check("device reachable", False, str(e),
                            "check the address, and the password the device was set up with"))
        return checks

    try:
        kvm.connect()
        checks.append(Check("control socket", True, "open, control claimed"))
    except KvmError as e:
        checks.append(Check("control socket", False, str(e),
                            "install the extra with `pipx install './client[espkvm]'` from a clone, "
                            "or close a browser session holding control of the device"))
        return checks

    # Video is the one that fails for a reason outside the software: no source.
    try:
        shot = kvm.screenshot(timeout=5.0)
        ok = shot.startswith(b"\xff\xd8")
        checks.append(Check("video", ok, f"{len(shot)} bytes" + ("" if ok else " (not a JPEG)")))
    except KvmError as e:
        checks.append(Check("video", False, str(e),
                            "is the target powered on and its HDMI output connected "
                            "to the board?"))
    return checks


def run(target: str, key: str) -> list[Check]:
    host = resolve_host(target)
    checks: list[Check] = []

    # 1. the board itself
    try:
        uname = ssh(target, "uname -r").strip()
        checks.append(Check("board reachable", True, f"{host}, kernel {uname}"))
    except InstallError as e:
        checks.append(Check("board reachable", False, str(e),
                            "check the address, and that SSH is enabled on the board"))
        return checks  # nothing below this can be checked

    # 2. the daemon
    kvm = Kvm(host, key)
    try:
        kvm.healthz()
        checks.append(Check("daemon", True, "responding on :9200"))
    except Exception as e:
        checks.append(Check("daemon", False, f"{e}",
                            "run `/etc/init.d/S99agentkvmd restart` on the board"))
        return checks

    # 3. the key
    try:
        kvm.release()
        checks.append(Check("key", True, "accepted"))
    except KvmError as e:
        checks.append(Check("key", False, str(e),
                            "re-run `agentkvm install` to read the board's key again"))
        return checks

    # 4. the gadget, and which way it faces. A board whose USB-C is in the
    #    control port has perfectly healthy HID nodes pointed at the wrong
    #    machine, which is the single most confusing way for this to fail.
    try:
        state = ssh(target, "cat /sys/class/udc/*/state 2>/dev/null || true").strip()
        if state == "configured":
            checks.append(Check("USB gadget", True, "configured: a host has enumerated it"))
        else:
            checks.append(Check("USB gadget", False, f"state {state or 'unknown'}",
                                "the USB-C cable is not in a powered host port on the TARGET, "
                                "or the target has not enumerated the device"))
    except InstallError as e:
        checks.append(Check("USB gadget", False, str(e)))

    # 5. capture
    try:
        img = kvm.screenshot()
        ok = img.startswith(b"\xff\xd8")
        # Stock NanoKVM firmware 2.x has no unauthenticated snapshot URL: the
        # daemon's default capture gets a 404 page, not a picture. Seen on the
        # first run against hardware (application 2.4.3). The eyes belong on the
        # host anyway; say so instead of sending someone to check a cable.
        hint = ("this firmware has no snapshot endpoint the daemon can read; run "
                "`agentkvm vision --device http://<board>/api/stream/mjpeg` on the host "
                "for eyes (it logs in), and pass --eyes to `agentkvm mcp`"
                if b"404" in img or b"unauthorized" in img
                else "check the board's own video settings and the HDMI input")
        checks.append(Check("capture", ok,
                            f"{len(img)} bytes" + ("" if ok else " (not a JPEG)"),
                            "" if ok else hint))
    except KvmError as e:
        checks.append(Check("capture", False, str(e),
                            "the board's own capture service is not answering on loopback"))

    return checks


def report(checks: list[Check]) -> str:
    body = "\n".join(str(c) for c in checks)
    if all(c.ok for c in checks):
        return body + "\n\nAll good. `agentkvm type 'hello'` should land on the target."
    first = next(c for c in checks if not c.ok)
    return body + f"\n\nFirst broken link: {first.name}."
