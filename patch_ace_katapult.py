#!/usr/bin/env python3
"""
Patch an Anycubic ACE Pro firmware binary so that the 'iap_upgrade' JSON-RPC
command reboots the MCU into the Katapult bootloader instead of starting OTA.

Works by finding ota_set_upgrade_params() via its unique Thumb-2 prologue
signature and replacing the first 48 bytes with position-independent code
that writes the REQUEST_CANBOOT magic to SRAM and triggers SYSRESETREQ.

Usage:
    python3 patch_ace_katapult.py ACE_V1.3.84_20240929.bin
    python3 patch_ace_katapult.py ACE_V1.3.76_20240703.bin
    python3 patch_ace_katapult.py firmware.bin -o custom_output.bin

The patched binary is written alongside the original as:
    ACE_V1.3.84_20240929_katapult.bin
"""

import argparse
import os
import re
import sys
from typing import List, Match

FULL_FLASH_SIZE = 256 * 1024
BOOTLOADER_SIZE = 32 * 1024

# ---------- Signature of ota_set_upgrade_params() ----------
# This matches the function's early-return path:
#   PUSH  {r4-r8, lr}
#   MOV   r4, r0
#   MOV   r5, r1
#   MOV   r6, r2
#   MOV   r7, r3
#   LDR   r0, [pc, #N]        ; load &ota_state struct (N varies)
#   LDRB  r0, [r0, #0x18]     ; check "already upgrading" flag
#   CBZ   r0, .Lcontinue
#   MOVS  r0, #0              ; return false
#   POP   {r4-r8, pc}
#
# Byte pattern (24 bytes, one wildcard for LDR index):
#   2d e9 f0 41  04 46 0d 46  16 46 1f 46  XX 48  00 7e  10 b1  00 20  bd e8 f0 81
SIGNATURE = re.compile(
    b"\x2d\xe9\xf0\x41"          # PUSH {r4-r8, lr}
    b"\x04\x46\x0d\x46"          # MOV r4, r0 ; MOV r5, r1
    b"\x16\x46\x1f\x46"          # MOV r6, r2 ; MOV r7, r3
    b".\x48"                     # LDR r0, [pc, #N]   (any N)
    b"\x00\x7e"                  # LDRB r0, [r0, #0x18]
    b"\x10\xb1"                  # CBZ r0, +4
    b"\x00\x20"                  # MOVS r0, #0
    b"\xbd\xe8\xf0\x81",         # POP {r4-r8, pc}
    re.DOTALL,
)

# ---------- Reboot-to-Katapult payload (48 bytes) ----------
# Position-independent Thumb-2 code that:
#   1. Writes REQUEST_CANBOOT magic (0x5984E3FA_6CA1589B) to 0x2000BFF8
#   2. DSB
#   3. Writes SYSRESETREQ (0x05FA0004) to AIRCR (0xE000ED0C)
#   4. DSB
#   5. Infinite loop (WFI-like)
# Source: katapult_reboot/reboot_to_katapult.S (code portion, no vector table)
PAYLOAD = bytes.fromhex(
    "0648"          # LDR  r0, [pc, #24]   ; &0x2000BFF8
    "0749"          # LDR  r1, [pc, #28]   ; 0x6CA1589B (magic low)
    "074a"          # LDR  r2, [pc, #28]   ; 0x5984E3FA (magic high)
    "0160"          # STR  r1, [r0, #0]
    "4260"          # STR  r2, [r0, #4]
    "bff34f8f"      # DSB  SY
    "0648"          # LDR  r0, [pc, #24]   ; &0xE000ED0C (AIRCR)
    "0649"          # LDR  r1, [pc, #24]   ; 0x05FA0004 (SYSRESETREQ)
    "0160"          # STR  r1, [r0, #0]
    "bff34f8f"      # DSB  SY
    "fee7"          # B    .              ; infinite loop
    "0000"          # (alignment pad)
    "f8bf0020"      # .word 0x2000BFF8    ; _stack_end (REQUEST_CANBOOT addr)
    "9b58a16c"      # .word 0x6CA1589B    ; magic low
    "fae38459"      # .word 0x5984E3FA    ; magic high
    "0ced00e0"      # .word 0xE000ED0C    ; AIRCR
    "0400fa05"      # .word 0x05FA0004    ; VECTKEY | SYSRESETREQ
)

assert len(PAYLOAD) == 48, f"Payload must be 48 bytes, got {len(PAYLOAD)}"


def select_signature_offset(fw: bytes, matches: List[Match[bytes]]) -> int:
    """Pick the correct ota_set_upgrade_params() signature occurrence.

    For raw 256 KiB full-flash dumps, prefer a unique match in the app region
    (offset >= 0x8000). This avoids false positives from the stock bootloader.
    For app-only binaries, require a unique match to stay conservative.
    """
    if len(matches) == 1:
        return matches[0].start()

    # Full-chip dumps include the first-stage bootloader at 0x08000000.
    # ota_set_upgrade_params() lives in the app image at 0x08008000+.
    if len(fw) == FULL_FLASH_SIZE:
        app_matches = [m.start() for m in matches if m.start() >= BOOTLOADER_SIZE]
        if len(app_matches) == 1:
            return app_matches[0]

    offsets = [f"0x{m.start():04X}" for m in matches]
    raise ValueError(
        f"Multiple signature matches ({offsets}) — ambiguous, refusing to patch."
    )


def image_base_addr(fw_len: int) -> int:
    """Return the flash base represented by file offset 0.

    - Full 256 KiB dump: file starts at 0x08000000
    - App-only firmware blob: file starts at 0x08008000
    """
    if fw_len == FULL_FLASH_SIZE:
        return 0x08000000
    return 0x08008000


def main() -> int:
    ap = argparse.ArgumentParser(description="Patch ACE firmware for Katapult reboot on iap_upgrade")
    ap.add_argument("firmware", help="Input .bin firmware file")
    ap.add_argument("-o", "--output", help="Output file (default: <name>_katapult.bin)")
    args = ap.parse_args()

    fw = open(args.firmware, "rb").read()
    print(f"Input:  {args.firmware} ({len(fw)} bytes)")

    # Verify this looks like an ACE firmware (should contain "iap_upgrade" string)
    if b"iap_upgrade" not in fw:
        print("ERROR: No 'iap_upgrade' string found — this doesn't look like ACE firmware.")
        return 1

    # Check if already patched (payload present, signature absent)
    if PAYLOAD in fw:
        print("SKIP:   Firmware is already patched!")
        return 0

    # Search for the signature
    matches = list(SIGNATURE.finditer(fw))
    if len(matches) == 0:
        print("ERROR: ota_set_upgrade_params() signature not found in firmware.")
        print("       This firmware version may have a different function layout.")
        return 1

    try:
        offset = select_signature_offset(fw, matches)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    addr = image_base_addr(len(fw)) + offset
    print(f"Found:  ota_set_upgrade_params() at offset 0x{offset:04X} (addr 0x{addr:08X})")

    # Show what we're replacing
    original = fw[offset : offset + 48]
    print(f"Before: {original.hex()}")
    print(f"After:  {PAYLOAD.hex()}")

    # Apply patch
    patched = bytearray(fw)
    patched[offset : offset + 48] = PAYLOAD

    # Output path
    if args.output:
        out_path = args.output
    else:
        base, ext = os.path.splitext(args.firmware)
        out_path = f"{base}_katapult{ext}"

    with open(out_path, "wb") as f:
        f.write(patched)
    print(f"Output: {out_path} ({len(patched)} bytes)")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
