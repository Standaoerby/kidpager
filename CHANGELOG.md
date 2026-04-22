# Changelog

## v0.14 — 2026-04-22

### Fixed
- **Dropped characters while typing.** Fast typing ("hello") sometimes
  came out as "hllo" or "helo". Root cause was the main asyncio loop
  owning the evdev read path: whenever a full E-Ink refresh (~2 s) or
  a blocking BT reconnect (the old `time.sleep(2)` in
  `keyboard.reconnect`) stretched a tick beyond the kernel's evdev
  buffer, keystrokes were silently dropped. Fix: the keyboard reader
  now runs in a background daemon thread that drains `/dev/input/event*`
  into a 256-slot `collections.deque` via bulk `os.read` of up to 32
  events per syscall, using `select.poll` with a 50 ms tick that is
  immune to main-loop starvation. The main loop polls that deque via
  the new `kb.poll()` (non-blocking, O(1)) and drains up to 128 keys
  per tick instead of the old 20.
- **Auto-repeat appeared as input freeze.** Holding a key generated
  `value=2` (KEY_REPEAT) events from the kernel, but the previous
  decoder only accepted `value=1` (KEY_PRESS). So holding 'h' produced
  exactly one character until release. Now repeats are accepted with
  a 50 ms per-keycode debounce (`REPEAT_DEBOUNCE_S`) — deliberate fast
  typing at ~15 keys/sec (~66 ms between keys) sails through, while
  kernel-level contact bounce of the same keycode within 50 ms is
  folded into a single press.
- **Reconnect no longer blocks the main loop for 2 s.** `kb.reconnect()`
  previously called `time.sleep(2)` directly, freezing input handling
  long enough to overflow the evdev buffer. Now reconnect spawns a
  transient `bluetoothctl connect` worker thread and does a bounded
  200 ms rescan of `/dev/input/event*` — the caller returns within
  ~200 ms worst case and the reader resumes as soon as the fd
  reappears.
- **Systemd journal spam during typing.** `ui._term_redraw()` was
  pumping full-screen escape sequences into the journal on every
  keystroke (stdout under systemd is the journal, not a TTY). Added
  `sys.stdout.isatty()` guard — redraw is skipped entirely when not
  on a terminal, which also measurably cuts per-keystroke latency.

### Added
- **Terminus bitmap font for message rendering.** DejaVu Sans at 12 pt
  on a 1-bpp 250×122 panel rounded anti-aliased mid-pixels to solid
  black, making narrow letter pairs like 'ov' in "love" visually
  touch. Terminus ships as hand-pixelled bitmap glyphs designed
  specifically for mono console rendering — no AA rounding, no
  touching. Body font becomes Terminus 14 px where available; falls
  back to DejaVu Sans 12 pt if Terminus isn't installed. Header
  (owner name, badges) stays DejaVu Bold — proportional looks better
  for varied-width labels. Startup log line shows which font was
  actually loaded so a bad deploy is visible in `journalctl`. **Deploy
  note:** add `fonts-terminus-otb` to the `apt install` line in
  `deploy.ps1` step 1/8 alongside the existing `fonts-dejavu-core`;
  without it the driver silently falls back to DejaVu.
- **Static underscore cursor on input lines.** An `_` is drawn
  immediately after the last character of the input buffer in both
  the chat view and the name-edit view, with a reserved pixel budget
  so long input that triggers tail-view trimming (leading chars
  dropped + "." marker) never hides the cursor. Static, not blinking
  — an animated cursor would require a partial refresh every ~500 ms
  which is needless E-Ink wear. Caret movement into the middle of
  the input buffer is **not** supported in v0.14 (LEFT/RIGHT remain
  menu/scroll keys); use backspace to edit mid-line.
- **Emoji shortcuts in the input buffer.** 15 common text-face
  sequences get replaced on-the-fly with Unicode emoji: `:)` `:(`
  `:D` `:P` `:O` `;)` `<3` `:|` `:*` `xD` `XD` `:'(` `^_^` `o_O` `O_o`.
  Replacement happens at the trailing edge after each printable key,
  so the E-Ink view shows the final character as the user types.
  `get_message()` also runs a full-buffer expansion at send time so
  shortcuts the user typed past without pausing still reach the air.
  The emoji travel as plain UTF-8 in the existing LoRa payload — no
  protocol change, no compat break. Backspace on an emoji with a
  Unicode variation selector (e.g. ❤️ = heart + VS16) deletes the
  full visible glyph in one press.

### Changed
- **`keyboard.py` almost entirely rewritten.** New surface:
  `poll()` (non-blocking deque pop, replaces `read_key_sync` which
  stays as an alias for one release), `queue_depth()`, `dropped()`.
  Internals: background `kb-reader` thread, `REPEAT_DEBOUNCE_S=0.050`
  per-key debounce, `QUEUE_MAX=256` ring buffer with overflow counter,
  bulk `os.read(fd, EVENT_SIZE*32)`, non-blocking `_bt_connect_async`
  helper for reconnect. Modifier state (shift, alt) still tracked in
  the reader thread.
- **`main.py` drain loop.** `KB_DRAIN_MAX` raised from 20 to 128 so a
  full-deque burst is processed in one tick. Dropped-key counter
  warns into the log if the main loop ever starves the consumer.
  `got_typing` flag — only printable characters trigger a debounced
  redraw, modifier-only events don't reset the settle timer.
- **`display_eink.py` font loader.** `_load_font(paths, size)` tries
  a list of candidates in order; Terminus canonical Debian Bookworm
  paths checked first, then DejaVu, then Pillow's bitmap default.
  Cursor rendering reserves pixel width before tail-view trimming so
  it is always visible at the input line's right edge.
- **`ui.py` emoji table.** Sorted longest-first at module load so
  `:'(` matches before `:(` and `XD` before `X`. `apply_emoji_shortcuts`
  (trailing-only) runs on every printable keystroke;
  `expand_emoji_in_full` (global) runs inside `get_message()`.

### Unchanged (verified during release check)
- All 15 emoji shortcuts expand correctly in unit tests, including
  trailing-only + full-expand + backspace-over-VS16 edge cases.
- Sleep → wake → chat transition still works; `wifi_on` gate on
  auto-sleep from v0.13 unchanged.
- E-Ink worker thread + "latest image wins" queue unchanged —
  rendering stays off the main loop.

## v0.13 — 2026-04-22

### Fixed
- **Sleep screen ghosting.** Entering the idle screen-saver used partial
  refresh like every other frame, so the `Zzz` + name view landed on top
  of the previous chat view and lightly "remembered" the text
  underneath — permanently visible for the hours the pager might sit
  asleep. `draw_sleep()` now forces a full refresh via a new
  `force_full=True` path through `_submit()` → `_render()`. One ~2 s
  flash on sleep entry, then a clean screen saver for as long as it sits.
- **Auto-sleep no longer ambushes SSH sessions.** When Wi-Fi is on
  (Alt+W), the pager is almost always in a live deploy/debug session;
  the screen-saver full-refresh flash mid-`deploy.ps1` is maximally
  annoying. Auto-sleep is now gated behind `not ui.wifi_on`. Turn
  Wi-Fi back off and normal screen-saver behavior resumes. The user
  can still wake a sleeping pager by pressing any key as before.

### Changed
- **`display_eink._submit(img, force_full=False)`** gains a keyword flag.
  Sticky semantics: if a force-full submission is overtaken by a later
  plain submission before the worker picks it up, the force-full
  property is preserved for the winning frame. Better to flash once
  extra than leave ghosting on a long-lived frame.
- **`display_eink._render(img, force_full=False)`** short-circuits the
  partial-refresh counter on force-full and resets `updates = 0` so the
  cadence continues normally afterwards.
- **`display_eink.EInkDisplay.__init__`** adds `_pending_force_full`
  state; `clear()` resets it along with `_pending`.

### Unchanged (verified during release check)
- Cross-module API stable: all 16 `ui.*` references from `main.py`,
  all 7 `buzzer.*` methods, both `power.*` functions, all 5
  `display_eink` draw methods match their call sites exactly.
- Retry state machine (`test_retry.py`): 7/7 tests pass.
- Sleep state machine smoke tests: `enter_sleep` → key → `wake`
  transition verified; `wifi_on` set/clear round-trips through
  `set_wifi()`.

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

### Hardware (power topology rework)
- **Charger moved off the M4 board onto an external TP4056+DW01A+FS8205A
  module.** The M4 keyboard's onboard LP4068 was factory-tuned for its
  stock 300 mAh cell (ISET resistor → ~100 mA charge current). With a
  2000 mAh cell and the Pi drawing ~333 mA from the battery during use,
  the battery was *always* discharging whenever the pager was on — USB
  plugged in or not — and eventually latched the BMS at 2.5 V (RIP one
  EEMB cell). New topology: USB-C on the TP4056 module, TP4056 delivers
  1 A into the cell (0.5 C — textbook for 2000 mAh), DW01A cuts off
  cleanly at 2.5 V so a repeat of the latch-then-dendrite failure is
  physically impossible. Replacement cell: JL 505060 2000 mAh. The M4's
  LP4068 stays on its PCB but is no longer connected to the battery;
  its USB-C is unused. Net: charges while playing, can't self-destruct.

### Unchanged
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
