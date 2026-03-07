#!/usr/bin/env python3
"""
Send iap_upgrade to patched ACE firmware to reboot into Katapult bootloader.

Usage:
    python3 enter_katapult.py [PORT]

Default port: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00
"""

import struct, json, sys, time
import serial

FRAME_START = b"\xff\xaa"
FRAME_END   = 0xFE
DEFAULT_PORT = "/dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00"


def crc16_x25_raw(payload: bytes) -> int:
    crc = 0xFFFF
    for b in payload:
        data = b
        data ^= crc & 0xFF
        data ^= (data & 0x0F) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
        crc &= 0xFFFF
    return crc


def pack_json_frame(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode()
    crc = crc16_x25_raw(payload)
    return struct.pack("<BBH", 0xFF, 0xAA, len(payload)) + payload + struct.pack("<HB", crc, FRAME_END)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    print(f"Opening {port} ...")
    ser = serial.Serial(port, 115200, timeout=2)

    # Handshake: get_info
    ser.write(pack_json_frame({"method": "get_info", "id": 0}))
    time.sleep(0.3)
    resp = ser.read(4096)
    if not resp:
        print("No response to get_info — is patched ACE firmware running?")
        ser.close()
        sys.exit(1)
    print(f"ACE responded ({len(resp)} bytes)")

    # Trigger: iap_upgrade → patched code writes REQUEST_CANBOOT magic + SYSRESETREQ
    print("Sending iap_upgrade → rebooting into Katapult ...")
    ser.write(pack_json_frame({"method": "iap_upgrade", "id": 1, "params": {"size": 100, "crc": 0, "version": "V0"}}))
    time.sleep(0.5)
    ser.close()

    print("Done. Verify with:  lsusb | grep 1d50:6177")


if __name__ == "__main__":
    main()
