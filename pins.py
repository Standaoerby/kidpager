"""Pin assignments and LoRa radio config for KidPager."""

# --- SPI bus ---
SPI_BUS = 0

# --- E-Ink HAT (Waveshare 2.13" V4, 250x122) ---
EINK_CS   = 8    # SPI CE0 (phys pin 24)
EINK_DC   = 25   # Data/Command  (phys pin 22)
EINK_RST  = 17   # Reset          (phys pin 11)
EINK_BUSY = 24   # Busy input     (phys pin 18)

# --- SX1262 LoRa module (Waveshare Core1262-HF, 868 MHz, +22 dBm) ---
LORA_CS   = 7    # SPI CE1        (phys pin 26)
LORA_RST  = 27   # Reset          (phys pin 13)
LORA_DIO1 = 22   # IRQ line       (phys pin 15)  -- was DIO0 on SX1276
LORA_BUSY = 23   # SX1262 BUSY    (phys pin 16)  -- NEW, required for SX1262

# --- LoRa radio parameters ---
LORA_FREQ  = 868.0   # MHz, EU ISM band
LORA_SF    = 9       # Spreading factor
LORA_BW    = 125.0   # Bandwidth, kHz
LORA_CR    = 5       # Coding rate 4/LORA_CR (5 -> 4/5)
LORA_POWER = 22      # TX power in dBm (SX1262 HP max). Drop to 17 or 14 for battery
LORA_SYNC  = 0x1424  # SX1262 sync word for private network (LoRaWAN public is 0x3444)

# --- Buzzer ---
BUZZER = 13   # GPIO 13 (phys pin 33), hardware PWM via pigpio
