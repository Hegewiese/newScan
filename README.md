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
==============================================================================
MyNode  |  !04332f58  |  hw HELTEC_V3  |  bat ▰▰▰▰▱ 82%  |  ✅ 2.5.x
==============================================================================

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
| `l` | Toggle log fullscreen (press `l` again to return) |
| `e` | Export node config to JSON |
| Enter | Quit (asks for confirmation) |

### Inflow View (`i`)

Live session dashboard showing which relay nodes forwarded packets to your radio, grouped by the last-hop relayer (`relayNode` field). Refreshes every second.

```
  Inflow View  —  58 packets from 3 nodes  (session 6m 14s)
  ────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Direct Connected        Hops Out    Dist    last  Pkts  ████████████████████  txt pos usr tel  nb  tr    SNR   RSSI
  ────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Morpheus                     dir   1.2km      5s    38  ████████████████████    9  14   3   8   2   2  ●●●● -3.5dB  -82dBm
  IAH Solar Reiheim             4h      —    1m20s    15  ███████░░░░░░░░░░░░░░    0   6   1   5   3   0  ○○○○    —dB    —dBm
  Unknown !..3a                  —      —      43s     5  ██░░░░░░░░░░░░░░░░░░░    1   2   0   2   0   0  ●○○○ -9.0dB  -95dBm
```

**Columns:**
| Column | Meaning |
|---|---|
| Direct Connected | Node whose radio your device physically heard (last-hop relayer) |
| Hops Out | `dir` = direct link · `Nh` = N hops away in routing table · `—` = unknown |
| Dist | GPS-derived great-circle distance to the relay node (requires position data for both nodes) |
| last | Time since the most recent packet from this relay |
| Pkts | Total packets relayed by this node this session |
| Bar | Relative packet volume |
| txt pos usr tel nb tr | Per-type packet counts (text · position · nodeinfo · telemetry · neighborinfo · traceroute) |
| Signal dots | `●●●●` green > 5 dB · `●●●○` bright green 0–5 dB · `●●○○` yellow −10–0 dB · `●○○○` red < −10 dB · `○○○○` no data |
| SNR / RSSI | Average signal quality of the last-hop link (only packets with signal data counted) |

> **Direct Connected vs. routing:** A node appearing here means your radio can hear it directly over the air. This does not imply you can reach it directly for outbound messages — the `Hops Out` column from the routing table governs that.

### Outbound View (opens automatically after `m<n>`)

Shown immediately after sending a DM. Updates every second until Enter is pressed.

**With a known traced route:**
```
  Outbound: DM to Alice #0a3f2b11  |  t+4.7s
  ════════════════════════════════════════════════════════════════════════════

  ► Sent   "Hey, are you there?"  →  Alice
    Route (35s ago):  [YOU] → [Morpheus](+2.5dB) → [Alice]

  ─── Relay echoes (re-broadcasts of our packet we could hear) ─────────────────
  Node                    SNR   RSSI  hops      at
  ────────────────────────────────────────────────────────────────────────────
  Morpheus               +2.5    -78     1    0.3s

  ─── ACK return path ────────────────────────────────────────────────────────
  t+3.92s  ✓ ACK from Alice  via Morpheus  SNR +1.0dB  RSSI -79dBm  2 hops
  [Alice] ──[Morpheus]── [YOU]
  → Next-hop learned: Morpheus will relay future DMs to Alice

  Press Enter to return
```

**With a direct link:**
```
  Outbound: DM to Morpheus #0b1c2d3e  |  t+2.1s
  ════════════════════════════════════════════════════════════════════════════

  ► Sent   "Are you on channel 2?"  →  Morpheus
    [YOU] ────────────────────── [Morpheus]  (direct link)

  ─── Relay echoes (re-broadcasts of our packet we could hear) ─────────────────
  (none observed — direct link, no relay expected)

  ─── ACK return path ────────────────────────────────────────────────────────
  t+1.84s  ✓ ACK from Morpheus  (direct)  SNR -4.5dB  RSSI -81dBm  0 hops
  [Morpheus] ───────────────── [YOU]  (direct)
  → Direct link confirmed — no relay needed

  Press Enter to return
```

**Relay echoes** are re-broadcasts of your own packet that your radio overhears. They only appear if the relaying node is within direct RF range of you. If the destination is multiple hops away the relay nodes are usually too far to hear directly.

---

## Logging

Appended to `newscan.log` in the project directory (excluded from git).

```
----  2026-03-29 14:23:42  INFO      === newscan starting ===
----  2026-03-29 14:23:47  INFO      Connecting to HEHO_ab07
HEHO  2026-03-29 14:24:12  INFO      Connected to HEHO_ab07
HEHO  2026-03-29 14:24:13  INFO      ◀◀ ✉ CH0 Short Slow  Alice -> MyNode: 'Hello'  [snr=5dB, rssi=-82dBm]
HEHO  2026-03-29 14:24:13  INFO      ▶▶ CH0 Short Slow  Message sent to Bob: 'Hi'  ✉
HEMO  2026-03-29 14:24:14  INFO      Connected to HEMO_cb7e
HEMO  2026-03-29 14:25:10  INFO      ▶▶ ⌁ ping (nodeinfo request) sent to Alice (!aabbccdd)
HEMO  2026-03-29 14:25:15  WARNING   ◀◀ NAK from Bob (!deadbeef): NO_RESPONSE
```

Every line is prefixed with the connected node's short name (4-char radio identifier). Pre-connection lines show `----`. When switching between devices in one session the prefix changes accordingly.

`◀◀` lines render in bright white (incoming); `▶▶` lines render in dim (outgoing).

Relay nodes are resolved to full names where known (`via Alice` instead of `via !..07`). Own node packets are not logged.

The last 8 log lines are always visible as a pinned footer at the bottom of the terminal. Press `l` from the main view to expand the log to full-screen (shows the last ~terminal-height lines, refreshes every second). Press `l` again to return to the normal view.

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
