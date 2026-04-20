# KidPager v0.9

First tagged public release. Both pagers run the same code; each one can send
and receive messages independently.

## What works

- Two-way messaging with retry indicator (`·` pending → `✓` delivered → `✗` timeout)
- BT Classic keyboard (M4) attached as HID, survives sleep/wake
- E-Ink refreshes off the main loop (background worker thread)
- Persistent history with periodic flush (2 s) to reduce SD card wear
- Four-tone buzzer: sent, incoming, ack, error
- LoRa group isolation via channel byte in the packet header
- Diagnostics script verifies every component end-to-end

## Hardware state

- Raspberry Pi Zero 2 W, Raspberry Pi OS Bookworm, Python 3.13
- Waveshare 2.13" E-Ink HAT V4
- **SX1262 LoRa module (Waveshare Core1262-HF)** — 868 MHz, +22 dBm max,
  TCXO on DIO3, external RF switch on DIO2
- M4 Bluetooth Classic keyboard
- LiPo 2000 mAh + CKCS boost

**Migration note:** v0.9 is the first release on SX1262. If you had a
pre-release build running on SX1276, solder one new wire (GPIO 23 →
SX1262 BUSY, phys pin 16) before deploying. Nothing else changes on the
hardware side. Over-the-air packet format is identical.

## Pair the M4 keyboard

```bash
ssh pi@kidpager.local
sudo ~/bt_pair.sh
```

Then on the other pager:

```bash
ssh pi@kidpager2.local
sudo ~/bt_pair.sh
```

Script runs pair / trust / connect in one piped `bluetoothctl` session —
the SSP agent must stay alive across the whole sequence, otherwise the
link key doesn't persist and the keyboard reconnects as "Bonded: no"
which prevents HID attachment.

## Verify

```bash
cd ~/kidpager && sudo python3 diagnose.py -y
```

Expected: ~33 `OK`, 0 `FAIL`, 0 `WARN` on both pagers. After that:

```powershell
.\deploy.ps1 -Restart
```

and start sending messages.

## Known limitations

- TX power is set to 22 dBm (Waveshare Core1262-HF high-power PA config).
  EU 868 MHz regulations vary by sub-band — drop `LORA_POWER` in `pins.py`
  to 14 or 17 if you need stricter compliance.
- M4 keyboard goes to sleep after ~30 s of idle. First keypress after sleep
  is lost during reconnect (~1-2 s). This is a peripheral-side design
  decision; no host-side wake protocol is available for consumer BT-Classic
  HID keyboards.
- Relative time only ("только что", "5 мин назад") — no NTP/GPS/DCF77 sync.
  Deliberate: real-time clock adds complexity without meaningful UX gain at
  this scope.
