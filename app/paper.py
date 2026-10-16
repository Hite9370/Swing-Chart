"""Paper trading: a pretend account with dummy money. It never sends real orders.

The account is saved in data/paper.json so it survives restarts.

The bot only buys (no short selling) and only stocks in PAPER_WATCHLIST:
1. Trend filter: the 20 EMA must be above the 50 EMA.
2. Entry: price dipped to a support level and held, or dipped to the 20 EMA and closed back above it.
3. Stop just under that support / candle low. Target at the next resistance (capped at 3x the risk),
   or 2x the risk if there is none. Skips the trade if resistance is closer than 1.5x the risk.
4. Size: risk about PAPER_RISK_PCT of the account per trade, never more than half the account in one stock.
5. Once a trade is 1x risk in profit the stop moves to the entry price (break-even).
6. Live mode: no new trades before 9:20 or after 15:00 IST, everything is sold at 15:15.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from . import config

log = logging.getLogger("paper")

DATA_FILE = Path(os.getenv("PAPER_FILE") or Path(__file__).resolve().parent.parent / "data" / "paper.json")


def ist_now() -> float:
    return time.time() + config.IST_OFFSET


def minute_of_day(ts: float) -> int:
    return int(ts % 86400 // 60)


def is_weekday(ts: float) -> bool:
    return datetime.fromtimestamp(ts, timezone.utc).weekday() < 5


def fee_of(value: float) -> float:
    return round(value * config.PAPER_FEE_PCT / 100, 2)


def _ema(values: List[float], period: int) -> float:
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def entry_signal(candles: List[dict], levels: List[dict], atr: Optional[float]) -> Optional[dict]:
    """A buy setup on these candles right now, or None. The last candle is still forming."""
    done = candles[:-1]
    if len(done) < 60:
        return None
    price = candles[-1]["close"]
    closes = [c["close"] for c in done[-300:]]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20 <= e50:
        return None  # only buy in an uptrend

    atr = atr if atr and atr > 0 else price * 0.004
    buf = max(0.25 * atr, price * 0.001)
    gap = max(0.5 * atr, price * 0.0015)
    sup = max((l["price"] for l in levels if l["kind"] == "support" and l["price"] < price), default=None)
    res = min((l["price"] for l in levels if l["kind"] == "resistance" and l["price"] > price), default=None)
    last = done[-1]

    if sup is not None and price <= sup + buf and last["low"] <= sup + buf and last["close"] > sup:
        stop = sup - gap
        why = f"bounced at support {sup:.2f} in an uptrend (EMA20 above EMA50)"
    elif last["low"] <= e20 < last["close"] and e20 < price < e20 + atr:
        stop = last["low"] - buf
        why = f"pulled back to the 20 EMA ({e20:.2f}) and closed above it, uptrend"
    else:
        return None

    risk = price - stop
    if risk <= 0:
        return None
    if res is not None and res - price < 1.5 * risk:
        return None  # resistance too close, not worth the risk
    target = min(res, price + 3 * risk) if res is not None else price + 2 * risk
    return {"stop": round(stop, 2), "target": round(target, 2), "why": why}


class Account:
    def __init__(self, path: Path = DATA_FILE) -> None:
        self.path = path
        self.s = self._load() or self._fresh(config.PAPER_START_CASH)

    @staticmethod
    def _fresh(cash: float) -> dict:
        return {
            "start_cash": cash, "cash": cash, "positions": {}, "trades": [], "log": [],
            "cooldown": {}, "bot_on": True, "created": ist_now(),
        }

    def _load(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("could not read %s (%s), starting a new paper account", self.path, exc)
            return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def note(self, text: str) -> None:
        log.info(text)
        self.s["log"].insert(0, {"t": ist_now(), "text": text})
        del self.s["log"][60:]

    def reset(self, cash: float) -> None:
        self.s = self._fresh(cash)
        self.note(f"Account reset to {cash:,.2f}")
        self.save()

    def set_bot(self, on: bool) -> None:
        self.s["bot_on"] = on
        self.note("Bot started" if on else "Bot paused (open trades still use their stop and target)")
        self.save()

    # ---------- orders ----------
    def buy(self, inst: dict, qty: int, price: float, reason: str,
            stop: Optional[float] = None, target: Optional[float] = None) -> Optional[str]:
        """Returns an error message, or None when filled."""
        token = inst["token"]
        if inst.get("kind") != "EQ":
            return "Only stocks can be bought, not indices."
        if token in self.s["positions"]:
            return f"Already holding {inst['name']}."
        if qty < 1:
            return "Quantity must be at least 1."
        value = qty * price
        fee = fee_of(value)
        if value + fee > self.s["cash"] + 1e-9:
            return f"Not enough cash: {qty} x {price:,.2f} needs {value + fee:,.2f}, you have {self.s['cash']:,.2f}."
        self.s["cash"] = round(self.s["cash"] - value - fee, 2)
        self.s["positions"][token] = {
            "token": token, "name": inst["name"], "qty": qty, "entry": price,
            "stop": stop, "target": target, "risk": round(price - stop, 2) if stop else None,
            "breakeven": False, "t": ist_now(), "reason": reason, "fees": fee,
        }
        extra = f" · stop {stop:,.2f} · target {target:,.2f}" if stop and target else ""
        self.note(f"BUY {qty} {inst['name']} at {price:,.2f}{extra} · {reason}")
        self.save()
        return None

    def sell(self, token: str, price: float, reason: str) -> Optional[str]:
        p = self.s["positions"].pop(token, None)
        if p is None:
            return "No open position in that stock."
        value = p["qty"] * price
        fee = fee_of(value)
        self.s["cash"] = round(self.s["cash"] + value - fee, 2)
        pnl = round((price - p["entry"]) * p["qty"] - p["fees"] - fee, 2)
        self.s["trades"].insert(0, {
            "token": token, "name": p["name"], "qty": p["qty"], "entry": p["entry"], "exit": price,
            "t_in": p["t"], "t_out": ist_now(), "pnl": pnl, "fees": round(p["fees"] + fee, 2),
            "why_in": p["reason"], "why_out": reason,
        })
        del self.s["trades"][300:]
        self.s["cooldown"][token] = ist_now()
        self.note(f"SELL {p['qty']} {p['name']} at {price:,.2f} · {reason} · P&L {pnl:+,.2f}")
        self.save()
        return None

    # ---------- bot rules ----------
    def size(self, price: float, stop: float, equity: float) -> int:
        risk_per_share = price - stop
        if risk_per_share <= 0:
            return 0
        by_risk = equity * config.PAPER_RISK_PCT / 100 / risk_per_share
        budget = min(self.s["cash"], equity / config.PAPER_MAX_POSITIONS)
        by_cash = budget / (price * (1 + config.PAPER_FEE_PCT / 100))
        return max(0, math.floor(min(by_risk, by_cash)))

    def day_pnl(self, now: float) -> float:
        today = int(now // 86400)
        return round(sum(t["pnl"] for t in self.s["trades"] if int(t["t_out"] // 86400) == today), 2)

    def cooling(self, token: str, now: float) -> bool:
        return now - self.s["cooldown"].get(token, 0) < config.PAPER_COOLDOWN_MIN * 60

    def entry_block(self, now: float, live: bool) -> Optional[str]:
        """Why the bot will not open a new trade right now, or None if it may."""
        if not self.s["bot_on"]:
            return "Bot is paused."
        if live:
            m = minute_of_day(now)
            if not is_weekday(now) or not (9 * 60 + 20 <= m < 15 * 60):
                return "Market closed. The bot opens trades 9:20 AM to 3:00 PM IST, Mon to Fri."
        if self.day_pnl(now) <= -self.s["start_cash"] * config.PAPER_DAILY_LOSS_PCT / 100:
            return f"Daily loss limit ({config.PAPER_DAILY_LOSS_PCT:g}%) reached. No new trades today."
        if len(self.s["positions"]) >= config.PAPER_MAX_POSITIONS:
            return f"Holding the maximum of {config.PAPER_MAX_POSITIONS} positions."
        return None

    def exit_reason(self, p: dict, price: float, now: float, live: bool) -> Optional[str]:
        """Why this position should be sold now, or None. May move the stop to break-even."""
        if live:
            if int(p["t"] // 86400) < int(now // 86400):
                return "held from an earlier day, sold at the open"
            if minute_of_day(now) >= 15 * 60 + 15:
                return "end of day square-off (3:15 PM)"
        if p.get("stop") is not None and price <= p["stop"]:
            return "break-even stop hit" if p.get("breakeven") else "stop loss hit"
        if p.get("target") is not None and price >= p["target"]:
            return "target hit"
        if p.get("risk") and not p.get("breakeven") and price >= p["entry"] + p["risk"]:
            p["stop"] = p["entry"]
            p["breakeven"] = True
            self.note(f"{p['name']} is 1x risk in profit, stop moved to break-even {p['entry']:,.2f}")
            self.save()
        return None

    # ---------- view ----------
    def snapshot(self, price_of: Callable[[str], Optional[float]], live: bool) -> dict:
        now = ist_now()
        positions, holdings = [], 0.0
        for p in self.s["positions"].values():
            ltp = price_of(p["token"]) or p["entry"]
            holdings += ltp * p["qty"]
            positions.append({**p, "ltp": ltp, "pnl": round((ltp - p["entry"]) * p["qty"] - p["fees"], 2)})
        equity = round(self.s["cash"] + holdings, 2)
        trades = self.s["trades"]
        wins = sum(1 for t in trades if t["pnl"] > 0)
        return {
            "start_cash": self.s["start_cash"], "cash": self.s["cash"], "holdings": round(holdings, 2),
            "equity": equity, "pnl": round(equity - self.s["start_cash"], 2), "day_pnl": self.day_pnl(now),
            "bot_on": self.s["bot_on"],
            "bot_status": self.entry_block(now, live) or f"Watching {len(config.PAPER_WATCHLIST)} stocks for a setup.",
            "positions": positions, "trades": trades[:50], "log": self.s["log"][:20],
            "wins": wins, "losses": len(trades) - wins, "watchlist": config.PAPER_WATCHLIST,
        }
