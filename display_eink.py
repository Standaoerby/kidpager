"""E-Ink display driver for Waveshare 2.13 V4 HAT.

Rendering runs in a background worker thread so the main asyncio loop
never blocks on a partial/full refresh (~300 ms partial, ~2 s full).
The worker uses a single-slot "latest image wins" queue: if a new image
is submitted while the worker is still drawing, the pending image is
replaced - we never render a stale frame and the caller never waits.

Fonts (v0.14)
-------------
Previously: DejaVu Sans 12pt for everything. On a 250x122 1-bit panel
the anti-aliased TrueType rendering rounded mid-pixels to black (1-bit
has no grey), which made narrow pairs like 'ov' in "love" visually
touch. Terminus is a bitmap font designed for exactly this case --
every glyph is hand-pixelled for 1bpp output, no anti-aliasing rounding.

Terminus is monospace. Header and small labels keep DejaVu (proportional
spacing looks better for the sender name and status badges).

Cursor
------
A static underscore ``_`` is drawn immediately after the last character
of the input buffer. Not mid-line -- the UI doesn't support caret
motion in v0.14. We reserve pixel room for the cursor so tail-view
trimming doesn't accidentally hide it.
"""
import sys, time, threading, os
import RPi.GPIO as GPIO
sys.path.insert(0, "/home/pi")
from PIL import Image, ImageDraw, ImageFont
from pins import EINK_RST, EINK_BUSY

WIDTH = 250
HEIGHT = 122

# Periodic full refresh. The V4 panel accumulates LUT error under
# sustained partial refreshes; an occasional full + base-image seed
# resets it. With explicit force_full triggers on send / receive /
# state-transition (see ui.py), periodic-full is mostly a safety net
# for "user types 20 chars without hitting any other trigger" -- 20 is
# conservative enough to rarely matter in practice but still catches
# pathological cases.
#
# History: v1.0 tried 7 here to fight ghosting, combined with a double
# full refresh (display + displayPartBaseImage) that produced ~6
# visible flashes per cycle. Moving to a single displayPartBaseImage
# (which does both in one panel turn-on) plus more explicit force_full
# events lets us relax this back to 20 without the ghost coming back.
FULL_REFRESH_EVERY = 20

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

CURSOR = "_"

_STATUS_SENDING = "~"


# --- Font loading -----------------------------------------------------------
#
# Strategy:
#   1. Try Terminus in its canonical Debian paths. The fonts-terminus-otb
#      package (present on Bookworm and Trixie) ships .otb (OpenType
#      Bitmap) files which Pillow loads via its truetype() API. Path
#      changed between releases -- see _TERMINUS_CANDIDATES below.
#   2. Fall back to DejaVu Sans if Terminus isn't installed. DejaVu is
#      already a hard dependency (fonts-dejavu-core in deploy.ps1).
#   3. Final fallback: Pillow's built-in default font (tiny bitmap).
#
# Sizes are picked for the 250x122 panel: 14 px body, 10 px small, 18
# px bold header. Body 14 on Terminus = 7-px-wide monospace cells, so
# 35 chars fit across 250 px minus margins.

def _load_font(paths, size):
    """Try each path in order, return the first truetype that loads,
    or ``None`` if nothing in the list worked. Caller is responsible
    for chaining to a lower-priority fallback.

    Previous versions of this function returned ``ImageFont.load_default()``
    on failure, and the caller checked ``isinstance(FONT, ImageFont.ImageFont)``
    to decide whether to try a secondary path. That check was broken in
    both directions depending on the Pillow version:

      * Pillow < 10: ``FreeTypeFont`` was a subclass of ``ImageFont`` so
        the check was ALWAYS True -- even a successfully loaded Terminus
        got discarded in favour of DejaVu.
      * Pillow >= 10 (our Trixie target): ``FreeTypeFont`` is NOT a
        subclass so the check is ALWAYS False -- the DejaVu fallback
        never fires, so a missing Terminus drops to Pillow's bundled
        Aileron at BMP size rather than DejaVu at 12 px.

    Returning ``None`` on failure makes the caller's fallback chain
    explicit (``or``) and is correct across all Pillow versions.
    """
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return None


# Terminus body font for message text + input line. Try multiple names
# because packaging layout changed between Debian releases:
#   * Trixie (testing/stable 13, our current target) — fonts-terminus-otb
#     4.48-3.1 installs to /usr/share/fonts/opentype/terminus/ with the
#     maintainer-chosen `terminus-normal.otb` naming.
#   * Bookworm (12) shipped the same .otb files under /usr/share/fonts/X11/misc/
#     with upstream names (`ter-u14n.otb` etc.).
#   * xfonts-terminus, Arch, etc. ship TTF conversions at varied paths.
# Order matters: we return the first hit, so the Trixie path is first because
# that's what lives on the pagers we actually deploy to.
_TERMINUS_CANDIDATES = [
    "/usr/share/fonts/opentype/terminus/terminus-normal.otb",  # Debian Trixie
    "/usr/share/fonts/X11/misc/ter-u14n.otb",                  # Debian Bookworm
    "/usr/share/fonts/X11/misc/ter-u16n.otb",
    "/usr/share/fonts/terminus/TerminusTTF.ttf",
    "/usr/share/fonts/truetype/terminus/TerminusTTF.ttf",
]
_DEJAVU_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Final-fallback: Pillow's bundled default. Never fails. Used only if
# neither the primary font nor DejaVu exist (should never happen on a
# correctly deployed pager, but keeps rendering working for unit tests).
_DEFAULT = ImageFont.load_default()

# Body font for messages + input line. 14 px Terminus ~= 7 px per cell
# so ~35 cols fit in (WIDTH - 4) px. Falls back to DejaVu 12 if
# Terminus is absent, then to Pillow's bundled default.
FONT = (_load_font(_TERMINUS_CANDIDATES, 14)
        or _load_font([_DEJAVU_SANS], 12)
        or _DEFAULT)

# Small font: 10 px for header badges and timestamps. Always DejaVu
# because Terminus at 10 px is too pixelated for the tiny badges.
FONT_SM = _load_font([_DEJAVU_SANS], 10) or _DEFAULT

# Bold for header. DejaVu Bold looks better for the owner name.
FONT_BD = _load_font([_DEJAVU_BOLD], 12) or _DEFAULT

# Medium bold for multi-word headings on full-screen frames (e.g.
# "Rebooting..."). 20 px keeps 12-char strings comfortably under the
# 250 px panel width whereas FONT_BIG 36 px overflows anything longer
# than ~7 chars.
FONT_MD = _load_font([_DEJAVU_BOLD], 20) or _DEFAULT

# Big bold for the sleep screen Zzz glyph.
FONT_BIG = _load_font([_DEJAVU_BOLD], 36) or _DEFAULT


def _relative_time(ts):
    diff = time.time() - ts
    if diff < 10:      return "now"
    elif diff < 60:    return f"{int(diff)}s"
    elif diff < 3600:  return f"{int(diff / 60)}m"
    elif diff < 86400: return f"{int(diff / 3600)}h"
    else:              return f"{int(diff / 86400)}d"


def _text_width(font, s):
    try:
        return font.getlength(s)
    except Exception:
        try:
            return font.getbbox(s)[2]
        except Exception:
            # Fallback for bitmap default font
            return len(s) * 6


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
    """Flatten every visible message into a list of (line_text, ts_str) pairs."""
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


# NOTE: _hw_reset() is NOT called here at import time. Importing this
# module used to poke EINK_RST/EINK_BUSY which is a side effect that
# breaks in two ways: (a) `import display_eink` from a diagnostic tool
# without a live panel raises GPIO warnings / leaves the pin configured
# for the rest of the process; (b) it happens before __init__ wants to,
# so if the caller never instantiates EInkDisplay we've still touched
# the hardware. Moved into EInkDisplay.__init__ below.
from waveshare_epd import epd2in13_V4 as epd_driver


def _draw_header(d, name, lora_on=False, wifi_on=False, silent=False):
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
        # Hardware reset first. Clears a stuck BUSY from a prior run /
        # crash / unclean systemd shutdown -- otherwise epd.init() hangs
        # waiting for the panel to ack. Moved here from module load so
        # `import display_eink` is side-effect-free (matters for tests
        # and introspection tools that don't need a live panel).
        _hw_reset()
        self.epd = epd_driver.EPD()
        self.epd.init()
        self.epd.Clear(0xFF)
        self.first_draw = True
        self.updates = 0
        # True while the panel is in controller-sleep (epd.sleep()) and
        # the next submitted frame must be preceded by epd.init() + full
        # refresh. Currently only set by the public sleep() method (the
        # shutdown path); _render() clears it when it wakes the panel.
        # Kept as a guard so a stray submission AFTER cleanup() but
        # BEFORE the process exits doesn't try to draw on a slept panel.
        self._asleep = False
        self._pending = None
        self._pending_force_full = False
        self._lock = threading.Lock()
        self._hw_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._worker, daemon=True, name="eink-worker")
        self._thread.start()
        # Log which body font we ended up with so journalctl shows
        # whether Terminus was found or we fell back. FreeTypeFont.path
        # is either a string (loaded from disk) or a BytesIO (bundled
        # default); handle both.
        fp = getattr(FONT, "path", None)
        if isinstance(fp, str):
            font_label = os.path.basename(fp)
        else:
            font_label = "<pillow bundled default>"
        print(f"E-Ink: {WIDTH}x{HEIGHT}, V4 (bg worker, full/{FULL_REFRESH_EVERY}) font={font_label}")

    def draw_chat(self, name, channel, messages, input_text,
                  lora_on=False, wifi_on=False, silent=False,
                  force_full=False):
        """Render the chat view. ``force_full=True`` requests a full
        panel refresh instead of partial -- used on wake from sleep to
        clear the "Zzz" ghost. All other times partial is fine."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        _draw_header(d, name, lora_on=lora_on, wifi_on=wifi_on, silent=silent)

        # Messages
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

        # Input line with tail-view (drop leading chars if input overflows).
        # Reserve pixel room for the cursor so it's always visible at the
        # tail of whatever we drew.
        d.line([(0, SEPARATOR_Y), (WIDTH, SEPARATOR_Y)], fill=0)
        cursor_w = _text_width(FONT, CURSOR)
        budget = usable_w - cursor_w
        prefix = "> "
        prefix_w = _text_width(FONT, prefix)
        text_budget = budget - prefix_w
        if _text_width(FONT, input_text) <= text_budget:
            visible_inp = input_text
            trim_marker = ""
        else:
            # Drop leading chars until the tail fits
            tail = input_text
            while tail and _text_width(FONT, tail) > text_budget - _text_width(FONT, "."):
                tail = tail[1:]
            visible_inp = tail
            trim_marker = "."
        d.text((3, INPUT_TOP), prefix, font=FONT, fill=0)
        x_after_prefix = 3 + prefix_w
        if trim_marker:
            d.text((x_after_prefix, INPUT_TOP), trim_marker, font=FONT, fill=0)
            x_after_prefix += _text_width(FONT, trim_marker)
        d.text((x_after_prefix, INPUT_TOP), visible_inp, font=FONT, fill=0)
        cursor_x = x_after_prefix + _text_width(FONT, visible_inp)
        d.text((cursor_x, INPUT_TOP), CURSOR, font=FONT, fill=0)

        self._submit(img, force_full=force_full)

    def draw_profile(self, name, channel, silent, selection, force_full=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "PROFILE", font=FONT_BD, fill=255)
        silent_label = f"Silent: {'ON' if silent else 'OFF'}"
        items = [
            f"Name: {name}",
            f"Channel: {channel}",
            silent_label,
            "Reboot device",
        ]
        y = 22
        row_h = 22
        for i, item in enumerate(items):
            if i == selection:
                d.rectangle([4, y - 3, WIDTH - 4, y + 15], fill=0)
                d.text((10, y), item, font=FONT, fill=255)
            else:
                d.text((10, y), item, font=FONT, fill=0)
            y += row_h
        self._submit(img, force_full=force_full)

    def draw_name_edit(self, name, force_full=False):
        """Name editor. Same cursor treatment as the chat input line --
        draw the buffer, then an underscore immediately after."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "EDIT NAME", font=FONT_BD, fill=255)
        d.text((10, 42), "Name:", font=FONT, fill=0)
        d.rectangle([10, 60, WIDTH - 10, 78], outline=0)
        d.text((14, 62), name, font=FONT, fill=0)
        cursor_x = 14 + _text_width(FONT, name)
        # Clamp so the cursor doesn't spill past the box
        max_x = WIDTH - 10 - _text_width(FONT, CURSOR) - 2
        if cursor_x > max_x:
            cursor_x = max_x
        d.text((cursor_x, 62), CURSOR, font=FONT, fill=0)
        self._submit(img, force_full=force_full)

    def draw_channel_edit(self, channel, force_full=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "EDIT CHANNEL", font=FONT_BD, fill=255)
        d.text((10, 30), "Channel (1-99):", font=FONT, fill=0)
        d.rectangle([10, 50, WIDTH - 10, 72], outline=0)
        d.text((18, 54), f"<   {channel}   >", font=FONT_BD, fill=0)
        d.text((10, 82), "UP/DOWN or L/R: change", font=FONT_SM, fill=0)
        d.text((10, 96), "ENTER: save", font=FONT_SM, fill=0)
        self._submit(img, force_full=force_full)

    def draw_reboot_confirm(self, name):
        """Numeric-choice confirmation before issuing a reboot from the
        profile menu. Forced full refresh so the previous menu frame
        doesn't ghost under the prompt (the selected row in the menu is
        a black-filled rectangle, which partial refresh can leave faint
        artifacts of). Numeric keys instead of ENTER/ESC: the kid can't
        trigger a reboot with a stray ENTER while scrolling the chat."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, HEADER_H - 1], fill=0)
        d.text((3, 1), "REBOOT DEVICE", font=FONT_BD, fill=255)
        # Visually distinct warning box so the kid doesn't confirm by reflex.
        d.rectangle([6, 24, WIDTH - 6, 60], outline=0)
        prompt = "Reboot now?"
        pw = _text_width(FONT_BD, prompt)
        d.text(((WIDTH - pw) // 2, 34), prompt, font=FONT_BD, fill=0)
        d.text((10, 70), "1  yes, reboot", font=FONT, fill=0)
        d.text((10, 88), "2  cancel", font=FONT, fill=0)
        nw = _text_width(FONT_SM, name)
        d.text((WIDTH - nw - 4, HEIGHT - 12), name, font=FONT_SM, fill=0)
        self._submit(img, force_full=True)

    def draw_rebooting(self, name):
        """Terminal frame shown after the user confirms. Display stays
        on this image until the kernel actually cuts power, so it needs
        to look intentional (not like a crash). FONT_MD (not FONT_BIG)
        because 'Rebooting...' at 36 px overflows the 250 px panel.
        Force-full refresh to make sure it's legible on first draw."""
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        msg = "Rebooting..."
        mw = _text_width(FONT_MD, msg)
        # max(0, ...) guards against a tiny pathological font that
        # somehow renders wider than the panel; the text gets left-
        # clipped instead of disappearing off-screen on the left.
        x = max(0, (WIDTH - mw) // 2)
        d.text((x, 40), msg, font=FONT_MD, fill=0)
        nw = _text_width(FONT_BD, name)
        d.text((max(0, (WIDTH - nw) // 2), 80), name, font=FONT_BD, fill=0)
        self._submit(img, force_full=True)

    def draw_sleep(self, name, silent=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        zzz = "Zzz"
        zw = _text_width(FONT_BIG, zzz)
        d.text(((WIDTH - zw) // 2, 6), zzz, font=FONT_BIG, fill=0)
        nw = _text_width(FONT_BD, name)
        d.text(((WIDTH - nw) // 2, 60), name, font=FONT_BD, fill=0)
        hint = "Press any key"
        hw = _text_width(FONT_SM, hint)
        d.text(((WIDTH - hw) // 2, 88), hint, font=FONT_SM, fill=0)
        if silent:
            d.text((WIDTH - 44, 104), "(muted)", font=FONT_SM, fill=0)
        self._submit(img, force_full=True)

    # ---------- worker plumbing ----------
    def _submit(self, img, force_full=False):
        with self._lock:
            self._pending = img
            if force_full:
                self._pending_force_full = True
        self._wake.set()

    def _worker(self):
        """Background render loop.

        Simple wait-drain pattern:
          * wait() blocks on self._wake (no timeout -- we don't need
            controller-level idle sleep, V4 auto-enters self-refresh
            after TurnOnDisplay and draws near-zero between frames).
          * on wake, drain ALL pending frames. Drain is UNCONDITIONAL
            (not gated on self._stop) so the last frame submitted
            before shutdown -- typically the "Rebooting..." screen --
            always reaches the panel even if cleanup() set _stop
            between wait() and here.
        """
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    img = self._pending
                    force_full = self._pending_force_full
                    self._pending = None
                    self._pending_force_full = False
                if img is None:
                    break
                try:
                    with self._hw_lock:
                        self._render(img, force_full=force_full)
                except Exception as e:
                    print(f"E-Ink worker error: {e}")

    def _render(self, img, force_full=False):
        """Render one image. Called under _hw_lock.

        Full-refresh triggers:
          * first_draw        -- very first frame after boot.
          * was_asleep        -- woken from public sleep() call (shutdown
                                 path or future low-power mode). Partial
                                 refresh on a just-woken panel is
                                 guaranteed broken, so we force full.
          * force_full        -- caller asked for one. Set by ui.py on:
                                 message send, message receive, state
                                 transitions, and the always-full screens
                                 (reboot_confirm, rebooting, sleep).
          * updates >= N-1    -- periodic LUT reset every
                                 FULL_REFRESH_EVERY partials as a
                                 safety net against ghost accumulation
                                 in cases with no other trigger (e.g.
                                 user typing a very long message
                                 without sending).

        Full-refresh implementation:
          We use displayPartBaseImage() when available (V4 driver does
          one full panel turn-on that writes to BOTH the current RAM
          bank (0x24) AND the previous-frame bank (0x26) used as the
          reference for subsequent partials). That's the same visual
          cost as display() -- a single TurnOnDisplay -- but seeds the
          base image as a side effect, which is what stops ghost
          accumulation.

          Previous version called display() + displayPartBaseImage()
          which produced TWO panel turn-ons per full = ~6 visible
          flashes and that's exactly what users complained about.
          Single call = ~3 flashes, same cleanliness.
        """
        was_asleep = self._asleep
        if was_asleep:
            # Panel was in controller-sleep; re-init wakes it and
            # reloads the full-refresh LUT.
            self.epd.init()
            self._asleep = False

        buf = self.epd.getbuffer(img)

        needs_full = (
            self.first_draw
            or force_full
            or was_asleep
            or (self.updates + 1) >= FULL_REFRESH_EVERY
        )

        if needs_full:
            # For a periodic full (not on wake, not first), re-init
            # before the refresh to reset the LUT state. Wake path
            # already inited above; first_draw path inited in __init__.
            if not was_asleep and not self.first_draw:
                self.epd.init()
            self._full_refresh(buf)
            self.first_draw = False
            self.updates = 0
            return

        self.updates += 1
        self.epd.displayPartial(buf)

    def _full_refresh(self, buf):
        """One full refresh that also seeds the partial-refresh base.
        V4: displayPartBaseImage is a single panel turn-on that writes
        to both RAM banks -- strictly better than calling display()
        then displayPartBaseImage() separately (which costs two panel
        cycles and produces ~6 visible flashes per "full")."""
        setter = getattr(self.epd, "displayPartBaseImage", None)
        if setter is not None:
            try:
                setter(buf)
                return
            except Exception as e:
                print(f"E-Ink displayPartBaseImage error: {e}; "
                      f"falling back to display()")
        # Non-V4 driver or displayPartBaseImage broke. Plain full
        # refresh still works; partials will ghost more than V4 with
        # proper base seeding but the visible content is correct.
        try:
            self.epd.display(buf)
        except Exception as e:
            print(f"E-Ink display fallback error: {e}")

    def clear(self):
        with self._lock:
            self._pending = None
            self._pending_force_full = False
        with self._hw_lock:
            if self._asleep:
                # Wake the panel before issuing display ops, otherwise
                # Clear() sends commands to a dead controller and hangs
                # on BUSY waiting for an ack that never comes.
                self.epd.init()
                self._asleep = False
            self.epd.init()
            self.epd.Clear(0xFF)
            self.first_draw = True
            self.updates = 0

    def cleanup(self):
        self._stop = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def sleep(self):
        """Public sleep for the shutdown path. Idempotent against the
        worker's own idle-sleep so main.py can always call it in
        `finally:` without worrying whether the worker beat us to it."""
        with self._hw_lock:
            if self._asleep:
                return
            try:
                self.epd.sleep()
                self._asleep = True
            except Exception as e:
                print(f"E-Ink sleep error: {e}")
