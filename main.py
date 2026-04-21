#!/usr/bin/env python3
"""KidPager - LoRa messenger for kids.

E-Ink refresh strategy
----------------------
E-Ink hardware refreshes are slow (~300 ms partial, ~2 s full) and wear out
the panel if done too often. We balance responsiveness against cost with a
two-pronged debounce:

  TYPING_SETTLE     After the last keypress, wait this long before redrawing.
                    Catches the common case: user types "hello", pauses, we
                    redraw once instead of six times.
  TYPING_MAX_STALE  During *continuous* typing the settle timer never fires
                    (last_key keeps moving). This is a cap: we still redraw
                    at least every TYPING_MAX_STALE seconds so the user sees
                    what they're typing even when they don't pause.

Non-typing events (incoming message, ack received, profile navigation, send)
bypass debounce and trigger an immediate full_redraw() -- they're rare and
the user wants to see the result now.
"""
import asyncio, os, sys, time
from config import Config
from keyboard import KeyboardReader
from lora import LoRaRadio
from ui import PagerUI, STATUS_FAIL
from buzzer import Buzzer
import power

CONFIG = os.path.expanduser("~/.kidpager/config.json")
FLUSH_INTERVAL = 2.0       # seconds between history flushes to SD card
TYPING_SETTLE = 0.3        # seconds idle before E-Ink redraws after a keypress
TYPING_MAX_STALE = 1.5     # seconds max between redraws during continuous typing
KB_CHECK_INTERVAL = 5      # seconds between keyboard liveness checks
ACK_CHECK_INTERVAL = 2     # seconds between retry/timeout scans


async def main():
    config = Config(CONFIG)
    config.load()
    print(f"=== KidPager ===")
    print(f"Name: {config.name}\n")

    kb = KeyboardReader()
    if not kb.find_m4():
        print("WARNING: M4 keyboard not found!")

    lora = LoRaRadio(config)
    lora_ok = lora.init()

    buzzer = Buzzer()

    ui = PagerUI(config, lora if lora_ok else None)
    # Reflect real Wi-Fi state on startup (kidpager-power.service soft-blocks it
    # at boot, but /root/.kidpager is persistent across reboots so state can drift).
    ui.set_wifi(power.wifi_is_enabled())
    print("\nReady! Enter=send Esc=menu Alt+W=wifi\n")
    ui.full_redraw()

    last_key = 0
    eink_pending = False
    last_eink_draw = time.time()   # timestamp of most recent E-Ink submit
    last_kb_check = time.time()
    last_ack_check = time.time()
    last_flush = time.time()

    try:
        while True:
            got_key = False
            action = None

            # Drain keyboard queue. Cap at 20 keys per tick so a stuck key
            # can't starve the rest of the loop.
            for _ in range(20):
                key = kb.read_key_sync()
                if key is None: break
                got_key = True
                last_key = time.time()
                a = ui.handle_key(key)
                if a == "send": action = "send"
                elif a == "toggle_wifi": action = "toggle_wifi"

            if action == "send":
                msg = ui.get_message()
                if msg:
                    msg_id = None
                    if lora_ok: msg_id = lora.send(config.name, msg)
                    ui.add_message(config.name, msg, outgoing=True, msg_id=msg_id)
                    asyncio.create_task(buzzer.beep_sent())
                ui.full_redraw()
                eink_pending = False
                last_eink_draw = time.time()
            elif action == "toggle_wifi":
                # Distinct audible feedback: "ack" when turning ON (clear success),
                # "sent" (shorter/lower) when turning OFF.
                new_state = power.wifi_toggle()
                ui.set_wifi(new_state)
                asyncio.create_task(buzzer.beep_ack() if new_state else buzzer.beep_sent())
                ui.full_redraw()
                eink_pending = False
                last_eink_draw = time.time()
            elif got_key:
                # Lightweight: update the SSH/terminal input line immediately
                # (cheap) and flag the E-Ink for deferred redraw (expensive).
                ui.term_input_line()
                eink_pending = True

            # E-Ink refresh: settle after idle OR cap staleness during typing.
            now = time.time()
            if eink_pending:
                paused = (now - last_key) > TYPING_SETTLE
                stale  = (now - last_eink_draw) > TYPING_MAX_STALE
                if paused or stale:
                    ui.eink_refresh()
                    eink_pending = False
                    last_eink_draw = now

            # Keyboard liveness: periodically reconnect if the BT link died.
            if now - last_kb_check > KB_CHECK_INTERVAL:
                last_kb_check = now
                if not kb.is_alive():
                    print("M4 lost, searching...")
                    kb.reconnect()
                    if kb.fd is not None:
                        print("M4 reconnected!")

            # Inbound radio: always ack, dedupe on msg_id.
            if lora_ok:
                result = lora.receive()
                if result:
                    rtype, data = result
                    if rtype == "msg":
                        sender, text, msg_id = data
                        # Ack even duplicates -- lost-ack recovery depends on it.
                        lora.send_ack(msg_id)
                        is_new = ui.add_message(sender, text, outgoing=False, msg_id=msg_id)
                        if is_new:
                            asyncio.create_task(buzzer.beep_incoming())
                            ui.full_redraw()
                            last_eink_draw = time.time()
                    elif rtype == "ack":
                        if ui.mark_delivered(data):
                            asyncio.create_task(buzzer.beep_ack())
                            ui.full_redraw()
                            last_eink_draw = time.time()

            # Retry / timeout scan. Retransmits silently increment m.retries;
            # the failure beep only fires when a message actually crosses into
            # STATUS_FAIL (retries exhausted).
            if now - last_ack_check > ACK_CHECK_INTERVAL:
                last_ack_check = now
                before = sum(1 for m in ui.messages if m.status == STATUS_FAIL)
                if ui.check_timeouts():
                    after = sum(1 for m in ui.messages if m.status == STATUS_FAIL)
                    if after > before:
                        asyncio.create_task(buzzer.beep_error())
                    ui.full_redraw()
                    last_eink_draw = time.time()

            if now - last_flush > FLUSH_INTERVAL:
                last_flush = now
                ui.flush_history()

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Any other exception still lands in finally for cleanup,
        # but we log it so journalctl captures the root cause.
        print(f"FATAL in main loop: {e!r}")
        raise
    finally:
        # Cleanup runs on Ctrl+C AND on any unexpected exception -- without
        # this, a mid-loop crash would leave history unflushed and E-Ink BUSY
        # stuck HIGH, blocking the next boot at LoRa init.
        try: config.save()
        except Exception as e: print(f"config.save error: {e}")
        try: ui.flush_history()
        except Exception as e: print(f"flush_history error: {e}")
        if lora_ok:
            try: lora.cleanup()
            except Exception as e: print(f"lora.cleanup error: {e}")
        if ui.eink:
            try:
                ui.eink.cleanup()   # stop worker thread
                ui.eink.sleep()     # deep-sleep display so BUSY drops LOW for next start
            except Exception as e: print(f"eink cleanup error: {e}")
        try: buzzer.cleanup()
        except Exception as e: print(f"buzzer.cleanup error: {e}")
        try: kb.close()
        except Exception as e: print(f"kb.close error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
