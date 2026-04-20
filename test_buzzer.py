#!/usr/bin/env python3
"""Quick buzzer test — plays all 4 KidPager beep patterns.

Run:  sudo python3 test_buzzer.py
"""
import asyncio, sys
from buzzer import Buzzer


async def main():
    b = Buzzer()
    if not b.enabled:
        print("\nFAIL: buzzer not enabled.")
        print("  - Check pigpiod: systemctl is-active pigpiod")
        print("  - Check wiring: GPIO 13 (phys pin 33) -> 100-220R -> piezo (+)")
        print("  - Piezo (-) -> GND (phys pin 34)")
        return 1

    print("\n1/5 tone sweep — should hear 500/1000/2000/3000 Hz")
    for f in (500, 1000, 2000, 3000):
        print(f"    {f} Hz")
        await b.tone(f, 200)
        await asyncio.sleep(0.15)

    print("\n2/5 beep_incoming (two rising beeps — new message)")
    await b.beep_incoming()
    await asyncio.sleep(0.6)

    print("3/5 beep_sent (one short blip — message sent)")
    await b.beep_sent()
    await asyncio.sleep(0.6)

    print("4/5 beep_ack (soft confirm — delivery confirmed)")
    await b.beep_ack()
    await asyncio.sleep(0.6)

    print("\n5/5 beep_error (descending — send failed)")
    await b.beep_error()
    await asyncio.sleep(0.6)

    b.cleanup()
    print("\nDone. If you heard all of the above, buzzer is working.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
