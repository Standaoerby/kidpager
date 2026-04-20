# KidPager 0.9 — Release notes

First public release. The project is running on two devices (`kidpager.local` and `kidpager2.local`) and has moved past the hardware prototyping stage.

## Highlights

- **Working two-device LoRa messaging** at 868 MHz with ACK-based delivery status.
- **Passive piezo buzzer** via hardware PWM (pigpio) — silent on the CPU side, does not interfere with E-Ink SPI.
- **Single-battery power topology**: one LiPo feeds both the Pi (via CKCS boost to 5 V) and the M4 keyboard (direct 3.7 V to the keyboard's own pad).
- **No GPS / NTP / DCF77.** Relative timestamps only (`now`, `5m`, `2h`, `1d`).
- **Windows-based deployment** over SSH via `deploy.ps1` to multiple devices in one command.

## Hardware state

- Raspberry Pi Zero 2 W with pre-soldered headers.
- Waveshare 2.13" E-Ink HAT V4 on top.
- SX1276 LoRa module wired directly to the Pi header pins (no passthrough PCB) — 8 wires soldered as ring-loops into the gap between Pi and HAT.
- CKCS Mini Boost 10 W (replaces the earlier MT3608 which could not sustain current under LoRa TX + E-Ink refresh).
- LiPo 3.7 V 2000 mAh with internal BMS.
- Passive piezo (optional) with 100–220 Ω series resistor on GPIO 13.

## What's new since prototypes

Compared to the earlier ESP32 / Heltec Wireless Paper prototypes:

- Moved from Arduino/C++ to Python 3 + asyncio.
- Full Linux stack (BlueZ, systemd, Python) instead of bare-metal BT-HID workarounds.
- SD-card-friendly history persistence (dirty-flag + periodic flush, ~2 s interval).
- Bluetooth Classic pairing works reliably through a single-session `bluetoothctl` script (the gotcha that bit us for a week).

## Known limitations

- Battery life ~6 h with a 2000 mAh LiPo. A 4000 mAh cell would comfortably hit the 12 h target.
- No WiFi transport. The `channel` field in the profile menu provides lightweight LoRa group isolation — the receiver drops packets whose channel byte does not match.
- No encryption. This was a deliberate simplification for v0.9.
- No OTA update mechanism — updates go through `deploy.ps1` over SSH.

## Known gotchas (baked into the code)

- **E-Ink BUSY stuck after power interruption** — fixed by `_hw_reset()` before driver import in `display_eink.py`.
- **BlueZ pair/trust/connect** must run in a single `bluetoothctl` session — handled by `bt_pair.sh`.
- **Software PWM corrupts E-Ink partial refresh** — use `pigpio.hardware_PWM` for the buzzer (this release).
- **`RPi.GPIO` and `pigpio` both touch `/dev/gpiomem`** — the code orders initialisation so each owns only its own pins.

## Thanks

To the M4 keyboard for dying valiantly during polarity testing, to the first MT3608 boost that refused to tell us it was broken, and to the E-Ink BUSY pin that got stuck just often enough to be interesting.

— Stan, April 2026
