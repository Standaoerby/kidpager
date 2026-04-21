"""Passive buzzer via hardware PWM on GPIO 13 (pigpio).

Uses the pigpiod daemon for true hardware PWM -- it does not generate
software interrupts, so it does not disturb SPI timing for the e-ink
display during partial refresh.

Silent mode: set via set_silent(True); every tone() call becomes a no-op.
The higher-level beep_* methods all go through tone(), so a single flag
gates every sound the pager can make.

Concurrency: tone() is guarded by an asyncio.Lock so that overlapping
create_task(buzzer.beep_*) calls from main.py serialise cleanly rather
than stomping each other's hardware_PWM state. Without this, a beep_sent
kicked off right before an incoming beep_incoming would clobber PWM
mid-tone and leave the piezo silent (or stuck on).
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
        self.silent = False          # flipped by set_silent() from main.py
        # Lazy-created on first tone() call to avoid depending on a running
        # event loop at construction time (main.py builds the Buzzer before
        # asyncio.run() takes over).
        self._tone_lock = None
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

    def set_silent(self, on):
        """Toggle silent mode. Any in-flight tone() will finish; subsequent
        tone() calls return immediately until set_silent(False)."""
        self.silent = bool(on)

    async def tone(self, freq_hz, duration_ms, duty_pct=50):
        """Play a single tone non-blockingly. duty_pct: 0-100.
        No-op if silent mode is on or the buzzer is disabled.
        Serialised: concurrent beep_* tasks queue rather than overlap."""
        if self.silent or not self.enabled or self.pi is None:
            return
        if self._tone_lock is None:
            self._tone_lock = asyncio.Lock()
        async with self._tone_lock:
            try:
                freq = max(50, int(freq_hz))
                duty = max(0, min(100, int(duty_pct))) * 10000  # 0-1000000
                self.pi.hardware_PWM(BUZZER, freq, duty)
                await asyncio.sleep(duration_ms / 1000.0)
                self.pi.hardware_PWM(BUZZER, 0, 0)  # stop
            except Exception as e:
                print(f"Buzzer tone error: {e}")

    async def beep_incoming(self):
        """Two short rising beeps -- incoming message (awake)."""
        await self.tone(1800, 60)
        await asyncio.sleep(0.04)
        await self.tone(2400, 60)

    async def beep_sent(self):
        """One short blip -- message sent."""
        await self.tone(2200, 30)

    async def beep_ack(self):
        """One soft confirmation -- delivery confirmed."""
        await self.tone(3000, 25)

    async def beep_error(self):
        """Low descending -- send failed / timeout."""
        await self.tone(800, 80)
        await asyncio.sleep(0.03)
        await self.tone(500, 120)

    async def beep_alarm(self):
        """Three short equal beeps -- wake-from-sleep alarm for a new incoming
        message while the pager is in screen-saver mode. Distinct from the
        two-beep incoming pattern (so the user can tell "new while asleep"
        from "new while awake") but without the rising-siren shriek the
        earlier version had. Respects silent mode (each tone() re-checks)."""
        for _ in range(3):
            await self.tone(2000, 80)
            await asyncio.sleep(0.12)

    def cleanup(self):
        if self.pi is not None:
            try:
                self.pi.hardware_PWM(BUZZER, 0, 0)
                self.pi.stop()
            except Exception:
                pass
            self.pi = None
