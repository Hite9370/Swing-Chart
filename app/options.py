"""Black-Scholes helpers used to ESTIMATE option premiums.

These are model estimates, not market prices. Real premiums also move with
implied volatility changes, bid/ask spread and demand.
"""
from __future__ import annotations

from math import erf, exp, log, sqrt
from typing import Optional

RISK_FREE = 0.065


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, kind: str, r: float = RISK_FREE) -> float:
    """kind: 'CE' (call) or 'PE' (put)."""
    if kind == "CE":
        intrinsic = max(spot - strike, 0.0)
    else:
        intrinsic = max(strike - spot, 0.0)
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic
    vol_t = sigma * sqrt(t_years)
    d1 = (log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / vol_t
    d2 = d1 - vol_t
    if kind == "CE":
        return spot * _cdf(d1) - strike * exp(-r * t_years) * _cdf(d2)
    return strike * exp(-r * t_years) * _cdf(-d2) - spot * _cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t_years: float, kind: str) -> Optional[float]:
    """Find the volatility that makes the model price equal the market price."""
    if price <= 0 or t_years <= 0:
        return None
    lo, hi = 0.01, 3.0
    if not (bs_price(spot, strike, t_years, lo, kind) <= price <= bs_price(spot, strike, t_years, hi, kind)):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t_years, mid, kind) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
