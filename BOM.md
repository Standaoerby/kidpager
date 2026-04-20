# KidPager — Bill of Materials & Wiring

Everything you need to build **two** pagers (each pager is pointless by itself
— you need a pair to send messages between).

## Per-unit BOM

| # | Part | Spec / part number | Qty | ~Price | Notes |
|---|---|---|---|---|---|
| 1 | Raspberry Pi Zero 2 W | with 40-pin header pre-soldered | 1 | $15 | not Zero W — Zero 2 W has the quad-core SoC |
| 2 | microSD card | 8 GB+, Class 10 / A1 | 1 | $5 | flashed with Raspberry Pi OS **Bookworm Lite** |
| 3 | E-Ink HAT | **Waveshare 2.13" V4** (250×122, mono) | 1 | $20 | Waveshare SKU 19717; V4 specifically (V2/V3 have different driver init) |
| 4 | LoRa module | **Waveshare Core1262-HF** (SX1262, 868 MHz) | 1 | $12 | the -HF (+22 dBm HP PA). Core1262-LF is 433 MHz — wrong band |
| 5 | Antenna | 868 MHz, SMA-M or IPEX, λ/4 helical or stubby | 1 | $3 | must match LoRa module's connector |
| 6 | BT keyboard | "M4" compact Bluetooth Classic (not BLE) | 1 | $15 | any BT keyboard whose name contains `M4`/`KB`/`HID`/`KEYBOARD`/`BT-KEY` |
| 7 | Piezo buzzer | passive, 3-5 V, 12 mm | 1 | $1 | **passive** — active buzzers generate their own tone and ignore PWM |
| 8 | Series resistor | 100-220 Ω, 1/8 W | 1 | <$1 | limits buzzer current below GPIO's 16 mA |
| 9 | LiPo battery | 2000 mAh, 3.7 V, JST-PH 2.0 | 1 | $7 | built-in protection circuit strongly recommended |
| 10 | Boost converter | 3.7 V → 5 V, 1 A+ (CKCS or any TP4056 + boost combo) | 1 | $5 | with USB-C or micro-USB charge port if you want on-device charging |
| 11 | Hookup wire | 28-30 AWG silicone, stranded | ~30 cm | <$1 | thin keeps the HAT profile flat |
| 12 | Solder + flux | lead-free 0.5-0.8 mm, no-clean flux | — | — | — |
| 13 | Case (optional) | 3D-printed, cardstock, or generic project box | 1 | — | screen + keyboard need a carry strap for kid use |

**Total per unit: ~$85. Pair: ~$170.**

## Tools

- Soldering iron, 25-40 W, fine conical or chisel tip
- Tweezers (SMD-style, pointy)
- Wire stripper for 28-30 AWG
- Multimeter (continuity mode, at minimum)
- Heat-shrink tubing, 1-2 mm
- Tape (Kapton or electrical) to hold wires during soldering

## Pi Zero 2 W pinout (as soldered)

Physical pin numbers in parentheses. `E-` = E-Ink HAT (goes through the 40-pin
header as-is). `L-` = LoRa (you solder these). `B-` = buzzer.

```
                     +-------------------+
              3V3    |  1 ● ● 2  | 5V
         (I2C SDA)   |  3 ● ● 4  | 5V        <- power IN from boost
         (I2C SCL)   |  5 ● ● 6  | GND       <- power return
              GPIO4  |  7 ● ● 8  | GPIO14 (UART TX)
              GND    |  9 ● ● 10 | GPIO15 (UART RX)
     E-RST  GPIO17   | 11 ● ● 12 | GPIO18
     L-RST  GPIO27   | 13 ● ● 14 | GND
     L-DIO1 GPIO22   | 15 ● ● 16 | GPIO23   L-BUSY
              3V3    | 17 ● ● 18 | GPIO24   E-BUSY
     MOSI   GPIO10   | 19 ● ● 20 | GND
     MISO   GPIO9    | 21 ● ● 22 | GPIO25   E-DC
     SCLK   GPIO11   | 23 ● ● 24 | GPIO8    E-CS (CE0)
              GND    | 25 ● ● 26 | GPIO7    L-CS (CE1)
              GPIO0  | 27 ● ● 28 | GPIO1
              GPIO5  | 29 ● ● 30 | GND
              GPIO6  | 31 ● ● 32 | GPIO12
     B-OUT  GPIO13   | 33 ● ● 34 | GND       B-GND
              GPIO19 | 35 ● ● 36 | GPIO16
              GPIO26 | 37 ● ● 38 | GPIO20
              GND    | 39 ● ● 40 | GPIO21
                     +-------------------+
```

**Bold = you solder here.** Everything else is either used by the E-Ink HAT
(which plugs onto all 40 pins and occupies the SPI/3V3/GND pins it needs),
unused, or reserved.

## Wiring tables

### LoRa module (Waveshare Core1262-HF)

Core1262-HF has a 2×5 0.1" header. Solder from this header to the Pi pins
listed. All SPI lines (MOSI/MISO/SCK) are **shared** with the E-Ink HAT —
that's fine, the two chips are addressed by separate chip-selects (CE0 vs CE1).

| Core1262-HF pin | Pi phys pin | BCM GPIO | Function |
|---|---|---|---|
| VCC | **17** | 3V3 | power (use pin 17, not pin 1 — keeps pin 1 free for eink) |
| GND | **20** or **25** | GND | ground |
| MOSI | **19** | GPIO10 | SPI MOSI (shared with E-Ink) |
| MISO | **21** | GPIO9 | SPI MISO (E-Ink doesn't drive this back, LoRa does) |
| SCK | **23** | GPIO11 | SPI SCK (shared) |
| NSS | **26** | GPIO7 | **LoRa CS = CE1** (distinct from E-Ink's CE0) |
| RESET | **13** | GPIO27 | hardware reset |
| DIO1 | **15** | GPIO22 | IRQ line (SX1262 uses DIO1, not DIO0 like SX1276) |
| BUSY | **16** | GPIO23 | SX1262 handshake; **mandatory** — driver can't operate without it |
| ANT | SMA/IPEX | — | 868 MHz antenna |

**DO NOT power on the module without an antenna.** At +22 dBm into an open
output, the PA can be damaged in a few transmissions.

### Buzzer

| Buzzer pin | Connection |
|---|---|
| + | GPIO13 (phys pin **33**) — via 100-220 Ω series resistor |
| − | GND (phys pin **34**, directly next to 33) |

GPIO13 supports hardware PWM via pigpio. Don't drive the buzzer from a
software-PWM pin — it'll clock-glitch the E-Ink SPI during refresh.

### Power

LiPo → boost converter → Pi 5V rail.

| Wire | From | To |
|---|---|---|
| 1 | LiPo + (red) | boost IN+ |
| 2 | LiPo − (black) | boost IN− |
| 3 | boost OUT+ (5V) | Pi phys pin **2** or **4** (5V) |
| 4 | boost OUT− (GND) | Pi phys pin **6** or any GND |

If the boost has an integrated charger (TP4056 + boost combo board), the
charge input (USB-C / micro-USB) also becomes the charge-while-playing port.

## Assembly order

1. **Flash SD card** with Raspberry Pi OS Bookworm Lite. First-boot: enable
   SSH and Wi-Fi via `rpi-imager`'s customization screen. Set hostname to
   `kidpager` (and `kidpager2` on the second device).
2. **Verify the Pi boots** over SSH before touching the soldering iron. A
   dead Pi with wires already soldered is no fun to debug.
3. **Solder LoRa wires to the Pi header** (top side, so the HAT can still
   seat on the pins). Use 28-30 AWG silicone wire. Keep each wire <5 cm.
   Heat-shrink every solder joint to avoid shorts when the HAT lands on top.
4. **Solder buzzer + resistor.** Resistor goes in series with the buzzer `+`
   lead. One leg to pin 33, resistor body, buzzer `+`. Buzzer `−` to pin 34.
5. **Plug the Waveshare 2.13" HAT** onto the 40-pin header. It covers the
   top — your soldered wires stick out the back side.
6. **Connect the LoRa module.** Tape or hot-glue it to the underside of the
   Pi (the side without the HAT), antenna wire routed to a case exit.
7. **Wire the battery** through the boost converter to the Pi's 5V rail.
   Double-check polarity with a multimeter **before** plugging in — a
   reversed LiPo kills the Pi instantly and may set fire to the battery.
8. **Pair the M4 keyboard** over Bluetooth — see [README.md](README.md#bluetooth-keyboard-pairing).
9. **Run diagnostics:**
   ```bash
   cd ~/kidpager && sudo python3 diagnose.py -y
   ```
   All checks should pass. The most common failure is `LoRa BUSY LOW after
   reset` — 9/10 times it's a cold solder joint on GPIO 23 / phys pin 16.

## Power budget (rough)

`kidpager-power.service` applies these at boot: Wi-Fi blocked, CPU governor
set to `powersave`, ACT LED trigger off. See [power.py](power.py) and
[kidpager-power.sh](kidpager-power.sh). Toggle Wi-Fi on demand with **Alt+W**.

| State (with power-save on) | Current | Notes |
|---|---|---|
| Pi idle (Wi-Fi off, powersave, ACT LED off) | ~60-70 mA | versus ~110-130 mA defaults |
| Wi-Fi associated (Alt+W ON) | +40-60 mA | debug-only state |
| E-Ink full refresh | ~20 mA × 2 s | full refresh every 20 frames |
| E-Ink partial refresh | ~10 mA × 0.3 s | the normal case |
| LoRa RX listen | ~5 mA | continuous receive |
| LoRa TX @ +22 dBm | ~120 mA × ~200 ms | per packet |
| BT Classic HID link | ~10 mA | keyboard stays paired |
| Buzzer @ 50% duty | ~15 mA × 60-120 ms | per beep |

At a 2000 mAh LiPo with power-save active ≈ **18-20 hours** of mixed use
(idle + occasional messages). With Wi-Fi left on for debug, back to ~12 h.
Heavy TX load (dozens of messages/hour) adds 1-2 h of penalty.

## Known gotchas

- **SX1262 ≠ SX1276.** Do not try to reuse an SX1276 (RFM95/96) module —
  the driver in `lora.py` is command-based and uses the SX1262 opcodes and
  BUSY handshake. Wrong chip → GetStatus returns garbage.
- **Waveshare 2.13 V2 vs V3 vs V4** all use different init sequences.
  `display_eink.py` imports `epd2in13_V4` specifically. The deploy script
  downloads the V4 driver from Waveshare's repo.
- **BlueZ "Bonded: no"** is the single most common keyboard failure. Always
  pair via `bt_pair.sh` (single piped `bluetoothctl` session), never via
  separate `bluetoothctl pair` → `trust` → `connect` invocations.
- **pigpio on Bookworm** — the apt package was dropped. `deploy.ps1` builds
  it from source. Don't try `sudo apt install pigpio` — it won't work on
  Raspberry Pi OS Bookworm.
