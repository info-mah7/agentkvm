---
name: agentkvm
description: Drive a real computer through an IP-KVM - screen in over HDMI, keyboard and mouse out over USB HID, nothing installed on the target. Use when a task needs a machine you have no API, agent, or shell on: GUI installers, BIOS/UEFI screens, UAC prompts, appliances, kiosks, air-gapped or vendor-locked hosts, or anything that only exists behind a monitor and a keyboard port.
---

# Driving a machine through a KVM

You have hands and eyes on a real computer and nothing else. No filesystem, no
shell, no undo. Everything you know about that machine comes from a screenshot,
and everything you do to it is a keystroke someone could have typed.

The tools are `look`, `click`, `move`, `type_text`, `press`, `scroll`, and
`release_all`. They are easy. Using them without flailing is the hard part, and
that is what this document is about.

## The loop

**Look → model → act → verify once.**

1. `look` before you act.
2. Decide what should happen, in full, before you touch anything.
3. Act **open-loop** - send the whole action, do not sample between steps.
4. `look` once afterwards to confirm what actually happened.

The trap is step 3. A screenshot shows you the screen *as it was*, and by the
time you have reasoned about it the machine has moved on. **Perception returns
dynamics, not state.** If you act on each frame as though it were current, you
chase, and chasing does not converge.

Constant latency is something you can calibrate for. Jitter is not. So predict
where things will be and commit, rather than nudging and re-reading.

## Rules that are not negotiable

**Never report success without looking.** "I pressed Enter" is not "the dialog
closed". The only evidence that an action worked is a screenshot taken after it,
showing the thing you expected. If you did not look, you do not know, and you
must not say that you do.

**Change is not progress.** A screen that differs from the last one has not
necessarily moved toward your goal - a repaint, a blinking caret, or a spinner
all change pixels. Verify by the *displacement of something you are tracking*
(a window title, a selected item, a value in a field), not by "the screen is
different now".

**Escalate, do not repeat.** If an action did not work, doing it again harder
rarely helps - you are usually stuck in a state the action cannot leave. Change
the approach: press Escape, click elsewhere first, use the keyboard instead of
the mouse, or back out to a screen you recognise.

**Type text, press chords.** `type_text` is for content. `press` is for keys
that mean something: `enter`, `tab`, `ctrl+c`, `alt+f4`. Do not try to type a
chord as text.

**US layout only.** `type_text` assumes the target's keyboard layout is US. If the
target is set to another layout, characters like `@`, `"`, `\`, `z`/`y` may come out
wrong; prefer `press` for anything that must be exact, and verify with `look`.

**Check the count.** `type_text` returns how many characters it *sent*. If that
is lower than what you asked for, the layout could not produce some of them and
the target got a shorter string than you think. Look before assuming the field
contains what you typed.

## Coordinates

`click` and `move` take **fractions of the screen**, 0..1, origin top-left. A
position you measured on a screenshot is the same number you pass to a click,
whatever the resolution.

Useful scale at 1080p: a text caret is about 36 px, a mouse cursor about 380 px,
a window's close button about 30 px across. When you are checking whether
something moved, a threshold near 100 px will false-fail on a cursor that did
in fact move.

## Before you touch anything

This is a real machine. Ask, in this order:

1. **Whose machine is it, and does the task actually authorise this?** Driving a
   keyboard is indistinguishable from a person sitting at it.
2. **What is on screen right now?** Do not act on an assumption about which
   application has focus. `look` first, every time you resume.
3. **Is anything unsaved?** You cannot undo a click. A modal you dismiss without
   reading may have been the only warning.

Never type credentials you were not explicitly given for this purpose, and never
type a password into a screen you have not confirmed is the login prompt you
expected - the target machine gets real secrets typed at it through this path.

## When it stops responding

Work down the chain rather than guessing:

- `release_all` first. A key stuck down auto-repeats forever and nothing on
  screen explains why; it looks exactly like "my typing is corrupted".
- `look` - is the screen black (target asleep or the input switched), frozen
  (same frame repeatedly), or fine but ignoring you?
- If the screen is live but input does nothing, the target is probably showing a
  secure desktop (a UAC prompt on Windows) or has lost the USB device.
- `agentkvm doctor <board>` from a shell walks the whole chain - board, daemon,
  key, USB gadget, capture - and names the broken link.

## What this is bad at

Report it rather than working around it badly:

- **Reading small or dense text.** If you cannot read it, zoom the target's own
  UI rather than guessing.
- **Long text entry.** Typing is deliberately paced. A page of text is minutes,
  and a clipboard path on the target is better if one exists.
- **Anything needing exact timing.** Round-trip latency is tens of milliseconds
  and varies. Do not build on frame-accurate input.
- **Knowing what happened while you were not looking.** There is no event log.
  If a dialog appeared and vanished between screenshots, it is simply gone.
