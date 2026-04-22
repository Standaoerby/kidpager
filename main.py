#!/usr/bin/env python3
"""KidPager - LoRa messenger for kids.

Event loop
----------
The main loop is a standard asyncio ``while True`` with a short sleep.
Keyboard events are produced by a background thread in ``keyboard.py``
into a ``deque`` -- we drain as many as are ready per tick (up to
``KB_DRAIN_MAX``) with no syscall cost when the queue is empty. This
decouples input capture from E-Ink refresh: the worker thread keeps
filling the queue even during a ~2 s full panel refresh, and on the
next tick the whole burst is processed in one batch.

E-Ink refresh strategy
----------------------
E-Ink hardware refreshes are slow (~300 ms partial, ~2 s full) and wear
out the panel. Two-pronged debounce while typing:

  TYPING_SETTLE     after the last keypress, wait this long before
                    redrawing. Catches the common case: user types
                    "hello", pauses, we redraw once.
  TYPING_MAX_STALE  during CONTINUOUS typing the settle timer never
                    fires; this is the cap that guarantees the user
                    sees their input at least every N seconds.

Non-typing events (incoming message, ack, profile navigation, send,
wifi toggle, silent toggle, wake) bypass debounce and redraw now.

Sleep / screen-saver
--------------------
After IDLE_TIMEOUT seconds with no key and no incoming message, the UI
enters sleep. Any key wakes; incoming message also wakes and plays
``beep_alarm`` instead of ``beep_incoming``. Silent mode mutes all.

Auto-sleep is suppressed while Wi-Fi is on -- Wi-Fi on = live SSH
session = don't ambush the user with a full-refresh flash mid-debug.
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
IDLE_TIMEOUT = 300   # 5 minutes

# Drain up to this many queued keys per tick. The background reader
# can queue a full 256-deep buffer while we're off doing E-Ink, so on
# the next tick we might need to process a big batch. Higher than 20
# (v0.13) because a paste or fast burst easily exceeds 20 and we
# don't want to carry leftover events across multiple ticks.
KB_DRAIN_MAX = 128


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
    buzzer.set_silent(config.silent)

    ui = PagerUI(config, lora if lora_ok else None)
    ui.set_wifi(power.wifi_is_enabled())
    print("\nReady! Enter=send Esc=menu Alt+W=wifi\n")
    ui.full_redraw()

    last_key = 0.0
    eink_pending = False
    last_eink_draw = time.time()
    last_kb_check = time.time()
    last_ack_check = time.time()
    last_flush = time.time()
    last_activity = time.time()

    # Dropped-keys monitoring. If the background reader ever overflows
    # its deque we print a warning so journalctl shows the problem.
    last_dropped = 0

    try:
        while True:
            got_typing = False  # a printable character was added in chat state
            action = None

            # Drain the keyboard queue. The reader thread keeps
            # producing during our other work; this loop only runs
            # briefly each tick.
            drained = 0
            while drained < KB_DRAIN_MAX:
                key = kb.poll()
                if key is None:
                    break
                drained += 1
                a = ui.handle_key(key)
                if a == "send":             action = "send"
                elif a == "toggle_wifi":    action = "toggle_wifi"
                elif a == "silent_changed": action = "silent_changed"
                elif a == "wake":           action = "wake"
                elif a == "typing":         got_typing = True
                # Anything else (None) = non-typing action whose
                # handler already did its own redraw (menu nav,
                # name/channel edit). Don't arm debounce for those --
                # that was the v0.14 bug where name editing got two
                # E-Ink refreshes per keystroke.

            # Hoist time.time() out of the per-key loop. All keys in a
            # single drain burst belong to the same ~10 ms tick so
            # stamping them with one timestamp is accurate enough for
            # the TYPING_SETTLE / IDLE_TIMEOUT heuristics and avoids
            # up to KB_DRAIN_MAX=128 syscalls per tick.
            if drained > 0:
                last_key = time.time()
                last_activity = last_key

            # Dropped-key diagnostics
            dropped_now = kb.dropped()
            if dropped_now != last_dropped:
                delta = dropped_now - last_dropped
                last_dropped = dropped_now
                print(f"WARNING: kb dropped {delta} events "
                      f"(total {dropped_now}) -- main loop starved")

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
                buzzer.set_silent(config.silent)
                eink_pending = False
                last_eink_draw = time.time()
            elif action == "wake":
                eink_pending = False
                last_eink_draw = time.time()
            elif got_typing:
                # At least one printable char landed this tick.
                # Schedule a debounced redraw; also nudge the TTY line
                # so SSH sessions see the input immediately (cheap).
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
                        lora.send_ack(msg_id)
                        is_new = ui.add_message(sender, text, outgoing=False, msg_id=msg_id)
                        if is_new:
                            last_activity = time.time()
                            if ui.state == "sleep":
                                ui.wake()
                                asyncio.create_task(buzzer.beep_alarm())
                            else:
                                asyncio.create_task(buzzer.beep_incoming())
                            ui.full_redraw()
                            last_eink_draw = time.time()
                    elif rtype == "ack":
                        if ui.mark_delivered(data):
                            asyncio.create_task(buzzer.beep_ack())
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
                    if ui.state != "sleep":
                        ui.full_redraw()
                        last_eink_draw = time.time()

            if now - last_flush > FLUSH_INTERVAL:
                last_flush = now
                ui.flush_history()

            # Auto-sleep: chat state + Wi-Fi off + idle timeout.
            # Wi-Fi on means an SSH session is in progress; don't
            # ambush the user with the full-refresh sleep flash.
            if (ui.state == "chat"
                    and not ui.wifi_on
                    and (now - last_activity) > IDLE_TIMEOUT):
                ui.enter_sleep()
                last_eink_draw = time.time()
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
