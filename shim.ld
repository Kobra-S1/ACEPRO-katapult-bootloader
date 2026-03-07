/*
 * Linker script for ACE OTA shim
 * Runs from the stock app area at 0x08008000.
 * Target: GD32F303CCT6 / STM32F103xE compatible
 */

MEMORY
{
    /* Stock bootloader copies OTA payload here; shim executes from here */
    FLASH (rx)  : ORIGIN = 0x08008000, LENGTH = 112K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 48K
}

SECTIONS
{
    /* Vector table must be first (stock bootloader reads MSP + Reset from here) */
    .isr_vector :
    {
        . = ALIGN(4);
        KEEP(*(.isr_vector))
        . = ALIGN(4);
    } > FLASH

    .text :
    {
        . = ALIGN(4);
        *(.text)
        *(.text*)
        . = ALIGN(4);
    } > FLASH

    .rodata :
    {
        . = ALIGN(4);
        *(.rodata)
        *(.rodata*)
        *(.rodata.katapult)
        . = ALIGN(4);
        _eflash = .;
    } > FLASH

    /* No .data or .bss needed — shim has no mutable globals */

    /DISCARD/ :
    {
        *(.ARM.exidx*)
        *(.ARM.attributes)
        *(.comment)
    }
}
