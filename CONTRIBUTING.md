# Contributing

Patches, board reports, and corrections are all welcome. A few things are worth
knowing before you spend time on them.

## The one rule that is not negotiable

**Do not copy code, key tables, or report definitions out of any board's
firmware.** The firmwares this talks to or learns from are GPL-3.0 (Sipeed
NanoKVM, PiKVM), GPL-2.0 (JetKVM) or Apache-2.0 (espkvm); this project is Apache-2.0
and stays that way by containing none of their code. Talking to a device over
loopback HTTP and writing bytes to a kernel character device is not a derivative
work. Pasting in their keycode map is.

If you need a HID detail, take it from the USB HID Usage Tables, or read it off
the hardware and write your own implementation. A report *layout* - "byte 0 is
the report ID, bytes 2-3 are X little-endian" - is a fact about a device and is
fine to record. Their source expressing it is not.

A pull request that adds firmware-derived code will be closed, however good it
is, because the alternative is relicensing the project.

## Development

No hardware is needed for anything below.

```
go test ./...                      # the daemon: report layouts, auth, tap bounds
cd client && python3 -m pytest -q  # the client: coordinates, typing, the look gate
make build                         # cross-compile the daemon
```

The Python client is deliberately **dependency-free** at runtime. The thing a
person installs to get hands on a machine should not pull in a video stack. If a
change needs a dependency, it probably belongs behind an extra, or somewhere
else entirely.

## Adding a board

This is the most useful contribution, and the easiest one to get subtly wrong.

A board is a `profile` in `main.go`: which gadget node the keyboard is on, which
node the absolute pointer is on, and how the pointer report is laid out. Boards
disagree about all three, and the failure mode when you guess is the bad kind - **an incorrect report layout usually moves the cursor somewhere plausible rather
than returning an error**, so it looks like it works until it does not.

So: do not infer a layout from a similar board. Establish it, and say in the pull
request how you established it:

1. `ls /dev/hidg*` on the board - which nodes exist.
2. For each, the report descriptor: `hexdump -C /sys/kernel/config/usb_gadget/*/functions/hid.*/report_desc`, or read it off the host the board is plugged into. The descriptor tells you the report ID, the axis ranges, and whether the pointer is absolute or relative.
3. Confirm which node is *absolute*. On at least one supported board `hidg1` is a **relative** mouse and the absolute pointer is a touchpad function on `hidg2`. Sending absolute reports to the relative node produces confident nonsense.
4. Add the profile, add a test asserting the exact bytes for a known coordinate, and add the board to the installer's detection in `client/agentkvm/install.py`.

Detection keys off which nodes exist, not off branding. A rebadged board that has
what we need should work; a board that reports a familiar name but lacks an
absolute pointer should fail loudly.

## What the code is trying to be

If a change conflicts with one of these, say so in the pull request and we can
talk about it - they are conclusions, not commandments, but each one came from
something going wrong.

- **Fail closed.** No key, no service. This types on real machines.
- **Report what happened, not what was asked for.** `type()` returns characters
  actually sent. A write that was not acknowledged is an error, not a success.
  The expensive bugs in this project's history were all a layer reporting
  success into the void.
- **Never claim an action worked without evidence it did.** That applies to the
  code and to the agent using it.
- **Bound anything held.** Keys, buttons, requests. A stuck key auto-repeats on
  the target forever and nothing on screen says why.
- **Prefer a loud failure to a convenient one.** Silently skipping something
  unmappable, clamping a coordinate that should have been rejected, or retrying
  a write that may have half-landed are all worse than an error.

Changes that weaken an authentication default, or that make a failure quieter,
need a strong argument rather than a small diff.

## Tests

Every non-trivial change should leave behind one runnable check that fails if the
logic breaks. Not a suite - the smallest thing that would have caught it. All the
existing tests run against temporary files and an in-process fake, so "I do not
have the hardware" is not a reason to skip one.

## Reporting bugs

`agentkvm doctor <board>` output in the issue saves everyone a round trip: the
chain is board → daemon → key → USB gadget → capture, and almost every report of
"it does not type" is one of those five, already named in that output.

## Security issues

Please do not open a public issue. See [SECURITY.md](SECURITY.md).

## Licence of contributions

A patch you send is licensed under Apache-2.0, as section 5 of LICENSE says, unless you state
otherwise in writing when you send it.
