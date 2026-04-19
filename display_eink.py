"""E-Ink display driver for Waveshare 2.13 V4 HAT."""
import sys, time
import RPi.GPIO as GPIO
sys.path.insert(0, "/home/pi")
from PIL import Image, ImageDraw, ImageFont
from pins import EINK_RST, EINK_BUSY

WIDTH = 250
HEIGHT = 122

try:
    FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    FONT_SM = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    FONT_BD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
except:
    FONT = ImageFont.load_default()
    FONT_SM = FONT
    FONT_BD = FONT


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
    # Wait for BUSY to go low (max 3 sec)
    for _ in range(300):
        if GPIO.input(EINK_BUSY) == 0:
            return True
        time.sleep(0.01)
    print("WARNING: E-Ink BUSY stuck after reset")
    return False


# Do hardware reset before importing driver
_hw_reset()
from waveshare_epd import epd2in13_V4 as epd_driver


class EInkDisplay:
    def __init__(self):
        self.epd = epd_driver.EPD()
        self.epd.init()
        self.epd.Clear(0xFF)
        self.first_draw = True
        self.updates = 0
        print(f"E-Ink: {WIDTH}x{HEIGHT}, V4")

    def draw_chat(self, name, channel, messages, input_text, lora_on=False):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, 15], fill=0)
        lora = "LoRa" if lora_on else "----"
        d.text((3, 1), name, font=FONT_BD, fill=255)
        d.text((WIDTH - 32, 2), lora, font=FONT_SM, fill=255)
        y = 18
        for msg in messages[-5:]:
            if msg.outgoing:
                line = f"[{msg.status}] {msg.sender}: {msg.text}"
            else:
                line = f"  {msg.sender}: {msg.text}"
            if len(line) > 32:
                line = line[:31] + ".."
            d.text((2, y), line, font=FONT, fill=0)
            y += 17
        d.line([(0, 105), (WIDTH, 105)], fill=0)
        inp = f"> {input_text}"
        if len(inp) > 32:
            inp = inp[-32:]
        d.text((3, 107), inp, font=FONT, fill=0)
        self._update(img)

    def draw_profile(self, name, channel, selection):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, 15], fill=0)
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
        self._update(img)

    def draw_name_edit(self, name):
        img = Image.new("1", (WIDTH, HEIGHT), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, 15], fill=0)
        d.text((3, 1), "EDIT NAME", font=FONT_BD, fill=255)
        d.text((10, 42), "Name:", font=FONT, fill=0)
        d.rectangle([10, 60, WIDTH - 10, 78], outline=0)
        d.text((14, 62), name, font=FONT, fill=0)
        self._update(img)

    def _update(self, img):
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
        self.epd.init()
        self.epd.Clear(0xFF)
        self.first_draw = True

    def sleep(self):
        self.epd.sleep()
