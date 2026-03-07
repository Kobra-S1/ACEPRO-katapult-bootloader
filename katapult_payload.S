/*
 * katapult_payload.S — Embed pre-built katapult.bin as a const array
 *
 * Referenced from shim.c as:
 *   extern const uint8_t katapult_bin_start[];
 *   extern const uint8_t katapult_bin_end[];
 */

    .section .rodata.katapult, "a"
    .global katapult_bin_start, katapult_bin_end
    .balign 4

katapult_bin_start:
    .incbin "katapult.bin"
katapult_bin_end:
