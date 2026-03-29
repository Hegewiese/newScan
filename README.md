# newscan

A terminal-based tool to discover, connect to, and communicate with [Meshtastic](https://meshtastic.org) mesh network nodes via Bluetooth Low Energy (BLE).

> **Platform notice:** This tool is primarily developed and tested on **Linux**. macOS should work for the core BLE functionality but has some limitations (see [macOS notes](#macos-notes)). Windows is not supported.

---

## Features

- **Startup requirements check** — verifies platform, Python version, virtual environment, and Bluetooth state before doing anything; offers to activate the included `meshtastic_venv` automatically
- **Smart device discovery** — checks for already-paired Meshtastic devices instantly via `bluetoothctl`, skipping a full BLE scan when possible
- **BLE scan with progress bar** — performs a 10-second BLE scan with a live progress bar when no paired device is found
- **Fast connection** — connects using `find_device_by_address` instead of a full rescan, cutting connection time significantly
- **Connection spinner** — animated spinner with elapsed time while the BLE + mesh connection completes
- **Node header** — after connecting the screen clears and shows a single-line header with the node's name, hex ID, firmware version, and hardware model
- **Favorite nodes list** — shows only nodes marked as favorites on the connected device, with last-seen time, SNR, and hop count
- **Node details** — drill into any favorite node for full info: ID, short name, last seen, SNR, hops away, GPS position, battery level and voltage
- **Send a message** — send a single text message to any favorite node through the mesh; each message is automatically prefixed with a `[HH:MM:SS]` timestamp; acknowledgement (ACK/NAK) is shown on screen and written to the log when the mesh responds
- **Repeat send** — send a message repeatedly at a configurable interval (default 10 s); each transmission reports ACK/NAK; press Enter to stop
- **Tracer** — send a single traceroute to any favorite node with a configurable hop limit and a live progress bar; full route details (hops, per-hop SNR, rxSNR, rxRSSI) are written to the log
- **Repeat tracer** — send traceroutes repeatedly at a configurable interval; full route details logged for each run; press Enter to stop
- **Config export** — export the connected node's `localConfig`, `moduleConfig`, and channel settings to a timestamped JSON file
- **Clear screen on every action** — each menu action starts on a clean screen; returning from any action restores the full main view automatically
- **Live log footer** — the bottom 9 rows of the terminal are permanently reserved for a `── LOGs ──` divider and the last 8 lines of `newscan.log`, updated in real time via `tail -f`; newest entry appears at the top of the footer
- **Activity log** — all major events (messages sent/received, ACK/NAK, tracer routes and metrics, position updates, node info, telemetry, neighbor info, config exports, errors) are written to `newscan.log` with full timestamps and per-type symbols

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

### Main view

After connecting the screen clears and the node header is shown on one line, followed by the favorite peers list and a grouped command menu:

```
================================================================
MyNode Base  |  !04332f58  |  fw 2.5.x  |  hw HELTEC_V3
================================================================

Favorite peers: 3 of 18 visible

  [1] Alice        2m ago       SNR: -5.0   direct
  [2] Bob          15m ago      SNR: -8.5   2 hops
  [3] Charlie      1h ago       SNR: N/A    N/A

  d<n> Details          m<n> Message          t<n> tracer         Enter to quit
                        r<n> Repeat msg        rt<n> Repeat trace  e Export config

── LOGs ─────────────────────────────────────────────────────────
2026-03-29 12:09:17  INFO      tracer [Alice] towards:  !04332f58 --> !aabbccdd(2.25dB) --> !deadbeef(3.50dB)
2026-03-29 12:09:17  INFO      tracer [Alice] back:     !deadbeef --> !aabbccdd(1.75dB) --> !04332f58(4.00dB)
2026-03-29 12:09:17  INFO      tracer [Alice] metrics:  pktId=12345678, rxSNR=3.5dB, rxRSSI=-87dBm, hopStart=3
2026-03-29 12:09:22  INFO      Message sent to Bob (!deadbeef): 'Hello'
2026-03-29 12:09:23  INFO      ACK received from Bob (!deadbeef)
```

The bottom 9 rows — the `── LOGs ──` divider and the last 8 log lines — are always visible regardless of what the rest of the screen is doing. They update automatically as new entries are written to `newscan.log`.

### Menu commands

| Command | Action |
|---|---|
| `d<n>` | Show full details for node n (ID, position, battery, …) |
| `m<n>` | Send a single message to node n; ACK/NAK reported when received |
| `r<n>` | Send a message to node n repeatedly at a chosen interval; ACK/NAK per send |
| `t<n>` | Send a single tracer to node n; full route logged |
| `rt<n>` | Send tracers to node n repeatedly at a chosen interval; full route logged each time |
| `e` | Export the connected node's config to a JSON file |
| Enter | Quit and disconnect |

Every action clears the screen before running and restores the full main view when it returns.

### Messaging and acknowledgements

```
  > m2
  Sending message to Bob
  Message: Hello there
  Sent. (Waiting for ACK...)

  ACK received from Bob.
```

For repeated sends each transmission shows its sequence number and ACK/NAK as they arrive:

```
  Sent #3 to Bob. Press Enter to stop.
  ACK #2 from Bob.
```

### Tracer

```
  > t1
  Traceroute to Alice
  Hop limit [3]: 3

  Waiting for route... [########################################]  4.2s
  Success.
```

The library prints the hop-by-hop route to the screen. Full details are also written to the log:

```
tracer [Alice] towards:  !04332f58 --> !aabbccdd(2.25dB) --> !deadbeef(3.50dB)
tracer [Alice] back:     !deadbeef --> !aabbccdd(1.75dB) --> !04332f58(4.00dB)
tracer [Alice] metrics:  pktId=12345678, rxSNR=3.5dB, rxRSSI=-87dBm, hopStart=3
```

Unknown SNR values are shown as `?`. The back route is only logged when the remote node returns it.

### Repeat tracer

```
  > rt1
  Repeated traceroute to Alice
  Hop limit [3]: 3
  Interval in seconds [30]: 60

  --- tracer #1 to Alice ---
  Waiting for route... [########################################]  4.2s
  Next in 60s — press Enter to stop.
```

Press **Enter** at any time to stop and return to the main view.

### Config export

Press `e` at the main menu to export the connected node's full configuration to a JSON file in the project directory:

```
  > e
  Config exported: MyNode_20260329_143022_config.json
```

The file contains three top-level keys: `localConfig`, `moduleConfig`, and `channels`. Useful for backup, diffing settings between nodes, or restoring a configuration.

---

## Project structure

```
newscan/
├── main.py                              # Single-file application
├── newscan.log                          # Activity log (auto-created, excluded from git)
├── <NodeName>_<timestamp>_config.json  # Exported node configs (auto-created, excluded from git)
└── README.md
```

## Logging

Every run appends to `newscan.log` in the project directory. The file is created automatically and is excluded from version control via `.gitignore`.

Each line contains a timestamp, severity level, and message:

```
2026-03-29 14:23:01  INFO      === newscan starting ===
2026-03-29 14:23:01  INFO      Preflight: platform=Linux
2026-03-29 14:23:04  INFO      Connecting to Meshtastic [AA:BB:CC:DD:EE:FF]
2026-03-29 14:23:07  INFO      Connected to Meshtastic [AA:BB:CC:DD:EE:FF]
2026-03-29 14:23:09  INFO      Message sent to Alice (!04332f58): 'Hello'
2026-03-29 14:23:10  INFO      ACK received from Alice (!04332f58)
2026-03-29 14:24:00  INFO      tracer [Alice] towards:  !04332f58 --> !aabbccdd(2.25dB) --> !deadbeef(3.50dB)
2026-03-29 14:24:00  INFO      tracer [Alice] back:     !deadbeef --> !aabbccdd(1.75dB) --> !04332f58(4.00dB)
2026-03-29 14:24:00  INFO      tracer [Alice] metrics:  pktId=12345678, rxSNR=3.5dB, rxRSSI=-87dBm, hopStart=3
2026-03-29 14:25:00  INFO      === newscan session ended ===
```

Exceptions are logged at `ERROR` level and include the full stack trace.

The last 8 log lines are also displayed live at the bottom of the terminal, below the `── LOGs ──` divider (newest first), so you can monitor activity without leaving the main view.

### Received packet types

All incoming packets are logged automatically with a distinctive symbol, the sender name, the last relay node (where identifiable), and signal quality:

| Symbol | Type | Example log line |
|---|---|---|
| `✉` | Text — broadcast | `✉ CH0 Short Slow  Alice via Relay1 -> broadcast: 'Hallo!'  [snr=3.5dB]` |
| `✉` (red) | Text — direct | `✉ CH0 Short Slow  Alice -> MyNode: 'Direct msg'  [snr=5dB]` |
| `⊕` | Position update | `⊕ CH0 Short Slow  Alice via Relay1: lat=48.12345  lon=11.54321  alt=520m` |
| `◉` | Node info | `◉ CH0 Short Slow  Alice: long=Alice Base  short=ALCE  hw=HELTEC_V3` |
| `⊡` | Telemetry (device) | `⊡ CH0 Short Slow  Alice: bat=87%  volt=3.92V  chUtil=4.2%  up=3600s` |
| `⊛` | Telemetry (env) | `⊛ CH0 Short Slow  Alice: temp=21.5°C  hum=62.0%  pres=1013.2hPa` |
| `⬡` | Neighbor info | `⬡ CH0 Short Slow  Alice via Relay1: 3 neighbors: Bob, Charlie, Dave` |
| `⇌` | Traceroute | `⇌ CH0 Short Slow  Bob -> Charlie  [snr=2.0dB]` |

The relay node (`via …`) is resolved from the last 8 bits of the `relayNode` field matched against the known node database.

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

- Repeat send and repeat tracer run until manually stopped; no maximum count option yet
- Paired-device fast-lookup requires `bluetoothctl` (Linux only); macOS always falls back to a full scan
- Config export covers `localConfig`, `moduleConfig`, and channels only; node database and PKI keys are not included
