"""SX1262 LoRa driver for KidPager.

Command-based protocol (unlike SX1276 registers):
  1. Every SPI transaction waits for BUSY=LOW first
  2. Commands are opcode + params; reads return 1 garbage byte + status + data
  3. Waveshare Core1262-HF specifics:
     - TCXO on DIO3 (3.3V, 5 ms startup) via SetDIO3AsTcxoCtrl
     - External RF switch on DIO2 via SetDIO2AsRfSwitchCtrl
     - HP PA for +22 dBm: PaConfig(0x04, 0x07, 0x00, 0x01)
  4. Sync word: two bytes at regs 0x0740/0x0741 (Semtech's "private" mapping)

Preserves LoRaRadio API: init() / send() / send_ack() / receive() / cleanup()
so main.py does not change.
"""
import spidev
import RPi.GPIO as GPIO
import time
import hashlib
from pins import (SPI_BUS, LORA_RST, LORA_DIO1, LORA_BUSY,
                  LORA_FREQ, LORA_SF, LORA_BW, LORA_CR,
                  LORA_POWER, LORA_SYNC)

# ---------------------------------------------------------------------------
# SX1262 opcodes (datasheet section 13)
# ---------------------------------------------------------------------------
OP_SET_SLEEP          = 0x84
OP_SET_STANDBY        = 0x80
OP_SET_TX             = 0x83
OP_SET_RX             = 0x82
OP_CALIBRATE          = 0x89
OP_CALIBRATE_IMAGE    = 0x98
OP_SET_PACKET_TYPE    = 0x8A
OP_SET_RF_FREQ        = 0x86
OP_SET_PA_CONFIG      = 0x95
OP_SET_TX_PARAMS      = 0x8E
OP_SET_BUFFER_BASE    = 0x8F
OP_SET_MOD_PARAMS     = 0x8B
OP_SET_PACKET_PARAMS  = 0x8C
OP_SET_DIO_IRQ_PARAMS = 0x08
OP_SET_DIO2_RF_SW     = 0x9D
OP_SET_DIO3_TCXO      = 0x97
OP_GET_STATUS         = 0xC0
OP_GET_IRQ_STATUS     = 0x12
OP_CLEAR_IRQ          = 0x02
OP_GET_RX_BUF_STATUS  = 0x13
OP_WRITE_REG          = 0x0D
OP_READ_REG           = 0x1D
OP_WRITE_BUF          = 0x0E
OP_READ_BUF           = 0x1E

# Standby modes
STDBY_RC   = 0x00
STDBY_XOSC = 0x01

# Packet types
PKT_LORA = 0x01

# IRQ flags (16-bit, datasheet 13.1.11)
IRQ_TX_DONE    = 0x0001
IRQ_RX_DONE    = 0x0002
IRQ_HEADER_OK  = 0x0010
IRQ_HEADER_ERR = 0x0020
IRQ_CRC_ERR    = 0x0040
IRQ_TIMEOUT    = 0x0200

# Register addresses
REG_SYNC_WORD_MSB = 0x0740
REG_SYNC_WORD_LSB = 0x0741

# SX1262 BW encoding (datasheet 13.4.5) -- DIFFERENT from SX1276!
BW_MAP = {
    7.81:  0x00, 10.42: 0x08, 15.63: 0x01, 20.83: 0x09,
    31.25: 0x02, 41.67: 0x0A, 62.5:  0x03,
    125.0: 0x04, 250.0: 0x05, 500.0: 0x06,
}

# ---------------------------------------------------------------------------
# KidPager app-level packet format (unchanged from SX1276 version)
# ---------------------------------------------------------------------------
MAGIC    = b"KPG"
TYPE_MSG = 0x01
TYPE_ACK = 0x02

BUSY_TIMEOUT = 0.1  # seconds to wait for BUSY LOW before giving up


class LoRaRadio:
    def __init__(self, config):
        self.config = config
        self.spi = None
        self.msg_counter = 0

    # =======================================================================
    # Public API
    # =======================================================================
    def init(self):
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(LORA_RST,  GPIO.OUT)
            # Pull-down on DIO1: unwired pin reads LOW, receive() fast-path exits cheaply.
            # SX1262 actively drives DIO1 HIGH on IRQ, easily overcomes the ~50k pull-down.
            GPIO.setup(LORA_DIO1, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            # Pull-up on BUSY: unwired pin reads HIGH, _wait_busy() times out with clear
            # "BUSY stuck" error instead of random false-pass on floating noise.
            GPIO.setup(LORA_BUSY, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Hardware reset (datasheet 12.1: NRESET low >100us, then 3.5-30ms)
            GPIO.output(LORA_RST, GPIO.LOW)
            time.sleep(0.001)
            GPIO.output(LORA_RST, GPIO.HIGH)
            time.sleep(0.010)

            self.spi = spidev.SpiDev()
            self.spi.open(SPI_BUS, 1)  # CE1 -> LORA_CS (GPIO 7)
            self.spi.max_speed_hz = 2_000_000
            self.spi.mode = 0

            if not self._wait_busy():
                print("LoRa init: BUSY stuck HIGH after reset "
                      "(check LORA_BUSY wire on GPIO 23 / phys pin 16)")
                self._init_cleanup()
                return False

            # Sanity check: chip should be in STDBY_RC after reset
            status = self._get_status()
            chipmode = (status >> 4) & 0x07
            if chipmode not in (0x02, 0x03):
                print(f"LoRa init: unexpected status=0x{status:02X} chipmode=0x{chipmode}")
                self._init_cleanup()
                return False

            # ---- Initialization sequence ----
            # Order matters: TCXO setup must precede Calibrate (stable XTAL required)
            self._cmd(OP_SET_STANDBY,    [STDBY_RC])
            self._cmd(OP_SET_PACKET_TYPE,[PKT_LORA])

            # Waveshare Core1262-HF: TCXO at 3.3V, 5 ms startup (320 * 15.625us)
            self._cmd(OP_SET_DIO3_TCXO, [0x07, 0x00, 0x01, 0x40])

            # Waveshare Core1262-HF: DIO2 drives external RF switch
            self._cmd(OP_SET_DIO2_RF_SW, [0x01])

            # Calibrate all blocks (datasheet 13.1.13; mask 0x7F = everything)
            self._cmd(OP_CALIBRATE, [0x7F])
            time.sleep(0.025)  # calibration can take up to ~25 ms total
            if not self._wait_busy(timeout=0.1):
                print("LoRa init: BUSY stuck during calibration")
                self._init_cleanup()
                return False

            # RF frequency before CalibrateImage (image cal is band-specific)
            self._set_rf_freq(int(LORA_FREQ * 1e6))
            self._calibrate_image(LORA_FREQ)

            # HP PA configuration for +22 dBm (datasheet 13.1.14)
            self._cmd(OP_SET_PA_CONFIG, [0x04, 0x07, 0x00, 0x01])
            # TX power + 200 us ramp (ramp 0x04)
            self._cmd(OP_SET_TX_PARAMS, [LORA_POWER & 0xFF, 0x04])

            # TX and RX both use buffer offset 0
            self._cmd(OP_SET_BUFFER_BASE, [0x00, 0x00])

            # Modulation: SF, BW, CR, LDRO
            bw_code = BW_MAP.get(LORA_BW, 0x04)
            cr_code = (LORA_CR - 4) & 0x0F  # 5->0x01 (4/5), 6->0x02 (4/6) etc.
            symbol_ms = (1 << LORA_SF) / LORA_BW   # BW in kHz -> symbol time in ms
            ldro = 1 if symbol_ms > 16.38 else 0
            self._cmd(OP_SET_MOD_PARAMS, [LORA_SF, bw_code, cr_code, ldro])

            # Packet: preamble=8, variable header, max 255 payload, CRC on, std IQ
            self._set_packet_params(payload_len=255)

            # Private-network sync word (0x1424 = old SX1276 0x12 equivalent)
            self._cmd(OP_WRITE_REG, [
                (REG_SYNC_WORD_MSB >> 8) & 0xFF, REG_SYNC_WORD_MSB & 0xFF,
                (LORA_SYNC >> 8) & 0xFF, LORA_SYNC & 0xFF,
            ])

            # Route RX_DONE, TX_DONE, CRC_ERR, TIMEOUT to DIO1
            irq_mask = IRQ_TX_DONE | IRQ_RX_DONE | IRQ_CRC_ERR | IRQ_TIMEOUT
            self._cmd(OP_SET_DIO_IRQ_PARAMS, [
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,   # IRQ mask
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,   # DIO1 mask = same
                0x00, 0x00,                                 # DIO2 mask = none
                0x00, 0x00,                                 # DIO3 mask = none
            ])

            # Start continuous RX
            self._set_rx_continuous()

            print(f"LoRa OK: SX1262 {LORA_FREQ}MHz SF{LORA_SF} "
                  f"BW{LORA_BW}kHz CR4/{LORA_CR} +{LORA_POWER}dBm "
                  f"sync=0x{LORA_SYNC:04X}")
            return True

        except Exception as e:
            print(f"LoRa init failed: {e}")
            self._init_cleanup()
            return False

    def _init_cleanup(self):
        """Release SPI handle after a failed init() so retries / next boot start clean."""
        if self.spi:
            try:
                self.spi.close()
            except Exception:
                pass
            self.spi = None

    def send(self, sender, text, msg_id=None):
        # msg_id=None on first send -> generate; pass-through on retransmit so the
        # receiver can dedupe on msg_id (ui.py add_message).
        if msg_id is None:
            self.msg_counter += 1
            msg_id = hashlib.md5(
                f"{sender}{text}{time.time()}{self.msg_counter}".encode()
            ).hexdigest()[:8]
        nb = sender.encode("utf-8")[:16]
        tb = text.encode("utf-8")[:200]
        ib = msg_id.encode("ascii")
        ch = self.config.channel & 0xFF
        pkt = MAGIC + bytes([ch, TYPE_MSG, len(nb)]) + nb + bytes([len(ib)]) + ib + tb
        self._tx(pkt)
        self._set_rx_continuous()
        return msg_id

    def send_ack(self, msg_id):
        ch = self.config.channel & 0xFF
        ib = msg_id.encode("ascii") if isinstance(msg_id, str) else msg_id
        pkt = MAGIC + bytes([ch, TYPE_ACK]) + ib
        self._tx(pkt)
        self._set_rx_continuous()

    def receive(self):
        # Fast-path: skip SPI when DIO1 is LOW (no pending IRQ)
        if not GPIO.input(LORA_DIO1):
            return None
        irq = self._get_irq_status()
        if not (irq & IRQ_RX_DONE):
            # Some other IRQ (HEADER_ERR, stray TIMEOUT, ...) -- clear and ignore
            self._clear_irq(0xFFFF)
            return None
        crc_err = bool(irq & IRQ_CRC_ERR)
        self._clear_irq(0xFFFF)
        if crc_err:
            return None
        # Get payload length and start offset from RX buffer status
        r = self._cmd_read(OP_GET_RX_BUF_STATUS, 2)
        payload_len = r[0]
        rx_start    = r[1]
        raw = self._read_buffer(rx_start, payload_len)
        return self._parse(raw)

    def cleanup(self):
        if self.spi:
            try:
                # Drop the chip to STDBY_RC before releasing SPI. Skips
                # OP_SET_SLEEP because waking from the deeper sleep
                # requires a GPIO reset pulse and we may be mid-shutdown;
                # STDBY_RC is ~1 mA and safe to leave in any state.
                self._cmd(OP_SET_STANDBY, [STDBY_RC])
                self.spi.close()
            except Exception:
                pass
        # SCOPED cleanup: only the pins we configured in init(). Passing
        # no arg to GPIO.cleanup() would also reset the E-Ink pins
        # (EINK_RST, EINK_BUSY, EINK_DC) configured by display_eink.py,
        # which in main.py's shutdown sequence runs BEFORE the e-ink's
        # own cleanup -- blanking EINK_RST mid-shutdown used to cause
        # the panel to latch into an undefined state, leaving a partial
        # "Rebooting..." frame or streaked artifacts until the next
        # boot. Listing only LoRa pins here makes teardown order
        # insensitive.
        try:
            GPIO.cleanup([LORA_RST, LORA_DIO1, LORA_BUSY])
        except Exception:
            pass

    # =======================================================================
    # Packet parser (app layer, unchanged from SX1276 version)
    # =======================================================================
    def _parse(self, raw):
        try:
            if len(raw) < 5 or raw[:3] != MAGIC:
                return None
            ch = raw[3]
            if ch != (self.config.channel & 0xFF):
                return None
            pt = raw[4]
            if pt == TYPE_MSG:
                nl   = raw[5]
                name = raw[6:6+nl].decode("utf-8", errors="replace")
                p    = 6 + nl
                il   = raw[p]
                mid  = raw[p+1:p+1+il].decode("ascii", errors="replace")
                text = raw[p+1+il:].decode("utf-8", errors="replace")
                return ("msg", (name, text, mid))
            elif pt == TYPE_ACK:
                return ("ack", raw[5:].decode("ascii", errors="replace"))
        except Exception as e:
            print(f"Parse error: {e}")
        return None

    # =======================================================================
    # SPI transaction primitives
    # =======================================================================
    def _wait_busy(self, timeout=BUSY_TIMEOUT):
        """Block until BUSY goes LOW. Returns False on timeout."""
        t0 = time.time()
        while GPIO.input(LORA_BUSY):
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.0001)
        return True

    def _cmd(self, opcode, params=()):
        """Write opcode + params. No data returned."""
        if not self._wait_busy():
            raise RuntimeError(f"BUSY stuck before opcode 0x{opcode:02X}")
        self.spi.xfer2([opcode] + list(params))

    def _cmd_read(self, opcode, resp_len, params=()):
        """Write opcode + params, then read resp_len data bytes.

        SPI layout: [opcode][params...][NOP->status][NOP->data * resp_len]
        Returns data bytes only (status byte is stripped).
        """
        if not self._wait_busy():
            raise RuntimeError(f"BUSY stuck before read opcode 0x{opcode:02X}")
        nparams = len(params)
        buf = [opcode] + list(params) + [0x00] * (1 + resp_len)
        ret = self.spi.xfer2(buf)
        # ret[0..nparams]       = garbage while sending opcode and params
        # ret[1+nparams]        = status byte (discarded here)
        # ret[2+nparams..]      = data bytes
        return ret[2 + nparams : 2 + nparams + resp_len]

    def _get_status(self):
        """GetStatus is special: response is a single status byte, no separate status."""
        if not self._wait_busy():
            return 0
        r = self.spi.xfer2([OP_GET_STATUS, 0x00])
        return r[1]

    def _get_irq_status(self):
        r = self._cmd_read(OP_GET_IRQ_STATUS, 2)
        return (r[0] << 8) | r[1]

    def _clear_irq(self, mask):
        self._cmd(OP_CLEAR_IRQ, [(mask >> 8) & 0xFF, mask & 0xFF])

    # =======================================================================
    # Composite helpers
    # =======================================================================
    def _set_rf_freq(self, freq_hz):
        # rf = freq_hz * 2^25 / 32e6  (datasheet 13.4.1)
        rf = int(freq_hz * 33554432 / 32_000_000)
        self._cmd(OP_SET_RF_FREQ, [
            (rf >> 24) & 0xFF, (rf >> 16) & 0xFF,
            (rf >>  8) & 0xFF,  rf        & 0xFF,
        ])

    def _calibrate_image(self, freq_mhz):
        # Band-specific image calibration (datasheet 9.2.1)
        if   902 <= freq_mhz <= 928:
            args = [0xE1, 0xE9]
        elif 863 <= freq_mhz <= 870:
            args = [0xD7, 0xDB]   # our EU 868 MHz band
        elif 779 <= freq_mhz <= 787:
            args = [0xC1, 0xC5]
        elif 470 <= freq_mhz <= 510:
            args = [0x75, 0x81]
        elif 430 <= freq_mhz <= 440:
            args = [0x6B, 0x6F]
        else:
            args = [0xD7, 0xDB]
        self._cmd(OP_CALIBRATE_IMAGE, args)

    def _set_packet_params(self, payload_len=255, preamble=8, crc_on=1, invert_iq=0):
        self._cmd(OP_SET_PACKET_PARAMS, [
            (preamble >> 8) & 0xFF, preamble & 0xFF,
            0x00,                 # variable-length header (LoRa explicit mode)
            payload_len & 0xFF,
            crc_on & 0x01,
            invert_iq & 0x01,
        ])

    def _set_rx_continuous(self):
        # Timeout 0xFFFFFF = continuous RX (never times out)
        self._cmd(OP_SET_RX, [0xFF, 0xFF, 0xFF])

    def _set_tx(self, timeout_ms=5000):
        # Timeout in 15.625us steps
        t = int(timeout_ms * 1000 / 15.625)
        t &= 0xFFFFFF
        self._cmd(OP_SET_TX, [
            (t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF,
        ])

    def _write_buffer(self, offset, data):
        self._cmd(OP_WRITE_BUF, [offset] + list(data))

    def _read_buffer(self, offset, n):
        return bytes(self._cmd_read(OP_READ_BUF, n, params=[offset]))

    # =======================================================================
    # Transmit
    # =======================================================================
    def _tx(self, data):
        """Transmit a packet. Blocks until TX_DONE or timeout.

        Caller must set RX mode afterwards (send/send_ack do this).
        """
        try:
            self._cmd(OP_SET_STANDBY, [STDBY_RC])
            self._clear_irq(0xFFFF)
            # Update payload length for this packet
            self._set_packet_params(payload_len=len(data))
            self._write_buffer(0x00, data)
            self._set_tx(5000)   # 5 s hardware timeout

            t0 = time.time()
            while time.time() - t0 < 6:
                irq = self._get_irq_status()
                if irq & IRQ_TX_DONE:
                    self._clear_irq(0xFFFF)
                    return True
                if irq & IRQ_TIMEOUT:
                    self._clear_irq(0xFFFF)
                    return False
                time.sleep(0.005)
            # Software timeout fallback
            self._clear_irq(0xFFFF)
            return False
        except Exception as e:
            print(f"TX failed: {e}")
            return False
