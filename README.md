# newscan

A terminal-based tool to discover, connect to, and communicate with [Meshtastic](https://meshtastic.org) mesh network nodes via Bluetooth Low Energy (BLE).

> **Platform notice:** This tool is primarily developed and tested on **Linux**. macOS should work for the core BLE functionality but has some limitations (see [macOS notes](#macos-notes)). Windows is not supported.

---

## Features

- **Startup requirements check** — verifies platform, Python version, virtual environment, and Bluetooth state before doing anything; offers to activate the included `meshtastic_venv` automatically
- **Smart device discovery** — checks for already-paired Meshtastic devices instantly via `bluetoothctl`, skipping a full BLE scan when possible
- **BLE scan with progress bar** — performs a 10-second BLE scan with a live progress bar when no paired device is found
- **Fast connection** — connects using `find_device_by_address` instead of a full rescan, cutting connection time significantly
- **Connection spinner** — animated spinner with elapsed time while the BLE + mesh handshake completes
- **Favorite nodes list** — shows only nodes marked as favorites on the connected device, with last-seen time, SNR and hop count
- **Node details** — drill into any favorite node for full info: ID, short name, last seen, SNR, hops away, GPS position, battery level and voltage
- **Send a message** — send a single text message to any favorite node through the mesh; each message is automatically prefixed with a `[HH:MM:SS]` timestamp
- **Repeat send** — send a message repeatedly at a configurable interval (default 10 s); press Enter to stop
- **Navigate freely** — after viewing details or sending a message, the favorite list is reprinted and you can pick another node or quit
- **Activity log** — all major events and exceptions are written to `newscan.log` with full timestamps

---

## Prerequisites

### Linux

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| `bluetoothctl` | Part of the `bluez` package — used for instant paired-device lookup |
| Bluetooth adapter | BLE (Bluetooth 4.0+) support required |

Install BlueZ if missing:
```bash
sudo apt install bluez
```

Your user must have permission to use Bluetooth. Add yourself to the `bluetooth` group if needed:
```bash
sudo usermod -aG bluetooth $USER
# log out and back in for the change to take effect
```

### macOS

| Requirement | Notes |
|---|---|
| Python 3.10+ | Install via [Homebrew](https://brew.sh) or [python.org](https://python.org) |
| macOS 12 (Monterey)+ | Recommended; older versions may work but are untested |
| Built-in or USB Bluetooth adapter | BLE (Bluetooth 4.0+) support required |

> **macOS note:** `bluetoothctl` is not available on macOS, so the instant paired-device lookup is skipped and a full 10-second BLE scan always runs at startup. Everything else works the same.

**Bluetooth permission:** On first run macOS will ask for permission to use Bluetooth. You must grant access to Terminal (or whichever app you use) in:
`System Settings → Privacy & Security → Bluetooth`

If the scan finds nothing, check that Bluetooth is enabled and that the terminal app has Bluetooth permission.

**Install Python via Homebrew (recommended):**
```bash
brew install python
```

### Python packages (all platforms)

```
meshtastic >= 2.7
bleak >= 3.0
```

Install into a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate        # Linux / macOS
pip install meshtastic bleak
```

Or use the included `meshtastic_venv` if it is already set up:
```bash
source meshtastic_venv/bin/activate
```

> **Tip:** If you run `python3 main.py` without an active virtual environment and `meshtastic_venv/` exists in the project directory, the startup check will detect this and offer to re-launch automatically inside the venv — no manual activation needed.

---

## Usage

```bash
python main.py
```

### Startup flow

1. **Requirements check** — platform, Python version, virtual environment, and Bluetooth are verified. Issues are reported with `[WARN]` / `[FAIL]` labels. If the included `meshtastic_venv/` is found but not active, you are prompted to restart inside it automatically.
2. Screen is cleared and a reminder is shown to disconnect phones/nodes so they can be scanned.
3. The system checks for already-paired Meshtastic devices via `bluetoothctl` — no scan needed if one is found.
4. If no paired device is found, a 10-second BLE scan runs with a progress bar.
5. You are offered the option to run an additional scan even if a paired device was found.
6. Select the device to connect to from the numbered list.
7. A spinner shows progress while the BLE and mesh connection is established.

Example requirements check output:
```
Checking requirements...
  [ OK ] Platform : Linux
  [ OK ] Python   : 3.12.3
  [ OK ] Venv     : meshtastic_venv
  [ OK ] Bluetooth: adapter powered on
```

### Main menu

After connecting, local node info is displayed followed by a list of **favorite peers**:

```
==================================================
LOCAL NODE
==================================================
Node number : 123456789
Firmware    : 2.5.x
Hardware    : HELTEC_V3

Favorite peers: 3 of 18 visible

  [1] Alice        2m ago       SNR: -5.0   direct
  [2] Bob          15m ago      SNR: -8.5   2 hops
  [3] Charlie      1h ago       SNR: N/A    N/A

  d<n> Details   m<n> Message   r<n> Repeat msg   Enter to quit
  >
```

| Command | Action |
|---|---|
| `d<n>` | Show full details for node n (ID, position, battery, …) |
| `m<n>` | Send a single message to node n |
| `r<n>` | Send a message to node n repeatedly at a chosen interval |
| Enter | Quit and disconnect |

### Repeat send

```
  > r2
  Repeated message to Bob
  Message: Ping!
  Interval in seconds [10]: 5
  Sent #4 to Bob. Press Enter to stop.
```

Press **Enter** at any time to stop the loop and return to the menu.

---

## Project structure

```
newscan/
├── main.py           # Single-file application
├── newscan.log       # Activity log (auto-created, excluded from git)
└── README.md
```

## Logging

Every run appends to `newscan.log` in the project directory. The file is created automatically and is excluded from version control via `.gitignore`.

Each line contains a timestamp, severity level, and message:

```
2026-03-29 14:23:01  INFO      === newscan starting ===
2026-03-29 14:23:01  INFO      Preflight: platform=Linux
2026-03-29 14:23:01  INFO      Preflight: Python 3.12.3
2026-03-29 14:23:01  INFO      Preflight: venv=meshtastic_venv
2026-03-29 14:23:01  INFO      Bluetooth preflight: adapter powered on
2026-03-29 14:23:02  INFO      Found known device: Meshtastic [AA:BB:CC:DD:EE:FF]
2026-03-29 14:23:04  INFO      Connecting to Meshtastic [AA:BB:CC:DD:EE:FF]
2026-03-29 14:23:07  INFO      Connected to Meshtastic [AA:BB:CC:DD:EE:FF]
2026-03-29 14:23:09  INFO      Message sent to Alice (123456): 'Hello'
2026-03-29 14:25:00  INFO      === newscan session ended ===
```

Exceptions are logged at `ERROR` level and include the full stack trace.

---

## Meshtastic device setup

Before using this tool, make sure your Meshtastic device has:

- **BLE enabled** (Settings → Bluetooth on the device or via the Meshtastic app)
- **Favorite nodes set** — nodes you want to interact with should be starred/favorited in the Meshtastic app so they appear in the favorites list
- The device should be **paired** with your computer for the fastest startup experience:
  - **Linux:** use `bluetoothctl pair <address>` or the system Bluetooth manager
  - **macOS:** use System Settings → Bluetooth to pair the device

---

## macOS notes

- Paired-device lookup (`bluetoothctl`) is not available — a full 10-second BLE scan always runs at startup
- CoreBluetooth (used by `bleak` on macOS) may take slightly longer to establish a GATT connection than BlueZ on Linux
- Grant Bluetooth permission to your terminal app before first run (`System Settings → Privacy & Security → Bluetooth`)
- If you see `bleak.exc.BleakError: device not found`, make sure the Meshtastic device is powered on, BLE is enabled on it, and it is not already connected to another device (phone, tablet, etc.)

---

## Known limitations

- Receiving incoming messages is not yet implemented
- Repeat send runs until manually stopped; no maximum count option yet
- Paired-device fast-lookup requires `bluetoothctl` (Linux only); macOS always falls back to a full scan
