"""SX1276 LoRa driver."""
import spidev, RPi.GPIO as GPIO, time, hashlib
from pins import *
REG_FIFO=0x00;REG_OP_MODE=0x01;REG_FR_MSB=0x06;REG_FR_MID=0x07
REG_FR_LSB=0x08;REG_PA_CONFIG=0x09;REG_FIFO_ADDR_PTR=0x0D
REG_FIFO_TX_BASE=0x0E;REG_FIFO_RX_BASE=0x0F;REG_FIFO_RX_CURR=0x10
REG_IRQ_FLAGS=0x12;REG_RX_NB_BYTES=0x13;REG_PKT_SNR=0x19
REG_PKT_RSSI=0x1A;REG_MODEM_CONFIG1=0x1D;REG_MODEM_CONFIG2=0x1E
REG_PREAMBLE_MSB=0x20;REG_PREAMBLE_LSB=0x21;REG_PAYLOAD_LEN=0x22
REG_MODEM_CONFIG3=0x26;REG_SYNC_WORD=0x39;REG_DIO_MAPPING1=0x40
REG_VERSION=0x42;REG_PA_DAC=0x4D
MODE_SLEEP=0x00;MODE_STDBY=0x01;MODE_TX=0x03;MODE_RX_CONT=0x05
MODE_LORA=0x80;IRQ_TX_DONE=0x08;IRQ_RX_DONE=0x40;IRQ_CRC_ERROR=0x20
BW_MAP={7.8:0,10.4:1,15.6:2,20.8:3,31.25:4,41.7:5,62.5:6,125.0:7,250.0:8,500.0:9}
MAGIC=b"KPG";TYPE_MSG=0x01;TYPE_ACK=0x02
class LoRaRadio:
    def __init__(self,config):
        self.config=config;self.spi=None;self.msg_counter=0
    def init(self):
        try:
            GPIO.setwarnings(False);GPIO.setmode(GPIO.BCM)
            GPIO.setup(LORA_RST,GPIO.OUT);GPIO.setup(LORA_DIO0,GPIO.IN)
            GPIO.output(LORA_RST,GPIO.LOW);time.sleep(0.01)
            GPIO.output(LORA_RST,GPIO.HIGH);time.sleep(0.05)
            self.spi=spidev.SpiDev();self.spi.open(SPI_BUS,1)
            self.spi.max_speed_hz=2000000;self.spi.mode=0
            ver=self._read(REG_VERSION)
            if ver!=0x12:print(f"SX1276 not found (0x{ver:02X})");return False
            self._write(REG_OP_MODE,MODE_SLEEP|MODE_LORA);time.sleep(0.01)
            frf=int((LORA_FREQ*1e6)/(32e6/2**19))
            self._write(REG_FR_MSB,(frf>>16)&0xFF)
            self._write(REG_FR_MID,(frf>>8)&0xFF)
            self._write(REG_FR_LSB,frf&0xFF)
            self._write(REG_PA_CONFIG,0x80|(LORA_POWER-2))
            if LORA_POWER>=17:self._write(REG_PA_DAC,0x87)
            bw=BW_MAP.get(LORA_BW,7);cr=LORA_CR-4
            self._write(REG_MODEM_CONFIG1,(bw<<4)|(cr<<1))
            self._write(REG_MODEM_CONFIG2,(LORA_SF<<4)|0x04)
            self._write(REG_MODEM_CONFIG3,0x04 if LORA_SF>=10 else 0x00)
            self._write(REG_PREAMBLE_MSB,0x00);self._write(REG_PREAMBLE_LSB,0x08)
            self._write(REG_SYNC_WORD,LORA_SYNC)
            self._write(REG_FIFO_TX_BASE,0x00);self._write(REG_FIFO_RX_BASE,0x00)
            self._write(REG_DIO_MAPPING1,0x00)
            self._write(REG_OP_MODE,MODE_STDBY|MODE_LORA)
            self._start_rx()
            print(f"LoRa OK: {LORA_FREQ}MHz SF{LORA_SF} BW{LORA_BW}kHz")
            return True
        except Exception as e:print(f"LoRa init failed: {e}");return False
    def send(self,sender,text):
        self.msg_counter+=1
        msg_id=hashlib.md5(f"{sender}{text}{time.time()}{self.msg_counter}".encode()).hexdigest()[:8]
        nb=sender.encode("utf-8")[:16];tb=text.encode("utf-8")[:200];ib=msg_id.encode("ascii")
        ch=self.config.channel&0xFF
        pkt=MAGIC+bytes([ch,TYPE_MSG,len(nb)])+nb+bytes([len(ib)])+ib+tb
        self._tx(pkt);self._start_rx();return msg_id
    def send_ack(self,msg_id):
        ch=self.config.channel&0xFF
        ib=msg_id.encode("ascii") if isinstance(msg_id,str) else msg_id
        pkt=MAGIC+bytes([ch,TYPE_ACK])+ib;self._tx(pkt);self._start_rx()
    def receive(self):
        irq=self._read(REG_IRQ_FLAGS)
        if not(irq&IRQ_RX_DONE):return None
        crc=bool(irq&IRQ_CRC_ERROR);self._write(REG_IRQ_FLAGS,0xFF)
        if crc:self._start_rx();return None
        n=self._read(REG_RX_NB_BYTES)
        self._write(REG_FIFO_ADDR_PTR,self._read(REG_FIFO_RX_CURR))
        raw=bytes([self._read(REG_FIFO) for _ in range(n)])
        self._start_rx();return self._parse(raw)
    def _parse(self,raw):
        try:
            if len(raw)<5 or raw[:3]!=MAGIC:return None
            ch=raw[3]
            if ch!=(self.config.channel&0xFF):return None
            pt=raw[4]
            if pt==TYPE_MSG:
                nl=raw[5];name=raw[6:6+nl].decode("utf-8",errors="replace")
                p=6+nl;il=raw[p];mid=raw[p+1:p+1+il].decode("ascii",errors="replace")
                text=raw[p+1+il:].decode("utf-8",errors="replace")
                return("msg",(name,text,mid))
            elif pt==TYPE_ACK:return("ack",raw[5:].decode("ascii",errors="replace"))
        except Exception as e:print(f"Parse error: {e}")
        return None
    def cleanup(self):
        if self.spi:self._write(REG_OP_MODE,MODE_SLEEP|MODE_LORA);self.spi.close()
        GPIO.cleanup()
    def _tx(self,data):
        self._write(REG_OP_MODE,MODE_STDBY|MODE_LORA)
        self._write(REG_FIFO_ADDR_PTR,0x00)
        for b in data:self._write(REG_FIFO,b)
        self._write(REG_PAYLOAD_LEN,len(data));self._write(REG_IRQ_FLAGS,0xFF)
        self._write(REG_OP_MODE,MODE_TX|MODE_LORA)
        s=time.time()
        while time.time()-s<5:
            if self._read(REG_IRQ_FLAGS)&IRQ_TX_DONE:self._write(REG_IRQ_FLAGS,IRQ_TX_DONE);return True
            time.sleep(0.01)
        return False
    def _start_rx(self):
        self._write(REG_OP_MODE,MODE_STDBY|MODE_LORA)
        self._write(REG_FIFO_ADDR_PTR,0x00);self._write(REG_IRQ_FLAGS,0xFF)
        self._write(REG_OP_MODE,MODE_RX_CONT|MODE_LORA)
    def _read(self,a):return self.spi.xfer2([a&0x7F,0x00])[1]
    def _write(self,a,v):self.spi.xfer2([a|0x80,v])
