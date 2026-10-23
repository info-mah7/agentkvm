"""agentkvm - install a KVM daemon, check it, and drive a machine through it."""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import doctor as _doctor
from .client import KvmError
from .install import resolve_host, InstallError, connect, install, load_entry, save
from .keys import parse_chord


def _kvm(args):
    return connect(args.host)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentkvm", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("install", help="push the daemon to a board over ssh")
    q.add_argument("target", help="[user@]host of the board, e.g. root@nanokvm.local")
    q.add_argument("--binaries", default=None, help="directory holding agentkvmd.<arch>")
    q.add_argument("--board", default=None, choices=["nanokvm"],
                   help="skip detection and force a gadget profile")

    q = sub.add_parser("doctor", help="walk the chain and name the broken link")
    q.add_argument("target", help="[user@]host of the board")  # works for either transport

    q = sub.add_parser("add-espkvm", help="register a board running espkvm firmware")
    q.add_argument("host", help="host or host:port of the board")
    q.add_argument("--password", default=None, help="device login password, if it requires one")
    q.add_argument("--user", default="admin")
    q.add_argument("--tls", action="store_true")

    q = sub.add_parser("mcp", help="serve the KVM to an agent over MCP (stdio)")
    q.add_argument("--host", default=None)
    q.add_argument("--eyes", default=None, metavar="HOST",
                   help="host running `agentkvm vision`; adds the cheap "
                        "wait_for_change and look_at tools")

    # Deliberately takes raw argv: the organ has its own flags, and duplicating
    # them here would be two lists to keep in step.
    sub.add_parser("vision", help="run the capture loop and query service (needs [vision])",
                   add_help=False)

    for name, help_ in [("type", "type text"), ("key", "press a chord, e.g. ctrl+alt+del"),
                        ("click", "click at a fraction of the screen: 0.5 0.5"),
                        ("move", "move to a fraction of the screen"),
                        ("shot", "save a screenshot")]:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--host", default=None)
        if name in ("type", "key"):
            q.add_argument("arg")
        elif name in ("click", "move"):
            q.add_argument("x", type=float)
            q.add_argument("y", type=float)
        else:
            q.add_argument("path", nargs="?", default="screen.jpg")

    argv = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    if argv and argv[0] == "vision":
        argv, rest = argv[:1], argv[1:]
    args = p.parse_args(argv)
    try:
        if args.cmd == "install":
            here = pathlib.Path(args.binaries) if args.binaries else pathlib.Path(__file__).parent / "bin"
            out = install(args.target, here, args.board)
            print(f"installed on {out['host']} ({out['board']}); key saved to {out['config']}")
            print("next: agentkvm doctor " + args.target)
            return 0
        if args.cmd == "add-espkvm":
            from .espkvm import EspKvm
            kvm = EspKvm(args.host, password=args.password, user=args.user, tls=args.tls)
            kvm.connect()                       # prove it works before writing it down
            path = save(args.host, transport="espkvm", user=args.user,
                        password=args.password, tls=args.tls)
            print(f"espkvm at {args.host} reachable; saved to {path}")
            return 0
        if args.cmd == "doctor":
            host = resolve_host(args.target)
            host, entry = load_entry(host)
            if entry.get("transport") == "espkvm":
                print(_doctor.report(_doctor.run_espkvm(host, entry)))
            else:
                print(_doctor.report(_doctor.run(args.target, entry["key"])))
            return 0
        if args.cmd == "mcp":
            from .mcp import serve
            serve(args.host, args.eyes)
            return 0
        if args.cmd == "vision":
            try:
                from .vision.organ import main as serve_vision
            except ImportError as e:
                raise InstallError(f"the vision extra is not installed: {e} "
                                   "(pipx install './client[vision]')")
            serve_vision(rest)
            return 0

        kvm = _kvm(args)
        if args.cmd == "type":
            print(f"sent {kvm.type(args.arg)} chars")
        elif args.cmd == "key":
            mods, keys = parse_chord(args.arg)
            kvm.tap(mods, keys)
        elif args.cmd == "click":
            kvm.click(args.x, args.y)
        elif args.cmd == "move":
            kvm.move(args.x, args.y)
        elif args.cmd == "shot":
            pathlib.Path(args.path).write_bytes(kvm.screenshot())
            print(args.path)
        return 0
    except (KvmError, InstallError, ValueError) as e:
        print(f"agentkvm: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
