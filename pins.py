"""Pin assignments."""
SPI_BUS = 0
EINK_CS = 8; EINK_DC = 25; EINK_RST = 17; EINK_BUSY = 24
LORA_CS = 7; LORA_RST = 27; LORA_DIO0 = 22
LORA_FREQ = 868.0; LORA_SF = 9; LORA_BW = 125.0
LORA_CR = 5; LORA_SYNC = 0x12; LORA_POWER = 17
BUZZER = 13  # GPIO 13 (physical pin 33), hardware PWM via pigpio
