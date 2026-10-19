# From a bare board to a verified keystroke - an off-the-shelf KVM

This path adds a small daemon beside a commercial KVM's own firmware. Nothing is
flashed and nothing is replaced: the board keeps its web UI, its video, its
virtual media and its power control, and you gain an interface an agent can use.

Written against the **Sipeed NanoKVM**. Another board with a USB HID gadget and
an absolute pointer should work - see [CONTRIBUTING](../CONTRIBUTING.md) if you
want to add one, and please establish its report layout rather than assuming it.

> **Not yet verified end to end by this project.** The `agentkvm` steps are
> tested against fakes, not hardware. If a step here is wrong, that is a bug
> worth reporting.

## What you need

| | |
|---|---|
| board | a NanoKVM, with its stock firmware |
| cables | HDMI from the target, USB-C from the board into the target, Ethernet |
| on your machine | Python 3.10+, `ssh` and `scp`, and `pipx` or `pip` |

## 1. Bring the board up on its own terms

Flash the vendor image to the card if the board did not ship with one, connect
Ethernet, and find the board's address from your DHCP leases or its mDNS name.

Open its web console and **confirm you can see the target and type into it from
there** before going any further. If the vendor's own console cannot drive the
machine, nothing built on top of it will either.

While you are in there:

- **change the default password.** The stock credentials for these boards are
  published, and this one holds a keyboard on another computer.
- **enable SSH.** The installer needs it; it is off by default on some firmware,
  and there is usually a toggle in the settings or an API call for it.

## 2. Check the USB-C is in the right port

This is the single most common way a first install "works" and does nothing.

The board's USB-C is what presents the keyboard. It must go **into the target** - not into your laptop, not into the machine running the agent. If it faces the
wrong way, the gadget is perfectly healthy and pointed at the wrong computer,
which looks exactly like a broken install.

`agentkvm doctor` checks this explicitly and reports it.

> ⚠️ On boards where the single USB-C carries both data and power, the board is
> powered *by the target* - so it goes down when the target does. That is worth
> knowing before you rely on it for a machine that will be off.

## 3. Install the daemon

```sh
pipx install agentkvm
agentkvm install root@<board-address>
agentkvm doctor  root@<board-address>
```

`install` runs on **your** machine and pushes over ssh. The board is never told
to fetch and execute anything from the internet. It:

1. reads which gadget nodes exist and picks the profile from **that**, not from
   the board's branding;
2. uploads a static binary and an init script;
3. lets the board generate its own key, then reads it back once into
   `~/.config/agentkvm/config.json` (mode 0600).

`doctor` then walks board → daemon → key → USB gadget → capture:

```
✓ board reachable       10.0.0.7, kernel 5.10.4
✓ daemon                responding on :9200
✓ key                   accepted
✓ USB gadget            configured: a host has enumerated it
✓ capture               169102 bytes
```

## 4. Prove it types

Against a text editor or a search box on the target - not a shell.

```sh
agentkvm shot before.jpg
agentkvm type 'hello from the kvm'
agentkvm shot after.jpg
```

Open both images and **look at them**. A tool returning success is not evidence.

Watch for **duplicated characters** (`helllllo`): that is a key held past the
target's auto-repeat threshold. The daemon bounds the hold on the board
specifically to prevent it, so if you see it, report it with the screenshots.

## 5. Give it to an agent

```sh
claude mcp add agentkvm -- agentkvm mcp
```

And the eyes, for the verbs that answer a question instead of returning a
picture:

```sh
pipx install 'agentkvm[vision]'
agentkvm vision --device http://<board-address>/api/stream/mjpeg
claude mcp add agentkvm -- agentkvm mcp --eyes 127.0.0.1
```

`agentkvm vision` needs `ffmpeg` on PATH and runs on **your** machine, not the
board - see [Where the eyes live](../README.md#where-the-eyes-live).

## When it does not work

`agentkvm doctor` names the first broken link. In rough order of how often each
one is the answer:

| symptom | cause |
|---|---|
| `USB gadget: not attached` | the USB-C faces the wrong machine - see step 2 |
| ssh refused | SSH is off in the vendor firmware, or the password was changed |
| daemon not responding | `/etc/init.d/S99agentkvmd restart` on the board |
| daemon responds, nothing types | some firmware re-adds a firewall rule on every boot; restart the service after a reboot |
| capture unreachable | the board's own video service, not this daemon - check it in the web console |

Two things that are the design rather than faults: the daemon and the board's own
firmware **share one HID gadget**, so driving from the web console while an agent
types means two people at one keyboard; and a firmware update may remove the init
script, in which case re-run `agentkvm install`.
