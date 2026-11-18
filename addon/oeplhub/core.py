"""The AP logic: what the stock AP's S3 does (newproto.cpp, pendingupload), for several hubs and one DB.

Threads: each hub's HubLink calls on_event from its dispatcher thread. Web requests call the public
methods from the server's thread. State is guarded by self.lock; radio I/O happens outside it.
"""

import hashlib
import json
import logging
import os
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from . import imageenc
from . import v2proto as v2
from .link import HubLink, LinkError
from .tagdb import NEVER, TagDB
from .transport import open_transport

CONTENT_PRICES = 27  # "Dayahead prices" in the stock AP; here from a Home Assistant Nordpool sensor

log = logging.getLogger("core")

DATATYPE_NOUPDATE = 0
DATATYPE_FW_UPDATE = 3
DATATYPE_COMMAND_DATA = 0xAF
CMD_DO_REBOOT, CMD_DO_SCAN, CMD_DO_RESET_SETTINGS, CMD_DO_DEEPSLEEP, CMD_DO_LEDFLASH = 0, 1, 2, 3, 4
WAKEUP_REASON_FIRSTBOOT = 0xFC
MAX_XFER_ATTEMPTS = 20
PRELOAD_AGAIN_S = 600

AUTO_CHANNELS = (26, 25, 20, 15, 11)  # radio firmware channels; Zigbee and Thread usually sit low

IMAGE_TYPES = {0x20, 0x21, 0x22, 0x23, 0x30, 0x31}

DEFAULT_AP_CONFIG = {
    "alias": "", "channel": 0, "subghzchannel": 0, "led": 127, "tft": 255, "language": 0, "maxsleep": 10,
    "stopsleep": 1, "preview": 1, "nightlyreboot": 0, "lock": 0, "wifipower": 34,
    "timezone": "CET-1CEST-2,M3.5.0/02:00:00,M10.5.0/03:00:00", "sleeptime1": 0, "sleeptime2": 0, "ble": 0,
    "repo": "OpenEPaperLink/OpenEPaperLink", "discovery": 0, "showtimestamp": 0,
}


@dataclass
class Item:
    data_type: int
    arg: int = 0
    ver: int = 0
    data: bytes = b""
    next_checkin: int = 0
    filename: str = ""
    attempts: int = MAX_XFER_ATTEMPTS
    cmd_size: int = 0  # a command carries 12 payload bytes in dataVer and dataSize

    @property
    def size(self):
        return self.cmd_size if self.data_type == DATATYPE_COMMAND_DATA else len(self.data)

    @property
    def plain_image(self):
        return self.data_type in IMAGE_TYPES and (self.arg & 0xFC) == 0


@dataclass
class Hub:
    name: str
    spec: str
    channel: int
    link: HubLink = None
    preloaded: dict = field(default_factory=dict)  # dataVer -> time sent to the cache


def now() -> int:
    return int(time.time())


class Core:
    def __init__(self, data_dir, hubs, tagtypes, notify=lambda msg: None, ack_timeout_ms=None):
        self.data_dir = data_dir
        self.current_dir = os.path.join(data_dir, "current")
        os.makedirs(self.current_dir, exist_ok=True)
        self.db = TagDB(os.path.join(data_dir, "tagDB.json"))
        self.db.load()
        self.tagtypes = tagtypes
        self.notify = notify
        self.lock = threading.RLock()
        self.queues: dict[str, list[Item]] = {}
        self.pending_path = os.path.join(data_dir, "pending.json")
        self.pending_dir = os.path.join(data_dir, "pending")
        os.makedirs(self.pending_dir, exist_ok=True)
        self._load_queues()
        self.started = time.monotonic()
        self.ws_clients = 0
        self.cfg_path = os.path.join(data_dir, "apconfig.json")
        self.ap_config = dict(DEFAULT_AP_CONFIG)
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path) as f:
                self.ap_config.update(json.load(f))
        self._apply_timezone()
        self.hubs = []
        # hubs without a channel get a free one, so no two hubs answer the same tag
        taken = {int(h.get("channel") or 0) for h in hubs}
        free = [c for c in AUTO_CHANNELS if c not in taken]
        for i, h in enumerate(hubs):
            channel = int(h.get("channel") or 0)
            if not channel and free:
                channel = free.pop(0)
                log.info("%s: channel %d chosen automatically", h.get("name") or f"hub{i + 1}", channel)
            hub = Hub(name=h.get("name") or f"hub{i + 1}", spec=h["spec"], channel=channel)
            hub.link = HubLink(hub.name, open_transport(hub.spec), on_event=self._on_event, on_state=self._on_state,
                               ack_timeout_ms=ack_timeout_ms)
            self.hubs.append(hub)
        self._stop = threading.Event()
        self.fonts = None
        self.vars_path = os.path.join(data_dir, "variables.json")
        self.variables = {}
        if os.path.exists(self.vars_path):
            with open(self.vars_path) as f:
                self.variables = json.load(f)

    # --- lifecycle -------------------------------------------------------------------------------

    def start(self):
        for hub in self.hubs:
            threading.Thread(target=self._keep_up, args=(hub,), name=f"{hub.name}-up", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    def stop(self):
        self._stop.set()
        for hub in self.hubs:
            hub.link.close()
        self.db.save()

    def _keep_up(self, hub):
        while not self._stop.is_set():
            if not hub.link.up:
                try:
                    hub.link.connect()
                except (LinkError, OSError) as e:
                    log.warning("%s (%s): connect failed: %s", hub.name, hub.spec, e)
                    hub.link.close()
                    self._stop.wait(3)
                    continue
            self._stop.wait(0.5)

    def _housekeeping(self):
        while not self._stop.wait(5):
            try:
                self._idle_requests()
            except Exception:
                log.exception("idle requests")
            try:
                self._run_content()
            except Exception:
                log.exception("content")
            if self.db.dirty:
                try:
                    self.db.save()
                except OSError as e:
                    log.error("saving the tag DB: %s", e)

    # --- sleep: idle requests and quiet hours (contentRunner in contentmanager.cpp) ----------------

    def _apply_timezone(self):
        tz = self.ap_config.get("timezone") or "UTC0"
        os.environ["TZ"] = tz
        time.tzset()

    def is_sleeping(self):
        """Quiet hours: no updates from sleeptime1 to sleeptime2 (local hours). Equal hours: off."""
        t1, t2 = int(self.ap_config.get("sleeptime1", 0)), int(self.ap_config.get("sleeptime2", 0))
        if t1 == t2:
            return False
        hour = time.localtime().tm_hour
        return t1 <= hour < t2 if t1 < t2 else hour >= t1 or hour < t2

    def _minutes_until_wake(self, ts):
        lt = time.localtime(ts)
        wake = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(self.ap_config.get("sleeptime2", 0)), 0, 0,
                            0, 0, -1))
        if wake < ts:
            wake += 24 * 3600
        return int(wake - ts) // 60 - 2

    def _idle_requests(self):
        """prepareIdleReq: shortly before a tag's check-in with nothing queued, tell it how long to sleep."""
        ts = now()
        maxsleep = int(self.ap_config.get("maxsleep", 10))
        if self.ws_clients and int(self.ap_config.get("stopsleep", 1)) == 1:
            return
        sleeping = self.is_sleeping()
        todo = []
        with self.lock:
            for t in list(self.db.tags.values()):
                if not (ts - 10 < t.nextcheckin < ts + 30) or t.pending_idle or self.queues.get(t.mac):
                    continue
                minutes = min((t.nextupdate - ts) // 60, maxsleep)
                if sleeping:
                    minutes = self._minutes_until_wake(ts)
                if minutes <= 1:
                    continue
                minutes = min(minutes, 0x7FFF)
                t.pending_idle = minutes * 60
                t.nextcheckin = ts + t.pending_idle
                todo.append((t.mac, minutes))
        for mac, minutes in todo:
            for hub in self._target_hubs(mac):
                try:
                    hub.link.set_pending(v2.mac_bytes(mac), 0, 0, DATATYPE_NOUPDATE, 0, minutes,
                                         ttl_min=10 + maxsleep)
                except LinkError as e:
                    log.warning("%s: idle request for %s failed: %s", hub.name, mac, e)
            log.debug("%s sleeping %d minutes", mac, minutes)

    # --- pending items on disk -------------------------------------------------------------------

    def _load_queues(self):
        if not os.path.exists(self.pending_path):
            return
        try:
            with open(self.pending_path) as f:
                stored = json.load(f)
        except (OSError, ValueError) as e:
            log.error("reading %s: %s", self.pending_path, e)
            return
        for mac, items in stored.items():
            q = []
            for d in items:
                data = b""
                if d.get("has_data"):
                    try:
                        with open(os.path.join(self.pending_dir, f"{d['ver']:016X}.bin"), "rb") as f:
                            data = f.read()
                    except OSError:
                        log.warning("%s: data for pending item %016X is missing, dropped", mac, d["ver"])
                        continue
                q.append(Item(data_type=d["data_type"], arg=d["arg"], ver=d["ver"], data=data,
                              next_checkin=d["next_checkin"], filename=d["filename"], cmd_size=d["cmd_size"]))
            if q:
                self.queues[mac] = q
                t = self.db.get(mac)
                if t:
                    t.pending = len(q)
        log.info("restored pending items for %d tags", len(self.queues))

    def _save_queues(self):
        """Call with self.lock held."""
        try:
            self._write_queues()
        except OSError as e:
            log.error("saving pending items: %s", e)

    def _write_queues(self):
        stored, keep = {}, set()
        for mac, q in self.queues.items():
            if not q:
                continue
            stored[mac] = []
            for i in q:
                if i.data:
                    name = f"{i.ver:016X}.bin"
                    keep.add(name)
                    p = os.path.join(self.pending_dir, name)
                    if not os.path.exists(p):
                        with open(p + ".tmp", "wb") as f:
                            f.write(i.data)
                        os.replace(p + ".tmp", p)
                stored[mac].append({"data_type": i.data_type, "arg": i.arg, "ver": i.ver, "has_data": bool(i.data),
                                    "next_checkin": i.next_checkin, "filename": i.filename,
                                    "cmd_size": i.cmd_size})
        with open(self.pending_path + ".tmp", "w") as f:
            json.dump(stored, f, indent=1)
        os.replace(self.pending_path + ".tmp", self.pending_path)
        for name in os.listdir(self.pending_dir):
            if name not in keep:
                os.remove(os.path.join(self.pending_dir, name))

    def hub_by_name(self, name):
        return next((h for h in self.hubs if h.name == name), None)

    def hubs_up(self):
        return [h for h in self.hubs if h.link.up]

    # --- messages to the web side ----------------------------------------------------------------

    def ws_log(self, text):
        log.info("%s", text)
        self.notify({"logMsg": text})

    def ws_err(self, text):
        log.warning("%s", text)
        self.notify({"errMsg": text})

    def ws_tag(self, mac):
        t = self.db.get(mac)
        if t:
            self.notify({"tags": [t.to_json()]})

    # --- hub events ------------------------------------------------------------------------------

    def _on_state(self, link, up):
        hub = next(h for h in self.hubs if h.link is link)
        if not up:
            self.ws_err(f"{hub.name} offline")
            return
        hub.preloaded.clear()
        try:
            link.set_config(channel=hub.channel or v2.CFG_UNCHANGED_U8,
                            led=int(self.ap_config.get("led", 127)) & 0xFF)
        except LinkError as e:
            log.warning("%s: SET_CONFIG failed: %s", hub.name, e)
        try:
            channel = link.get_stats().get("channel", hub.channel)
        except LinkError:
            channel = hub.channel
        self.ws_log(f"{hub.name} online, channel {channel}")
        # what this hub should offer: tags last seen on it, and tags not seen anywhere yet
        with self.lock:
            macs = [m for m, q in self.queues.items() if q]
        for mac in macs:
            t = self.db.get(mac)
            if t and (t.hub in ("", hub.name)):
                self._announce(mac, only=hub)

    def _on_event(self, link, ftype, ev):
        hub = next(h for h in self.hubs if h.link is link)
        if ftype == v2.CHECKIN:
            self._checkin(hub, ev)
        elif ftype == v2.DATA_NEEDED:
            self._data_needed(hub, ev)
        elif ftype == v2.BLOCK_SERVED:
            self._block_served(hub, ev)
        elif ftype == v2.XFER_COMPLETE:
            self._xfer_complete(hub, ev)
        elif ftype == v2.XFER_TIMEOUT:
            self._xfer_timeout(hub, ev)
        elif ftype == v2.TAG_RETURN_DATA:
            self.ws_log(f"<TRD {ev['mac']} {ev['data'].hex()}")
        elif ftype == v2.LOG:
            log.info("%s radio: %s", hub.name, ev["text"])

    def _checkin(self, hub, ev):
        mac = ev["mac"]
        adr = ev["adr"]
        roamed_from = None
        with self.lock:
            t = self.db.get(mac)
            if t is None:
                lock = int(self.ap_config.get("lock", 0))
                if lock == 1 or (lock == 2 and (not adr or adr["wakeupReason"] != WAKEUP_REASON_FIRSTBOOT)):
                    return
                t = self.db.add(mac)
                self.ws_log(f"new tag {mac} on {hub.name}")
            if t.hub and t.hub != hub.name:
                roamed_from = self.hub_by_name(t.hub)
            t.hub = hub.name
            ts = now()
            if self.queues.get(mac) or t.pending_idle == 0:
                if t.nextcheckin < ts + 60 or t.nextcheckin == NEVER:
                    t.nextcheckin = ts + 60
            else:
                t.nextcheckin = ts + t.pending_idle
            t.pending_idle = 0
            t.lastseen = ts
            if adr and adr["lastPacketRSSI"] != 0:
                if adr["wakeupReason"] >= 0xE0:
                    if not self.queues.get(mac):
                        t.nextupdate = 0
                        t.md5 = b"\x00" * 16
                    reason = {0xFC: "Booting", 0xFD: "Network scan", 0xFE: "Watchdog reset",
                              0xE0: "Firmware update rejected"}.get(adr["wakeupReason"], "")
                    self.ws_log(f"{mac} {reason}")
                t.LQI = adr["lastPacketLQI"]
                t.RSSI = adr["lastPacketRSSI"]
                t.temperature = adr["temperature"]
                t.batteryMv = adr["batteryMv"]
                t.wakeupReason = adr["wakeupReason"]
                t.capabilities = adr["capabilities"]
                t.ch = adr["currentChannel"]
                t.hwType = adr["hwType"]
                t.ver = adr["tagSoftwareVersion"]
            self.db.dirty = True
            has_queue = bool(self.queues.get(mac))
        if roamed_from is not None:
            self.ws_log(f"{mac} moved from {roamed_from.name} to {hub.name}")
            if roamed_from.link.up:
                try:
                    roamed_from.link.cancel_pending(v2.mac_bytes(mac))
                except LinkError:
                    pass
        if has_queue and (roamed_from is not None or ev["answeredType"] == 0):
            self._announce(mac, only=hub)
        self.ws_tag(mac)

    def _item_by_ver(self, mac, ver):
        with self.lock:
            return next((i for i in self.queues.get(mac, []) if i.ver == ver), None)

    def _data_needed(self, hub, ev):
        mac = ev["mac"]
        item = self._item_by_ver(mac, ev["dataVer"])
        if item is None or not item.data:
            with self.lock:
                queued = bool(self.queues.get(mac))
            if queued:
                self._announce(mac, only=hub)
            else:
                self._cancel_on(hub, mac)
            return
        off, length = ev["offset"], ev["length"]
        try:
            hub.link.send_data(item.ver, item.data, off, length)
        except LinkError as e:
            log.warning("%s: block for %s not sent: %s", hub.name, mac, e)
            return
        block = off // v2.BLOCK_SIZE
        checksum = sum(item.data[off:off + length]) & 0xFFFF
        total = (item.size + v2.BLOCK_SIZE - 1) // v2.BLOCK_SIZE
        self.ws_log(f"{mac} block request {item.filename} block {block}, len {length} checksum {checksum}")
        self.notify({"upload": {"src": mac, "current": block + 1, "total": total}})

    def _block_served(self, hub, ev):
        mac = ev["mac"]
        item = self._item_by_ver(mac, ev["dataVer"])
        if item is None:
            return
        total = (item.size + v2.BLOCK_SIZE - 1) // v2.BLOCK_SIZE
        self.ws_log(f"{mac} block served {item.filename} block {ev['blockId']}, {ev['partsRequested']} parts, "
                    f"{ev['framesSent']} frames, {ev['lateMs']} ms late")
        self.notify({"upload": {"src": mac, "current": ev["blockId"] + 1, "total": total}})

    def _xfer_complete(self, hub, ev):
        mac = ev["mac"]
        self.ws_log(f"{mac} reports xfer complete")
        with self.lock:
            q = self.queues.get(mac, [])
            head = q[0] if q else None
            if head is not None and head.ver != ev["dataVer"]:
                # finished data that was replaced meanwhile: offer the current item again
                replaced = True
            else:
                replaced = False
                if head is not None:
                    q.pop(0)
            t = self.db.get(mac)
            if t is not None and not replaced:
                if head is not None:
                    if head.data_type != DATATYPE_COMMAND_DATA:
                        t.md5 = struct.pack("<Q", head.ver) + b"\x00" * 8
                    if head.data_type in IMAGE_TYPES:
                        t.imgtype = head.data_type
                        if int(self.ap_config.get("preview", 1)):
                            with open(os.path.join(self.current_dir, f"{mac}.raw"), "wb") as f:
                                f.write(head.data)
                    if head.data_type == DATATYPE_COMMAND_DATA and head.arg == CMD_DO_DEEPSLEEP:
                        t.nextcheckin = NEVER
                t.updatecount += 1
                t.updatelast = now()
                t.pending = len(q)
                t.wakeupReason = 0
                self.db.dirty = True
            more = bool(q)
            self._save_queues()
        if replaced or more:
            self._announce(mac, only=hub)
        elif head is not None:
            self._cancel_on(hub, mac)
        self.ws_tag(mac)

    def _xfer_timeout(self, hub, ev):
        mac = ev["mac"]
        self.ws_err(f"< {mac} xfer timeout")
        with self.lock:
            self.queues[mac] = []
            self._save_queues()
            t = self.db.get(mac)
            if t:
                t.pending_idle = 60
                t.pending = 0
        self.ws_tag(mac)

    # --- announcing to the radios ----------------------------------------------------------------

    def _target_hubs(self, mac, only=None):
        if only is not None:
            return [only] if only.link.up else []
        t = self.db.get(mac)
        hub = self.hub_by_name(t.hub) if t and t.hub else None
        if hub is not None and hub.link.up:
            return [hub]
        return self.hubs_up()  # not seen yet, or its hub is down: every hub offers it

    def _announce(self, mac, only=None):
        """SET_PENDING for the tag's first queued item (checkQueue in the stock AP)."""
        with self.lock:
            q = self.queues.get(mac, [])
            if not q:
                return
            item = q[0]
            next_checkin = (5 | 0x8000) if len(q) > 1 else item.next_checkin
        mb = v2.mac_bytes(mac)
        for hub in self._target_hubs(mac, only):
            link = hub.link
            preload = bool(item.data) and bool(link.hello.get("caps", 0) & v2.CAP_BLOCK_CACHE) \
                and time.monotonic() - hub.preloaded.get(item.ver, -1e9) > PRELOAD_AGAIN_S
            try:
                link.set_pending(mb, item.ver, item.size, item.data_type, item.arg, next_checkin,
                                 ttl_min=item.attempts, data_follows=preload)
                if preload:
                    t0 = time.monotonic()
                    link.send_data(item.ver, item.data)
                    hub.preloaded[item.ver] = time.monotonic()
                    blocks = (item.size + v2.BLOCK_SIZE - 1) // v2.BLOCK_SIZE
                    self.ws_log(f"{mac} {item.filename} sent to {hub.name} in advance, {blocks} of {blocks} blocks "
                                f"in {(time.monotonic() - t0) * 1000:.0f} ms")
            except LinkError as e:
                log.warning("%s: announcing %s failed: %s", hub.name, mac, e)

    def _cancel_on(self, hub, mac):
        if hub.link.up:
            try:
                hub.link.cancel_pending(v2.mac_bytes(mac))
            except LinkError:
                pass

    def _cancel_everywhere(self, mac):
        for hub in self.hubs_up():
            self._cancel_on(hub, mac)

    def _queue(self, mac, item: Item):
        """queueDataAvail: a new plain image replaces queued plain images."""
        with self.lock:
            q = self.queues.setdefault(mac, [])
            if item.plain_image:
                q[:] = [i for i in q if not i.plain_image]
            q.append(item)
            self._save_queues()
            t = self.db.get(mac)
            if t:
                t.pending = len(q)
        self._announce(mac)
        self.ws_tag(mac)

    # --- requests from the web side --------------------------------------------------------------

    def max_sleep(self, minutes):
        maxsleep = int(self.ap_config.get("maxsleep", 10))
        if minutes > maxsleep:
            minutes = maxsleep
        if self.ws_clients and int(self.ap_config.get("stopsleep", 1)) == 1:
            minutes = 0
        return minutes

    def upload_image(self, mac, image_bytes, dither=1, ttl=0, lut=None, rotate=None, invert=None, alias=None,
                     preload_type=None, preload_lut=0, contentmode=24):
        """/imgupload: convert and queue. Returns (status, text)."""
        t = self.db.get(mac)
        if t is None:
            return 400, "mac not found"
        with self.lock:
            if alias is not None:
                t.alias = alias
            if rotate is not None:
                t.rotate = rotate
            if lut is not None:
                t.lut = lut
            if invert is not None:
                t.invert = invert
            name = f"/temp/{mac}_{int(time.monotonic() * 1000) % 100000000}.jpg"
            preload = preload_type is not None
            t.modecfgjson = json.dumps({"filename": name, "timetolive": str(ttl), "dither": str(dither), "delete": "1",
                                        "preload": str(int(preload)), "preload_lut": str(preload_lut),
                                        "preload_type": str(preload_type or 0)}, separators=(",", ":"))
            t.contentMode = contentmode
            t.nextupdate = NEVER
            self.db.dirty = True
        self.ws_tag(mac)
        tt = self.tagtypes.get(t.hwType)
        if tt is None:
            self.ws_err(f"no tag type for hwType {t.hwType:02X}")
            return 200, "Ok, saved"
        try:
            data_type, data = imageenc.encode_jpeg(image_bytes, tt, dither=dither, rotate=t.rotate, tag_fw=t.ver,
                                                          invert=t.invert == 1)
        except Exception as e:
            self.ws_err(f"image conversion failed: {e}")
            return 200, "Ok, saved"
        if preload:
            arg = imageenc.data_type_arg(lut=preload_lut, preload=True, special_type=preload_type)
        else:
            arg = imageenc.data_type_arg(lut=t.lut)
        self.queue_data(mac, data_type, data, arg, self.max_sleep(ttl), name)
        return 200, "Ok, saved"

    def queue_data(self, mac, data_type, data, arg=0, next_checkin=0, filename=""):
        ver = imageenc.data_ver(data)
        t = self.db.get(mac)
        if t is not None and t.md5[:8] == struct.pack("<Q", ver):
            self.ws_log("new image is the same as current image. not updating tag.")
            self.ws_tag(mac)
            return
        with self.lock:
            waiting = any(i.ver == ver for i in self.queues.get(mac, []))
        if waiting:
            self.ws_log("new image is the same as the one waiting for the tag. not queued again.")
            return
        if t is not None:
            t.pending_idle = (next_checkin & 0x7FFF) + 5 if next_checkin & 0x8000 else next_checkin * 60 + 60
        name = f"/current/{mac}_{int(time.monotonic() * 1000) % 1000000}.pending"
        self.ws_log(f"new image: {name}")
        self._queue(mac, Item(data_type=data_type, arg=arg, ver=ver, data=data, next_checkin=next_checkin,
                              filename=name))

    # --- /jsonupload (content mode 19, drawJsonStream) -------------------------------------------

    def json_upload(self, mac, json_text, ttl=0):
        """Draw a JSON list of elements for the tag and queue it. Returns (status, text)."""
        import copy
        from . import jsondraw
        t = self.db.get(mac)
        if t is None:
            return 400, "mac not found in tagDB"
        try:
            elements = json.loads(json_text)
        except ValueError as e:
            return 400, f"Error: json: {e}"
        name = f"/current/{mac}.json"
        with open(os.path.join(self.current_dir, f"{mac}.json"), "w") as f:
            f.write(json_text)
        with self.lock:
            t.modecfgjson = json.dumps({"filename": name, "interval": str(ttl)}, separators=(",", ":"))
            t.contentMode = 19
            t.nextupdate = NEVER
            self.db.dirty = True
        self.ws_tag(mac)
        tt = self.tagtypes.get(t.hwType)
        if tt is None:
            self.ws_err(f"no tag type for hwType {t.hwType:02X}")
            return 200, "Ok, saved"
        if self.fonts is None:
            self.fonts = jsondraw.Fonts(os.path.join(self.data_dir, "fonts"))

        def load_image(filename):
            p = os.path.realpath(os.path.join(self.data_dir, filename.lstrip("/")))
            if not p.startswith(os.path.realpath(self.data_dir) + os.sep) or not os.path.isfile(p):
                return None
            with open(p, "rb") as f:
                return f.read()

        try:
            img, toggled = jsondraw.render(elements, tt.width, tt.height, self.fonts, image_loader=load_image,
                                           log=self.ws_err, variables=dict(self.variables))
            if toggled:
                tt = copy.copy(tt)
                tt.rotatebuffer = 1 - tt.rotatebuffer % 2
            data_type, data = imageenc.encode(img, tt, dither=2, rotate=t.rotate, tag_fw=t.ver,
                                              invert=t.invert == 1)
        except Exception as e:
            log.exception("jsonupload for %s", mac)
            self.ws_err(f"json drawing failed: {e}")
            return 200, "Ok, saved"
        self.queue_data(mac, data_type, data, imageenc.data_type_arg(lut=t.lut), self.max_sleep(ttl),
                        f"/current/{mac}_{int(time.monotonic() * 1000) % 1000000}.pending")
        return 200, "Ok, saved"

    # --- content drawn by the addon: electricity prices (content mode 27) ------------------------

    def _run_content(self):
        due = getattr(self, "_content_due", None)
        if due is None:
            due = self._content_due = {}
        mono = time.monotonic()
        with self.lock:
            todo = [t.mac for t in self.db.tags.values()
                    if t.contentMode == CONTENT_PRICES and mono >= due.get(t.mac, 0)]
        for mac in todo:
            t = self.db.get(mac)
            try:
                cfg = json.loads(t.modecfgjson or "{}") or {}
            except ValueError:
                cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            interval = max(5, int(cfg.get("interval") or 15))
            due[mac] = mono + interval * 60
            self.draw_prices(mac, cfg.get("entity", ""), interval)

    def content_changed(self, mac):
        """Draw a tag's content at the next housekeeping pass (settings changed)."""
        getattr(self, "_content_due", {}).pop(mac, None)

    def draw_prices(self, mac, entity, interval=15):
        from datetime import datetime
        from . import haapi, pricechart
        t = self.db.get(mac)
        if t is None:
            return
        tt = self.tagtypes.get(t.hwType)
        if tt is None:
            self.ws_err(f"{mac}: no tag type for hwType {t.hwType:02X}")
            return
        try:
            if not entity:  # one Nordpool sensor in HA: use it
                found = [st["entity_id"] for st in haapi.get_states()
                         if isinstance(st.get("attributes", {}).get("raw_today"), list)]
                if len(found) != 1:
                    self.ws_err(f"{mac}: electricity prices: choose a price sensor in the tag's settings"
                                f" ({len(found)} Nordpool sensors found)")
                    return
                entity = found[0]
            attrs = haapi.get_state(entity).get("attributes", {})
        except haapi.HAError as e:
            self.ws_err(f"{mac}: electricity prices: {e}")
            return
        prices = pricechart.parse_prices(attrs)
        if not prices:
            self.ws_err(f"{mac}: electricity prices: {entity} has no raw_today prices")
            return
        from . import jsondraw
        if self.fonts is None:
            self.fonts = jsondraw.Fonts(os.path.join(self.data_dir, "fonts"))
        img = pricechart.render(tt, prices, datetime.now().astimezone(), pricechart.currency_of(attrs), self.fonts,
                                pricechart.template_for(tt, self.tagtypes.get), t.rotate)
        data_type, data = imageenc.encode(img, tt, dither=2, rotate=t.rotate, tag_fw=t.ver, invert=t.invert == 1)
        with self.lock:
            t.nextupdate = now() + interval * 60
            self.db.dirty = True
        self.queue_data(mac, data_type, data, imageenc.data_type_arg(lut=t.lut), self.max_sleep(interval),
                        f"/current/{mac}_{int(time.monotonic() * 1000) % 1000000}.pending")

    # --- variables for JSON templates (/set_var, /set_vars, checkVars) ---------------------------

    def set_vars(self, values: dict):
        changed = [k for k, v in values.items() if self.variables.get(k) != v]
        if not changed:
            return
        with self.lock:
            self.variables.update(values)
            with open(self.vars_path + ".tmp", "w") as f:
                json.dump(self.variables, f, indent=1)
            os.replace(self.vars_path + ".tmp", self.vars_path)
            redraw = []
            for t in self.db.tags.values():
                if t.contentMode != 19:
                    continue
                p = os.path.join(self.current_dir, f"{t.mac}.json")
                if not os.path.isfile(p):
                    continue
                with open(p) as f:
                    text = f.read()
                if any("{" + k + "}" in text for k in changed):
                    try:
                        ttl = int(json.loads(t.modecfgjson or "{}").get("interval", 0))
                    except (ValueError, AttributeError):
                        ttl = 0
                    redraw.append((t.mac, text, ttl))
        for mac, text, ttl in redraw:
            self.ws_log(f"{mac} redrawn: variable changed")
            self.json_upload(mac, text, ttl)

    # --- backup, restore, import from a stock AP ---------------------------------------------------

    def backup(self) -> bytes:
        with self.lock:
            return json.dumps([t.to_store() for t in self.db.tags.values()], indent=1).encode()

    def restore(self, records) -> int:
        """Merge tag records (our backup or a stock AP's tagDB.json). User settings come from the records; for
        tags we already know, our live state (hub, signal, current image) stays. Returns the number of tags."""
        from .tagdb import Tag
        n = 0
        with self.lock:
            for d in records:
                if not isinstance(d, dict) or not d.get("mac") or d.get("isexternal"):
                    continue
                new = Tag.from_store(d)
                t = self.db.get(new.mac)
                if t is None:
                    new.hub = ""
                    new.pending = 0
                    self.db.tags[new.mac] = new
                else:
                    for k in ("alias", "rotate", "lut", "invert"):
                        if k in d:
                            setattr(t, k, getattr(new, k))
                    if not t.lastseen:
                        for k in ("hwType", "ver", "lastseen", "batteryMv", "RSSI", "LQI", "temperature",
                                  "updatecount", "updatelast"):
                            setattr(t, k, getattr(new, k))
                n += 1
            self.db.dirty = True
        for mac in list(self.db.tags):
            self.ws_tag(mac)
        self.ws_log(f"restored {n} tags")
        return n

    def import_from_ap(self, host: str):
        """Fetch a stock AP's tag DB and current images. Returns (status, text)."""
        host = host.strip().rstrip("/")
        base = host if host.startswith(("http://", "https://")) else "http://" + host
        try:
            with urllib.request.urlopen(base + "/backup_db", timeout=30) as r:
                records = json.load(r)
        except (OSError, ValueError) as e:
            return 502, f"Error: could not read the tag DB from {host}: {e}"
        n = self.restore(records)
        images = 0
        for d in records:
            mac = str(d.get("mac", "")).upper()
            t = self.db.get(mac)
            dest = os.path.join(self.current_dir, f"{mac}.raw")
            if t is None or os.path.exists(dest) or not d.get("imgtype"):
                continue
            try:
                with urllib.request.urlopen(f"{base}/current/{mac}.raw", timeout=15) as r:
                    data = r.read()
            except OSError:
                continue
            with open(dest, "wb") as f:
                f.write(data)
            with self.lock:
                t.imgtype = d["imgtype"]
                if d.get("hash"):
                    t.md5 = bytes.fromhex(d["hash"])
                self.db.dirty = True
            self.ws_tag(mac)
            images += 1
        return 200, f"Ok, imported {n} tags and {images} images from {host}"

    # --- tag firmware (the stock UI's "auto update", content mode 5) -----------------------------

    def _fw_info(self):
        repo = self.ap_config.get("repo") or "OpenEPaperLink/OpenEPaperLink"
        cached = getattr(self, "_fw_cache", None)
        if cached and cached[0] == repo and time.monotonic() - cached[1] < 3600:
            return cached[2]
        url = f"https://raw.githubusercontent.com/{repo}/master/binaries/Tag/tagotaversions.json"
        with urllib.request.urlopen(url, timeout=15) as r:
            info = json.load(r)[0]
        self._fw_cache = (repo, time.monotonic(), info)
        return info

    def tag_fw_available(self, mac):
        """The newest tag firmware for this tag: {"name", "version", "md5"}, or None if its type has none."""
        t = self.db.get(mac)
        if t is None:
            return None
        e = self._fw_info().get(f"{t.hwType:02X}")
        if not e:
            return None
        sfx = ""
        if e["type"].startswith("chroma"):
            variation = int(mac[4:6], 16) >> 5
            if variation:
                sfx = f"_{variation}"
        if "type" + sfx not in e:
            return None
        return {"name": e["type" + sfx], "version": int(e["version" + sfx], 16), "md5": e["md5" + sfx].lower()}

    def tag_fw_update(self, mac, data=None, filename=""):
        """Queue a tag firmware: the given file, or the newest one from the repo. Returns (status, text)."""
        t = self.db.get(mac)
        if t is None:
            return 400, "Error: mac not found"
        if data is None:
            try:
                fw = self.tag_fw_available(mac)
            except (OSError, ValueError) as e:
                return 502, f"Error: firmware list: {e}"
            if fw is None:
                return 404, f"Error: no firmware known for tag type {t.hwType:02X}"
            repo = self.ap_config.get("repo") or "OpenEPaperLink/OpenEPaperLink"
            filename = f"{fw['name']}_{fw['version']:04X}.bin"
            try:
                with urllib.request.urlopen(f"https://raw.githubusercontent.com/{repo}/master/binaries/Tag/{filename}",
                                            timeout=30) as r:
                    data = r.read()
            except OSError as e:
                return 502, f"Error: download of {filename}: {e}"
            if hashlib.md5(data).hexdigest() != fw["md5"]:
                return 502, f"Error: {filename} has the wrong MD5"
        if not data or b"<html" in data[:1024].lower():
            return 400, "Error: that is not a firmware file"
        with self.lock:
            t.contentMode = 5
            t.modecfgjson = json.dumps({"filename": "/" + filename}, separators=(",", ":"))
            self.db.dirty = True
        self.ws_log(f"{mac} firmware update queued: {filename}, {len(data)} bytes")
        self.queue_data(mac, DATATYPE_FW_UPDATE, data, 0, 0, "/" + filename)
        return 200, "Ok, firmware update queued"

    def tag_command(self, mac, cmd, payload=b""):
        ver, size = struct.unpack("<QI", payload.ljust(12, b"\x00")[:12])
        self._queue(mac, Item(data_type=DATATYPE_COMMAND_DATA, arg=cmd, ver=ver, cmd_size=size))

    def cmd(self, mac, cmd):
        """/tag_cmd. Returns (status, text)."""
        t = self.db.get(mac)
        if t is None:
            return 400, "Error: mac not found"
        if cmd == "del":
            self.db.delete(mac)
            with self.lock:
                self.queues.pop(mac, None)
                self._save_queues()
        elif cmd == "purge":
            ts = now()
            for tag in list(self.db.tags.values()):
                if tag.nextcheckin == NEVER or tag.lastseen < ts - 24 * 3600 or ts > tag.nextcheckin + 600:
                    self.db.delete(tag.mac)
        elif cmd == "clear":
            self._cancel_everywhere(mac)
            with self.lock:
                self.queues[mac] = []
                self._save_queues()
                t.pending = 0
            self.ws_tag(mac)
        elif cmd == "refresh":
            p = os.path.join(self.current_dir, f"{mac}.raw")
            if os.path.exists(p) and t.imgtype:
                with open(p, "rb") as f:
                    data = f.read()
                t.md5 = b"\x00" * 16
                self.queue_data(mac, t.imgtype, data, imageenc.data_type_arg(lut=t.lut), 0)
        elif cmd in ("reboot", "scan", "reset", "deepsleep"):
            c = {"reboot": CMD_DO_REBOOT, "scan": CMD_DO_SCAN, "reset": CMD_DO_RESET_SETTINGS,
                 "deepsleep": CMD_DO_DEEPSLEEP}[cmd]
            self.tag_command(mac, c)
        elif cmd == "ledflash":
            self.tag_command(mac, CMD_DO_LEDFLASH, bytes([0x81, 0x3C, 0x13, 10, 0xE4, 0x53, 10, 0x03, 0xA3, 10, 2, 0]))
        elif cmd == "ledflash_long":
            self.tag_command(mac, CMD_DO_LEDFLASH, bytes([0x11, 0xE4, 0x33, 50, 0, 0, 0, 0, 0, 0, 60, 0]))
        elif cmd == "ledflash_stop":
            self.tag_command(mac, CMD_DO_LEDFLASH, bytes(12))
        self.db.dirty = True
        return 200, "Ok, done"

    def led_flash(self, mac, pattern_hex):
        if self.db.get(mac) is None:
            return 400, "parameters are missing"
        payload = bytes(12)
        if pattern_hex is not None:
            try:
                payload = bytes.fromhex(pattern_hex[:24])
                assert len(payload) == 12
            except (ValueError, AssertionError):
                return 400, "Error: expects 12 hex bytes in pattern"
        self.tag_command(mac, CMD_DO_LEDFLASH, payload)
        return 200, "ok, request transmitted"

    def save_tag_cfg(self, mac, form):
        t = self.db.get(mac)
        if t is None:
            return 200, "Error while saving: mac not found"
        with self.lock:
            if "contentmode" in form:
                t.contentMode = int(form["contentmode"])
            if "alias" in form:
                t.alias = form["alias"]
            if "modecfgjson" in form:
                t.modecfgjson = form["modecfgjson"]
            for k in ("rotate", "lut", "invert"):
                if k in form:
                    setattr(t, k, int(form[k]))
            self.db.dirty = True
        self.content_changed(mac)
        self.ws_tag(mac)
        return 200, "Ok, saved"

    def save_ap_config(self, form):
        int_keys = {"channel", "subghzchannel", "led", "tft", "language", "maxsleep", "stopsleep", "preview",
                    "nightlyreboot", "lock", "wifipower", "sleeptime1", "sleeptime2", "ble", "discovery",
                    "showtimestamp"}
        for k, v in form.items():
            if k in DEFAULT_AP_CONFIG:
                self.ap_config[k] = int(v) if k in int_keys else v
        with open(self.cfg_path, "w") as f:
            json.dump(self.ap_config, f, indent=1)
        self._apply_timezone()
        led = int(self.ap_config.get("led", 127)) & 0xFF
        for hub in self.hubs_up():
            try:
                hub.link.set_config(led=led)
            except LinkError:
                pass
        return 200, "Ok, saved"

    # --- status ----------------------------------------------------------------------------------

    def sys_info(self, counts=False):
        d = {"currtime": now(), "heap": 0, "recordcount": len(self.db.tags), "dbsize": len(self.db.tags) * 300,
             "littlefsfree": 0, "psfree": 0, "apstate": 1 if self.hubs_up() else 0, "runstate": 2, "rssi": 0,
             "wifistatus": 3, "wifissid": "", "uptime": int(time.monotonic() - self.started)}
        if counts:
            ts = now()
            d["lowbattcount"] = sum(1 for t in self.db.tags.values() if 0 < t.batteryMv < 2600)
            d["timeoutcount"] = sum(1 for t in self.db.tags.values()
                                    if t.nextcheckin and t.nextcheckin != NEVER and ts > t.nextcheckin + 600)
        return d

    def hub_status(self):
        out = []
        for h in self.hubs:
            st = h.link.status if h.link.up else {}
            out.append({"name": h.name, "spec": h.spec, "up": h.link.up, "channel": st.get("channel", h.channel),
                        "mac": v2.mac_str(h.link.hello["mac"]) if h.link.hello else "",
                        "fw": h.link.hello.get("fwVersion", 0), "status": st, "stats": dict(h.link.stats),
                        "tags": sum(1 for t in self.db.tags.values() if t.hub == h.name)})
        return out
