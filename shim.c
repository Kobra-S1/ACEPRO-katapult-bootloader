/**
 * OTA Shim — Install Katapult bootloader via stock ACE OTA
 *
 * This tiny firmware is delivered to the ACE Pro via the stock USB OTA
 * mechanism.  The stock bootloader copies it to 0x08008000 (app area)
 * and boots it.  It then:
 *
 *   1. Disconnects USB (PB9 LOW — external D+ pullup off)
 *   2. Erases the stock bootloader region (0x08000000, 32 KB)
 *   3. Writes the embedded katapult.bin at 0x08000000
 *   4. Zeroes the shim's own vector-table MSP at 0x08008000 so Katapult
 *      does not re-launch the shim after its startup timeout
 *   5. Issues SYSRESETREQ → MCU reboots into Katapult (stays resident)
 *
 * After reset, Katapult is running.  Flash Klipper via:
 *   make flash FLASH_DEVICE=1d50:6177
 *
 * Target: GD32F303CCT6 (STM32F103-compatible, Cortex-M4/M3)
 * Linked at: 0x08008000 (stock app area)
 */

#include <stdint.h>

/* ---- Embedded Katapult binary (from katapult_payload.S) ------------ */
extern const uint8_t katapult_bin_start[];
extern const uint8_t katapult_bin_end[];

/* ---- Hardware addresses -------------------------------------------- */

/* Flash controller (STM32F1 / GD32F303) */
typedef struct {
    volatile uint32_t ACR;
    volatile uint32_t KEYR;
    volatile uint32_t OPTKEYR;
    volatile uint32_t SR;
    volatile uint32_t CR;
    volatile uint32_t AR;
} FLASH_t;

#define FLASH           ((FLASH_t *)0x40022000UL)
#define FLASH_SR_BSY    (1u << 0)
#define FLASH_CR_PG     (1u << 0)
#define FLASH_CR_PER    (1u << 1)
#define FLASH_CR_STRT   (1u << 6)
#define FLASH_CR_LOCK   (1u << 7)

/* GPIO (STM32F1 style CRL/CRH/ODR/BSRR) */
#define GPIOB_BASE      0x40010C00UL
#define GPIOB_CRH       (*(volatile uint32_t *)(GPIOB_BASE + 0x04))
#define GPIOB_BSRR      (*(volatile uint32_t *)(GPIOB_BASE + 0x10))
#define GPIOB_BRR       (*(volatile uint32_t *)(GPIOB_BASE + 0x14))

/* RCC — enable GPIOB clock */
#define RCC_APB2ENR     (*(volatile uint32_t *)0x40021018UL)
#define RCC_APB2ENR_IOPBEN  (1u << 3)

/* SCB AIRCR — system reset */
#define SCB_AIRCR       (*(volatile uint32_t *)0xE000ED0CUL)
#define AIRCR_SYSRESETREQ  0x05FA0004UL

/* ---- Constants ----------------------------------------------------- */
#define STOCK_BL_START  0x08000000UL
#define STOCK_BL_END    0x08008000UL    /* 32 KB = 16 pages */
#define FLASH_PAGE_SIZE 2048U
#define KATAPULT_DEST   0x08000000UL

/* ---- Flash routines (same as bootloader.c) ------------------------- */

static void flash_wait(void)
{
    while (FLASH->SR & FLASH_SR_BSY)
        ;
}

static void flash_unlock(void)
{
    if (FLASH->CR & FLASH_CR_LOCK) {
        FLASH->KEYR = 0x45670123UL;
        FLASH->KEYR = 0xCDEF89ABUL;
    }
}

static void flash_lock(void)
{
    FLASH->CR |= FLASH_CR_LOCK;
}

static void flash_erase_page(uint32_t addr)
{
    flash_wait();
    FLASH->CR |= FLASH_CR_PER;
    FLASH->AR  = addr;
    FLASH->CR |= FLASH_CR_STRT;
    flash_wait();
    FLASH->CR &= ~FLASH_CR_PER;
}

static void flash_write_u16(uint32_t addr, uint16_t data)
{
    flash_wait();
    FLASH->CR |= FLASH_CR_PG;
    *(volatile uint16_t *)addr = data;
    flash_wait();
    FLASH->CR &= ~FLASH_CR_PG;
}

/* ---- GPIO helper --------------------------------------------------- */

static void pb9_output_low(void)
{
    /* Enable GPIOB clock */
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN;

    /* PB9 is in CRH bits [7:4] (pin 9 = CRH index 1)
     * Set MODE=01 (output 10 MHz), CNF=00 (push-pull) → nibble = 0x1 */
    uint32_t crh = GPIOB_CRH;
    crh &= ~(0xFu << 4);       /* clear bits [7:4] for pin 9 */
    crh |=  (0x1u << 4);       /* MODE=01, CNF=00 */
    GPIOB_CRH = crh;

    /* Drive PB9 LOW (USB D+ pullup off → disconnect) */
    GPIOB_BRR = (1u << 9);
}

/* ---- Simple delay -------------------------------------------------- */

static void delay_cycles(volatile uint32_t n)
{
    while (n--)
        ;
}

/* ---- Main entry point ---------------------------------------------- */

void shim_main(void)
{
    /* Step 1: Disconnect USB immediately */
    pb9_output_low();

    /* Brief settling delay (~50 ms at 72 MHz) */
    delay_cycles(72000UL * 50);

    /* Step 2: Compute payload size */
    uint32_t katapult_size = (uint32_t)(katapult_bin_end - katapult_bin_start);

    /* Sanity: don't proceed if payload is missing or too large */
    if (katapult_size == 0 || katapult_size > (STOCK_BL_END - STOCK_BL_START))
        while (1);  /* hang — something is very wrong */

    /* Step 2b: Validate MSP in embedded katapult.bin points to GD32F303 SRAM.
     *          Catches wrong-MCU builds (e.g. LPC1768 with RAM at 0x10000000). */
    uint32_t payload_msp = (uint32_t)katapult_bin_start[0]
                         | ((uint32_t)katapult_bin_start[1] << 8)
                         | ((uint32_t)katapult_bin_start[2] << 16)
                         | ((uint32_t)katapult_bin_start[3] << 24);
    if ((payload_msp & 0xFFFF0000UL) != 0x20000000UL)
        while (1);  /* hang — katapult.bin built for wrong MCU */

    /* Step 3: Unlock flash */
    flash_unlock();

    /* Step 4: Erase stock bootloader region (0x08000000 – 0x08007FFF)
     *
     * This is safe because we are executing from 0x08008000+.
     * The stock bootloader is 32 KB (16 pages × 2 KB). */
    for (uint32_t addr = STOCK_BL_START; addr < STOCK_BL_END; addr += FLASH_PAGE_SIZE)
        flash_erase_page(addr);

    /* Step 5: Write Katapult at 0x08000000 (half-word writes)
     *
     * Round up to even length for half-word access. */
    uint32_t write_len = (katapult_size + 1U) & ~1U;
    for (uint32_t i = 0; i < write_len; i += 2U) {
        uint16_t hw;
        if (i + 1 < katapult_size) {
            hw = (uint16_t)katapult_bin_start[i]
               | ((uint16_t)katapult_bin_start[i + 1] << 8);
        } else {
            /* Odd last byte: pad with 0xFF */
            hw = (uint16_t)katapult_bin_start[i] | 0xFF00u;
        }
        flash_write_u16(KATAPULT_DEST + i, hw);
    }

    /* Step 6: Lock flash */
    flash_lock();

    /* Step 7: Verify first word (Katapult MSP) was written correctly */
    uint32_t written_msp = *(volatile uint32_t *)KATAPULT_DEST;
    uint32_t expected_msp = (uint32_t)katapult_bin_start[0]
                          | ((uint32_t)katapult_bin_start[1] << 8)
                          | ((uint32_t)katapult_bin_start[2] << 16)
                          | ((uint32_t)katapult_bin_start[3] << 24);
    if (written_msp != expected_msp)
        while (1);  /* hang — flash write failed */

    /* Step 8: Zero our own vector-table MSP to prevent Katapult re-launch.
     *
     * Katapult validates APPLICATION_START (= STOCK_BL_END = 0x08008000) by
     * checking whether *(uint32_t*)0x08008000 looks like a valid RAM SP.
     * Our shim's vector table has SP = 0x2000C000 there — which IS valid —
     * so after its startup timeout Katapult would launch us again, erasing
     * and rewriting Katapult in an infinite loop.
     *
     * Fix: program both halfwords of the MSP field to 0x0000.
     * Flash programming (bits 1→0 only) never requires a prior erase.
     * STM32F1/GD32F303 stalls instruction fetches from the same flash bank
     * while the write completes, then resumes — code on the page is intact.
     * After the write *(uint32_t*)0x08008000 == 0x00000000, which fails
     * Katapult's RAM-address check, so it stays in bootloader mode. */
    flash_unlock();
    flash_write_u16(STOCK_BL_END,     0x0000u);   /* MSP[15:0]  0xC000 → 0x0000 */
    flash_write_u16(STOCK_BL_END + 2, 0x0000u);   /* MSP[31:16] 0x2000 → 0x0000 */
    flash_lock();

    /* Step 9: System reset → Katapult boots and stays resident indefinitely */
    SCB_AIRCR = AIRCR_SYSRESETREQ;
    while (1);
}

/* ---- Vector table -------------------------------------------------- */

void Reset_Handler(void)
{
    shim_main();
}

typedef void (*ISR_t)(void);

static void Default_Handler(void) { while (1); }

__attribute__((section(".isr_vector"), used))
const ISR_t g_vectors[] = {
    (ISR_t)0x2000C000UL,    /* Initial MSP: top of 48 KB SRAM */
    Reset_Handler,           /* Reset                          */
    Default_Handler,         /* NMI                            */
    Default_Handler,         /* HardFault                      */
    Default_Handler,         /* MemManage                      */
    Default_Handler,         /* BusFault                       */
    Default_Handler,         /* UsageFault                     */
};
