"""OEPL radio protocol v2: constants, framing and message structs.

Mirrors oepl-radio-v2.h in upstream OpenEPaperLink. All integers little-endian, structs packed,
MACs in over-the-air byte order (reversed against the usual display order).
"""

import struct
from dataclasses import dataclass

PROTO_MAJOR = 2
PROTO_MINOR = 1

BAUD = 2_000_000
MAX_PAYLOAD = 256
CHUNK_MAX = 240
BLOCK_SIZE = 4096
ACK_TIMEOUT_MS = 30
RETRIES = 3
CHUNK_WINDOW = 4
HEARTBEAT_MS = 1000
SWITCH_DELAY_MS = 20
LINK_LOST_MS = 5000  # the radio falls back to ASCII after this long without frames

FLAG_RESPONSE = 0x01
FLAG_RETRANSMIT = 0x02

# host -> radio
HELLO = 0x01
HEARTBEAT = 0x02
SET_CONFIG = 0x03
SET_PENDING = 0x04
CANCEL_PENDING = 0x05
DATA_CHUNK = 0x06
DATA_DROP = 0x07
GET_STATS = 0x08
ALLOW_CLEAR = 0x09
ALLOW_ADD = 0x0A
ALLOW_REMOVE = 0x0B
# radio -> host, events
CHECKIN = 0x40
DATA_NEEDED = 0x41
BLOCK_SERVED = 0x42
XFER_COMPLETE = 0x43
XFER_TIMEOUT = 0x44
TAG_RETURN_DATA = 0x45
LOG = 0x46
# responses
ACK = 0x7E
NAK = 0x7F
HELLO_REPLY = 0x81
STATUS = 0x82

TYPE_NAMES = {globals()[n]: n for n in (
    "HELLO", "HEARTBEAT", "SET_CONFIG", "SET_PENDING", "CANCEL_PENDING", "DATA_CHUNK", "DATA_DROP", "GET_STATS",
    "ALLOW_CLEAR", "ALLOW_ADD", "ALLOW_REMOVE", "CHECKIN", "DATA_NEEDED", "BLOCK_SERVED", "XFER_COMPLETE",
    "XFER_TIMEOUT", "TAG_RETURN_DATA", "LOG", "ACK", "NAK", "HELLO_REPLY", "STATUS")}

EVENT_TYPES = range(CHECKIN, LOG + 1)

NAK_UNKNOWN_TYPE = 1
NAK_BAD_LENGTH = 2
NAK_INVALID_PARAM = 3
NAK_NO_SLOT = 4
NAK_NO_CACHE_SPACE = 5
NAK_BUSY = 6

CAP_FRAMING = 1 << 0
CAP_BLOCK_CACHE = 1 << 1
CAP_SUBGHZ = 1 << 2
CAP_TAG_RETURN_DATA = 1 << 3
CAP_LOG = 1 << 5
CAP_ALLOW_LIST = 1 << 6

CFG_UNCHANGED_U8 = 0xFF
CFG_UNCHANGED_POWER = 0x7F
CFG_LOG_FRAMES = 0x01
CFG_LOCK_SHIFT = 1
LOCK_OFF, LOCK_LOCKED, LOCK_LEARNING = 0, 1, 2

PENDING_DATA_FOLLOWS = 0x01

CHECKIN_FULL = 0
CHECKIN_SHORT = 1

ALLOW_MAX_MACS = CHUNK_MAX // 8


def type_name(t: int) -> str:
    return TYPE_NAMES.get(t, f"0x{t:02X}")


# --- MACs ---------------------------------------------------------------------------------------

def mac_str(mac: bytes) -> str:
    """Over-the-air MAC bytes to the display form the AP uses (most significant byte first)."""
    return mac[::-1].hex().upper()


def mac_bytes(s: str) -> bytes:
    """Display form (16 hex digits, separators allowed) to over-the-air bytes."""
    h = "".join(c for c in s if c in "0123456789abcdefABCDEF")
    if len(h) != 16:
        raise ValueError(f"MAC needs 16 hex digits: {s!r}")
    return bytes.fromhex(h)[::-1]


# --- framing ------------------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_encode(src: bytes) -> bytes:
    out = bytearray([0])
    code_pos, code = 0, 1
    for b in src:
        if b == 0:
            out[code_pos] = code
            code_pos, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_pos] = code
                code_pos, code = len(out), 1
                out.append(0)
    out[code_pos] = code
    return bytes(out)


def cobs_decode(src: bytes) -> bytes | None:
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        code = src[i]
        i += 1
        if code == 0 or i + code - 1 > n:
            return None
        out += src[i:i + code - 1]
        i += code - 1
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def build_frame(ftype: int, seq: int, flags: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    raw = bytes([ftype, seq & 0xFF, flags]) + payload
    raw += struct.pack("<H", crc16(raw))
    return cobs_encode(raw) + b"\x00"


@dataclass
class Frame:
    type: int
    seq: int
    flags: int
    payload: bytes

    @property
    def is_response(self) -> bool:
        return bool(self.flags & FLAG_RESPONSE)


def parse_frame(cobs: bytes) -> Frame | None:
    """One frame without its 0x00 delimiter; None if malformed or the CRC is wrong."""
    raw = cobs_decode(cobs)
    if raw is None or len(raw) < 5:
        return None
    if struct.unpack_from("<H", raw, len(raw) - 2)[0] != crc16(raw[:-2]):
        return None
    return Frame(raw[0], raw[1], raw[2], raw[3:-2])


class Deframer:
    """Splits a byte stream into frames at 0x00 delimiters."""

    def __init__(self):
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b:
                if len(self.buf) < 2 * MAX_PAYLOAD:
                    self.buf.append(b)
                continue
            if self.buf:
                f = parse_frame(bytes(self.buf))
                if f is None:
                    self.bad += 1
                else:
                    frames.append(f)
            self.buf.clear()
        return frames


# --- message structs ----------------------------------------------------------------------------

def _struct(name, fmt, fields):
    s = struct.Struct("<" + fmt)
    cls = type(name, (), {})

    def unpack(data: bytes):
        return dict(zip(fields, s.unpack_from(data)))

    def pack(**kw):
        return s.pack(*(kw[f] for f in fields))

    cls.size = s.size
    cls.unpack = staticmethod(unpack)
    cls.pack = staticmethod(pack)
    return cls


Hello = _struct("Hello", "BBH", ["protoMajor", "protoMinor", "wantedCaps"])
SetConfig = _struct("SetConfig", "BBbBB", ["channel", "subghzChannel", "txPowerDbm", "flags", "ledBrightness"])
SetPending = _struct("SetPending", "8sQIBBHHB",
                     ["mac", "dataVer", "dataSize", "dataType", "dataTypeArg", "nextCheckin", "ttlMin", "flags"])
DataChunkHeader = _struct("DataChunkHeader", "QII", ["dataVer", "offset", "totalSize"])
HelloReply = _struct("HelloReply", "BBHB8sHHHH",
                     ["protoMajor", "protoMinor", "fwVersion", "hwType", "mac", "caps", "maxPayload", "cacheKb",
                      "pendingSlots"])
Status = _struct("Status", "IBBbBHHHIIHHHHB",
                 ["uptimeMs", "channel", "subghzChannel", "txPowerDbm", "lastResetReason", "pending", "pendingIdle",
                  "cacheUsedKb", "rxFrames", "txFrames", "lateRequestsSkipped", "serialCrcErrors",
                  "serialOverflows", "retransmits", "eventsQueued"])
Checkin = _struct("Checkin", "8sIbBBBHBQB21s",
                  ["mac", "timestampUs", "rssi", "lqi", "radio", "channel", "pan", "kind", "answeredVer",
                   "answeredType", "adr"])
DataNeeded = _struct("DataNeeded", "8sQIIH", ["mac", "dataVer", "offset", "length", "pleaseWaitMs"])
BlockServed = _struct("BlockServed", "8sQBBBh", ["mac", "dataVer", "blockId", "partsRequested", "framesSent", "lateMs"])
XferEnd = _struct("XferEnd", "8sQ", ["mac", "dataVer"])
TagReturnDataHeader = _struct("TagReturnDataHeader", "8sIbB", ["mac", "timestampUs", "rssi", "lqi"])
AvailDataReq = _struct("AvailDataReq", "BBbbHBBBHBB8s",
                       ["checksum", "lastPacketLQI", "lastPacketRSSI", "temperature", "batteryMv", "hwType",
                        "wakeupReason", "capabilities", "tagSoftwareVersion", "currentChannel", "customMode",
                        "reserved"])

assert Hello.size == 4 and SetConfig.size == 5 and SetPending.size == 27 and DataChunkHeader.size == 16
assert HelloReply.size == 21 and Status.size == 31 and Checkin.size == 49 and DataNeeded.size == 26
assert BlockServed.size == 21 and XferEnd.size == 16 and AvailDataReq.size == 21


def decode_event(ftype: int, payload: bytes) -> dict:
    """An event payload as a dict (MACs in display form); raises struct.error if too short."""
    if ftype == CHECKIN:
        d = Checkin.unpack(payload)
        d["adr"] = AvailDataReq.unpack(d["adr"]) if d["kind"] == CHECKIN_FULL else None
    elif ftype == DATA_NEEDED:
        d = DataNeeded.unpack(payload)
    elif ftype == BLOCK_SERVED:
        d = BlockServed.unpack(payload)
    elif ftype in (XFER_COMPLETE, XFER_TIMEOUT):
        d = XferEnd.unpack(payload)
    elif ftype == TAG_RETURN_DATA:
        d = TagReturnDataHeader.unpack(payload)
        d["data"] = payload[TagReturnDataHeader.size:]
    elif ftype == LOG:
        return {"level": payload[0] if payload else 0, "text": payload[1:].decode("utf-8", "replace")}
    else:
        return {"raw": payload.hex()}
    if "mac" in d:
        d["mac"] = mac_str(d["mac"])
    return d
