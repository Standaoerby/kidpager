# KidPager

Two-device LoRa text messenger for kids. Each unit pairs with a Bluetooth
keyboard, shows incoming messages on an E-Ink screen, beeps when something
happens, and has no internet access by design.

## Hardware (per unit)

| Part | Notes |
| --- | --- |
| Raspberry Pi Zero 2 W | headless, runs from a systemd service |
| Waveshare 2.13" E-Ink HAT V4 | 250×122 mono, SPI |
| SX1262 LoRa module | Waveshare Core1262-HF, 868 MHz, +22 dBm, SPI |
| M4 Bluetooth Classic keyboard | paired via BlueZ (SSP agent) |
| Passive buzzer | GPIO 13 hardware PWM via pigpio |
| LiPo 2000 mAh + CKCS boost | ~12 hours mixed use |

## Pin map (BCM)

| Signal | GPIO | Phys pin |
| --- | --- | --- |
| E-Ink CS (CE0) | 8 | 24 |
| E-Ink DC | 25 | 22 |
| E-Ink RST | 17 | 11 |
| E-Ink BUSY | 24 | 18 |
| LoRa CS (CE1) | 7 | 26 |
| LoRa RST | 27 | 13 |
| LoRa DIO1 | 22 | 15 |
| **LoRa BUSY** | **23** | **16** |
| Buzzer | 13 | 33 |

`LoRa BUSY` is required for SX1262 — the chip uses this handshake line to
signal when it has finished processing a command. Without it the driver
cannot operate safely.

## Software architecture

- `main.py` — asyncio main loop: polls keyboard, updates UI, drives the radio,
  schedules E-Ink refreshes and buzzer beeps.
- `ui.py` — message list + input line. Writes history to SD via a dirty flag
  and periodic flush (every 2 s) to avoid wearing out the card.
- `display_eink.py` — the Waveshare driver runs in a background worker thread
  with a single-slot "latest image wins" queue, so slow refreshes never block
  the main loop.
- `lora.py` — SX1262 driver over SPI. Command-based protocol with BUSY
  handshake; TCXO on DIO3 (3.3 V, 5 ms startup); external RF switch on DIO2.
- `keyboard.py` — reads `/dev/input/event*`. Finds the M4 by scanning paired
  BlueZ devices (matches "M4", "KB", "HID", etc. in the device name).
- `buzzer.py` — short asyncio tones via pigpio hardware PWM.
- `config.py` — JSON config for name and radio channel.

LoRa: 868 MHz, SF 9, BW 125 kHz, CR 4/5, sync word 0x1424 (private network),
TX power 22 dBm. Packet format: `"KPG" | channel | type | payload`, with
`type` = 0x01 (message) or 0x02 (ack).

## Deploy

Windows side, from the project folder:

```powershell
.\deploy.ps1 -Setup            # once — sets up SSH keys to both pagers
.\deploy.ps1 -All               # push code, install pigpio, enable service
.\deploy.ps1 -PiHost kidpager.local   # push to one device
.\deploy.ps1 -Restart           # restart service on both
.\deploy.ps1 -WipeHistory       # clear chat history on both
```

First time you grab the repo on Windows you'll get a "not digitally signed"
error when running the script. Unblock once:

```powershell
Get-ChildItem -Path .\ -Recurse | Unblock-File
```

## Bluetooth keyboard pairing

From the Pi (or via ssh):

```bash
sudo ~/bt_pair.sh
```

Put the M4 into pairing mode (usually `Fn` + some key), then follow the
script. It runs pair / trust / connect in a single `bluetoothctl` session
so the SSP agent stays alive long enough for the link key to write — this
is the only reliable way on BlueZ.

## Diagnostics

```bash
cd ~/kidpager && sudo python3 diagnose.py -y
```

Runs 30+ checks across [System], [Python modules], [Files],
[Bluetooth keyboard], [LoRa radio], [E-Ink display], [Buzzer]. Exit code
0 if everything healthy, 1 if any failure. Flags:

- `-y` — don't prompt before stopping the service
- `--skip-hw` — software/files/BT only
- `--quick` — HW checks but no visible/audible output

## Files

```
kidpager/
├── main.py           asyncio event loop
├── ui.py             message list + input line, history flush
├── display_eink.py   E-Ink driver (worker thread)
├── lora.py           SX1262 radio driver
├── keyboard.py       BT keyboard reader (evdev)
├── buzzer.py         passive buzzer (pigpio)
├── config.py         JSON config
├── pins.py           pin assignments + radio params
├── main.py
├── diagnose.py       full health check
├── test_lora_spi.py  LoRa SPI smoke test
├── test_buzzer.py    buzzer tone test
├── bt_pair.sh        robust M4 pairing script
├── bt.ps1            Windows helper to drive bt_pair.sh over ssh
└── deploy.ps1        push + install from Windows
```

## License

MIT. See `LICENSE`.
