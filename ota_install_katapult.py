#!/usr/bin/env python3
"""
ota_install_katapult.py — Install Katapult bootloader on ACE Pro via stock USB OTA

Pushes shim.bin through the stock ACE firmware's OTA mechanism.  The stock
bootloader copies the shim to the app area (0x08008000), boots it, and the
shim overwrites the stock bootloader with Katapult at 0x08000000.

Prerequisites:
  - ACE Pro with stock firmware running (responds on /dev/ttyACM0)
  - shim.bin built (contains embedded katapult.bin)
  - pyserial installed (pip install pyserial)

Usage:
  python3 ota_install_katapult.py [--port /dev/ttyACM0] [--shim shim.bin]
  python3 ota_install_katapult.py --info   # just check connection, no flash
"""

from __future__ import annotations

import argparse
import os
import sys

# ace_ota.py lives in the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ace_ota import AceOtaUpdater  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install Katapult bootloader on ACE Pro via stock USB OTA"
    )
    ap.add_argument(
        "--port", default="/dev/ttyACM0",
        help="Serial port for ACE USB CDC (default: /dev/ttyACM0)"
    )
    ap.add_argument(
        "--shim", default=os.path.join(os.path.dirname(__file__), "shim.bin"),
        help="Path to shim.bin (default: ./shim.bin)"
    )
    ap.add_argument(
        "--info", action="store_true",
        help="Just query device info and exit (no OTA)"
    )
    ap.add_argument(
        "--fw-crc-xorout", action="store_true",
        help="Use CRC with xorout (try this if raw CRC is rejected)"
    )
    args = ap.parse_args()

    if not args.info and not os.path.isfile(args.shim):
        print(f"ERROR: shim.bin not found at {args.shim}")
        print("       Build it first: cd ace-shim && make")
        return 1

    updater = AceOtaUpdater(args.port)

    print("Connecting to ACE Pro...")
    info = updater.handshake()
    fw = info.get("result", {}).get("firmware", "?")
    bl = info.get("result", {}).get("boot_firmware", "?")
    print(f"  App firmware:  {fw}")
    print(f"  Bootloader:    {bl}")

    if args.info:
        return 0

    print()
    print("=" * 60)
    print("  WARNING: This will REPLACE the stock bootloader with")
    print("  Katapult.  The stock ACE firmware will be gone.")
    print()
    print("  If power is lost during the ~200ms flash write,")
    print("  the device will be bricked (SWD recovery required).")
    print("=" * 60)
    print()

    confirm = input("Type 'YES' to proceed: ").strip()
    if confirm != "YES":
        print("Aborted.")
        return 1

    print()
    print(f"Pushing shim ({os.path.getsize(args.shim)} bytes) via OTA...")

    ok = updater.update(
        args.shim,
        "KATAPULT",
        fw_crc_xorout=args.fw_crc_xorout,
    )

    if not ok:
        print("ERROR: OTA update failed.")
        return 1

    print()
    print("=" * 60)
    print("  Shim delivered successfully!")
    print()
    print("  The device will now go through two reboot cycles:")
    print("    1. Stock bootloader copies shim → app area (~5s)")
    print("    2. Shim installs Katapult, resets (<1s)")
    print()
    print("  Wait ~10 seconds, then verify:")
    print("    lsusb | grep 1d50:6177")
    print()
    print("  Then flash Klipper:")
    print("    cd klipper && make flash FLASH_DEVICE=1d50:6177")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
