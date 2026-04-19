"""Keyboard reader for M4 BT keyboard."""
import struct, glob, os, select, time, subprocess

EVENT_FORMAT="llHHi"; EVENT_SIZE=struct.calcsize(EVENT_FORMAT)
EV_KEY=0x01; KEY_PRESS=1; KEY_RELEASE=0
KEYCODE_MAP={2:'1',3:'2',4:'3',5:'4',6:'5',7:'6',8:'7',9:'8',10:'9',11:'0',
    16:'q',17:'w',18:'e',19:'r',20:'t',21:'y',22:'u',23:'i',24:'o',25:'p',
    30:'a',31:'s',32:'d',33:'f',34:'g',35:'h',36:'j',37:'k',38:'l',
    44:'z',45:'x',46:'c',47:'v',48:'b',49:'n',50:'m',
    12:'-',13:'=',26:'[',27:']',39:';',40:"'",41:'`',43:'\\',51:',',52:'.',53:'/',57:' '}
SHIFT_MAP={2:'!',3:'@',4:'#',5:'$',6:'%',7:'^',8:'&',9:'*',10:'(',11:')',
    12:'-',13:'+',26:'{',27:'}',39:':',40:'"',41:'~',43:'|',51:'<',52:'>',53:'?'}
KEY_ESC=1;KEY_BACKSPACE=14;KEY_TAB=15;KEY_ENTER=28
KEY_LSHIFT=42;KEY_RSHIFT=54;KEY_LCTRL=29;KEY_RCTRL=97
KEY_UP=103;KEY_DOWN=108;KEY_LEFT=105;KEY_RIGHT=106
KEY_LALT=56;KEY_RALT=100
SKIP_NAMES={"vc4-hdmi","vc4-hdmi HDMI Jack","fe205000.gpio"}

def _bt_try():
    try:
        subprocess.run(["bluetoothctl","connect","45:40:86:00:03:21"],capture_output=True,timeout=5)
    except Exception:
        pass

class KeyboardReader:
    def __init__(self):
        self.fd=None;self.path=None;self.shift=False;self.alt=False

    def find_m4(self):
        for attempt in range(6):
            for path in sorted(glob.glob("/dev/input/event*")):
                try:
                    np=f"/sys/class/input/{os.path.basename(path)}/device/name"
                    if not os.path.exists(np):
                        continue
                    with open(np) as f:
                        name=f.read().strip()
                    if name in SKIP_NAMES:
                        continue
                    up=name.upper()
                    if any(k in up for k in ["M4","KEYBOARD","KB","BT-KEY","HID"]):
                        self.fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
                        self.path=path
                        print(f"Keyboard: {name} at {path}")
                        return True
                except Exception:
                    continue
            if attempt<5:
                print(f"  Waiting for BT keyboard... ({attempt+1}/6)")
                _bt_try()
                time.sleep(5)
        print("No keyboard found")
        return False

    def read_key_sync(self):
        if self.fd is None:
            return None
        try:
            data=os.read(self.fd,EVENT_SIZE)
            if len(data)<EVENT_SIZE:
                return None
            _,_,etype,code,value=struct.unpack(EVENT_FORMAT,data)
            if etype!=EV_KEY:
                return None
            if code in (KEY_LSHIFT,KEY_RSHIFT):
                self.shift=(value!=KEY_RELEASE)
                return None
            if code in (KEY_LALT,KEY_RALT):
                self.alt=(value!=KEY_RELEASE)
                return None
            if value!=KEY_PRESS:
                return None
            if code==KEY_ENTER: return "ENTER"
            if code==KEY_BACKSPACE: return "BACKSPACE"
            if code==KEY_ESC: return "ESC"
            if code==KEY_TAB: return "TAB"
            if code==KEY_UP: return "UP"
            if code==KEY_DOWN: return "DOWN"
            if code==KEY_LEFT: return "LEFT"
            if code==KEY_RIGHT: return "RIGHT"
            if self.alt and code==24: return "UP"
            if self.alt and code==38: return "DOWN"
            if self.shift and code in SHIFT_MAP:
                return SHIFT_MAP[code]
            if code in KEYCODE_MAP:
                ch=KEYCODE_MAP[code]
                return ch.upper() if self.shift else ch
            return None
        except (BlockingIOError,OSError):
            return None

    def is_alive(self):
        if self.fd is None:
            return False
        try:
            if not os.path.exists(self.path):
                return False
            select.select([self.fd],[],[],0)
            return True
        except Exception:
            return False

    def reconnect(self):
        self.close()
        _bt_try()
        time.sleep(2)
        self.find_m4()

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd=None
            self.path=None
