##
## Makefile for ACE OTA shim
##
## Builds shim.bin: a tiny OTA payload that installs Katapult on an ACE Pro
## by erasing the stock bootloader and writing the embedded katapult.bin.
##
## Usage:
##   make                         - build shim.bin
##   make KATAPULT_BIN=path.bin   - use a specific katapult binary
##   make clean                   - remove build artefacts
##   make info                    - show section sizes and verify
##

# ARM toolchain — override with: make CROSS_PREFIX=/path/to/arm-none-eabi-
# By default uses arm-none-eabi-* from PATH.
CROSS_PREFIX ?= arm-none-eabi-

CC      := $(CROSS_PREFIX)gcc
AS      := $(CROSS_PREFIX)as
OBJCOPY := $(CROSS_PREFIX)objcopy
OBJDUMP := $(CROSS_PREFIX)objdump
SIZE    := $(CROSS_PREFIX)size

# Path to pre-built Katapult binary (adjust if yours is elsewhere)
KATAPULT_BIN ?= ../katapult/out/katapult.bin

# Max OTA payload = stock staging area = 112 KB
MAX_SIZE := 114688

CFLAGS := -mcpu=cortex-m3 -mthumb -Os -g \
          -ffunction-sections -fdata-sections \
          -fno-common -nostdlib -Wall -Wextra \
          -ffreestanding

LDFLAGS := -T shim.ld \
           -Wl,--gc-sections \
           -Wl,-Map=shim.map \
           -nostdlib

.PHONY: all clean info

all: shim.bin

# Check that katapult.bin exists before building
katapult.bin: $(KATAPULT_BIN)
	@test -f $(KATAPULT_BIN) || { echo "ERROR: $(KATAPULT_BIN) not found. Build Katapult first."; exit 1; }
	cp $(KATAPULT_BIN) katapult.bin

katapult_payload.o: katapult_payload.S katapult.bin
	$(AS) -mcpu=cortex-m3 -mthumb -o $@ $<

shim.o: shim.c
	$(CC) $(CFLAGS) -c -o $@ $<

shim.elf: shim.o katapult_payload.o shim.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ shim.o katapult_payload.o

shim.bin: shim.elf
	$(OBJCOPY) -O binary $< $@
	@echo ""
	@$(SIZE) $<
	@BINSIZE=$$(stat -c %s $@); \
	 KATSIZE=$$(stat -c %s katapult.bin); \
	 printf "  shim.bin:     %d bytes\n" $$BINSIZE; \
	 printf "  katapult.bin: %d bytes (embedded)\n" $$KATSIZE; \
	 printf "  max OTA:      %d bytes (112 KB staging area)\n" $(MAX_SIZE); \
	 if [ $$BINSIZE -gt $(MAX_SIZE) ]; then \
	   echo "ERROR: shim.bin exceeds 112 KB!"; exit 1; \
	 fi; \
	 echo "  ✓ shim.bin OK"

info: shim.elf
	@$(SIZE) -A $<
	@echo ""
	@echo "--- Disassembly (first 60 lines) ---"
	@$(OBJDUMP) -d $< | head -60
	@echo ""
	@echo "--- Section headers ---"
	@$(OBJDUMP) -h $<

clean:
	rm -f *.o *.elf *.bin *.map
