#!/usr/bin/env python3
import spidev, RPi.GPIO as GPIO, time
from pins import *
GPIO.setwarnings(False); GPIO.setmode(GPIO.BCM)
GPIO.setup(LORA_RST, GPIO.OUT)
GPIO.output(LORA_RST, GPIO.LOW); time.sleep(0.01)
GPIO.output(LORA_RST, GPIO.HIGH); time.sleep(0.05)
spi = spidev.SpiDev(); spi.open(SPI_BUS, 1)
spi.max_speed_hz = 1000000; spi.mode = 0
ver = spi.xfer2([0x42, 0x00])[1]
print(f"SX1276 version: 0x{ver:02X}")
print("OK!" if ver == 0x12 else "FAIL")
spi.close(); GPIO.cleanup()
