# newscan

A terminal-based tool to discover, connect to, and communicate with [Meshtastic](https://meshtastic.org) mesh nodes via Bluetooth Low Energy (BLE).

> **Platform:** Primarily developed on **Linux**. macOS works with limitations (see below). Windows is not supported.

---

## Features

- Startup requirements check (platform, Python, venv, Bluetooth)
- Smart device discovery — checks paired devices via `bluetoothctl` (Linux), falls back to full BLE scan
- Fast BLE connection with animated spinner
- **Favorite nodes list** — last seen, SNR, hop count; ping results shown with green/orange status dots
- **Ping Favorites** — sends a NodeInfo request to each favorite (5 s apart), shows who responded
- **Node details** — ID, short name, hardware, last seen, SNR, RSSI, hops, GPS, battery, voltage, telemetry
- **Send message** — single or repeated at a configurable interval; ACK/NAK reported per send
- **Tracer** — single or repeated traceroute with live progress bar; full route + per-hop SNR logged
- **Config export** — `localConfig`, `moduleConfig`, and channels to a timestamped JSON file
- **Live log footer** — last 8 lines of `newscan.log` always visible at the bottom of the terminal
- All received packets logged with symbols: `✉` text · `⊕` position · `◉` nodeinfo · `⊡⊛` telemetry · `⬡` neighbors · `⇌` traceroute · `⌁` ping

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
pip install meshtastic bleak
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
4. Main view appears

### Main view

```
================================================================
MyNode  |  !04332f58  |  fw 2.5.x  |  hw HELTEC_V3
================================================================

Favorite peers: 3 of 18 visible

  [1] ● Alice        2m ago       SNR: -5.0   direct
  [2] ● Bob          15m ago      SNR: -8.5   2 hops
  [3] ● Charlie      1h ago       SNR: N/A    N/A

  d<n> Node Details       m<n> Message          t<n> tracer         Enter to quit
  pf  Ping Favorites      r<n> Repeat msg        rt<n> Repeat trace  e Export config
```

After `pf`, each node gets a green `●` (responded) or orange `●` (no response).

### Commands

| Command | Action |
|---|---|
| `d<n>` | Full node details |
| `m<n>` | Send a message; ACK/NAK reported |
| `r<n>` | Repeat message at a chosen interval |
| `t<n>` | Single traceroute with progress bar |
| `rt<n>` | Repeated traceroute at a chosen interval |
| `pf` | Ping all favorites via NodeInfo request |
| `e` | Export node config to JSON |
| Enter | Quit (asks for confirmation) |

---

## Logging

Appended to `newscan.log` in the project directory (excluded from git).

```
2026-03-29 14:23:09  INFO      ✉ CH0 Short Slow  Alice -> MyNode: 'Hello'  [snr=5dB, rssi=-82dBm]
2026-03-29 14:24:00  INFO      ⇌ CH0 Short Slow  Bob -> Charlie  [snr=2.0dB]
2026-03-29 14:25:10  INFO      ⌁ ping ACK from Alice (!aabbccdd)
2026-03-29 14:25:15  WARNING   ⌁ ping NAK from Bob (!deadbeef): NO_RESPONSE
```

Relay nodes are resolved to full names where known (`via Alice` instead of `via !..07`). Own node packets are not logged.

---

## Project structure

```
newscan/
├── main.py
├── newscan.log                          # auto-created
├── <NodeName>_<timestamp>_config.json  # auto-created on export
└── README.md
```

---

## Known limitations

- Paired-device fast-lookup requires `bluetoothctl` (Linux only)
- Repeat send / repeat tracer run until manually stopped; no max-count option
- Config export covers `localConfig`, `moduleConfig`, and channels only
