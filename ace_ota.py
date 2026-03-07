#!/usr/bin/env python3
"""
ace_ota.py — Anycubic ACE / Color Engine Pro OTA updater (USB CDC-ACM)

This version:
- Uses the SAME frame CRC variant as ace_console.py (no final xorout).
- Keeps firmware CRC (iap_upgrade params.crc) configurable (default: xorout, per RE notes).
- Uses buffered nonblocking reads (4096-byte reads).
- Survives Linux cdc_acm "ready but returned no data" by treating it as a port hangup,
  reopening, re-handshaking, and retrying the current chunk.
- Makes --info actually exit (info-only).

Usage:
  python3 ace_ota.py /dev/ttyACM0 firmware.bin V1.3.864 --info
  python3 ace_ota.py /dev/ttyACM0 firmware.bin V1.3.864

Tip (stable path):
  /dev/serial/by-id/... is better than /dev/ttyACM0 if the device re-enumerates.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional

import serial


# ---------------------- Protocol constants ----------------------

FRAME_START_1 = 0xFF
FRAME_START_2 = 0xAA
FRAME_END     = 0xFE

CHUNK_SIZE    = 64
STAGING_BASE  = 0x08024000


# ---------------------- CRC helpers ----------------------

def crc16_x25_raw(payload: bytes) -> int:
    """
    CRC implementation matching ace_console.py:
      init=0xFFFF, reflected poly (via the bit-twiddling step), NO final xorout.
    """
    crc = 0xFFFF
    for b in payload:
        data = b
        data ^= crc & 0xFF
        data ^= (data & 0x0F) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
        crc &= 0xFFFF
    return crc

def crc16_x25_xorout(payload: bytes) -> int:
    """Same as raw but with final xorout (standard CRC-16/X-25 check=0x906E)."""
    return crc16_x25_raw(payload) ^ 0xFFFF


# ---------------------- Framing ----------------------

def pack_frame(payload: bytes) -> bytes:
    """
    Frame: FF AA [len_u16_le] [payload] [crc_u16_le] FE
    IMPORTANT: CRC here uses crc16_x25_raw() (no xorout), matching ace_console.py.
    """
    ln = len(payload)
    crc = crc16_x25_raw(payload)
    return struct.pack("<BBH", FRAME_START_1, FRAME_START_2, ln) + payload + struct.pack("<HB", crc, FRAME_END)

def pack_json_frame(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return pack_frame(payload)

def pack_chunk_frame(flash_addr: int, data: bytes) -> bytes:
    if len(data) > CHUNK_SIZE:
        raise ValueError(f"Chunk too large: {len(data)} > {CHUNK_SIZE}")

    # Pad to 4-byte boundary for word-aligned flash writes
    padded_len = ((len(data) + 3) // 4) * 4
    data_padded = data.ljust(padded_len, b"\xFF")

    # Chunk payload: 0x55 magic | addr_u32_LE | byte_count_u8 | data
    # The 0x55 byte tells the firmware this is a binary chunk, not JSON.
    payload = b'\x55' + struct.pack("<IB", flash_addr, padded_len) + data_padded
    return pack_frame(payload)

def try_unpack_one_frame(buf: bytearray) -> Optional[bytes]:
    """
    If buf contains a full valid frame at the start, consume it and return payload.
    If not enough data, return None. If malformed, resync and return None.
    """
    # Need at least 7 bytes minimum
    if len(buf) < 7:
        return None

    # Sync to header
    if not (buf[0] == FRAME_START_1 and buf[1] == FRAME_START_2):
        hdr = buf.find(bytes([FRAME_START_1, FRAME_START_2]))
        if hdr == -1:
            buf.clear()
            return None
        del buf[:hdr]
        if len(buf) < 7:
            return None

    payload_len = struct.unpack_from("<H", buf, 2)[0]
    frame_len = 2 + 2 + payload_len + 2 + 1
    if len(buf) < frame_len:
        return None

    term_idx = 4 + payload_len + 2
    if buf[term_idx] != FRAME_END:
        # Bad terminator, resync
        next_hdr = buf.find(bytes([FRAME_START_1, FRAME_START_2]), 1)
        if next_hdr == -1:
            buf.clear()
        else:
            del buf[:next_hdr]
        return None

    frame = bytes(buf[:frame_len])
    del buf[:frame_len]

    payload = frame[4:4 + payload_len]
    crc_rx = struct.unpack_from("<H", frame, 4 + payload_len)[0]
    crc_calc = crc16_x25_raw(payload)
    if crc_rx != crc_calc:
        # Drop and keep scanning
        return None

    return payload


# ---------------------- Transport ----------------------

class PortHungUp(Exception):
    """Raised when Linux cdc_acm reports readable but returns EOF (pyserial throws)."""

@dataclass
class SerialOpenParams:
    port: str
    baud: int = 115200
    timeout: float = 0.0          # nonblocking
    write_timeout: float = 1.0
    exclusive: bool = True        # best effort


class AceSerial:
    def __init__(self, params: SerialOpenParams):
        self.params = params
        self.ser: serial.Serial = self._open_serial()
        self._buf = bytearray()
        print(f"[transport] Opened {params.port} @ {params.baud} baud")

    def _open_serial(self) -> serial.Serial:
        kwargs = dict(
            port=self.params.port,
            baudrate=self.params.baud,
            timeout=self.params.timeout,
            write_timeout=self.params.write_timeout,
        )
        # pyserial on Linux supports exclusive=True on many versions, but not all
        if self.params.exclusive:
            try:
                kwargs["exclusive"] = True
            except Exception:
                pass

        ser = serial.Serial(**kwargs)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        return ser

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def reopen(self, wait: float = 8.0) -> None:
        self.close()
        self._buf.clear()

        deadline = time.time() + wait
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            if os.path.exists(self.params.port):
                try:
                    self.ser = self._open_serial()
                    print(f"[transport] Reopened {self.params.port}")
                    return
                except Exception as e:
                    last_err = e
            time.sleep(0.05)
        raise TimeoutError(f"Port did not come back: {self.params.port} (last error: {last_err})")

    def send(self, frame: bytes) -> None:
        self.ser.write(frame)
        self.ser.flush()

    def recv_frame(self, timeout: float = 5.0) -> bytes:
        """
        Buffered, nonblocking reader. Returns payload bytes.
        May raise TimeoutError or PortHungUp.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.ser.read(4096)
            except serial.SerialException as e:
                # Linux cdc_acm EOF/hangup
                if "returned no data" in str(e):
                    raise PortHungUp(str(e)) from e
                raise

            if chunk:
                self._buf += chunk

            payload = try_unpack_one_frame(self._buf)
            if payload is not None:
                return payload

            time.sleep(0.01)

        raise TimeoutError("Timed out waiting for frame")

    def cmd(self, method: str, cmd_id: int, params: Optional[dict] = None, timeout: float = 5.0) -> dict:
        req = {"method": method, "id": cmd_id}
        if params is not None:
            req["params"] = params
        frame = pack_json_frame(req)
        print(f"[cmd] → {method}  id={cmd_id}")
        self.send(frame)
        payload = self.recv_frame(timeout=timeout)
        try:
            resp = json.loads(payload.decode("utf-8"))
        except Exception:
            raise RuntimeError(f"Non-JSON response to {method}: {payload.hex()}")
        print(f"[cmd] ← {json.dumps(resp)}")
        return resp


# ---------------------- OTA logic ----------------------

class AceOtaUpdater:
    def __init__(self, port: str, baud: int = 115200):
        self.transport = AceSerial(SerialOpenParams(port=port, baud=baud))
        self._cmd_id = 0

    def _next_id(self) -> int:
        i = self._cmd_id
        self._cmd_id += 1
        return i

    def handshake(self, tries: int = 10) -> dict:
        last = None
        for attempt in range(1, tries + 1):
            try:
                resp = self.transport.cmd("get_info", self._next_id(), timeout=3.0)
                if resp.get("code", 1) == 0:
                    fw = resp.get("result", {}).get("firmware", "?")
                    print(f"[ota] Connected: firmware={fw}")
                    return resp
                last = resp
            except (TimeoutError, PortHungUp) as e:
                last = str(e)
                # If hung up, reopen and retry quickly
                if isinstance(e, PortHungUp):
                    self.transport.reopen(wait=8.0)
            time.sleep(0.05)
        raise RuntimeError(f"Handshake failed after {tries} tries: {last}")

    def get_info(self) -> dict:
        return self.transport.cmd("get_info", self._next_id(), timeout=5.0)

    def update(
        self,
        firmware_path: str,
        version: str,
        *,
        fw_crc_xorout: bool = True,
        chunk_retries: int = 5,
        ota_restarts: int = 2,
    ) -> bool:
        fw = open(firmware_path, "rb").read()
        size = len(fw)

        fw_crc_raw = crc16_x25_raw(fw)
        fw_crc_xo  = fw_crc_raw ^ 0xFFFF
        fw_crc = fw_crc_xo if fw_crc_xorout else fw_crc_raw

        chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"[ota] Firmware: {os.path.basename(firmware_path)}")
        print(f"[ota]   Size:    {size} bytes")
        print(f"[ota]   CRC raw: 0x{fw_crc_raw:04X}")
        print(f"[ota]   CRC xor: 0x{fw_crc_xo:04X}")
        print(f"[ota]   Sent CRC:{' xorout' if fw_crc_xorout else ' raw'} = 0x{fw_crc:04X}")
        print(f"[ota]   Version: {version}")
        print(f"[ota]   Chunks:  {chunks} × {CHUNK_SIZE} bytes")

        restart = 0
        while True:
            try:
                # Step 1: iap_upgrade (erase staging)
                print("[ota] Step 1: Sending iap_upgrade (erasing 112 KB staging area)...")
                resp = self.transport.cmd(
                    "iap_upgrade",
                    self._next_id(),
                    params={"size": size, "crc": int(fw_crc), "version": version},
                    timeout=20.0,
                )
                if resp.get("code", 1) != 0:
                    raise RuntimeError(f"iap_upgrade failed: {resp}")

                print("[ota] Staging area erased. Sending chunks...")

                # Step 2: send chunks
                t0 = time.time()
                for i in range(chunks):
                    flash_addr = STAGING_BASE + i * CHUNK_SIZE
                    chunk = fw[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
                    frame = pack_chunk_frame(flash_addr, chunk)

                    ok = False
                    for attempt in range(1, chunk_retries + 1):
                        try:
                            self.transport.send(frame)
                            payload = self.transport.recv_frame(timeout=8.0)
                        except PortHungUp as e:
                            print(f"[ota] Chunk {i+1}/{chunks}: port hung up ({e})")
                            # IMPORTANT: do NOT continue chunking. OTA state is now unknown.
                            # Let the outer handler restart from iap_upgrade.
                            raise
                        except TimeoutError:
                            print(f"[ota] Chunk {i+1}/{chunks} attempt {attempt}: timeout waiting for ack")
                            continue

                        # Expect JSON ack per RE doc; if not, show raw
                        try:
                            ack = json.loads(payload.decode("utf-8"))
                        except Exception:
                            raise RuntimeError(
                                f"[ota] Chunk {i+1}/{chunks}: Non-JSON ack: {payload.hex()}"
                            )

                        msg = ack.get("msg", "")
                        code = ack.get("code", 0)
                        if code == 0 and msg in ("write_success", "success"):
                            ok = True
                            break

                        # Device-side documented errors: write_error1/write_error2
                        print(f"[ota] Chunk {i+1}/{chunks} attempt {attempt}: ack={ack}")
                        time.sleep(0.05)

                    if not ok:
                        raise RuntimeError(f"Chunk {i+1}/{chunks} failed after {chunk_retries} retries")

                    if (i + 1) % 50 == 0 or (i + 1) == chunks:
                        elapsed = max(0.001, time.time() - t0)
                        kbps = ( (i + 1) * CHUNK_SIZE / 1024.0 ) / elapsed
                        pct = 100.0 * (i + 1) / chunks
                        print(f"[ota] Chunks: {i+1}/{chunks}  {pct:5.1f}%  {kbps:5.1f} KB/s")

                # Step 3: iap_upgrade_finish (CRC verify + reset)
                print("[ota] Step 3: Sending iap_upgrade_finish (CRC verify)...")
                resp2 = self.transport.cmd("iap_upgrade_finish", self._next_id(), timeout=10.0)
                if resp2.get("code", 1) != 0:
                    raise RuntimeError(f"iap_upgrade_finish failed: {resp2}")

                msg2 = resp2.get("msg", "")
                if msg2 not in ("Upgrade_success", "success"):
                    raise RuntimeError(f"Unexpected finish response: {resp2}")

                print("[ota] ✓ CRC verified / finish accepted. Device may reset now.")
                return True

            except PortHungUp as e:
                print(f"[ota] Port hung up outside chunk loop: {e}")
                self.transport.reopen(wait=8.0)
                self.handshake(tries=6)

            except Exception as e:
                restart += 1
                if restart > ota_restarts:
                    raise
                print(f"[ota] ERROR: {e}")
                print(f"[ota] Restarting OTA from iap_upgrade (attempt {restart}/{ota_restarts})...")
                # Best effort: reopen and handshake before restarting
                try:
                    self.transport.reopen(wait=8.0)
                except Exception:
                    pass
                self.handshake(tries=6)


# ---------------------- CLI ----------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ACE Pro OTA updater")
    ap.add_argument("port", help="Serial port (e.g. /dev/ttyACM0 or /dev/serial/by-id/...)")
    ap.add_argument("firmware", help="Firmware .bin file")
    ap.add_argument("version", help="Target version string (e.g. V1.3.864)")
    ap.add_argument("--baud", type=int, default=115200, help="Baud (ignored for USB CDC, kept for compatibility)")
    ap.add_argument("--info", action="store_true", help="Print get_info and exit (no OTA)")
    ap.add_argument("--fw-crc-xorout", action="store_true",
                    help="Send firmware CRC WITH xorout in iap_upgrade params.crc (default sends raw, matching firmware)")
    ap.add_argument("--chunk-retries", type=int, default=5, help="Retries per chunk")
    ap.add_argument("--ota-restarts", type=int, default=2, help="How many times to restart OTA from scratch on failures")
    args = ap.parse_args()

    up = AceOtaUpdater(args.port, baud=args.baud)

    # Always handshake first (also helps confirm port is right)
    up.handshake()

    if args.info:
        info = up.get_info()
        print("Device info:", json.dumps(info, indent=2))
        return 0

    ok = up.update(
        args.firmware,
        args.version,
        fw_crc_xorout=args.fw_crc_xorout,
        chunk_retries=args.chunk_retries,
        ota_restarts=args.ota_restarts,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())