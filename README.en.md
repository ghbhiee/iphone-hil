# iphone-hil — let an AI agent see and operate your iPhone

> **A hardware-in-the-loop iPhone assistant.** A Mac reads the iPhone's screen over USB (the same official channel QuickTime uses to record an iPhone), an agent such as Claude Code looks at the frames and decides what to do, and a $3 ESP32 board plays the part of a Bluetooth mouse + keyboard, delivering taps and keystrokes through iOS's built-in accessibility support for pointing devices.
> To the apps on the phone this is just a person using a mouse on an ordinary iPhone. No jailbreak, no configuration profiles, nothing touching the app's process or network.
>
> 中文版（原文）：[README.md](README.md)

```
┌──────────────────────────────── Mac ─────────────────────────────────┐
│  HIL Capture.app ◀── USB (the channel QuickTime uses to record iPhones) ┼─── iPhone
│        │ frames                                                        │     ▲
│        ▼                                                              │     │ BLE HID
│  Agent (Claude Code): look → decide where to tap / what to type         │     │ mouse + keyboard
│        │ commands (MV / CK / KP / TX …)                                 │     │
│        ▼                                                              │     │
│  HIL Link.app ────── BLE custom command service ──────────────────────▶ ESP32 (HIL-Bridge)
└──────────────────────────────────────────────────────────────────────┘   powered by any USB charger
```

Measured on a classic ESP32 and a 1284×2778 iPhone: about 20 ms per frame grab; one tap lands within 2–5 px and takes 1–4 s;
a Mac→ESP32 command takes 30 ms median, 200 commands in a row with zero failures. The first real task was navigating WeChat to a hospital's
official account, going through "Outpatient services → Lab reports", and opening eight reports one after another to read them out:
ten taps in total, none missed, not a single character typed.

---

## Contents

1. [Why this exists](#1-why-this-exists)
2. [How it works: two channels Apple left open](#2-how-it-works-two-channels-apple-left-open)
3. [The hard part: a Bluetooth mouse only reports relative motion](#3-the-hard-part-a-bluetooth-mouse-only-reports-relative-motion)
4. [Architecture and components](#4-architecture-and-components)
5. [What it does and what it refuses to do](#5-what-it-does-and-what-it-refuses-to-do)
6. [Hardware](#6-hardware)
7. [Installation](#7-installation)
8. [Usage](#8-usage)
9. [The Claude Code skill](#9-the-claude-code-skill)
10. [Safety and privacy](#10-safety-and-privacy)
11. [Known limitations and to-do](#11-known-limitations-and-to-do)
12. [Pitfalls (for people who want to change the code)](#12-pitfalls-for-people-who-want-to-change-the-code)
13. [Repository layout, tests, protocol](#13-repository-layout-tests-protocol)

---

## 1. Why this exists

### 1.1 The Chinese internet has become an archipelago of islands

A browser is the best pair of hands and eyes an AI agent has. Let Claude compare international flights across dozens of pages, filter and
cross-check, and it finishes in minutes what would take a person an afternoon. Point the same ability at the Chinese internet and it stops
working. The reason is not technical; the ecosystem has arrived at a peculiar place.

**The symptom: services have retreated from the web into apps.** Hospital appointments, lab results, water/electricity/gas bills, social
insurance, government paperwork—ten years ago most of these had their own websites. Today nearly all of them exist only as WeChat official
accounts or mini programs, Alipay mini programs, or a standalone app you must install on a phone. Food delivery, grocery, ride-hailing and
local services practically never had a usable web version; e-commerce and travel sites that do have one require scanning a QR code with
your phone to log in, and many buttons just say "please open in the app". A major hospital used to run its own portal; it was taken down,
leaving two windows inside WeChat and Alipay.

**Why it happened: every party's rational choice adds up to an island.**

- For the platforms, keeping users inside their own ecosystem is the most direct form of value: traffic stays internal, payments run through
  their own rails, user data and attention belong to them. The entry point is power—whoever owns the first tap after a phone is unlocked
  can charge a toll to every service downstream.
- For institutions and merchants, opening a mini program inside a super-app is far cheaper than building a website: low development cost,
  login and payment ready-made, service notifications pushed straight to the user's phone, and the platform's traffic on top. A standalone
  website with no traffic gets no maintenance; abandoning the "real internet portal" on cost grounds is entirely rational.
- For ordinary users, the experience genuinely is polished to the limit: scan and use, no registration, one-tap payment, timely
  notifications. In fairness, inside the platforms, Chinese product craftsmanship is world-class.

**The cost: involution replaces innovation.** When every service lives inside two or three super-apps, competition happens only within
the platform's rules—over traffic allocation, ranking, and commission rates, not over who can build something new. More importantly,
**the ecosystem has closed its doors to the outside**: no public web pages, no open interfaces. Third-party tools, browser extensions,
search engines, assistive software for disabled users, and now AI agents are all locked out. The original fabric of the internet—URLs,
hyperlinks, indexability, programmability, "anyone, any device, any browser"—has been discarded wholesale. Users lose choice and cannot
move their data; developers lose any room to exist independently of a platform; the whole ecosystem grows ever more refined within its
own borders while becoming ever less interoperable with the world outside.

**The long-term risk: Galápagos syndrome.** The term was coined for Japan's mobile phone industry. In the i-mode era, Japanese feature
phones offered a superb domestic experience and features years ahead of the rest of the world, yet they evolved entirely along carriers'
proprietary standards, detached from the global ecosystem; when the smartphone wave arrived, what had been the most advanced industry on
earth was almost entirely wiped out. Japan's car makers' slowness to pivot to electrification and software is often cited as the same
pattern—perfecting your own paradigm is exactly what makes the turn hardest. China's app islands are on the same path: a perfected
experience inside private runtimes, and a vanishing interface to the open world. And another paradigm shift is arriving right now:
**AI agents are becoming the next generation of "users"**, and what they need is open, programmable, machine-accessible interfaces.
An ecosystem that locks everything inside private runtimes will be the first to lose the ability to be connected to.

### 1.2 What we tried first, and why each route failed

Before this project we spent a separate effort trying every approach we could think of (the test case was "read my lab reports from a
hospital's WeChat official account on a computer"):

| Attempt | Outcome |
|---|---|
| Intercepting proxy + root certificate to carry the session WeChat's in-app browser obtains after OAuth into Chrome | WeChat for Mac detected the abnormal environment and logged the account out |
| No proxy: read the cookies WeChat's in-app browser (Chromium-based) stores on disk and inject them into Chrome | Logged out again as soon as desktop automation touched WeChat; the "open it by hand, then read cookies" step stalled on macOS |
| On Windows, drive WeChat with `SendInput` hardware-level input | No better. Risk control is a server-side composite score, not a check on the injection method |
| Run a mini program in a browser as-is | Impossible: there is no `wx.*` runtime outside WeChat, and unpacking someone else's program to mock its API is neither realistic nor appropriate |
| Look for "iPhone-compatible" WeChat / Alipay / government apps in the Mac App Store | None of them allow running on Apple Silicon |
| **macOS iPhone Mirroring** (the phone screen in a Mac window) | You can see it, but Apple deliberately walled it off from automation: the window **exposes no accessibility (AX) elements**, System Events / osascript **cannot see the process at all**, **region screenshots come back black**, and clicks register only while the window is frontmost. An agent is left computing pixel coordinates from full-screen screenshots—slow and unreliable |

The conclusion was unambiguous: **the only clean entry point for an agent into Chinese internet services is a real phone, running the real
app, receiving real input.** From the app's and the risk engine's point of view, it has to be a person using a phone. The remaining
questions were how an agent on a Mac could *see* that phone, and how it could *reach out and touch* it.

### 1.3 This project's answer

- **Seeing**: the USB channel QuickTime uses to record an iPhone's screen; on the Mac, AVFoundation treats the phone as a video source and grabs frames.
- **Touching**: iOS's **AssistiveTouch pointer-device support**, built for users with motor impairments, lets an iPhone accept a standard Bluetooth mouse + keyboard. An ESP32 plays that mouse and keyboard, and the Mac sends it "move / click / key" commands over Bluetooth.
- **Closing the loop**: a Bluetooth mouse only reports relative motion and iOS adds pointer acceleration, so the agent cannot tell the pointer "go to (x, y)". The project **finds the pointer in the frame**, moves a step, looks again, and converges on the target in a few iterations before clicking.

All three are public capabilities Apple ships on purpose. No jailbreak, no private API, nothing installed on the iPhone.

This is not a solution to the island problem, only a detour: walls can stop APIs and browsers, but not pixels on a screen and a hand holding
a mouse. It does let an agent do on your behalf the things that can only be done inside a phone—and it is also a measure of how far an
ecosystem has drifted from the open world when this is the only way in.

---

## 2. How it works: two channels Apple left open

### 2.1 Seeing: USB screen capture (QuickTime's channel)

QuickTime Player's "New Movie Recording" can select a USB-connected iPhone as a camera and record its screen. Apple added this in OS X
Yosemite / iOS 8 so developers and media could record app demos. Underneath is CoreMediaIO: once a process sets
`kCMIOHardwarePropertyAllowScreenCaptureDevices`, a trusted iPhone appears as an AVFoundation capture device (modelID "iOS Device",
device type *muxed*). ffmpeg and OBS use the same path.

`hil/capture.py` does exactly this: enumerate the device, open an `AVCaptureSession`, and in the `AVCaptureVideoDataOutput` callback
convert the `CVPixelBuffer` to an RGB array and keep the latest frame. Measured: 1284×2778 at ~60 fps; grabbing a frame (including
encoding a half-size PNG) takes tens of milliseconds.

Things worth knowing:

- **There is no recording indicator on the phone.** This is not iOS's own Screen Recording feature; the Mac is pulling a mirror stream over USB and iOS shows nothing in the status bar. The side effect is that **iOS swaps the status bar for its demo style** (always 9:41, full signal, full battery). The agent must not trust the time or battery level in a screenshot.
- **The permission involved is the Mac's "Camera" permission**, granted per app. Hosts like Claude Code have no camera entitlement, so the capture program must be a standalone `.app` (see §12).
- **The capture device disappears when the iPhone locks** and returns on unlock. Set Auto-Lock to Never while in use.
- As long as the cable is connected and the phone trusts the computer, any Mac program with camera permission can see the phone's screen. That is both the foundation of this project and a privacy fact you should be aware of (§10).

### 2.2 Touching: AssistiveTouch pointer-device support

**AssistiveTouch** is an accessibility feature introduced in iOS 5 (2011). It was built for people with limited hand mobility who cannot
press physical buttons or perform multi-finger gestures: a floating button on screen opens a menu from which "Home", "Screenshot",
"Shake", "Pinch" and the like can be done with a single finger.

Since **iOS 13 (2019)**, AssistiveTouch includes **pointer-device support**: an iPhone can connect Bluetooth or USB mice, trackpads,
trackballs, joysticks and other adaptive pointing devices, a round pointer appears on screen, and clicking is equivalent to touching. Its
intended users rely on adaptive input—a chin-operated joystick, a head tracker. By design, then, it is "translate a standard HID mouse
into touches", which is precisely what we need. Details:

- Any mouse that follows the Bluetooth HID specification works; pairing is Just Works (no PIN).
- Settings → Accessibility → Pointer Control lets you change the pointer's color, size, auto-hide, snapping animations and tracking speed. This project sets the pointer to a **vivid solid color with auto-hide and animations off**, purely so it can be detected reliably in frames.
- iOS applies an **acceleration curve**: the larger a single mouse report's delta, the farther the pointer travels (non-linearly). This is why calibration and a visual feedback loop are mandatory (§3).
- The scroll wheel scrolls whatever is under the pointer; right-click opens the long-press menu; holding the mouse button is a long press.
- **Hardware keyboards** have been supported since iOS 4, independently of AssistiveTouch: any Bluetooth keyboard can type ASCII and send shortcuts (`⌘+H` Home, `⌘+Space` Spotlight, `⌘+Tab` app switcher). The Consumer Control "AC Home" key also goes Home.

In one sentence: **iOS treats a mouse as an accessibility input, not as an automation interface.** That is why it has not been blocked and
why apps cannot tell—all they ever see is `UITouch`.

### 2.3 The hand: an ESP32 as Bluetooth HID, also commanded over Bluetooth

The iPhone's only port is taken by the Mac's capture cable, so the mouse and keyboard have to be wireless. An ESP32 is the easiest hardware
for the job: a few dollars, BLE built in, and the NimBLE library in the Arduino ecosystem can run a HID service and a custom service at
the same time.

The firmware `firmware/hid_bridge/hid_bridge.ino` does three things:

1. **A BLE HID device** (report IDs: keyboard 1, mouse 2, consumer 3), bonded with the iPhone.
2. **A Nordic-UART-style custom command service** (RX write / TX notify). The Mac connects, sends `AUTH <token>`, then text commands: `MV dx dy`, `CK`, `LP ms`, `WH n`, `KP GUI+H`, `TX hello`, `CC HOME` … Each command returns one `OK` / `ERR` line. Full protocol in [docs/PROTOCOL.md](docs/PROTOCOL.md).
3. **Two simultaneous connections**: one for the iPhone (HID, bonded, locked to its address) and one for the Mac (commands, unpaired, token-protected). It keeps advertising until both slots are filled.

So the ESP32 normally just sits on a USB charger. The Mac talks to it through CoreBluetooth (`hil/ble_central.py`) without pairing in
System Settings—and it must **not** be paired there, or macOS will claim it as its own mouse and keyboard. To that end the firmware
advertises a generic appearance (0) rather than a HID appearance and locks its HID target to the first device that subscribes to mouse
reports (the iPhone).

Serial (a USB cable to the board) remains available for flashing, reading the token and changing configuration.

---

## 3. The hard part: a Bluetooth mouse only reports relative motion

A HID mouse report says "moved by dx, dy", never an absolute position; iOS adds acceleration on top, and each report is capped at ±127.
So the agent cannot say "pointer, go to (640, 1200)". `hil/locate.py` solves it with a visual feedback loop:

1. **Park**: send a huge negative move (e.g. −6000, −6000). The screen edge clamps the pointer in the top-left corner—the only deterministic origin. Nudge inward so the whole pointer is visible.
2. **Calibrate**: from the origin send 4 / 12 / 32 / 64 / 127 counts, measure the pixel displacement, and build a gain table "per-report count magnitude → pixels per count" (the measured iOS acceleration curve; 127 comes out lower than 64 because the firmware splits large moves into ±127 reports and the report counts differ). The table lives in `hil.json`; `Calib.predict_px` predicts with the same splitting the firmware uses and `counts_for` inverts it by bisection.
3. **Approach (`move_to`)**: error × damping 0.85 → counts → send → wait for the display to refresh and **for the pointer to settle** (two consecutive frames within 2 px) → search only a ROI around the predicted position → correct the gain online from observed/predicted (not while touching an edge). Typically 3–8 iterations to within 8 px, then click.
4. **Recover**: if the pointer vanishes, nudge first (defeats auto-hide), then re-park; if a move was sent and the "pointer" did not move twice in a row, it is probably something else—re-park.
5. **Detection (`pointer.py`)**: HSV threshold → connected components → fill the outer contour (the iOS colored pointer is a ring with a translucent center) → judge roundness by fill ratio ≈ π/4 plus perimeter circularity. A half circle cut off by the screen edge still passes; rectangles always score 1.0 and are rejected. The `pointer-sample` command generates the thresholds from a screenshot.

It also runs without hardware: `hil/sim.py` is a simulated phone (receives HID reports, models acceleration, noise, auto-hide, red decoys,
renders frames), and the localization tests in `pytest` run on it.

---

## 4. Architecture and components

```
hil/capture.py         AVFoundation frame grabber (open the iOS capture device, keep the latest frame, pixel-format conversion)
hil/capture_server.py  Frame-grabber HTTP service (127.0.0.1:8765), packaged into ~/Applications/HIL Capture.app
hil/client.py          RemoteGrabber client; opens the app automatically if the service is not running
hil/protocol.py        Mac↔ESP32 command protocol (single source of truth); docs/PROTOCOL.md is generated from it
hil/link.py            Link abstraction: SerialLink / HttpLink (via daemon) / LoopbackLink (stub) / FirmwareModel (Python reference of the firmware)
hil/link_server.py     Link daemon (127.0.0.1:8766): holds the serial or BLE connection; every CLI command is relayed through it
hil/ble_central.py     CoreBluetooth central: scan for HIL-Bridge → subscribe TX → AUTH → synchronous request/reply
hil/pointer.py         Pointer detection (color + circularity)
hil/locate.py          Feedback localization: park / calib / move_to / tap / long_press / drag
hil/loop.py            grab → Policy → act loop (script runner)
hil/sim.py             Simulated phone + simulated grabber (hardware-free tests)
hil/cli.py             python -m hil …  command line (this is what the agent calls)
firmware/hid_bridge/   ESP32 firmware: BLE HID + BLE command service + serial; firmware/prebuilt/esp32/ holds verified binaries
tools/make_app.sh      Builds HIL Capture.app (owns the camera permission)
tools/make_link_app.sh + tools/launcher.c   Builds HIL Link.app (owns the Bluetooth permission; native launcher loads libpython)
skill/iphone-hil/      Claude Code skill: when to use it, how, pacing, safety boundaries
tests/                 Protocol / firmware model / localization loop (simulation)
```

Both `.app`s are **on-demand services that quit after 10 idle minutes**: any `hil` command that finds a service missing runs `open -g` on
it, and capture and Bluetooth are each ready in about 2 s. The capture service counts only frame grabs as activity and the link service only
commands; polling status does not keep them alive. They are tiny (about 30 MB RAM combined, near-zero CPU); the auto-quit mainly saves
the iPhone's battery, because while capture runs the phone is continuously mirroring its display.

The board idles too (firmware 0.3.2+): **after 120 s without an input command it drops the iPhone's HID connection so the phone's
keyboard works again**. iOS hides the on-screen keyboard in every app while any Bluetooth keyboard is connected, so a board that stays
connected around the clock leaves the user unable to type on their own phone.

**Connecting is "software opens listening, you tap by hand" (important).** On iOS the iPhone (central) decides what to connect to; the
board (peripheral) cannot force it, and once the board drops the iPhone, iOS enters a reconnection back-off and **no amount of advertising
brings it back from software** (see `docs/IOS_RECONNECT_HANDOFF.md` for the measurements and the underlying constraint). The only reliable
way is for the board to **advertise as a Bluetooth keyboard** (firmware `appearance=0x3C2`, versus the `appearance=0` it uses normally to
keep macOS from claiming it) so the iPhone sees it in Settings → Bluetooth and **you tap HIL-Bridge by hand** to connect.

So the workflow is: normally the board is in normal mode (`appearance=0`, drops the iPhone on idle to free the keyboard) → to use the tool,
`hil up` (when the iPhone is not connected) or `hil listen` switches it to listening mode and prompts, and you tap HIL-Bridge → once
connected, listening mode does not drop it mid-session → when done, `hil normal` / `hil stop` (or the services quitting after 10 idle
minutes) switches back to normal mode, releases the iPhone, and the on-screen keyboard returns. Change the idle limit over serial with
`CFG hididle <seconds>`; see `firmware/FLASH.md`.

---

## 5. What it does and what it refuses to do

**Target scenarios**: the everyday errands that exist only inside a phone app or mini program, done by an agent on your behalf:

- Food delivery and shopping: find the shop or product you described, add to cart, compare prices, stop before payment for your confirmation.
- Appointments and reports: check schedules and book in a hospital's official account / mini program; open lab reports one by one and tabulate them.
- Utilities, property fees, social insurance: read bills and usage in the payment mini program.
- Government apps: check application progress, read notices, fill in forms that involve no IDs or passwords.
- Anything that starts with "can you look at that app on my phone and…".

**Already done**: in WeChat, from Contacts, locate a hospital's service account via the alphabetical index (avoiding Chinese input), enter
"Outpatient services → Lab reports", open eight reports in turn, scroll, and read out the conclusions.

**Explicitly refused** (written into the skill's safety rules; copy them into your own agent prompts):

- Never enter ID numbers, passwords, payment PINs or verification codes. When a page asks for them, stop and hand over to a human.
- Never pay, send messages or change phone settings unless the user explicitly asked for that exact step.
- Screenshots contain personal data; delete the images under `captures/` when a task is finished.

---

## 6. Hardware

| Item | Requirement | Notes |
|---|---|---|
| Mac | macOS 13+, Apple Silicon or Intel | Developed and verified on Apple Silicon |
| iPhone | iOS 13+ (AssistiveTouch pointer support) | Verified on a 1284×2778 display; other resolutions only need a fresh `pointer-sample` + `calib` |
| Cable | iPhone ↔ Mac, data-capable | For screen capture |
| ESP32 dev board | Classic ESP32 (DevKitC-style, CH340/CP2102 serial bridge); ESP32-S3 also supported | A few dollars |
| USB charger / port | Powers the ESP32 | Does not need to be connected to the Mac |

The ESP32-S3 firmware has an extra "native USB port as wired HID" backend, useful only when the screen is captured some other way
(e.g. AirPlay). Only the classic-ESP32 BLE route has been verified on hardware.

---

## 7. Installation

### 7.1 Mac side

```bash
git clone https://github.com/ghbhiee/iphone-hil.git ~/iphone-hil
cd ~/iphone-hil
uv venv .venv --python 3.12 && uv pip install -r requirements.txt   # or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp hil.example.json hil.json
.venv/bin/python -m pytest -q tests                                  # 17 tests, all in simulation, no hardware needed
```

`tools/make_app.sh` and `tools/make_link_app.sh` run automatically on the first `hil up`, producing `~/Applications/HIL Capture.app`
and `HIL Link.app`. On first launch macOS asks for **Camera** and **Bluetooth** permission respectively; allow both (the camera permission
is what covers iPhone screen capture).

### 7.2 ESP32 firmware

See [firmware/FLASH.md](firmware/FLASH.md). Short version:

```bash
pip install esptool
esptool.py --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write_flash \
  0x1000 firmware/prebuilt/esp32/hid_bridge.ino.bootloader.bin \
  0x8000 firmware/prebuilt/esp32/hid_bridge.ino.partitions.bin \
  0xe000 firmware/prebuilt/esp32/boot_app0.bin \
  0x10000 firmware/prebuilt/esp32/hid_bridge.ino.bin
screen /dev/cu.usbserial-XXXX 115200       # type ST ⏎ and note token=…; exit with Ctrl-A K
```

Put the string after `token=` into `hil.json` as `ble_token`. Later firmware updates flash only the app partition at `0x10000`; never
flash the whole chip again, because that erases the NVS holding the bond with the iPhone.

### 7.3 iPhone side (one-time)

1. Settings → Accessibility → Touch → **AssistiveTouch: on**.
2. Same page → Devices → Bluetooth Devices → pair **HIL-Bridge**.
3. Settings → Accessibility → **Pointer Control**: red or orange color, larger pointer, **Auto-Hide off, Pointer Animations off**.
4. AssistiveTouch → Idle Opacity to the minimum.
5. Settings → General → Keyboard → Hardware Keyboard → layout "U.S.".
6. Settings → Display & Brightness → **Auto-Lock: Never**.
7. Connect the cable to the Mac, unlock, tap "Trust This Computer".

### 7.4 First run

```bash
.venv/bin/python -m hil up                        # launch both services; reports resolution / firmware version / whether the iPhone HID is connected
.venv/bin/python -m hil grab                      # captures/frame.png
.venv/bin/python -m hil move 80 80                # the pointer should move
.venv/bin/python -m hil pointer-sample captures/frame.png X Y   # X Y = pointer center in the (half-size) screenshot
.venv/bin/python -m hil find --debug dbg.png      # confirm exactly one pointer is detected
.venv/bin/python -m hil park && .venv/bin/python -m hil calib   # calibrate; written to hil.json
.venv/bin/python -m hil tap 300 520               # tap an icon
```

---

## 8. Usage

Every step is a standalone command and all state lives in the two daemons and `hil.json`, so any agent that can run a shell can be the brain:

```bash
python -m hil grab                 # screenshot to captures/frame.png (0.5 scale by default) for the model to look at
python -m hil tap X Y              # coordinates in the scaled image; converges, then clicks
python -m hil long X Y 800         # long press
python -m hil drag X0 Y0 X1 Y1     # drag (has momentum; good for "back to top", use wheel for fine paging)
python -m hil wheel 6              # scroll down about 8 rows
python -m hil type "ascii"         # ASCII text (make sure the English keyboard is active first)
python -m hil key GUI+H            # key combos; GUI+SPACE opens Spotlight
python -m hil home                 # Home
python -m hil goto X Y             # move the pointer without clicking (so wheel events land on a list)
python -m hil status / up / stop   # both services + firmware state / start / stop now
python -m hil run script.txt       # one action per line through the Runner (frame + log.jsonl per step)
python -m hil step                 # interactive: a human is the brain
```

Global options: `--scale` (screenshot scale, default `brain_scale` 0.5 in `hil.json`), `--link serial|direct|loop|echo`,
`--capture remote|sim`, `-v`. Without hardware: `python -m hil --capture sim --link loop sim-demo` shows the localization loop on the
simulated phone.

**Pacing**: wait 1.5 s after a tap before grabbing; 3–4 s for H5 pages to load; wheel events land on the element under the pointer, so
`goto` the middle of a list if it won't scroll; picking up the phone rotates it and changes the frame size, invalidating coordinates from
the previous frame.

---

## 9. The Claude Code skill

`skill/iphone-hil/SKILL.md` packages all of the above as a Claude Code skill: preconditions, `hil up`, the work loop, pacing, known
limits, safety rules. Install:

```bash
ln -s ~/iphone-hil/skill/iphone-hil ~/.claude/skills/iphone-hil   # edit the path inside SKILL.md if you cloned elsewhere
```

Then open a new session and just talk:

- "Take a look at my phone."
- "Open WeChat, find hospital X's official account and read out all my recent reports."
- "On Meituan, find a congee place rated 4.8 or above, add one bowl of century-egg pork congee to the cart and stop before payment."
- "Open the State Grid mini program and check this month's electricity bill."

Claude will run `hil up`, screenshot, tap, screenshot again, and stop to ask you wherever a password, a payment or a message is involved.
The services quit on their own after 10 idle minutes.

> If your Mac does **not** have this project and you only want Claude to build a simplified "I browse my phone myself, it captures
> frames in the background and reads them" tool, [docs/PROMPT_从零做后台截帧读报告.md](docs/PROMPT_从零做后台截帧读报告.md) is a
> requirement description (in Chinese) you can paste into a fresh session.

---

## 10. Safety and privacy

- **Where the frames go.** Frames stay in `captures/` on your Mac. But an agent "looking at a screenshot" means the image is sent to whatever model service you use (for example Claude's API)—the same as pasting the screenshot into a chat. Whatever is on the screen, the model sees; delete `captures/*.png` when done.
- **What the capture channel implies.** While the iPhone is cabled to the Mac, trusts it and is unlocked, any Mac program with camera permission can see the phone's screen, with no indication on the phone. Unplug when finished, or revoke trust via "Reset Location & Privacy" on the iPhone.
- **The Bluetooth command channel.** The ESP32's command service is unpaired and unencrypted, protected only by the `AUTH` token, which is readable over serial. Its threat model is "don't let a neighbor's device connect by accident", not a determined attacker. Don't leave the ESP32 powered in an environment you don't trust.
- **The agent's behavioral boundary** is written into the skill: no IDs / passwords / payment details, no paying, messaging or changing settings unless the user explicitly asked. Copy those rules when using your own agent.
- **The risk-control view.** To the apps this is a Bluetooth mouse plus a person watching the screen. Whether "operating my account through an agent" fits a given platform's terms of service is your call; this project is for your own devices and your own accounts.

---

## 11. Known limitations and to-do

- **Chinese input goes through Universal Clipboard: slow but workable.** A Bluetooth keyboard only sends ASCII; for anything else use `paste`: Mac `pbcopy` → Universal Clipboard → `⌘V` on the iPhone, with Handoff enabled on both devices. Measured behaviour: the iPhone only fetches the Mac's clipboard when it receives `⌘V`, and only if it has already seen the Mac's Bluetooth advertisement. Pasting 2 s after the copy often does nothing at all; 4–5 s is reliable; a copy on the Mac expires after 2 minutes. So `paste` always copies fresh, waits 5 s by default, and the caller must confirm with a screenshot, re-sending `⌘V` once after 3 s if nothing appeared. Prefer lists, index bars and menus over typing whenever possible.
- **Negative wheel direction has no effect** (cannot scroll up; cause unknown). Use `drag` or arrow keys to go up.
- The Chinese Pinyin keyboard swallows letters into pinyin composition and treats digits as candidate selectors; switch to the English keyboard before `type` (`key CTRL+SPACE` cycles; confirm with a screenshot).
- `⌘+Space` occasionally does not open Spotlight on the first press.
- A tap takes 1–4 s. The bottleneck is waiting for the pointer and display to settle, not Bluetooth; more aggressive gain prediction could bring it under 1 s but hasn't been done.
- Verified on one iPhone and one classic ESP32. After changing phones, redo `pointer-sample` + `calib`; the ESP32-S3 firmware compiles but is untested on hardware.
- No systematic test of whether app risk engines react to a Bluetooth mouse; dozens of WeChat operations so far produced no warnings.

---

## 12. Pitfalls (for people who want to change the code)

- **An uppercase letter in a chord used to mean Shift+letter.** `KP GUI+V` went out as ⌘⇧V (paste and match style), which does nothing in the Spotlight field, and we spent a while blaming the clipboard. The Mac-side `enc_key` now lowercases letters inside chords, and firmware 0.3.0 no longer implies Shift there either (write `SHIFT+` explicitly).
- **Camera and Bluetooth permissions are granted per app.** Claude Code's host process is hardened-runtime with no camera entitlement; a child process asking for camera access is silently refused by TCC (no prompt, state stuck at notDetermined). Hence the capture service is packaged as `HIL Capture.app`. CoreBluetooth is stricter still: **the executable itself** must live in a bundle with `NSBluetoothAlwaysUsageDescription`, and a shell wrapper that `exec`s the venv's python is SIGABRTed outright (exit code 134). So `HIL Link.app`'s executable is a native launcher compiled from `tools/launcher.c` that `dlopen`s libpython and runs `hil.link_server` in-process.
- **The iOS capture device reports no formats** (0x0 @ 0 fps); don't set `activeFormat`, read the resolution from the first frame. On macOS the session resets the device format to the preset's 1080p before `startRunning`; the fix is the same as ffmpeg's: set the format after `lockForConfiguration` and **keep the lock** until after `startRunning`.
- **`CMSampleBuffer` in pyobjc callbacks is a raw pointer** (PyObjCPointer), not bridged to a CF object; all frame handling goes through ctypes into CoreMedia / CoreVideo. A Python exception inside a callback becomes an NSException that aborts the process, so every delegate method is wrapped in `_safe`.
- **The main thread must pump an NSRunLoop**, or AVFoundation's device hot-plug notifications are never delivered and a resident service never sees an iPhone plugged in later.
- **CH340 boards reset every time the serial port is opened**, so the daemon opens it once and the CLI relays over HTTP.
- **BLE reports queue up in the stack.** The firmware splits large moves into ±127 reports; the gap between reports must be ≥ the connection interval (25 ms measured), otherwise dozens of reports pile up, every frame shows a stale position and calibration produces garbage.
- **macOS auto-claims BLE devices with a HID appearance.** As soon as the Mac connects to a peripheral advertising HID, bluetoothd initiates pairing on its own and registers it as a mouse. Countermeasures: advertise a generic appearance, advertise the custom service UUID instead of the HID UUID, and lock the HID target to one address in firmware.
- **A full-chip flash erases NVS**, which holds the iPhone bond keys and configuration; upgrades flash only the app partition.
- **Bluetooth turns off and on when the Mac sleeps**; the connection is dead but the object survives. The central marks itself disconnected as soon as the state leaves PoweredOn and the main loop reconnects.
- **The iOS pointer is a ring, not a disk**: sample saturated pixels on the ring and fill the outer contour before judging roundness.
- More detail, the timeline and measured numbers are in [HANDOFF.md](HANDOFF.md) (Chinese).

---

## 13. Repository layout, tests, protocol

```
.
├── hil/                 Mac-side Python package (capture, link, localization, CLI)
├── firmware/
│   ├── hid_bridge/      ESP32 firmware source (Arduino / NimBLE)
│   ├── prebuilt/esp32/  binaries verified on hardware
│   ├── build.sh         arduino-cli build/upload script (esp32 | ble | usb)
│   └── FLASH.md         flashing and iPhone pairing
├── tools/               generators for the two .app shells and the native launcher
├── skill/iphone-hil/    Claude Code skill
├── docs/PROTOCOL.md     Mac↔ESP32 command protocol (generated from hil/protocol.py)
├── tests/               pytest (simulation, no hardware)
├── hil.example.json     config template (copy to hil.json; includes one real device's calibration for reference)
├── requirements.txt
└── HANDOFF.md           engineering log: decisions, pitfalls, measurements
```

```bash
.venv/bin/python -m pytest -q tests
```

License: MIT.
