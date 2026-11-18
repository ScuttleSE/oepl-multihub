"""Electricity price chart for a tag: the next 24 hours from a Nordpool-style price list, the current price
large, expensive periods in the tag's accent colour and the cheapest coming hour in the footer."""
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from .imageenc import TagType
from .testimage import _fit_font, _font, _lum, _text

SUBUNIT = {"SEK": "öre", "NOK": "øre", "DKK": "øre", "EUR": "cent"}


def parse_prices(attrs: dict):
    """[(start datetime, value per kWh in the main unit)] from a Nordpool sensor's attributes, sorted.
    Takes raw_today/raw_tomorrow ({start, value}); values in cents when price_in_cents is set."""
    out = []
    for key in ("raw_today", "raw_tomorrow"):
        for p in attrs.get(key) or []:
            try:
                start = datetime.fromisoformat(str(p["start"]))
                value = float(p["value"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((start, value))
    if attrs.get("price_in_cents"):
        out = [(s, v / 100) for s, v in out]
    out.sort(key=lambda x: x[0])
    return out


def currency_of(attrs: dict):
    cur = str(attrs.get("currency") or "").upper()
    if not cur:
        uom = str(attrs.get("unit_of_measurement") or "")
        cur = uom.split("/")[0].strip().upper()
    return cur


def render(tt: TagType, prices, now: datetime, currency="SEK", rotate=0, title="") -> Image.Image:
    w, h = (tt.height, tt.width) if rotate % 2 else (tt.width, tt.height)
    colors = list(tt.colortable) or [(255, 255, 255), (0, 0, 0)]
    light = max(colors, key=_lum)
    dark = min(colors, key=_lum)
    accents = [c for c in colors if c not in (light, dark)]
    accent = accents[0] if accents else dark
    unit = SUBUNIT.get(currency, currency.lower() or "")
    mult = 100 if currency in SUBUNIT else 1

    img = Image.new("RGB", (w, h), light)
    d = ImageDraw.Draw(img)
    pad = max(3, w // 60)

    # the slots to show: from the current one, at most 24 h
    if now.tzinfo is None:
        now = now.astimezone()
    step = prices[1][0] - prices[0][0] if len(prices) > 1 else timedelta(hours=1)
    slots = [(s, v * mult) for s, v in prices if s + step > now and s < now + timedelta(hours=24)]
    if not slots:
        _text(d, (0, 0, w, h), "No prices", _font(max(10, h // 10)), dark)
        return img
    cur = slots[0][1]
    values = [v for _, v in slots]
    avg = sum(values) / len(values)
    expensive = avg + (max(values) - avg) * 0.35

    # header: current price, title and time
    head = max(16, h * 22 // 100)
    num = f"{cur:.0f}" if mult > 1 else f"{cur:.2f}"
    fbig = _fit_font(d, num, w * 45 // 100, head - pad, bold=True)
    l, t, r, b = d.textbbox((0, 0), num, font=fbig)
    d.text((pad - l, pad - t), num, font=fbig, fill=accent if cur >= expensive else dark)
    fu = _font(max(9, (head - pad) * 3 // 10))
    d.text((pad + r - l + pad, pad + (b - t) - d.textbbox((0, 0), unit, font=fu)[3]), f"{unit}/kWh", font=fu, fill=dark)
    fs = _font(max(9, head // 4))
    _text(d, (w // 2, pad, w - pad, pad + head // 2), title or "Electricity price", fs, dark, "right")
    _text(d, (w // 2, pad + head // 2, w - pad, head), f"{now:%H:%M}  ·  avg {avg:.0f}", fs, dark, "right")

    # footer: the cheapest whole hour from now
    foot = max(12, h * 11 // 100)
    per_hour = max(1, round(timedelta(hours=1) / step))
    best = None
    for i in range(0, max(1, len(slots) - per_hour + 1)):
        win = slots[i:i + per_hour]
        if len(win) < per_hour:
            break
        m = sum(v for _, v in win) / per_hour
        if best is None or m < best[1]:
            best = (win[0][0], m)
    if best:
        txt = f"Cheapest hour {best[0]:%H:%M}–{best[0] + timedelta(hours=1):%H:%M}  ·  {best[1]:.0f} {unit}"
        _text(d, (pad, h - foot, w - pad, h - 2), txt, _fit_font(d, txt, w - 2 * pad, (foot - 4) * 3 // 4), dark)

    # chart area with a y axis on the left and hour labels below
    fl = _font(max(8, h // 22))
    lab_w = d.textbbox((0, 0), "8888", font=fl)[2] + pad
    x0, x1 = pad + lab_w, w - pad
    y_top, y_bot = head + pad * 2, h - foot - (d.textbbox((0, 0), "00", font=fl)[3] + pad * 2)
    lo = min(0, min(values))
    hi = max(values) * 1.05 or 1
    # gridline step: about 4 lines
    raw = (hi - lo) / 4
    mag = 10 ** len(str(int(max(1, raw)))) / 10
    gstep = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    hi = max(hi, gstep)

    def y_of(v):
        return y_bot - (v - lo) / (hi - lo) * (y_bot - y_top)

    g = 0
    while g <= hi:
        y = round(y_of(g))
        for x in range(x0, x1, 4):
            d.point((x, y), fill=dark)
        _text(d, (pad, y - 8, x0 - pad, y + 8), f"{g:.0f}", fl, dark, "right")
        g += gstep
    d.line([x0, y_bot, x1, y_bot], fill=dark)

    n = 24 * per_hour
    bw = (x1 - x0) / n
    for i, (s, v) in enumerate(slots):
        bx0, bx1 = x0 + i * bw, x0 + (i + 1) * bw - (1 if bw >= 3 else 0)
        top = y_of(max(v, 0))
        d.rectangle([round(bx0), round(top), max(round(bx0), round(bx1) - 1), round(y_of(0))],
                    fill=accent if v >= expensive else dark)
        if s.minute == 0 and s.hour == 0 and i:  # midnight
            for y in range(y_top, y_bot, 3):
                d.point((round(bx0) - 1, y), fill=dark)
        if s.minute == 0 and s.hour % 3 == 0:
            d.line([round(bx0), y_bot, round(bx0), y_bot + pad], fill=dark)
            _text(d, (round(bx0) - 20, y_bot + pad, round(bx0) + 20, y_bot + pad * 2 + 12), f"{s:%H}", fl, dark)
    # the current slot: a marker above it
    mx = round(x0 + bw / 2)
    my = round(y_of(max(cur, 0))) - 3
    d.polygon([(mx - 4, my - 6), (mx + 4, my - 6), (mx, my)], fill=accent)
    return img
