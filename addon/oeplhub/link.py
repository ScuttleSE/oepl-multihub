"""One link to one hub (an OEPL C6 radio): startup, requests with retries, events, heartbeat.

Threads: a reader (frames in, ACKs for events), a dispatcher (calls on_event, so a handler may make
requests itself), and a heartbeat. Several HubLink objects can run side by side, one per hub.
"""

import logging
import queue
import re
import struct
import threading
import time

from . import v2proto as v2


class LinkError(Exception):
    pass


class NakError(LinkError):
    def __init__(self, ftype, code):
        super().__init__(f"{v2.type_name(ftype)} refused, NAK code {code}")
        self.code = code


class HubLink:
    def __init__(self, name, transport, on_event=None, on_state=None, ack_timeout_ms=v2.ACK_TIMEOUT_MS):
        self.name = name
        self.t = transport
        self.on_event = on_event or (lambda link, ftype, ev: None)
        self.on_state = on_state or (lambda link, up: None)
        self.ack_timeout = ack_timeout_ms / 1000
        self.log = logging.getLogger(f"hub.{name}")

        self.up = False
        self.info = {}    # from the ASCII NFO? answer
        self.hello = {}   # HELLO_REPLY
        self.status = {}  # last STATUS
        self.stats = {"retransmits": 0, "timeouts": 0, "bad_frames": 0, "dup_events": 0}

        self._seq = 0
        self._seq_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._waiting = {}  # seq -> [threading.Event, Frame | None]
        self._waiting_lock = threading.Lock()
        self._last_event_seq = -1
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._threads = []

    # --- startup ---------------------------------------------------------------------------------

    def connect(self):
        """Open the transport and bring the link up. Raises LinkError on failure."""
        self._stop.clear()
        self.t.open()
        try:
            # A radio that is still in v2 (host restart within 5 s) answers HELLO directly
            if self.t.has_baud:
                self.t.set_baud(v2.BAUD)
            self._start_reader()
            if not self._try_hello(attempts=1):
                self._stop_reader()
                if self.t.has_baud:
                    self.t.set_baud(115200)
                self._ascii_startup()
                self._start_reader()
                if not self._try_hello(attempts=3):
                    raise LinkError("no HELLO_REPLY")
            self.status = self.get_stats()
        except Exception:
            self.close()
            raise
        self.up = True
        self._spawn(self._heartbeat_loop, "hb")
        self._spawn(self._dispatch_loop, "ev")
        self.log.info("link up: radio %02X fw %04X mac %s caps %04X cache %d KB, %d slots, channel %d",
                      self.hello["hwType"], self.hello["fwVersion"], v2.mac_str(self.hello["mac"]),
                      self.hello["caps"], self.hello["cacheKb"], self.hello["pendingSlots"],
                      self.status.get("channel", 0))
        self.on_state(self, True)

    def _read_until(self, pattern, timeout):
        buf = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            buf += self.t.read(min(0.05, max(0.0, end - time.monotonic())))
            m = re.search(pattern, buf)
            if m:
                return buf, m
        return buf, None

    def _ascii_startup(self):
        self.t.read(0.05)
        for _ in range(3):
            self.t.write(b"RDY?")
            _, m = self._read_until(rb"ACK>", 0.5)
            if m:
                break
        else:
            raise LinkError("no answer to RDY?")
        self.t.write(b"NFO?")
        buf, m = self._read_until(rb"CAP>([0-9A-Fa-f]{4})", 1.0)
        if not m:
            raise LinkError("no CAP> in the NFO? answer: radio without protocol v2")
        text = buf.decode("ascii", "replace")
        info = {k: v for k, v in re.findall(r"([A-Z]{3})>([0-9A-Fa-f]+)", text) if k != "ACK"}
        self.info = info
        self.log.debug("NFO: %s", info)
        self.t.write(b"FRM1")
        _, m = self._read_until(rb"ACK>", 0.5)
        if not m:
            raise LinkError("no ACK> to FRM1")
        time.sleep((v2.SWITCH_DELAY_MS + 10) / 1000)
        if self.t.has_baud:
            self.t.set_baud(v2.BAUD)

    def _try_hello(self, attempts):
        payload = v2.Hello.pack(protoMajor=v2.PROTO_MAJOR, protoMinor=v2.PROTO_MINOR,
                                wantedCaps=v2.CAP_FRAMING | v2.CAP_TAG_RETURN_DATA | v2.CAP_ALLOW_LIST)
        for _ in range(attempts):
            try:
                f = self._request(v2.HELLO, payload, retries=0, timeout=0.2)
            except LinkError:
                continue
            if f.type != v2.HELLO_REPLY or len(f.payload) < v2.HelloReply.size:
                continue
            self.hello = v2.HelloReply.unpack(f.payload)
            if self.hello["protoMajor"] != v2.PROTO_MAJOR:
                raise LinkError(f"radio speaks protocol {self.hello['protoMajor']}.{self.hello['protoMinor']}")
            return True
        return False

    # --- threads ---------------------------------------------------------------------------------

    def _spawn(self, fn, tag):
        th = threading.Thread(target=fn, name=f"{self.name}-{tag}", daemon=True)
        th.start()
        self._threads.append(th)

    def _start_reader(self):
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, name=f"{self.name}-rx", daemon=True)
        self._reader.start()

    def _stop_reader(self):
        self._reader_stop.set()
        self._reader.join()

    def _reader_loop(self):
        deframer = v2.Deframer()
        while not self._reader_stop.is_set() and not self._stop.is_set():
            try:
                data = self.t.read(0.1)
            except ConnectionError as e:
                self._lost(f"read: {e}")
                return
            for f in deframer.feed(data):
                self._handle_frame(f)
            self.stats["bad_frames"] = deframer.bad

    def _handle_frame(self, f):
        if f.is_response:
            with self._waiting_lock:
                w = self._waiting.get(f.seq)
                if w is not None and w[1] is None:
                    w[1] = f
                    w[0].set()
            return
        if f.type not in v2.EVENT_TYPES:
            self._write(v2.build_frame(v2.NAK, f.seq, v2.FLAG_RESPONSE, bytes([v2.NAK_UNKNOWN_TYPE])))
            return
        if f.type != v2.LOG:
            self._write(v2.build_frame(v2.ACK, f.seq, v2.FLAG_RESPONSE))
        if f.seq == self._last_event_seq:
            self.stats["dup_events"] += 1
            return
        self._last_event_seq = f.seq
        try:
            ev = v2.decode_event(f.type, f.payload)
        except struct.error:
            self.log.warning("short %s event dropped", v2.type_name(f.type))
            return
        self._events.put((f.type, ev))

    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                ftype, ev = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.on_event(self, ftype, ev)
            except Exception:
                self.log.exception("event handler failed for %s", v2.type_name(ftype))

    def _heartbeat_loop(self):
        missed = 0
        while not self._stop.wait(v2.HEARTBEAT_MS / 1000):
            try:
                f = self._request(v2.HEARTBEAT)
                if f.type == v2.STATUS:
                    self.status = v2.Status.unpack(f.payload)
                missed = 0
            except LinkError:
                missed += 1
                self.log.warning("heartbeat missed (%d)", missed)
                if missed >= 3:
                    self._lost("3 heartbeats missed")
                    return

    def _lost(self, why):
        if self._stop.is_set():
            return
        self.log.warning("link lost: %s", why)
        self.close()
        self.on_state(self, False)

    def close(self):
        self._stop.set()
        self.up = False
        self.t.close()
        with self._waiting_lock:
            for w in self._waiting.values():
                w[0].set()

    # --- requests --------------------------------------------------------------------------------

    def _write(self, wire):
        with self._write_lock:
            self.t.write(wire)

    def _next_seq(self):
        with self._seq_lock:
            s = self._seq
            self._seq = (self._seq + 1) & 0xFF
            return s

    def _send(self, ftype, seq, payload, retransmit):
        w = [threading.Event(), None]
        with self._waiting_lock:
            self._waiting[seq] = w
        if retransmit:
            self.stats["retransmits"] += 1
        try:
            self._write(v2.build_frame(ftype, seq, v2.FLAG_RETRANSMIT if retransmit else 0, payload))
        except ConnectionError as e:
            raise LinkError(str(e)) from e
        return w

    def _request(self, ftype, payload=b"", retries=v2.RETRIES, timeout=None):
        """Stop-and-wait: the response frame (ACK, NAK, HELLO_REPLY or STATUS). LinkError on no answer."""
        timeout = timeout or self.ack_timeout
        seq = self._next_seq()
        try:
            for attempt in range(retries + 1):
                w = self._send(ftype, seq, payload, attempt > 0)
                if w[0].wait(timeout) and w[1] is not None:
                    return w[1]
                if self._stop.is_set() and ftype != v2.HELLO:
                    break
            self.stats["timeouts"] += 1
            raise LinkError(f"no answer to {v2.type_name(ftype)}")
        finally:
            with self._waiting_lock:
                self._waiting.pop(seq, None)

    def request(self, ftype, payload=b""):
        f = self._request(ftype, payload)
        if f.type == v2.NAK:
            raise NakError(ftype, f.payload[0] if f.payload else 0)
        return f

    # --- commands --------------------------------------------------------------------------------

    def get_stats(self):
        f = self.request(v2.GET_STATS)
        return v2.Status.unpack(f.payload)

    def set_config(self, channel=v2.CFG_UNCHANGED_U8, power=v2.CFG_UNCHANGED_POWER, lock=v2.LOCK_OFF,
                   led=v2.CFG_UNCHANGED_U8, log_frames=False):
        flags = ((lock << v2.CFG_LOCK_SHIFT) & 0x06) | (v2.CFG_LOG_FRAMES if log_frames else 0)
        self.request(v2.SET_CONFIG, v2.SetConfig.pack(channel=channel, subghzChannel=v2.CFG_UNCHANGED_U8,
                                                      txPowerDbm=power, flags=flags, ledBrightness=led))

    def set_pending(self, mac, data_ver, data_size, data_type, data_type_arg=0, next_checkin=0, ttl_min=60,
                    data_follows=False):
        self.request(v2.SET_PENDING, v2.SetPending.pack(
            mac=mac, dataVer=data_ver, dataSize=data_size, dataType=data_type, dataTypeArg=data_type_arg,
            nextCheckin=next_checkin, ttlMin=ttl_min, flags=v2.PENDING_DATA_FOLLOWS if data_follows else 0))

    def cancel_pending(self, mac):
        self.request(v2.CANCEL_PENDING, mac)

    def data_drop(self, data_ver):
        self.request(v2.DATA_DROP, struct.pack("<Q", data_ver))

    def send_data(self, data_ver, data, offset=0, length=None, total_size=None):
        """DATA_CHUNKs for data[offset:offset+length], up to CHUNK_WINDOW unanswered at a time."""
        total_size = len(data) if total_size is None else total_size
        end = min(len(data), offset + (len(data) - offset if length is None else length))
        chunks = []
        pos = offset
        while pos < end:
            n = min(v2.CHUNK_MAX, end - pos, v2.BLOCK_SIZE - pos % v2.BLOCK_SIZE)
            chunks.append((pos, n))
            pos += n

        flight = {}  # seq -> [chunk, tries, sent_at, waiter, payload]
        nxt = 0
        while nxt < len(chunks) or flight:
            while len(flight) < v2.CHUNK_WINDOW and nxt < len(chunks):
                pos, n = chunks[nxt]
                nxt += 1
                payload = v2.DataChunkHeader.pack(dataVer=data_ver, offset=pos, totalSize=total_size) \
                    + data[pos:pos + n]
                seq = self._next_seq()
                flight[seq] = [pos, 0, time.monotonic(), self._send(v2.DATA_CHUNK, seq, payload, False), payload]
            time.sleep(0.001)
            now = time.monotonic()
            for seq, f in list(flight.items()):
                w = f[3]
                if w[0].is_set() and w[1] is not None:
                    if w[1].type == v2.NAK:
                        self._drop_waiters(flight)
                        raise NakError(v2.DATA_CHUNK, w[1].payload[0] if w[1].payload else 0)
                    del flight[seq]
                    with self._waiting_lock:
                        self._waiting.pop(seq, None)
                elif self._stop.is_set():
                    self._drop_waiters(flight)
                    raise LinkError("link closed")
                elif now - f[2] >= self.ack_timeout:
                    f[1] += 1
                    if f[1] > v2.RETRIES:
                        self.stats["timeouts"] += 1
                        self._drop_waiters(flight)
                        raise LinkError(f"no answer to DATA_CHUNK at {f[0]}")
                    f[2] = now
                    f[3] = self._send(v2.DATA_CHUNK, seq, f[4], True)

    def _drop_waiters(self, flight):
        with self._waiting_lock:
            for seq in flight:
                self._waiting.pop(seq, None)

    def allow_clear(self):
        self.request(v2.ALLOW_CLEAR)

    def allow_add(self, macs):
        for i in range(0, len(macs), v2.ALLOW_MAX_MACS):
            self.request(v2.ALLOW_ADD, b"".join(macs[i:i + v2.ALLOW_MAX_MACS]))

    def allow_remove(self, macs):
        for i in range(0, len(macs), v2.ALLOW_MAX_MACS):
            self.request(v2.ALLOW_REMOVE, b"".join(macs[i:i + v2.ALLOW_MAX_MACS]))
