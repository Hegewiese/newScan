#!/usr/bin/env python3
"""
Meshtastic BLE scanner — discover devices and connect to one.
Copyright by me
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

# Firmware update checking
try:
    from firmware_check import check_device_firmware, FirmwareChecker
except ImportError:
    # Define dummy functions if module not found
    def check_device_firmware(iface):
        return {"error": "firmware_check module not found", "message": "Update checking disabled"}
    
    class FirmwareChecker:
        @staticmethod
        def check_firmware_update(current_version, hw_model=None):
            return {"error": "firmware_check module not found", "current_version": current_version}

        @staticmethod
        def format_update_message(check_result, verbose=True):
            if check_result.get("error"):
                return f"⚠  {check_result['error']}"
            return f"Current: {check_result.get('current_version', 'N/A')}"

# Re-exec into meshtastic_venv before attempting any third-party imports.
if sys.prefix == sys.base_prefix:
    _venv_python = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "meshtastic_venv", "bin", "python"
    )
    if os.path.isfile(_venv_python):
        print("Not running in a virtual environment.")
        print("  Included venv found: meshtastic_venv/")
        _ans = input("  Activate meshtastic_venv and restart? [Y/n]: ").strip().lower()
        if _ans != "n":
            os.execv(_venv_python, [_venv_python] + sys.argv)

try:
    import meshtastic.ble_interface
    from meshtastic.ble_interface import BLEInterface, BLEClient, SERVICE_UUID
    from bleak import BleakScanner
except ImportError:
    print("Error: meshtastic not installed. Run: pip install meshtastic bleak")
    sys.exit(1)

logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "newscan.log"),
    level=logging.WARNING,  # suppress third-party library noise (bleak, meshtastic, dbus, …)
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("newscan")
log.setLevel(logging.DEBUG)  # our own logger always writes in full detail

# ---------------------------------------------------------------------------
# Fixed log-tail footer
# ---------------------------------------------------------------------------
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "newscan.log")
_TAIL_LINES = 8
_FOOTER_ROWS = _TAIL_LINES + 1          # separator row + 5 log lines
_tail_cache: list = []
_tail_lock = threading.Lock()
_tail_proc = None


def _redraw_log_footer() -> None:
    """Overwrite the pinned footer rows without disturbing the cursor."""
    try:
        cols, rows = shutil.get_terminal_size()
    except Exception:
        return
    with _tail_lock:
        lines = list(reversed(_tail_cache))
    buf = "\0337"                        # DECSC — save cursor
    sep_row = rows - _FOOTER_ROWS + 1
    label = " LOGs "
    buf += f"\033[{sep_row};1H\033[K" + "─" * 2 + label + "─" * max(0, cols - 2 - len(label))
    for i in range(_TAIL_LINES):
        row  = sep_row + 1 + i
        text = lines[i] if i < len(lines) else ""
        if "WARNING" in text or "ERROR" in text:
            text = f"\033[31m{text[:cols]}\033[0m"
        elif "tracer [" in text or "tracer to " in text or "Repeated tracer" in text:
            text = f"\033[94m{text[:cols]}\033[0m"
        else:
            text = text[:cols]
        buf += f"\033[{row};1H\033[K{text}"
    buf += "\0338"                       # DECRC — restore cursor
    sys.stdout.write(buf)
    sys.stdout.flush()


def _setup_scroll_region() -> None:
    """Restrict terminal scrolling to the rows above the footer."""
    try:
        rows = shutil.get_terminal_size().lines
    except Exception:
        return
    sys.stdout.write(f"\033[1;{rows - _FOOTER_ROWS}r")
    sys.stdout.flush()
    _redraw_log_footer()


def _tail_worker() -> None:
    global _tail_proc
    try:
        _tail_proc = subprocess.Popen(
            ["tail", "-n", str(_TAIL_LINES), "-f", _LOG_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        for raw in _tail_proc.stdout:
            line = raw.rstrip("\n")
            with _tail_lock:
                _tail_cache.append(line)
                if len(_tail_cache) > _TAIL_LINES:
                    del _tail_cache[:-_TAIL_LINES]
            _redraw_log_footer()
    except Exception:
        pass


def start_log_tail() -> None:
    threading.Thread(target=_tail_worker, daemon=True).start()
    _setup_scroll_region()


def stop_log_tail() -> None:
    global _tail_proc
    if _tail_proc is not None:
        try:
            _tail_proc.terminate()
        except Exception:
            pass
        _tail_proc = None
    try:
        rows = shutil.get_terminal_size().lines
        sys.stdout.write(f"\033[r\033[{rows};1H\n")
        sys.stdout.flush()
    except Exception:
        pass


def clear_screen() -> None:
    """Clear the scrolling region and re-establish the pinned footer."""
    sys.stdout.write("\033[H\033[2J")
    sys.stdout.flush()
    _setup_scroll_region()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Received packet logging — all types
# ---------------------------------------------------------------------------
_rx_subs: list = []        # list of (callback, topic) registered by start_message_log
_last_rx_time: float = 0.0  # epoch timestamp of last received packet — updated by start_message_log
_RX_WATCHDOG_SECS = 300     # warn if no packet received for this long (5 minutes)

# ---------------------------------------------------------------------------
# Inflow view — session-wide packet counter (populated by start_message_log)
# ---------------------------------------------------------------------------
_inflow_lock      = threading.Lock()
_inflow_data: dict = {}   # {node_id_int: {name, total, text, position, user,
                           #                telemetry, neighborinfo, traceroute,
                           #                relay, last_ts}}
_inflow_start_ts: float = 0.0

# ---------------------------------------------------------------------------
# Route cache — populated whenever a traceroute completes successfully
# ---------------------------------------------------------------------------
_route_cache: dict = {}   # {dest_node_num_int: {ts, hops: [{name, snr_raw}]}}

BROADCAST = 0xFFFFFFFF


def _rx_resolve(interface, num):
    """Resolve a node number (int) to its best display name."""
    if num is None:
        return "?"
    # nodesByNum is keyed by int — preferred; nodes is keyed by "!hex" string
    node = (
        (getattr(interface, "nodesByNum", None) or {}).get(num)
        or (getattr(interface, "nodes",      None) or {}).get(num)
        or (getattr(interface, "nodes",      None) or {}).get(f"!{num:08x}" if isinstance(num, int) else num)
        or {}
    )
    u = node.get("user", {})
    return (
        u.get("longName")
        or u.get("shortName")
        or (f"!{num:08x}" if isinstance(num, int) else str(num))
    )


def _rx_relay(interface, packet) -> str:
    """Return 'via <name>' for the last relay hop, or '' if direct / unknown."""
    relay_byte = packet.get("relayNode")
    if not relay_byte:
        return ""
    try:
        # nodesByNum is int-keyed — preferred
        for num, node in (getattr(interface, "nodesByNum", None) or {}).items():
            if isinstance(num, int) and (num & 0xFF) == relay_byte:
                u = node.get("user", {})
                name = u.get("longName") or u.get("shortName") or f"!{num:08x}"
                return f"via {name}"
        # nodes may be keyed by "!hex" strings
        for key, node in (interface.nodes or {}).items():
            num = int(key.lstrip("!"), 16) if isinstance(key, str) and key.startswith("!") else key
            if isinstance(num, int) and (num & 0xFF) == relay_byte:
                u = node.get("user", {})
                name = u.get("longName") or u.get("shortName") or f"!{num:08x}"
                return f"via {name}"
    except Exception:
        pass
    return f"via !..{relay_byte:02x}"


def _rx_sig(packet) -> str:
    """Return '[snr=x, rssi=y]' signal suffix when available."""
    parts = []
    snr  = packet.get("rxSnr")
    rssi = packet.get("rxRssi")
    if snr  is not None: parts.append(f"snr={snr}dB")
    if rssi is not None: parts.append(f"rssi={rssi}dBm")
    return f"  [{', '.join(parts)}]" if parts else ""


def start_message_log(iface) -> None:
    """Subscribe to all incoming packet types and write each to the log."""
    from pubsub import pub
    global _rx_subs

    my_num = iface.myInfo.my_node_num

    # ── text messages ──────────────────────────────────────────────────────
    def _on_text(packet, interface):
        try:
            if packet.get("from") == my_num:
                return
            nodes      = interface.nodes or {}
            sender_num = packet.get("from")
            to_num     = packet.get("to", 0)
            sender     = _rx_resolve(interface,sender_num)
            dest       = "broadcast" if to_num == BROADCAST else _rx_resolve(interface,to_num)
            relay      = _rx_relay(interface, packet)
            via        = f"  {relay}" if relay else ""
            ch         = _ch_label(interface, packet.get("channel", 0))
            text       = packet.get("decoded", {}).get("text", "")
            icon       = "💬"
            log.info(f"{_RX}◀◀ {icon} {ch}  {sender}{via} -> {dest}: {text!r}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX text log error: {e}")

    # ── position ───────────────────────────────────────────────────────────
    def _on_position(packet, interface):
        try:
            ch  = _ch_label(interface, packet.get("channel", 0))
            pos = packet.get("decoded", {}).get("position", {})
            parts = []
            if "latitudeI"  in pos: parts.append(f"lat={pos['latitudeI']/1e7:.5f}")
            if "longitudeI" in pos: parts.append(f"lon={pos['longitudeI']/1e7:.5f}")
            if pos.get("altitude"):  parts.append(f"alt={pos['altitude']}m")
            if pos.get("satsInView"):parts.append(f"sats={pos['satsInView']}")
            detail = "  ".join(parts) if parts else "no fix"
            if packet.get("from") == my_num:
                log.info(f"{_TX}▶▶ ⊕ {ch}  {_rx_resolve(interface, my_num)}: {detail}{_RST}")
                return
            sender = _rx_resolve(interface, packet.get("from"))
            via    = f"  {_rx_relay(interface, packet)}" if _rx_relay(interface, packet) else ""
            log.info(f"{_RX}◀◀ ⊕ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX position log error: {e}")

    # ── node info ──────────────────────────────────────────────────────────
    def _on_user(packet, interface):
        try:
            ch = _ch_label(interface, packet.get("channel", 0))
            u  = packet.get("decoded", {}).get("user", {})
            detail = (f"long={u.get('longName')}  short={u.get('shortName')}  "
                      f"hw={u.get('hwModel')}  role={u.get('role')}")
            if packet.get("from") == my_num:
                log.info(f"{_TX}▶▶ ◉ {ch}  {_rx_resolve(interface, my_num)}: {detail}{_RST}")
                return
            sender = _rx_resolve(interface, packet.get("from"))
            via    = f"  {_rx_relay(interface, packet)}" if _rx_relay(interface, packet) else ""
            log.info(f"{_RX}◀◀ ◉ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX nodeinfo log error: {e}")

    # ── telemetry ──────────────────────────────────────────────────────────
    def _on_telemetry(packet, interface):
        try:
            ch = _ch_label(interface, packet.get("channel", 0))
            t  = packet.get("decoded", {}).get("telemetry", {})
            dm = t.get("deviceMetrics", {})
            em = t.get("environmentMetrics", {})
            parts = []
            if dm:
                if dm.get("batteryLevel")       is not None: parts.append(f"bat={dm['batteryLevel']}%")
                if dm.get("voltage")            is not None: parts.append(f"volt={dm['voltage']:.2f}V")
                if dm.get("channelUtilization") is not None: parts.append(f"chUtil={dm['channelUtilization']:.1f}%")
                if dm.get("airUtilTx")          is not None: parts.append(f"airTx={dm['airUtilTx']:.1f}%")
                if dm.get("uptimeSeconds")      is not None: parts.append(f"up={dm['uptimeSeconds']}s")
            if em:
                if em.get("temperature")        is not None: parts.append(f"temp={em['temperature']:.1f}°C")
                if em.get("relativeHumidity")   is not None: parts.append(f"hum={em['relativeHumidity']:.1f}%")
                if em.get("barometricPressure") is not None: parts.append(f"pres={em['barometricPressure']:.1f}hPa")
            icon   = "⊡" if dm else "⊛"
            detail = "  ".join(parts) if parts else "—"
            if packet.get("from") == my_num:
                log.info(f"{_TX}▶▶ {icon} {ch}  {_rx_resolve(interface, my_num)}: {detail}{_RST}")
                return
            sender = _rx_resolve(interface, packet.get("from"))
            via    = f"  {_rx_relay(interface, packet)}" if _rx_relay(interface, packet) else ""
            log.info(f"{_RX}◀◀ {icon} {ch}  {sender}{via}: {detail}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX telemetry log error: {e}")

    # ── neighbor info ──────────────────────────────────────────────────────
    def _on_neighborinfo(packet, interface):
        try:
            ch        = _ch_label(interface, packet.get("channel", 0))
            ni        = packet.get("decoded", {}).get("neighborinfo", {})
            neighbors = ni.get("neighbors", [])
            nb_names  = [_rx_resolve(interface, nb.get("nodeId")) for nb in neighbors[:6]]
            detail    = f"{len(neighbors)} neighbors: {', '.join(nb_names)}" if nb_names else "0 neighbors"
            if packet.get("from") == my_num:
                log.info(f"{_TX}▶▶ ⬡ {ch}  {_rx_resolve(interface, my_num)}: {detail}{_RST}")
                return
            sender = _rx_resolve(interface, packet.get("from"))
            via    = f"  {_rx_relay(interface, packet)}" if _rx_relay(interface, packet) else ""
            log.info(f"{_RX}◀◀ ⬡ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX neighborinfo log error: {e}")

    # ── traceroute received from others ────────────────────────────────────
    def _on_traceroute_rx(packet, interface):
        try:
            # Skip responses to our own traceroutes — _log_tracer_details already
            # logs those in full detail (towards / back / metrics).
            if packet.get("to") == my_num:
                return
            sender = _rx_resolve(interface, packet.get("from"))
            dest   = _rx_resolve(interface, packet.get("to"))
            relay  = _rx_relay(interface, packet)
            via    = f"  {relay}" if relay else ""
            ch     = _ch_label(interface, packet.get("channel", 0))
            log.info(f"{_RX}◀◀ ⇌ {ch}  {sender}{via} -> {dest}{_rx_sig(packet)}{_RST}")
        except Exception as e:
            log.warning(f"RX traceroute log error: {e}")

    def _stamp_rx(packet, interface):
        global _last_rx_time
        _last_rx_time = time.time()

    subs = [
        (_on_text,          "meshtastic.receive.text"),
        (_on_position,      "meshtastic.receive.position"),
        (_on_user,          "meshtastic.receive.user"),
        (_on_telemetry,     "meshtastic.receive.telemetry"),
        (_on_neighborinfo,  "meshtastic.receive.neighborinfo"),
        (_on_traceroute_rx, "meshtastic.receive.traceroute"),
    ]
    for cb, topic in subs:
        pub.subscribe(cb, topic)
        pub.subscribe(_stamp_rx, topic)

    # ── inflow tracking — count every received packet per source node ──────
    global _inflow_start_ts
    _inflow_start_ts = time.time()
    with _inflow_lock:
        _inflow_data.clear()

    def _make_inflow_handler(pkt_type):
        def _handler(packet, interface):
            if packet.get("from") == my_num:
                return
            if packet.get("from") is None:
                return
            relay_raw = _rx_relay(interface, packet)
            # strip the "via " prefix to get just the relay node name
            relay_name = relay_raw[4:] if relay_raw.startswith("via ") else relay_raw
            key = relay_name if relay_name else "direct"
            snr  = packet.get("rxSnr")
            rssi = packet.get("rxRssi")
            with _inflow_lock:
                if key not in _inflow_data:
                    _inflow_data[key] = {
                        "name": key, "total": 0,
                        "text": 0, "position": 0, "user": 0,
                        "telemetry": 0, "neighborinfo": 0, "traceroute": 0,
                        "snr_sum": 0.0, "rssi_sum": 0, "sig_count": 0,
                        "last_ts": time.time(),
                    }
                d = _inflow_data[key]
                d["total"]   += 1
                d[pkt_type]  += 1
                d["last_ts"]  = time.time()
                if snr is not None:
                    d["snr_sum"]   += snr
                    d["rssi_sum"]  += (rssi or 0)
                    d["sig_count"] += 1
        return _handler

    _inflow_topics = [
        ("text",         "meshtastic.receive.text"),
        ("position",     "meshtastic.receive.position"),
        ("user",         "meshtastic.receive.user"),
        ("telemetry",    "meshtastic.receive.telemetry"),
        ("neighborinfo", "meshtastic.receive.neighborinfo"),
        ("traceroute",   "meshtastic.receive.traceroute"),
    ]
    _inflow_subs = [(_make_inflow_handler(pt), topic) for pt, topic in _inflow_topics]
    for cb, topic in _inflow_subs:
        pub.subscribe(cb, topic)

    _rx_subs = subs + [(_stamp_rx, t) for _, t in subs] + _inflow_subs


def stop_message_log() -> None:
    global _rx_subs
    if _rx_subs:
        try:
            from pubsub import pub
            for cb, topic in _rx_subs:
                try:
                    pub.unsubscribe(cb, topic)
                except Exception:
                    pass
        except Exception:
            pass
        _rx_subs = []

# ---------------------------------------------------------------------------


def _check_bt_linux():
    try:
        out = subprocess.run(
            ["bluetoothctl", "show"], capture_output=True, text=True, timeout=3
        )
    except FileNotFoundError:
        log.warning("Bluetooth preflight: bluetoothctl not found")
        print(
            "  [WARN] Bluetooth: bluetoothctl not found\n"
            "                    sudo apt install bluez\n"
            "                    sudo usermod -aG bluetooth $USER"
        )
        return
    except subprocess.TimeoutExpired:
        log.warning("Bluetooth preflight: bluetoothctl timed out")
        print("  [WARN] Bluetooth: bluetoothctl timed out — status unknown")
        return

    if not out.stdout.strip():
        log.warning("Bluetooth preflight: no adapter found")
        print(
            "  [WARN] Bluetooth: no adapter found\n"
            "                    sudo systemctl start bluetooth"
        )
        return

    if "Powered: yes" in out.stdout:
        log.info("Bluetooth preflight: adapter powered on")
        print("  [ OK ] Bluetooth: adapter powered on")
    else:
        log.warning("Bluetooth preflight: adapter not powered on")
        print("  [WARN] Bluetooth: adapter found but not powered on")
        ans = input("          Power on Bluetooth now? [Y/n]: ").strip().lower()
        if ans != "n":
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=3)
            log.info("Bluetooth preflight: powered on by user request")
            print("          Bluetooth powered on.")


def _check_bt_macos():
    try:
        out = subprocess.run(
            ["system_profiler", "SPBluetoothDataType"],
            capture_output=True, text=True, timeout=5,
        )
        if "Bluetooth Power: On" in out.stdout or "State: On" in out.stdout:
            log.info("Bluetooth preflight: on (macOS)")
            print("  [ OK ] Bluetooth: on")
        else:
            log.warning("Bluetooth preflight: may be off (macOS)")
            print(
                "  [WARN] Bluetooth: may be off\n"
                "                    System Settings → Bluetooth → turn on"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("Bluetooth preflight: could not determine status (macOS)")
        print("  [ ?? ] Bluetooth: could not determine status (bleak will report errors)")


def preflight_check():
    """Verify platform, Python version, venv, and Bluetooth before starting."""
    log.info("=== newscan starting ===")
    print("Checking requirements...")

    # ── Platform ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        log.error("Preflight failed: Windows platform")
        print("  [FAIL] Platform : Windows is not supported. Use Linux or macOS.")
        sys.exit(1)
    elif sys.platform == "linux":
        log.info("Preflight: platform=Linux")
        print("  [ OK ] Platform : Linux")
    elif sys.platform == "darwin":
        log.info("Preflight: platform=macOS")
        print("  [ OK ] Platform : macOS")
    else:
        log.warning(f"Preflight: untested platform={sys.platform}")
        print(f"  [WARN] Platform : {sys.platform} (untested — proceed with caution)")

    # ── Python version ────────────────────────────────────────────────────────
    ver = sys.version_info
    if ver < (3, 10):
        log.error(f"Preflight failed: Python {ver.major}.{ver.minor} < 3.10")
        print(f"  [FAIL] Python   : {ver.major}.{ver.minor} — 3.10+ required.")
        sys.exit(1)
    log.info(f"Preflight: Python {ver.major}.{ver.minor}.{ver.micro}")
    print(f"  [ OK ] Python   : {ver.major}.{ver.minor}.{ver.micro}")

    # ── Virtual environment ───────────────────────────────────────────────────
    if sys.prefix != sys.base_prefix:
        venv_name = os.path.basename(sys.prefix)
        log.info(f"Preflight: venv={venv_name}")
        print(f"  [ OK ] Venv     : {venv_name}")
    else:
        log.warning("Preflight: not running in a virtual environment")
        print(
            "  [WARN] Venv     : not active, meshtastic_venv/ not found\n"
            "                    pip install meshtastic bleak  (or create a venv first)"
        )

    # ── Bluetooth ─────────────────────────────────────────────────────────────
    if sys.platform == "linux":
        _check_bt_linux()
    elif sys.platform == "darwin":
        _check_bt_macos()

    print()


class _FastBLEInterface(BLEInterface):
    """BLEInterface that skips the internal 10 s rescan.

    The base class always calls BLEInterface.scan() before connecting, even
    when the address is already known. This subclass replaces that with
    BleakScanner.find_device_by_address() which returns as soon as the
    specific device responds (typically 1-3 s).
    """

    def connect(self, address=None):
        with BLEClient() as helper:
            device = helper.async_await(
                BleakScanner.find_device_by_address(address, timeout=5.0)
            )
        if device is None:
            raise BLEInterface.BLEError(f"Device not found: {address}")
        client = BLEClient(device.address, disconnected_callback=lambda _: self.close())
        client.connect()
        client.discover()
        return client

    def close(self):
        try:
            import atexit
            from meshtastic.mesh_interface import MeshInterface
            MeshInterface.close(self)
        except Exception:
            pass
        self._want_receive = False
        if self.client:
            try:
                atexit.unregister(self._exit_handler)
                self.client.disconnect()  # BLE disconnect — non-blocking
            except Exception:
                pass
            self.client = None
        try:
            self._disconnected()  # publish meshtastic.connection.lost
        except Exception:
            pass


SCAN_DURATION = 10  # seconds — must match BLEInterface.scan() timeout


class _Device:
    """Minimal stand-in for a BLEDevice (name + address)."""
    def __init__(self, name, address):
        self.name = name
        self.address = address


def find_known_devices():
    """Return already-paired Meshtastic devices from the system without scanning."""
    log.info("Searching for known paired Meshtastic devices")
    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        log.warning("find_known_devices: bluetoothctl not found")
        return []
    except subprocess.TimeoutExpired:
        log.warning("find_known_devices: bluetoothctl timed out")
        return []

    devices = []
    for line in out.stdout.splitlines():
        parts = line.split(" ", 2)           # "Device <addr> <name>"
        if len(parts) < 3 or parts[0] != "Device":
            continue
        address, name = parts[1], parts[2]
        try:
            info = subprocess.run(
                ["bluetoothctl", "info", address],
                capture_output=True, text=True, timeout=3,
            )
        except subprocess.TimeoutExpired:
            log.warning(f"find_known_devices: bluetoothctl info timed out for {address}")
            continue
        if SERVICE_UUID.lower() in info.stdout.lower():
            log.info(f"Found known device: {name} [{address}]")
            devices.append(_Device(name, address))

    log.info(f"Known devices found: {len(devices)}")
    return devices


def scan():
    """Return a list of nearby Meshtastic BLE devices, with a progress bar."""
    log.info("Starting BLE scan")
    result = []
    done = threading.Event()

    def _scan():
        result.extend(BLEInterface.scan())
        done.set()

    threading.Thread(target=_scan, daemon=True).start()

    bar_width = 40
    tick = 0.1
    steps = int(SCAN_DURATION / tick)

    for i in range(steps + 1):
        filled = int(bar_width * i / steps)
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r  Scanning BLE... [{bar}] {i * tick:4.1f}s", end="", flush=True)
        if done.is_set():
            break
        time.sleep(tick)

    done.wait()
    print(f"\r  Scanning BLE... [{'#' * bar_width}] {SCAN_DURATION:.1f}s")
    print(f"Found {len(result)} device(s). Please choose device you want to connect to and use")
    log.info(f"BLE scan complete: {len(result)} device(s) found")
    for d in result:
        log.info(f"  Scanned device: {getattr(d, 'name', '?')} [{getattr(d, 'address', '?')}]")
    return result


def pick_device(devices):
    """Print device list and let the user choose one. Returns the chosen BLEDevice."""
    print()
    for i, d in enumerate(devices, 1):
        print(f"  [{i}] {d.name or 'Unknown'}  |  {d.address}")

    while True:
        choice = input("\nSelect device (number) [1]: ").strip()
        if not choice:
            log.info(f"User selected device (default): {devices[0].name} [{devices[0].address}]")
            return devices[0]
        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            chosen = devices[int(choice) - 1]
            log.info(f"User selected device: {chosen.name} [{chosen.address}]")
            return chosen
        print("Invalid choice, try again.")


def _ch_label(iface, channel_index: int) -> str:
    """Return 'CH<n> <name>' if the channel has a name, else 'CH<n>'."""
    try:
        chs = iface.localNode.channels or []
        ch_obj = next((c for c in chs if c.index == channel_index), None)
        name = ch_obj.settings.name.strip() if ch_obj and ch_obj.settings.name.strip() else None
    except Exception:
        name = None
    if not name and channel_index == 0:
        name = "Short Slow"
    return f"CH{channel_index} {name}" if name else f"CH{channel_index}"


def _ago(ts):
    """Format a Unix timestamp aas a human-readable 'X ago' string."""
    if not ts:
        return "never"
    diff = int(time.time()) - int(ts)
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    return f"{diff // 3600}h ago"


def _peer_name(peer):
    user = peer.get("user", {})
    return user.get("longName") or user.get("shortName") or "Unknown"


def send_message(iface, node_id, name):
    """Prompt for a message, send it, then show the outbound routing view."""
    print(f"\n  Sending message to {name}")
    text = input("  Message: ").strip()
    if not text:
        log.info(f"send_message to {name} ({node_id}): cancelled (empty input)")
        print("  Cancelled.")
        return

    ack_event  = threading.Event()
    ack_result = {"packet": None, "elapsed": None, "error": None}
    send_time  = time.time()

    def _on_ack(packet):
        if ack_event.is_set():
            return
        routing = packet.get("decoded", {}).get("routing", {})
        error   = routing.get("errorReason", "NONE")
        ack_result["packet"]  = packet
        ack_result["elapsed"] = time.time() - send_time
        ack_result["error"]   = error
        if error == "NONE":
            log.info(f"{_RX}◀◀ ACK received from {name} ({node_id}){_RST}")
        else:
            log.warning(f"{_RX}◀◀ NAK from {name} ({node_id}): {error}{_RST}")
        ack_event.set()

    try:
        sent_pkt = iface.sendText(
            f"[{time.strftime('%H:%M:%S')}] {text}",
            destinationId=node_id, wantAck=True, onResponse=_on_ack,
        )
        pkt_id = sent_pkt.get("id") if isinstance(sent_pkt, dict) else None
        nh = sent_pkt.get("nextHop") if isinstance(sent_pkt, dict) else None
        if nh:
            nh_node = (getattr(iface, "nodesByNum", None) or {}).get(nh, {})
            nh_name = (nh_node.get("user", {}).get("longName")
                       or nh_node.get("user", {}).get("shortName")
                       or f"!{nh:08x}")
            routing_tag = f"[next-hop: {nh_name}]"
        else:
            routing_tag = "[flooding]"
        log.info(f"{_TX}▶▶ {_ch_label(iface, 0)}  Message sent to {name} ({node_id}): {text!r}  💬  {routing_tag}{_RST}")
    except Exception as e:
        log.exception(f"send_message to {name} ({node_id}) failed: {e}")
        print(f"  Failed: {e}")
        return

    show_outbound_view(iface, node_id, name, pkt_id, text, send_time, ack_event, ack_result)


def send_repeated(iface, node_id, name):
    """Prompt for a message and interval, then send repeatedly until Enter is pressed."""
    print(f"\n  Repeated message to {name}")
    text = input("  Message: ").strip()
    if not text:
        log.info(f"send_repeated to {name} ({node_id}): cancelled (empty input)")
        print("  Cancelled.")
        return
    interval_str = input("  Interval in seconds [10]: ").strip()
    interval = int(interval_str) if interval_str.isdigit() else 10
    log.info(f"Repeated message to {name} ({node_id}) started: interval={interval}s text={text!r}")

    stop = threading.Event()

    def _send_loop():
        count = 0
        while not stop.is_set():
            count += 1
            current = count

            def onAckNak(packet, _c=current):
                routing = packet.get("decoded", {}).get("routing", {})
                error = routing.get("errorReason", "NONE")
                if error == "NONE":
                    log.info(f"{_RX}◀◀ ACK received for repeated message #{_c} from {name} ({node_id}){_RST}")
                    print(f"\n  ACK #{_c} from {name}.", flush=True)
                else:
                    log.warning(f"{_RX}◀◀ NAK for repeated message #{_c} from {name} ({node_id}): {error}{_RST}")
                    print(f"\n  NAK #{_c} from {name}: {error}", flush=True)

            try:
                _spkt = iface.sendText(f"[{time.strftime('%H:%M:%S')}] {text}", destinationId=node_id, wantAck=True, onResponse=onAckNak)
                _nh = _spkt.get("nextHop") if isinstance(_spkt, dict) else None
                if _nh:
                    _nh_node = (getattr(iface, "nodesByNum", None) or {}).get(_nh, {})
                    _nh_name = (_nh_node.get("user", {}).get("longName")
                                or _nh_node.get("user", {}).get("shortName")
                                or f"!{_nh:08x}")
                    _rtag = f"[next-hop: {_nh_name}]"
                else:
                    _rtag = "[flooding]"
                log.info(f"{_TX}▶▶ {_ch_label(iface, 0)}  Repeated message #{current} sent to {name} ({node_id})  💬  {_rtag}{_RST}")
                print(f"\r  Sent #{current} to {name}. Press Enter to stop.", end="", flush=True)
            except Exception as e:
                log.exception(f"Repeated message #{current} to {name} ({node_id}) failed: {e}")
                print(f"\r  Send #{current} failed: {e}")
            stop.wait(interval)

    t = threading.Thread(target=_send_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join()
    log.info(f"Repeated message to {name} ({node_id}) stopped")
    print(f"  Stopped.")


TRACEROUTE_TIMEOUT = 30  # seconds
_LB  = "\033[94m"   # light blue — used for traceroute terminal output
_RST = "\033[0m"
_RX  = "\033[97m"   # bright white  — incoming packets (lighter)
_TX  = "\033[2m"    # dim           — outgoing packets (darker)
PING_TIMEOUT       = 10  # seconds
_UNK_SNR = -128  # meshtastic sentinel for unknown SNR


def _log_tracer_details(packet: dict, name: str, iface=None) -> None:
    """Write every available field of a tracer response packet to the log."""
    from pubsub import pub  # noqa: F401 (already imported by meshtastic; just use it)

    tr = packet.get("decoded", {}).get("traceroute", {})
    frm        = packet.get("from",     "?")
    to         = packet.get("to",       "?")
    pkt_id     = packet.get("id",       "?")
    rx_snr     = packet.get("rxSnr")
    rx_rssi    = packet.get("rxRssi")
    hop_start  = packet.get("hopStart")
    hop_limit  = packet.get("hopLimit")

    route        = tr.get("route",       [])
    snr_towards  = tr.get("snrTowards",  [])
    route_back   = tr.get("routeBack",   [])
    snr_back     = tr.get("snrBack",     [])

    def _n(num):
        if iface is not None and isinstance(num, int):
            return _rx_resolve(iface, num)
        return f"!{num:08x}" if isinstance(num, int) else str(num)

    def _snr(raw):
        return "?" if raw == _UNK_SNR else f"{raw / 4:.2f}dB"

    # --- towards route --------------------------------------------------
    parts = [_n(to)]
    for i, n in enumerate(route):
        snr = _snr(snr_towards[i]) if i < len(snr_towards) else "?"
        parts.append(f"{_n(n)}({snr})")
    last_snr = _snr(snr_towards[-1]) if snr_towards else "?"
    parts.append(f"{_n(frm)}({last_snr})")
    towards_line = f"tracer [{name}] towards:  {' --> '.join(parts)}"
    log.info(towards_line)
    print(f"\n{_LB}  {towards_line}{_RST}")

    # --- back route (only if present) -----------------------------------
    if route_back or snr_back:
        bp = [_n(frm)]
        for i, n in enumerate(route_back):
            snr = _snr(snr_back[i]) if i < len(snr_back) else "?"
            bp.append(f"{_n(n)}({snr})")
        last_snr_back = _snr(snr_back[-1]) if snr_back else "?"
        bp.append(f"{_n(to)}({last_snr_back})")
        back_line = f"tracer [{name}] back:     {' --> '.join(bp)}"
        log.info(back_line)
        print(f"{_LB}  {back_line}{_RST}")

    # --- packet-level metrics -------------------------------------------
    extras = [f"pktId={pkt_id}"]
    if rx_snr  is not None: extras.append(f"rxSNR={rx_snr}dB")
    if rx_rssi is not None: extras.append(f"rxRSSI={rx_rssi}dBm")
    if hop_start is not None: extras.append(f"hopStart={hop_start}")
    if hop_limit is not None: extras.append(f"hopLimit={hop_limit}")
    metrics_line = f"tracer [{name}] metrics:  {', '.join(extras)}"
    log.info(metrics_line)
    print(f"{_LB}  {metrics_line}{_RST}")

    # --- update route cache so outbound view can show the last known path ---
    if isinstance(frm, int):
        hops = []
        for i, node_num in enumerate(route):
            snr_raw = snr_towards[i] if i < len(snr_towards) else None
            hops.append({"name": _n(node_num), "snr_raw": snr_raw})
        _route_cache[frm] = {"ts": time.time(), "hops": hops, "dest": _n(frm)}


def _tracer_with_bar(iface, node_id, hop_limit, name="?"):
    """Run sendTraceRoute in a thread and show a progress bar while waiting.

    The library prints the route result itself when the response arrives.
    Logs full tracer details via pub/sub.
    Returns (success: bool, error: str|None).
    """
    from pubsub import pub

    result  = {"ok": None, "error": None}
    done    = threading.Event()
    pkt_evt = threading.Event()
    captured = [None]

    def _on_tracer(packet, interface):
        captured[0] = packet
        pkt_evt.set()

    pub.subscribe(_on_tracer, "meshtastic.receive.traceroute")

    def _run():
        try:
            iface.sendTraceRoute(node_id, hop_limit)
            result["ok"] = True
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
        done.set()

    threading.Thread(target=_run, daemon=True).start()

    bar_width = 40
    tick = 0.1
    steps = int(TRACEROUTE_TIMEOUT / tick)
    for i in range(steps + 1):
        filled = int(bar_width * i / steps)
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r{_LB}  Waiting for route... [{bar}] {i * tick:4.1f}s{_RST}", end="", flush=True)
        if done.is_set() or pkt_evt.is_set():
            break
        time.sleep(tick)

    print()
    done.wait(timeout=1)
    pkt_evt.wait(timeout=2)  # let the pub/sub thread deliver the packet

    try:
        pub.unsubscribe(_on_tracer, "meshtastic.receive.traceroute")
    except Exception:
        pass

    if captured[0] is not None:
        # Received the route response — this is success regardless of whether
        # sendTraceRoute() has returned yet (race condition at timeout boundary).
        _log_tracer_details(captured[0], name, iface=iface)
        return True, None

    if result["ok"] is False:
        return False, result["error"]

    return False, f"timed out after {TRACEROUTE_TIMEOUT}s"


def tracer_node(iface, node_id, name):
    """Send a single traceroute to the given node and display the full path.

    Prompts for a hop limit (1–7, default 3).  Runs the trace with a live
    progress bar, then prints the towards and back routes with per-hop SNR.
    Results are also written to the log and cached in _route_cache so the
    outbound view can display the last known path for future messages to
    this node.
    """
    print(f"\n{_LB}  tracer to {name}{_RST}")
    hop_str = input("  Hop limit [3]: ").strip()
    hop_limit = int(hop_str) if hop_str.isdigit() and 1 <= int(hop_str) <= 7 else 3
    log.info(f"{_TX}▶▶ tracer to {name} ({node_id}), hop_limit={hop_limit}{_RST}")
    print()
    success, err = _tracer_with_bar(iface, node_id, hop_limit, name=name)
    if success:
        log.info(f"{_RX}◀◀ tracer to {name} ({node_id}) completed successfully{_RST}")
        print(f"{_LB}  Done.{_RST}")
    else:
        log.error(f"{_RX}◀◀ tracer to {name} ({node_id}) failed: {err}{_RST}")
        print(f"{_LB}  Failed: {err}{_RST}")


def tracer_repeated(iface, node_id, name):
    """Send a tracer repeatedly at a configurable interval until Enter is pressed."""
    print(f"\n{_LB}  Repeated tracer to {name}{_RST}")
    hop_str = input("  Hop limit [3]: ").strip()
    hop_limit = int(hop_str) if hop_str.isdigit() and 1 <= int(hop_str) <= 7 else 3
    interval_str = input("  Interval in seconds [30]: ").strip()
    interval = int(interval_str) if interval_str.isdigit() else 30
    log.info(f"Repeated tracer to {name} ({node_id}) started: hop_limit={hop_limit}, interval={interval}s")

    stop = threading.Event()
    last_result = {"count": 0, "success": None}

    def _trace_loop():
        while not stop.is_set():
            last_result["count"] += 1
            count = last_result["count"]
            print(f"\n{_LB}  --- tracer #{count} to {name} ---{_RST}")
            success, err = _tracer_with_bar(iface, node_id, hop_limit, name=name)
            last_result["success"] = success
            if success:
                log.info(f"{_RX}◀◀ Repeated tracer #{count} to {name} ({node_id}) completed{_RST}")
            else:
                log.error(f"{_RX}◀◀ Repeated tracer #{count} to {name} ({node_id}) failed: {err}{_RST}")
                print(f"{_LB}  Failed: {err}{_RST}")
            if not stop.is_set():
                print(f"{_LB}  Next in {interval}s — press Enter to stop.{_RST}")
            stop.wait(interval)

    t = threading.Thread(target=_trace_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join(timeout=3)
    log.info(f"Repeated tracer to {name} ({node_id}) stopped")
    if last_result["success"] is True:
        print(f"{_LB}  Stopped. Last tracer #{last_result['count']}: success.{_RST}")
    elif last_result["success"] is False:
        print(f"{_LB}  Stopped. Last tracer #{last_result['count']}: failed.{_RST}")
    else:
        print("  Stopped.")


def ping_favorites(iface, favorites):
    """Ping all favorite nodes via NodeInfo request; return {node_id: responded} dict."""
    from meshtastic.protobuf import portnums_pb2

    if not favorites:
        return {}

    responded = {node_id: False for node_id, _ in favorites}
    lock = threading.Lock()

    total = len(favorites)
    print(f"\n  Pinging {total} node(s)...\n")
    log.info(f"⌁ Ping favorites started: {total} node(s)")

    for idx, (node_id, peer) in enumerate(favorites):
        name = _peer_name(peer)

        def _on_response(packet, _nid=node_id, _nm=name):
            with lock:
                responded[_nid] = True
            log.info(f"{_RX}◀◀ ⌁ ping response from {_nm} ({_nid}){_RST}")

        try:
            iface.sendData(
                b'',
                destinationId=node_id,
                portNum=portnums_pb2.PortNum.NODEINFO_APP,
                wantResponse=True,
                onResponse=_on_response,
            )
            print(f"  [{idx + 1}/{total}] NodeInfo request sent to {name}")
            log.info(f"{_TX}▶▶ ⌁ ping (nodeinfo request) sent to {name} ({node_id}){_RST}")
        except Exception as e:
            print(f"  [{idx + 1}/{total}] Failed to reach {name}: {e}")
            log.warning(f"⌁ ping to {name} ({node_id}) failed to send: {e}")

        if idx < total - 1:
            tick = 0.1
            steps = int(5 / tick)
            for i in range(steps + 1):
                remaining = 5 - i * tick
                print(f"\r  Next node in {remaining:.1f}s...", end="", flush=True)
                time.sleep(tick)
            print()

    ok = sum(1 for v in responded.values() if v)
    log.info(f"⌁ Ping favorites done: {ok}/{len(favorites)} responded")
    return responded


_EXTRA_FAVORITES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra_favorites.json")
_EXTRA_FAVORITES_TEMPLATE = [
    {"id": "!xxxxxxxx", "name": "Example Node A — replace with real node id", "short": "EXA"},
    {"id": "!yyyyyyyy", "name": "Example Node B — replace with real node id"},
]


def _load_extra_favorites():
    """Load extra favorites from extra_favorites.json next to main.py.

    Creates the file with example entries on first run so the user knows it exists.
    Returns [] on any error.
    """
    try:
        with open(_EXTRA_FAVORITES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        try:
            with open(_EXTRA_FAVORITES_PATH, "w") as f:
                json.dump(_EXTRA_FAVORITES_TEMPLATE, f, indent=2)
                f.write("\n")
            log.info("Created extra_favorites.json with example entries")
        except OSError as e:
            log.warning(f"Could not create extra_favorites.json: {e}")
        return []
    except json.JSONDecodeError as e:
        log.warning(f"extra_favorites.json parse error: {e}")
        return []


def show_outbound_view(iface, node_id, dest_name, pkt_id, message_text,
                       send_time, ack_event, ack_result):
    """Live routing-journey view shown immediately after a DM is sent.

    Displays relay echoes (opportunistic — only visible if relay nodes are in
    direct radio range), ACK timing, signal quality, hop count, and the
    inferred next-hop that Meshtastic will use for future DMs to this node.
    """
    from pubsub import pub

    _stop        = threading.Event()
    _relay_lock  = threading.Lock()
    _relay_events = []   # [{name, snr, rssi, hops_used, elapsed}]

    my_num = iface.myInfo.my_node_num

    # -- dest node context (hops / SNR as last known by node list) ----------
    dest_num = None
    if isinstance(node_id, int):
        dest_num = node_id
    elif isinstance(node_id, str) and node_id.startswith("!"):
        try:
            dest_num = int(node_id[1:], 16)
        except ValueError:
            pass
    dest_node    = ((getattr(iface, "nodesByNum", None) or {}).get(dest_num) or {})
    known_hops   = dest_node.get("hopsAway")
    known_snr    = dest_node.get("snr")
    last_heard   = dest_node.get("lastHeard")

    # hop-scaled ACK timeout: 15s base + 15s per hop, floor 45s
    _hops_for_timeout = known_hops if known_hops is not None else 4
    _ack_timeout = max(45, 15 + _hops_for_timeout * 15)

    # -- relay echo listener ------------------------------------------------
    def _on_echo(packet, interface=None, **kwargs):
        if _stop.is_set():
            return
        if packet.get("from") != my_num:
            return
        if pkt_id is not None and packet.get("id") != pkt_id:
            return
        relay_byte = packet.get("relayNode")
        if not relay_byte:
            return
        rname = f"!..{relay_byte:02x}"
        for num, node in (getattr(iface, "nodesByNum", None) or {}).items():
            if isinstance(num, int) and (num & 0xFF) == relay_byte:
                u = node.get("user", {})
                rname = u.get("longName") or u.get("shortName") or f"!{num:08x}"
                break
        hop_start = packet.get("hopStart")
        hop_limit = packet.get("hopLimit")
        hops_used = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
        with _relay_lock:
            if not any(r["name"] == rname for r in _relay_events):
                _relay_events.append({
                    "name":      rname,
                    "snr":       packet.get("rxSnr"),
                    "rssi":      packet.get("rxRssi"),
                    "hops_used": hops_used,
                    "elapsed":   time.time() - send_time,
                })

    # -- ACK detector via pubsub --------------------------------------------
    # onResponse on BLE is unreliable; subscribe to the routing topic directly
    # and match by requestId so we catch the ACK regardless.
    def _on_routing_ack(packet, interface=None, **kwargs):
        if _stop.is_set() or ack_event.is_set():
            return
        if packet.get("from") != dest_num:
            return
        req_id = packet.get("requestId")
        # Only filter by requestId when both sides are known — the field is
        # often absent (proto3 default 0 / omitted) on the BLE path.
        if pkt_id is not None and req_id is not None and req_id != pkt_id:
            return
        routing = packet.get("decoded", {}).get("routing", {})
        error   = routing.get("errorReason", "NONE")
        ack_result["packet"]  = packet
        ack_result["elapsed"] = time.time() - send_time
        ack_result["error"]   = error
        ack_event.set()
        if error == "NONE":
            log.info(f"{_RX}◀◀ ACK from {dest_name} ({node_id}) [pubsub]{_RST}")
        else:
            log.warning(f"{_RX}◀◀ NAK from {dest_name} ({node_id}) [pubsub]: {error}{_RST}")

    _echo_topics = [
        "meshtastic.receive.text",
        "meshtastic.receive.position",
        "meshtastic.receive.user",
        "meshtastic.receive.telemetry",
        "meshtastic.receive.neighborinfo",
    ]
    _subs = [(t, pub.subscribe(_on_echo, t)) for t in _echo_topics]
    _subs.append(("meshtastic.receive.routing",
                  pub.subscribe(_on_routing_ack, "meshtastic.receive.routing")))

    # -- render -------------------------------------------------------------
    def _render():
        elapsed = time.time() - send_time
        pkt_str = f" #{pkt_id:08x}" if isinstance(pkt_id, int) else ""
        sep_dbl = "  " + "═" * 76

        lines = [
            f"  Outbound: DM to {dest_name}{pkt_str}  |  t+{elapsed:.1f}s",
            sep_dbl,
            "",
        ]

        preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
        lines.append(f"  ► Sent   \"{preview}\"  →  {dest_name}")

        # distance / route info
        cached = _route_cache.get(dest_num) if dest_num is not None else None
        if cached:
            age_s   = int(time.time() - cached["ts"])
            age_str = f"{age_s // 60}m {age_s % 60}s" if age_s >= 60 else f"{age_s}s"
            hops    = cached["hops"]
            if hops:
                _fmt_snr = lambda raw: "" if (raw is None or raw == _UNK_SNR) else f" ({raw / 4:+.1f}dB)"
                route_parts = (["[YOU]"]
                                + [f"[{h['name']}]{_fmt_snr(h['snr_raw'])}" for h in hops]
                                + [f"[{dest_name}]"])
                lines.append(f"    Route ({age_str} ago):  " + " → ".join(route_parts))
            else:
                lines.append(f"    Route ({age_str} ago):  [YOU] → [{dest_name}]  (direct)")
        elif known_hops == 0:
            lines.append(f"    [YOU] ────────────────────── [{dest_name}]  (direct link)")
        elif known_hops is not None:
            snr_sfx = f"  last SNR {known_snr:+.1f}dB" if known_snr is not None else ""
            if last_heard:
                age_s = int(time.time() - last_heard)
                age_sfx = f"  ·  {age_s // 60}m ago" if age_s >= 60 else f"  ·  {age_s}s ago"
            else:
                age_sfx = ""
            hops_vis = " ── ? " * known_hops
            lines.append(f"    [YOU]{hops_vis}── [{dest_name}]"
                         f"  ({known_hops} hop{'s' if known_hops != 1 else ''}{snr_sfx}{age_sfx})")
            lines.append(f"    Intermediate nodes unknown — run t{{n}} to trace  ·  hop count from last inbound packet, may be stale")
        else:
            lines.append(f"    [YOU] ── ? ── ... ── [{dest_name}]  (distance unknown)")
            lines.append(f"    Run t{{n}} to discover the route")
        lines.append("")

        # relay echo table
        lines.append("  ─── Relay echoes (re-broadcasts of our packet we could hear) ─────────────────")
        with _relay_lock:
            relays = list(_relay_events)

        if relays:
            lines.append(f"  {'Node':<22}  {'SNR':>5}  {'RSSI':>6}  {'hops':>4}  {'at':>6}")
            lines.append("  " + "─" * 52)
            for r in sorted(relays, key=lambda x: x["elapsed"]):
                snr_s  = f"{r['snr']:>+5.1f}"  if r["snr"]      is not None else "    —"
                rssi_s = f"{r['rssi']:>6}"      if r["rssi"]     is not None else "     —"
                hops_s = str(r["hops_used"])    if r["hops_used"] is not None else "—"
                at_s   = f"{r['elapsed']:.1f}s"
                lines.append(f"  {r['name']:<22}  {snr_s}  {rssi_s}  {hops_s:>4}  {at_s:>6}")
        else:
            if known_hops is not None and known_hops > 1:
                lines.append(f"  (none observed — expected: relay nodes are {known_hops} hops away,")
                lines.append( "   almost certainly beyond direct radio range of this device)")
            elif known_hops == 1:
                lines.append( "  (none observed — the single relay node may be just out of direct range,")
                lines.append( "   or the meshtastic library deduplicated the echo before delivery)")
            else:
                lines.append( "  (none observed — relay nodes may be out of direct earshot, or")
                lines.append( "   the meshtastic library deduplicates re-broadcasts before delivery)")
        lines.append("")

        # ACK / return path
        lines.append("  ─── ACK return path ────────────────────────────────────────────────────────")
        ack_pkt = ack_result["packet"]
        ack_el  = ack_result["elapsed"]
        ack_err = ack_result["error"]

        if ack_pkt is not None:
            if ack_err == "NONE":
                ack_snr  = ack_pkt.get("rxSnr")
                ack_rssi = ack_pkt.get("rxRssi")
                rb       = ack_pkt.get("relayNode")
                hs       = ack_pkt.get("hopStart")
                hl       = ack_pkt.get("hopLimit")
                ack_hops = (hs - hl) if (hs is not None and hl is not None) else None

                relay_name = None
                if rb:
                    relay_name = f"!..{rb:02x}"
                    for num, node in (getattr(iface, "nodesByNum", None) or {}).items():
                        if isinstance(num, int) and (num & 0xFF) == rb:
                            u = node.get("user", {})
                            relay_name = u.get("longName") or u.get("shortName") or f"!{num:08x}"
                            break

                snr_s  = f"  SNR {ack_snr:+.1f}dB"  if ack_snr  is not None else ""
                rssi_s = f"  RSSI {ack_rssi}dBm"     if ack_rssi is not None else ""
                hops_s = f"  {ack_hops} hop{'s' if ack_hops != 1 else ''}" if ack_hops is not None else ""
                via_s  = f"  via {relay_name}"        if relay_name else "  (direct)"

                lines.append(f"  t+{ack_el:.2f}s  \u2713 ACK from {dest_name}{via_s}{snr_s}{rssi_s}{hops_s}")

                if relay_name:
                    lines.append(f"  [{dest_name}] \u2500\u2500[{relay_name}]\u2500\u2500 [YOU]")
                    lines.append(f"  \u2192 Next-hop learned: {relay_name} will relay future DMs to {dest_name}")
                else:
                    lines.append(f"  [{dest_name}] \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 [YOU]  (direct)")
                    lines.append(f"  \u2192 Direct link confirmed \u2014 no relay needed")
            else:
                lines.append(f"  t+{ack_el:.2f}s  \u2717 NAK from {dest_name}: {ack_err}")
        else:
            if elapsed >= _ack_timeout:
                lines.append(f"  t+{elapsed:.0f}s  \u2717 No ACK received — delivery unconfirmed (mesh may still deliver)")
                lines.append(f"  (timeout after {_ack_timeout}s — {_hops_for_timeout} hop{'s' if _hops_for_timeout != 1 else ''} × 15s + 15s base)")
            else:
                remaining = int(_ack_timeout - elapsed)
                lines.append(f"  t+{elapsed:.1f}s  \u2026 waiting for ACK  (timeout in {remaining}s)")

        lines += ["", "  Press Enter to return to menu"]
        return lines

    # -- refresh worker & entry point ---------------------------------------
    def _refresh():
        while not _stop.wait(1.0):
            new_lines = _render()
            buf = "\0337"
            for i, line in enumerate(new_lines, 1):
                buf += f"\033[{i};1H\033[K{line}"
            buf += "\0338"
            sys.stdout.write(buf)
            sys.stdout.flush()

    clear_screen()
    for line in _render():
        print(line)

    _t = threading.Thread(target=_refresh, daemon=True)
    _t.start()
    try:
        input("")
    finally:
        _stop.set()
        _t.join(timeout=1)
        for topic, sub in _subs:
            try:
                pub.unsubscribe(sub, topic)
            except Exception:
                pass


def show_inflow_view(iface):
    """Live inflow traffic view — every received packet counted per source node."""
    _stop = threading.Event()
    BAR_W = 20

    def _render():
        with _inflow_lock:
            snap = {k: dict(v) for k, v in _inflow_data.items()}
        rows_data  = sorted(snap.items(), key=lambda x: x[1]["total"], reverse=True)
        total_pkts = sum(v["total"] for v in snap.values())
        elapsed    = int(time.time() - _inflow_start_ts)
        elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"
        max_total  = rows_data[0][1]["total"] if rows_data else 1

        h1  = (f"  Inflow View  —  {total_pkts} packet{'s' if total_pkts != 1 else ''} "
               f"from {len(rows_data)} node{'s' if len(rows_data) != 1 else ''}  "
               f"(session {elapsed_str})")
        sep = "  " + "─" * 86
        hdr = (f"  {'Via':<22}  {'Pkts':>5}  {'':<{BAR_W}}  "
               f"{'txt':>3} {'pos':>3} {'usr':>3} {'tel':>3} {'nb':>3} {'tr':>3}  "
               f"{'SNR':>5} {'RSSI':>6}  {'last':>6}")

        lines = [h1, sep, hdr, sep]

        for node_id, d in rows_data:
            bar_fill = int(d["total"] / max_total * BAR_W)
            bar      = "\u2588" * bar_fill + "\u2591" * (BAR_W - bar_fill)
            name     = d["name"][:22].ljust(22)
            types    = (f"{d['text']:>3} {d['position']:>3} {d['user']:>3} "
                        f"{d['telemetry']:>3} {d['neighborinfo']:>3} {d['traceroute']:>3}")
            ago_s    = int(time.time() - d["last_ts"])
            ago_str  = f"{ago_s // 60}m{ago_s % 60:02d}s" if ago_s >= 60 else f"{ago_s}s"
            sc = d["sig_count"]
            snr_str  = f"{d['snr_sum']  / sc:>4.1f}" if sc else "   —"
            rssi_str = f"{d['rssi_sum'] // sc:>4}"   if sc else "   —"
            lines.append(f"  {name}  {d['total']:>5}  {bar}  {types}  "
                         f"{snr_str}dB {rssi_str}dBm  {ago_str:>6}")

        if not rows_data:
            lines.append("  (no packets received yet — waiting...)")

        lines += ["", "  Press Enter to return to menu"]
        return lines

    def _refresh_worker():
        while not _stop.wait(1.0):
            lines = _render()
            buf = "\0337"
            for i, line in enumerate(lines, 1):
                buf += f"\033[{i};1H\033[K{line}"
            buf += "\0338"
            sys.stdout.write(buf)
            sys.stdout.flush()

    clear_screen()
    for line in _render():
        print(line)

    _t = threading.Thread(target=_refresh_worker, daemon=True)
    _t.start()
    try:
        input("")
    finally:
        _stop.set()
        _t.join(timeout=1)


def show_node_info(iface):
    """Print info about the connected node and visible mesh peers."""
    my_num = iface.myInfo.my_node_num
    metadata = iface.metadata
    node_name = iface.getLongName() or "Unknown"
    fw = metadata.firmware_version if metadata else "N/A"
    hw = metadata.hw_model if metadata else "N/A"
    try:
        from meshtastic.protobuf import mesh_pb2 as _mesh_pb2
        hw_display = _mesh_pb2.HardwareModel.Name(hw)
    except Exception:
        hw_display = str(hw)

    log.info(f"Local node: {node_name}  !{my_num:08x}  fw {fw}  hw {hw_display}")

    # Firmware update check — appended to header line, populated by background thread
    _fw_status = ["  |  fw: checking..."]

    nodes = iface.nodes or {}
    all_peers = [(k, v) for k, v in nodes.items() if k != my_num]
    favorites = [(k, v) for k, v in all_peers if v.get("isFavorite")]
    log.info(f"Peers visible: {len(all_peers)}  radio favorites: {len(favorites)}")

    # Merge extras from extra_favorites.json
    _fav_ids = {nid for nid, _ in favorites}
    for entry in _load_extra_favorites():
        raw_id = entry.get("id", "")
        try:
            node_id = int(raw_id.lstrip("!"), 16)
        except (ValueError, AttributeError):
            log.warning(f"extra_favorites: invalid id {raw_id!r}, skipping")
            continue
        if node_id in _fav_ids:
            continue  # already a favorite from the radio
        if node_id in nodes:
            favorites.append((node_id, nodes[node_id]))
        else:
            name = entry.get("name") or raw_id
            short = entry.get("short") or name[:4]
            favorites.append((node_id, {
                "user": {"id": raw_id, "longName": name, "shortName": short},
                "isFavorite": True,
            }))
        _fav_ids.add(node_id)
        log.info(f"Loaded extra favorite from favorites file: {entry.get('name', raw_id)} ({raw_id})")

    log.info(f"Total favorites: {len(favorites)}")

    _GREEN    = "\033[32m"
    _ORANGE   = "\033[33m"
    _RESET    = "\033[0m"
    ping_results = {}  # {node_id: bool} — populated after 'pf' command

    name_w = max((len(_peer_name(p)) for _, p in favorites), default=0)

    # ── receive watchdog ──────────────────────────────────────────────────
    _watchdog_stop = threading.Event()

    def _watchdog():
        global _last_rx_time
        _last_rx_time = time.time()   # reset on session start
        while not _watchdog_stop.wait(60):
            silent = time.time() - _last_rx_time
            if silent >= _RX_WATCHDOG_SECS:
                log.warning(
                    f"No packets received for {int(silent)}s — "
                    "BLE notifications may have stalled (hw issue on connected node)"
                )

    _watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    _watchdog_thread.start()

    from pubsub import pub as _pub

    # detect unexpected BLE disconnect
    _disconnected = threading.Event()

    def _on_connection_lost(interface):
        if not _disconnected.is_set():
            _disconnected.set()
            log.warning("BLE connection lost unexpectedly")
            # Print a visible alert without disturbing the cursor position
            sys.stdout.write(
                f"\n\033[31m  !! BLE connection lost — press Enter to exit\033[0m\n"
            )
            sys.stdout.flush()

    _pub.subscribe(_on_connection_lost, "meshtastic.connection.lost")

    # ── firmware check (background, so GitHub API call doesn't block startup) ──
    def _fw_check_worker():
        try:
            result = FirmwareChecker.check_firmware_update(fw)
            msg = FirmwareChecker.format_update_message(result, verbose=False)
        except Exception:
            msg = "⚠  check failed"
        _fw_status[0] = f"  |  {msg}"
        # Repaint rows 1-3 (separator / header / separator) in-place
        h   = f"{node_name}  |  !{my_num:08x}  |  hw {hw_display}{_fw_status[0]}"
        sep = "=" * len(h)
        buf = ("\0337"
               + f"\033[1;1H\033[K{sep}"
               + f"\033[2;1H\033[K{h}"
               + f"\033[3;1H\033[K{sep}"
               + "\0338")
        sys.stdout.write(buf)
        sys.stdout.flush()

    threading.Thread(target=_fw_check_worker, daemon=True).start()

    # ── main display / command loop ───────────────────────────────────────
    def print_main():
        clear_screen()
        h = f"{node_name}  |  !{my_num:08x}  |  hw {hw_display}{_fw_status[0]}"
        print("=" * len(h))
        print(h)
        print("=" * len(h))
        if not favorites:
            print("\nNo favorite nodes visible yet.")
            return
        print(f"\nFavorite peers: {len(favorites)} of {len(all_peers)} visible\n")
        for i, (node_id, p) in enumerate(favorites, 1):
            name = _peer_name(p).ljust(name_w)
            last = _ago(p.get("lastHeard"))
            snr  = p.get("snr", "N/A")
            hops = p.get("hopsAway")
            hops_str = "direct" if hops == 0 else f"{hops} hops" if hops is not None else "N/A"
            if node_id in ping_results:
                dot = f"{_GREEN}●{_RESET}" if ping_results[node_id] else f"{_ORANGE}●{_RESET}"
                print(f"  [{i}] {dot} {name}  {last:<12}  SNR: {str(snr):<6}  {hops_str}")
            else:
                print(f"  [{i}]   {name}  {last:<12}  SNR: {str(snr):<6}  {hops_str}")
        c = 22
        print(f"\n  {'d<n> Node Details'.ljust(c)}{'m<n> Message'.ljust(c)}{'t<n> tracer'.ljust(c)}{'i   Inflow View'.ljust(c)}Enter to quit"
              f"\n  {'pf  Ping Favorites'.ljust(c)}{'r<n> Repeat msg'.ljust(c)}{'rt<n> Repeat trace'.ljust(c)}e Export config")

    print_main()

    if not favorites:
        return

    try:
        while True:
            if _disconnected.is_set():
                break
            choice = input("  > ").strip().lower()
            if _disconnected.is_set():
                break
            if not choice:
                confirm = input("  Really quit? [y/N]: ").strip().lower()
                if confirm == "y":
                    return
                print_main()
                continue
            if choice == "e":
                clear_screen()
                export_node_config(iface)
                print_main()
                continue
            if choice == "pf":
                clear_screen()
                results = ping_favorites(iface, favorites)
                ping_results.update(results)
                print_main()
                continue
            if choice == "i":
                clear_screen()
                show_inflow_view(iface)
                print_main()
                continue
            if choice[:2] == "rt":
                action, num = "rt", choice[2:]
            elif choice[0] in ("d", "m", "r", "t"):
                action, num = choice[0], choice[1:]
            else:
                action, num = "d", choice
            if num.isdigit() and 1 <= int(num) <= len(favorites):
                node_id, peer = favorites[int(num) - 1]
                if action == "d":
                    clear_screen()
                    show_peer_detail(peer)
                    input("\n  Press Enter to continue...")
                    print_main()
                elif action == "m":
                    clear_screen()
                    send_message(iface, node_id, _peer_name(peer))
                    print_main()
                elif action == "r":
                    clear_screen()
                    send_repeated(iface, node_id, _peer_name(peer))
                    print_main()
                elif action == "t":
                    clear_screen()
                    tracer_node(iface, node_id, _peer_name(peer))
                    print_main()
                elif action == "rt":
                    clear_screen()
                    tracer_repeated(iface, node_id, _peer_name(peer))
                    print_main()
            else:
                print("  Invalid — try d1, m2, r3, t4, rt4, or Enter to quit.")
    finally:
        _watchdog_stop.set()
        try:
            _pub.unsubscribe(_on_connection_lost, "meshtastic.connection.lost")
        except Exception:
            pass


def show_peer_detail(peer):
    """Show detailed info for a selected peer and log all available fields."""
    user    = peer.get("user", {})
    name    = _peer_name(peer)
    pos     = peer.get("position") or {}
    metrics = peer.get("deviceMetrics") or {}
    snr     = peer.get("snr")
    hops    = peer.get("hopsAway")
    rssi    = peer.get("rxRssi")
    last    = peer.get("lastHeard")

    # --- log everything available ----------------------------------------
    log.info(f"Node detail: {name}")
    log.info(f"  id={user.get('id','N/A')}  short={user.get('shortName','N/A')}  "
             f"hw={user.get('hwModel','N/A')}  role={user.get('role','N/A')}")
    log.info(f"  lastHeard={last}  snr={snr}dB  rssi={rssi}  hopsAway={hops}")
    if pos:
        lat  = pos.get("latitude")
        lon  = pos.get("longitude")
        alt  = pos.get("altitude")
        sats = pos.get("satsInView")
        ptime= pos.get("time")
        log.info(f"  pos: lat={lat} lon={lon} alt={alt}m sats={sats} time={ptime}")
    if metrics:
        log.info(
            f"  metrics: battery={metrics.get('batteryLevel')}%  "
            f"voltage={metrics.get('voltage')}V  "
            f"chUtil={metrics.get('channelUtilization')}%  "
            f"airTx={metrics.get('airUtilTx')}%  "
            f"uptime={metrics.get('uptimeSeconds')}s"
        )
    env = peer.get("environmentMetrics") or {}
    if env:
        log.info(
            f"  env: temp={env.get('temperature')}°C  "
            f"humidity={env.get('relativeHumidity')}%  "
            f"pressure={env.get('barometricPressure')}hPa"
        )

    # --- screen display ---------------------------------------------------
    hops_str = "direct" if hops == 0 else str(hops) if hops is not None else "N/A"
    print("\n" + "=" * 50)
    print(f"NODE: {name}")
    print("=" * 50)
    print(f"  ID         : {user.get('id', 'N/A')}")
    print(f"  Short name : {user.get('shortName', 'N/A')}")
    print(f"  Hardware   : {user.get('hwModel', 'N/A')}")
    print(f"  Role       : {user.get('role', 'N/A')}")
    print(f"  Last seen  : {_ago(last)}")
    if snr is not None:
        snr_time = time.strftime("%H:%M %d.%m.%Y", time.localtime(last)) if last else "unknown"
        print(f"  SNR        : {snr} dB  (seen {snr_time})")
    if rssi is not None:
        print(f"  RSSI       : {rssi} dBm")
    print(f"  Hops away  : {hops_str}")
    if pos.get("latitude"):
        print(f"  Position   : {pos['latitude']:.5f}, {pos['longitude']:.5f}", end="")
        if pos.get("altitude"):
            print(f"  alt {pos['altitude']} m", end="")
        print()
    if metrics.get("batteryLevel") is not None:
        print(f"  Battery    : {metrics['batteryLevel']}%")
    if metrics.get("voltage") is not None:
        print(f"  Voltage    : {metrics['voltage']:.2f} V")
    if metrics.get("channelUtilization") is not None:
        print(f"  Ch util    : {metrics['channelUtilization']:.1f}%")
    if metrics.get("airUtilTx") is not None:
        print(f"  Air TX     : {metrics['airUtilTx']:.1f}%")
    if env.get("temperature") is not None:
        print(f"  Temp       : {env['temperature']:.1f} °C")
    if env.get("relativeHumidity") is not None:
        print(f"  Humidity   : {env['relativeHumidity']:.1f}%")


def export_node_config(iface):
    """Export the connected node's config (localConfig, moduleConfig, channels) to a JSON file."""
    from google.protobuf.json_format import MessageToDict

    node = iface.localNode
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        node_name = (iface.getLongName() or "unknown").replace(" ", "_")
    except Exception:
        node_name = "unknown"
    filename = f"{node_name}_{timestamp}_config.json"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    config = {}
    try:
        if node.localConfig:
            config["localConfig"] = MessageToDict(node.localConfig)
        if node.moduleConfig:
            config["moduleConfig"] = MessageToDict(node.moduleConfig)
        if node.channels:
            config["channels"] = [MessageToDict(ch) for ch in node.channels]
    except Exception as e:
        log.exception(f"export_node_config: failed to serialize config: {e}")
        print(f"  [WARN] Config export failed (serialization): {e}")
        return

    try:
        with open(filepath, "w") as f:
            json.dump(config, f, indent=2)
        log.info(f"Node config exported to {filepath}")
        print(f"  Config exported: {filename}")
    except OSError as e:
        log.error(f"export_node_config: failed to write {filepath}: {e}")
        print(f"  [WARN] Config export failed (write): {e}")


def _ensure_disconnected(address):
    """On Linux, drop an existing BLE connection before we try to connect ourselves.

    A device that is still marked as Connected in the BlueZ stack will cause
    meshtastic's _waitConnected() to time out after ~60 s.
    """
    if sys.platform != "linux":
        return
    try:
        info = subprocess.run(
            ["bluetoothctl", "info", address],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if "Connected: yes" not in info.stdout:
        return
    log.info(f"Device {address} still connected — disconnecting before re-connecting")
    print("  Device is still connected from a previous session — disconnecting first...")
    try:
        subprocess.run(
            ["bluetoothctl", "disconnect", address],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(1)  # give BlueZ a moment to settle
        log.info(f"Disconnected {address}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning(f"Could not disconnect {address}: {e}")


def main():
    preflight_check()
    print("\033[2J\033[H", end="", flush=True)
    print("!! Disconnect your phones and nodes as we need to see/scan them..\n")
    devices = find_known_devices()

    if devices:
        print(f"\nFound {len(devices)} known Meshtastic device(s):")
        for d in devices:
            print(f"  {d.name or 'Unknown'}  |  {d.address}")
        ans = input("\nScan for additional devices? [y/N]: ").strip().lower()
        if ans == "y":
            scanned = scan()
            known_addresses = {d.address for d in devices}
            devices += [d for d in scanned if d.address not in known_addresses]
    else:
        devices = scan()

    if not devices:
        log.error("No Meshtastic devices found — aborting")
        print("No Meshtastic devices found.")
        print("Make sure Bluetooth is enabled and the device is powered on.")
        sys.exit(1)

    device = pick_device(devices)
    _ensure_disconnected(device.address)

    # Spinner runs in background; BLE connection stays on main thread
    _spin_done = threading.Event()

    def _spinner():
        frames = "|/-\\"
        i = 0
        elapsed = 0.0
        while not _spin_done.wait(0.1):
            elapsed += 0.1
            print(f"\r  Connecting to {device.name or device.address}... (might take up to 40 sec) "
                  f"{frames[i % 4]}  {elapsed:.1f}s", end="", flush=True)
            i += 1

    _spin_thread = threading.Thread(target=_spinner, daemon=True)
    _spin_thread.start()

    log.info(f"Connecting to {device.name} [{device.address}]")
    try:
        iface = _FastBLEInterface(device.address)
    except BLEInterface.BLEError as e:
        _spin_done.set()
        _spin_thread.join()
        log.error(f"Connection failed: {e}")
        print(f"\n  Connection failed: {e}")
        sys.exit(1)
    except Exception as e:
        _spin_done.set()
        _spin_thread.join()
        log.exception(f"Unexpected error during connection: {e}")
        print(f"\n  Connection failed: {e}")
        sys.exit(1)
    finally:
        _spin_done.set()
        _spin_thread.join()

    log.info(f"Connected to {device.name} [{device.address}]")
    print(f"\r  Connected to {device.name or device.address}.{' ' * 20}")

    start_log_tail()
    start_message_log(iface)
    try:
        show_node_info(iface)
    except Exception as e:
        log.exception(f"Unexpected error in session: {e}")
        raise
    finally:
        stop_message_log()
        stop_log_tail()
        log.info("Closing connection")
        print("\nClosing connection...")
        t = threading.Thread(target=iface.close, daemon=True)
        t.start()
        t.join(timeout=3)
        log.info("=== newscan session ended ===")
        print("Node Disconnected. Bye!")
        sys.stdout.flush()
        os._exit(0)  # bypass meshtastic's atexit disconnect handler which blocks indefinitely


if __name__ == "__main__":
    main()
