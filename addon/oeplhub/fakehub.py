"""A simulated hub: the radio side of protocol v2 over TCP, with fake tags. For testing without hardware.

It behaves like the C6 radio firmware as far as the host can see: ASCII startup (RDY?, NFO?, FRM1),
then frames; request dedup by seq; events one at a time with ACK timeout and retries; fallback to
ASCII after 5 s without frames; a block cache filled by DATA_CHUNKs; DATA_NEEDED for missing blocks.
"""

import logging
import random
import socket
import struct
import threading
import time

from . import v2proto as v2

log = logging.getLogger("fakehub")


class FakeTag:
    def __init__(self, mac: str, hw_type=0x94, interval=10.0, fw=0x0030):
        self.mac = v2.mac_bytes(mac)
        self.hw_type = hw_type
        self.interval = interval
        self.fw = fw
        self.next_checkin = time.monotonic() + random.uniform(0.5, 2.0)
        self.boot = True
        self.battery = 2900


class FakeRadio:
    def __init__(self, tags, channel=11, loss=0.0, jitter_ms=0, cache_kb=128, slots=64):
        self.tags = tags
        self.channel = channel
        self.power = 10
        self.loss = loss
        self.jitter = jitter_ms / 1000
        self.cache_kb = cache_kb
        self.slots = slots
        self.mac = bytes.fromhex("0123456789ABCDEF")[::-1]
        self.start = time.monotonic()
        self.pending = {}  # mac -> SetPending dict
        self.cache = {}    # (dataVer, blockId) -> bytearray
        self.cache_size = {}  # dataVer -> totalSize
        self.counters = {"rx": 0, "tx": 0, "crc": 0, "retransmits": 0}
        self.filled = {}  # (dataVer, blockId) -> byte offsets received
        self.lock = threading.RLock()
        self.conn = None
        self.reset_link()

    # --- link state ------------------------------------------------------------------------------

    def reset_link(self):
        self.v2 = False
        self.ascii = b""
        self.last_rx_seq = -1
        self.last_reply = b""
        self.events = []  # [(type, payload)]
        self.ev_out = False
        self.ev_sent = 0.0
        self.ev_tries = 0
        self.link_down = False
        self.tx_seq = 0
        self.last_frame = time.monotonic()

    def send(self, data: bytes, droppable=True):
        if self.conn is None:
            return
        if droppable and self.loss and random.random() < self.loss:
            return
        if self.jitter:
            time.sleep(random.uniform(0, self.jitter))
        try:
            self.conn.sendall(data)
        except OSError:
            pass

    def pr(self, text: str):
        self.send(text.encode(), droppable=False)

    # --- ASCII -----------------------------------------------------------------------------------

    def rx_ascii(self, data: bytes):
        for b in data:
            self.ascii = (self.ascii + bytes([b]))[-4:]
            if self.ascii == b"NFO?":
                self.pr("ACK>")
                self.pr("TYP>C6")
                self.pr("VER>0042")
                m = self.mac
                for i in range(0, 8, 2):
                    self.pr(("MAC>" if i == 0 else "") + f"{m[i]:02X}{m[i + 1]:02X}")
                self.pr(f"ZCH>{self.channel:02X}")
                self.pr(f"ZPW>{self.power:02X}")
                self.pr(f"PEN>{len(self.pending):02X}")
                self.pr("NOP>00")
                self.pr(f"CAP>{self.caps():04X}")
            elif self.ascii == b"RDY?":
                self.pr("ACK>")
            elif self.ascii == b"FRM1":
                self.pr("ACK>")
                self.ascii = b""
                self.v2 = True
                self.last_rx_seq = -1
                self.last_reply = b""
                self.ev_out = False
                self.link_down = False
                self.tx_seq = 0
                self.last_frame = time.monotonic()
                self.deframer = v2.Deframer()
                log.info("protocol v2")
                return

    @staticmethod
    def caps():
        return v2.CAP_FRAMING | v2.CAP_BLOCK_CACHE | v2.CAP_TAG_RETURN_DATA | v2.CAP_ALLOW_LIST

    # --- v2 requests -----------------------------------------------------------------------------

    def rx_v2(self, data: bytes):
        for f in self.deframer.feed(data):
            if self.loss and random.random() < self.loss:
                continue
            self.counters["rx"] += 1
            self.handle_frame(f)
        self.counters["crc"] = self.deframer.bad

    def handle_frame(self, f: v2.Frame):
        self.last_frame = time.monotonic()
        if self.link_down:
            self.link_down = False
            self.ev_tries = 0
            self.ev_out = False
        if f.is_response:
            if self.ev_out and f.seq == self.tx_seq and f.type in (v2.ACK, v2.NAK):
                self.events.pop(0)
                self.ev_out = False
                self.ev_tries = 0
                self.tx_seq = (self.tx_seq + 1) & 0xFF
            return
        if f.type != v2.HELLO and f.seq == self.last_rx_seq:
            if self.last_reply:
                self.send(self.last_reply)
            return
        rtype, rpayload = self.handle_request(f.type, f.payload)
        self.last_rx_seq = f.seq
        self.last_reply = v2.build_frame(rtype, f.seq, v2.FLAG_RESPONSE, rpayload)
        self.send(self.last_reply)

    def handle_request(self, t, p):
        nak = lambda code: (v2.NAK, bytes([code]))
        if t == v2.HELLO:
            if len(p) < v2.Hello.size:
                return nak(v2.NAK_BAD_LENGTH)
            if v2.Hello.unpack(p)["protoMajor"] != v2.PROTO_MAJOR:
                return nak(v2.NAK_INVALID_PARAM)
            return v2.HELLO_REPLY, v2.HelloReply.pack(
                protoMajor=v2.PROTO_MAJOR, protoMinor=v2.PROTO_MINOR, fwVersion=0x0042, hwType=0xC6, mac=self.mac,
                caps=self.caps(), maxPayload=v2.MAX_PAYLOAD, cacheKb=self.cache_kb, pendingSlots=self.slots)
        if t in (v2.HEARTBEAT, v2.GET_STATS):
            return v2.STATUS, self.status()
        if t == v2.SET_CONFIG:
            if len(p) < v2.SetConfig.size:
                return nak(v2.NAK_BAD_LENGTH)
            c = v2.SetConfig.unpack(p)
            if c["channel"] != v2.CFG_UNCHANGED_U8:
                if not 11 <= c["channel"] <= 27:
                    return nak(v2.NAK_INVALID_PARAM)
                self.channel = c["channel"]
            if c["txPowerDbm"] != v2.CFG_UNCHANGED_POWER:
                self.power = c["txPowerDbm"]
            log.info("config: channel %d, power %d", self.channel, self.power)
            return v2.ACK, b""
        if t == v2.SET_PENDING:
            if len(p) < v2.SetPending.size:
                return nak(v2.NAK_BAD_LENGTH)
            sp = v2.SetPending.unpack(p)
            with self.lock:
                if sp["mac"] not in self.pending and len(self.pending) >= self.slots:
                    return nak(v2.NAK_NO_SLOT)
                self.pending[sp["mac"]] = sp
            log.info("pending for %s: ver %016X, %d bytes, type %02X", v2.mac_str(sp["mac"]), sp["dataVer"],
                     sp["dataSize"], sp["dataType"])
            return v2.ACK, b""
        if t == v2.CANCEL_PENDING:
            if len(p) < 8:
                return nak(v2.NAK_BAD_LENGTH)
            with self.lock:
                self.pending.pop(p[:8], None)
            return v2.ACK, b""
        if t == v2.DATA_CHUNK:
            if len(p) <= v2.DataChunkHeader.size:
                return nak(v2.NAK_BAD_LENGTH)
            h = v2.DataChunkHeader.unpack(p)
            data = p[v2.DataChunkHeader.size:]
            block, off = divmod(h["offset"], v2.BLOCK_SIZE)
            if off + len(data) > v2.BLOCK_SIZE:
                return nak(v2.NAK_INVALID_PARAM)
            with self.lock:
                key = (h["dataVer"], block)
                if key not in self.cache and self.cache_used_kb() + 4 > self.cache_kb:
                    return nak(v2.NAK_NO_CACHE_SPACE)
                buf = self.cache.setdefault(key, bytearray())
                if len(buf) < off + len(data):
                    buf.extend(b"\x00" * (off + len(data) - len(buf)))
                buf[off:off + len(data)] = data
                self.cache_size[h["dataVer"]] = h["totalSize"]
                self.filled.setdefault(key, set()).update(range(off, off + len(data)))
            return v2.ACK, b""
        if t == v2.DATA_DROP:
            ver = struct.unpack_from("<Q", p)[0]
            with self.lock:
                for key in [k for k in self.cache if k[0] == ver]:
                    del self.cache[key]
                    self.filled.pop(key, None)
            return v2.ACK, b""
        if t in (v2.ALLOW_CLEAR, v2.ALLOW_ADD, v2.ALLOW_REMOVE):
            return v2.ACK, b""
        return nak(v2.NAK_UNKNOWN_TYPE)

    def cache_used_kb(self):
        return 4 * len(self.cache)

    def block_complete(self, ver, block):
        total = self.cache_size.get(ver)
        if total is None:
            return False
        need = min(v2.BLOCK_SIZE, total - block * v2.BLOCK_SIZE)
        return len(self.filled.get((ver, block), ())) >= need

    def status(self):
        with self.lock:
            pend = sum(1 for p in self.pending.values() if p["dataType"])
            idle = len(self.pending) - pend
            used = self.cache_used_kb()
        return v2.Status.pack(
            uptimeMs=int((time.monotonic() - self.start) * 1000) & 0xFFFFFFFF, channel=self.channel,
            subghzChannel=0xFF, txPowerDbm=self.power, lastResetReason=1, pending=pend, pendingIdle=idle,
            cacheUsedKb=used, rxFrames=self.counters["rx"], txFrames=self.counters["tx"], lateRequestsSkipped=0,
            serialCrcErrors=self.counters["crc"] & 0xFFFF, serialOverflows=0,
            retransmits=self.counters["retransmits"] & 0xFFFF, eventsQueued=len(self.events))

    # --- events ----------------------------------------------------------------------------------

    def queue_event(self, t, payload):
        if not self.v2:
            return
        if len(self.events) >= 24:
            for i, (et, _) in enumerate(self.events):
                if (i > 0 or not self.ev_out) and et in (v2.CHECKIN, v2.BLOCK_SERVED):
                    del self.events[i]
                    break
            else:
                return
        self.events.append((t, payload))

    def poll_events(self):
        if not self.v2 or not self.events or self.link_down:
            return
        now = time.monotonic()
        if not self.ev_out:
            self.send_head(False)
        elif now - self.ev_sent >= v2.ACK_TIMEOUT_MS / 1000:
            if self.ev_tries < v2.RETRIES:
                self.ev_tries += 1
                self.counters["retransmits"] += 1
                self.send_head(True)
            else:
                log.warning("no answer to event %s", v2.type_name(self.events[0][0]))
                self.link_down = True

    def send_head(self, retransmit):
        t, p = self.events[0]
        self.counters["tx"] += 1
        self.send(v2.build_frame(t, self.tx_seq, v2.FLAG_RETRANSMIT if retransmit else 0, p))
        self.ev_out = True
        self.ev_sent = time.monotonic()

    # --- fake tags -------------------------------------------------------------------------------

    def tick_tags(self):
        now = time.monotonic()
        for tag in self.tags:
            if now < tag.next_checkin:
                continue
            tag.next_checkin = now + tag.interval
            self.tag_checkin(tag)

    def tag_checkin(self, tag):
        with self.lock:
            sp = self.pending.get(tag.mac)
        adr = v2.AvailDataReq.pack(
            checksum=0, lastPacketLQI=200, lastPacketRSSI=-55, temperature=21, batteryMv=tag.battery,
            hwType=tag.hw_type, wakeupReason=1 if tag.boot else 0, capabilities=0, tagSoftwareVersion=tag.fw,
            currentChannel=self.channel, customMode=0, reserved=b"\x00" * 8)
        tag.boot = False
        ci = v2.Checkin.pack(
            mac=tag.mac, timestampUs=int(time.monotonic() * 1e6) & 0xFFFFFFFF, rssi=-55, lqi=200, radio=0,
            channel=self.channel, pan=0x4447, kind=v2.CHECKIN_FULL,
            answeredVer=sp["dataVer"] if sp else 0, answeredType=sp["dataType"] if sp else 0, adr=adr)
        self.queue_event(v2.CHECKIN, ci)
        if sp and sp["dataType"] == 0xAF:
            # a command travels in the answer itself: done at once
            del self.pending[tag.mac]
            self.queue_event(v2.XFER_COMPLETE, v2.XferEnd.pack(mac=tag.mac, dataVer=sp["dataVer"]))
            log.info("%s: command %02X delivered", v2.mac_str(tag.mac), sp["dataTypeArg"])
        elif sp and sp["dataType"] and sp["dataSize"]:
            threading.Thread(target=self.tag_transfer, args=(tag, sp), daemon=True).start()

    def tag_transfer(self, tag, sp):
        ver, size = sp["dataVer"], sp["dataSize"]
        blocks = (size + v2.BLOCK_SIZE - 1) // v2.BLOCK_SIZE
        for block in range(blocks):
            for _ in range(20):  # the tag asks again, up to a few seconds
                with self.lock:
                    complete = self.block_complete(ver, block)
                if complete:
                    break
                length = min(v2.BLOCK_SIZE, size - block * v2.BLOCK_SIZE)
                with self.lock:
                    self.queue_event(v2.DATA_NEEDED, v2.DataNeeded.pack(
                        mac=tag.mac, dataVer=ver, offset=block * v2.BLOCK_SIZE, length=length, pleaseWaitMs=200))
                time.sleep(0.25)
            else:
                log.warning("%s: block %d never arrived", v2.mac_str(tag.mac), block)
                return
            time.sleep(0.05)
            with self.lock:
                self.queue_event(v2.BLOCK_SERVED, v2.BlockServed.pack(
                    mac=tag.mac, dataVer=ver, blockId=block, partsRequested=42, framesSent=42, lateMs=0))
        with self.lock:
            if self.pending.get(tag.mac, {}).get("dataVer") == ver:
                del self.pending[tag.mac]
            self.queue_event(v2.XFER_COMPLETE, v2.XferEnd.pack(mac=tag.mac, dataVer=ver))
        log.info("%s: transfer of %016X complete", v2.mac_str(tag.mac), ver)

    # --- main loop -------------------------------------------------------------------------------

    def serve(self, host="0.0.0.0", port=6639):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        srv.settimeout(0.01)
        log.info("listening on %s:%d, %d fake tags", host, port, len(self.tags))
        while True:
            if self.conn is None:
                try:
                    conn, addr = srv.accept()
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.settimeout(0.005)
                    self.conn = conn
                    log.info("host connected from %s", addr[0])
                except socket.timeout:
                    pass
            else:
                try:
                    data = self.conn.recv(4096)
                    if not data:
                        raise ConnectionError
                    with self.lock:
                        if self.v2:
                            self.rx_v2(data)
                        else:
                            self.rx_ascii(data)
                except socket.timeout:
                    pass
                except (ConnectionError, OSError):
                    log.info("host disconnected")
                    self.conn.close()
                    self.conn = None
            with self.lock:
                if self.v2 and time.monotonic() - self.last_frame > v2.LINK_LOST_MS / 1000:
                    log.warning("no frames from the host, back to the ASCII protocol")
                    self.reset_link()
                self.tick_tags()
                self.poll_events()
