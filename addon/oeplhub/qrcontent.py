"""Content mode 10 "QR code": a static QR code, built from typed fields (URL, Wi-Fi, contact, ...)."""

from PIL import Image, ImageDraw, ImageFont

from .tagtypes import TagType

FONT_FILES = ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _esc(s: str, chars: str) -> str:
    return "".join("\\" + c if c in chars else c for c in str(s))


def _ics_time(s: str) -> str:
    """"2026-11-18T19:30" (datetime-local) -> "20261118T193000"."""
    s = (s or "").replace("-", "").replace(":", "")
    return (s + "00")[:15] if "T" in s else s[:8]


def payload(cfg: dict) -> str:
    """The text encoded in the QR code."""
    g = lambda k: str(cfg.get(k) or "").strip()  # noqa: E731
    kind = cfg.get("kind", "url")
    if kind == "url":
        u = g("url")
        return u if "://" in u or not u else "https://" + u
    if kind == "wifi":
        sec = g("security") or "WPA"
        p = f"WIFI:T:{sec};S:{_esc(g('ssid'), chr(92) + ';,:' + chr(34))};"
        if sec != "nopass":
            p += f"P:{_esc(g('password'), chr(92) + ';,:' + chr(34))};"
        if cfg.get("hidden"):
            p += "H:true;"
        return p + ";"
    if kind == "contact":
        esc = lambda s: _esc(s, "\\;,")  # noqa: E731
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{esc(g('name'))}"]
        parts = g("name").rsplit(" ", 1)
        lines.append(f"N:{esc(parts[-1])};{esc(parts[0]) if len(parts) > 1 else ''};;;")
        for key, tag in (("org", "ORG"), ("phone", "TEL"), ("email", "EMAIL"), ("url", "URL")):
            if g(key):
                lines.append(f"{tag}:{esc(g(key)) if key == 'org' else g(key)}")
        return "\n".join(lines + ["END:VCARD"])
    if kind == "phone":
        return "tel:" + g("phone").replace(" ", "")
    if kind == "sms":
        return f"SMSTO:{g('phone').replace(' ', '')}:{g('message')}"
    if kind == "email":
        from urllib.parse import quote
        q = "&".join(f"{k}={quote(g(k))}" for k in ("subject", "body") if g(k))
        return f"mailto:{g('to')}" + ("?" + q if q else "")
    if kind == "location":
        return f"geo:{g('lat')},{g('lon')}"
    if kind == "event":
        lines = ["BEGIN:VEVENT", f"SUMMARY:{g('summary')}", f"DTSTART:{_ics_time(g('start'))}"]
        if g("end"):
            lines.append(f"DTEND:{_ics_time(g('end'))}")
        if g("location"):
            lines.append(f"LOCATION:{g('location')}")
        return "\n".join(lines + ["END:VEVENT"])
    return g("text")


def caption_lines(cfg: dict) -> list:
    """Readable text next to the code: the caption, plus the useful fields for the type."""
    g = lambda k: str(cfg.get(k) or "").strip()  # noqa: E731
    out = [g("caption")] if g("caption") else []
    kind = cfg.get("kind", "url")
    if kind == "wifi" and cfg.get("showpass", True):
        out += [f"Wi-Fi: {g('ssid')}"] + ([f"Password: {g('password')}"] if g("security") != "nopass" else [])
    elif kind == "url" and not out:
        out.append(g("url"))
    elif kind in ("phone", "sms") and not out:
        out.append(g("phone"))
    elif kind == "contact" and not out:
        out += [s for s in (g("name"), g("phone"), g("email")) if s]
    elif kind == "event" and not out:
        out += [s for s in (g("summary"), g("start").replace("T", " ")) if s]
    return out


def _font(size):
    for p in FONT_FILES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit_lines(d, lines, max_w, max_h):
    """The largest font size (up to 40) at which every line fits max_w and all lines fit max_h."""
    for size in range(40, 9, -2):
        f = _font(size)
        lh = int(size * 1.25)
        if lh * len(lines) <= max_h and all(d.textlength(s, font=f) <= max_w for s in lines):
            return f, lh
    f = _font(10)
    return f, 13


def _wrap(d, text, font, max_w):
    out, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) > max_w and cur:
            out.append(cur)
            cur = ""
        cur += ch
    return out + [cur] if cur else out


def render(tt: TagType, cfg: dict, rotate=0) -> Image.Image:
    import segno
    w, h = (tt.height, tt.width) if rotate % 2 else (tt.width, tt.height)
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    data = payload(cfg)
    if not data:
        d.text((w // 2, h // 2), "No QR data", fill=(0, 0, 0), font=_font(20), anchor="mm")
        return img
    qr = segno.make(data, error="m", boost_error=True)
    matrix = [list(row) for row in qr.matrix]
    n = len(matrix) + 8  # 4-module quiet zone each side
    lines = caption_lines(cfg)
    side = bool(lines) and w >= h * 1.4  # wide panel: code left, text right
    if side:
        mod = max(1, h // n)
    elif lines:
        mod = max(1, min(w, h - min(h // 4, 60)) // n)
    else:
        mod = max(1, min(w, h) // n)
    size = mod * n
    if side:
        x0, y0 = 0, (h - size) // 2
    else:
        x0, y0 = (w - size) // 2, 0 if lines else (h - size) // 2
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            if v:
                x, y = x0 + (c + 4) * mod, y0 + (r + 4) * mod
                d.rectangle([x, y, x + mod - 1, y + mod - 1], fill=(0, 0, 0))
    if lines:
        if side:
            tx, tw, ty, th = size, w - size - mod * 2, mod * 4, h - mod * 8
        else:
            tx, tw, ty, th = mod * 2, w - mod * 4, size, h - size
        f, lh = _fit_lines(d, lines, tw, th)
        wrapped = [p for s in lines for p in _wrap(d, s, f, tw)][:max(1, th // lh)]
        y = ty + max(0, (th - lh * len(wrapped)) // 2)
        for s in wrapped:
            if side:
                d.text((tx, y), s, fill=(0, 0, 0), font=f)
            else:
                d.text((w // 2, y), s, fill=(0, 0, 0), font=f, anchor="ma")
            y += lh
    return img
