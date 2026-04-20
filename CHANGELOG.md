# Changelog

## v0.9 — 2026-04-20

### Changed
- **Radio: SX1276 → SX1262** migration. Both pagers now use Waveshare Core1262-HF.
  - TX power bumped 17 → 22 dBm (+5 dB, ~1.8× range)
  - Sync word: 0x12 (1 byte) → 0x1424 (2 bytes, SX1262's private-network equivalent)
  - Requires one new wire: `LORA_BUSY` on GPIO 23 (phys pin 16)
  - `LORA_DIO0` renamed to `LORA_DIO1` (same GPIO 22, SX1262 terminology)
  - App-level packet format unchanged — no over-the-air breakage if anyone had
    in-flight messages during the migration (both units are being upgraded together).

### Fixed
- **BlueZ pairing:** `bt_pair.sh` now runs pair / trust / connect in a single
  piped `bluetoothctl` session. Previously, running them as separate subprocess
  calls reported success but silently failed to write the link key because the
  SSP agent died between commands. Result: `Bonded: yes`, HID attaches correctly.
- **E-Ink refreshes on the main loop.** Moved to a background worker thread
  with a "latest image wins" single-slot queue. The main asyncio loop no longer
  blocks for 2 s during full refreshes.
- **SD card write wear:** switched from per-message `history.json` writes to
  a dirty flag + periodic flush every 2 s. Saves on shutdown too.
- **Keyboard MAC hardcoded.** `keyboard.py` now enumerates paired BlueZ
  devices and matches by name hints (`M4`, `KEYBOARD`, `KB`, `BT-KEY`, `HID`)
  so the same code works on both pagers without edits.
- **Terminal scroll padding:** scrollback re-renders now use actual shown
  count instead of `len(messages[-8:])`.
- **Duplicate ack handling:** removed double-slicing in `mark_delivered`.

### Added
- **`diagnose.py`** — 30+ check health script covering system, Python modules,
  files, BT keyboard bond/trust/HID, SX1262 GetStatus, E-Ink draw test,
  buzzer tone. Exit 0/1 for CI-style verification.
- **`test_lora_spi.py`** — SX1262-specific SPI smoke test (GetStatus + BUSY
  handshake check).
- **`test_buzzer.py`** — standalone test for all four beep patterns.
- **`deploy.ps1 -WipeHistory`** — standalone or combinable flag to clear
  `history.json` on one or both pagers.
- **Build pigpio from source in deploy script** — the Bookworm repos dropped
  `pigpio` (it doesn't support the RP1 chip on Pi 5; still works on Pi
  Zero/2/3/4). Deploy handles C library build + pip module + systemd unit +
  `/usr/bin/pigpiod` symlink + `ldconfig` refresh.

### Hardware requirements
- **New wire required:** solder GPIO 23 (phys pin 16) → SX1262 BUSY pin on
  both modules before flashing v0.9. Without it, `diagnose.py` will fail at
  "BUSY LOW after reset".
