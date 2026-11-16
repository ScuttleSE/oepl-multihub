"""Tag types (resources/tagtypes/XX.json): local overrides first, then a cache, then upstream GitHub."""

import json
import logging
import os
import urllib.request

from .imageenc import TagType

log = logging.getLogger("tagtypes")

UPSTREAM = "https://raw.githubusercontent.com/OpenEPaperLink/OpenEPaperLink/master/resources/tagtypes/{:02X}.json"


class TagTypes:
    def __init__(self, cache_dir: str, extra_dirs=()):
        self.cache_dir = cache_dir
        self.dirs = list(extra_dirs) + [cache_dir]
        self.types: dict[int, TagType] = {}
        self.raw: dict[int, dict] = {}
        self.missing: set[int] = set()
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, hw_type: int) -> TagType | None:
        if hw_type in self.types:
            return self.types[hw_type]
        for d in self.dirs:
            p = os.path.join(d, f"{hw_type:02X}.json")
            if os.path.exists(p):
                return self._add(hw_type, p)
        if hw_type in self.missing:
            return None
        try:
            with urllib.request.urlopen(UPSTREAM.format(hw_type), timeout=10) as r:
                data = r.read()
            p = os.path.join(self.cache_dir, f"{hw_type:02X}.json")
            with open(p, "wb") as f:
                f.write(data)
            log.info("tag type %02X fetched from upstream", hw_type)
            return self._add(hw_type, p)
        except Exception as e:
            log.warning("tag type %02X not found: %s", hw_type, e)
            self.missing.add(hw_type)
            return None

    def _add(self, hw_type, path):
        with open(path) as f:
            d = json.load(f)
        self.raw[hw_type] = d
        self.types[hw_type] = TagType(d)
        return self.types[hw_type]
