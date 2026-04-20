# KidPager

A two-device LoRa text messenger for kids, built on Raspberry Pi Zero 2 W with an E-Ink display and a Bluetooth keyboard. Off-grid, no SIM, no accounts — two pagers talk to each other directly at 868 MHz.

![version](https://img.shields.io/badge/version-0.9-blue)
![platform](https://img.shields.io/badge/platform-Raspberry%20Pi-red)
![license](https://img.shields.io/badge/license-MIT-green)

## What it does

- Two identical pagers on the same LoRa channel send each other text messages.
- Typed on a Bluetooth keyboard, shown on a 2.13" E-Ink display, delivered over ~1 km of 868 MHz LoRa.
- Delivery status per message: sending `~` → delivered `+` → failed `x`.
- Relative timestamps: `now`, `5m`, `2h`, `1d`.
- Message history persisted on disk, survives reboots.
- Optional piezo buzzer for audio alerts on incoming/sent/delivered/failed.

No internet. No accounts. No tracking. You carry the whole network on your hip.

## Hardware

| Component | Specs |
|---|---|
| Raspberry Pi Zero 2 W | With pre-soldered headers (WH) |
| Waveshare 2.13" E-Ink HAT V4 | 250×122, SPI |
| SX1276 LoRa module | 868 MHz, SPI |
| M4 Bluetooth Classic keyboard | Paired via BlueZ |
| LiPo 3.7 V 2000 mAh | With internal BMS (EEMB or similar) |
| CKCS Mini Boost 10 W | 3.7 V → 5 V, jumpers A/B both open = 5 V |
| Passive piezo buzzer (optional) | GPIO 13 via 100–220 Ω series resistor |

### Power topology

```
USB-C (M4 keyboard case) ──→ M4 native charger ──→ LiPo 2000 mAh (+BMS)
                                                        │
                            ┌───────────────────────────┤
                            │                           │
                       M4 keyboard                 CKCS Boost 5V
                       (direct 3.7 V)                   │
                                                   Pi Zero 2 W
                                                    ├── E-Ink HAT
                                                    ├── SX1276 LoRa
                                                    └── Piezo buzzer
```

Single battery, single USB-C port to charge everything.

### Pin assignments (BCM)

| Function | GPIO | Phys |
|---|---|---|
| E-Ink CS | 8 | 24 |
| E-Ink DC | 25 | 22 |
| E-Ink RST | 17 | 11 |
| E-Ink BUSY | 24 | 18 |
| LoRa CS | 7 | 26 |
| LoRa RST | 27 | 13 |
| LoRa DIO0 | 22 | 15 |
| LoRa VCC (3.3 V) | — | 17 |
| LoRa GND | — | 20 |
| LoRa MOSI | 10 | 19 |
| LoRa MISO | 9 | 21 |
| LoRa SCK | 11 | 23 |
| Buzzer (via 100–220 Ω) | 13 | 33 |
| Buzzer GND | — | 34 |

LoRa: 868 MHz, SF9, BW 125 kHz, CR 4/5, sync 0x12, TX power 17 dBm.

## Software

Python 3 + asyncio, single-process event loop. Runs as a systemd service.

### Modules

- `main.py` — main event loop; coordinates keyboard, LoRa, UI, buzzer.
- `config.py` — JSON config at `~/.kidpager/config.json` (name, channel).
- `pins.py` — GPIO pin assignments and LoRa radio parameters.
- `keyboard.py` — reads `/dev/input/event*` for the paired M4 keyboard, maps scancodes to chars, auto-reconnects if BT drops. Keyboard MAC is discovered dynamically from `bluetoothctl devices` — no hardcoded addresses.
- `lora.py` — SX1276 driver over SPI. Packet format: `MAGIC "KPG" + channel byte + type byte + payload`. Types: `0x01` message (sender + msg_id + text), `0x02` ACK.
- `display_eink.py` — Waveshare V4 driver with background worker thread and "latest image wins" queue. Full refresh every 20 partial updates. Hardware reset before driver init to clear stuck BUSY.
- `ui.py` — chat / profile / name-edit states, scrolling, message history persistence at `~/.kidpager/history.json` (capped at 100 messages). Dirty-flag + periodic flush (~2 s) to avoid SD-card write amplification.
- `buzzer.py` — passive piezo via **hardware PWM through pigpio**. Four patterns: `beep_incoming`, `beep_sent`, `beep_ack`, `beep_error`.

### Tools

- `diagnose.py` — full health check: system, Python modules, files, Bluetooth, LoRa, E-Ink, buzzer.
- `test_lora_spi.py` — SPI sanity check for the SX1276.
- `test_buzzer.py` — standalone buzzer test, plays all 4 patterns.

### Key design decisions

- **Relative timestamps only.** No GPS, no NTP, no DCF77 — just `now`, `5m`, `2h`, `1d`. Simpler and sufficient for kids.
- **Channel byte in LoRa header.** Lightweight group isolation without infrastructure; receiver drops packets from other channels.
- **E-Ink writes never run on the asyncio loop.** Background thread with a single-slot "latest wins" queue; main loop never blocks on display refresh, stale frames never land.
- **Hardware PWM for the buzzer via pigpio.** Software PWM (`RPi.GPIO.PWM`) creates CPU interrupts at 2 kHz that corrupt SPI timing during E-Ink partial refresh. `pigpio.hardware_PWM()` is silent on the CPU side and leaves SPI alone.
- **Delivery status with 10-second ACK timeout.** Sent messages start as `~`, become `+` on ACK, `x` after 10 s with no reply.
- **Dirty-flag history writes.** Mutations flip a flag; the main loop flushes to disk every ~2 s only if dirty. Shutdown flushes one last time.

## Deployment

From Windows over SSH via `deploy.ps1`.

### First time

```powershell
.\deploy.ps1 -Setup          # generates SSH key, copies to both pagers (password once)
.\deploy.ps1 -All            # installs packages, fetches Waveshare driver, copies code, sets up systemd
```

On Bookworm the `pigpio` apt package is gone (upstream dropped it for the RP1 chip on Pi 5). For Pi Zero 2 W the library still works, so `deploy.ps1 -All` builds `joan2937/pigpio` from source, installs the Python module via pip, drops in a systemd unit, and refreshes the dynamic linker cache. Idempotent — skips the build on second run.

### Bluetooth pairing

```powershell
.\bt.ps1 -PiHost kidpager.local
```

Runs `bt_pair.sh` on the Pi, which does `agent on → pair → trust → connect` in a **single piped `bluetoothctl` session** — critical for the SSP link key to actually be written. Running pairing as separate subprocess calls looks successful (`Paired: yes`) but leaves `Bonded: no` — HID never attaches, no `/dev/input/eventN` appears.

### Updates

```powershell
.\deploy.ps1 -All                       # both devices
.\deploy.ps1 -PiHost kidpager.local     # single device
.\deploy.ps1 -Restart                   # just restart the service
.\deploy.ps1 -WipeHistory               # wipe message history on both pagers
.\deploy.ps1 -All -WipeHistory          # deploy + wipe
```

### Service

`/etc/systemd/system/kidpager.service` — starts after `bluetooth.target` and `pigpiod.service`, runs as root, restarts on failure.

```bash
sudo systemctl status kidpager
sudo journalctl -u kidpager -f
```

Logs are unbuffered (`PYTHONUNBUFFERED=1` + `python3 -u`).

## Diagnostics

After deployment, run the full health check:

```bash
ssh pi@kidpager.local "cd ~/kidpager && sudo python3 diagnose.py -y"
```

`diagnose.py` covers SPI, Bluetooth services, Python modules, files on disk, BT keyboard state (Paired / Bonded / Trusted / Connected), SX1276 register read + full radio init, E-Ink draw + deep-sleep cycle, and a buzzer tone test. Exit code is `0` on success, `1` if anything failed — convenient for CI-style post-deploy verification.

Flags: `-y` auto-approves stopping the service for HW tests; `--skip-hw` does software/files/BT only (safe while service is running); `--quick` runs register/pin checks without the E-Ink draw or buzzer tone.

### Manual spot-checks

```bash
# LoRa SPI sanity
cd ~/kidpager && python3 test_lora_spi.py
# expects: SX1276 version: 0x12  OK!

# Buzzer tones
sudo systemctl stop kidpager
cd ~/kidpager && sudo python3 test_buzzer.py
sudo systemctl start kidpager

# Bluetooth state
bluetoothctl devices
bluetoothctl info <MAC> | grep -E "Paired|Bonded|Trusted|Connected"

# Keyboard attached as HID?
cat /proc/bus/input/devices | grep -A 4 -i m4

# Run main.py manually
sudo systemctl stop kidpager
cd ~/kidpager && sudo python3 -u main.py

# pigpio daemon
systemctl status pigpiod
```

## Known gotchas

- **BlueZ pairing:** `bluetoothctl pair` run as separate subprocess calls silently reports success but leaves `Bonded: no` — the HID profile never attaches. Commands must be piped into one `bluetoothctl` session with `sleep` delays so the SSP agent stays alive across the handshake. `bt_pair.sh` already handles this correctly.
- **E-Ink BUSY stuck:** sometimes after a power interruption. `_hw_reset()` in `display_eink.py` runs before driver import to unstick it.
- **MT3608 boost fakes "2A":** real sustained current is ~800 mA and it browns out under LoRa TX + E-Ink refresh. CKCS fixed-voltage boost with both A/B jumpers open (= 5 V output) is the working replacement.
- **Passive piezo on GPIO:** needs a 100–220 Ω series resistor to protect the pin from capacitive kickback.
- **Software PWM + SPI:** do **not** use `RPi.GPIO.PWM` for the buzzer while running E-Ink partial refresh — the 2 kHz interrupt stream produces visible corruption in partial updates. Use hardware PWM via pigpio (as this release does).
- **pigpio on Bookworm:** the apt package was removed. `deploy.ps1` builds from source and handles three gotchas in a row: `make install` fails on Py3.12 (distutils removed) → we install the Python module via pip instead; the pip wheel ships a `pigpiod.service` pointing at `/usr/bin/pigpiod` → we symlink from `/usr/local/bin/pigpiod`; `libpigpio.so.1` lands in `/usr/local/lib/` which the dynamic linker does not cache until `ldconfig` runs → deploy.ps1 runs it explicitly.

## Two devices

`kidpager.local` and `kidpager2.local` — identical config, same LoRa channel (default 1). Names are set through the in-device profile menu (Esc → `Name`).

## License

MIT — see `LICENSE`.
