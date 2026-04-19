# Changelog

All notable changes to KidPager will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9] — 2026-04-19

First public release.

### Added
- Two-device LoRa messaging at 868 MHz with ACK-based delivery status (`~` sending, `+` delivered, `x` timed out after 10 s).
- Bluetooth Classic keyboard support via BlueZ with auto-reconnect on drop.
- Waveshare 2.13" V4 E-Ink display driver with background-thread refresh and full-refresh-every-20 cycle.
- Profile menu: name editing, channel selector.
- Message history persistence at `~/.kidpager/history.json`, capped at 100 messages.
- Relative timestamps (`now`, `5m`, `2h`, `1d`).
- Passive piezo buzzer support via hardware PWM through `pigpio`, with four event patterns (incoming / sent / ack / error).
- Windows-based deployment script `deploy.ps1` targeting multiple devices by mDNS hostname.
- Bluetooth pairing helper `bt_pair.sh` / `bt.ps1` using a single piped `bluetoothctl` session.
- systemd service with `pigpiod` dependency and unbuffered logging.
- LoRa SPI sanity test `test_lora_spi.py`.

### Changed
- Power supply: moved from MT3608 (unreliable under load) to CKCS Mini Boost 10 W with fixed 5 V via A/B jumpers.
- Buzzer driver: moved from `RPi.GPIO.PWM` (software PWM, corrupts E-Ink partial refresh) to `pigpio.hardware_PWM` (true hardware PWM, SPI-safe).
- Hardware layout: removed the passthrough PCB — LoRa module now wired directly to Pi header pins as ring-loops in the Pi↔HAT gap.

### Fixed
- BlueZ pairing silently dropping the link key when `pair`, `trust`, `connect` were run as separate subprocesses.
- E-Ink `BUSY` pin stuck high after unclean shutdown — resolved by hardware reset before driver import.
- SD-card write amplification from per-message history writes — replaced with dirty-flag + periodic flush (~2 s).
- Padding miscalculation in terminal redraw on scroll.
- Duplicate message-slicing logic in scroll handling.
- Terminal logs not appearing in `journalctl` — now unbuffered via `PYTHONUNBUFFERED=1` and `python3 -u`.
