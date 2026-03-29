#!/usr/bin/env python3
"""
Meshtastic BLE scanner — discover devices and connect to one.
"""

import subprocess
import sys
import threading
import time

try:
    import meshtastic.ble_interface
    from meshtastic.ble_interface import BLEInterface, BLEClient, SERVICE_UUID
    from bleak import BleakScanner
except ImportError:
    print("Error: meshtastic not installed. Run: pip install meshtastic bleak")
    sys.exit(1)


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
    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
            continue
        if SERVICE_UUID.lower() in info.stdout.lower():
            devices.append(_Device(name, address))
    return devices


def scan():
    """Return a list of nearby Meshtastic BLE devices, with a progress bar."""
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
    return result


def pick_device(devices):
    """Print device list and let the user choose one. Returns the chosen BLEDevice."""
    print()
    for i, d in enumerate(devices, 1):
        print(f"  [{i}] {d.name or 'Unknown'}  |  {d.address}")

    while True:
        choice = input("\nSelect device (number) [1]: ").strip()
        if not choice:
            return devices[0]
        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            return devices[int(choice) - 1]
        print("Invalid choice, try again.")


def _ago(ts):
    """Format a Unix timestamp as a human-readable 'X ago' string."""
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
        print("  Cancelled.")
        return
    try:
        iface.sendText(text, destinationId=node_id, wantAck=True)
        print("  Sent.")
    except Exception as e:
        print(f"  Failed: {e}")


def send_repeated(iface, node_id, name):
    """Prompt for a message and interval, then send repeatedly until Enter is pressed."""
    print(f"\n  Repeated message to {name}")
    text = input("  Message: ").strip()
    if not text:
        print("  Cancelled.")
        return
    interval_str = input("  Interval in seconds [10]: ").strip()
    interval = int(interval_str) if interval_str.isdigit() else 10

    stop = threading.Event()

    def _send_loop():
        count = 0
        while not stop.is_set():
            try:
                iface.sendText(text, destinationId=node_id, wantAck=True)
                count += 1
                print(f"\r  Sent #{count} to {name}. Press Enter to stop.", end="", flush=True)
            except Exception as e:
                print(f"\r  Send #{count + 1} failed: {e}")
            stop.wait(interval)

    t = threading.Thread(target=_send_loop, daemon=True)
    t.start()
    input()
    stop.set()
    t.join()
    print(f"  Stopped.")


def show_node_info(iface):
    """Print info about the connected node and visible mesh peers."""
    my_num = iface.myInfo.my_node_num
    metadata = iface.metadata

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

    if not favorites:
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
        print(f"\n  d<n> Details   m<n> Message   r<n> Repeat msg   Enter to quit")

    print_favorites()

    while True:
        choice = input("  > ").strip().lower()
        if not choice:
            return
        action, num = (choice[0], choice[1:]) if choice[0] in ("d", "m", "r") else ("d", choice)
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
        else:
            print("  Invalid — try d1, m2, r3, or Enter to quit.")


def show_peer_detail(peer):
    """Show detailed info for a selected peer."""
    user = peer.get("user", {})
    name = _peer_name(peer)

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


def main():
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
        print("No Meshtastic devices found.")
        print("Make sure Bluetooth is enabled and the device is powered on.")
        sys.exit(1)

    device = pick_device(devices)

    # Spinner runs in background; BLE connection stays on main thread
    _spin_done = threading.Event()

    def _spinner():
        frames = "|/-\\"
        i = 0
        elapsed = 0.0
        while not _spin_done.wait(0.1):
            elapsed += 0.1
            print(f"\r  Connecting to {device.name or device.address}... "
                  f"{frames[i % 4]}  {elapsed:.1f}s", end="", flush=True)
            i += 1

    _spin_thread = threading.Thread(target=_spinner, daemon=True)
    _spin_thread.start()

    try:
        iface = _FastBLEInterface(device.address)
    except BLEInterface.BLEError as e:
        _spin_done.set()
        _spin_thread.join()
        print(f"\n  Connection failed: {e}")
        sys.exit(1)
    finally:
        _spin_done.set()
        _spin_thread.join()

    print(f"\r  Connected to {device.name or device.address}.{' ' * 20}")

    try:
        show_node_info(iface)
    finally:
        print("\nClosing connection...")
        t = threading.Thread(target=iface.close, daemon=True)
        t.start()
        t.join(timeout=3)
        print("Node Disconnected. Bye!")


if __name__ == "__main__":
    main()
