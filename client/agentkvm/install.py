"""Put agentkvmd on a board.

Runs from the operator's machine and pushes, rather than telling the board to
fetch and execute something from the internet as root. The board is a device
that types on your computer; it should not also be one that runs whatever a
URL served it today.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess

CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "agentkvm"

# What each board looks like from the inside. Detection is by which gadget
# nodes exist, because that is the thing we actually depend on - a board that
# reports the right name but has no absolute pointer is useless to us, and a
# rebadged one that has it works fine.
PROFILES = {
    "nanokvm": {"arch": "riscv64", "pointer": "/dev/hidg2", "dir": "/root/agentkvm"},
}


class InstallError(RuntimeError):
    pass


def ssh(target: str, cmd: str, timeout: int = 30) -> str:
    out = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"ConnectTimeout=10", target, cmd],
        capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise InstallError(f"ssh {target}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout


def resolve_host(target: str) -> str:
    """The address the daemon's HTTP API is reached at, given the ssh target.

    `install` talks to the board over ssh, so people naturally pass an alias
    from ~/.ssh/config. The API client does not go through ssh, and an alias
    is not a hostname: `ssh -G` prints what ssh would actually connect to.
    Found on the first run against hardware - the daemon came up fine and
    `doctor` then tried to resolve the alias.
    """
    bare = target.split("@")[-1]
    try:
        out = subprocess.run(["ssh", "-G", target], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return bare
    for line in out.splitlines():
        if line.startswith("hostname "):
            return line.split(None, 1)[1].strip() or bare
    return bare


def scp(local: str, target: str, remote: str, timeout: int = 300) -> None:
    out = subprocess.run(["scp", "-O", "-o", "StrictHostKeyChecking=accept-new", local,
                          f"{target}:{remote}"], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise InstallError(f"scp {local}: {out.stderr.strip()}")


def detect(target: str) -> str:
    """Which board is this? By gadget nodes, not by branding."""
    nodes = ssh(target, "ls /dev/hidg* 2>/dev/null || true").split()
    if not nodes:
        raise InstallError(
            "no /dev/hidg* on this board: its USB gadget is not configured, so there is "
            "nothing to type through. Check that HID is enabled in the board's own settings.")
    if "/dev/hidg2" in nodes:
        return "nanokvm"
    raise InstallError(
        f"found {' '.join(nodes)} but no absolute-pointer node this tool recognises. "
        "Guessing the report layout would move the cursor somewhere plausible rather "
        "than failing, so this stops here instead.")


def install(target: str, binary_dir: pathlib.Path, board: str | None = None) -> dict:
    """Push the daemon and its init script, provision a key, and prove it works."""
    board = board or detect(target)
    prof = PROFILES[board]
    binary = binary_dir / f"agentkvmd.{prof['arch']}"
    if not binary.exists():
        raise InstallError(f"no daemon built for {prof['arch']} at {binary}; "
                           "run `make` in the repo, or download agentkvmd.<arch> from the GitHub "
                           "release and pass --binaries <dir>")

    ssh(target, f"mkdir -p {prof['dir']}")
    # A running daemon holds its binary open, and Linux refuses to overwrite a
    # running executable ("Text file busy"). Stop it first; a fresh board has
    # no init script yet and the `|| true` covers that.
    ssh(target, "/etc/init.d/S99agentkvmd stop 2>/dev/null || true")
    scp(str(binary), target, f"{prof['dir']}/agentkvmd")
    scp(str(binary_dir / "S99agentkvmd"), target, "/etc/init.d/S99agentkvmd")
    ssh(target, f"chmod +x {prof['dir']}/agentkvmd /etc/init.d/S99agentkvmd")
    ssh(target, "/etc/init.d/S99agentkvmd restart")

    # The key is generated ON the board by the init script and read back once.
    # It never crosses this machine's shell history or its process list.
    key = ssh(target, f"cat {prof['dir']}/key").strip()
    if not key:
        raise InstallError("the daemon did not provision a key; it will not serve without one")

    host = resolve_host(target)
    CONFIG.mkdir(parents=True, exist_ok=True)
    conf = CONFIG / "config.json"
    saved = json.loads(conf.read_text()) if conf.exists() else {}
    saved[host] = {"key": key, "board": board, "port": 9200}
    conf.write_text(json.dumps(saved, indent=2))
    conf.chmod(0o600)
    return {"host": host, "board": board, "key": key, "config": str(conf)}


def save(host: str, **fields) -> str:
    """Record a board in the config. Returns the path written."""
    CONFIG.mkdir(parents=True, exist_ok=True)
    conf = CONFIG / "config.json"
    saved = json.loads(conf.read_text()) if conf.exists() else {}
    saved[host] = {**saved.get(host, {}), **fields}
    conf.write_text(json.dumps(saved, indent=2))
    conf.chmod(0o600)
    return str(conf)


def load_entry(host: str | None = None) -> tuple[str, dict]:
    """(host, whole entry) for a saved board, whatever its transport."""
    conf = CONFIG / "config.json"
    if not conf.exists():
        raise InstallError("no board configured yet: run `agentkvm install root@<board>` "
                           "or `agentkvm add-espkvm <host>`")
    saved = json.loads(conf.read_text())
    if host:
        if host not in saved:
            raise InstallError(f"{host} is not configured; known: {', '.join(saved) or 'none'}")
        return host, saved[host]
    if len(saved) != 1:
        raise InstallError(f"several boards configured, name one: {', '.join(saved)}")
    only = next(iter(saved))
    return only, saved[only]


def connect(host: str | None = None):
    """Open the configured board, whichever transport it speaks.

    THE selection point. Everything above it - CLI verbs, MCP tools, the skill -
    is written against one verb surface, so adding a board type means touching
    this function and nothing else.
    """
    host, entry = load_entry(host)
    if entry.get("transport") == "espkvm":
        from .espkvm import EspKvm            # imported late: it needs an extra
        return EspKvm(host, password=entry.get("password"),
                      user=entry.get("user", "admin"), tls=entry.get("tls", False))
    from .client import Kvm
    return Kvm(host, entry["key"])


def load(host: str | None = None) -> tuple[str, str]:
    """(host, key) for a saved board. With one board configured, no argument needed."""
    conf = CONFIG / "config.json"
    if not conf.exists():
        raise InstallError("no board configured yet: run `agentkvm install root@<board>`")
    saved = json.loads(conf.read_text())
    if host:
        if host not in saved:
            raise InstallError(f"{host} is not configured; known: {', '.join(saved) or 'none'}")
        return host, saved[host]["key"]
    if len(saved) != 1:
        raise InstallError(f"several boards configured, name one: {', '.join(saved)}")
    only = next(iter(saved))
    return only, saved[only]["key"]
