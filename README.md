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
- `keyboard.py` — reads `/dev/input/event*` for the paired M4 keyboard, maps scancodes to chars, auto-reconnects if BT drops.
- `lora.py` — SX1276 driver over SPI. Packet format: `MAGIC "KPG" + channel byte + type byte + payload`. Types: `0x01` message (sender + msg_id + text), `0x02` ACK.
- `display_eink.py` — Waveshare V4 driver with background thread and "latest image wins" queue. Full refresh every 20 partial updates. Hardware reset before driver init to clear stuck BUSY.
- `ui.py` — chat / profile / name-edit states, scrolling, message history persistence at `~/.kidpager/history.json` (capped at 100 messages).
- `buzzer.py` — passive piezo via **hardware PWM through pigpio**. Four patterns: `beep_incoming`, `beep_sent`, `beep_ack`, `beep_error`.

### Key design decisions

- **Relative timestamps only.** No GPS, no NTP, no DCF77 — just `now`, `5m`, `2h`, `1d`. Simpler and sufficient for kids.
- **Channel byte in LoRa header.** Lightweight group isolation without infrastructure; receiver drops packets from other channels.
- **E-Ink writes never run on the asyncio loop.** Background thread with a queue; main loop never blocks on display refresh.
- **Hardware PWM for the buzzer via pigpio.** Software PWM (`RPi.GPIO.PWM`) creates CPU interrupts at 2 kHz that corrupt SPI timing during E-Ink partial refresh. `pigpio.hardware_PWM()` is silent on the CPU side and leaves SPI alone.
- **Delivery status with 10-second ACK timeout.** Sent messages start as `~`, become `+` on ACK, `x` after 10 s with no reply.

## Deployment

From Windows over SSH via `deploy.ps1`.

### First time

```powershell
.\deploy.ps1 -Setup          # generates SSH key, copies to both pagers (password once)
.\deploy.ps1 -All            # installs packages, fetches Waveshare driver, copies code, sets up systemd
```

### Bluetooth pairing

```powershell
.\bt.ps1 -PiHost kidpager.local
```

Runs `bt_pair.sh` on the Pi, which does `pair → trust → connect` in a **single piped `bluetoothctl` session** — critical for the SSP link key to actually be written. Running pairing as separate subprocess calls looks successful but silently fails to save the key.

### Updates

```powershell
.\deploy.ps1 -All                       # both devices
.\deploy.ps1 -PiHost kidpager.local     # single device
.\deploy.ps1 -Restart                   # just restart the service
```

### Service

`/etc/systemd/system/kidpager.service` — starts after `bluetooth.target` and `pigpiod.service`, runs as root, restarts on failure.

```bash
sudo systemctl status kidpager
sudo journalctl -u kidpager -f
```

Logs are unbuffered (`PYTHONUNBUFFERED=1` + `python3 -u`).

## Diagnostics

```bash
# LoRa SPI sanity
cd ~/kidpager && python3 test_lora_spi.py
# expects: SX1276 version: 0x12  OK!

# Bluetooth
sudo hciconfig
sudo bluetoothctl
  > devices
  > info <MAC>

# Is the keyboard recognised as an input device?
cat /proc/bus/input/devices | grep -A 4 -i m4

# Run main.py manually
sudo systemctl stop kidpager
cd ~/kidpager && sudo python3 -u main.py

# Buzzer daemon
systemctl status pigpiod
```

## Known gotchas

- **BlueZ pairing:** `bluetoothctl pair` run as separate subprocess calls silently fails to write the link key. Commands must be piped into one `bluetoothctl` session with `sleep` delays so the SSP agent stays alive across the handshake. `bt_pair.sh` already handles this correctly.
- **E-Ink BUSY stuck:** sometimes after a power interruption. `_hw_reset()` in `display_eink.py` runs before driver import to unstick.
- **MT3608 boost fakes "2A":** real sustained current is ~800 mA and it browns out under LoRa TX + E-Ink refresh. CKCS fixed-voltage boost with both A/B jumpers open (= 5 V output) is the working replacement.
- **Passive piezo on GPIO:** needs a 100–220 Ω series resistor to protect the pin from capacitive kickback.
- **Software PWM + SPI:** do **not** use `RPi.GPIO.PWM` for the buzzer while running E-Ink partial refresh — the 2 kHz interrupt stream produces visible corruption in partial updates. Use hardware PWM via pigpio (as this release does).

## Two devices

`kidpager.local` and `kidpager2.local` — identical config, same LoRa channel (default 1). Names are set through the in-device profile menu (Esc → `Name`).

## License

MIT — see `LICENSE`.
