"""HTTP and websocket server with the stock AP's API (web.cpp), on top of Core.

Run: python -m oeplhub.server --data DIR --hub HOST[:PORT]@CHANNEL [--hub ...] [--port 8080]
"""

import argparse
import asyncio
import io
import json
import logging
import os

from aiohttp import web, WSMsgType

from . import imageenc
from . import v2proto as v2
from .core import Core
from .tagtypes import TagTypes

log = logging.getLogger("server")

WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "www")


class Server:
    def __init__(self, core: Core, tagtypes: TagTypes):
        self.core = core
        self.tagtypes = tagtypes
        self.clients: set[web.WebSocketResponse] = set()
        self.loop = None
        core.notify = self.notify_threadsafe

    # --- websocket -------------------------------------------------------------------------------

    def notify_threadsafe(self, msg):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._broadcast, json.dumps(msg, separators=(",", ":")))

    def _broadcast(self, text):
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(text))

    async def ws_handler(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        self.core.ws_clients = len(self.clients)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            self.core.ws_clients = len(self.clients)
        return ws

    async def sys_loop(self):
        n = 0
        while True:
            self._broadcast(json.dumps({"sys": self.core.sys_info(counts=n % 12 == 0)}, separators=(",", ":")))
            n += 1
            await asyncio.sleep(5)

    # --- helpers ---------------------------------------------------------------------------------

    async def run(self, fn, *args, **kw):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kw))

    @staticmethod
    def mac_param(value):
        """hex2mac: 16 hex digits (12 also accepted by the stock AP, padded with zeros)."""
        if value is None:
            return None
        h = value.strip().upper()
        if len(h) == 12:
            h = "0000" + h
        try:
            v2.mac_bytes(h)
        except ValueError:
            return None
        return h

    # --- routes ----------------------------------------------------------------------------------

    async def index(self, request):
        return web.FileResponse(os.path.join(WWW, "index.html"), headers={"Cache-Control": "no-cache"})

    async def get_db(self, request):
        mac = request.query.get("mac")
        if mac is not None:
            m = self.mac_param(mac)
            if m is None:
                return web.json_response({"error": "malformatted parameter"})
            t = self.core.db.get(m)
            return web.json_response({"tags": [t.to_json()] if t else []})
        pos = int(request.query.get("pos", 0) or 0)
        return web.json_response(self.core.db.page(pos))

    async def sysinfo(self, request):
        hubs = self.core.hubs_up()
        h = hubs[0].link if hubs else None
        return web.json_response({
            "alias": self.core.ap_config.get("alias", ""), "env": "oepl-multihub", "buildtime": "0",
            "buildversion": "0.1", "sha": "", "psramsize": 0, "flashsize": 0, "rollback": False,
            "ap_version": h.hello.get("fwVersion", 0) if h else 0, "ap_type": 0xC6, "ap_typename": "ESP32-C6",
            "channel": h.status.get("channel", 0) if h else 0, "heapsize": 0, "fssize": 0,
            "resetreason": "", "crashed": False, "ntpserver": "", "ntpsync": 0,
            "currtime": self.core.sys_info()["currtime"], "has_sws_flasher": 0, "hasC6": 1, "hasH2": 0,
            "hasTslr": 0, "hasFlasher": 0, "hubs": len(self.core.hubs), "hubs_up": len(hubs)})

    async def get_ap_config(self, request):
        c = self.core.ap_config
        d = {"C6": "1", "H2": "0", "TLSR": "0", "savespace": "0", "hasFlasher": "0", "hasBLE": "0",
             "hasSubGhz": "0", "hotspotpw": "", "apstate": str(1 if self.core.hubs_up() else 0)}
        d.update(c)
        d["env"] = "oepl-multihub"
        return web.json_response(d)

    async def save_apcfg(self, request):
        form = dict(await request.post())
        status, text = await self.run(self.core.save_ap_config, form)
        return web.Response(status=status, text=text)

    async def save_cfg(self, request):
        form = dict(await request.post())
        mac = self.mac_param(form.get("mac"))
        if mac is None:
            return web.Response(text="Ok, saved")
        status, text = await self.run(self.core.save_tag_cfg, mac, form)
        return web.Response(status=status, text=text)

    async def tag_cmd(self, request):
        form = await request.post()
        if "mac" not in form or "cmd" not in form:
            return web.Response(status=500, text="param error")
        mac = self.mac_param(form["mac"])
        if mac is None:
            return web.Response(status=400, text="Error: mac not found")
        status, text = await self.run(self.core.cmd, mac, form["cmd"])
        return web.Response(status=status, text=text)

    async def led_flash(self, request):
        mac = self.mac_param(request.query.get("mac"))
        if mac is None:
            return web.Response(status=400, text="parameters are missing")
        status, text = await self.run(self.core.led_flash, mac, request.query.get("pattern"))
        return web.Response(status=status, text=text)

    async def imgupload(self, request):
        form = await request.post()
        mac = self.mac_param(form.get("mac"))
        if mac is None:
            return web.Response(status=400, text="parameters incomplete")
        f = form.get("image") or form.get("file")
        if f is None or not hasattr(f, "file"):
            return web.Response(status=400, text="parameters incomplete")
        data = f.file.read()

        def opt_int(k, default=None):
            v = form.get(k)
            try:
                return int(v) if v not in (None, "") else default
            except ValueError:
                return default

        status, text = await self.run(
            self.core.upload_image, mac, data, dither=opt_int("dither", 1), ttl=opt_int("ttl", 0),
            lut=opt_int("lut"), rotate=opt_int("rotate"), invert=opt_int("invert"), alias=form.get("alias"),
            preload_type=opt_int("preloadtype"), preload_lut=opt_int("preloadlut", 0),
            contentmode=opt_int("contentmode", 24))
        return web.Response(status=status, text=text)

    async def reboot(self, request):
        return web.Response(text="OK Reboot")

    async def current(self, request):
        name = os.path.basename(request.match_info["name"])
        p = os.path.join(self.core.current_dir, name)
        if not os.path.isfile(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "max-age=604800"})

    async def tagtype(self, request):
        name = os.path.basename(request.match_info["name"])
        try:
            hw = int(name.split(".")[0], 16)
        except ValueError:
            raise web.HTTPNotFound()
        await self.run(self.tagtypes.get, hw)
        if hw not in self.tagtypes.raw:
            raise web.HTTPNotFound()
        return web.json_response(self.tagtypes.raw[hw])

    async def api_preview(self, request):
        mac = self.mac_param(request.match_info["mac"])
        t = self.core.db.get(mac) if mac else None
        p = os.path.join(self.core.current_dir, f"{mac}.raw")
        if t is None or not t.imgtype or not os.path.isfile(p):
            raise web.HTTPNotFound()
        png = await self.run(self._render_preview, t, p)
        if png is None:
            raise web.HTTPNotFound()
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-cache"})

    def _render_preview(self, t, path):
        tt = self.tagtypes.get(t.hwType)
        if tt is None:
            return None
        with open(path, "rb") as f:
            data = f.read()
        try:
            im = imageenc.decode(t.imgtype, data, tt, rotate=t.rotate)
        except Exception as e:  # a damaged or unknown file must not break the UI
            log.warning("preview of %s: %s", t.mac, e)
            return None
        if im is None:
            return None
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    async def api_hubs(self, request):
        return web.json_response(self.core.hub_status())

    def app(self):
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.add_routes([
            web.get("/", self.index),
            web.get("/ws", self.ws_handler),
            web.get("/get_db", self.get_db),
            web.get("/sysinfo", self.sysinfo),
            web.get("/get_ap_config", self.get_ap_config),
            web.post("/save_apcfg", self.save_apcfg),
            web.post("/save_cfg", self.save_cfg),
            web.post("/tag_cmd", self.tag_cmd),
            web.get("/led_flash", self.led_flash),
            web.post("/imgupload", self.imgupload),
            web.post("/reboot", self.reboot),
            web.get("/current/{name}", self.current),
            web.get("/tagtypes/{name}", self.tagtype),
            web.get("/api/hubs", self.api_hubs),
            web.get("/api/preview/{mac}", self.api_preview),
        ])

        async def on_startup(app):
            self.loop = asyncio.get_running_loop()
            app["sys"] = asyncio.create_task(self.sys_loop())
            self.core.start()

        async def on_cleanup(app):
            app["sys"].cancel()
            await self.run(self.core.stop)

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        return app


def parse_hub(spec: str) -> dict:
    """HOST[:PORT]@CHANNEL or /dev/ttyACM0@CHANNEL; channel optional."""
    s, _, ch = spec.rpartition("@") if "@" in spec else (spec, "", "")
    return {"spec": s, "channel": int(ch) if ch else 0}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="./data", help="directory for the tag DB, images and settings")
    p.add_argument("--hub", action="append", default=[], help="HOST[:PORT]@CHANNEL or /dev/ttyACM0@CHANNEL")
    p.add_argument("--config", help="JSON file with {\"hubs\": [{\"spec\": ..., \"channel\": ...}]}")
    p.add_argument("--tagtypes", action="append", default=[], help="extra directory with tagtype JSONs")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--ack-timeout", type=int, default=v2.ACK_TIMEOUT_MS, help="ms")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.makedirs(a.data, exist_ok=True)
    hubs = [parse_hub(h) for h in a.hub]
    if a.config:
        with open(a.config) as f:
            hubs += json.load(f).get("hubs", [])
    tagtypes = TagTypes(os.path.join(a.data, "tagtypes"), a.tagtypes)
    core = Core(a.data, hubs, tagtypes, ack_timeout_ms=a.ack_timeout)
    server = Server(core, tagtypes)
    web.run_app(server.app(), host=a.host, port=a.port, print=lambda *x: log.info("listening on %s:%d", a.host, a.port))


if __name__ == "__main__":
    main()
