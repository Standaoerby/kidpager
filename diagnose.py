#!/usr/bin/env python3
"""KidPager full diagnostics.

Run:  sudo python3 ~/kidpager/diagnose.py [flags]

Flags:
  -y            auto-yes: stop kidpager service for hardware tests
  --skip-hw     software checks only; don't touch SPI/E-Ink/buzzer
  --quick       no E-Ink draw, no buzzer tone (pin checks + registers only)
"""
import sys, os, subprocess, time, json, argparse
from pathlib import Path

# ANSI colours — suppressed if stdout is not a TTY (journalctl pipes, etc.)
if sys.stdout.isatty():
    G, R, Y, C, M, D = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[35m", "\033[0m"
else:
    G = R = Y = C = M = D = ""

PASS = f"{G}OK  {D}"
FAIL = f"{R}FAIL{D}"
WARN = f"{Y}WARN{D}"
SKIP = f"    "

results = []  # (label, status, detail)


def section(name):
    print(f"\n{C}[{name}]{D}")


def check(label, status, detail=""):
    icon = {"pass": PASS, "fail": FAIL, "warn": WARN, "skip": SKIP}[status]
    line = f"  {icon}  {label}"
    if detail:
        line += f"  {D}— {detail}"
    print(line)
    results.append((label, status, detail))


def sh(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)


# ---------- Software checks ----------

def check_system():
    section("System")
    rc, _ = sh("ls /dev/spidev0.0")
    check("SPI bus /dev/spidev0.0", "pass" if rc == 0 else "fail")

    rc, _ = sh("systemctl is-active bluetooth")
    check("bluetooth.service active", "pass" if rc == 0 else "fail")

    rc, _ = sh("systemctl is-active pigpiod")
    check("pigpiod.service active", "pass" if rc == 0 else "fail")

    rc, _ = sh("systemctl is-enabled kidpager")
    check("kidpager.service enabled", "pass" if rc == 0 else "fail")

    rc, _ = sh("systemctl is-active kidpager")
    check("kidpager.service currently running", "pass" if rc == 0 else "warn",
          "" if rc == 0 else "not running (OK if you stopped it manually)")

    fonts = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists()
    check("DejaVu fonts installed", "pass" if fonts else "fail")


def check_modules():
    section("Python modules")
    for mod in ("RPi.GPIO", "spidev", "PIL", "pigpio"):
        try:
            __import__(mod)
            check(mod, "pass")
        except Exception as e:
            check(mod, "fail", str(e)[:60])

    try:
        import pigpio
        pi = pigpio.pi()
        if pi.connected:
            pi.stop()
            check("pigpio daemon connection", "pass")
        else:
            check("pigpio daemon connection", "fail", "socket unreachable")
    except Exception as e:
        check("pigpio daemon connection", "fail", str(e)[:60])


def check_files():
    section("Files")
    code = Path("/home/pi/kidpager")
    for f in ("pins.py", "lora.py", "display_eink.py", "keyboard.py",
              "buzzer.py", "config.py", "ui.py", "main.py"):
        p = code / f
        check(f"~/kidpager/{f}", "pass" if p.exists() else "fail")

    waveshare = Path("/home/pi/waveshare_epd/epd2in13_V4.py")
    check("~/waveshare_epd/epd2in13_V4.py", "pass" if waveshare.exists() else "fail")

    # Service runs as User=root, so persistent state lives under /root
    for base in ("/root/.kidpager", "/home/pi/.kidpager"):
        cfg = Path(base) / "config.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text())
                check(f"{base}/config.json", "pass",
                      f"name={data.get('name')!r} channel={data.get('channel')}")
            except Exception as e:
                check(f"{base}/config.json", "fail", f"invalid JSON: {e}")

    for base in ("/root/.kidpager", "/home/pi/.kidpager"):
        hist = Path(base) / "history.json"
        if hist.exists():
            try:
                data = json.loads(hist.read_text())
                check(f"{base}/history.json", "pass", f"{len(data)} messages")
            except Exception as e:
                check(f"{base}/history.json", "fail", f"invalid JSON: {e}")


def check_bluetooth():
    section("Bluetooth keyboard")
    rc, out = sh("bluetoothctl devices")
    if rc != 0:
        check("bluetoothctl devices", "fail", out[:60])
        return

    hints = ("M4", "KEYBOARD", "KB", "BT-KEY", "HID")
    keyboards = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 3 and parts[0] == "Device":
            up = parts[2].upper()
            if any(k in up for k in hints):
                keyboards.append((parts[1], parts[2]))

    if not keyboards:
        check("Paired keyboard", "fail", "no keyboard in BT database — run bt_pair.sh")
        return

    mac, name = keyboards[0]
    check("Paired keyboard", "pass", f"{name} ({mac})")

    _, info = sh(f"bluetoothctl info {mac}")
    connected = f"Connected: yes" in info
    for prop in ("Paired", "Bonded", "Trusted", "Connected"):
        yes = f"{prop}: yes" in info
        if prop == "Bonded" and not yes:
            check(f"  {prop}", "fail", "HID won't attach without bond — re-run bt_pair.sh")
        elif prop == "Trusted" and not yes:
            check(f"  {prop}", "fail", f"run: bluetoothctl trust {mac}")
        elif prop == "Connected" and not yes:
            check(f"  {prop}", "warn", "keyboard offline (asleep or powered off)")
        else:
            check(f"  {prop}", "pass" if yes else "fail")

    # Input event device — only relevant if keyboard is actually connected
    _, evs = sh("ls /dev/input/event*")
    found = None
    for ev in evs.split():
        np = f"/sys/class/input/{os.path.basename(ev)}/device/name"
        try:
            dev_name = Path(np).read_text().strip().upper()
        except Exception:
            continue
        if any(k in dev_name for k in hints):
            found = (ev, dev_name)
            break
    if found:
        check("Input event device", "pass", f"{found[0]} = {found[1]}")
    elif connected:
        check("Input event device", "fail", "connected but no HID attachment")
    else:
        check("Input event device", "warn", "no event device (OK if keyboard offline)")


# ---------- Hardware checks (need service stopped) ----------

def check_lora(quick=False):
    section("LoRa radio (SX1262)")
    sys.path.insert(0, "/home/pi/kidpager")
    try:
        import spidev
        import RPi.GPIO as GPIO
        from pins import (LORA_RST, LORA_BUSY, SPI_BUS,
                          LORA_FREQ, LORA_SF, LORA_BW, LORA_POWER)

        GPIO.setwarnings(False); GPIO.setmode(GPIO.BCM)
        GPIO.setup(LORA_RST,  GPIO.OUT)
        GPIO.setup(LORA_BUSY, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # Reset pulse
        GPIO.output(LORA_RST, GPIO.LOW);  time.sleep(0.001)
        GPIO.output(LORA_RST, GPIO.HIGH); time.sleep(0.010)

        # Wait BUSY LOW — this is the critical SX1262 handshake
        t0 = time.time()
        busy_ok = True
        while GPIO.input(LORA_BUSY):
            if time.time() - t0 > 0.1:
                busy_ok = False; break
            time.sleep(0.0001)

        if not busy_ok:
            GPIO.cleanup()
            check("BUSY LOW after reset", "fail",
                  "stuck HIGH — check LORA_BUSY wiring (GPIO 23, phys pin 16)")
            return
        check("BUSY LOW after reset", "pass")

        spi = spidev.SpiDev(); spi.open(SPI_BUS, 1)
        spi.max_speed_hz = 1_000_000; spi.mode = 0

        # GetStatus (0xC0) — chipmode bits [6:4] should be 0x2 (STDBY_RC) after reset
        r = spi.xfer2([0xC0, 0x00])
        status = r[1]
        chipmode = (status >> 4) & 0x07
        spi.close()
        GPIO.cleanup()

        modes = {0x02: "STDBY_RC", 0x03: "STDBY_XOSC"}
        if chipmode in modes:
            check("SX1262 GetStatus", "pass",
                  f"status=0x{status:02X} chipmode={modes[chipmode]}")
        else:
            check("SX1262 GetStatus", "fail",
                  f"status=0x{status:02X} chipmode=0x{chipmode} (expected STDBY)")
            return
    except Exception as e:
        check("SX1262 GetStatus", "fail", str(e)[:60])
        return

    if quick:
        return

    try:
        from config import Config
        from lora import LoRaRadio
        cfg = Config("/root/.kidpager/config.json")
        cfg.load()
        radio = LoRaRadio(cfg)
        ok = radio.init()
        if ok:
            check("Radio init + RX mode", "pass",
                  f"{LORA_FREQ}MHz SF{LORA_SF} BW{LORA_BW}kHz "
                  f"+{LORA_POWER}dBm ch={cfg.channel}")
            radio.cleanup()
        else:
            check("Radio init + RX mode", "fail")
    except Exception as e:
        check("Radio init + RX mode", "fail", str(e)[:60])


def check_eink(quick=False):
    section("E-Ink display (Waveshare 2.13 V4)")
    sys.path.insert(0, "/home/pi/kidpager")
    try:
        import RPi.GPIO as GPIO
        from pins import EINK_BUSY
        GPIO.setwarnings(False); GPIO.setmode(GPIO.BCM)
        GPIO.setup(EINK_BUSY, GPIO.IN)
        busy = GPIO.input(EINK_BUSY)
        GPIO.cleanup()
        if busy == 0:
            check("BUSY pin idle (LOW)", "pass")
        else:
            check("BUSY pin idle (LOW)", "warn",
                  "HIGH — display may not be in deep sleep from last run; hw_reset will fix")
    except Exception as e:
        check("BUSY pin idle (LOW)", "fail", str(e)[:60])

    if quick:
        return

    try:
        # display_eink does a hw_reset on import — expected
        from display_eink import EInkDisplay
        from PIL import Image, ImageDraw, ImageFont
        d = EInkDisplay()
        img = Image.new("1", (250, 122), 255)
        draw = ImageDraw.Draw(img)
        try:
            f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        except Exception:
            f = ImageFont.load_default()
        draw.rectangle([0, 0, 250, 20], fill=0)
        draw.text((6, 2), "DIAGNOSTICS", font=f, fill=255)
        draw.text((10, 40), f"time: {time.strftime('%H:%M:%S')}", fill=0)
        draw.text((10, 70), "E-Ink draw OK", fill=0)
        d._submit(img)
        time.sleep(3)   # let background worker render
        d.cleanup()     # stop worker thread first (joins with timeout)
        d.sleep()       # then put display in deep sleep (BUSY drops LOW)
        check("Draw test (worker thread)", "pass", "check the screen — should show DIAGNOSTICS")
    except Exception as e:
        check("Draw test (worker thread)", "fail", str(e)[:80])


def check_buzzer(quick=False):
    section("Buzzer (GPIO 13, pigpio hardware PWM)")
    if quick:
        check("Tone test", "skip", "--quick mode")
        return
    sys.path.insert(0, "/home/pi/kidpager")
    try:
        import asyncio
        from buzzer import Buzzer
        b = Buzzer()
        if not b.enabled:
            check("Buzzer init", "fail", "pigpiod not reachable")
            return
        check("Buzzer init", "pass")

        async def sweep():
            await b.tone(1500, 80)
            await asyncio.sleep(0.05)
            await b.tone(2500, 80)

        asyncio.run(sweep())
        b.cleanup()
        check("Tone test", "pass", "heard two short beeps?")
    except Exception as e:
        check("Tone test", "fail", str(e)[:80])


# ---------- Driver ----------

def print_summary():
    pass_n = sum(1 for r in results if r[1] == "pass")
    fail_n = sum(1 for r in results if r[1] == "fail")
    warn_n = sum(1 for r in results if r[1] == "warn")
    skip_n = sum(1 for r in results if r[1] == "skip")

    print(f"\n{C}=== Summary ==={D}")
    print(f"  {G}pass {pass_n}{D}   {R}fail {fail_n}{D}   {Y}warn {warn_n}{D}   skip {skip_n}")

    if fail_n:
        print(f"\n{R}{fail_n} failure(s) — see above.{D}")
        sys.exit(1)
    if warn_n:
        print(f"\n{Y}{warn_n} warning(s) — review but probably OK.{D}")
    print(f"\n{G}All critical checks passed. KidPager is healthy.{D}")
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description="KidPager full diagnostics")
    ap.add_argument("-y", action="store_true",
                    help="auto-yes: stop kidpager service for HW tests without prompting")
    ap.add_argument("--skip-hw", action="store_true",
                    help="skip hardware tests (software/files/BT only)")
    ap.add_argument("--quick", action="store_true",
                    help="HW checks but no E-Ink draw and no buzzer tone")
    args = ap.parse_args()

    print(f"{C}=== KidPager Diagnostics ==={D}")
    print(f"{D}Host: {os.uname().nodename}   Python: {sys.version.split()[0]}{D}")

    check_system()
    check_modules()
    check_files()
    check_bluetooth()

    if args.skip_hw:
        section("Hardware")
        check("LoRa + E-Ink + Buzzer", "skip", "--skip-hw")
        print_summary()

    # HW tests — need service stopped (it holds SPI, E-Ink, keyboard)
    rc, _ = sh("systemctl is-active kidpager")
    running = (rc == 0)

    if running and not args.y:
        print()
        try:
            ans = input(f"{Y}kidpager service is running. Stop it for hardware tests? [Y/n]: {D}").strip().lower()
        except EOFError:
            ans = "n"
        if ans == "n":
            section("Hardware")
            check("LoRa + E-Ink + Buzzer", "skip", "user declined to stop service")
            print_summary()

    if running:
        sh("systemctl stop kidpager", timeout=10)
        time.sleep(1.5)

    try:
        check_lora(args.quick)
        check_eink(args.quick)
        check_buzzer(args.quick)
    finally:
        if running:
            sh("systemctl start kidpager", timeout=10)
            print(f"\n  {C}(kidpager service restarted){D}")

    print_summary()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Y}interrupted{D}")
        sys.exit(130)
