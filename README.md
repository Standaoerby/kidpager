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
| M4 Bluetooth Classic keyboard | paired via BlueZ (SSP agent); body reused as the pager case |
| Passive buzzer | GPIO 13 hardware PWM via pigpio |
| LiPo 2000 mAh (JL 505060) | shared between Pi and keyboard |
| External TP4056 + DW01A + FS8205A | 1 A charger + protection; USB-C in |
| CKCS boost converter | 3.7 V → 5 V for the Pi, takes TP4056 OUT+/OUT− |

The M4 keyboard's onboard LP4068 charger is left in place but not wired
to the battery — charging goes through the external TP4056's USB-C port
instead. See [BOM.md](BOM.md) for the power topology diagram and why it
has to be this way (short version: the LP4068 is factory-tuned for the
stock 300 mAh cell and can't keep up with the Pi's draw on a 2000 mAh
cell, so the battery was always discharging while the pager was on).

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

For the full parts list, wiring tables, and assembly order see [BOM.md](BOM.md).

## Software architecture

- `main.py` — asyncio main loop: polls keyboard, updates UI, drives the radio,
  schedules E-Ink refreshes and buzzer beeps.
- `ui.py` — message list + input line. Writes history to SD via a dirty flag
  and periodic flush (every 2 s) to avoid wearing out the card.
- `display_eink.py` — the Waveshare driver runs in a background worker thread
  with a single-slot "latest image wins" queue, so slow refreshes never block
  the main loop. Sleep screen forces a full refresh to avoid partial-refresh
  ghosting on the long-lived screen-saver frame.
- `lora.py` — SX1262 driver over SPI. Command-based protocol with BUSY
  handshake; TCXO on DIO3 (3.3 V, 5 ms startup); external RF switch on DIO2.
- `keyboard.py` — reads `/dev/input/event*`. Finds the M4 by scanning paired
  BlueZ devices (matches "M4", "KB", "HID", etc. in the device name).
- `buzzer.py` — short asyncio tones via pigpio hardware PWM.
- `power.py` — Wi-Fi rfkill toggle + state query, used by the Alt+W hotkey.
- `config.py` — JSON config for name, radio channel, silent mode.

## Power-saving

At boot, `kidpager-power.service` (oneshot, runs before `kidpager.service`)
applies:

- `rfkill block wifi` — radio down (~40-60 mA saved)
- CPU governor → `powersave` on all cores (~20-40 mA)
- ACT LED trigger → `none` (stops the blink, saves ~1 mA)

BT stays up — the M4 keyboard needs it.

### Hotkeys

| Combo | Action |
| --- | --- |
| `Enter` | send message |
| `Esc` / `Tab` | open / close profile menu |
| `Alt+O` / `Alt+L` | scroll chat up / down (M4 has no arrow keys) |
| `Alt+W` | toggle Wi-Fi rfkill (for SSH debugging); `W` badge on header while ON |

Wi-Fi state is runtime-only: every reboot returns to blocked.

### Auto-sleep

After 5 minutes of no activity in the chat view, the pager drops to a
minimal `Zzz` + owner name screen saver to reduce E-Ink wear. Any key
or incoming message wakes it. Incoming while asleep plays a distinct
alarm pattern (silent mode mutes it).

Auto-sleep is suppressed while Wi-Fi is on, so an SSH session isn't
interrupted mid-command by a full-refresh flash.

## Radio

LoRa: 868 MHz, SF 9, BW 125 kHz, CR 4/5, sync word 0x1424 (private network),
TX power 22 dBm. Packet format: `"KPG" | channel | type | payload`, with
`type` = 0x01 (message) or 0x02 (ack).

## Deploy

Windows side, from the project folder:

```powershell
.\deploy.ps1 -Setup            # once — sets up SSH keys to both pagers
.\deploy.ps1 -All               # push code, install pigpio, enable service
.\deploy.ps1 -PiHost kp3.local   # push to one device
.\deploy.ps1 -Restart           # restart service on both
.\deploy.ps1 -WipeHistory       # clear chat history on both
.\deploy.ps1 -Diag              # run diagnose.py on both
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

`diagnose.py` is the **single unified health check** — one command verifies
every subsystem (system services, Python + project modules, files,
Bluetooth keyboard, power-save config, LoRa radio, E-Ink display, buzzer).

On the pager directly:
```bash
cd ~/kidpager && sudo python3 diagnose.py -y
```

Remotely from Windows (runs on both pagers, prints per-device summary):
```powershell
.\deploy.ps1 -Diag                          # both pagers
.\deploy.ps1 -Diag -PiHost kp3.local   # one pager
```

Exit code 0 if everything healthy, 1 if any failure. Flags:

- `-y` — don't prompt before stopping the service
- `--skip-hw` — software/files/BT only
- `--quick` — HW checks but no visible/audible output

Standalone per-subsystem smoke tests (copied only with `deploy.ps1 -Tests`):
- `test_lora_spi.py` — SX1262 GetStatus via SPI
- `test_buzzer.py` — all four beep patterns
- `test_power.py` — rfkill / governor / LED / kidpager-power.service state
- `test_retry.py` — outgoing-message retry state machine (no hardware)

## Files

```
kidpager/
├── main.py              asyncio event loop
├── ui.py                message list + input line, history flush, sleep state
├── display_eink.py      E-Ink driver (worker thread, force-full on sleep)
├── lora.py              SX1262 radio driver
├── keyboard.py          BT keyboard reader (evdev)
├── buzzer.py            passive buzzer (pigpio)
├── power.py             Wi-Fi rfkill toggle + state query
├── config.py            JSON config
├── pins.py              pin assignments + radio params
├── diagnose.py          full health check
├── test_lora_spi.py     LoRa SPI smoke test
├── test_buzzer.py       buzzer tone test
├── test_power.py        power-saving config check
├── test_retry.py        retry state machine unit tests
├── bt_pair.sh           robust M4 pairing script
├── kidpager-power.sh    boot-time power-save applier (installed to /usr/local/bin)
├── bt.ps1               Windows helper to drive bt_pair.sh over ssh
└── deploy.ps1           push + install from Windows
```

## License

MIT. See `LICENSE`.
