# iphone-hil — let an AI agent see and operate your iPhone

> **A hardware-in-the-loop iPhone assistant.** A Mac captures the iPhone's screen over USB (the same channel QuickTime uses to record an iPhone), an agent such as Claude Code looks at the frames and decides what to do, and a $3 ESP32 board acts as a Bluetooth mouse + keyboard, delivering taps and keystrokes through iOS's built-in accessibility support for pointer devices.
> To the apps on the phone this is simply a person using a mouse on an ordinary iPhone. No jailbreak, no profiles, no touching the app's process or network.
>
> 中文说明（更详细）：[README.md](README.md)

```
┌──────────────────────────── Mac ─────────────────────────────┐
│  HIL Capture.app ◀── USB (QuickTime screen-recording channel) ┼── iPhone
│        │ frames                                               │     ▲
│        ▼                                                      │     │ BLE HID
│  Agent (Claude Code): look → decide where to tap / what to type│     │ mouse+keyboard
│        │ commands (MV / CK / KP / TX …)                        │     │
│        ▼                                                      │     │
│  HIL Link.app ──── BLE custom command service ────────────────▶ ESP32 (HIL-Bridge)
└──────────────────────────────────────────────────────────────┘  powered by any USB charger
```

Measured on a classic ESP32 and a 1284×2778 iPhone: ~20 ms per frame grab; a tap lands within 2–5 px in 1–4 s; a Mac→ESP32 command takes 30 ms median, 200 commands in a row with zero failures. The first real task was navigating WeChat to a hospital's official account, opening "Outpatient → Lab reports", and reading eight reports one by one: ten taps, none missed, no text typed.

## Why

In China, most everyday services—hospital appointments and lab results, utility bills, government services, food delivery, shopping—no longer have a usable web front end. They live exclusively inside WeChat official accounts and mini programs, Alipay mini programs, or standalone phone apps. A browser-driving agent that happily compares flights across dozens of sites can't read your own lab report, because there is no page to open.

We tried the obvious routes first and documented why each fails:

- Proxying WeChat's in-app browser to steal the OAuth session into Chrome → WeChat's risk control logs the account out.
- Reading the in-app browser's cookies from disk and injecting them → same outcome once any automation touches the WeChat window; `SendInput` on Windows didn't help either.
- Running a mini program in a browser → impossible; it needs WeChat's private `wx.*` runtime.
- macOS **iPhone Mirroring** → the window exposes **no accessibility tree**, is **invisible to System Events / osascript**, **blocks region screenshots**, and only accepts clicks while frontmost. Apple deliberately walled it off from automation.

The conclusion: the only clean entry point is **a real phone running the real app, receiving real input**. This project gives a Mac-side agent eyes and a hand for that phone, using only capabilities Apple ships on purpose:

- **Eyes**: the USB screen-capture device that QuickTime's "New Movie Recording" uses (CoreMediaIO `AllowScreenCaptureDevices` + AVFoundation). 60 fps, no recording indicator on the phone, camera permission on the Mac side.
- **Hand**: **AssistiveTouch pointer-device support**. AssistiveTouch (iOS 5, 2011) is the accessibility feature for people with limited motor control; since iOS 13 (2019) it lets an iPhone use Bluetooth/USB mice, trackpads, joysticks and other adaptive pointing devices—a standard HID mouse is translated into touches. Any Bluetooth keyboard has worked since iOS 4. iOS treats a mouse as an accessibility input, not an automation interface, so apps simply see `UITouch`.
- **Closing the loop**: a HID mouse only reports relative motion and iOS adds pointer acceleration, so the agent cannot say "go to (x, y)". `hil/locate.py` finds the pointer in the frame, moves, looks again, and converges in a few iterations.

## How the pieces fit

| Path | Role |
|---|---|
| `hil/capture.py`, `hil/capture_server.py`, `tools/make_app.sh` | Frame grabber as an HTTP service packaged into `HIL Capture.app` (camera permission is granted per app; the Claude Code host has no camera entitlement) |
| `hil/protocol.py` → `docs/PROTOCOL.md` | Text-line command protocol: `MV dx dy`, `CK`, `LP ms`, `WH n`, `KP GUI+H`, `TX text`, `CC HOME`, `CFG …`; one `OK`/`ERR` line per command |
| `hil/link.py`, `hil/link_server.py`, `hil/ble_central.py`, `tools/launcher.c` | Link daemon holding the serial or BLE connection; BLE central via CoreBluetooth, packaged into `HIL Link.app` (CoreBluetooth requires the executable itself to live in a bundle with a usage description, hence the native launcher that `dlopen`s libpython) |
| `hil/pointer.py`, `hil/locate.py` | Pointer detection (HSV + circularity of the filled ring) and feedback localization: park to the top-left corner, calibrate a gain table, approach with damping and online gain adaptation, recover when lost |
| `hil/sim.py`, `tests/` | Simulated phone (acceleration, noise, auto-hide, decoys) so the loop runs in `pytest` without hardware |
| `firmware/hid_bridge/` | ESP32 firmware (Arduino + NimBLE): BLE HID (keyboard/mouse/consumer) for the iPhone **and** a Nordic-UART-style command service for the Mac, two simultaneous connections, config in NVS, serial for flashing and reading the token |
| `skill/iphone-hil/` | A Claude Code skill: preconditions, work loop, pacing, limits, safety rules |

Both `.app`s are launched on demand by any `hil` command and quit after 10 idle minutes.

## Hardware

- Mac (macOS 13+), iPhone (iOS 13+), a data-capable cable between them.
- A classic ESP32 dev board (DevKitC-style, CH340/CP2102 serial bridge); ESP32-S3 is supported but untested on hardware. Powered by any USB charger during use.

## Setup

```bash
git clone https://github.com/ghbhiee/iphone-hil.git ~/iphone-hil && cd ~/iphone-hil
uv venv .venv --python 3.12 && uv pip install -r requirements.txt
cp hil.example.json hil.json
.venv/bin/python -m pytest -q tests          # 17 tests, simulation only
```

Flash the firmware (details in [firmware/FLASH.md](firmware/FLASH.md)):

```bash
pip install esptool
esptool.py --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write_flash \
  0x1000 firmware/prebuilt/esp32/hid_bridge.ino.bootloader.bin \
  0x8000 firmware/prebuilt/esp32/hid_bridge.ino.partitions.bin \
  0xe000 firmware/prebuilt/esp32/boot_app0.bin \
  0x10000 firmware/prebuilt/esp32/hid_bridge.ino.bin
screen /dev/cu.usbserial-XXXX 115200          # type ST ⏎, copy token=…, exit with Ctrl-A K
```

Put the token into `hil.json` as `ble_token`. Later firmware updates flash only the app partition at `0x10000`; a full-chip flash erases the NVS that holds the iPhone bond.

On the iPhone (once): Settings → Accessibility → Touch → **AssistiveTouch on** → Devices → Bluetooth Devices → pair **HIL-Bridge**; Accessibility → **Pointer Control**: a vivid color, larger size, **auto-hide off, pointer animations off**; Hardware Keyboard layout US; **Auto-Lock: Never**; connect the cable, unlock, trust the computer. Do **not** pair HIL-Bridge from the Mac's Bluetooth settings—macOS would claim it as its own mouse.

First run:

```bash
.venv/bin/python -m hil up                     # launches both services (approve camera + Bluetooth once)
.venv/bin/python -m hil grab                   # captures/frame.png
.venv/bin/python -m hil pointer-sample captures/frame.png X Y
.venv/bin/python -m hil park && .venv/bin/python -m hil calib
.venv/bin/python -m hil tap 300 520
```

## Driving it from an agent

```bash
python -m hil grab            # screenshot (half scale by default) for the model to look at
python -m hil tap X Y         # coordinates in the scaled image; converges then clicks
python -m hil long X Y 800    # long press
python -m hil drag X0 Y0 X1 Y1
python -m hil wheel 6         # scroll down (negative direction currently ineffective on iOS)
python -m hil type "ascii"    # ASCII only; make sure the English keyboard is active
python -m hil key GUI+H       # key combos; GUI+SPACE opens Spotlight
python -m hil home
python -m hil status | up | stop
```

Install the Claude Code skill with `ln -s ~/iphone-hil/skill/iphone-hil ~/.claude/skills/iphone-hil` (edit the path inside if you cloned elsewhere) and then just ask: "open WeChat, find hospital X's account and read my latest lab reports", "find a congee place rated 4.8+ on Meituan, add one to the cart and stop before payment".

## Safety and privacy

- Frames stay in `captures/` on your Mac, but the agent "looking" at them means they are sent to whatever model you use—same as pasting a screenshot into a chat. Delete them when done.
- While the iPhone is cabled, trusted and unlocked, any Mac program with camera permission can see its screen, with no indicator on the phone. Unplug when finished.
- The ESP32 command channel is unpaired and unencrypted, protected only by a token readable over serial. Its threat model is "don't let a neighbour's device connect by accident", not a determined attacker.
- The skill forbids entering IDs, passwords or payment details, and forbids paying, sending messages or changing settings unless the user explicitly asks. Copy those rules into your own agent prompts.
- Use it with your own devices and your own accounts.

## Known limitations

No reliable way to type Chinese yet (Universal Clipboard needs Handoff on both ends; untested). Negative wheel direction does nothing. Each tap takes 1–4 s, dominated by waiting for the pointer and the display to settle. Verified on one iPhone and one classic ESP32. The status bar in captured frames is iOS's fake demo bar (9:41, full battery)—never trust it.

See [HANDOFF.md](HANDOFF.md) (Chinese) for the full engineering log and pitfalls. MIT license.
