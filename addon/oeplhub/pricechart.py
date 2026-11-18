"""Electricity price chart: a port of the stock AP's day-ahead content (content_dayahead.inl, content 27),
with prices from a Home Assistant Nordpool sensor instead of the ENTSO-E feed. Layout from the tag type's
template "27" (or its usetemplate type), percentile bar colours, the "now" arrow and the cheapest block per day."""
import math
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from . import jsondraw
from .imageenc import TagType

SUBUNIT = {"SEK": "öre", "NOK": "øre", "DKK": "øre", "EUR": "cent"}

TARGET_BARWIDTH = 5.0
MIN_LABEL_SPACING = 30
DASH_LENGTH, GAP_LENGTH = 3, 2
STEM_WIDTH, STEM_HEIGHT = 3, 10
ARROW_WIDTH, ARROW_HEIGHT = 8, 6
GAP_AFTER_ARROW = 2
CHEAP_BLOCK_HOURS = 3.0
# for tag types without a template 27 anywhere in their usetemplate chain: 0x02's, scaled
FALLBACK = {"bars": [15, 385, 225, 20, 22], "time": ["calibrib16.vlw"], "yaxis": ["tahoma9.vlw", 3, 6],
            "head": ["calibrib30.vlw"]}


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


def template_for(tt: TagType, get_tagtype, depth=0):
    """getTemplate(27): the type's own template 27, else its usetemplate type's, else None."""
    t = (tt.template or {}).get("27")
    if t:
        return t
    if tt.usetemplate is not None and depth < 4:
        other = get_tagtype(int(tt.usetemplate))
        if other is not None:
            return template_for(other, get_tagtype, depth + 1)
    return None


def _y_axis_scale(lo, hi, divisions):
    rng = hi - lo
    if rng <= 0:
        rng = abs(hi) or 1
    step = rng / divisions
    mag = 10 ** math.floor(math.log10(step))
    sig = step / mag
    rounded = next((t for t in (1.0, 2.0, 5.0, 10.0) if sig <= t), 10.0) * mag
    y_min = math.floor(lo / rounded) * rounded
    return y_min, y_min + rounded * divisions, rounded


def _map(v, a, b, c, d):
    return int(round((v - a) / (b - a) * (d - c) + c)) if b != a else c


def _data_range(times, now, bar_area_width):
    """calculateDataRange() without averaging: (start, end, pixels per bar)."""
    n = len(times)
    hour_ago = 0
    for i in range(n - 1, -1, -1):
        if times[i] <= now - timedelta(hours=1):
            hour_ago = i
            break
    first_needed, end = hour_ago, n
    needed = end - first_needed
    ppb = int(TARGET_BARWIDTH) + 1
    while ppb > 1 and bar_area_width // ppb < needed:
        ppb -= 1
    num = bar_area_width // ppb
    if num < needed:
        end = first_needed + num
    while num > end and bar_area_width // (ppb + 1) >= needed:
        ppb += 1
        num = bar_area_width // ppb
    num = min(num, end)
    return end - num, end, ppb


def _percentile_color(sorted_prices, price, tt: TagType):
    if tt.bpp in (3, 4):
        colors, bounds = ["blue", "green", "yellow", "orange", "red"], [20.0, 50.0, 70.0, 90.0]
    else:
        colors, bounds = ["black", "darkgray", "pink", "red"], [40.0, 80.0, 90.0]
        if tt.highlight_color == 3:
            colors[2:] = ["brown", "yellow"]
    idx = len(colors) - 1
    for i, b in enumerate(bounds):
        if price < sorted_prices[int(len(sorted_prices) * b / 100)]:
            idx = i
            break
    return jsondraw.get_color(colors[idx])


def _cheap_blocks(times, prices, step, now, bar_times):
    """findCheapestBlockPerDay(): per day (today, tomorrow) the bar index range of the cheapest block."""
    out = []
    block = int(CHEAP_BLOCK_HOURS * 3600 / step.total_seconds() + 0.5)
    if block <= 0 or block > len(times):
        return out
    for day in (0, 1):
        day_start = (now + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        best, best_start = None, None
        for i in range(0, len(times) - block + 1):
            if times[i] < day_start:
                continue
            if times[i + block - 1] + step > day_end:
                break
            if times[i + block - 1] - times[i] != step * (block - 1):
                continue
            s = sum(prices[i:i + block])
            if best is None or s < best:
                best, best_start = s, times[i]
        if best_start is None:
            continue
        best_end = best_start + step * block
        idx = [b for b, t in enumerate(bar_times) if t + step > best_start and t < best_end]
        if idx:
            out.append((idx[0], idx[-1]))
    return out


def render(tt: TagType, prices, now: datetime, currency="SEK", fonts=None, template=None, rotate=0) -> Image.Image:
    w, h = (tt.height, tt.width) if rotate % 2 else (tt.width, tt.height)
    loc = template
    if not loc:
        sx, sy = w / 400, h / 300
        b = FALLBACK["bars"]
        loc = dict(FALLBACK, bars=[round(b[0] * sx), round(b[1] * sx), round(b[2] * sy), b[3], b[4]])
    black, white = jsondraw.get_color("black"), jsondraw.get_color("white")
    img = Image.new("RGB", (w, h), white)
    d = ImageDraw.Draw(img)
    fonts = fonts or jsondraw.Fonts("/tmp/oepl-fonts")

    def font(name):
        return fonts.get(name) if name else None

    def text(s, x, y, fname, align):
        f = font(fname)
        if isinstance(f, jsondraw.VlwFont):
            f.draw(img, s, int(x), int(y), align, black, white)

    if now.tzinfo is None:
        now = now.astimezone()
    mult = 100 if currency in SUBUNIT else 1
    if len(prices) < 2:
        text("No prices", w // 2, h // 2 - 8, "calibrib16.vlw", jsondraw.TC)
        return img
    step = prices[1][0] - prices[0][0]
    if step <= timedelta(0):
        step = timedelta(minutes=15)
    times = [t for t, _ in prices]
    values = [v * mult for _, v in prices]
    current = next((i for i in range(len(times) - 1, -1, -1) if times[i] <= now < times[i] + step), None)
    if current is None:
        text("No price for now", w // 2, h // 2 - 8, "calibrib16.vlw", jsondraw.TC)
        return img

    bar_x, bar_area, bar_h, bar_bottom = (int(v) for v in loc["bars"][:4])
    arrow_y = int(loc["bars"][4]) if len(loc["bars"]) >= 5 else 0
    start, end, _ = _data_range(times, now, bar_area)
    bt, bv = times[start:end], values[start:end]
    n = len(bv)
    lo, hi = min(bv), max(bv)
    srt = sorted(bv)

    yaxis = loc.get("yaxis") or [None, 0, 6]
    lo, _, ystep = _y_axis_scale(lo, hi, int(yaxis[2]) if len(yaxis) > 2 else 6)  # bars: rounded min to real max
    y_x = int(yaxis[1]) if len(yaxis) > 1 else 0
    y_off = int(yaxis[3]) if len(yaxis) > 3 else 9
    g = lo
    while g <= hi + 1e-9:
        y = _map(g, lo, hi, h - bar_bottom, h - bar_bottom - bar_h)
        d.line([0, y, w, y], fill=black)
        if yaxis[0]:
            label = f"{g:.1f}" if hi < 10 else f"{int(g)}"
            text(label, y_x, y - y_off, yaxis[0], jsondraw.TL)
        g += ystep

    ppb = bar_area // n
    spacing = 1 if ppb >= 3 and step > timedelta(minutes=15) else 0
    bw = ppb - spacing

    max_labels = max(1, bar_area // MIN_LABEL_SPACING)
    label_interval = max(1, (n + max_labels - 1) // max_labels)
    per_hour = max(0, int(timedelta(hours=1) / step))
    hours_per_label = 0
    if per_hour > 0:
        hours_per_label = max(1, (label_interval + per_hour - 1) // per_hour)
        label_interval = hours_per_label * per_hour
        if n // label_interval > max_labels:
            label_interval = (n + max_labels - 1) // max_labels
            hours_per_label = 0

    baseline = h - bar_bottom
    darkgray, lightgray = jsondraw.get_color("darkgray"), jsondraw.get_color("lightgray")
    for a, b in _cheap_blocks(times, values, step, now, bt):
        d.rectangle([bar_x + a * (bw + spacing), baseline + 1,
                     bar_x + (b + 1) * (bw + spacing) - 1, baseline + 19], fill=lightgray)

    skip_ticks = h < 150
    tfont = (loc.get("time") or [None])[0]
    pointer = None
    for i, (t, v) in enumerate(zip(bt, bv)):
        th = _map(v, lo, hi, 0, bar_h)
        x = bar_x + i * (bw + spacing)
        if th > 0:
            d.rectangle([x, baseline - th, x + bw - 1, baseline - 1], fill=_percentile_color(srt, v, tt))
        lx = x + bw // 2
        here = (t.minute == 0 and t.hour % hours_per_label == 0) if hours_per_label else i % label_interval == 0
        if here and tfont:
            if not skip_ticks:
                d.line([lx, baseline, lx, baseline + 4], fill=black)
            text(str(t.hour), lx, baseline + (3 if skip_ticks else 6), tfont, jsondraw.TC)
        elif t.minute == 0 and not skip_ticks:
            d.line([lx, baseline, lx, baseline + 3], fill=darkgray)
        elif t.minute == 30 and not skip_ticks:
            d.point((lx, baseline + 1), fill=darkgray)
        if pointer is None and t <= now < t + step:
            pointer = (lx, th)

    if pointer:
        px, th = pointer
        d.rectangle([px - STEM_WIDTH // 2, 5 + arrow_y, px - STEM_WIDTH // 2 + STEM_WIDTH - 1, 5 + arrow_y + STEM_HEIGHT - 1],
                    fill=black)
        d.polygon([(px - ARROW_WIDTH // 2, 15 + arrow_y), (px + ARROW_WIDTH // 2, 15 + arrow_y),
                   (px, 15 + arrow_y + ARROW_HEIGHT)], fill=black)
        bar_top = baseline - th
        y = 21 + arrow_y + GAP_AFTER_ARROW
        while y < bar_top - 2:
            d.line([px, y, px, min(y + DASH_LENGTH, bar_top - 2)], fill=black)
            y += DASH_LENGTH + GAP_LENGTH
        d.line([px, baseline, px, h], fill=black)

    t_now = times[current]
    price_now = f"{values[current]:.2f}/kWh"
    head = (loc.get("head") or ["calibrib30.vlw"])[0]
    if bw < 5:
        text(f"{t_now.hour}:{t_now.minute:02d}", w // 2, 5, "calibrib16.vlw", jsondraw.TC)
        text(price_now, w // 2, 25, head, jsondraw.TC)
    else:
        text(f"{t_now.hour}:{t_now.minute:02d}", bar_x, 5, head, jsondraw.TL)
        text(price_now, w - bar_x, 5, head, jsondraw.TR)
    return img
