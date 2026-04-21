"""E-Ink display driver for Waveshare 2.13 V4 HAT.

Rendering runs in a background worker thread so the main asyncio loop
never blocks on a partial/full refresh (~300 ms partial, ~2 s full).
The worker uses a single-slot "latest image wins" queue: if a new image
is submitted while the worker is still drawing, the pending image is
replaced - we never render a stale frame and the caller never waits.

Layout (250x122):
  [0..14]  header bar (inverted, 15px) -- name, Wi-Fi badge, LoRa badge
  [17..101] message area (6 lines * 14px) -- multi-line messages with wrap
  [103]    separator
  [105..121] input line (16px)

Multi-line messages:
  - Wrapped with word boundaries where possible, char-break for oversize words
  - Timestamp right-aligned on the FIRST line of each message (small font)
  - Continuation lines are indented by 2 spaces so they're visually linked
  - Area shows the last MAX_MSG_LINES lines regardless of message boundaries
    (i.e. a very long recent message can push older messages off-screen -- use
    UP/DOWN to scroll back)
"""
import sys, time, threading
import RPi.GPIO as GPIO
sys.path.insert(0, "/home/pi")
from PIL import Image, ImageDraw, ImageFont
from pins import EINK_RST, EINK_BUSY

WIDTH = 250
HEIGHT = 122

# Layout
HEADER_H = 15       # y = 0..14 filled black, white text inside
MSG_TOP = 17        # first pixel of message area
LINE_H = 14         # per-line height in message area
MAX_MSG_LINES = 6   # 6 * 14 = 84 px, ends at y = 101
SEPARATOR_Y = 103
INPUT_TOP = 105     # ends around y = 121 with FONT (12pt) ascent/descent

# Rendered retry marker: status "~" becomes "~1", "~2" once retries > 0,
# so the user can tell a message is being retransmitted.
_STATUS_SENDING = "~"

try:
    FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    FONT_SM = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    FONT_BD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
except Exception:
    FONT = ImageFont.load_default()
    FONT_SM = FONT
    FONT_BD = FONT


def _relative_time(ts):
    """Short relative-time string (matches ui.relative_time but kept local so
    this module has no import cycle with ui)."""
    diff = time.time() - ts
    if diff < 10:      return "now"
    elif diff < 60:    return f"{int(diff)}s"
    elif diff < 3600:  return f"{int(diff/60)}m"
    elif diff < 86400: return f"{int(diff/3600)}h"
    else:              return f"{int(diff/86400)}d"


def _text_width(font, s):
    """Pixel width of s in font. Works for FreeTypeFont and fallback ImageFont."""
    try:
        return font.getlength(s)
    except Exception:
        # Very old Pillow fallback
        return font.getbbox(s)[2]


def _wrap_msg(prefix, text, font, first_max_w, max_w):
    """Word-wrap 'prefix + text' so the FIRST line fits within first_max_w pixels
    and subsequent lines fit within max_w pixels. The narrower first line
    reserves space for a right-aligned timestamp.

    Words longer than the available line width are broken by characters as a
    last resort (URLs, concatenated text, etc.).

    Returns a non-empty list of line strings (no newline characters).
    """
    words = (prefix + text).split(" ")
    if not any(words):
        return [""]

    lines = []
    cur = ""
    cur_max = first_max_w
    for word in words:
        if word == "":
            word = " "   # preserve double-space as a single space token
        trial = (cur + " " + word) if cur else word
        if _text_width(font, trial) <= cur_max:
            cur = trial
            continue
        # Trial overflows current line.
        if cur:
            lines.append(cur)
            cur = ""
            cur_max = max_w
        # Retry on a fresh line.
        if _text_width(font, word) <= cur_max:
            cur = word
            continue
        # Word alone longer than line -> character-break.
        rem = word
        while rem:
            c = len(rem)
            while c > 0 and _text_width(font, rem[:c]) > cur_max:
                c -= 1
            if c == 0:
                c = 1   # give up, accept a pixel of overflow rather than loop forever
            lines.append(rem[:c])
            rem = rem[c:]
            cur_max = max_w
        cur = ""
    if cur:
        lines.append(cur)
    return lines or [""]


def _build_message_lines(messages, font, font_sm, max_w):
    """Turn a list of Message objects into (text, timestamp_or_None, indent) tuples.
    The first line of each message carries a timestamp; continuation lines get
    indent=True so the caller can visually link them to the parent message."""
    rendered = []
    for msg in messages:
        ts_str = _relative_time(msg.timestamp)
        ts_w = _text_width(font_sm, ts_str) + 6  # 4 px margin + 2 px gap

        # Build the text prefix: "[status] sender: " for outgoing (with retry
        # indicator), "sender: " for incoming.
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
            # Degenerate: screen too narrow to reserve timestamp space.
            # Drop the timestamp for this message rather than clip the text.
            first_line_max = max_w
            wrapped = _wrap_msg(prefix, msg.text, font, first_line_max, max_w)
            rendered.append((wrapped[0], None, False))
        else:
            wrapped = _wrap_msg(prefix, msg.text, font, first_line_max, max_w)
            rendered.append((wrapped[0], ts_str, False))

        for w in wrapped[1:]:
            rendered.append(("  " + w, None, True))
    return rendered


def _hw_reset():
    """Hardware reset e-ink before driver init to clear stuck BUSY."""
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(EINK_RST, GPIO.OUT)
    GPIO.setup(EINK_BUSY, GPIO.IN)
    GPIO.output(EINK_RST, GPIO.HIGH)
    time.sleep(0.05)
    GPIO.output(EINK_RST, GPIO.LOW)
    time.sleep(0.5)
    GPIO.output(EINK_RST, GPIO.HIGH)
    time.sleep(0.5)
    for _ in range(300):
        if GPIO.input(EINK_BUSY) == 0:
            return True
        time.sleep(0.01)
    print("WARNING: E-Ink BUSY stuck after reset")
    return False


_hw_reset()
from waveshare_epd import epd2in13_V4 as epd_driver


class EInkDisplay:
    def __init__(self):
        self.epd = epd_driver.EPD()
        self.epd.init()
        self.epd.Clear(0xFF)
        self.first_draw = True
        self.updates = 0
        # Worker state
        self._pending = None              # latest image awaiting render; "latest wins"
        self._lock = threading.Lock()     # protects _pending
        self._hw_lock = threading.Lock()  # serialises actual SPI/hardware access
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._worker, daemon=True, name="eink-worker")
        self._thread.start()
        print(f"E-Ink: {WIDTH}x{HEIGHT}, V4 (bg worker)")

    def draw_chat(self, name, channel, messages, input_text, lora_on=False, wifi_on=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)

        # --- header ---
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        lora = "LoRa" if lora_on else "----"
        d.text((3, 1), name, font=FONT_BD, fill=255)
        # W badge left of LoRa when Wi-Fi is ON (debug state)
        if wifi_on:
            d.text((WIDTH - 45, 2), "W", font=FONT_BD, fill=255)
        d.text((WIDTH - 32, 2), lora, font=FONT_SM, fill=255)

        # --- messages (multi-line, timestamped) ---
        usable_w = WIDTH - 4  # 2 px padding each side
        all_lines = _build_message_lines(messages, FONT, FONT_SM, usable_w)
        # Show the most recent MAX_MSG_LINES lines; older stuff is off-screen
        # until the user scrolls up (UP arrow). Scroll is applied by the caller
        # before passing the slice in.
        visible = all_lines[-MAX_MSG_LINES:]
        y = MSG_TOP
        for line, ts_str, _indent in visible:
            d.text((2, y), line, font=FONT, fill=0)
            if ts_str:
                ts_x = WIDTH - _text_width(FONT_SM, ts_str) - 2
                # FONT_SM is 10 px, FONT is 12 px; nudge ts down by 1 px so
                # baselines roughly align.
                d.text((ts_x, y + 1), ts_str, font=FONT_SM, fill=0)
            y += LINE_H

        # --- input line ---
        d.line([(0, SEPARATOR_Y), (WIDTH, SEPARATOR_Y)], fill=0)
        # Input wraps visually to a "tail view": if the string overflows, we
        # drop leading characters (not trailing) so the user always sees what
        # they just typed plus the cursor. Keep the ">" prefix glued.
        cursor_str = f"> {input_text}"
        if _text_width(FONT, cursor_str) <= usable_w:
            visible_inp = cursor_str
        else:
            # Drop chars from the middle (after "> ") until it fits.
            tail = input_text
            while tail and _text_width(FONT, f"> {tail}") > usable_w - 4:
                tail = tail[1:]
            visible_inp = f">.{tail}"   # "." hints that we dropped characters
        d.text((3, INPUT_TOP), visible_inp, font=FONT, fill=0)

        self._submit(img)

    def draw_profile(self, name, channel, selection):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "PROFILE", font=FONT_BD, fill=255)
        items = [f"Name: {name}", f"Channel: {channel}", "Back to chat"]
        y = 26
        for i, item in enumerate(items):
            if i == selection:
                d.rectangle([4, y - 3, WIDTH - 4, y + 15], fill=0)
                d.text((10, y), item, font=FONT, fill=255)
            else:
                d.text((10, y), item, font=FONT, fill=0)
            y += 24
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
        # `< N >` cue - arrows on the screen hint that arrow keys change the value.
        d.text((18, 54), f"<   {channel}   >", font=FONT_BD, fill=0)
        d.text((10, 82), "UP/DOWN or L/R: change", font=FONT_SM, fill=0)
        d.text((10, 96), "ENTER: save", font=FONT_SM, fill=0)
        self._submit(img)

    def _submit(self, img):
        """Hand the image to the worker. Latest-wins: replaces any queued frame."""
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
                # Full refresh every 20 partials -- clears ghosting. This is a
                # hardware limitation of E-Ink, not an optional choice.
                self.epd.init()
                self.epd.display(buf)
                self.updates = 0
            else:
                self.epd.displayPartial(buf)

    def clear(self):
        """Drop any pending frame and fully clear the screen."""
        with self._lock:
            self._pending = None
        with self._hw_lock:
            self.epd.init()
            self.epd.Clear(0xFF)
            self.first_draw = True

    def cleanup(self):
        """Stop the worker cleanly (called on shutdown)."""
        self._stop = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def sleep(self):
        with self._hw_lock:
            self.epd.sleep()
