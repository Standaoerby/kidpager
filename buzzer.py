"""Passive buzzer via hardware PWM on GPIO 13 (pigpio).

Uses the pigpiod daemon for true hardware PWM — it does not generate
software interrupts, so it does not disturb SPI timing for the e-ink
display during partial refresh.

Setup is handled automatically by deploy.ps1 step [2/7]. On Raspberry Pi
OS Bookworm the pigpio apt package is no longer available (it does not
support the RP1 chip on Pi 5), so deploy.ps1 builds joan2937/pigpio from
source, installs the Python module via pip, drops in a systemd unit, and
refreshes the dynamic linker cache. See deploy.ps1 for the full recipe.
"""
import asyncio, time

try:
    import pigpio
    HAS_PIGPIO = True
except Exception as e:
    print(f"pigpio not available: {e}")
    HAS_PIGPIO = False

try:
    from pins import BUZZER
except Exception:
    BUZZER = 13


class Buzzer:
    def __init__(self):
        self.pi = None
        self.enabled = False
        if not HAS_PIGPIO:
            return
        try:
            self.pi = pigpio.pi()
            if not self.pi.connected:
                print("pigpiod not running (try: sudo systemctl start pigpiod)")
                self.pi = None
                return
            # Stop any leftover PWM on this pin
            self.pi.hardware_PWM(BUZZER, 0, 0)
            self.enabled = True
            print(f"Buzzer OK on GPIO {BUZZER} (hardware PWM via pigpio)")
        except Exception as e:
            print(f"Buzzer init failed: {e}")
            self.pi = None

    async def tone(self, freq_hz, duration_ms, duty_pct=50):
        """Play a single tone non-blockingly. duty_pct: 0-100."""
        if not self.enabled or self.pi is None:
            return
        try:
            freq = max(50, int(freq_hz))
            duty = max(0, min(100, int(duty_pct))) * 10000  # 0-1000000
            self.pi.hardware_PWM(BUZZER, freq, duty)
            await asyncio.sleep(duration_ms / 1000.0)
            self.pi.hardware_PWM(BUZZER, 0, 0)  # stop
        except Exception as e:
            print(f"Buzzer tone error: {e}")

    async def beep_incoming(self):
        """Two short rising beeps — incoming message."""
        await self.tone(1800, 60)
        await asyncio.sleep(0.04)
        await self.tone(2400, 60)

    async def beep_sent(self):
        """One short blip — message sent."""
        await self.tone(2200, 30)

    async def beep_ack(self):
        """One soft confirmation — delivery confirmed."""
        await self.tone(3000, 25)

    async def beep_error(self):
        """Low descending — send failed / timeout."""
        await self.tone(800, 80)
        await asyncio.sleep(0.03)
        await self.tone(500, 120)

    def cleanup(self):
        if self.pi is not None:
            try:
                self.pi.hardware_PWM(BUZZER, 0, 0)
                self.pi.stop()
            except Exception:
                pass
            self.pi = None
