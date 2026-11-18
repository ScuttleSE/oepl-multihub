"""A test image for any tag type: border, corner labels, left/right halves, one bar per palette colour and
fine line patterns. Drawn at the tag's size (swapped for rotate 1/3) from its colour table."""
from PIL import Image, ImageDraw, ImageFont

from .imageenc import TagType


_FONT_DIRS = ("/usr/share/fonts/dejavu", "/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu")


def _font(size, bold=False):
    """DejaVu Sans (Alpine: font-dejavu) for å/ä/ö and dashes; Pillow's own font otherwise."""
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        try:
            return ImageFont.truetype(f"{d}/{name}", max(8, int(size)))
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except TypeError:  # Pillow < 10.1: one small bitmap font
        return ImageFont.load_default()


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _fit_font(draw, text, max_w, max_h, bold=False):
    size = max_h
    while size > 8:
        f = _font(size, bold)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= max_w and b - t <= max_h:
            return f
        size -= 1
    return _font(8)


def _text(draw, box, text, font, fill, align="center"):
    x0, y0, x1, y1 = box
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    w, h = r - l, b - t
    if align == "left":
        x = x0
    elif align == "right":
        x = x1 - w
    else:
        x = x0 + (x1 - x0 - w) // 2
    draw.text((x - l, y0 + (y1 - y0 - h) // 2 - t), text, font=font, fill=fill)


def render(tt: TagType, rotate=0, title="") -> Image.Image:
    w, h = (tt.height, tt.width) if rotate % 2 else (tt.width, tt.height)
    colors = list(zip(tt.colornames, tt.colortable)) or [("white", (255, 255, 255)), ("black", (0, 0, 0))]
    light = max(colors, key=lambda c: _lum(c[1]))[1]
    dark = min(colors, key=lambda c: _lum(c[1]))[1]
    accents = [c for _, c in colors if c not in (light, dark)]
    acc1 = accents[0] if accents else dark
    acc2 = accents[1] if len(accents) > 1 else acc1

    img = Image.new("RGB", (w, h), light)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=dark)

    margin = max(2, w // 13)
    head = max(10, h * 17 // 100)
    foot = max(10, h * 22 // 100)
    corner_w = max(12, w // 9)

    # header: TL, LEFT | RIGHT, TR
    f = _fit_font(d, "TR", corner_w, head - 4)
    _text(d, (3, 2, 3 + corner_w, head), "TL", f, dark, "left")
    _text(d, (w - 4 - corner_w, 2, w - 4, head), "TR", f, acc1, "right")
    mid = w // 2
    d.line([mid, 2, mid, head - 2], fill=acc1)
    fl = _fit_font(d, "RIGHT", mid - corner_w - 8, head * 2 // 3)
    _text(d, (corner_w + 4, 2, mid - 2, head), "LEFT", fl, dark)
    _text(d, (mid + 2, 2, w - corner_w - 4, head), "RIGHT", fl, acc1)

    # the tag's name (or MAC) on its own line
    top, bottom = head + max(1, h // 60), h - foot
    if title:
        title_h = max(10, (bottom - top) // 5)
        ft = _fit_font(d, title, w - 2 * margin, title_h - 2)
        _text(d, (margin, top, w - margin, top + title_h), title, ft, dark)
        top += title_h

    # one bar per colour, the name on both halves
    n = len(colors)
    gap = max(1, (bottom - top) // (n * 8))
    bar_h = max(4, (bottom - top - gap * (n - 1)) // n)
    longest = max((name.upper() for name, _ in colors), key=len)
    fb = _fit_font(d, longest, (w - 2 * margin) // 2 - max(8, w // 20), max(8, bar_h * 2 // 3))
    y = top
    for name, c in colors:
        x0, x1 = margin, w - margin - 1
        d.rectangle([x0, y, x1, y + bar_h - 1], fill=c)
        if c == light:
            d.line([x0, y + bar_h - 1, x1, y + bar_h - 1], fill=dark)
        ink = light if _lum(c) < 128 else dark
        _text(d, (x0, y, mid, y + bar_h), name.upper(), fb, ink)
        _text(d, (mid, y, x1, y + bar_h), name.upper(), fb, ink)
        y += bar_h + gap

    # footer: BL, line patterns, BR
    fy = bottom + max(1, foot // 10)
    _text(d, (3, fy, 3 + corner_w, h - 3), "BL", f, acc2, "left")
    _text(d, (w - 4 - corner_w, fy, w - 4, h - 3), "BR", f, dark, "right")
    d.line([3, fy, 3 + corner_w, fy], fill=dark)
    px0, px1 = corner_w + 10, w - corner_w - 10
    pm = (px0 + px1) // 2
    py0, py1 = h - 3 - max(4, foot // 3), h - 4
    for yy in range(py0, py1 + 1, 2):  # horizontal lines, 1 px apart
        d.line([px0, yy, pm - 6, yy], fill=dark)
    for xx in range(pm + 6, px1 + 1, 2):  # vertical lines, 1 px apart
        d.line([xx, py0, xx, py1], fill=dark)
    return img
