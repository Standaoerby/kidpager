"""UI module for KidPager."""
import time, sys, json, os

try:
    from display_eink import EInkDisplay
    HAS_EINK = True
except Exception as e:
    print(f"E-Ink not available: {e}")
    HAS_EINK = False

STATUS_LOCAL = "."
STATUS_SENDING = "~"
STATUS_OK = "+"
STATUS_FAIL = "x"
ACK_TIMEOUT = 4        # seconds per attempt (send + ack round-trip)
MAX_RETRIES = 2        # retransmit count before giving up (total attempts = 1 + MAX_RETRIES)
HISTORY_FILE = os.path.expanduser("~/.kidpager/history.json")
MAX_HISTORY = 100

def relative_time(ts):
    diff = time.time() - ts
    if diff < 10: return "now"
    elif diff < 60: return f"{int(diff)}s"
    elif diff < 3600: return f"{int(diff/60)}m"
    elif diff < 86400: return f"{int(diff/3600)}h"
    else: return f"{int(diff/86400)}d"

class Message:
    def __init__(self, sender, text, outgoing=False, msg_id=None, timestamp=None, status=None):
        self.sender = sender
        self.text = text
        self.outgoing = outgoing
        self.msg_id = msg_id
        # `timestamp or time.time()` was buggy: a loaded timestamp of 0 falsy-ed into "now".
        self.timestamp = time.time() if timestamp is None else timestamp
        self.retries = 0                      # in-memory only; incremented by check_timeouts
        self.last_sent_ts = self.timestamp    # when the most recent TX attempt went out
        if status:
            self.status = status
        else:
            self.status = STATUS_SENDING if (outgoing and msg_id) else STATUS_LOCAL

    def to_dict(self):
        return {"sender":self.sender,"text":self.text,"outgoing":self.outgoing,
                "msg_id":self.msg_id,"timestamp":self.timestamp,"status":self.status}

    @staticmethod
    def from_dict(d):
        return Message(sender=d["sender"],text=d["text"],outgoing=d.get("outgoing",False),
                      msg_id=d.get("msg_id"),timestamp=d.get("timestamp"),status=d.get("status",STATUS_LOCAL))

class PagerUI:
    def __init__(self, config, lora=None):
        self.config = config
        self.lora = lora
        self.messages = []
        self.input_buf = ""
        self.state = "chat"; self.scroll = 0
        self.profile_sel = 0
        self.eink = None
        self.wifi_on = False   # set by main.py from power.wifi_is_enabled()
        self._dirty = False  # history needs flushing to disk
        self._load_history()
        if HAS_EINK:
            try:
                self.eink = EInkDisplay()
            except Exception as e:
                print(f"E-Ink init failed: {e}")

    def _load_history(self):
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
                self.messages = [Message.from_dict(d) for d in data[-MAX_HISTORY:]]
                for m in self.messages:
                    if m.status == STATUS_SENDING:
                        m.status = STATUS_FAIL
                        self._dirty = True  # sender -> failed needs saving
                print(f"Loaded {len(self.messages)} messages")
        except:
            self.messages = []

    def flush_history(self):
        """Write history to disk only if something changed since last flush.
        Called periodically from the main loop to avoid SD-card write amplification."""
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                json.dump([m.to_dict() for m in self.messages[-MAX_HISTORY:]], f)
            self._dirty = False
        except Exception as e:
            print(f"History save error: {e}")

    def handle_key(self, key):
        # Alt+W is global: toggles Wi-Fi from any screen, doesn't consume input.
        if key == "WIFI":
            return "toggle_wifi"
        if self.state == "chat": return self._handle_chat(key)
        elif self.state == "profile": return self._handle_profile(key)
        elif self.state == "name_edit": return self._handle_name_edit(key)
        elif self.state == "channel_edit": return self._handle_channel_edit(key)
        return None

    def set_wifi(self, on):
        """Called from main.py after power.wifi_toggle(); updates the header badge."""
        self.wifi_on = bool(on)

    def _handle_chat(self, key):
        if key == "ENTER": return "send"
        elif key == "BACKSPACE": self.input_buf = self.input_buf[:-1]
        elif key == "ESC" or key == "TAB": self.state = "profile"; self.profile_sel = 0; self.full_redraw()
        elif key == "UP":
            self.scroll = min(self.scroll + 1, max(0, len(self.messages) - 5))
            self.full_redraw()
        elif key == "DOWN":
            self.scroll = max(0, self.scroll - 1)
            self.full_redraw()
        elif isinstance(key, str) and len(key) == 1: self.input_buf += key; self.scroll = 0
        return None

    def _handle_profile(self, key):
        if key == "UP": self.profile_sel = max(0, self.profile_sel - 1); self.full_redraw()
        elif key == "DOWN": self.profile_sel = min(2, self.profile_sel + 1); self.full_redraw()
        elif key == "ENTER":
            if self.profile_sel == 0: self.state = "name_edit"; self.full_redraw()
            elif self.profile_sel == 1: self.state = "channel_edit"; self.full_redraw()
            elif self.profile_sel == 2: self.config.save(); self.state = "chat"; self.full_redraw()
        elif key == "ESC" or key == "TAB": self.config.save(); self.state = "chat"; self.full_redraw()
        return None

    def _handle_name_edit(self, key):
        if key == "ENTER": self.config.save(); self.state = "profile"; self.full_redraw()
        elif key == "BACKSPACE": self.config.name = self.config.name[:-1]; self.full_redraw()
        elif key == "ESC" or key == "TAB": self.state = "profile"; self.full_redraw()
        elif isinstance(key, str) and len(key) == 1: self.config.name += key; self.full_redraw()
        return None

    def _handle_channel_edit(self, key):
        # Channel 1..99. UP/RIGHT +1, DOWN/LEFT -1, ENTER saves + back to profile.
        if key == "ENTER": self.config.save(); self.state = "profile"; self.full_redraw()
        elif key in ("UP", "RIGHT"): self.config.channel = min(99, self.config.channel + 1); self.full_redraw()
        elif key in ("DOWN", "LEFT"): self.config.channel = max(1, self.config.channel - 1); self.full_redraw()
        elif key == "ESC" or key == "TAB": self.state = "profile"; self.full_redraw()
        return None

    def get_message(self):
        msg = self.input_buf.strip(); self.input_buf = ""; return msg

    def add_message(self, sender, text, outgoing=False, msg_id=None):
        # Dedupe incoming retries: sender resends on ack loss, we must not show it twice.
        # Outgoing messages don't need dedupe (we only call add_message once on send).
        if not outgoing and msg_id:
            for m in reversed(self.messages[-20:]):
                if not m.outgoing and m.msg_id == msg_id:
                    return False
        self.messages.append(Message(sender, text, outgoing, msg_id))
        if len(self.messages) > MAX_HISTORY:
            self.messages = self.messages[-MAX_HISTORY:]
        self._dirty = True
        self.scroll = 0
        return True

    def mark_delivered(self, msg_id):
        for m in reversed(self.messages):
            if m.msg_id == msg_id and m.status == STATUS_SENDING:
                m.status = STATUS_OK; self._dirty = True; return True
        return False

    def check_timeouts(self):
        """Retransmit pending messages up to MAX_RETRIES; flip to FAIL when exhausted.
        Returns True if any message transitioned (so main loop redraws)."""
        changed = False; now = time.time()
        for m in self.messages:
            if m.status != STATUS_SENDING:
                continue
            if (now - m.last_sent_ts) <= ACK_TIMEOUT:
                continue
            if m.retries < MAX_RETRIES and self.lora is not None and m.msg_id:
                # Retransmit with the SAME msg_id so receiver can dedupe.
                try:
                    self.lora.send(m.sender, m.text, msg_id=m.msg_id)
                except Exception as e:
                    print(f"retry TX error: {e}")
                m.retries += 1
                m.last_sent_ts = now
                # status stays STATUS_SENDING — caller won't beep_error yet
                changed = True
            else:
                m.status = STATUS_FAIL
                changed = True
        if changed:
            self._dirty = True
        return changed

    def full_redraw(self): self._term_redraw(); self.eink_refresh()

    def eink_refresh(self):
        if not self.eink: return
        try:
            if self.state == "chat":
                self.eink.draw_chat(self.config.name, self.config.channel,
                                     self.messages[max(0,len(self.messages)-5-self.scroll):len(self.messages)-self.scroll] if self.scroll else self.messages, self.input_buf, self.lora is not None, self.wifi_on)
            elif self.state == "profile":
                self.eink.draw_profile(self.config.name, self.config.channel, self.profile_sel)
            elif self.state == "name_edit":
                self.eink.draw_name_edit(self.config.name)
            elif self.state == "channel_edit":
                self.eink.draw_channel_edit(self.config.channel)
        except Exception as e:
            print(f"E-Ink error: {e}")

    def _term_redraw(self):
        lora = "LoRa:ON" if self.lora else "LoRa:OFF"
        wifi = "  WiFi:ON" if self.wifi_on else ""
        print(f"\033[2J\033[H KidPager [{self.config.name}]  {lora}{wifi}")
        print("-" * 50)
        end = len(self.messages) - self.scroll
        start = max(0, end - 8)
        shown = self.messages[start:end]
        for msg in shown:
            t = relative_time(msg.timestamp)
            if msg.outgoing: print(f"  [{msg.status}] {msg.sender}: {msg.text}  ({t})")
            else: print(f"  {msg.sender}: {msg.text}  ({t})")
        for _ in range(8 - len(shown)): print()
        print("-" * 50)
        print(f" > {self.input_buf}_")
        print("=" * 50)

    def term_input_line(self):
        sys.stdout.write(f"\r > {self.input_buf}_   \r"); sys.stdout.flush()
