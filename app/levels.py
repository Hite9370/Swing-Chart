"""Support / resistance detection.

Method (plain code, no AI):
1. Find swing highs and swing lows (a candle whose high/low is the extreme
   of the `window` candles on each side).
2. Group swing points that sit within `tol` of each other into one level.
   A level touched many times is a stronger level.
3. Levels below the current price are support, above are resistance.
4. Classic daily pivot points are calculated from the previous trading day.
5. ATR (average true range) is returned too: it is the typical candle size and
   is used to place stop losses.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def _swing_points(candles: List[dict], window: int) -> List[Tuple[int, float]]:
    points: List[Tuple[int, float]] = []
    for i in range(window, len(candles) - window):
        seg = candles[i - window: i + window + 1]
        if candles[i]["high"] >= max(c["high"] for c in seg):
            points.append((i, candles[i]["high"]))
        if candles[i]["low"] <= min(c["low"] for c in seg):
            points.append((i, candles[i]["low"]))
    return points


def _cluster(points: List[Tuple[int, float]], tol: float) -> List[dict]:
    clusters: List[dict] = []
    for idx, p in sorted(points, key=lambda x: x[1]):
        if clusters and abs(p - clusters[-1]["level"]) / clusters[-1]["level"] <= tol:
            c = clusters[-1]
            c["prices"].append(p)
            c["idx"].append(idx)
            c["level"] = sum(c["prices"]) / len(c["prices"])
        else:
            clusters.append({"level": p, "prices": [p], "idx": [idx]})
    return clusters


def _atr(candles: List[dict], period: int = 14) -> Optional[float]:
    if len(candles) < 2:
        return None
    trs = []
    for i in range(max(1, len(candles) - period), len(candles)):
        c, prev_close = candles[i], candles[i - 1]["close"]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev_close), abs(c["low"] - prev_close)))
    return round(sum(trs) / len(trs), 2) if trs else None


def _daily_pivots(candles: Optional[List[dict]], today_day: Optional[int]) -> Optional[dict]:
    if not candles:
        return None
    days: dict = {}
    for c in candles:
        days.setdefault(c["time"] // 86400, []).append(c)
    keys = sorted(days)
    if not keys:
        return None
    if today_day is not None and keys[-1] != today_day:
        prev_key = keys[-1]          # market not open yet today, the last day is complete
    elif len(keys) >= 2:
        prev_key = keys[-2]          # today is in progress, use the day before
    else:
        return None
    prev = days[prev_key]
    high = max(c["high"] for c in prev)
    low = min(c["low"] for c in prev)
    close = prev[-1]["close"]
    p = (high + low + close) / 3
    rng = high - low
    return {
        "R2": round(p + rng, 2),
        "R1": round(2 * p - low, 2),
        "P": round(p, 2),
        "S1": round(2 * p - high, 2),
        "S2": round(p - rng, 2),
    }


def compute_levels(
    candles: List[dict],
    price: float,
    window: int = 5,
    tol: float = 0.003,
    per_side: int = 3,
    lookback: int = 1500,
    today_day: Optional[int] = None,
) -> dict:
    recent = candles[-lookback:]
    n = len(recent)
    levels: List[dict] = []
    if n >= window * 2 + 5 and price:
        clusters = _cluster(_swing_points(recent, window), tol)
        scored = []
        for c in clusters:
            lvl = c["level"]
            touches = len(c["prices"])
            dist_pct = abs(lvl - price) / price * 100
            recency = max(c["idx"]) / n
            score = touches * (0.5 + 0.5 * recency) / (1 + dist_pct / 2)
            scored.append((score, touches, lvl))

        for side in ("support", "resistance"):
            pool = [s for s in scored if (s[2] < price) == (side == "support")]
            pool.sort(reverse=True)
            chosen = [s for s in pool if s[1] >= 2][:per_side]
            if len(chosen) < 2:  # not enough proven levels, add best single-touch ones
                chosen += [s for s in pool if s[1] < 2][: 2 - len(chosen)]
            for _score, touches, lvl in chosen:
                levels.append({"price": round(lvl, 2), "touches": touches, "kind": side})

    levels.sort(key=lambda l: abs(l["price"] - price))
    return {
        "levels": levels,
        "pivots": _daily_pivots(candles[-3000:], today_day),
        "atr": _atr(recent),
    }
