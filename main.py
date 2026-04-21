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

Sleep / screen-saver
--------------------
After IDLE_TIMEOUT seconds without *any* activity (key or incoming message),
the UI enters the "sleep" state and shows a minimal screen saver. Any key
wakes it back into chat. An incoming message also wakes it, but with a
louder rising alarm (beep_alarm instead of beep_incoming). Silent mode
mutes all tones regardless of state.
"""
import asyncio, os, sys, time
from config import Config
from keyboard import KeyboardReader
from lora import LoRaRadio
from ui import PagerUI, STATUS_FAIL
from buzzer import Buzzer
import power

CONFIG = os.path.expanduser("~/.kidpager/config.json")
FLUSH_INTERVAL = 2.0
TYPING_SETTLE = 0.3
TYPING_MAX_STALE = 1.5
KB_CHECK_INTERVAL = 5
ACK_CHECK_INTERVAL = 2
IDLE_TIMEOUT = 300   # 5 minutes -- idle time before entering sleep screen


async def main():
    config = Config(CONFIG)
    config.load()
    print(f"=== KidPager ===")
    print(f"Name: {config.name}  Silent: {config.silent}\n")

    kb = KeyboardReader()
    if not kb.find_m4():
        print("WARNING: M4 keyboard not found!")

    lora = LoRaRadio(config)
    lora_ok = lora.init()

    buzzer = Buzzer()
    # Apply persisted silent preference before the first beep can fire.
    buzzer.set_silent(config.silent)

    ui = PagerUI(config, lora if lora_ok else None)
    ui.set_wifi(power.wifi_is_enabled())
    print("\nReady! Enter=send Esc=menu Alt+W=wifi\n")
    ui.full_redraw()

    last_key = 0
    eink_pending = False
    last_eink_draw = time.time()
    last_kb_check = time.time()
    last_ack_check = time.time()
    last_flush = time.time()
    # Any user input OR incoming message resets this. When now - last_activity
    # exceeds IDLE_TIMEOUT in chat state, we drop into the screen saver.
    last_activity = time.time()

    try:
        while True:
            got_key = False
            action = None

            for _ in range(20):
                key = kb.read_key_sync()
                if key is None: break
                got_key = True
                last_key = time.time()
                last_activity = last_key     # any key = activity
                a = ui.handle_key(key)
                if   a == "send":            action = "send"
                elif a == "toggle_wifi":     action = "toggle_wifi"
                elif a == "silent_changed":  action = "silent_changed"
                elif a == "wake":            action = "wake"

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
                new_state = power.wifi_toggle()
                ui.set_wifi(new_state)
                asyncio.create_task(buzzer.beep_ack() if new_state else buzzer.beep_sent())
                ui.full_redraw()
                eink_pending = False
                last_eink_draw = time.time()
            elif action == "silent_changed":
                # ui already persisted + redrew the profile menu. We just need
                # to mirror the new setting into the buzzer so the next beep
                # respects it.
                buzzer.set_silent(config.silent)
                # No beep here -- toggling silent shouldn't make a sound either
                # way (it would be confusing when turning silent ON).
                eink_pending = False
                last_eink_draw = time.time()
            elif action == "wake":
                # User hit a key while asleep; ui already transitioned to chat
                # and redrew. Just record that we're active again and skip the
                # typing-debounce path.
                eink_pending = False
                last_eink_draw = time.time()
            elif got_key:
                ui.term_input_line()
                eink_pending = True

            now = time.time()
            if eink_pending:
                paused = (now - last_key) > TYPING_SETTLE
                stale  = (now - last_eink_draw) > TYPING_MAX_STALE
                if paused or stale:
                    ui.eink_refresh()
                    eink_pending = False
                    last_eink_draw = now

            if now - last_kb_check > KB_CHECK_INTERVAL:
                last_kb_check = now
                if not kb.is_alive():
                    print("M4 lost, searching...")
                    kb.reconnect()
                    if kb.fd is not None:
                        print("M4 reconnected!")

            if lora_ok:
                result = lora.receive()
                if result:
                    rtype, data = result
                    if rtype == "msg":
                        sender, text, msg_id = data
                        # Always ack, even duplicates -- lost-ack recovery needs it.
                        lora.send_ack(msg_id)
                        is_new = ui.add_message(sender, text, outgoing=False, msg_id=msg_id)
                        if is_new:
                            last_activity = time.time()
                            if ui.state == "sleep":
                                # Wake + louder rising alarm. Buzzer itself
                                # handles silent-mode muting.
                                ui.wake()
                                asyncio.create_task(buzzer.beep_alarm())
                            else:
                                asyncio.create_task(buzzer.beep_incoming())
                            ui.full_redraw()
                            last_eink_draw = time.time()
                    elif rtype == "ack":
                        if ui.mark_delivered(data):
                            asyncio.create_task(buzzer.beep_ack())
                            # Skip redraw if asleep -- ack isn't worth waking
                            # the screen for, and we don't want the sleep
                            # page repainted with partial refresh.
                            if ui.state != "sleep":
                                ui.full_redraw()
                                last_eink_draw = time.time()

            if now - last_ack_check > ACK_CHECK_INTERVAL:
                last_ack_check = now
                before = sum(1 for m in ui.messages if m.status == STATUS_FAIL)
                if ui.check_timeouts():
                    after = sum(1 for m in ui.messages if m.status == STATUS_FAIL)
                    if after > before:
                        asyncio.create_task(buzzer.beep_error())
                    # Same idea as above: if asleep, keep the sleep screen.
                    if ui.state != "sleep":
                        ui.full_redraw()
                        last_eink_draw = time.time()

            if now - last_flush > FLUSH_INTERVAL:
                last_flush = now
                ui.flush_history()

            # Auto-sleep: only from chat state, so we don't clobber a user who
            # left the profile menu open for 5 minutes.
            if (ui.state == "chat"
                    and (now - last_activity) > IDLE_TIMEOUT):
                ui.enter_sleep()
                last_eink_draw = time.time()
                # Reset so we don't re-enter sleep every tick.
                last_activity = now

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"FATAL in main loop: {e!r}")
        raise
    finally:
        try: config.save()
        except Exception as e: print(f"config.save error: {e}")
        try: ui.flush_history()
        except Exception as e: print(f"flush_history error: {e}")
        if lora_ok:
            try: lora.cleanup()
            except Exception as e: print(f"lora.cleanup error: {e}")
        if ui.eink:
            try:
                ui.eink.cleanup()
                ui.eink.sleep()
            except Exception as e: print(f"eink cleanup error: {e}")
        try: buzzer.cleanup()
        except Exception as e: print(f"buzzer.cleanup error: {e}")
        try: kb.close()
        except Exception as e: print(f"kb.close error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
