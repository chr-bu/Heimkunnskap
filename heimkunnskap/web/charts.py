"""Charts drawn on the server as inline SVG.

The web app's Content-Security-Policy allows no scripts and no inline
styles, so every chart is plain SVG markup: colours come from classes in
static/style.css (light and dark), and each mark carries a <title> that the
browser shows on hover. Every chart has a table or a line of figures beside
it with the same numbers.
"""

from __future__ import annotations

import math
from datetime import date
from html import escape

from markupsafe import Markup

W, H = 720, 260
LEFT, RIGHT, TOP, BOTTOM = 64, 176, 14, 28
LABEL_GAP = 28
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def num(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def scale(value: float, target: int = 5) -> tuple[float, list[float]]:
    """An axis top at or above value and its ticks, about `target` steps of
    1, 2, 2.5 or 5 times a power of ten."""
    if value <= 0:
        return 1.0, [0.0, 1.0]
    raw = value / target
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    top = math.ceil(value / step - 1e-9) * step
    return top, [step * i for i in range(int(round(top / step)) + 1)]


def axis_label(value: float, unit: str) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 10_000:
        return f"{value / 1000:g}k"
    return num(value)


def progress(series: list[tuple[date, float]], start: date, end: date, today: date,
             milestones: list[dict], unit: str, projected: float | None = None,
             cap: float | None = None, label: str = "") -> Markup:
    """Cumulative progress through a period against milestone lines.

    series: (day, running total). milestones: dicts with value, reward and
    reached (a date or None). unit: "kr" or "points"."""
    total = series[-1][1] if series else 0.0
    marks = [m["value"] for m in milestones] + [total] + ([projected] if projected else [])
    if cap:
        marks.append(cap)
    top, grid = scale(max(marks) * 1.05)
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    days = max(1, (end - start).days)

    def x(d: date) -> float:
        return LEFT + plot_w * min(max((d - start).days, 0), days) / days

    def y(v: float) -> float:
        return TOP + plot_h * (1 - min(v, top) / top)

    out = [f'<svg class="viz" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">']
    # Grid and y axis
    for t in grid:
        out.append(f'<line class="viz-grid" x1="{LEFT}" x2="{LEFT + plot_w}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        out.append(f'<text class="viz-axis" x="{LEFT - 8}" y="{y(t) + 4:.1f}" text-anchor="end">'
                   f'{axis_label(t, unit)}</text>')
    # Month labels: the first of each month in the period
    m = date(start.year, start.month, 1)
    while m <= end:
        if m >= start:
            out.append(f'<text class="viz-axis" x="{x(m) + 2:.1f}" y="{H - 8}">{MONTHS[m.month - 1]}</text>')
            if m > start:
                out.append(f'<line class="viz-tick" x1="{x(m):.1f}" x2="{x(m):.1f}" '
                           f'y1="{TOP + plot_h}" y2="{TOP + plot_h + 4}"/>')
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    # Milestones (and the cap): reference lines with their reward at the right
    refs = [(mm["value"], mm["reward"], mm.get("reached")) for mm in milestones]
    if cap:
        refs.append((cap, "no points above", None))
    refs.sort(key=lambda r: r[0])
    # Labels two lines tall: keep them LABEL_GAP apart, moving upwards from
    # the lowest, and join a moved label to its line with a short leader
    placed = []
    for value, reward, when in refs:
        want = y(value)
        at = min(want, placed[-1] - LABEL_GAP) if placed else want
        placed.append(at)
    for (value, reward, when), at in zip(refs, placed):
        yy = y(value)
        cls = "viz-ref reached" if when else "viz-ref"
        out.append(f'<line class="{cls}" x1="{LEFT}" x2="{LEFT + plot_w}" y1="{yy:.1f}" y2="{yy:.1f}">'
                   f'<title>{escape(num(value))} {unit}: {escape(reward)}'
                   f'{", reached " + when.strftime("%d.%m.%Y") if when else ""}</title></line>')
        lx = LEFT + plot_w + 8
        if abs(at - yy) > 3:
            out.append(f'<polyline class="viz-leader" points="{LEFT + plot_w:.1f},{yy:.1f} '
                       f'{lx - 3:.1f},{at - 4:.1f}"/>')
        mark = "✓ " if when else ""
        out.append(f'<text class="viz-ref-label" x="{lx}" y="{at - 2:.1f}">'
                   f'{mark}{escape(num(value))}{" kr" if unit == "kr" else ""}</text>')
        sub = escape(reward) + (f", {when.strftime('%d.%m')}" if when else "")
        out.append(f'<text class="viz-ref-sub" x="{lx}" y="{at + 11:.1f}">{sub}</text>')
    if series:
        # Hover: one band per month with the total at its end
        m = date(start.year, start.month, 1)
        while m <= min(today, end):
            nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
            upto = min(today, end, nxt)
            value = ([v for d, v in series if d < nxt] or [0])[-1]
            x0, x1 = x(max(m, start)), x(upto)
            if x1 > x0:
                out.append(f'<rect class="viz-hit" x="{x0:.1f}" y="{TOP}" width="{x1 - x0:.1f}" '
                           f'height="{plot_h}"><title>{MONTHS[m.month - 1]} {m.year}: '
                           f'{num(value)} {unit} by the end of the month</title></rect>')
            m = nxt
    # The running total as a step line, with a light wash under it
    if series:
        points = [(x(start), y(0))]
        prev = 0.0
        for d, v in series:
            points.append((x(d), y(prev)))
            points.append((x(d), y(v)))
            prev = v
        points.append((x(today), y(prev)))
        line = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
        out.append(f'<polygon class="viz-area" points="{line} {x(today):.1f},{y(0):.1f}"/>')
        out.append(f'<polyline class="viz-line" points="{line}"/>')
        if projected and today < end:
            out.append(f'<line class="viz-proj" x1="{x(today):.1f}" y1="{y(total):.1f}" '
                       f'x2="{x(end):.1f}" y2="{y(projected):.1f}"><title>At this pace: '
                       f'{num(projected)} {unit} by {end.strftime("%d.%m.%Y")}</title></line>')
        out.append(f'<circle class="viz-dot" cx="{x(today):.1f}" cy="{y(total):.1f}" r="4">'
                   f'<title>{today.strftime("%d.%m.%Y")}: {num(total)} {unit}</title></circle>')
    out.append("</svg>")
    return Markup("".join(out))


def stacked_months(months: list[date], stacks: dict[str, list[int]], order: list[str],
                   label: str = "", names: dict[str, str] | None = None) -> Markup:
    """Monthly totals as columns, each split by source (order fixes the colour
    slot of each source, so a source keeps its colour whatever is shown)."""
    plot_w, plot_h = W - LEFT - 16, H - TOP - BOTTOM
    totals = [sum(stacks[s][i] for s in stacks) for i in range(len(months))]
    top, grid = scale(max(totals + [1]) * 1.05)
    band = plot_w / max(1, len(months))
    bar = min(24.0, band * 0.6)

    def y(v: float) -> float:
        return TOP + plot_h * (1 - v / top)

    out = [f'<svg class="viz" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">']
    for t in grid:
        out.append(f'<line class="viz-grid" x1="{LEFT}" x2="{LEFT + plot_w}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        out.append(f'<text class="viz-axis" x="{LEFT - 8}" y="{y(t) + 4:.1f}" text-anchor="end">'
                   f'{axis_label(t, "points")}</text>')
    present = [s for s in order if s in stacks]
    for i, month in enumerate(months):
        x0 = LEFT + band * i + (band - bar) / 2
        base = 0.0
        out.append(f'<rect class="viz-hit" x="{LEFT + band * i:.1f}" y="{TOP}" width="{band:.1f}" '
                   f'height="{plot_h}"><title>{MONTHS[month.month - 1]} {month.year}: '
                   f'{num(totals[i])} points</title></rect>')
        visible = [s for s in present if stacks[s][i] > 0]
        for j, s in enumerate(visible):
            v = stacks[s][i]
            y_top, y_bottom = y(base + v), y(base)
            gap = 2 if j > 0 else 0                        # surface gap between segments
            h = max(0.0, y_bottom - y_top - gap)
            slot = order.index(s) + 1
            title = f"{MONTHS[month.month - 1]} {month.year}, {escape((names or {}).get(s, s))}: {num(v)} points"
            if j == len(visible) - 1 and h > 4:            # rounded data end on the top segment
                r = 4
                out.append(f'<path class="viz-s{slot}" d="M{x0:.1f},{y_top + h:.1f} V{y_top + r:.1f} '
                           f'Q{x0:.1f},{y_top:.1f} {x0 + r:.1f},{y_top:.1f} H{x0 + bar - r:.1f} '
                           f'Q{x0 + bar:.1f},{y_top:.1f} {x0 + bar:.1f},{y_top + r:.1f} '
                           f'V{y_top + h:.1f} Z"><title>{title}</title></path>')
            else:
                out.append(f'<rect class="viz-s{slot}" x="{x0:.1f}" y="{y_top:.1f}" width="{bar:.1f}" '
                           f'height="{h:.1f}"><title>{title}</title></rect>')
            base += v
        # Quarter starts, and the first month unless a quarter starts the month after
        if month.month in (1, 4, 7, 10) or (i == 0 and month.month % 3 != 0):
            text = MONTHS[month.month - 1] + (f" {month.year % 100:02d}" if month.month == 1 or i == 0 else "")
            out.append(f'<text class="viz-axis" x="{LEFT + band * i + band / 2:.1f}" y="{H - 8}" '
                       f'text-anchor="middle">{text}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def legend(items: list[tuple[int, str]]) -> Markup:
    """A legend row: (colour slot, name)."""
    parts = ['<ul class="viz-legend">']
    for slot, name in items:
        parts.append(f'<li><svg viewBox="0 0 10 10" aria-hidden="true"><rect class="viz-s{slot}" '
                     f'width="10" height="10" rx="2"/></svg>{escape(name)}</li>')
    parts.append("</ul>")
    return Markup("".join(parts))
