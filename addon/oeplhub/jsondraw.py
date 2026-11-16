"""JSON drawing for /jsonupload, as the stock AP does it (content_json.inl, content_draw.inl, TFT_eSPI Smooth_font).

drawJsonStream() draws each element onto a white sprite the size of the tag: text, textbox, box, rbox, line,
triangle, circle, image and rotate. Colours follow getColor() (RGB565, expanded here to RGB888). Text uses
TFT_eSPI smooth (VLW) fonts, alpha-blended onto the text background colour, or a TrueType font (TinyTTF on the AP,
Pillow here). initSprite()'s setRotation(3) is a no-op for 16 bpp sprites in TFT_eSPI, so coordinates are plain x/y.
"""

import gzip
import io
import json
import os
import re
import struct
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

FONT_URL = "https://raw.githubusercontent.com/OpenEPaperLink/OpenEPaperLink/master/ESP32_AP-Flasher/data/fonts/"

TL, TC, TR = 0, 1, 2

COLORS = {
    "white": 0xFFFF, "black": 0x0000, "red": 0xF800, "yellow": 0xFFE0, "lightgray": 0xBDF7,
    "darkgray": 0x7BEF, "pink": 0xFBCF, "brown": 0x8400, "green": 0x07E0, "blue": 0x001F, "orange": 0xFBE0,
}
COLOR_NUMBERS = ["white", "black", "red", "yellow", "lightgray", "darkgray", "pink", "brown", "green", "blue",
                 "orange"]


def rgb565(c: int) -> tuple:
    r, g, b = (c >> 11) & 0x1F, (c >> 5) & 0x3F, c & 0x1F
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def get_color(color) -> tuple:
    """getColor(): names, "0".."10", "#rrggbb" (truncated to RGB565); anything else is white."""
    if color is None or color is False:
        color = ""
    s = str(color).strip()
    if s == "":
        return rgb565(COLORS["black"])
    if s.isdigit() and int(s) < len(COLOR_NUMBERS):
        return rgb565(COLORS[COLOR_NUMBERS[int(s)]])
    if s in COLORS:
        return rgb565(COLORS[s])
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        r, g, b = int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
        return rgb565(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return rgb565(COLORS["white"])


class VlwFont:
    """A TFT_eSPI smooth font, with the metrics loadFont()/loadMetrics() derive."""

    def __init__(self, data: bytes):
        count, _, size, _, ascent, descent = struct.unpack_from(">6i", data, 0)
        self.ascent, self.descent = ascent, descent
        self.max_ascent, self.max_descent = ascent, descent
        self.glyphs = {}
        off, bmp = 24, 24 + count * 28
        for _ in range(count):
            code, h, w, adv, dy, dx, _ = struct.unpack_from(">7i", data, off)
            off += 28
            dy, dx = (dy + 0x8000) % 0x10000 - 0x8000, (dx + 0x80) % 0x100 - 0x80
            self.glyphs[code] = (w, h, adv, dy, dx, data[bmp:bmp + w * h])
            bmp += w * h
            if (0x20 < code < 0xA0 and code != 0x7F) or code > 0xFF:
                self.max_ascent = max(self.max_ascent, dy)
                self.max_descent = max(self.max_descent, h - dy)
        self.y_advance = self.max_ascent + self.max_descent
        self.space_width = (ascent + descent) * 2 // 7

    def text_width(self, text: str) -> int:
        width = 0
        for i, ch in enumerate(text):
            g = self.glyphs.get(ord(ch))
            if g is None:
                width += self.space_width + 1
                continue
            w, _, adv, _, dx, _ = g
            if width == 0 and dx < 0:
                width -= dx
            width += adv if i < len(text) - 1 else dx + w
        return width

    def draw(self, img: Image.Image, text: str, x: int, y: int, align: int, fg: tuple, bg: tuple):
        if align == TC:
            x -= self.text_width(text) // 2
        elif align == TR:
            x -= self.text_width(text)
        px = img.load()
        iw, ih = img.size
        for ch in text:
            g = self.glyphs.get(ord(ch))
            if g is None:
                if ch != " ":
                    ImageDraw.Draw(img).rectangle(
                        (x, y + self.max_ascent - self.ascent, x + self.space_width - 1, y + self.max_ascent - 1),
                        outline=fg)
                x += self.space_width + 1
                continue
            w, h, adv, dy, dx, bitmap = g
            cx, cy = x + dx, y + self.max_ascent - dy
            for gy in range(h):
                py = cy + gy
                if not 0 <= py < ih:
                    continue
                for gx in range(w):
                    a = bitmap[gy * w + gx]
                    qx = cx + gx
                    if a and 0 <= qx < iw:
                        px[qx, py] = fg if a == 255 else tuple((f * a + b * (255 - a)) // 255 for f, b in zip(fg, bg))
            x += adv


class TtfFont:
    def __init__(self, data: bytes):
        self.data = data
        self.sizes = {}

    def at(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self.sizes:
            self.sizes[size] = ImageFont.truetype(io.BytesIO(self.data), max(1, size))
        return self.sizes[size]


class Fonts:
    """Finds fonts in local_dirs, then cache_dir, then downloads them from upstream into cache_dir."""

    def __init__(self, cache_dir, local_dirs=(), fetch=True):
        self.cache_dir = cache_dir
        self.local_dirs = list(local_dirs)
        self.fetch = fetch
        self.loaded = {}

    def _read(self, filename: str):
        for d in self.local_dirs + [self.cache_dir]:
            path = os.path.join(d, filename)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
        if not self.fetch:
            return None
        try:
            with urllib.request.urlopen(FONT_URL + filename, timeout=20) as r:
                data = r.read()
        except Exception:
            return None
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = os.path.join(self.cache_dir, filename + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.cache_dir, filename))
        return data

    def get(self, font: str):
        """A VlwFont or TtfFont for a processFontPath()-style name, or None."""
        name = os.path.basename(font)
        if name.endswith(".vlw"):
            name = name[:-4]
        if name in self.loaded:
            return self.loaded[name]
        result = None
        if name.endswith(".ttf"):
            data = self._read(name)
            result = TtfFont(data) if data else None
        else:
            data = self._read(name + ".vlw")
            if data is None:
                data = self._read(name + ".vlw.gz")
                data = gzip.decompress(data) if data else None
            result = VlwFont(data) if data else None
        self.loaded[name] = result
        return result


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _arg(a: list, i: int, default=None):
    return a[i] if i < len(a) and a[i] is not None else default


def _replace_variables(text: str, variables: dict) -> str:
    variables = dict(variables or {}, ap_time=time.strftime("%H:%M:%S"))
    return re.sub(r"\{([^{}]*)\}", lambda m: str(variables.get(m.group(1), "-")), text)


def _font_kind(font: str) -> int:
    """processFontPath(): 1 legacy u8g2 (not drawn), 2 truetype, 3 vlw."""
    if font in ("glasstown_nbp_tf", "7x14_tf", "t0_14b_tf"):
        return 1
    return 2 if font.endswith(".ttf") else 3


class _Ctx:
    def __init__(self, img, fonts, image_loader, log, variables):
        self.img, self.fonts, self.image_loader, self.log, self.variables = img, fonts, image_loader, log, variables
        self.orientation = 0
        self.toggled = False

    def font(self, name: str):
        f = self.fonts.get(name)
        if f is None:
            self.log("can't load font " + name)
        return f


def draw_string(ctx, text, x, y, font, align, color, size, bgcolor):
    text = _replace_variables(str(text), ctx.variables)
    if font.startswith("fonts/calibrib"):
        calibri = _int(re.match(r"\d*", font[14:]).group())
        if calibri not in (30, 16):
            font, size = "Signika-SB.ttf", calibri
    legacy = {"glasstown_nbp_tf": ("tahoma9.vlw", 8), "7x14_tf": ("REFSAN12.vlw", 10),
              "t0_14b_tf": ("calibrib16.vlw", 11)}
    if font in legacy:
        font, dy = legacy[font]
        y -= dy
    kind = _font_kind(font)
    if font == "" or kind == 1:
        return
    f = ctx.font(font)
    if isinstance(f, TtfFont) and kind == 2:
        tf = f.at(size)
        w = int(tf.getlength(text))
        x -= w // 2 if align == TC else w if align == TR else 0
        ImageDraw.Draw(ctx.img).text((x, y), text, font=tf, fill=color, anchor="la")
    elif isinstance(f, VlwFont):
        f.draw(ctx.img, text, x, y, align, color, bgcolor)


def draw_textbox(ctx, text, x, y, boxwidth, boxheight, font, color, lineheight, align, size):
    text = _replace_variables(str(text), ctx.variables)
    kind = _font_kind(font)
    if font == "" or kind == 1:
        return
    f = ctx.font(font)
    if f is None:
        return
    if isinstance(f, TtfFont):
        size = size or 20
        tf = f.at(size)
        width, advance = (lambda s: int(tf.getlength(s))), size

        def line(s, ly):
            lx = x - (width(s) // 2 if align == TC else width(s) if align == TR else 0)
            ImageDraw.Draw(ctx.img).text((lx, ly), s, font=tf, fill=color, anchor="la")
    else:
        width, advance = f.text_width, f.y_advance

        def line(s, ly):
            f.draw(ctx.img, s, x, ly, align, color, (255, 255, 255))

    n, start, top = len(text), 0, y
    while start < n and y + advance <= top + boxheight:
        end, hasspace = start, False
        while end < n and width(text[start:end + 1]) <= boxwidth and text[end] != "\n":
            hasspace = hasspace or text[end] in " -"
            end += 1
        while end < n and end > start and hasspace and text[end - 1] not in " -" and text[end] != "\n":
            end -= 1
        line(text[start:end], y)
        y = int(y + advance * lineheight)
        if end < n and text[end] == "\n":
            end += 1
        start = end
        while start < n and text[start] == " ":
            start += 1


def rotate_buffer(ctx, rotation: int):
    rotation %= 4
    if rotation == ctx.orientation:
        return
    step = (ctx.orientation - rotation + 4) % 4
    ctx.img = ctx.img.rotate({1: -90, 2: 180, 3: -270}[step], expand=True)
    if step != 2:
        ctx.toggled = not ctx.toggled
    ctx.orientation = rotation


def _rect(d, x, y, w, h, fill=None, outline=None, r=0):
    if w <= 0 or h <= 0:
        return
    box = (x, y, x + w - 1, y + h - 1)
    if r > 0:
        d.rounded_rectangle(box, radius=min(r, (w - 1) // 2, (h - 1) // 2), fill=fill, outline=outline)
    else:
        d.rectangle(box, fill=fill, outline=outline)


def draw_element(ctx, el: dict):
    d = ImageDraw.Draw(ctx.img)
    if isinstance(el.get("text"), list):
        a = el["text"]
        bg = _arg(a, 7, "")
        draw_string(ctx, _arg(a, 2, ""), _int(_arg(a, 0)), _int(_arg(a, 1)), str(_arg(a, 3, "")), _int(_arg(a, 5)),
                    get_color(_arg(a, 4)), _int(_arg(a, 6)), get_color(bg) if str(bg) else (255, 255, 255))
    elif isinstance(el.get("textbox"), list):
        a = el["textbox"]
        lh = float(_arg(a, 7, 0) or 0) or 1
        draw_textbox(ctx, _arg(a, 4, ""), _int(_arg(a, 0)), _int(_arg(a, 1)), _int(_arg(a, 2)), _int(_arg(a, 3)),
                     str(_arg(a, 5, "")), get_color(_arg(a, 6)), lh, _int(_arg(a, 8)), _int(_arg(a, 9)))
    elif isinstance(el.get("box"), list):
        a = el["box"]
        x, y, w, h = (_int(_arg(a, i)) for i in range(4))
        _rect(d, x, y, w, h, fill=get_color(_arg(a, 4)))
        if len(a) >= 7:
            for i in range(_int(a[6])):
                _rect(d, x + i, y + i, w - 2 * i, h - 2 * i, outline=get_color(_arg(a, 5)))
    elif isinstance(el.get("rbox"), list):
        a = el["rbox"]
        x, y, w, h, r = (_int(_arg(a, i)) for i in range(5))
        _rect(d, x, y, w, h, fill=get_color(_arg(a, 5)), r=r)
        if len(a) >= 8:
            for i in range(_int(a[7])):
                _rect(d, x + i, y + i, w - 2 * i, h - 2 * i, outline=get_color(_arg(a, 6)), r=int(r - i / 1.41))
                if i > 0:
                    _rect(d, x + i - 1, y + i, w - 2 * i + 2, h - 2 * i, outline=get_color(_arg(a, 6)),
                          r=int(r - i / 1.41))
    elif isinstance(el.get("line"), list):
        a = el["line"]
        d.line([(_int(_arg(a, 0)), _int(_arg(a, 1))), (_int(_arg(a, 2)), _int(_arg(a, 3)))], fill=get_color(_arg(a, 4)))
    elif isinstance(el.get("triangle"), list):
        a = el["triangle"]
        d.polygon([(_int(_arg(a, i)), _int(_arg(a, i + 1))) for i in (0, 2, 4)], fill=get_color(_arg(a, 6)))
    elif isinstance(el.get("circle"), list):
        a = el["circle"]
        x, y, r = _int(_arg(a, 0)), _int(_arg(a, 1)), _int(_arg(a, 2))
        d.ellipse((x - r, y - r, x + r, y + r), fill=get_color(_arg(a, 3)))
        if len(a) >= 6:
            for i in range(_int(a[5])):
                ri = r - i
                if ri >= 0:
                    d.ellipse((x - ri, y - ri, x + ri, y + ri), outline=get_color(_arg(a, 4)))
    elif isinstance(el.get("image"), list):
        a = el["image"]
        name = str(_arg(a, 0, ""))
        data = ctx.image_loader(name) if ctx.image_loader else None
        if not data:
            ctx.log("image not found: " + name)
            return
        try:
            src = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:
            ctx.log(f"invalid image {name}: {e}")
            return
        ctx.img.paste(src, (_int(_arg(a, 1)), _int(_arg(a, 2))))
    elif "rotate" in el and isinstance(el["rotate"], (int, float)) and not isinstance(el["rotate"], bool):
        rotate_buffer(ctx, int(el["rotate"]) & 0xFF)
    else:
        ctx.log("unknown json element: " + json.dumps(el)[:100])


def render(elements, width, height, fonts, image_loader=None, log=lambda msg: None, variables=None):
    """Draw a /jsonupload element list; returns (image, rotatebuffer_toggled).

    The image is RGB and in the final buffer orientation: after an odd number of quarter turns by "rotate"
    elements it is height x width, and rotatebuffer_toggled is True, meaning the caller must flip the tag's
    rotatebuffer (rotatebuffer = 1 - rotatebuffer % 2) as rotateBuffer() does on the AP. image_loader(name)
    returns the bytes of a file named in an "image" element, or None. variables fill {name} placeholders in text.
    """
    if isinstance(elements, (str, bytes)):
        try:
            elements = json.loads(elements)
        except ValueError as e:
            log(f"json error {e}")
            elements = []
    if isinstance(elements, dict):
        elements = [elements]
    ctx = _Ctx(Image.new("RGB", (width, height), (255, 255, 255)), fonts, image_loader, log, variables)
    for el in elements if isinstance(elements, list) else []:
        if not isinstance(el, dict):
            log("json element is not an object: " + repr(el)[:100])
            continue
        try:
            draw_element(ctx, el)
        except Exception as e:
            log(f"json element {json.dumps(el)[:100]} failed: {e}")
    return ctx.img, ctx.toggled
