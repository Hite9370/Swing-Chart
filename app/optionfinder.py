"""Pick an option contract for a trade setup and estimate what it will cost.

This does NOT predict anything. Given a setup from the trade planner (entry,
stop, targets on the stock or index), it:

1. Picks the CALL (bullish) or PUT (bearish) at the strike nearest the entry,
   with at least 7 days to expiry so time decay is not brutal.
2. Reads the contract's current price and works out the implied volatility.
3. ESTIMATES the premium at the stop and at each target with Black-Scholes,
   assuming you hold for about 3 days.
4. Sizes the trade so a stop-out loses only your chosen % of capital.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, List, Optional

from . import options as om

MIN_DAYS_TO_EXPIRY = 7
HOLD_DAYS = 3     # assumed holding period when estimating the price at the stop / targets
TICK = 0.05


def _tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def _fail(message: str) -> dict:
    return {"ok": False, "message": message}


def find_option(
    chain: dict, side: str, spot: float, entry: float, stop: float, targets: List[float],
    ltp_fn: Callable[[dict, float], Optional[float]], capital: float, risk_pct: float,
    today: date, live: bool,
) -> dict:
    kind = "CE" if side == "bull" else "PE"
    expiries = sorted(e for e in chain if e >= today)
    if not expiries:
        return _fail("No option contracts found for this one. It is probably not in the F&O segment, so use the share numbers above.")
    wanted = [e for e in expiries if (e - today).days >= MIN_DAYS_TO_EXPIRY]
    expiry = wanted[0] if wanted else expiries[-1]
    days = max((expiry - today).days, 0)

    strikes = sorted(k for k, v in chain[expiry].items() if kind in v)
    if not strikes:
        return _fail(f"No {kind} contracts found for the {expiry:%d %b %Y} expiry.")
    strike = min(strikes, key=lambda k: abs(k - entry))
    contract = chain[expiry][strike][kind]

    prem_now = ltp_fn(contract, spot)
    if not prem_now or prem_now <= 0:
        return _fail(f"Could not get a price for {contract['symbol']} right now. Try again in a moment.")

    warnings: List[str] = []
    t_now = max(days, 1) / 365
    iv = om.implied_vol(prem_now, spot, strike, t_now, kind)
    if iv is None:
        iv = 0.25
        warnings.append("Could not work out implied volatility from the price, so 25% was assumed.")

    # price when the underlying reaches the entry level (now, if it is already there)
    at_entry = abs(entry - spot) <= 0.001 * spot
    entry_p = _tick(prem_now if at_entry else om.bs_price(entry, strike, t_now, iv, kind))

    t_exit = max(days - HOLD_DAYS, 0.5) / 365
    stop_p = _tick(max(om.bs_price(stop, strike, t_exit, iv, kind), TICK))
    target_ps = [_tick(om.bs_price(t, strike, t_exit, iv, kind)) for t in targets]

    if stop_p >= entry_p:
        return _fail("The estimated price at the stop is not below the entry price, so this setup cannot be sized. Try a different setup.")

    lot = int(contract["lot"])
    loss_per_lot = (entry_p - stop_p) * lot
    cost_per_lot = entry_p * lot
    risk_amount = capital * risk_pct / 100
    lots_by_risk = int(risk_amount // loss_per_lot) if loss_per_lot > 0 else 0
    lots_by_cash = int(capital // cost_per_lot) if cost_per_lot > 0 else 0
    lots = max(0, min(lots_by_risk, lots_by_cash))

    sizing_note = None
    if lots == 0:
        if lots_by_cash == 0:
            sizing_note = f"One lot costs about ₹{cost_per_lot:,.0f}, more than your capital."
        else:
            sizing_note = (f"One lot would lose about ₹{loss_per_lot:,.0f} at the stop, more than your "
                           f"₹{risk_amount:,.0f} risk limit. Skip it, or raise the risk %.")

    profit = [tp > entry_p for tp in target_ps]
    rr = round((target_ps[0] - entry_p) / (entry_p - stop_p), 2)
    for i, tp in enumerate(target_ps, 1):
        if tp <= entry_p:
            warnings.append(
                f"Target {i} would still lose money on the option: its estimated price ({tp:,.2f}) is not above the "
                f"buy price ({entry_p:,.2f}), because about {HOLD_DAYS} days of time decay outweigh the move. "
                "Skip this setup, or wait for one with a bigger move."
            )
    if profit and profit[0] and rr < 1:
        warnings.append(f"On the option, target 1 earns only {rr:.1f}x what the stop loses. That is a poor trade.")
    if not at_entry:
        warnings.append(f"The underlying is not at the entry level yet, so the entry price is an estimate for when it gets to {entry:,.2f}.")
    if days <= 10:
        warnings.append(f"Only {days} days to expiry: time decay will eat the premium quickly if price goes sideways.")
    if entry_p > 0 and (entry_p - stop_p) > 0.5 * entry_p:
        warnings.append("The stop is more than half of the premium. That is a wide stop for an option, so keep the size small.")

    return {
        "ok": True,
        "contract": contract["symbol"], "type": kind, "strike": strike,
        "expiry": expiry.strftime("%d %b %Y"), "days": days, "lot": lot,
        "premium_now": round(prem_now, 2), "source": "live" if live else "demo",
        "iv_pct": round(iv * 100, 1), "hold_days": HOLD_DAYS,
        "entry": entry_p, "stop": stop_p, "targets": target_ps, "targets_profit": profit, "rr": rr,
        "loss_per_lot": round(loss_per_lot, 2), "cost_per_lot": round(cost_per_lot, 2),
        "lots": lots, "capital_needed": round(lots * cost_per_lot, 2), "max_loss": round(lots * loss_per_lot, 2),
        "sizing_note": sizing_note, "warnings": warnings,
    }
