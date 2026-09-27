"""Dashboard trend view-models: daily totals → chart geometry, sparklines, deltas.

Everything is computed here, server-side, so the page ships a finished
chart and a few numbers for the hover readout — no charting library.
The chart plots 登记总数 and 登记成功 on one axis (both are record counts);
待补正 is an order of magnitude smaller and only gets a sparkline, so no
second scale is ever needed.

Geometry is in plot units 0–1000 on both axes: the lines are drawn in an
SVG stretched to the plot box (preserveAspectRatio="none" with
non-scaling strokes), while tick labels, end labels and dots are HTML
placed by percentage. Text therefore keeps its size on a phone instead
of shrinking with the drawing.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any, Iterable

from src import daily_stats

RANGES = (30, 90)

PLOT = 1000  # plot units per axis

SERIES = (
    ("total", "登记总数", "blue"),
    ("success", "登记成功", "green"),
)

SPARK_W, SPARK_H = 88, 26


def nice_ticks(lo: float, hi: float, target: int = 4) -> list[int]:
    """Clean integer ticks (1/2/5 × 10^k steps) covering [lo, hi]."""
    lo, hi = math.floor(lo), math.ceil(hi)
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = mag
    for m in (1, 2, 5, 10):
        step = m * mag
        if (hi - lo) / step <= target:
            break
    step = max(1, int(round(step)))
    start = (lo // step) * step
    end = -((-hi) // step) * step
    return list(range(int(start), int(end) + step, step))


def _x_ticks(dates: list[date], xs: list[float], want: int = 5) -> list[dict[str, Any]]:
    """Up to `want` date labels, first and last included; `pct` is the left offset."""
    n = len(dates)
    if n <= want:
        picks = list(range(n))
    else:
        picks = sorted({round(i * (n - 1) / (want - 1)) for i in range(want)})
    out = []
    for j, i in enumerate(picks):
        anchor = "start" if j == 0 else ("end" if j == len(picks) - 1 else "middle")
        out.append({"pct": round(xs[i] / PLOT * 100, 2), "label": dates[i].strftime("%m-%d"), "anchor": anchor})
    return out


def build_chart(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Geometry for the two-line chart; None when there are fewer than two days."""
    if len(points) < 2:
        return None
    dates = [date.fromisoformat(p["date"]) for p in points]
    span = max(1, (dates[-1] - dates[0]).days)
    values = [p[key] for p in points for key, _, _ in SERIES]
    lo, hi = min(values), max(values)
    pad = max(1, (hi - lo) * 0.1)
    ticks = nice_ticks(max(0, lo - pad), hi + pad)
    t_lo, t_hi = ticks[0], ticks[-1]

    def sx(d: date) -> float:
        return round((d - dates[0]).days / span * PLOT, 1)

    def sy(v: float) -> float:
        return round((1 - (v - t_lo) / (t_hi - t_lo)) * PLOT, 1)

    xs = [sx(d) for d in dates]
    series = []
    for key, label, color in SERIES:
        pts = [(xs[i], sy(p[key])) for i, p in enumerate(points)]
        series.append({
            "key": key, "label": label, "color": color,
            "path": "M" + " L".join(f"{a},{b}" for a, b in pts),
            "end_left": round(pts[-1][0] / PLOT * 100, 2),
            "end_top": round(pts[-1][1] / PLOT * 100, 2),
            "last": points[-1][key],
        })
    # Direct end labels only while they cannot collide (the plot is ~200px
    # tall, so 8% ≈ 16px); otherwise the legend and the hover readout carry it.
    ends = sorted(s["end_top"] for s in series)
    end_labels = all(b - a >= 8 for a, b in zip(ends, ends[1:]))
    hover = {
        "x": xs,
        "d": [p["date"] for p in points],
        "s": [{"label": label, "color": color, "v": [p[key] for p in points],
               "y": [sy(p[key]) for p in points]} for key, label, color in SERIES],
    }
    return {
        "plot": PLOT,
        "y_ticks": [{"v": t, "y": sy(t), "pct": round(sy(t) / PLOT * 100, 2)} for t in ticks],
        "x_ticks": _x_ticks(dates, xs),
        "series": series,
        "end_labels": end_labels,
        "hover_json": json.dumps(hover, ensure_ascii=False, separators=(",", ":")),
    }


def sparkline(values: list[int]) -> dict[str, Any] | None:
    """Points for a small de-emphasised line with an accented last point."""
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    n = len(values)
    pad = 3

    def sy(v: float) -> float:
        if hi == lo:
            return SPARK_H / 2
        return round(pad + (1 - (v - lo) / (hi - lo)) * (SPARK_H - 2 * pad), 1)

    pts = [(round(pad + i * (SPARK_W - 2 * pad) / (n - 1), 1), sy(v)) for i, v in enumerate(values)]
    return {"w": SPARK_W, "h": SPARK_H, "points": " ".join(f"{a},{b}" for a, b in pts),
            "end_x": pts[-1][0], "end_y": pts[-1][1]}


def delta(points: list[dict[str, Any]], key: str, days: int = 7) -> int | None:
    """Change of `key` against the entry `days` days before the last one
    (the nearest earlier entry within 3 more days); None without one."""
    if len(points) < 2:
        return None
    last = date.fromisoformat(points[-1]["date"])
    target = last - timedelta(days=days)
    floor = target - timedelta(days=3)
    for p in reversed(points[:-1]):
        d = date.fromisoformat(p["date"])
        if floor <= d <= target:
            return points[-1][key] - p[key]
        if d < floor:
            break
    return None


def trend_ctx(companies: Iterable[str], year: int | None, range_days: int,
              *, today: date | None = None) -> dict[str, Any]:
    companies = set(companies)
    range_days = range_days if range_days in RANGES else RANGES[0]
    points = daily_stats.series(companies, year, range_days, today=today)
    recent = points if range_days == 30 else daily_stats.series(companies, year, 30, today=today)
    return {
        "range": range_days,
        "ranges": RANGES,
        "chart": build_chart(points),
        "points": points,
        "spark": {k: sparkline([p[k] for p in recent]) for k in ("total", "success", "pending")},
        "delta": {k: delta(recent, k) for k in ("total", "success", "pending")},
        "since": daily_stats.first_date(),
    }
