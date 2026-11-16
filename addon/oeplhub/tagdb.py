"""The tag database: one record per tag, as the stock AP keeps it (tag_db.cpp), stored as JSON."""

import json
import os
import threading
from dataclasses import dataclass, field

NEVER = 3216153600  # the stock AP's "no next update" / "in deep sleep" value


@dataclass
class Tag:
    mac: str  # display form, 16 hex digits
    md5: bytes = b"\x00" * 16
    lastseen: int = 0
    nextupdate: int = NEVER
    nextcheckin: int = 0
    pending: int = 0
    alias: str = ""
    contentMode: int = 0
    LQI: int = 0
    RSSI: int = 0
    temperature: int = 0
    batteryMv: int = 0
    hwType: int = 0
    wakeupReason: int = 0
    capabilities: int = 0
    modecfgjson: str = "null"
    rotate: int = 0
    lut: int = 0
    invert: int = 0
    updatecount: int = 0
    updatelast: int = 0
    ch: int = 0
    ver: int = 0
    imgtype: int = 0
    # not in the stock record
    hub: str = ""
    pending_idle: int = 0
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        """The record as the stock AP's fillNode() writes it, field for field."""
        return {
            "mac": self.mac, "hash": self.md5.hex(), "lastseen": self.lastseen, "nextupdate": self.nextupdate,
            "nextcheckin": self.nextcheckin, "pending": self.pending, "alias": self.alias,
            "contentMode": self.contentMode, "LQI": self.LQI, "RSSI": self.RSSI, "temperature": self.temperature,
            "batteryMv": self.batteryMv, "hwType": self.hwType, "wakeupReason": self.wakeupReason,
            "capabilities": self.capabilities, "modecfgjson": self.modecfgjson, "isexternal": False,
            "apip": "0.0.0.0", "rotate": self.rotate, "lut": self.lut, "invert": self.invert,
            "updatecount": self.updatecount, "updatelast": self.updatelast, "ch": self.ch, "ver": self.ver,
            "imgtype": self.imgtype, "hub": self.hub,
        }

    def to_store(self) -> dict:
        return self.to_json()

    @classmethod
    def from_store(cls, d: dict) -> "Tag":
        t = cls(mac=d["mac"].upper())
        for k in ("lastseen", "nextupdate", "nextcheckin", "alias", "contentMode", "LQI", "RSSI", "temperature",
                  "batteryMv", "hwType", "wakeupReason", "capabilities", "modecfgjson", "rotate", "lut", "invert",
                  "updatecount", "updatelast", "ch", "ver", "imgtype", "hub"):
            if k in d:
                setattr(t, k, d[k])
        if d.get("hash"):
            t.md5 = bytes.fromhex(d["hash"])
        return t


class TagDB:
    def __init__(self, path: str):
        self.path = path
        self.tags: dict[str, Tag] = {}  # insertion order = the stock AP's DB order
        self.lock = threading.RLock()
        self.dirty = False

    def get(self, mac: str) -> Tag | None:
        return self.tags.get(mac.upper())

    def add(self, mac: str) -> Tag:
        with self.lock:
            t = self.tags.get(mac) or Tag(mac=mac)
            self.tags[mac] = t
            self.dirty = True
            return t

    def delete(self, mac: str):
        with self.lock:
            self.tags.pop(mac.upper(), None)
            self.dirty = True

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path) as f:
            for d in json.load(f):
                t = Tag.from_store(d)
                self.tags[t.mac] = t

    def save(self):
        with self.lock:
            data = [t.to_store() for t in self.tags.values()]
            self.dirty = False
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def page(self, pos: int = 0, limit_bytes: int = 5000) -> dict:
        """/get_db: tags from pos on, cut after about limit_bytes of JSON, with "continu" like the AP."""
        with self.lock:
            items = list(self.tags.values())
        out, size = [], 0
        for i in range(pos, len(items)):
            d = items[i].to_json()
            out.append(d)
            size += len(json.dumps(d))
            if size > limit_bytes and i + 1 < len(items):
                return {"tags": out, "continu": i + 1}
        return {"tags": out}
