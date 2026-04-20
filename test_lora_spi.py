#!/usr/bin/env python3
"""SX1262 SPI smoke test.

Resets the chip, waits for BUSY to drop, then issues GetStatus (0xC0).
A healthy chip returns status byte with chipmode bits [6:4] = 0x2 (STDBY_RC)
or 0x3 (STDBY_XOSC) after reset. Anything else = wiring or module problem.

Unlike SX1276 (which had a fixed version register at 0x42), SX1262 has no
simple chip-ID register -- GetStatus is the canonical way to probe the chip.
"""
import sys
import time
import spidev
import RPi.GPIO as GPIO
from pins import SPI_BUS, LORA_RST, LORA_BUSY

OP_GET_STATUS = 0xC0


def fail(msg):
    print(f"FAIL: {msg}")
    try:
        GPIO.cleanup()
    except Exception:
        pass
    sys.exit(1)


def main():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LORA_RST,  GPIO.OUT)
    GPIO.setup(LORA_BUSY, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # Reset pulse (datasheet 12.1: NRESET low >100us, then 3.5-30ms recovery)
    GPIO.output(LORA_RST, GPIO.LOW)
    time.sleep(0.001)
    GPIO.output(LORA_RST, GPIO.HIGH)
    time.sleep(0.010)

    # Wait BUSY low
    t0 = time.time()
    while GPIO.input(LORA_BUSY):
        if time.time() - t0 > 0.1:
            fail("BUSY stuck HIGH after reset -- check LORA_BUSY wiring (GPIO 23, phys pin 16)")
        time.sleep(0.0001)
    print("BUSY LOW after reset: OK")

    spi = spidev.SpiDev()
    spi.open(SPI_BUS, 1)        # CE1 = LORA_CS (GPIO 7)
    spi.max_speed_hz = 1_000_000
    spi.mode = 0

    # GetStatus: send [0xC0, NOP] -> receive [garbage, status]
    r = spi.xfer2([OP_GET_STATUS, 0x00])
    status = r[1]
    chipmode = (status >> 4) & 0x07
    cmdstat  = (status >> 1) & 0x07

    chipmode_names = {
        0x02: "STDBY_RC", 0x03: "STDBY_XOSC",
        0x04: "FS", 0x05: "RX", 0x06: "TX",
    }
    print(f"status = 0x{status:02X}  chipmode = 0x{chipmode} ({chipmode_names.get(chipmode, '?')})  cmdstat = 0x{cmdstat}")

    spi.close()
    GPIO.cleanup()

    if chipmode in (0x02, 0x03):
        print("SX1262 SPI OK")
        sys.exit(0)
    else:
        fail(f"expected STDBY_RC (0x2) or STDBY_XOSC (0x3), got 0x{chipmode}")


if __name__ == "__main__":
    main()
