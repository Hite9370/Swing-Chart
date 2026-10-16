"""Simulated market so you can see the whole platform working with no account.

Prices follow smooth waves plus noise, which creates realistic-looking swings
and therefore support/resistance levels. These are NOT real prices.
"""
from __future__ import annotations

import asyncio
import calendar
import math
import random
import time
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional

from .. import options as om
from ..candles import bucket_start
from ..config import IST_OFFSET, TIMEFRAMES

LISTING_YEARS = 8  # demo data goes back this far, then "runs out"

NAMES = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN", "ICICIBANK", "BHARTIARTL", "ITC", "LT", "AXISBANK",
    "KOTAKBANK", "HINDUNILVR", "MARUTI", "SUNPHARMA", "TATAMOTORS", "TATASTEEL", "WIPRO", "HCLTECH",
    "ADANIENT", "ASIANPAINT", "BAJFINANCE", "TITAN", "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC",
    "COALINDIA", "JSWSTEEL", "M&M", "TECHM", "NESTLEIND", "DRREDDY", "CIPLA", "GRASIM", "HINDALCO",
    "BPCL", "EICHERMOT", "HEROMOTOCO", "DIVISLAB", "BRITANNIA",
]
KNOWN = {"RELIANCE": ("2885", 1400.0), "TCS": ("11536", 3200.0), "INFY": ("1594", 1500.0),
         "HDFCBANK": ("1333", 1000.0), "SBIN": ("3045", 850.0)}


def build_mock_instruments() -> List[dict]:
    items = [
        {"token": "99926000", "symbol": "Nifty 50", "name": "Nifty 50", "exchange": "NSE",
         "exchange_type": 1, "kind": "INDEX", "base": 25000.0},
        {"token": "99926009", "symbol": "Nifty Bank", "name": "Nifty Bank", "exchange": "NSE",
         "exchange_type": 1, "kind": "INDEX", "base": 55000.0},
    ]
    for i, name in enumerate(NAMES):
        token, base = KNOWN.get(name) or (str(900000 + i), round(random.Random(name).uniform(80, 4500), 2))
        items.append({"token": token, "symbol": name + "-EQ", "name": name, "exchange": "NSE",
                      "exchange_type": 1, "kind": "EQ", "base": base})
    return items


def _last_tuesday(year: int, month: int) -> date:
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def build_chain(inst: dict, spot: float, today: date) -> dict:
    """Made-up option contracts around the current price, so the option finder can be tried in demo mode.
    Real contracts (names, lot sizes, expiries) come from Angel One in live mode."""
    expiries: List[date] = []
    y, m = today.year, today.month
    while len(expiries) < 3:
        e = _last_tuesday(y, m)
        if e >= today:
            expiries.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    is_index = inst["kind"] == "INDEX"
    name = "".join(ch for ch in inst["name"].upper() if ch.isalnum())
    if is_index:
        step = 100 if "BANK" in name else 50
        lot, iv = (35 if "BANK" in name else 75), 0.16
    else:
        step = 2.5 if spot < 250 else 5 if spot < 500 else 10 if spot < 1000 else 20 if spot < 2500 else 50 if spot < 5000 else 100
        lot, iv = max(25, int(round(700000 / max(spot, 1) / 25.0)) * 25), 0.30
    atm = round(spot / step) * step

    chain: dict = {}
    for e in expiries:
        for k in range(-12, 13):
            strike = round(atm + k * step, 2)
            for kind in ("CE", "PE"):
                sym = f"{name}{e:%d%b%y}".upper() + f"{strike:g}{kind}"
                chain.setdefault(e, {}).setdefault(strike, {})[kind] = {
                    "token": "D" + sym, "symbol": sym, "exchange": "NFO", "exchange_type": 2,
                    "lot": lot, "expiry": e, "strike": strike, "type": kind, "iv": iv,
                }
    return chain


class MockProvider:
    name = "mock"

    def option_ltp(self, contract: dict, spot: float) -> Optional[float]:
        """Demo price from Black-Scholes. Not a market price."""
        days = max((contract["expiry"] - date.today()).days, 1)
        return round(om.bs_price(spot, contract["strike"], days / 365, contract.get("iv", 0.25), contract["type"]), 2)

    def __init__(self) -> None:
        self.active: Dict[str, dict] = {}
        self._phase: Dict[str, tuple] = {}
        self._wiggle: Dict[str, float] = {}

    # ------------------------------------------------------------- price model
    def _phases(self, token: str) -> tuple:
        if token not in self._phase:
            r = random.Random("phase|" + token)
            self._phase[token] = tuple(r.uniform(0, 2 * math.pi) for _ in range(4))
        return self._phase[token]

    def price_at(self, inst: dict, ts: float) -> float:
        p1, p2, p3, p4 = self._phases(inst["token"])
        d = ts / 86400.0
        k = (1
             + 0.06 * math.sin(2 * math.pi * d / 37 + p1)
             + 0.03 * math.sin(2 * math.pi * d / 9 + p2)
             + 0.008 * math.sin(2 * math.pi * d / 0.9 + p3)
             + 0.003 * math.sin(2 * math.pi * d / 0.1 + p4))
        return inst["base"] * k

    # ------------------------------------------------------------- history
    def fetch_chunk(self, inst: dict, tf: str, end_ts: Optional[float]) -> List[dict]:
        """Candles ending just before end_ts (or up to now when end_ts is None)."""
        cfg = TIMEFRAMES[tf]
        s = cfg["seconds"]
        now = time.time() + IST_OFFSET
        last = bucket_start(now, s) if end_ts is None else int(end_ts) - s
        limit = bucket_start(now - LISTING_YEARS * 365 * 86400, s)
        times = [last - s * k for k in range(cfg["mock_bars"] - 1, -1, -1)]
        sig = math.sqrt(s / 60.0)
        out = []
        for t in times:
            if t < limit:
                continue
            rng = random.Random(f"{inst['token']}|{tf}|{t}")
            o = self.price_at(inst, t) * (1 + rng.gauss(0, 0.0004 * sig))
            c = self.price_at(inst, t + s) * (1 + rng.gauss(0, 0.0004 * sig))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003 * sig)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0003 * sig)))
            out.append({"time": t, "open": round(o, 2), "high": round(h, 2),
                        "low": round(l, 2), "close": round(c, 2)})
        return out

    # ------------------------------------------------------------- live
    def subscribe(self, inst: dict) -> None:
        self.active[inst["token"]] = inst

    def unsubscribe(self, inst: Optional[dict]) -> None:
        if inst:
            self.active.pop(inst["token"], None)

    async def run(self, push: Callable[[str, float, float], None]) -> None:
        while True:
            ts = time.time() + IST_OFFSET
            for token, inst in list(self.active.items()):
                w = self._wiggle.get(token, 0.0) * 0.9 + random.gauss(0, inst["base"] * 0.00012)
                self._wiggle[token] = w
                push(token, ts, round(self.price_at(inst, ts) + w, 2))
            await asyncio.sleep(0.5)
