#!/usr/bin/env python3
"""Talk to OEPL hubs (C6 radios) over protocol v2.

Hubs are given as: /dev/ttyACM0 (native USB), uart:/dev/ttyUSB0 (UART adapter at 2 Mbaud), or
host[:port] (Wi-Fi, port 6639 by default).

  hubtool.py monitor HUB [HUB ...] [--channel 26 25 ...]   log check-ins and events
  hubtool.py push HUB --mac MAC --file F [--type 0x20]     send a file to one tag, wait for the result
  hubtool.py push HUB --mac MAC --image I --tagtype J      encode an image like the stock AP and send it
  hubtool.py fakehub [--port 6639] [--tag MAC ...]          run a simulated hub
"""

import argparse
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon"))

from oeplhub import v2proto as v2  # noqa: E402
from oeplhub.link import HubLink, LinkError  # noqa: E402
from oeplhub.transport import open_transport  # noqa: E402

log = logging.getLogger("hubtool")


def fmt_event(ftype, ev):
    if ftype == v2.CHECKIN:
        s = f"CHECKIN {ev['mac']} rssi {ev['rssi']} lqi {ev['lqi']} ch {ev['channel']}"
        if ev["adr"]:
            a = ev["adr"]
            s += (f" hw {a['hwType']:02X} fw {a['tagSoftwareVersion']:04X} batt {a['batteryMv']} mV"
                  f" temp {a['temperature']} wake {a['wakeupReason']}")
        else:
            s += " (short)"
        if ev["answeredType"]:
            s += f" -> offered type {ev['answeredType']:02X} ver {ev['answeredVer']:016X}"
        return s
    if ftype == v2.DATA_NEEDED:
        return f"DATA_NEEDED {ev['mac']} ver {ev['dataVer']:016X} @{ev['offset']} +{ev['length']}"
    if ftype == v2.BLOCK_SERVED:
        return (f"BLOCK_SERVED {ev['mac']} block {ev['blockId']} parts {ev['partsRequested']}"
                f" frames {ev['framesSent']} late {ev['lateMs']} ms")
    if ftype in (v2.XFER_COMPLETE, v2.XFER_TIMEOUT):
        return f"{v2.type_name(ftype)} {ev['mac']} ver {ev['dataVer']:016X}"
    if ftype == v2.TAG_RETURN_DATA:
        return f"TAG_RETURN_DATA {ev['mac']} {ev['data'].hex()}"
    if ftype == v2.LOG:
        return f"radio log: {ev['text']}"
    return f"{v2.type_name(ftype)} {ev}"


class Runner:
    """Keeps one link per hub up, reconnecting when it drops."""

    def __init__(self, specs, channels, ack_timeout, on_event):
        self.links = []
        self.channels = channels
        for i, spec in enumerate(specs):
            link = HubLink(f"hub{i + 1}", open_transport(spec), on_event=on_event, ack_timeout_ms=ack_timeout)
            self.links.append(link)

    def keep_up(self, link, idx, stop):
        while not stop.is_set():
            if not link.up:
                try:
                    link.connect()
                    if idx < len(self.channels):
                        link.set_config(channel=self.channels[idx])
                        log.info("%s: channel %d", link.name, self.channels[idx])
                except (LinkError, OSError) as e:
                    log.warning("%s: connect failed: %s", link.name, e)
                    link.close()
                    stop.wait(2)
                    continue
            stop.wait(0.5)

    def start(self):
        self.stop = threading.Event()
        for i, link in enumerate(self.links):
            threading.Thread(target=self.keep_up, args=(link, i, self.stop), daemon=True).start()


def cmd_monitor(args):
    def on_event(link, ftype, ev):
        log.info("%s: %s", link.name, fmt_event(ftype, ev))

    r = Runner(args.hubs, args.channel, args.ack_timeout, on_event)
    r.start()
    try:
        while True:
            time.sleep(10)
            for link in r.links:
                if link.up:
                    st = link.status
                    log.info("%s: up %ds ch %d, pending %d, cache %d KB, radio rx %d tx %d; link retransmits %d"
                             " timeouts %d bad frames %d", link.name, st.get("uptimeMs", 0) // 1000,
                             st.get("channel", 0), st.get("pending", 0), st.get("cacheUsedKb", 0),
                             st.get("rxFrames", 0), st.get("txFrames", 0), link.stats["retransmits"],
                             link.stats["timeouts"], link.stats["bad_frames"])
    except KeyboardInterrupt:
        pass


def cmd_push(args):
    if args.image:
        from PIL import Image
        from oeplhub import imageenc
        tt = imageenc.TagType.load(args.tagtype)
        args.type, data = imageenc.encode(Image.open(args.image), tt, dither=args.dither, tag_fw=args.tag_fw)
        args.arg = imageenc.data_type_arg(lut=args.lut)
        log.info("%s: %dx%d -> type %02X, %d bytes", args.image, tt.width, tt.height, args.type, len(data))
        ver = imageenc.data_ver(data)
    else:
        data = open(args.file, "rb").read()
        ver = args.ver if args.ver is not None else int(time.time() * 1000)
    mac = v2.mac_bytes(args.mac)
    done = threading.Event()
    result = {}

    def on_event(link, ftype, ev):
        log.info("%s: %s", link.name, fmt_event(ftype, ev))
        if ev.get("mac") != v2.mac_str(mac):
            return
        if ftype == v2.DATA_NEEDED and ev["dataVer"] == ver:
            link.send_data(ver, data, ev["offset"], ev["length"])
        elif ftype in (v2.XFER_COMPLETE, v2.XFER_TIMEOUT) and ev["dataVer"] == ver:
            result["type"] = ftype
            done.set()

    link = HubLink("hub1", open_transport(args.hub), on_event=on_event, ack_timeout_ms=args.ack_timeout)
    link.connect()
    if args.channel:
        link.set_config(channel=args.channel)
    follows = link.hello["caps"] & v2.CAP_BLOCK_CACHE and not args.no_preload
    link.set_pending(mac, ver, len(data), args.type, args.arg, ttl_min=args.ttl, data_follows=bool(follows))
    t0 = time.monotonic()
    if follows:
        link.send_data(ver, data)
        log.info("%d bytes in the radio's cache in %.0f ms", len(data), (time.monotonic() - t0) * 1000)
    log.info("waiting for %s to check in (ver %016X)", args.mac, ver)
    if not done.wait(args.timeout):
        log.error("no result within %d s", args.timeout)
        link.close()
        return 1
    ok = result["type"] == v2.XFER_COMPLETE
    log.info("%s after %.1f s", "complete" if ok else "TIMEOUT", time.monotonic() - t0)
    link.close()
    return 0 if ok else 1


def cmd_fakehub(args):
    from oeplhub.fakehub import FakeRadio, FakeTag
    tags = [FakeTag(m, hw_type=args.hw, interval=args.interval) for m in (args.tag or ["8C73DAFFFE5D531B"])]
    FakeRadio(tags, loss=args.loss, jitter_ms=args.jitter).serve(port=args.port)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ack-timeout", type=int, help="ms (default: 30 for USB/UART hubs, 1000 for Wi-Fi hubs)")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("monitor")
    m.add_argument("hubs", nargs="+")
    m.add_argument("--channel", type=int, nargs="*", default=[], help="one per hub, in order")

    u = sub.add_parser("push")
    u.add_argument("hub")
    u.add_argument("--mac", required=True)
    src = u.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="send this file as is")
    src.add_argument("--image", help="encode this image (PNG/JPEG, tag size) like the stock AP")
    u.add_argument("--tagtype", help="tagtype JSON, with --image")
    u.add_argument("--tag-fw", type=lambda s: int(s, 0), default=0,
                   help="tag firmware version, with --image: zlib when >= the tagtype's zlib_compression")
    u.add_argument("--dither", type=int, default=2, help="0 none, 1 Burkes, 2 ordered (default)")
    u.add_argument("--lut", type=int, default=0, help="with --image: dataTypeArgument lut bits")
    u.add_argument("--type", type=lambda s: int(s, 0), default=0x20, help="dataType (default 0x20)")
    u.add_argument("--arg", type=lambda s: int(s, 0), default=0, help="dataTypeArgument")
    u.add_argument("--ver", type=lambda s: int(s, 0))
    u.add_argument("--channel", type=int)
    u.add_argument("--ttl", type=int, default=60, help="minutes")
    u.add_argument("--timeout", type=int, default=600, help="seconds")
    u.add_argument("--no-preload", action="store_true", help="don't push to the cache; answer DATA_NEEDED")

    f = sub.add_parser("fakehub")
    f.add_argument("--port", type=int, default=6639)
    f.add_argument("--tag", action="append", help="MAC, 16 hex digits (repeatable)")
    f.add_argument("--hw", type=lambda s: int(s, 0), default=0x94, help="hwType of the fake tags")
    f.add_argument("--interval", type=float, default=5.0, help="seconds between check-ins")
    f.add_argument("--loss", type=float, default=0.0, help="frame loss 0..1, each direction")
    f.add_argument("--jitter", type=int, default=0, help="max extra delay per frame, ms")

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S")
    return {"monitor": cmd_monitor, "push": cmd_push, "fakehub": cmd_fakehub}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
