# AgentKVM

<img src="assets/robot-at-desk.png" alt="A humanoid robot sitting at a desk, typing on a keyboard and watching a monitor" width="320" align="right">

An agent's hands and eyes on a computer that has nothing installed on it: HDMI
out of the target, USB HID in, and an interface built for a model, not for
eyeballs.

The nickname is the idea: a USB keyboard the target trusts because it cannot
tell otherwise. Unlike its namesake it does not fire a payload and vanish. It
stays, it can see the screen it types on, and it answers only to whoever holds
the key.

Agents can drive anything with an API. The gap is everything else: a BIOS
screen, a GUI installer, a UAC prompt, an appliance with a web UI and no
credentials worth scripting, a locked-down vendor box, a laptop that will not
run an agent. Ordinary jobs that stop because there is nowhere to put a
program. So we go in the other way: **video out of the HDMI port, keyboard and
mouse in through the USB port, nothing installed.** The target sees a monitor
and a keyboard and cannot tell this from a person, so every screen a person can
operate becomes operable - including the ones that exist because no software
is running yet. That also means it works on machines you have no other access
to, whether or not the owner would like it. See **Security**.

The goal is not "an agent can move the mouse" - several projects do that. It is
a loop **reliable enough to leave running**: keystrokes that do not duplicate
under load, a typing count you can trust, an abort that says what landed, a
stuck key that releases itself. Each of those cost us an afternoon.

## With BaudHoundTerm, or on its own

AgentKVM is the companion daemon to **[BaudHoundTerm](https://github.com/info-mah7/baudhoundterm)**,
the Windows terminal and session manager. BaudHoundTerm gives it a window and a
point-and-click setup: add a KVM session, enter the board's address, press
**Pair with portal** to sign in to the board and fetch its key for you, and an
agent drives the screen with the same approval gates, skills and interrupt as
its VNC and RDP tabs. If you already run BaudHoundTerm, that is the whole setup.

It is not tied to it, though. The daemon is a small HTTP service, and nothing
above it is BaudHoundTerm-specific: a Python client and CLI, an MCP server so
any LLM agent gets the hands and eyes as tools, and a plain `/v1` API you can
`curl`. Used on its own it is a general "drive a computer over the network with
no software on it" building block.

**Using it standalone.** Install the daemon on the board once, over SSH:
`agentkvm install root@<board>` (see [Install](#install)). It generates a bearer
key, writes it to the board, and starts listening on `:9200`. From your own
machine you then drive the target three ways, whichever suits you:

- **Scripts** - the `agentkvm` CLI and Python client: `agentkvm --host <board> type "hello"`, `move`, `screenshot`, and the rest; or import the client in your own code.
- **An agent** - `agentkvm mcp --host <board>` serves the keyboard, mouse and screen to any MCP-speaking LLM as tools, so the model operates the machine directly.
- **Raw HTTP** - every verb is one authenticated POST, e.g. `curl -H "Authorization: Bearer <key>" -d '{"buttons":0,"x":16000,"y":16000}' http://<board>:9200/v1/mouse`. See [API](#api).

Eyes come from the board's own capture (`/v1/screenshot` where the firmware
offers one; on a NanoKVM 2.x, `agentkvm vision` runs a capture-and-query service
on the host instead). `agentkvm doctor root@<board>` walks the whole chain and
names the broken link if something is off.

## Two ways to run it

Open hardware on the ESP32-P4, or a closed KVM you buy off the shelf. Same
eyes, same hands, same MCP tools, skill and cost model above the hardware. The
choice is one line of configuration.

### The espkvm way

One **ESP32-P4 + TC358743** board that you flash. HDMI from the target, USB
into it, ethernet to your network; the machine running the agent has nothing
plugged into it. Right for installers, BIOS screens, appliances, dashboards.
Idle screen costs 0 kbit/s. Upstream quotes 20 fps MJPEG at 1080p; on our
bench it delivered **7 fps at ~200 ms** with its encoder at 97-100 %. Fine
when a model turn sits between looks; not for a loop that has to be quick.

### The nanokvm way

Our small daemon on a KVM you already own - the **Sipeed NanoKVM**.

#### Which one?

| | The espkvm way | The nanokvm way |
|---|---|---|
| **Pick it when** | you want open source | you want the convenience of an off-the-shelf product |
| **Hardware** | ESP32-P4 + TC358743 dev board | Sipeed NanoKVM |
| **Firmware** | open source, you flash it | vendor's, untouched |
| **Setup** | flash, three cables | `agentkvm install` over ssh |
| **Needs ssh to the board** | no | yes |
| **Credential** | the device's login | a key the board generates |
| **Vendor extras** | none | web UI, virtual media, power control |
| **Frame rate, measured** | 7 fps | 24 fps |
| **Action-to-visible, measured** | ~200 ms | 175 ms |
| **Uninstall** | delete a config line | stop the service, delete two files |
| **Walkthrough** | [docs/setup-espkvm.md](docs/setup-espkvm.md) | [docs/setup-nanokvm.md](docs/setup-nanokvm.md) |

Both measured at 1080p the same way: a HID action, then the first frame in
which the target region changes. Your results may vary. A board on a bad cable
or a 10 Mbit switch port gave us single-digit frame rates and over a second of
delay; a USB capture card on the agent's own host does 60 fps at 66 ms.

## The daemon

Runs *on* the KVM beside the stock firmware, writes USB HID reports straight to
the gadget devices (`/dev/hidg0` keyboard, `/dev/hidg2` absolute pointer on the
NanoKVM), serves one authenticated JSON API. The stock firmware is not touched;
video, EDID, gadget setup and updates stay the vendor's problem.

Why the gadget and not the board's own control API: those tap one key at a
time, press-and-release, with no way to hold. No ctrl-drag, no shift-click, no
chord outside a fixed list. Boot-keyboard reports to the gadget have no such
limit.

## API

All endpoints are `POST`, take JSON, and need `Authorization: Bearer <key>`.
No localhost exemption.

| Endpoint | Body | Notes |
|---|---|---|
| `/v1/mouse` | `{"buttons":0,"x":0,"y":0}` | absolute, `0..32767` per axis, origin top-left |
| `/v1/wheel` | `{"dy":-3}` | signed, one report |
| `/v1/keys` | `{"modifier":1,"keys":[4]}` | raw boot report, up to six usages; `{}` releases |
| `/v1/tap` | `{"modifier":0,"keys":[4],"hold_ms":40}` | press and release on the device, hold capped at 200 ms |
| `/v1/release` | - | everything up; pointer stays put |
| `/v1/screenshot` | `GET` | JPEG from the board's own capture, where the firmware offers one |
| `/v1/pair` | `{"vendor_token":"<jwt>"}` | returns `{"key":"..."}`; not behind the key, see below |
| `/healthz` | `GET` | unauthenticated, says only `{"ok":true}` |

**Pairing.** `/v1/pair` is how a client that does not yet hold the key obtains
it, so a human need not copy 64 hex characters by hand. It is not behind the
key. The proof of authority is instead a valid session on the board's own web
portal: the caller sends the token that portal issued, and the daemon checks it
by calling the stock API on loopback (`--verify-url`, default
`http://127.0.0.1/api/vm/info`). A 200 there means the caller is signed in to
the board as an admin, which is the same authority as reading the key file over
SSH, so the daemon returns the key. Anything else is `403`, throttled to one
attempt every 500 ms. Over a network this endpoint, like the key it hands out,
belongs behind TLS or a tunnel.

**Why the tap happens on the device.** A key held past the target's auto-repeat
delay (~250 ms on Windows) duplicates: `notepad` arrives as
`noteeeeeeeeeeeepad`. As two round trips from a loaded host, press and release
drift 350-680 ms apart. Done here, the hold is 40 ms whatever the caller is
doing. Anything still held is released after 5 s of silence - a stuck key
repeats forever and nothing on screen says why.

## Install

The two paths are different shapes; read the table above before buying. Each
has a walkthrough from unopened box to verified keystroke:
[espkvm](docs/setup-espkvm.md) · [nanokvm](docs/setup-nanokvm.md).

### The espkvm way

Flash the firmware first - the one step this tool does not do. Upstream makes
it easy: esptool, or a browser at [espkvm.io/flash](https://espkvm.io/flash/).

```sh
esptool --chip esp32p4 -b 921600 write-flash 0x0 espkvm-<version>-<board>-merged.bin
```

Ethernet, HDMI from the target, USB-C OTG into the target. The board appears at
`https://espkvm.local/`. Details, drivers and serial notes are in upstream's
[FLASHING.md](https://github.com/espkvm/espkvm/blob/main/docs/FLASHING.md).
Then, from a clone of this repository (the package is not on PyPI):

```
pipx install './client[espkvm]'
agentkvm add-espkvm espkvm.local --password <device password>
agentkvm doctor  espkvm.local
```

Nothing lands on the board. `add-espkvm` opens the sockets to prove they work
and records the address. Uninstall: delete a line from the config file.

### The nanokvm way

Nothing to flash. The board keeps its stock firmware; this adds a daemon
beside it.

The package is not on PyPI; `./client` is the directory in a clone of this repository (`git clone https://github.com/info-mah7/agentkvm`).

```
pipx install ./client
agentkvm install root@nanokvm.local     # detects the board, provisions a key
                                        # (needs agentkvmd.<arch>: `make`, or
                                        #  a GitHub release + --binaries <dir>)
agentkvm doctor  root@nanokvm.local     # walks the chain, names the broken link
```

`install` runs on *your* machine and pushes over ssh; the board never fetches
anything. It detects the board from the gadget nodes present, not the branding,
uploads the matching static binary and init script, and reads back the key the
board generated for itself. The key goes to `~/.config/agentkvm/config.json`
(0600).

Then drive it from a shell:

```
agentkvm shot screen.jpg
agentkvm type 'hello'
agentkvm key ctrl+alt+del
agentkvm click 0.5 0.5
```

...or hand it to an agent:

```
claude mcp add agentkvm -- agentkvm mcp
```

With the eyes running too, the agent gets the cheap verbs - the ones that
answer a question instead of returning a picture:

```
pipx install './client[vision]'
agentkvm vision --device http://<board>/api/stream/mjpeg   # on YOUR machine
claude mcp add agentkvm -- agentkvm mcp --eyes 127.0.0.1
```

On espkvm the source is its own MJPEG endpoint and the organ logs in for you
(`AMI_ESPKVM_USER` / `AMI_ESPKVM_PASS`); on a NanoKVM the same with
`AMI_NANOKVM_USER` / `AMI_NANOKVM_PASS`:

```
agentkvm vision --device https://espkvm.local/stream
```

The MCP server exposes `look`, `click`, `move`, `type_text`, `press`, `scroll`
and `release_all`, plus `wait_for_change` and `look_at` when eyes are
configured. Everything that acts needs a `look` within the last 60 seconds; an
agent cannot drive a screen it has not seen. `release_all` is exempt - it is
the recovery verb. `skills/agentkvm/SKILL.md` carries the part that matters
more than the verbs: look, model, act open-loop, verify once, never report
success without looking.

### What it changes on the board

Nothing. One static binary in `/root/agentkvm` plus an init script; the stock
firmware is not modified, patched or replaced. Web UI, video stream, virtual
media, power control - all still there. Uninstall: stop the service, delete
two files.

One caveat: the daemon and the stock firmware write to the same HID gadget, and
keyboard reports are stateful - a report replaces what is held. Drive from the
board's web console while an agent types and the two interleave. Nothing
breaks; `release_all` clears it. It is one keyboard with two people at it.

## When it doesn't work

`agentkvm doctor` walks board → daemon → key → USB gadget → capture and stops
at the first broken link:

```
✓ board reachable       nanokvm.local, kernel 5.10.4
✓ daemon                responding on :9200
✓ key                   accepted
✗ USB gadget            state: not attached
                        → the USB-C cable is not in a powered host port on the
                          TARGET, or the target has not enumerated the device
```

Most first-install failures are that last one: the board's USB-C is still in
the control machine, so its keyboard is healthy and pointed at the wrong
computer.

### A healthy path is not a running target

`doctor` proves the chain from this machine to the target's USB stack. It says
nothing about whether the software on the target is still doing anything. Those
are two questions and only the first has an answer on this side of the wire.

A stopped application accepts keystrokes exactly as a working one does. The
gadget stays enumerated, reports are accepted, and on Windows the target's own
lock LEDs still come back. On one bench session the target's program stopped
advancing for minutes while the bridge reported no errors at all and the board's
status showed everything healthy. Nothing in the control path was wrong, and
nothing in the control path could have said so.

The picture does not settle it either. A still frame with something caught
mid-movement is what a crashed program looks like, and equally what a busy one,
a throttled one, or one waiting on a lock looks like. We spent a session on that
ambiguity and produced two confident wrong diagnoses before the real cause, each
with a plausible fix that moved the symptom enough to look like progress.

If a run depends on the target making progress, check something a stopped screen
cannot satisfy: a value that has to change, not a frame that has to arrive. Where
you control the software on the target, make it put its own faults on the screen,
where the camera can read them.

## What a look costs

Every screenshot is image tokens, and a full screen is not cheap. Models bill
`ceil(width / 28) * ceil(height / 28)` visual tokens. Recent Claude models (4.7
and later) take up to a 2576 px long edge, so **a 1080p screenshot is not
downscaled before billing**:

| What you send | Visual tokens | Opus 5 @ $5/M input |
|---|---:|---:|
| 1920x1080 - the whole screen | 2,691 | $0.0135 |
| 1456x819 - what a standard-tier model would see | 1,560 | $0.0078 |
| 960x540 - half scale, still readable for layout | 700 | $0.0035 |
| 400x200 - one dialog, cropped | 120 | $0.0006 |
| 200x100 - one field or button | 32 | $0.0002 |

Fifty full-screen looks is ~135,000 tokens, about **$0.67 a session**, and
most of them are asking "has it finished yet?" - a question, not a picture. So,
in order of what they save:

1. **Do not poll with screenshots.** `wait_for_change` returns a number and
   costs no image tokens. Look once when it says something happened.
2. **Crop to what you are reading.** `look_at("x,y,w,h")` on a dialog is 120
   tokens, not 2,691.
3. **Downscale when you only need layout.** Half scale is a quarter the cost.

Prices are per model as of 2026-08; check current
[pricing](https://claude.com/pricing) and multiply by the token counts, which
do not change. Other vendors bill differently; the principle - a full screen
costs one to two orders of magnitude more than a crop - holds.

### How `wait_for_change` works

Newest frame as baseline, then every *new* frame compared against it until
enough differs or the timeout expires. Baseline and comparisons are decoded in
JPEG **draft mode at 1/4 scale**, grayscale, cropped to the region asked about
- several times cheaper than a full decode and plenty to answer "did this
move". Pixels differing by more than a threshold are counted; past `min_px`,
the wait returns.

`min_px` is in **full-resolution** pixels, because that is the unit you can
reason about; the service converts. At 1/4 draft, sixteen screen pixels are
one. Reference points at 1080p: a text caret is ~36 pixels, a mouse cursor
~380. A threshold near 100 will insist a cursor that moved did not.

What happens when nothing arrives matters most. A timeout after real
comparisons is an **answer**: the screen did not change. A timeout with no
frame compared is a **capture problem**, and calling it "unchanged" would
confuse "it did not move" with "I could not look". So the second returns
`inconclusive: true` with a reason, and every reply carries `frame_age_s`,
`worst_gap_s` and `stale`, so the caller can see the health of the eye it is
trusting.

None of this returns an image or costs image tokens. An agent can follow a
ten-minute operation for free and pay for a picture only when something
happens.

#### Why not use the codec's own motion signal

With H.264, P-frame sizes and motion vectors say roughly where the picture
moved, for free. Not used here, for three reasons; the third decides it.

- **MJPEG is what is on the wire.** Every frame is independent; there is no
  inter-frame signal.
- **Decode stopped being expensive once the eyes moved to the host.** A
  quarter-scale draft of one region costs almost nothing there; the loop is
  dominated by capture and encode on the board.
- **The codec signal answers a weaker question.** Frame size says *something
  changed somewhere*; the caller asks whether the region at 400,300 changed by
  more than 150 pixels. A fade, a blinking caret or sensor noise inflates a
  frame, and a small real change hides in one. You decode to confirm anyway, so
  you have bought a coarse pre-filter, not an answer.

It would earn a place as a gate in front of the decode, and only if decoding
turned out to be the bottleneck on the host. Nobody has measured that, so it is
not here.

### What that means in a session

Twenty minutes watching an installer, Opus 5 at $5/M input:

| How you drive it | Image tokens | Cost |
|---|---:|---:|
| A full-screen look every 20 s | 161,460 | $0.81 |
| `wait_for_change` + six full looks + twenty crops | 18,546 | $0.09 |

**About 90 % less, and the waits are free.**

Screenshots must also come back as *images*. A 1080p JPEG off the organ is
~169 KB: as an image, ~2,700 visual tokens; as a base64 **string**, ~56,000
text tokens - **21x** - for characters no model can read as a picture. The MCP
tools return image content, and a test pins that, because it is the most
expensive mistake this interface can make and invisible until the bill.

Host cost is negligible: the capture loop is an ffmpeg MJPEG passthrough that
never decodes; only the query endpoints decode, in draft mode, over the region
asked for.

### Which model does the driving

The author runs a **local model for the routine driving and escalates to a
frontier model for the steps that need judgement**. Most of a session is
"click here", "type this", "has the dialog appeared" - work a small local model
does at zero marginal cost, screenshotting as often as it likes. Escalation is
worth paying for when the screen is ambiguous, the plan needs revising, or
something has gone wrong in a way that needs reading rather than matching.
Nothing requires that arrangement; the MCP server talks to whatever connects.
But the verbs are shaped for it: cheap questions a small model can loop on,
expensive looks worth escalating for.

## Security

**This is a device whose whole purpose is to type on your computer.** Read this
before putting one on a network you care about.

### Threat model

Whoever reaches the daemon's port with the key can press any key and click
anywhere. On a machine with a shell, a browser or a login prompt that is
**arbitrary code execution on the target**, out of band, invisible to the
target's logs, unaffected by anything installed on it. No sandbox, no undo, no
confirmation. Physical access to the board gives the same, plus the key.

### What the daemon does

- **Authentication is mandatory.** It refuses to start without a key and checks
  every request. No localhost exemption: code on the board is not authority to
  type on the target.
- **The key is generated on the board**, 32 bytes from the kernel CSPRNG,
  stored `0600`, read back once by the installer, never sent to the board and
  never on a command line.
- Comparison is constant-time.
- **Anything held is released** after five seconds of silence.
- The hold in a tap is capped, so no request can pin a key down.

### What it does not do, so you must

- **No TLS.** The bearer token crosses the network in cleartext. Run it on a
  trusted segment or tunnel it (`ssh -L`).
- **No rate limiting, no lockout.** The key is the whole of the access control.
- **No audit trail**, deliberately: an audit log of a keyboard is a keylogger,
  and real passwords go through this path. If you need accountability, record
  at the caller - who asked, what kind of action, how many characters, never
  the characters.
- **Screenshots are unfiltered.** Whatever is on the screen - a password
  manager, a customer's data, a private message - goes to whoever asked, and if
  that is a model it leaves your network. It is also **untrusted input**: text
  on a screen can carry instructions, and an agent reading it may act on them.
- **Expose only this port.** See below.

### Security testing, and where the boundary is

Driving a target through its own console is how you test a machine with no
agent on it - a build you are hardening, a kiosk, an appliance with no API, a
lab image against a checklist. The tool does not distinguish authorised work
from unauthorised, and neither does the target. Your authorisation to operate
the machine is what makes the difference, and it lives outside this software.

**It is not a rubber ducky, structurally.** A HID injector types blind into a
machine it cannot see and depends on nobody watching. This is closed-loop, and
the MCP interface **enforces it**: `click`, `type_text`, `press`, `move` and
`scroll` refuse unless `look` returned a frame in the last 60 seconds.
(`release_all` is exempt; needing a screenshot to clear a stuck key would be
backwards.)

That is **a guardrail on the agent, not an access control on an attacker**.
The HTTP API enforces nothing of the kind, and anyone with the key bypasses the
idea. What it buys is that an agent cannot act on an assumption about a screen
- a correctness property first, a safety one second.

**The real gate is the target's own session.** This daemon has no privilege
model and cannot have one: it presses keys, and keys do whatever the logged-in
account can do. So **run the agent against an account with exactly the
permissions the task needs.** A standard user cannot approve a UAC prompt; a
scoped service account cannot reach production; a VM snapshot makes a mistake
free to undo. If the account is a domain admin, that is the blast radius, and
nothing here narrows it. That is also the answer to "how do I stop the agent
doing something terrible": constrain the account, not the keyboard.

### Hardening the board

The daemon is not the only thing listening on a stock board. Treat the KVM as a
device that types on your computer, because it is: change the default
SSH/console password; set whatever API key or web password the firmware
offers; restrict the firmware's own ports to a management host and expose only
this daemon's port to whatever drives it; keep the firmware current.

### Using it on someone else's machine

A driven keyboard is indistinguishable from a person at the machine, and it
defeats every control that assumes one. Use it only on hardware you own or are
explicitly authorised to operate; "I had the credentials" and "I was
authorised" are different claims. Reporting a security issue in this project
is welcome. Creating one with it is not.

## Status

Report layouts and endpoint behaviour are covered by `go test`, the client by
`pytest`. Run on hardware as follows.

### Known gaps

- **Run on one board so far.** On 2026-08-23 the v0.1.0 daemon went onto a
  Sipeed NanoKVM (image 2026-06-10, application 2.4.3) with `agentkvm
  install`. With a Linux host: keystrokes landed, Caps Lock toggling 46 ms
  after the call with the default 40 ms hold, **4.5 ms with `hold_ms=1`**, so
  daemon + gadget + wire is about 3 ms. Three things broke and are fixed:
  `install` over a running daemon hit "Text file busy" (it now stops the
  service first); an ssh alias as the target became an unresolvable API host
  (it now asks `ssh -G`); and the capture URL returns a 404 page on firmware
  2.x, which has no unauthenticated snapshot - use `agentkvm vision` on the
  host for eyes. With a **Windows 11 host**: keyboard landed (Windows answered
  Caps Lock with LED reports), but Windows did not poll either mouse interface
  and a blocking write to the unread pointer **hung the daemon before it could
  listen**. Fixed: gadget nodes are opened non-blocking with a 300 ms deadline,
  an unread interface returns **503 "host is not polling this HID interface"**,
  startup release is best effort. After the board re-enumerated, Windows polled
  all three and the **pointer worked: 12 absolute moves, cursor visible in a
  median 175 ms**. So the unread-mouse state was a hot-plug hiccup on the
  host, not the descriptor; the daemon now survives it either way. Also seen: the board drops TCP SYNs now and then, so a
  fresh connection occasionally waits a second while a kept-alive one answers
  in 1.5 ms; the client opens a connection per request and should not.
- **US keyboard layout only.** `type_text` maps characters with a US table. On
  a target set to jp106, de105, uk105 and so on, some characters come out wrong
  or not at all; the returned count says how many were sent, not whether they
  were right. `press` chords are unaffected. Plumbing for other layouts exists
  (`Kvm.type(layout=...)`); nothing exposes it yet. Until then, set the target
  to US or type via `press`.
- The organ turns off TLS verification for an `https://` source, because these
  devices sign their own certificates. Fine on a trusted network, the only
  place any of this belongs.

### Where the eyes live

The daemon is **hands only, on purpose**: HID reports and one JPEG on request.
Change detection, ROI crops and settle-waiting belong on the driving machine,
for two reasons, the second decisive:

- **The board is the slowest thing in the loop** - a single-core SoC already
  encode-bound. A decode-and-diff there makes the slow leg slower.
- **Where the diff runs does not change the token bill.** The saving is not
  sending an image to a model, decided host-side whichever machine compared.
  Board-side diffing only saves LAN bandwidth, and a 1080p MJPEG stream is
  ~10 Mbit/s.

| | Runs on | Does |
|---|---|---|
| Hands | the board | HID reports, bounded tap, stuck-key watchdog, one-shot JPEG |
| Eyes | the host | stream decode, change detection, ROI crops, settle detection |

### Two things called agentkvm

The eye half is merged here as the `[vision]` extra: `agentkvm vision` runs
the capture loop and the query service that `wait_for_change` and `look_at`
sit on. What stays outside is the author's older research package - the
`Bridge` over swappable HID backends, actuator calibration, replay. The daemon
exists so that there is now **one** backend with a documented HTTP API.

```
pipx install ./client              # client, CLI, installer, doctor, MCP. No deps.
pipx install './client[vision]'    # + the capture loop and the cheap questions
pipx install './client[cursor]'    # + pixel-only cursor localisation (OpenCV)
```

The base install is dependency-free and CI asserts it. `[vision]` needs ffmpeg
on PATH and Pillow; numpy is optional and only makes the compare faster.

## Prior art

A small piece on top of a lot of other people's work.

**Boards and firmware.** [PiKVM](https://pikvm.org/) defined the open IP-KVM
(`kvmd`, `ustreamer`, a scriptable API). [JetKVM](https://jetkvm.com/) rebuilt
it on a cheap SoC with a Go backend and WebRTC, and its source is why a board
like this can be understood at all. [Sipeed's
NanoKVM](https://wiki.sipeed.com/nanokvm) put it on RISC-V at a one-per-machine
price, and its gadget configuration is what this daemon writes to.
[Luckfox's PicoKVM](https://wiki.luckfox.com/Luckfox-PicoKVM/) carries
JetKVM's lineage to another SoC and ships its own agent-facing tool server,
which is why this daemon does not target it. [TinyPilot](https://tinypilotkvm.com/) went
commercial on the Pi. All theirs, not ours; this project talks to their gadget
devices and contains no code from any of them.

**Agent-driven computer control.** Anthropic's computer use, and the
tool-calling and MCP conventions the verbs are exposed through, are why there
is anything to plug into. Those drive the machine from *inside* it - strictly
more capable when you can install them, useless when you cannot.

**Agent control through a KVM** is being approached from more than one
direction. What we think is missing is not the verbs but the loop discipline around them and the
accuracy of the feedback. A `type_text` that silently drops characters, or a
click that reports success without anyone looking, makes an agent confidently
wrong about a real machine. Most of `SKILL.md` and most of the awkward details
in this code are that problem.

**Why not the alternatives.** VNC and RDP need software on the target and a
booted OS - the cases this exists for have neither. A bare HID bridge (CH9329,
an ESP32, a Pi Pico) gives hands and no eyes. iDRAC, iLO and IPMI are
excellent, closed, vendor-specific, and absent from every laptop and mini PC.

## Disclaimer

**No warranty.** Provided "as is", without warranty or condition of any kind,
as set out in the Apache-2.0 licence. Nothing here is a guarantee and no
contributor is liable for loss or damage arising from use.

**The security section is design intent, not assurance.** Not audited,
reviewed, certified or tested against any standard. The properties described
are what the code is *meant* to do and may contain defects. Do not rely on any
of it as a security control; the boundary that matters is the target's own
account.

**Any use described is an illustration, not a recommendation.** Whether a use
is lawful, permitted, safe or sensible in your situation is yours to determine.
Confirm you are authorised to operate the machine and that doing so breaks no
licence, support agreement, acceptable-use policy, accreditation or law. A
vendor agreement that forbids automated interaction is not satisfied by
automating it through a keyboard.

**Not for safety-critical use.** Not an interlock, an alarm path, a control
loop, or the only thing watching something that matters. It presses keys on a
best-effort basis over a network, through hardware that can fail silently, and
may act on a screen it misread.

**It types, and typing is destructive.** No undo. A misread screen, a wrong
coordinate or a confused agent can delete data, change configuration or
approve something irreversible. Test against something you can restore, with
an account scoped to the task.

**Screenshots may capture personal or confidential data**, and sent to a
third-party model they leave your control. Where that data concerns
identifiable people, you are the party responsible for a lawful basis and for
what your model provider's terms allow.

**Not legal advice.** Written by engineers, not lawyers.

**Third-party names** are used for identification only and imply no
affiliation or endorsement; all marks belong to their owners.

This is AI-assisted work.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In brief: no hardware needed, every
change wants one runnable check, and **nothing may be copied out of a board's
GPL firmware** (NanoKVM and PiKVM are GPL-3.0) - that rule is what keeps this
licensable as it is. Security
issues go through [SECURITY.md](SECURITY.md), privately.

## License

Apache-2.0. This project talks to the stock firmware over loopback HTTP and
writes to kernel gadget devices; it contains no code, key tables or other
material from any board's firmware.
