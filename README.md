# newscan

A terminal-based tool to discover, connect to, and communicate with [Meshtastic](https://meshtastic.org) mesh nodes via Bluetooth Low Energy (BLE).

> **Platform:** Primarily developed on **Linux**. macOS works with limitations (see below). Windows is not supported.

---

## Features

- Startup requirements check (platform, Python, venv, Bluetooth)
- Smart device discovery — checks paired devices via `bluetoothctl` (Linux), falls back to full BLE scan
- Fast BLE connection with animated spinner
- **Firmware update check** — compares connected node firmware against latest GitHub release in the background; result shown in the header row as `✅ up to date` or `🔴 <new version> available`
- **Favorite nodes list** — last seen, SNR, hop count; ping results shown with green/orange status dots; supplemented by `extra_favorites.json`
- **Ping Favorites** — sends a NodeInfo request to each favorite (5 s apart), shows who responded
- **Node details** — ID, short name, hardware, last seen, SNR, RSSI, hops, GPS, battery, voltage, telemetry
- **Send message** — single or repeated at a configurable interval; after each send the **Outbound View** opens automatically (see below)
- **Outbound View** — live routing-journey screen shown after every DM: last-known route visualisation (or hop-count if not yet traced), relay echo detection, ACK return path with SNR/RSSI/hop count and inferred next-hop node; updates every second until Enter is pressed
- **Tracer** — single or repeated traceroute with live progress bar; full route + per-hop SNR logged; result cached so Outbound View can display it for subsequent messages
- **Inflow View** — live session dashboard grouped by relay/via node: packet counts per type, avg SNR and RSSI of the last-hop link, time since last packet
- **Config export** — `localConfig`, `moduleConfig`, and channels to a timestamped JSON file
- **Live log footer** — last 8 lines of `newscan.log` always visible at the bottom of the terminal
- All packets logged with directional arrows and color: `◀◀` incoming (bright) · `▶▶` outgoing (dim); packet-type symbols: `✉` text · `⊕` position · `◉` nodeinfo · `⊡⊛` telemetry · `⬡` neighbors · `⇌` traceroute · `⌁` ping

---

## Prerequisites

### Linux

```bash
sudo apt install bluez
sudo usermod -aG bluetooth $USER   # log out and back in
```

### macOS

- Python 3.10+ (via Homebrew: `brew install python`)
- macOS 12+
- Grant Bluetooth permission to your terminal app: `System Settings → Privacy & Security → Bluetooth`
- Paired-device lookup is skipped (no `bluetoothctl`); a full BLE scan always runs at startup

### Python packages

```bash
python3 -m venv meshtastic_venv
source meshtastic_venv/bin/activate
pip install meshtastic bleak requests
```

If `meshtastic_venv/` is already present, running `python3 main.py` outside the venv will offer to restart inside it automatically.

---

## Usage

```bash
python main.py
```

### Startup flow

1. Requirements check (platform / Python / venv / Bluetooth)
2. Known paired devices listed (Linux); full BLE scan if none found
3. Select device → spinner while connecting
4. Main view appears; firmware check runs in background and updates the header

### Main view

```
================================================================
MyNode  |  !04332f58  |  hw HELTEC_V3  |  ✅ 2.5.x
================================================================

Favorite peers: 3 of 18 visible

  [1] ● Alice        2m ago       SNR: -5.0   direct
  [2] ● Bob          15m ago      SNR: -8.5   2 hops
  [3] ● Charlie      1h ago       SNR: N/A    N/A

  d<n> Node Details       m<n> Message          t<n> tracer         i   Inflow View
  pf  Ping Favorites      r<n> Repeat msg        rt<n> Repeat trace  e Export config
```

After `pf`, each node gets a green `●` (responded) or orange `●` (no response).

### Extra favorites

Nodes can be added to the favorites list independently of what the radio has marked as favorite. Create or edit `extra_favorites.json` in the project directory:

```json
[
  {"id": "!aabbccdd", "name": "Base Station", "short": "BS"},
  {"id": "!11223344", "name": "Remote Repeater"}
]
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Node ID as hex string, e.g. `!aabbccdd` |
| `name` | yes | Display name (used when node is not visible in the mesh) |
| `short` | no | Short name (defaults to first 4 characters of `name`) |

**Behaviour:**
- If the node is currently visible in the mesh, live radio data is used (name comes from the radio).
- If the node is not visible, it appears with the name from the file and `N/A` for SNR / hops / last seen.
- If the node is already marked as a favorite on the radio, the file entry is ignored (no duplication).
- The file is created automatically with placeholder examples on first run if it does not exist.

### Commands

| Command | Action |
|---|---|
| `d<n>` | Full node details |
| `m<n>` | Send a message; opens Outbound View (ACK, route, signal) |
| `r<n>` | Repeat message at a chosen interval |
| `t<n>` | Single traceroute with progress bar |
| `rt<n>` | Repeated traceroute at a chosen interval |
| `pf` | Ping all favorites via NodeInfo request |
| `i` | Inflow View — live relay traffic dashboard |
| `e` | Export node config to JSON |
| Enter | Quit (asks for confirmation) |

---

## Logging

Appended to `newscan.log` in the project directory (excluded from git).

```
2026-03-29 14:23:09  INFO      ◀◀ ✉ CH0 Short Slow  Alice -> MyNode: 'Hello'  [snr=5dB, rssi=-82dBm]
2026-03-29 14:23:09  INFO      ▶▶ CH0 Short Slow  Message sent to Bob: 'Hi'  ✉
2026-03-29 14:24:00  INFO      ◀◀ ⇌ CH0 Short Slow  Bob -> Charlie  [snr=2.0dB]
2026-03-29 14:25:10  INFO      ▶▶ ⌁ ping (nodeinfo request) sent to Alice (!aabbccdd)
2026-03-29 14:25:11  INFO      ◀◀ ⌁ ping response from Alice (!aabbccdd)
2026-03-29 14:25:15  WARNING   ◀◀ NAK from Bob (!deadbeef): NO_RESPONSE
```

`◀◀` lines render in bright white (incoming); `▶▶` lines render in dim (outgoing).

Relay nodes are resolved to full names where known (`via Alice` instead of `via !..07`). Own node packets are not logged.

---

## Project structure

```
newscan/
├── main.py
├── firmware_check.py                    # firmware version checker (GitHub API)
├── extra_favorites.json                 # auto-created on first run; edit to add extra favorites
├── newscan.log                          # auto-created
├── <NodeName>_<timestamp>_config.json  # auto-created on export
└── README.md
```

---

## Known limitations

- Paired-device fast-lookup requires `bluetoothctl` (Linux only)
- Repeat send / repeat tracer run until manually stopped; no max-count option
- Config export covers `localConfig`, `moduleConfig`, and channels only
