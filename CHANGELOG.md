# Changelog

## v0.12 — 2026-04-21

### Fixed
- **Buzzer tone race condition.** Concurrent `asyncio.create_task(beep_*)` calls
  (e.g. sending + receiving in the same 10 ms tick) used to clobber each
  other's `hardware_PWM` state, leaving the piezo silent or stuck on.
  `Buzzer.tone()` now serialises via a lazy `asyncio.Lock`, so overlapping
  beeps queue cleanly instead of overlapping. Alarm + ack + incoming all
  chain audibly.
- **`diagnose.py` missed `power.py`** in the file-presence check — a
  silently-missing `power.py` on the deployed device would break Alt+W
  without the diagnostic flagging it. Now included.

### Changed
- **`display_eink.py` _build_message_lines** cleaned up: dropped the unused
  `is_continuation` tuple field; rendered rows are now 2-tuples
  `(line_text, timestamp_or_None)`. Shorter call site, same output.
- **`deploy.ps1` auto-installs the SSH key on first contact.** Previously
  you had to run `-Setup` manually before `-All` or suffer a password
  prompt on every single SSH invocation (dozens per deploy). Now any
  command (`-All`, `-Restart`, `-Diag`, `-WipeHistory`, ...) transparently
  installs `~\.ssh\id_kidpager.pub` on a fresh pager (single password
  prompt per device), then proceeds with zero prompts. Re-running is
  idempotent: the install is `grep -qxF`-guarded so dupes don't accumulate
  in `authorized_keys`.
- **`deploy.ps1` fails fast on missing passwordless sudo.** Added an
  explicit `sudo -n true` probe after key install. Bad NOPASSWD config
  used to cause a silent hang on step 2's first `sudo` call. Now prints
  a copy-pasteable one-liner fix and skips the target.
- **`deploy.ps1` pre-flight.** Autogenerates `~\.ssh\id_kidpager` if
  missing (ed25519, no passphrase, `-C kidpager-deploy`). Checks for
  OpenSSH client on Windows with a clear install-instruction message if
  absent. Added `-Help` flag.
- **`deploy.ps1` summary.** Per-target OK/FAIL status and total elapsed
  seconds at the end; exit 0 only if every target succeeded.
- **`beep_alarm` tamed.** The wake-from-sleep pattern was a 6-tone rising
  siren up to 3200 Hz (~1 s). Replaced with 3 short equal beeps at 2 kHz
  (80 ms each, 120 ms gap, ~600 ms total). Still distinct from
  `beep_incoming` (two rising beeps) so the user can tell "new while
  asleep" from "new while awake", without the earlier shriek.

## v0.11 — 2026-04-21

### Added
- **Silent mode** in the profile menu (TAB → Silent: ON/OFF, toggled with
  ENTER). When on, the buzzer is muted for every event — sent, incoming,
  ack, error, and the new wake alarm. Persisted in `config.json` so it
  survives reboots. Header shows an **`M`** badge while muted (next to the
  existing **`W`** Wi-Fi badge and the LoRa indicator).
- **Screen saver / sleep state** after `IDLE_TIMEOUT = 300 s` (5 min) of
  no user input and no incoming messages in `chat` state. Shows a minimal
  `Zzz` + owner name + hint screen to reduce E-Ink wear. Auto-sleep only
  triggers from chat, never from the profile menu (so an open menu
  doesn't disappear on you).
- **Wake transitions.** Any keypress wakes into chat (the key itself is
  not consumed — an accidental bump doesn't start typing). An incoming
  message also wakes, and triggers the new `beep_alarm` (a rising
  6-tone siren, ~1 s) instead of the short `beep_incoming` — louder
  because the user isn't looking at the screen. Silent mode mutes the
  alarm too.
- **`Buzzer.set_silent(bool)`** — single-flag gate on every tone, so
  silent-mode enforcement lives entirely in `buzzer.py` and the rest of
  the codebase doesn't need conditional beep calls.

### Changed
- **Profile menu is now 4 items** (Name, Channel, Silent, Back). ENTER on
  Silent toggles in place; ENTER on Back or ESC/TAB saves and returns to
  chat.
- **`config.json` gains a `silent` field** (default `false`). Older
  configs without it load cleanly via `.get()` and get the new field on
  next save — no migration needed.
- **Main loop skips E-Ink redraws while asleep** for ack/timeout events.
  An incoming message is the one thing that wakes the screen; everything
  else stays on the sleep view to preserve the panel.

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
