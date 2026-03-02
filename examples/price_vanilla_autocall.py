"""Example: Price a 1-year vanilla autocallable note with quarterly observations.

Assumptions
-----------
- S0 = 100, notional = 1000
- Flat implied vol surface: σ = 20%
- r = 5%, q = 2% (continuous)
- Quarterly autocall observations at 3m, 6m, 9m, 12m
- Autocall barrier: 100% of S0 at each observation
- Coupon on autocall: $50 (5% of notional)
- Capital barrier: 80% of S0 (European, maturity only)
"""

import numpy as np
from autocall_pricer import (
    PricingEngine,
    VolSurface,
    FlatRateCurve,
    VanillaAutocall,
)

# ── Market data ──────────────────────────────────────────────────────────────
S0 = 100.0
SIGMA = 0.20
R = 0.05
Q = 0.02

K_grid = np.linspace(60, 140, 30)
T_grid = np.linspace(0.1, 2.0, 20)
sigma_grid = np.full((len(K_grid), len(T_grid)), SIGMA)

surface = VolSurface(K_grid, T_grid, sigma_grid)
rate = FlatRateCurve(rate=R, dividend=Q)

# ── Product ───────────────────────────────────────────────────────────────────
product = VanillaAutocall(
    S0=S0,
    notional=1000.0,
    observation_dates=[0.25, 0.5, 0.75, 1.0],
    autocall_barriers=[1.0, 1.0, 1.0, 1.0],
    coupon_amounts=[50.0, 50.0, 50.0, 50.0],
    maturity=1.0,
    capital_barrier=0.80,
)

# ── Price ─────────────────────────────────────────────────────────────────────
engine = PricingEngine(
    vol_surface=surface,
    rate_curve=rate,
    N_s=400,
    min_steps_per_year=252,
)

result = engine.price(product)

print("=" * 55)
print("  Vanilla Autocallable — Pricing Results")
print("=" * 55)
print(f"  Price  : {result.price:>10.4f}")
print(f"  Delta  : {result.delta:>10.6f}")
print(f"  Gamma  : {result.gamma:>10.6f}")
print(f"  Theta  : {result.theta:>10.4f}  (per year)")
print(f"  Vega   : {result.vega:>10.4f}  (per 1% vol bump)")
print("=" * 55)
