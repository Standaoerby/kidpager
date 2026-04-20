#!/usr/bin/env python3
"""KidPager - LoRa messenger for kids."""
import asyncio, os, sys, time
from config import Config
from keyboard import KeyboardReader
from lora import LoRaRadio
from ui import PagerUI
from buzzer import Buzzer

CONFIG = os.path.expanduser("~/.kidpager/config.json")
FLUSH_INTERVAL = 2.0  # seconds between history flushes to SD card

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
    print("\nReady! Enter=send Esc=menu\n")
    ui.full_redraw()

    last_key = 0
    eink_pending = False
    last_kb_check = time.time()
    last_ack_check = time.time()
    last_flush = time.time()

    try:
        while True:
            got_key = False
            action = None

            for _ in range(20):
                key = kb.read_key_sync()
                if key is None: break
                got_key = True
                last_key = time.time()
                a = ui.handle_key(key)
                if a == "send": action = "send"

            if action == "send":
                msg = ui.get_message()
                if msg:
                    msg_id = None
                    if lora_ok: msg_id = lora.send(config.name, msg)
                    ui.add_message(config.name, msg, outgoing=True, msg_id=msg_id)
                    asyncio.create_task(buzzer.beep_sent())
                ui.full_redraw()
                eink_pending = False
            elif got_key:
                ui.term_input_line()
                eink_pending = True

            if eink_pending and (time.time() - last_key > 0.3):
                ui.eink_refresh()
                eink_pending = False

            now = time.time()
            if now - last_kb_check > 5:
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
                        ui.add_message(sender, text, outgoing=False)
                        lora.send_ack(msg_id)
                        asyncio.create_task(buzzer.beep_incoming())
                        ui.full_redraw()
                    elif rtype == "ack":
                        if ui.mark_delivered(data):
                            asyncio.create_task(buzzer.beep_ack())
                            ui.full_redraw()

            if now - last_ack_check > 2:
                last_ack_check = now
                before = sum(1 for m in ui.messages if m.status == "x")
                if ui.check_timeouts():
                    after = sum(1 for m in ui.messages if m.status == "x")
                    if after > before:
                        asyncio.create_task(buzzer.beep_error())
                    ui.full_redraw()

            if now - last_flush > FLUSH_INTERVAL:
                last_flush = now
                ui.flush_history()

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        pass

    config.save()
    ui.flush_history()  # persist any pending changes on shutdown
    if lora_ok: lora.cleanup()
    if ui.eink:
        ui.eink.cleanup()   # stop worker thread
        ui.eink.sleep()     # deep-sleep display so BUSY drops LOW for next start
    buzzer.cleanup()
    kb.close()

if __name__ == "__main__":
    asyncio.run(main())
