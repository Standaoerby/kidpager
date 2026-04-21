# Changelog

## v0.10 — 2026-04-21

### Changed
- **Message rendering is now multi-line.** Long messages are word-wrapped to
  fit the 250 px width instead of being truncated to 32 characters with `..`.
  Continuation lines are indented 2 spaces so they visually link to the
  parent message. Words longer than the screen width fall back to
  character-break.
- **E-Ink layout.** Message area holds 6 text lines (14 px each, was 5 × 17).
  A very long message can now push older messages off-screen; use `UP`/`DOWN`
  to scroll back, same as before. Scroll bound extended so the user can
  reach the very first message in history.
- **Refresh strategy explicit.** Typing triggers two-pronged debounce:
  `TYPING_SETTLE = 0.3 s` (redraw after the user pauses) plus
  `TYPING_MAX_STALE = 1.5 s` (force-redraw during continuous typing so the
  user sees what they're entering on the E-Ink, not just the SSH terminal).
  Non-typing events (incoming message, ack, profile navigation) bypass
  debounce and redraw immediately. Strategy documented at the top of
  `main.py`.
- **Input line tail-view.** When the input buffer overflows the visible
  width, leading characters are dropped (not trailing) and a `>.` prefix
  indicates truncation, so the user always sees what they just typed.

### Added
- **Timestamps on the E-Ink.** Each message now shows a relative time
  (`now`, `5m`, `2h`, `3d`) right-aligned at the top of its first line in
  small font. Matches the terminal output.
- **Visible retry indicator.** While a message is being retransmitted, its
  status badge shows the attempt count: `[~1]`, `[~2]`, ... instead of a
  bare `[~]`. The user can tell at a glance whether a stuck message is
  still trying or has given up (`[x]`).
- **`test_retry.py`** — 7 unit tests covering the retry state machine:
  initial state, no-retry-before-timeout, retry fires with same `msg_id`,
  MAX_RETRIES exhaustion → FAIL, ack halts retry sequence, incoming dedup
  on repeated `msg_id`, graceful degradation when `lora=None`. Uses mock
  LoRa and mock E-Ink; runs without any hardware. Shipped with
  `deploy.ps1 -Tests`.

### Unchanged (but worth noting)
- Retry timing parameters are the same as v0.9:
  `ACK_TIMEOUT = 4 s`, `MAX_RETRIES = 2` (→ 3 total attempts),
  `check_timeouts()` runs every 2 s from the main loop.
  Worst-case time-to-FAIL is ~14 s. Constants exposed at the top of `ui.py`
  with a docstring explaining the policy.
- Packet format on the air is identical — no over-the-air breakage.

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
