"""UI module for KidPager.

Retry policy for outgoing messages:
  ACK_TIMEOUT       seconds per attempt (send + expected ack round-trip)
  MAX_RETRIES       number of retransmits after the first send
  CHECK_INTERVAL    how often main.py calls check_timeouts()
Total attempts = 1 initial + MAX_RETRIES retries = 3 by default.
Worst-case time to FAIL = (MAX_RETRIES + 1) * ACK_TIMEOUT + CHECK_INTERVAL.
With defaults = 3 * 4 + 2 = 14 s.

While a message is being retransmitted, its status stays STATUS_SENDING (`~`)
but the UI shows `~1`, `~2`, ... so the user can see the attempt count.

States:
  "chat"         -- main view: messages + input line
  "profile"      -- TAB/ESC menu: Name / Channel / Silent / Back
  "name_edit"    -- text edit for name
  "channel_edit" -- number picker for channel
  "sleep"        -- screen saver after IDLE_TIMEOUT seconds of inactivity.
                    Any key or incoming message returns to "chat".

Emoji shortcuts
---------------
Typing ``:)`` replaces the trailing 2 chars in the input buffer with
the slightly-smiling Unicode emoji. Table below. Replacement is done
at append time (so the E-Ink view already shows the emoji as the user
types) and again at send time (``expand_emoji_in_full``) to catch any
shortcuts the user typed past without pausing.

Cursor
------
A static underscore ``_`` is drawn at the END of the input buffer. We
don't support caret movement into the middle of the buffer in this
release -- LEFT/RIGHT are UI-level navigation keys. If the user wants
to edit mid-line, backspace is the tool.
"""
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

ACK_TIMEOUT = 4
MAX_RETRIES = 2
CHECK_INTERVAL = 2

HISTORY_FILE = os.path.expanduser("~/.kidpager/history.json")
MAX_HISTORY = 100
EINK_WINDOW = 30

# Emoji shortcut table. Each entry = (typed_sequence, emoji_char).
# The emoji is sent on the air as UTF-8 like any other character, so
# nothing in lora.py changes. Entries are sorted longest-first at
# module load so ":'(" matches before ":(" and "XD" before "X".
EMOJI_SHORTCUTS = [
    (":)",  "\U0001F642"),  # 🙂
    (":(",  "\U0001F641"),  # 🙁
    (":D",  "\U0001F604"),  # 😄
    (":P",  "\U0001F61B"),  # 😛
    (":O",  "\U0001F62E"),  # 😮
    (";)",  "\U0001F609"),  # 😉
    ("<3",  "\u2764\uFE0F"), # ❤️
    (":|",  "\U0001F610"),  # 😐
    (":*",  "\U0001F618"),  # 😘
    ("xD",  "\U0001F606"),  # 😆
    ("XD",  "\U0001F606"),  # 😆
    (":'(", "\U0001F622"),  # 😢
    ("^_^", "\U0001F60A"),  # 😊
    ("o_O", "\U0001F928"),  # 🤨
    ("O_o", "\U0001F928"),  # 🤨
]
_EMOJI_SORTED = sorted(EMOJI_SHORTCUTS, key=lambda kv: -len(kv[0]))


def apply_emoji_shortcuts(text):
    """If ``text`` ends with one of the shortcut sequences, replace the
    trailing sequence with the corresponding emoji. Returns
    ``(new_text, replaced)``. Only the trailing position is considered
    so mid-sentence colons don't get ambushed."""
    for seq, emoji in _EMOJI_SORTED:
        if text.endswith(seq):
            return text[:-len(seq)] + emoji, True
    return text, False


def expand_emoji_in_full(text):
    """Expand every occurrence of every shortcut anywhere in ``text``.
    Run at send time so messages like "hi :) ok :(" come out correctly
    even if the user didn't pause between shortcuts (the trailing-only
    replacement would have missed the middle one)."""
    for seq, emoji in _EMOJI_SORTED:
        if seq in text:
            text = text.replace(seq, emoji)
    return text


def relative_time(ts):
    diff = time.time() - ts
    if diff < 10: return "now"
    elif diff < 60: return f"{int(diff)}s"
    elif diff < 3600: return f"{int(diff / 60)}m"
    elif diff < 86400: return f"{int(diff / 3600)}h"
    else: return f"{int(diff / 86400)}d"


class Message:
    def __init__(self, sender, text, outgoing=False, msg_id=None, timestamp=None, status=None):
        self.sender = sender
        self.text = text
        self.outgoing = outgoing
        self.msg_id = msg_id
        self.timestamp = time.time() if timestamp is None else timestamp
        self.retries = 0
        self.last_sent_ts = self.timestamp
        if status:
            self.status = status
        else:
            self.status = STATUS_SENDING if (outgoing and msg_id) else STATUS_LOCAL

    def to_dict(self):
        return {"sender": self.sender, "text": self.text, "outgoing": self.outgoing,
                "msg_id": self.msg_id, "timestamp": self.timestamp, "status": self.status}

    @staticmethod
    def from_dict(d):
        return Message(sender=d["sender"], text=d["text"], outgoing=d.get("outgoing", False),
                       msg_id=d.get("msg_id"), timestamp=d.get("timestamp"),
                       status=d.get("status", STATUS_LOCAL))


class PagerUI:
    PROF_NAME = 0
    PROF_CHANNEL = 1
    PROF_SILENT = 2
    PROF_BACK = 3
    PROF_COUNT = 4

    def __init__(self, config, lora=None):
        self.config = config
        self.lora = lora
        self.messages = []
        self.input_buf = ""
        self.state = "chat"
        self.scroll = 0
        self.profile_sel = 0
        self.eink = None
        self.wifi_on = False
        self._dirty = False
        self._load_history()
        if HAS_EINK:
            try:
                self.eink = EInkDisplay()
            except Exception as e:
                print(f"E-Ink init failed: {e}")

    # ---------- history ----------
    def _load_history(self):
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
                self.messages = [Message.from_dict(d) for d in data[-MAX_HISTORY:]]
                for m in self.messages:
                    if m.status == STATUS_SENDING:
                        m.status = STATUS_FAIL
                        self._dirty = True
                print(f"Loaded {len(self.messages)} messages")
        except Exception:
            self.messages = []

    def flush_history(self):
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                json.dump([m.to_dict() for m in self.messages[-MAX_HISTORY:]], f)
            self._dirty = False
        except Exception as e:
            print(f"History save error: {e}")

    # ---------- key dispatch ----------
    def handle_key(self, key):
        if key == "WIFI":
            return "toggle_wifi"
        if self.state == "sleep":
            return self._handle_sleep(key)
        if self.state == "chat":           return self._handle_chat(key)
        elif self.state == "profile":      return self._handle_profile(key)
        elif self.state == "name_edit":    return self._handle_name_edit(key)
        elif self.state == "channel_edit": return self._handle_channel_edit(key)
        return None

    def set_wifi(self, on):
        self.wifi_on = bool(on)

    def _handle_chat(self, key):
        if key == "ENTER":
            return "send"
        elif key == "BACKSPACE":
            if self.input_buf:
                # Strip trailing Unicode variation selectors (U+FE00-FE0F)
                # first so one backspace deletes the visible glyph (e.g.
                # the emoji-style heart is ❤ + VS16).
                while (self.input_buf
                       and 0xFE00 <= ord(self.input_buf[-1]) <= 0xFE0F):
                    self.input_buf = self.input_buf[:-1]
                self.input_buf = self.input_buf[:-1]
        elif key == "ESC" or key == "TAB":
            self.state = "profile"
            self.profile_sel = 0
            self.full_redraw()
        elif key == "UP":
            self.scroll = min(self.scroll + 1, max(0, len(self.messages) - 1))
            self.full_redraw()
        elif key == "DOWN":
            self.scroll = max(0, self.scroll - 1)
            self.full_redraw()
        elif isinstance(key, str) and len(key) == 1:
            self.input_buf += key
            self.scroll = 0
            # Trailing-only shortcut replacement after each printable
            # key. Mid-buffer shortcuts that the user types past
            # without pausing are expanded later in get_message().
            self.input_buf, _ = apply_emoji_shortcuts(self.input_buf)
        return None

    def _handle_profile(self, key):
        if key == "UP":
            self.profile_sel = max(0, self.profile_sel - 1); self.full_redraw()
        elif key == "DOWN":
            self.profile_sel = min(self.PROF_COUNT - 1, self.profile_sel + 1); self.full_redraw()
        elif key == "ENTER":
            if self.profile_sel == self.PROF_NAME:
                self.state = "name_edit"; self.full_redraw()
            elif self.profile_sel == self.PROF_CHANNEL:
                self.state = "channel_edit"; self.full_redraw()
            elif self.profile_sel == self.PROF_SILENT:
                self.config.silent = not self.config.silent
                self.config.save()
                self.full_redraw()
                return "silent_changed"
            elif self.profile_sel == self.PROF_BACK:
                self.config.save(); self.state = "chat"; self.full_redraw()
        elif key == "ESC" or key == "TAB":
            self.config.save(); self.state = "chat"; self.full_redraw()
        return None

    def _handle_name_edit(self, key):
        if key == "ENTER":
            self.config.save(); self.state = "profile"; self.full_redraw()
        elif key == "BACKSPACE":
            self.config.name = self.config.name[:-1]; self.full_redraw()
        elif key == "ESC" or key == "TAB":
            self.state = "profile"; self.full_redraw()
        elif isinstance(key, str) and len(key) == 1:
            self.config.name += key; self.full_redraw()
        return None

    def _handle_channel_edit(self, key):
        if key == "ENTER":
            self.config.save(); self.state = "profile"; self.full_redraw()
        elif key in ("UP", "RIGHT"):
            self.config.channel = min(99, self.config.channel + 1); self.full_redraw()
        elif key in ("DOWN", "LEFT"):
            self.config.channel = max(1, self.config.channel - 1); self.full_redraw()
        elif key == "ESC" or key == "TAB":
            self.state = "profile"; self.full_redraw()
        return None

    def _handle_sleep(self, key):
        self.state = "chat"
        self.full_redraw()
        return "wake"

    def enter_sleep(self):
        if self.state != "sleep":
            self.state = "sleep"
            self.full_redraw()

    def wake(self):
        if self.state == "sleep":
            self.state = "chat"

    # ---------- messages ----------
    def get_message(self):
        """Return the send-ready string (shortcuts expanded, whitespace
        stripped) and clear the input buffer."""
        msg = self.input_buf.strip()
        msg = expand_emoji_in_full(msg)
        self.input_buf = ""
        return msg

    def add_message(self, sender, text, outgoing=False, msg_id=None):
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
        changed = False
        now = time.time()
        for m in self.messages:
            if m.status != STATUS_SENDING:
                continue
            if (now - m.last_sent_ts) <= ACK_TIMEOUT:
                continue
            if m.retries < MAX_RETRIES and self.lora is not None and m.msg_id:
                try:
                    self.lora.send(m.sender, m.text, msg_id=m.msg_id)
                except Exception as e:
                    print(f"retry TX error: {e}")
                m.retries += 1
                m.last_sent_ts = now
                changed = True
            else:
                m.status = STATUS_FAIL
                changed = True
        if changed:
            self._dirty = True
        return changed

    # ---------- rendering ----------
    def full_redraw(self):
        self._term_redraw()
        self.eink_refresh()

    def eink_refresh(self):
        if not self.eink:
            return
        try:
            if self.state == "chat":
                cutoff = len(self.messages) - self.scroll
                start = max(0, cutoff - EINK_WINDOW)
                if cutoff <= 0:
                    cutoff = 1; start = 0
                visible = self.messages[start:cutoff]
                self.eink.draw_chat(self.config.name, self.config.channel,
                                    visible, self.input_buf,
                                    self.lora is not None, self.wifi_on,
                                    self.config.silent)
            elif self.state == "profile":
                self.eink.draw_profile(self.config.name, self.config.channel,
                                       self.config.silent, self.profile_sel)
            elif self.state == "name_edit":
                self.eink.draw_name_edit(self.config.name)
            elif self.state == "channel_edit":
                self.eink.draw_channel_edit(self.config.channel)
            elif self.state == "sleep":
                self.eink.draw_sleep(self.config.name, self.config.silent)
        except Exception as e:
            print(f"E-Ink error: {e}")

    def _term_redraw(self):
        # Skip terminal redraw when not on a TTY. Under systemd stdout
        # is the journal, and pumping clear-screen escape codes into
        # it on every keystroke is both noisy and slow (measurable
        # latency added to the input path).
        if not sys.stdout.isatty():
            return
        lora = "LoRa:ON" if self.lora else "LoRa:OFF"
        wifi = "  WiFi:ON" if self.wifi_on else ""
        mute = "  MUTE" if self.config.silent else ""
        sleep_tag = "  [SLEEP]" if self.state == "sleep" else ""
        print(f"\033[2J\033[H KidPager [{self.config.name}]  {lora}{wifi}{mute}{sleep_tag}")
        print("-" * 50)
        end = len(self.messages) - self.scroll
        start = max(0, end - 8)
        shown = self.messages[start:end]
        for msg in shown:
            t = relative_time(msg.timestamp)
            if msg.outgoing:
                status = msg.status
                if status == STATUS_SENDING and msg.retries > 0:
                    status = f"~{msg.retries}"
                print(f"  [{status}] {msg.sender}: {msg.text}  ({t})")
            else:
                print(f"  {msg.sender}: {msg.text}  ({t})")
        for _ in range(8 - len(shown)): print()
        print("-" * 50)
        print(f" > {self.input_buf}_")
        print("=" * 50)

    def term_input_line(self):
        if not sys.stdout.isatty():
            return
        sys.stdout.write(f"\r > {self.input_buf}_   \r")
        sys.stdout.flush()
