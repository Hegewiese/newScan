#!/usr/bin/env python3
"""
Meshtastic BLE scanner — discover devices and connect to one.
Copyright by me
"""

import logging
import os
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
    try:
        iface.sendText(f"[{time.strftime('%H:%M:%S')}] {text}", destinationId=node_id, wantAck=True)
        log.info(f"Message sent to {name} ({node_id}): {text!r}")
        print("  Sent.")
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
            try:
                iface.sendText(f"[{time.strftime('%H:%M:%S')}] {text}", destinationId=node_id, wantAck=True)
                count += 1
                log.info(f"Repeated message #{count} sent to {name} ({node_id})")
                print(f"\r  Sent #{count} to {name}. Press Enter to stop.", end="", flush=True)
            except Exception as e:
                log.exception(f"Repeated message #{count + 1} to {name} ({node_id}) failed: {e}")
                print(f"\r  Send #{count + 1} failed: {e}")
            stop.wait(interval)

    t = threading.Thread(target=_send_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join()
    log.info(f"Repeated message to {name} ({node_id}) stopped")
    print(f"  Stopped.")


TRACEROUTE_TIMEOUT = 30  # seconds


def _traceroute_with_bar(iface, node_id, hop_limit):
    """Run sendTraceRoute in a thread and show a progress bar while waiting.

    The library prints the route result itself when the response arrives.
    Returns (success: bool, error: str|None).
    """
    result = {"ok": None, "error": None}
    done = threading.Event()

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

    print()  # end the progress bar line
    done.wait(timeout=1)  # brief grace period for the thread to finish

    if result["ok"] is True:
        return True, None
    elif result["ok"] is False:
        return False, result["error"]
    else:
        return False, f"timed out after {TRACEROUTE_TIMEOUT}s"


def traceroute_node(iface, node_id, name):
    """Send a single traceroute to the given node and print the result."""
    print(f"\n  Traceroute to {name}")
    hop_str = input("  Hop limit [3]: ").strip()
    hop_limit = int(hop_str) if hop_str.isdigit() and 1 <= int(hop_str) <= 7 else 3
    log.info(f"Traceroute to {name} ({node_id}), hop_limit={hop_limit}")
    print()
    success, err = _traceroute_with_bar(iface, node_id, hop_limit)
    if success:
        log.info(f"Traceroute to {name} ({node_id}) completed successfully")
        print("  Success.")
    else:
        log.error(f"Traceroute to {name} ({node_id}) failed: {err}")
        print(f"  Failed: {err}")


def traceroute_repeated(iface, node_id, name):
    """Send a traceroute repeatedly at a configurable interval until Enter is pressed."""
    print(f"\n  Repeated traceroute to {name}")
    hop_str = input("  Hop limit [3]: ").strip()
    hop_limit = int(hop_str) if hop_str.isdigit() and 1 <= int(hop_str) <= 7 else 3
    interval_str = input("  Interval in seconds [30]: ").strip()
    interval = int(interval_str) if interval_str.isdigit() else 30
    log.info(f"Repeated traceroute to {name} ({node_id}) started: hop_limit={hop_limit}, interval={interval}s")

    stop = threading.Event()
    last_result = {"count": 0, "success": None}

    def _trace_loop():
        while not stop.is_set():
            last_result["count"] += 1
            count = last_result["count"]
            print(f"\n  --- Traceroute #{count} to {name} ---")
            success, err = _traceroute_with_bar(iface, node_id, hop_limit)
            last_result["success"] = success
            if success:
                log.info(f"Repeated traceroute #{count} to {name} ({node_id}) completed")
            else:
                log.error(f"Repeated traceroute #{count} to {name} ({node_id}) failed: {err}")
                print(f"  Failed: {err}")
            if not stop.is_set():
                print(f"  Next in {interval}s — press Enter to stop.")
            stop.wait(interval)

    t = threading.Thread(target=_trace_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join(timeout=3)
    log.info(f"Repeated traceroute to {name} ({node_id}) stopped")
    if last_result["success"] is True:
        print(f"  Stopped. Last traceroute #{last_result['count']}: success.")
    elif last_result["success"] is False:
        print(f"  Stopped. Last traceroute #{last_result['count']}: failed.")
    else:
        print("  Stopped.")


def show_node_info(iface):
    """Print info about the connected node and visible mesh peers."""
    my_num = iface.myInfo.my_node_num
    metadata = iface.metadata

    log.info(f"Local node number: {my_num}")
    if metadata:
        log.info(f"Firmware: {metadata.firmware_version}  Hardware: {metadata.hw_model}")

    print("\n" + "=" * 50)
    print("LOCAL NODE")
    print("=" * 50)
    print(f"Node number : {my_num}")
    if metadata:
        print(f"Firmware    : {metadata.firmware_version}")
        print(f"Hardware    : {metadata.hw_model}")

    nodes = iface.nodes or {}
    all_peers = [(k, v) for k, v in nodes.items() if k != my_num]
    favorites = [(k, v) for k, v in all_peers if v.get("isFavorite")]
    log.info(f"Peers visible: {len(all_peers)}  favorites: {len(favorites)}")

    if not favorites:
        log.info("No favorite nodes visible")
        print("\nNo favorite nodes visible yet.")
        return

    def print_favorites():
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
        print(f"\n  d<n> Details   m<n> Message   r<n> Repeat msg   t<n> Traceroute   rt<n> Repeat trace   Enter to quit")

    print_favorites()

    while True:
        choice = input("  > ").strip().lower()
        if not choice:
            return
        if choice[:2] == "rt":
            action, num = "rt", choice[2:]
        elif choice[0] in ("d", "m", "r", "t"):
            action, num = choice[0], choice[1:]
        else:
            action, num = "d", choice
        if num.isdigit() and 1 <= int(num) <= len(favorites):
            node_id, peer = favorites[int(num) - 1]
            if action == "d":
                show_peer_detail(peer)
                print()
                print_favorites()
            elif action == "m":
                send_message(iface, node_id, _peer_name(peer))
                print()
                print_favorites()
            elif action == "r":
                send_repeated(iface, node_id, _peer_name(peer))
                print()
                print_favorites()
            elif action == "t":
                traceroute_node(iface, node_id, _peer_name(peer))
                print()
                print_favorites()
            elif action == "rt":
                traceroute_repeated(iface, node_id, _peer_name(peer))
                print()
                print_favorites()
        else:
            print("  Invalid — try d1, m2, r3, t4, rt4, or Enter to quit.")


def show_peer_detail(peer):
    """Show detailed info for a selected peer."""
    user = peer.get("user", {})
    name = _peer_name(peer)
    log.info(f"Viewing details for peer: {name} (id={user.get('id', 'N/A')})")

    print("\n" + "=" * 50)
    print(f"NODE: {name}")
    print("=" * 50)
    print(f"  ID         : {user.get('id', 'N/A')}")
    print(f"  Short name : {user.get('shortName', 'N/A')}")
    print(f"  Last seen  : {_ago(peer.get('lastHeard'))}")
    snr = peer.get("snr")
    if snr is not None:
        print(f"  SNR        : {snr} dB")
    hops = peer.get("hopsAway")
    print(f"  Hops away  : {'direct' if hops == 0 else hops if hops is not None else 'N/A'}")
    pos = peer.get("position") or {}
    if pos.get("latitude"):
        print(f"  Position   : {pos['latitude']:.5f}, {pos['longitude']:.5f}")
    metrics = peer.get("deviceMetrics") or {}
    if metrics.get("batteryLevel") is not None:
        print(f"  Battery    : {metrics['batteryLevel']}%")
    if metrics.get("voltage") is not None:
        print(f"  Voltage    : {metrics['voltage']:.2f} V")


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

    try:
        show_node_info(iface)
    except Exception as e:
        log.exception(f"Unexpected error in session: {e}")
        raise
    finally:
        log.info("Closing connection")
        print("\nClosing connection...")
        t = threading.Thread(target=iface.close, daemon=True)
        t.start()
        t.join(timeout=3)
        log.info("=== newscan session ended ===")
        print("Node Disconnected. Bye!")


if __name__ == "__main__":
    main()
