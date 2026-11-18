"""Image encoding for OEPL tags, as the stock AP does it (makeimage.cpp, colorquant.h, contentmanager.cpp).

Pipeline: JPEG -> RGB565 (like the AP's sprite) -> palette indices (dither 0 none, 1 Burkes, 2 ordered)
-> bit planes (bpp 1/2) or packed indices (bpp 3/4) -> file (raw, or zlib with a 4 KB window).

dataVer is the first 8 bytes of the file's MD5, little-endian, as prepareDataAvail() does.
"""

import hashlib
import io
import json
import struct
import zlib

DATATYPE_IMG_RAW_1BPP = 0x20
DATATYPE_IMG_RAW_2BPP = 0x21
DATATYPE_IMG_RAW_3BPP = 0x22
DATATYPE_IMG_RAW_4BPP = 0x23
DATATYPE_IMG_ZLIB = 0x30
DATATYPE_IMG_G5 = 0x31

UINT32_MAX = 0xFFFFFFFF


class TagType:
    def __init__(self, d: dict):
        self.name = d.get("name", "")
        self.width = d["width"]
        self.height = d["height"]
        self.bpp = d["bpp"]
        self.rotatebuffer = d.get("rotatebuffer", 0)
        # palette order is the order in the JSON: index 0, 1, 2, 3 ... (white, black, red, yellow for 0x94)
        self.colortable = [tuple(v) for v in d.get("colortable", {}).values()]
        self.colornames = list(d.get("colortable", {}).keys())
        self.zlib = int(d["zlib_compression"], 16) if isinstance(d.get("zlib_compression"), str) else 0
        self.g5 = int(d["g5_compression"], 16) if isinstance(d.get("g5_compression"), str) else 0
        self.shortlut = d.get("shortlut", 0)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))


# --- colour quantisation (colorquant.h) ---------------------------------------------------------

def _distance(r, g, b, pixel_grey, p, p_coloured):
    if pixel_grey and p_coloured:
        return UINT32_MAX
    dr, dg, db = r - p[0], g - p[1], b - p[2]
    return (300 * dr * dr + 547 * dg * dg + 153 * db * db) // 100


def _is_grey(r, g, b):
    return abs(r - g) < 20 and abs(b - g) < 20


def _is_coloured(c):
    return abs(c[0] - c[1]) > 20 or abs(c[2] - c[1]) > 20


def _ordered_mix(r, g, b, palette, coloured):
    grey = _is_grey(r, g, b)
    closest = second = -1
    cd = sd = UINT32_MAX
    cset = sset = False
    for i, p in enumerate(palette):
        d = _distance(r, g, b, grey, p, coloured[i])
        if not cset or d < cd:
            second, sd, sset = closest, cd, cset
            closest, cd, cset = i, d, True
        elif not sset or d < sd:
            second, sd, sset = i, d, True
    if closest != -1 and second != -1:
        a, c = palette[second], palette[closest]
        if ((a[0] << 16) | (a[1] << 8) | a[2]) > ((c[0] << 16) | (c[1] << 8) | c[2]):
            closest, second = second, closest
            cd, sd = sd, cd
    total = cd + sd
    weight = cd / total if total else 0.0
    if weight <= 0.03:
        level = 0
    elif weight < 0.30:
        level = 1
    elif weight < 0.70:
        level = 2
    elif weight < 0.97:
        level = 3
    else:
        level = 4
    return closest, second, level


def rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _expand565(v):
    return (((v >> 8) & 0xF8) | ((v >> 13) & 0x07), ((v >> 3) & 0xFC) | ((v >> 9) & 0x03),
            ((v << 3) & 0xF8) | ((v >> 2) & 0x07))


def quantize(pixels565, w, h, palette, dither=2, rotate=0, exact=None):
    """Palette indices, row-major, for a w x h RGB565 image (list of ints). rotate 0..3 as in QuantParams.

    Returns (indices, bufw, bufh)."""
    bufw, bufh = (h, w) if rotate % 2 else (w, h)
    coloured = [_is_coloured(c) for c in palette]
    exact = exact if exact is not None else [rgb565(*c) for c in palette]
    n = len(palette)
    out = bytearray(bufw * bufh)

    def read(x, y):
        if rotate == 0:
            return pixels565[y * w + x]
        if rotate == 1:
            return pixels565[(bufw - 1 - x) * w + y]
        if rotate == 2:
            return pixels565[(bufh - 1 - y) * w + (bufw - 1 - x)]
        return pixels565[x * w + (bufh - 1 - y)]

    err_old = [[0, 0, 0] for _ in range(bufw + 4)] if dither == 1 else None
    err_new = [[0, 0, 0] for _ in range(bufw + 4)] if dither == 1 else None
    cache_rgb, cache_exact = -1, -1
    cache_mix_rgb, cache_mix = -1, (0, 0, 0)

    for y in range(bufh):
        reverse = err_old is not None and (y % 2) == 1
        d = -1 if reverse else 1
        if err_new is not None:
            for e in err_new:
                e[0] = e[1] = e[2] = 0
        row = y * bufw
        for i in range(bufw):
            x = bufw - 1 - i if reverse else i
            v = read(x, y)
            r, g, b = _expand565(v)
            if v != cache_rgb:
                cache_rgb = v
                cache_exact = exact.index(v) if v in exact else -1
            best = 0
            er = eg = eb = 0
            if cache_exact >= 0:
                best = cache_exact
            elif dither == 2:
                if v != cache_mix_rgb:
                    cache_mix = _ordered_mix(r, g, b, palette, coloured)
                    cache_mix_rgb = v
                c1, c2, level = cache_mix
                checker = (x + y) % 2
                sparse = (y % 2) and ((y // 2 + x) % 2)
                best = (c1, c2 if sparse else c1, c2 if checker else c1, c1 if sparse else c2, c2)[level]
            else:
                grey = _is_grey(r, g, b)
                if err_old is not None:
                    o = err_old[x + 2]
                    r2, g2, b2 = r + ((o[0] + 16) >> 5), g + ((o[1] + 16) >> 5), b + ((o[2] + 16) >> 5)
                else:
                    r2, g2, b2 = r, g, b
                best_d = _distance(r2, g2, b2, grey, palette[0], coloured[0])
                for c in range(1, n):
                    if best_d == 0:
                        break
                    dd = _distance(r2, g2, b2, grey, palette[c], coloured[c])
                    if dd < best_d:
                        best_d, best = dd, c
                if err_old is not None:
                    pc = palette[best]
                    er, eg, eb = r2 - pc[0], g2 - pc[1], b2 - pc[2]
                    m = max(abs(er), abs(eg), abs(eb))
                    if m > 255:
                        s = 255.0 / m
                        er, eg, eb = int(er * s), int(eg * s), int(eb * s)
            out[row + x] = best
            if err_old is not None and (er or eg or eb):
                for t, wgt in ((err_old[x + 2 + d], 8), (err_old[x + 2 + 2 * d], 4), (err_new[x], 2),
                               (err_new[x + 1], 4), (err_new[x + 2], 8), (err_new[x + 3], 4), (err_new[x + 4], 2)):
                    t[0] += er * wgt
                    t[1] += eg * wgt
                    t[2] += eb * wgt
        if err_old is not None:
            err_old, err_new = err_new, err_old
    return out, bufw, bufh


# --- buffers and files (makeimage.cpp) ----------------------------------------------------------

def bit_planes(indices, bufw, bufh):
    """Black plane (index 1 or 3) and red plane (index 2 or 3), MSB first, no row padding."""
    size = (bufw * bufh + 7) // 8
    p0, p1 = bytearray(size), bytearray(size)
    for i, c in enumerate(indices):
        if c:
            mask = 0x80 >> (i % 8)
            if c & 1:
                p0[i // 8] |= mask
            if c & 2:
                p1[i // 8] |= mask
    return bytes(p0), bytes(p1)


def packed(indices, bits):
    out = bytearray((len(indices) * bits + 7) // 8)
    pos = 0
    for c in indices:
        for b in range(bits - 1, -1, -1):
            if (c >> b) & 1:
                out[pos // 8] |= 0x80 >> (pos % 8)
            pos += 1
    return bytes(out)


def image_header(bufw, bufh, bpp, rotatebuffer=0):
    h = bytearray(6)
    h[0] = 6
    if rotatebuffer % 2:
        struct.pack_into("<HH", h, 1, bufh, bufw)
    else:
        struct.pack_into("<HH", h, 1, bufw, bufh)
    h[5] = bpp
    return bytes(h)


def zlib_file(header, body):
    """[u32 uncompressed size][zlib stream with a 4 KB window: header + body]. The tag inflates with 4 KB."""
    raw = header + body
    c = zlib.compressobj(9, zlib.DEFLATED, 12, 9)
    return struct.pack("<I", len(raw)) + c.compress(raw) + c.flush()


def encode(img, tagtype: TagType, dither=2, rotate=0, tag_fw=0, invert=False):
    """A PIL image to (dataType, file bytes) for this tag type and tag firmware version."""
    w, h = img.size
    rgb = img.convert("RGB")
    if hasattr(rgb, "get_flattened_data"):
        data = rgb.get_flattened_data()
    else:
        data = rgb.getdata()
    pixels = [rgb565(r, g, b) for (r, g, b) in data]

    palette = list(tagtype.colortable)
    exact = [rgb565(*c) for c in palette]
    if invert and len(palette) >= 2:
        palette[0], palette[1] = palette[1], palette[0]
        exact[0], exact[1] = exact[1], exact[0]
    if tagtype.rotatebuffer % 2:
        params_rotate = ((rotate + 3) % 4 + (tagtype.rotatebuffer - 1)) % 4
    else:
        params_rotate = (rotate + tagtype.rotatebuffer) % 4
    indices, bufw, bufh = quantize(pixels, w, h, palette, dither=dither, rotate=params_rotate, exact=exact)

    bpp = tagtype.bpp
    if bpp in (3, 4):
        body = packed(indices, bpp)
        return (DATATYPE_IMG_RAW_3BPP if bpp == 3 else DATATYPE_IMG_RAW_4BPP), body
    p0, p1 = bit_planes(indices, bufw, bufh)
    has_red = bpp > 1 and any(c >= 2 for c in indices)
    use_zlib = tagtype.zlib and tag_fw >= tagtype.zlib
    if use_zlib:
        hdr = image_header(bufw, bufh, 2 if has_red else 1, tagtype.rotatebuffer)
        return DATATYPE_IMG_ZLIB, zlib_file(hdr, p0 + (p1 if has_red else b""))
    # G5 is not implemented yet: tag types with g5_compression get raw (the AP falls back to raw too)
    if has_red:
        return DATATYPE_IMG_RAW_2BPP, p0 + p1
    return DATATYPE_IMG_RAW_1BPP, p0


def encode_jpeg(jpeg: bytes, tagtype: TagType, **kw):
    from PIL import Image
    return encode(Image.open(io.BytesIO(jpeg)), tagtype, **kw)


def decode(data_type: int, data: bytes, tagtype: TagType, rotate=0):
    """A file as encode() makes it, back to an RGB PIL image the size of the source. None if not decodable."""
    from PIL import Image
    if tagtype.rotatebuffer % 2:
        p = ((rotate + 3) % 4 + (tagtype.rotatebuffer - 1)) % 4
    else:
        p = (rotate + tagtype.rotatebuffer) % 4
    bufw, bufh = (tagtype.height, tagtype.width) if p % 2 else (tagtype.width, tagtype.height)
    n = bufw * bufh
    plane = (n + 7) // 8
    if data_type == DATATYPE_IMG_ZLIB:
        raw = zlib.decompress(data[4:])
        bpp = raw[5]
        body = raw[raw[0]:]
        planes = [body[:plane], body[plane:2 * plane] if bpp == 2 else b""]
    elif data_type in (DATATYPE_IMG_RAW_1BPP, DATATYPE_IMG_RAW_2BPP):
        planes = [data[:plane], data[plane:2 * plane] if data_type == DATATYPE_IMG_RAW_2BPP else b""]
    elif data_type in (DATATYPE_IMG_RAW_3BPP, DATATYPE_IMG_RAW_4BPP):
        planes = None
        bits = 3 if data_type == DATATYPE_IMG_RAW_3BPP else 4
    else:
        return None
    if planes is not None:
        b0 = Image.frombytes("1", (bufw, bufh), planes[0].ljust(plane, b"\x00")).convert("L")
        idx = b0.point(lambda v: 1 if v else 0)
        if planes[1]:
            b1 = Image.frombytes("1", (bufw, bufh), planes[1].ljust(plane, b"\x00")).convert("L")
            idx = Image.frombytes("L", (bufw, bufh),
                                  bytes(a | (2 if b else 0) for a, b in zip(idx.tobytes(), b1.tobytes())))
    else:
        out = bytearray(n)
        pos = 0
        for i in range(n):
            v = 0
            for _ in range(bits):
                v = (v << 1) | ((data[pos >> 3] >> (7 - (pos & 7))) & 1) if (pos >> 3) < len(data) else v << 1
                pos += 1
            out[i] = v
        idx = Image.frombytes("L", (bufw, bufh), bytes(out))
    im = Image.frombytes("P", (bufw, bufh), idx.tobytes())
    pal = [c for rgb in tagtype.colortable for c in rgb]
    im.putpalette(pal + [0] * (768 - len(pal)))
    im = im.convert("RGB")
    undo = {1: Image.Transpose.ROTATE_90, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_270}
    return im.transpose(undo[p]) if p else im


def data_ver(file_bytes: bytes) -> int:
    return struct.unpack("<Q", hashlib.md5(file_bytes).digest()[:8])[0]


def data_type_arg(lut=0, preload=False, special_type=0):
    """imageDataTypeArgStruct: lut:2, preloadImage:1, specialType:5 (LSB first)."""
    return (lut & 0x03) | ((1 if preload else 0) << 2) | ((special_type & 0x1F) << 3)
