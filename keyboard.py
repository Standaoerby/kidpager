"""Keyboard reader for M4 BT keyboard.

Architecture
------------
The reader runs a background *daemon thread* that drains the evdev fd
into a ``collections.deque`` as fast as the kernel delivers events. The
main asyncio loop pops from the deque via ``poll()`` -- non-blocking,
O(1), never waits on I/O.

Why a thread and not an asyncio reader:
  * evdev is read via ``os.read`` on a non-blocking fd. With ``select``
    we could do it in asyncio, but the main loop would still starve the
    reader whenever a full E-Ink refresh (~2 s) ran inline. A dedicated
    thread with its own ``select.poll`` is immune to that.
  * Python's GIL doesn't matter here: the thread is almost always
    blocked in ``poll`` or ``os.read``. When the main loop does CPU
    work the reader wakes on the next kernel event regardless.

Bulk reads
----------
A single ``os.read(fd, EVENT_SIZE * 32)`` returns up to 32 struct
input_events at once (evdev batches aggressively). The previous
one-event-per-syscall path was fine for human typing speeds but broke
down during bursts (auto-repeat, paste over SSH, fast typists on
reconnect).

Auto-repeat handling
--------------------
Kernel generates ``value=1`` (KEY_PRESS) followed by periodic
``value=2`` (KEY_REPEAT) while a key is held. Before v0.14 we
**dropped** all repeats, so holding 'h' after the initial press
appeared frozen until release. Now repeats are accepted -- with a
50 ms per-key debounce so a genuine single keystroke that the kernel
bounces as press+repeat doesn't produce "hh".

Sleep/reconnect
---------------
``is_alive()`` and ``reconnect()`` never touch ``time.sleep`` on the
main loop's thread -- the old 2 s sleep blocked keyboard draining just
as badly as the original bug. Reconnect logic now spawns a transient
worker thread that attempts ``bluetoothctl connect`` without blocking
the caller; the main loop keeps polling while it runs.
"""
import struct, glob, os, select, time, subprocess, threading
from collections import deque

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01
KEY_RELEASE = 0
KEY_PRESS = 1
KEY_REPEAT = 2

KEYCODE_MAP = {
    2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
    16: 'q', 17: 'w', 18: 'e', 19: 'r', 20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p',
    30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l',
    44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n', 50: 'm',
    12: '-', 13: '=', 26: '[', 27: ']', 39: ';', 40: "'", 41: '`',
    43: '\\', 51: ',', 52: '.', 53: '/', 57: ' ',
}
SHIFT_MAP = {
    2: '!', 3: '@', 4: '#', 5: '$', 6: '%', 7: '^', 8: '&', 9: '*', 10: '(', 11: ')',
    12: '-', 13: '+', 26: '{', 27: '}', 39: ':', 40: '"', 41: '~',
    43: '|', 51: '<', 52: '>', 53: '?',
}

KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LSHIFT = 42
KEY_RSHIFT = 54
KEY_LCTRL = 29
KEY_RCTRL = 97
KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_LALT = 56
KEY_RALT = 100

SKIP_NAMES = {"vc4-hdmi", "vc4-hdmi HDMI Jack", "fe205000.gpio"}
KB_NAME_HINTS = ("M4", "KEYBOARD", "KB", "BT-KEY", "HID")

# Debounce window for auto-repeat + physical contact bounce. Key events
# arriving within this many seconds of the previous event for the SAME
# keycode are dropped. 50 ms is fast enough that deliberate fast typing
# (~15 keys/sec, 66 ms between keys) passes through.
REPEAT_DEBOUNCE_S = 0.050

# Ring buffer size. 256 events = 128 key presses worst case. Overflow
# is logged but never blocks the producer -- dropping stale input is
# strictly better than dropping fresh input, and the user will notice
# 100+ queued keys faster than we can react anyway.
QUEUE_MAX = 256


def _paired_keyboards():
    """Return MAC addresses of known BT devices whose names look like keyboards."""
    try:
        r = subprocess.run(["bluetoothctl", "devices"],
                           capture_output=True, text=True, timeout=5)
        macs = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[0] == "Device":
                mac = parts[1]
                name = parts[2] if len(parts) > 2 else ""
                up = name.upper()
                if any(k in up for k in KB_NAME_HINTS):
                    macs.append(mac)
        return macs
    except Exception:
        return []


def _bt_connect_async():
    """Attempt bluetoothctl connect for every paired keyboard in a
    transient thread. Safe to spam -- if a connect is already in
    flight, bluetoothctl returns fast."""
    def _worker():
        for mac in _paired_keyboards():
            try:
                subprocess.run(["bluetoothctl", "connect", mac],
                               capture_output=True, timeout=5)
            except Exception:
                pass
    t = threading.Thread(target=_worker, daemon=True, name="kb-reconnect")
    t.start()


class KeyboardReader:
    def __init__(self):
        self.fd = None
        self.path = None
        self.shift = False
        self.alt = False
        # Shared state between producer (reader thread) and consumer
        # (main asyncio loop). deque operations are GIL-atomic, so we
        # don't need an explicit lock for append/popleft at this size.
        self._queue = deque(maxlen=QUEUE_MAX)
        self._last_keycode = 0
        self._last_keytime = 0.0
        self._dropped = 0
        self._reader_thread = None
        self._reader_stop = False

    # ---------- device discovery ----------
    def find_m4(self, attempts=6, delay=5.0, verbose=True):
        """Scan /dev/input for a keyboard matching KB_NAME_HINTS.

        attempts=6, delay=5.0: default for cold-boot where BT is still
        associating. Runtime reconnect uses attempts=1 to avoid
        stalling the caller."""
        for attempt in range(attempts):
            for path in sorted(glob.glob("/dev/input/event*")):
                try:
                    np = f"/sys/class/input/{os.path.basename(path)}/device/name"
                    if not os.path.exists(np):
                        continue
                    with open(np) as f:
                        name = f.read().strip()
                    if name in SKIP_NAMES:
                        continue
                    up = name.upper()
                    if any(k in up for k in KB_NAME_HINTS):
                        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                        self.path = path
                        if verbose:
                            print(f"Keyboard: {name} at {path}")
                        self._start_reader()
                        return True
                except Exception:
                    continue
            if attempt < attempts - 1:
                if verbose:
                    print(f"  Waiting for BT keyboard... ({attempt + 1}/{attempts})")
                _bt_connect_async()
                time.sleep(delay)
        if verbose:
            print("No keyboard found")
        return False

    # ---------- background reader ----------
    def _start_reader(self):
        """Launch the reader thread. Safe to call repeatedly -- stops
        the previous one first."""
        self._stop_reader()
        self._reader_stop = False
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name="kb-reader",
        )
        self._reader_thread.start()

    def _stop_reader(self):
        """Signal the reader to exit and wait briefly for it."""
        self._reader_stop = True
        t = self._reader_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._reader_thread = None

    def _reader_loop(self):
        """Drain evdev into self._queue until stopped or fd dies.

        Uses select.poll with a 50 ms timeout so we can check
        _reader_stop periodically. Bulk-reads up to 32 events per
        syscall."""
        poller = select.poll()
        try:
            poller.register(self.fd, select.POLLIN)
        except Exception as e:
            print(f"kb reader: poll register failed: {e}")
            return
        bulk_size = EVENT_SIZE * 32
        while not self._reader_stop:
            try:
                ready = poller.poll(50)  # 50 ms tick
                if not ready:
                    continue
                try:
                    data = os.read(self.fd, bulk_size)
                except BlockingIOError:
                    continue
                except OSError as e:
                    # fd died (keyboard unpaired / USB yank / sleep)
                    print(f"kb reader: os.read failed: {e}")
                    break
                if not data:
                    # EOF; fd closed from the other side
                    break
                # Parse all events in the buffer. os.read returned a
                # multiple of EVENT_SIZE or we drop the tail.
                offset = 0
                end = len(data) - EVENT_SIZE + 1
                while offset < end:
                    _, _, etype, code, value = struct.unpack(
                        EVENT_FORMAT, data[offset:offset + EVENT_SIZE]
                    )
                    offset += EVENT_SIZE
                    self._handle_raw(etype, code, value)
            except Exception as e:
                print(f"kb reader loop error: {e}")
                break
        # Clean exit
        try:
            poller.unregister(self.fd)
        except Exception:
            pass

    def _handle_raw(self, etype, code, value):
        """Convert one raw evdev event into a key token and push it
        onto the consumer queue. Called from the reader thread only."""
        if etype != EV_KEY:
            return

        # Modifier tracking works for both press and release
        if code in (KEY_LSHIFT, KEY_RSHIFT):
            self.shift = (value != KEY_RELEASE)
            return
        if code in (KEY_LALT, KEY_RALT):
            self.alt = (value != KEY_RELEASE)
            return

        # Accept both press (1) and repeat (2); reject release (0)
        if value == KEY_RELEASE:
            return

        # Per-key debounce. Drop events of the SAME code that come within
        # REPEAT_DEBOUNCE_S of the previous one. Different-code events
        # are never debounced (typing "hj" fast = two keys, not one).
        now = time.monotonic()
        if code == self._last_keycode and (now - self._last_keytime) < REPEAT_DEBOUNCE_S:
            return
        self._last_keycode = code
        self._last_keytime = now

        # Decode
        token = self._decode(code)
        if token is None:
            return

        # Push. deque with maxlen drops from the opposite end on
        # overflow; since we append right, overflow drops from left
        # (oldest). Count drops for diagnostics.
        if len(self._queue) >= QUEUE_MAX:
            self._dropped += 1
        self._queue.append(token)

    def _decode(self, code):
        """Map a keycode (given current shift/alt state) to a token."""
        if code == KEY_ENTER: return "ENTER"
        if code == KEY_BACKSPACE: return "BACKSPACE"
        if code == KEY_ESC: return "ESC"
        if code == KEY_TAB: return "TAB"
        if code == KEY_UP: return "UP"
        if code == KEY_DOWN: return "DOWN"
        if code == KEY_LEFT: return "LEFT"
        if code == KEY_RIGHT: return "RIGHT"
        # Alt+O / Alt+L -- up/down scroll aliases (M4 has no arrow keys
        # accessible without Fn). Alt+W toggles Wi-Fi.
        if self.alt and code == 24: return "UP"
        if self.alt and code == 38: return "DOWN"
        if self.alt and code == 17: return "WIFI"
        if self.shift and code in SHIFT_MAP:
            return SHIFT_MAP[code]
        if code in KEYCODE_MAP:
            ch = KEYCODE_MAP[code]
            return ch.upper() if self.shift else ch
        return None

    # ---------- consumer-side API ----------
    def poll(self):
        """Pop one token from the queue, or None if empty. Non-blocking,
        safe to call from the main asyncio loop. Call in a tight
        while-not-None to drain the whole burst in one tick."""
        try:
            return self._queue.popleft()
        except IndexError:
            return None

    def queue_depth(self):
        """How many tokens are waiting. Useful for diagnostics /
        telling the UI to batch redraws instead of redrawing per key."""
        return len(self._queue)

    def dropped(self):
        """How many tokens overflowed since startup. Non-zero = the
        main loop starved the consumer at some point."""
        return self._dropped

    # Legacy single-read alias so any caller still using the old name
    # keeps working. New code should prefer poll().
    read_key_sync = poll

    # ---------- health / reconnect ----------
    def is_alive(self):
        """Cheap liveness check: does the event fd still exist? If the
        keyboard disconnects, /dev/input/eventN vanishes and this
        returns False. Does NOT poll the fd -- that's what the reader
        thread does, and we don't want to race it."""
        if self.fd is None:
            return False
        if self.path is None:
            return False
        try:
            return os.path.exists(self.path)
        except Exception:
            return False

    def reconnect(self):
        """Best-effort reconnect. Never blocks the caller longer than
        ~200 ms: spawns the BT connect attempt on a worker thread and
        does a quick device scan.

        If the evdev node is already gone we rescan briefly. If it
        reappears within a short window we reattach; otherwise we
        return and the caller tries again on the next tick."""
        # Tear down old fd + reader
        self.close()
        _bt_connect_async()
        # Brief non-blocking scan. 200 ms total, 5 x 40 ms, so the
        # main loop only pauses for 200 ms in the worst case.
        for _ in range(5):
            for path in sorted(glob.glob("/dev/input/event*")):
                try:
                    np = f"/sys/class/input/{os.path.basename(path)}/device/name"
                    if not os.path.exists(np):
                        continue
                    with open(np) as f:
                        name = f.read().strip()
                    if name in SKIP_NAMES:
                        continue
                    up = name.upper()
                    if any(k in up for k in KB_NAME_HINTS):
                        try:
                            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                            self.path = path
                            self._start_reader()
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
            time.sleep(0.040)
        return False

    def close(self):
        self._stop_reader()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
            self.path = None
