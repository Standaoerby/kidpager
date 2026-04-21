"""E-Ink display driver for Waveshare 2.13 V4 HAT.

Rendering runs in a background worker thread so the main asyncio loop
never blocks on a partial/full refresh (~300 ms partial, ~2 s full).
The worker uses a single-slot "latest image wins" queue: if a new image
is submitted while the worker is still drawing, the pending image is
replaced - we never render a stale frame and the caller never waits.

Layout (250x122):
  [0..14]  header bar (inverted, 15px) -- name, badges, LoRa
  [17..101] message area (6 lines * 14px) -- multi-line messages with wrap
  [103]    separator
  [105..121] input line

Header badges, right-to-left:
  LoRa / ----  always shown, far right
  W            when Wi-Fi is ON (debug state)
  M            when silent mode is ON

Multi-line messages:
  - Wrapped with word boundaries where possible, char-break for oversize words
  - Timestamp right-aligned on the FIRST line of each message (small font)
  - Continuation lines are indented by 2 spaces

Screens:
  draw_chat         -- normal chat view
  draw_profile      -- 4-item menu: Name / Channel / Silent / Back
  draw_name_edit    -- text input for name
  draw_channel_edit -- numeric picker for channel
  draw_sleep        -- screen saver shown after idle timeout
"""
import sys, time, threading
import RPi.GPIO as GPIO
sys.path.insert(0, "/home/pi")
from PIL import Image, ImageDraw, ImageFont
from pins import EINK_RST, EINK_BUSY

WIDTH = 250
HEIGHT = 122

# Layout
HEADER_H = 15
MSG_TOP = 17
LINE_H = 14
MAX_MSG_LINES = 6
SEPARATOR_Y = 103
INPUT_TOP = 105

# Badge X positions on the header (all y = 2 for small font alignment)
BADGE_LORA_X = WIDTH - 32   # "LoRa" or "----"
BADGE_WIFI_X = WIDTH - 45   # "W"
BADGE_MUTE_X = WIDTH - 58   # "M"

_STATUS_SENDING = "~"

try:
    FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    FONT_SM = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    FONT_BD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    FONT_BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
except Exception:
    FONT = ImageFont.load_default()
    FONT_SM = FONT
    FONT_BD = FONT
    FONT_BIG = FONT


def _relative_time(ts):
    diff = time.time() - ts
    if diff < 10:      return "now"
    elif diff < 60:    return f"{int(diff)}s"
    elif diff < 3600:  return f"{int(diff/60)}m"
    elif diff < 86400: return f"{int(diff/3600)}h"
    else:              return f"{int(diff/86400)}d"


def _text_width(font, s):
    try:
        return font.getlength(s)
    except Exception:
        return font.getbbox(s)[2]


def _wrap_msg(prefix, text, font, first_max_w, max_w):
    """Word-wrap 'prefix + text' so the first line fits within first_max_w
    pixels (to reserve space for a right-aligned timestamp) and subsequent
    lines fit within max_w. Long words fall back to char-break."""
    words = (prefix + text).split(" ")
    if not any(words):
        return [""]
    lines = []
    cur = ""
    cur_max = first_max_w
    for word in words:
        if word == "":
            word = " "
        trial = (cur + " " + word) if cur else word
        if _text_width(font, trial) <= cur_max:
            cur = trial; continue
        if cur:
            lines.append(cur); cur = ""; cur_max = max_w
        if _text_width(font, word) <= cur_max:
            cur = word; continue
        rem = word
        while rem:
            c = len(rem)
            while c > 0 and _text_width(font, rem[:c]) > cur_max:
                c -= 1
            if c == 0:
                c = 1
            lines.append(rem[:c])
            rem = rem[c:]
            cur_max = max_w
        cur = ""
    if cur:
        lines.append(cur)
    return lines or [""]


def _build_message_lines(messages, font, font_sm, max_w):
    """Flatten every visible message into a list of (line_text, ts_str) pairs.
    ts_str is set only on the first line of each message (and only when the
    line fits in first_line_max so the timestamp doesn't overlap the text).
    Continuation lines get ts_str=None and a 2-space indent baked in."""
    rendered = []
    for msg in messages:
        ts_str = _relative_time(msg.timestamp)
        ts_w = _text_width(font_sm, ts_str) + 6
        if msg.outgoing:
            status = msg.status
            retries = getattr(msg, "retries", 0)
            if status == _STATUS_SENDING and retries > 0:
                status = f"~{retries}"
            prefix = f"[{status}] {msg.sender}: "
        else:
            prefix = f"{msg.sender}: "

        first_line_max = max_w - ts_w
        if first_line_max < 40:
            # Not enough room for a timestamp without clobbering the message;
            # drop the timestamp and use the full width for text.
            wrapped = _wrap_msg(prefix, msg.text, font, max_w, max_w)
            rendered.append((wrapped[0], None))
        else:
            wrapped = _wrap_msg(prefix, msg.text, font, first_line_max, max_w)
            rendered.append((wrapped[0], ts_str))
        for w in wrapped[1:]:
            rendered.append(("  " + w, None))
    return rendered


def _hw_reset():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(EINK_RST, GPIO.OUT)
    GPIO.setup(EINK_BUSY, GPIO.IN)
    GPIO.output(EINK_RST, GPIO.HIGH); time.sleep(0.05)
    GPIO.output(EINK_RST, GPIO.LOW);  time.sleep(0.5)
    GPIO.output(EINK_RST, GPIO.HIGH); time.sleep(0.5)
    for _ in range(300):
        if GPIO.input(EINK_BUSY) == 0:
            return True
        time.sleep(0.01)
    print("WARNING: E-Ink BUSY stuck after reset")
    return False


_hw_reset()
from waveshare_epd import epd2in13_V4 as epd_driver


def _draw_header(d, name, lora_on=False, wifi_on=False, silent=False):
    """Common header: inverted bar with name on the left, LoRa/W/M badges
    on the right."""
    d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
    d.text((3, 1), name, font=FONT_BD, fill=255)
    if silent:
        d.text((BADGE_MUTE_X, 2), "M", font=FONT_BD, fill=255)
    if wifi_on:
        d.text((BADGE_WIFI_X, 2), "W", font=FONT_BD, fill=255)
    lora = "LoRa" if lora_on else "----"
    d.text((BADGE_LORA_X, 2), lora, font=FONT_SM, fill=255)


class EInkDisplay:
    def __init__(self):
        self.epd = epd_driver.EPD()
        self.epd.init()
        self.epd.Clear(0xFF)
        self.first_draw = True
        self.updates = 0
        self._pending = None
        self._lock = threading.Lock()
        self._hw_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._worker, daemon=True, name="eink-worker")
        self._thread.start()
        print(f"E-Ink: {WIDTH}x{HEIGHT}, V4 (bg worker)")

    def draw_chat(self, name, channel, messages, input_text,
                  lora_on=False, wifi_on=False, silent=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        _draw_header(d, name, lora_on=lora_on, wifi_on=wifi_on, silent=silent)

        # Messages (multi-line, timestamped)
        usable_w = WIDTH - 4
        all_lines = _build_message_lines(messages, FONT, FONT_SM, usable_w)
        visible = all_lines[-MAX_MSG_LINES:]
        y = MSG_TOP
        for line, ts_str in visible:
            d.text((2, y), line, font=FONT, fill=0)
            if ts_str:
                ts_x = WIDTH - _text_width(FONT_SM, ts_str) - 2
                d.text((ts_x, y + 1), ts_str, font=FONT_SM, fill=0)
            y += LINE_H

        # Input line with tail-view (drop leading chars if input overflows)
        d.line([(0, SEPARATOR_Y), (WIDTH, SEPARATOR_Y)], fill=0)
        cursor_str = f"> {input_text}"
        if _text_width(FONT, cursor_str) <= usable_w:
            visible_inp = cursor_str
        else:
            tail = input_text
            while tail and _text_width(FONT, f"> {tail}") > usable_w - 4:
                tail = tail[1:]
            visible_inp = f">.{tail}"
        d.text((3, INPUT_TOP), visible_inp, font=FONT, fill=0)

        self._submit(img)

    def draw_profile(self, name, channel, silent, selection):
        """Render the 4-item profile menu. `silent` is the current state of
        the silent-mode toggle, shown on the third line as ON/OFF."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "PROFILE", font=FONT_BD, fill=255)

        silent_label = f"Silent: {'ON' if silent else 'OFF'}"
        items = [
            f"Name: {name}",
            f"Channel: {channel}",
            silent_label,
            "Back to chat",
        ]

        # 4 items fit in 96 px: y = 22, 44, 66, 88 at 22 px spacing.
        y = 22
        row_h = 22
        for i, item in enumerate(items):
            if i == selection:
                d.rectangle([4, y - 3, WIDTH - 4, y + 15], fill=0)
                d.text((10, y), item, font=FONT, fill=255)
            else:
                d.text((10, y), item, font=FONT, fill=0)
            y += row_h
        self._submit(img)

    def draw_name_edit(self, name):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "EDIT NAME", font=FONT_BD, fill=255)
        d.text((10, 42), "Name:", font=FONT, fill=0)
        d.rectangle([10, 60, WIDTH - 10, 78], outline=0)
        d.text((14, 62), name, font=FONT, fill=0)
        self._submit(img)

    def draw_channel_edit(self, channel):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "EDIT CHANNEL", font=FONT_BD, fill=255)
        d.text((10, 30), "Channel (1-99):", font=FONT, fill=0)
        d.rectangle([10, 50, WIDTH - 10, 72], outline=0)
        d.text((18, 54), f"<   {channel}   >", font=FONT_BD, fill=0)
        d.text((10, 82), "UP/DOWN or L/R: change", font=FONT_SM, fill=0)
        d.text((10, 96), "ENTER: save", font=FONT_SM, fill=0)
        self._submit(img)

    def draw_sleep(self, name, silent=False):
        """Screen saver shown after IDLE_TIMEOUT of inactivity. Minimal
        drawing to reduce E-Ink wear: big 'Zzz' top-centered, owner name
        below, tiny hint at the bottom. A muted badge is shown bottom-right
        if silent mode is on, so the user knows alarms won't sound."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)

        # Big "Zzz"
        zzz = "Zzz"
        zw = _text_width(FONT_BIG, zzz)
        d.text(((WIDTH - zw) // 2, 6), zzz, font=FONT_BIG, fill=0)

        # Name
        nw = _text_width(FONT_BD, name)
        d.text(((WIDTH - nw) // 2, 60), name, font=FONT_BD, fill=0)

        # Hint
        hint = "Press any key"
        hw = _text_width(FONT_SM, hint)
        d.text(((WIDTH - hw) // 2, 88), hint, font=FONT_SM, fill=0)

        # Mute badge bottom-right so the kid knows whether they'll hear the
        # next incoming message. Silent = quiet icon in the corner; not
        # silent = nothing (default awake behavior).
        if silent:
            d.text((WIDTH - 44, 104), "(muted)", font=FONT_SM, fill=0)

        self._submit(img)

    # ---------- worker plumbing ----------
    def _submit(self, img):
        with self._lock:
            self._pending = img
        self._wake.set()

    def _worker(self):
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            while not self._stop:
                with self._lock:
                    img = self._pending
                    self._pending = None
                if img is None:
                    break
                try:
                    with self._hw_lock:
                        self._render(img)
                except Exception as e:
                    print(f"E-Ink worker error: {e}")

    def _render(self, img):
        buf = self.epd.getbuffer(img)
        if self.first_draw:
            self.epd.display(buf)
            self.first_draw = False
            self.updates = 0
        else:
            self.updates += 1
            if self.updates >= 20:
                self.epd.init()
                self.epd.display(buf)
                self.updates = 0
            else:
                self.epd.displayPartial(buf)

    def clear(self):
        with self._lock:
            self._pending = None
        with self._hw_lock:
            self.epd.init()
            self.epd.Clear(0xFF)
            self.first_draw = True

    def cleanup(self):
        self._stop = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def sleep(self):
        with self._hw_lock:
            self.epd.sleep()
