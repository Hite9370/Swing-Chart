from __future__ import annotations

from typing import Dict, List

from .config import TIMEFRAMES

SESSION_OPEN = 33300  # 09:15 in seconds after midnight, so 1h candles start at 09:15, 10:15 ...


def bucket_start(ts: float, seconds: int) -> int:
    """Start time of the candle that contains ts (IST-shifted epoch seconds)."""
    off = SESSION_OPEN % seconds if seconds < 86400 else 0
    return int((ts - off) // seconds * seconds + off)


class CandleStore:
    """Candles for one instrument, for every timeframe.

    History is merged in (newest chunk first, then older chunks as you scroll),
    and live ticks keep the newest candle up to date.
    """

    def __init__(self) -> None:
        self.data: Dict[str, List[dict]] = {k: [] for k in TIMEFRAMES}
        self.loaded: Dict[str, bool] = {k: False for k in TIMEFRAMES}
        self.exhausted: Dict[str, bool] = {k: False for k in TIMEFRAMES}

    def merge(self, tf: str, candles: List[dict]) -> int:
        """Merge history candles into the store. Returns how many were new."""
        if not candles:
            return 0
        by_time = {c["time"]: c for c in self.data[tf]}
        added = 0
        for c in candles:
            existing = by_time.get(c["time"])
            if existing is None:
                by_time[c["time"]] = dict(c)
                added += 1
                continue
            # same candle seen twice (history + live): keep the live close, widen the range
            existing["open"] = c["open"]
            existing["high"] = max(existing["high"], c["high"])
            existing["low"] = min(existing["low"], c["low"])
        self.data[tf] = [by_time[t] for t in sorted(by_time)]
        return added

    def add_tick(self, ts: float, price: float) -> Dict[str, dict]:
        """Add one live price. Returns {timeframe: candle} for every candle that changed."""
        updated: Dict[str, dict] = {}
        for tf, cfg in TIMEFRAMES.items():
            size = cfg["seconds"]
            series = self.data[tf]
            if series:
                last = series[-1]["time"]
                # stay on the same grid as the existing candles
                bucket = last + int((ts - last) // size) * size
            else:
                bucket = bucket_start(ts, size)

            if series and series[-1]["time"] == bucket:
                c = series[-1]
                if price > c["high"]:
                    c["high"] = price
                if price < c["low"]:
                    c["low"] = price
                c["close"] = price
            elif not series or bucket > series[-1]["time"]:
                c = {"time": bucket, "open": price, "high": price, "low": price, "close": price}
                series.append(c)
            else:
                continue  # late tick for an old candle, ignore
            updated[tf] = dict(c)
        return updated
