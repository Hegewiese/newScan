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
        row = sep_row + 1 + i
        text = lines[i] if i < len(lines) else ""
        buf += f"\033[{row};1H\033[K{text[:cols]}"
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
_rx_subs: list = []   # list of (callback, topic) registered by start_message_log

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
        for num, node in (interface.nodes or {}).items():
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

    # ── text messages ──────────────────────────────────────────────────────
    def _on_text(packet, interface):
        try:
            nodes      = interface.nodes or {}
            sender_num = packet.get("from")
            to_num     = packet.get("to", 0)
            sender     = _rx_resolve(interface,sender_num)
            dest       = "broadcast" if to_num == BROADCAST else _rx_resolve(interface,to_num)
            relay      = _rx_relay(interface, packet)
            via        = f"  {relay}" if relay else ""
            ch         = _ch_label(interface, packet.get("channel", 0))
            text       = packet.get("decoded", {}).get("text", "")
            icon       = "\033[31m✉\033[0m" if to_num != BROADCAST else "✉"
            log.info(f"{icon} {ch}  {sender}{via} -> {dest}: {text!r}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX text log error: {e}")

    # ── position ───────────────────────────────────────────────────────────
    def _on_position(packet, interface):
        try:
            nodes  = interface.nodes or {}
            sender = _rx_resolve(interface,packet.get("from"))
            relay  = _rx_relay(interface, packet)
            via    = f"  {relay}" if relay else ""
            ch     = _ch_label(interface, packet.get("channel", 0))
            pos    = packet.get("decoded", {}).get("position", {})
            parts  = []
            if "latitudeI"  in pos: parts.append(f"lat={pos['latitudeI']/1e7:.5f}")
            if "longitudeI" in pos: parts.append(f"lon={pos['longitudeI']/1e7:.5f}")
            if pos.get("altitude"):  parts.append(f"alt={pos['altitude']}m")
            if pos.get("satsInView"):parts.append(f"sats={pos['satsInView']}")
            detail = "  ".join(parts) if parts else "no fix"
            log.info(f"⊕ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX position log error: {e}")

    # ── node info ──────────────────────────────────────────────────────────
    def _on_user(packet, interface):
        try:
            nodes  = interface.nodes or {}
            sender = _rx_resolve(interface,packet.get("from"))
            relay  = _rx_relay(interface, packet)
            via    = f"  {relay}" if relay else ""
            ch     = _ch_label(interface, packet.get("channel", 0))
            u      = packet.get("decoded", {}).get("user", {})
            detail = (f"long={u.get('longName')}  short={u.get('shortName')}  "
                      f"hw={u.get('hwModel')}  role={u.get('role')}")
            log.info(f"◉ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX nodeinfo log error: {e}")

    # ── telemetry ──────────────────────────────────────────────────────────
    def _on_telemetry(packet, interface):
        try:
            nodes  = interface.nodes or {}
            sender = _rx_resolve(interface,packet.get("from"))
            relay  = _rx_relay(interface, packet)
            via    = f"  {relay}" if relay else ""
            ch     = _ch_label(interface, packet.get("channel", 0))
            t      = packet.get("decoded", {}).get("telemetry", {})
            dm     = t.get("deviceMetrics", {})
            em     = t.get("environmentMetrics", {})
            parts  = []
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
            log.info(f"{icon} {ch}  {sender}{via}: {detail}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX telemetry log error: {e}")

    # ── neighbor info ──────────────────────────────────────────────────────
    def _on_neighborinfo(packet, interface):
        try:
            nodes     = interface.nodes or {}
            sender    = _rx_resolve(interface,packet.get("from"))
            relay     = _rx_relay(interface, packet)
            via       = f"  {relay}" if relay else ""
            ch        = _ch_label(interface, packet.get("channel", 0))
            ni        = packet.get("decoded", {}).get("neighborinfo", {})
            neighbors = ni.get("neighbors", [])
            nb_names  = [_rx_resolve(interface,nb.get("nodeId")) for nb in neighbors[:6]]
            detail    = f"{len(neighbors)} neighbors: {', '.join(nb_names)}" if nb_names else "0 neighbors"
            log.info(f"⬡ {ch}  {sender}{via}: {detail}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX neighborinfo log error: {e}")

    # ── traceroute received from others ────────────────────────────────────
    def _on_traceroute_rx(packet, interface):
        try:
            my_num = interface.myInfo.my_node_num
            if packet.get("to") == my_num and packet.get("from") != my_num:
                return   # this is a response to our own tracer — already logged
            nodes  = interface.nodes or {}
            sender = _rx_resolve(interface,packet.get("from"))
            dest   = _rx_resolve(interface,packet.get("to"))
            relay  = _rx_relay(interface, packet)
            via    = f"  {relay}" if relay else ""
            ch     = _ch_label(interface, packet.get("channel", 0))
            log.info(f"⇌ {ch}  {sender}{via} -> {dest}{_rx_sig(packet)}")
        except Exception as e:
            log.warning(f"RX traceroute log error: {e}")

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
    _rx_subs = subs


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
    """Prompt for a message and send it to the given node."""
    print(f"\n  Sending message to {name}")
    text = input("  Message: ").strip()
    if not text:
        log.info(f"send_message to {name} ({node_id}): cancelled (empty input)")
        print("  Cancelled.")
        return

    def onAckNak(packet):
        routing = packet.get("decoded", {}).get("routing", {})
        error = routing.get("errorReason", "NONE")
        if error == "NONE":
            log.info(f"ACK received from {name} ({node_id})")
            print(f"\n  ACK received from {name}.")
        else:
            log.warning(f"NAK from {name} ({node_id}): {error}")
            print(f"\n  NAK from {name}: {error}")

    try:
        iface.sendText(f"[{time.strftime('%H:%M:%S')}] {text}", destinationId=node_id, wantAck=True, onResponse=onAckNak)
        log.info(f"{_ch_label(iface, 0)}  Message sent to {name} ({node_id}): {text!r}  \033[31m✉\033[0m")
        print("  Sent. (Waiting for ACK...)")
    except Exception as e:
        log.exception(f"send_message to {name} ({node_id}) failed: {e}")
        print(f"  Failed: {e}")


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
                    log.info(f"ACK received for repeated message #{_c} from {name} ({node_id})")
                    print(f"\n  ACK #{_c} from {name}.", flush=True)
                else:
                    log.warning(f"NAK for repeated message #{_c} from {name} ({node_id}): {error}")
                    print(f"\n  NAK #{_c} from {name}: {error}", flush=True)

            try:
                iface.sendText(f"[{time.strftime('%H:%M:%S')}] {text}", destinationId=node_id, wantAck=True, onResponse=onAckNak)
                log.info(f"{_ch_label(iface, 0)}  Repeated message #{current} sent to {name} ({node_id})  \033[31m✉\033[0m")
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
_UNK_SNR = -128  # meshtastic sentinel for unknown SNR


def _log_tracer_details(packet: dict, name: str) -> None:
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
    log.info(f"tracer [{name}] towards:  {' --> '.join(parts)}")

    # --- back route (only if present) -----------------------------------
    if route_back or snr_back:
        bp = [_n(frm)]
        for i, n in enumerate(route_back):
            snr = _snr(snr_back[i]) if i < len(snr_back) else "?"
            bp.append(f"{_n(n)}({snr})")
        last_snr_back = _snr(snr_back[-1]) if snr_back else "?"
        bp.append(f"{_n(to)}({last_snr_back})")
        log.info(f"tracer [{name}] back:     {' --> '.join(bp)}")

    # --- packet-level metrics -------------------------------------------
    extras = [f"pktId={pkt_id}"]
    if rx_snr  is not None: extras.append(f"rxSNR={rx_snr}dB")
    if rx_rssi is not None: extras.append(f"rxRSSI={rx_rssi}dBm")
    if hop_start is not None: extras.append(f"hopStart={hop_start}")
    if hop_limit is not None: extras.append(f"hopLimit={hop_limit}")
    log.info(f"tracer [{name}] metrics:  {', '.join(extras)}")


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
        print(f"\r  Waiting for route... [{bar}] {i * tick:4.1f}s", end="", flush=True)
        if done.is_set():
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
        _log_tracer_details(captured[0], name)

    if result["ok"] is True:
        return True, None
    elif result["ok"] is False:
        return False, result["error"]
    else:
        return False, f"timed out after {TRACEROUTE_TIMEOUT}s"


def tracer_node(iface, node_id, name):
    """Send a single tracer to the given node and print the result."""
    print(f"\n  tracer to {name}")
    hop_str = input("  Hop limit [3]: ").strip()
    hop_limit = int(hop_str) if hop_str.isdigit() and 1 <= int(hop_str) <= 7 else 3
    log.info(f"tracer to {name} ({node_id}), hop_limit={hop_limit}")
    print()
    success, err = _tracer_with_bar(iface, node_id, hop_limit, name=name)
    if success:
        log.info(f"tracer to {name} ({node_id}) completed successfully")
        print("  Success.")
    else:
        log.error(f"tracer to {name} ({node_id}) failed: {err}")
        print(f"  Failed: {err}")


def tracer_repeated(iface, node_id, name):
    """Send a tracer repeatedly at a configurable interval until Enter is pressed."""
    print(f"\n  Repeated tracer to {name}")
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
            print(f"\n  --- tracer #{count} to {name} ---")
            success, err = _tracer_with_bar(iface, node_id, hop_limit, name=name)
            last_result["success"] = success
            if success:
                log.info(f"Repeated tracer #{count} to {name} ({node_id}) completed")
            else:
                log.error(f"Repeated tracer #{count} to {name} ({node_id}) failed: {err}")
                print(f"  Failed: {err}")
            if not stop.is_set():
                print(f"  Next in {interval}s — press Enter to stop.")
            stop.wait(interval)

    t = threading.Thread(target=_trace_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join(timeout=3)
    log.info(f"Repeated tracer to {name} ({node_id}) stopped")
    if last_result["success"] is True:
        print(f"  Stopped. Last tracer #{last_result['count']}: success.")
    elif last_result["success"] is False:
        print(f"  Stopped. Last tracer #{last_result['count']}: failed.")
    else:
        print("  Stopped.")


def show_node_info(iface):
    """Print info about the connected node and visible mesh peers."""
    my_num = iface.myInfo.my_node_num
    metadata = iface.metadata
    node_name = iface.getLongName() or "Unknown"
    fw = metadata.firmware_version if metadata else "N/A"
    hw = metadata.hw_model if metadata else "N/A"

    log.info(f"Local node: {node_name}  !{my_num:08x}  fw {fw}  hw {hw}")

    nodes = iface.nodes or {}
    all_peers = [(k, v) for k, v in nodes.items() if k != my_num]
    favorites = [(k, v) for k, v in all_peers if v.get("isFavorite")]
    log.info(f"Peers visible: {len(all_peers)}  favorites: {len(favorites)}")

    header = f"{node_name}  |  !{my_num:08x}  |  fw {fw}  |  hw {hw}"

    def print_main():
        clear_screen()
        print("=" * len(header))
        print(header)
        print("=" * len(header))
        if not favorites:
            print("\nNo favorite nodes visible yet.")
            return
        names = [_peer_name(p) for _, p in favorites]
        name_w = max(len(n) for n in names)
        print(f"\nFavorite peers: {len(favorites)} of {len(all_peers)} visible\n")
        for i, (_, p) in enumerate(favorites, 1):
            name = _peer_name(p).ljust(name_w)
            last = _ago(p.get("lastHeard"))
            snr  = p.get("snr", "N/A")
            hops = p.get("hopsAway")
            hops_str = "direct" if hops == 0 else f"{hops} hops" if hops is not None else "N/A"
            print(f"  [{i}] {name}  {last:<12}  SNR: {str(snr):<6}  {hops_str}")
        c = 22
        print(f"\n  {'d<n> Node Details'.ljust(c)}{'m<n> Message'.ljust(c)}{'t<n> tracer'.ljust(c)}Enter to quit"
              f"\n  {''.ljust(c)}{'r<n> Repeat msg'.ljust(c)}{'rt<n> Repeat trace'.ljust(c)}e Export config")

    print_main()

    if not favorites:
        return

    while True:
        choice = input("  > ").strip().lower()
        if not choice:
            return
        if choice == "e":
            clear_screen()
            export_node_config(iface)
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


if __name__ == "__main__":
    main()
