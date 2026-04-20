# Changelog

All notable changes to KidPager will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9] — 2026-04-20

First public release.

### Added

- Two-device LoRa messaging at 868 MHz with ACK-based delivery status (`~` sending, `+` delivered, `x` timed out after 10 s).
- Bluetooth Classic keyboard support via BlueZ with auto-reconnect on drop. Keyboard MAC discovered dynamically from `bluetoothctl devices` — no hardcoded addresses.
- Waveshare 2.13" V4 E-Ink display driver with background worker thread, single-slot "latest image wins" queue, and full-refresh-every-20 cycle. Main loop never blocks on display refresh.
- Profile menu: name editing, channel selector.
- Message history persistence at `~/.kidpager/history.json`, capped at 100 messages. Dirty-flag + periodic flush (~2 s) to avoid SD-card write amplification.
- Relative timestamps (`now`, `5m`, `2h`, `1d`).
- Passive piezo buzzer support via hardware PWM through `pigpio`, with four event patterns (incoming / sent / ack / error).
- Windows-based deployment script `deploy.ps1` targeting multiple devices by mDNS hostname. Flags: `-Setup`, `-All`, `-PiHost`, `-Restart`, `-WipeHistory`.
- Bluetooth pairing helper `bt_pair.sh` / `bt.ps1` using a single piped `bluetoothctl` session so the SSP agent stays alive across the full handshake.
- systemd service with `pigpiod` dependency and unbuffered logging.
- Full health-check script `diagnose.py` covering system services, Python modules, files, BT keyboard state (including Bonded check), SX1276 register + init, E-Ink draw + deep-sleep cycle, and buzzer tone.
- Standalone test scripts: `test_lora_spi.py` (SX1276 version register) and `test_buzzer.py` (all 4 beep patterns).

### Changed

- Power supply: moved from MT3608 (unreliable under load) to CKCS Mini Boost 10 W with fixed 5 V via A/B jumpers.
- Buzzer driver: moved from `RPi.GPIO.PWM` (software PWM, corrupts E-Ink partial refresh) to `pigpio.hardware_PWM` (true hardware PWM, SPI-safe).
- Hardware layout: removed the passthrough PCB — LoRa module now wired directly to Pi header pins as ring-loops in the Pi↔HAT gap.

### Fixed

- BlueZ pairing silently dropping the link key when `pair`, `trust`, `connect` were run as separate subprocesses — reported `Paired: yes` but left `Bonded: no`, so HID profile never attached and `/dev/input/eventN` was never created.
- Bluetooth keyboard MAC was previously hardcoded in `keyboard.py::_bt_try` — replaced with dynamic enumeration from `bluetoothctl devices`, filtered by keyboard-like name patterns.
- E-Ink `BUSY` pin stuck high after unclean shutdown — resolved by hardware reset before driver import, and by deep-sleeping the display on clean shutdown.
- E-Ink partial refresh blocked the main asyncio loop for ~300 ms per update — moved to background worker thread with latest-wins queue.
- SD-card write amplification from per-message history writes — replaced with dirty-flag + periodic flush (~2 s).
- Padding miscalculation in terminal redraw on scroll.
- Duplicate message-slicing logic in scroll handling.
- Terminal logs not appearing in `journalctl` — now unbuffered via `PYTHONUNBUFFERED=1` and `python3 -u`.
- `deploy.ps1` failing to install `pigpio` on Raspberry Pi OS Bookworm (upstream dropped the package for the RP1 chip on Pi 5) — now builds `joan2937/pigpio` from source, installs the Python module via pip (to bypass the Py3.12 distutils removal), symlinks `/usr/bin/pigpiod` → `/usr/local/bin/pigpiod` for the pip-shipped systemd unit, and runs `ldconfig` so the linker finds `libpigpio.so.1`.
