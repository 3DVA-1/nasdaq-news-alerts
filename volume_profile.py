"""Provider-independent port of the supplied Pine volume-profile calculation.

Input must be finalized standard 15-minute OHLCV bars of one NQ contract.
The monitor supplies finalized bars in chronological order.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float

    def validate(self):
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(isfinite(v) for v in values):
            raise ValueError("Missing or non-finite OHLCV value")
        if self.volume < 0 or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Invalid OHLCV bar")


def profile(bars, rows=24, value_area_percent=70):
    """Return unrounded POC, VA bounds and row volumes; None if undefined."""
    if not bars or not 5 <= rows <= 100 or not 0 <= value_area_percent <= 100:
        raise ValueError("Invalid profile inputs")
    for bar in bars:
        bar.validate()
    bottom = min(b.low for b in bars)
    top = max(b.high for b in bars)
    step = (top - bottom) / rows
    if step == 0:
        return None
    levels = [bottom + step * i for i in range(rows + 1)]
    totals = [0.0] * rows
    for b in reversed(bars):
        body_top, body_bottom = max(b.open, b.close), min(b.open, b.close)
        body = body_top - body_bottom
        upper_wick = b.high - body_top
        lower_wick = body_bottom - b.low
        denominator = body + 2 * upper_wick + 2 * lower_wick
        if denominator == 0:
            continue  # Original zero-height candle has no allocated volume.
        parts = ((body_bottom, body_top, 1), (body_top, b.high, 2), (b.low, body_bottom, 2))
        for row in range(rows):
            lo, hi = levels[row], levels[row + 1]
            for part_lo, part_hi, weight in parts:
                overlap = max(0.0, min(hi, part_hi) - max(lo, part_lo))
                totals[row] += overlap * weight * b.volume / denominator
    maximum = max(totals)
    if maximum <= 0:
        return None
    poc_row = totals.index(maximum)  # Pine chooses first row in a tie.
    up = down = poc_row
    accumulated = totals[poc_row]
    target = sum(totals) * value_area_percent / 100
    for _ in range(rows):
        if accumulated >= target:
            break
        upper = totals[up + 1] if up < rows - 1 else 0
        lower = totals[down - 1] if down > 0 else 0
        if upper == lower == 0:
            break
        if upper >= lower:
            up += 1
            accumulated += upper
        else:
            down -= 1
            accumulated += lower
    return {"poc": (levels[poc_row] + levels[poc_row + 1]) / 2,
            "val": levels[down], "vah": levels[up + 1], "volumes": totals}


def crossing(previous_close, previous_poc, close, poc):
    """Same moving-POC closed-bar crossing rule as delivered Pine script."""
    if any(v is None for v in (previous_close, previous_poc, close, poc)):
        return None
    if close > poc and previous_close <= previous_poc:
        return "up"
    if close < poc and previous_close >= previous_poc:
        return "down"
    return None
