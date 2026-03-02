"""Black-Scholes implied volatility solver.

Uses Brent's method (scipy.optimize.brentq) for robustness — Newton-Raphson
is faster but can fail for deep ITM/OTM options or near-zero vega.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm


def bs_call(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(S * np.exp(-q * T) - K * np.exp(-r * T), 0.0)
    F = S * np.exp((r - q) * T)
    df = np.exp(-r * T)
    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


def bs_put(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float:
    call = bs_call(S, K, r, q, sigma, T)
    # Put-call parity
    return call - S * np.exp(-q * T) + K * np.exp(-r * T)


def implied_vol(
    price: float,
    S: float,
    K: float,
    r: float,
    T: float,
    q: float = 0.0,
    option_type: str = "call",
    sigma_lo: float = 1e-4,
    sigma_hi: float = 10.0,
) -> float | None:
    """Compute implied volatility by inverting the BS formula.

    Parameters
    ----------
    price       : observed market price (mid of bid/ask recommended)
    S           : current spot
    K           : strike
    r           : continuously-compounded risk-free rate
    T           : time to expiry in years
    q           : continuous dividend yield
    option_type : 'call' or 'put'
    sigma_lo    : lower bound for vol search (default 0.01%)
    sigma_hi    : upper bound for vol search (default 1000%)

    Returns
    -------
    float  : implied vol as a decimal (e.g. 0.20 = 20%), or
    None   : if no solution found (intrinsic-only, stale quote, etc.)
    """
    if T <= 0:
        return None

    pricer = bs_call if option_type.lower() == "call" else bs_put

    # Intrinsic value check: price must exceed intrinsic
    intrinsic = max(
        (S * np.exp(-q * T) - K * np.exp(-r * T)) if option_type == "call"
        else (K * np.exp(-r * T) - S * np.exp(-q * T)),
        0.0,
    )
    if price <= intrinsic + 1e-8:
        return None

    def objective(sigma: float) -> float:
        return pricer(S, K, r, q, sigma, T) - price

    try:
        f_lo = objective(sigma_lo)
        f_hi = objective(sigma_hi)
        if f_lo * f_hi > 0:
            # Price outside [BS(sigma_lo), BS(sigma_hi)] — no bracketed root
            return None
        return brentq(objective, sigma_lo, sigma_hi, xtol=1e-8, rtol=1e-8)
    except (ValueError, RuntimeError):
        return None
