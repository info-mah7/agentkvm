# From a bare board to a verified keystroke - espkvm

The recommended path: one ESP32-P4 with a TC358743 HDMI bridge does capture and USB
HID by itself. Nothing is plugged into the machine you drive from, and nothing is
installed on the target.

> **Not yet verified end to end by this project.** The espkvm steps follow
> [upstream's documentation](https://github.com/espkvm/espkvm); the `agentkvm`
> steps are tested against fakes, not hardware. If a step here is wrong, that is
> a bug worth reporting.

## What you need

| | |
|---|---|
| board | one ESP32-P4 board, below |
| bridge | a **TC358743** HDMI-to-CSI module and a 15-pin CSI ribbon |
| cables | HDMI from the target, USB-C from the board's **OTG** port to the target, Ethernet |
| on your machine | Python 3.10+, and `pipx` or `pip` |

### Which board

Upstream ships build targets and flasher images for two, and either is a better
starting point than whatever is cheapest:

| board | silicon | notes |
|---|---|---|
| **[Waveshare ESP32-P4-ETH](https://www.waveshare.com/esp32-p4-eth.htm)** | rev v1.3 | upstream's default. 32 MB flash and PSRAM, 100M Ethernet, Pi-compatible CSI connector, USB 2.0 OTG HS, microSD |
| **[Espressif ESP32-P4 Function EV](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32p4/esp32-p4x-function-ev-board/user_guide.html)** | rev 3.2 | has an onboard ESP32-C6, so it does WiFi as well. The newer silicon feeds YUV422 straight to the encoders, freeing PSRAM and lifting 1080p a little past 20 fps. The browser flasher offers it directly |

**The board this project was developed against is neither**: a
[Waveshare ESP32-P4-Module-DEV-KIT](https://www.waveshare.com/esp32-p4-module-dev-kit.htm) - ESP32-P4 **rev v1.3** with an onboard ESP32-C6, 32 MB PSRAM, 100M Ethernet,
MIPI-CSI, USB 2.0 OTG HS, microSD - and **16 MB of flash**.

That last number is the problem. Upstream's default partition table wants
32 MB; the alternative table that fits 16 MB is built for the **other** silicon
family, and this part is v1.3. So neither shipped target fitted, and a two-line
board overlay had to be written before it would boot. Nothing was wrong with the
firmware - it simply had no target for this board.

Two other things about it, since it looks like an upgrade on paper: it carries an
ESP32-C6, but espkvm's WiFi support is for the Function EV board, so on this one
you are on Ethernet. And v1.3 silicon cannot feed YUV420 straight to the
encoders, which is the difference the rev 3.2 board's extra frames come from.

If you have a spare board already, check its flash size and chip revision before
assuming it will take a stock image. If you are buying, buy one of the two above.

Buy the bridge and the board together if you can - the ribbon and the connector
pitch are the fiddly part.

## 1. Flash the firmware

Download a release image and write it, or use the browser flasher (Chrome or
Edge, nothing to install) at <https://espkvm.io/flash/>.

```sh
esptool --chip esp32p4 -b 921600 write-flash 0x0 espkvm-<version>-<board>-merged.bin
```

Per-platform serial-port and driver notes are in upstream's
[docs/FLASHING.md](https://github.com/espkvm/espkvm/blob/main/docs/FLASHING.md).

**If it flashes but does not boot**, check the chip revision - builds for `<3.0`
and `>=3.0` silicon are mutually exclusive, and `esptool chip-id` prints yours
with the board attached. On a board upstream does not ship a target for, also
check the flash size: a 32 MB partition table on a 16 MB part will not boot, and
that combination is where an evening goes.

## 2. Wire it and find it

1. HDMI out of the target into the board.
2. USB-C from the board's **OTG** port into the target. This is the keyboard.
3. Ethernet.
4. Power the target on.

The device announces itself over mDNS: open **`https://espkvm.local/`**. It
signs its own certificate, so the browser will warn the first time.

**Do not skip the browser.** Confirm you can see the target's screen and type in
it from the console *before* adding an agent. If it does not work for you, it
will not work for an agent, and you will be debugging two things at once.

Set a password while you are there.

## 3. Point agentkvm at it

The package is not on PyPI; `./client` is the directory in a clone of this repository (`git clone https://github.com/info-mah7/agentkvm`).

```sh
pipx install './client[espkvm]'
agentkvm add-espkvm espkvm.local --password '<the password you set>'
agentkvm doctor  espkvm.local
```

`add-espkvm` opens both sockets before recording anything, so a saved entry is
one that worked at least once. `doctor` walks the chain afterwards:

```
✓ device reachable      espkvm.local, login accepted
✓ control socket        open, control claimed
✓ video                 148231 bytes
```

A failure at **video** with everything else green almost always means the target
is off or its HDMI is not connected - the board is fine.

Nothing is installed on the board by any of this. Uninstalling is deleting the
entry from `~/.config/agentkvm/config.json`.

## 4. Prove it types

Do this against a text editor or a search box on the target, not a shell.

```sh
agentkvm shot before.jpg
agentkvm type 'hello from the kvm'
agentkvm shot after.jpg
```

Open both images. **Look at them.** A tool that returned success is not evidence;
the screen is.

Watch specifically for **duplicated characters** - `helllllo` instead of `hello`.
That means keys are being held past the target's auto-repeat threshold. Report it
with the two screenshots.

## 5. Give it to an agent

```sh
claude mcp add agentkvm -- agentkvm mcp
```

For the cheap verbs - the ones that answer a question instead of returning a
picture, and cost no image tokens - run the eyes as well:

```sh
pipx install './client[vision]'
export AMI_ESPKVM_PASS='<the password you set>'
agentkvm vision --device https://espkvm.local/stream
claude mcp add agentkvm -- agentkvm mcp --eyes 127.0.0.1
```

`agentkvm vision` needs `ffmpeg` on PATH. It runs on **your** machine, not the
board - see [Where the eyes live](../README.md#where-the-eyes-live).

## When it does not work

Work down the chain rather than guessing; `agentkvm doctor` names the first
broken link. The usual four, in the order they occur:

| symptom | cause |
|---|---|
| `espkvm.local` does not resolve | mDNS blocked; use the IP from your DHCP leases |
| login refused | the password is the device's own, not your machine's |
| control socket opens, nothing types | a browser tab holds control - close it, or let `add-espkvm` take over |
| video times out | target off, or HDMI in the wrong port |

Before reporting anything else, read [the security
section](../README.md#security). This device holds a keyboard on another
machine, and where it sits on your network matters more than any setting in it.
