# KidPager v0.14

Four-in-one UX release targeting feedback from hands-on use.

Software-only, but you need one extra apt package to get the new font.
Add `fonts-terminus-otb` to the `apt install` line in `deploy.ps1`
before running — otherwise the code falls back to DejaVu and you keep
v0.13 rendering.

## What changed

### 1. Typing no longer drops characters

"hello" coming out as "hllo" was the most visible bug in v0.13, and it
turned out the whole keyboard path needed rethinking. Before v0.14 the
main asyncio loop read `/dev/input/event*` directly: one `os.read` per
tick, 20 events per tick max, and any time the loop did something
slow — a full E-Ink refresh (2 s), a `kb.reconnect()` which contained
a blocking `time.sleep(2)`, or a BT glitch — the kernel's evdev buffer
overflowed and events were silently dropped by the driver. Every
dropped event was a lost keystroke.

The fix is structural. Keyboard reads now happen on a background
daemon thread with its own `select.poll` — immune to main-loop
starvation. It bulk-reads up to 32 events per syscall and pushes them
into a 256-slot `collections.deque`. The main loop drains that deque
via the new `kb.poll()`, which is a pure O(1) deque pop, up to 128
keys per tick. Overflow (which should be very rare now) is counted
and logged so we can see if it happens in the field.

While rewriting the path I also fixed two related bugs:

- **Held keys appeared frozen.** The kernel generates `KEY_REPEAT`
  (value=2) events while a key is held down. The v0.13 decoder
  dropped them, so holding 'h' produced one character, not a stream.
  Now repeats are accepted, with a 50 ms per-keycode debounce to
  absorb genuine contact bounce.
- **Reconnect was a 2 s freeze.** `kb.reconnect()` had a blocking
  `time.sleep(2)`. If a BT glitch triggered that path, the main loop
  froze long enough for the evdev buffer to fill and overflow. Now
  reconnect spawns `bluetoothctl connect` on a worker thread and
  does a bounded 200 ms rescan — the main loop is back to polling
  before the deque can fill.

### 2. New font — Terminus

DejaVu Sans at 12 pt is a TrueType font with anti-aliasing. On a 1-bpp
E-Ink panel Pillow has to threshold every mid-grey pixel to solid black
or white. In practice that meant narrow letter pairs — the 'ov' in
"love", the 'rn' in "corner" — had pixels between them rounded dark,
making the letters visually touch.

Terminus is a bitmap font. Every glyph is hand-pixelled at the target
size for 1-bpp rendering. No anti-aliasing, no rounding, no touching.
Body text and the input line are now Terminus 14 px where the font is
installed; the owner name and header badges stay DejaVu Bold because
proportional spacing still looks better on variable-width labels.

The display driver logs on startup which font it actually loaded. If
`fonts-terminus-otb` wasn't installed the log will say `DejaVuSans.ttf`
and you'll know to fix the deploy step.

### 3. Cursor

The input line now ends with a static underscore — you can see exactly
where the next character will go. Tail-view trimming (when the input
is longer than fits) now reserves pixel room for the cursor, so it's
always visible at the right edge. Same treatment in the name editor.

It's static, not blinking. An animated cursor would require a partial
refresh every ~500 ms which is needless panel wear, and on E-Ink a
blink is visually jarring anyway.

Mid-line editing (LEFT/RIGHT inside the buffer) is still not
supported — those keys remain menu/scroll navigation. Backspace is
the edit-mid-line tool for now.

### 4. Emoji shortcuts

Type `:)` and it turns into 🙂. Fifteen common text-face mappings:

| Type | Get | Type | Get | Type | Get |
|---|---|---|---|---|---|
| `:)` | 🙂 | `:(` | 🙁 | `:D` | 😄 |
| `:P` | 😛 | `:O` | 😮 | `;)` | 😉 |
| `<3` | ❤️ | `:\|` | 😐 | `:*` | 😘 |
| `xD` | 😆 | `XD` | 😆 | `:'(` | 😢 |
| `^_^` | 😊 | `o_O` | 🤨 | `O_o` | 🤨 |

Replacement happens at the **trailing edge** as you type, so the
E-Ink view shows the final character as soon as you type the last
character of the sequence. The send-time path also does a
full-buffer sweep, so shortcuts you typed past without pausing still
get expanded before going on the air.

Emoji travel as plain UTF-8 inside the existing LoRa payload — no
protocol change, two pagers on v0.14 just work, v0.14 ↔ v0.13 still
works but the v0.13 pager won't have glyphs for the emoji so it
will render Pillow's default "tofu" boxes.

Backspace on an emoji with a Unicode variation selector (the heart
❤️ is U+2764 + VS16) deletes the full visible glyph in one press,
not the VS16 alone.

## Deploy

The only out-of-band step is the font package. Open `deploy.ps1`,
find step `[1/8]` (the `apt install` line), and add
`fonts-terminus-otb` to the package list. The package name is correct
for Bookworm; on older Raspberry Pi OS try `xfonts-terminus` instead.
If the package isn't found, deploy still succeeds and the code falls
back to DejaVu — no crash, just v0.13 rendering.

Then:

```powershell
.\deploy.ps1 -All -Restart
```

Verify on each pager with

```bash
ssh kidpager.local journalctl -u kidpager -n 20 --no-pager
```

The first line after boot should read roughly
`E-Ink: 250x122, V4 (bg worker) font=ter-u14n.otb`. If it says
`DejaVuSans.ttf` the font package didn't install.

## Known limitations

- Terminus is mono-width, so the chat area fits fewer characters per
  line than DejaVu Sans did. The word-wrap routine handles this
  automatically; no layout bugs but messages wrap earlier.
- 15 emoji shortcuts only. Adding more is a two-line change in
  `ui.EMOJI_SHORTCUTS`.
- No emoji font on the device — emoji render as Pillow's default
  "tofu" glyph on E-Ink. Future work: switch to GNU Unifont or a
  custom 12×12 sprite set to render smiles natively.
- Mid-line caret movement (LEFT/RIGHT inside the input buffer) is
  still not implemented.

## Version interop

- v0.14 ↔ v0.14: fully interoperable.
- v0.14 ↔ v0.13: messages work both ways; emoji sent from v0.14 show
  as tofu on v0.13.
