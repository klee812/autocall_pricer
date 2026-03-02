"""Example: build a VolSurface from option prices.

This script shows the full pipeline from raw option records to a priced
autocallable.  It uses synthetic prices generated from a known vol surface so
you can verify the round-trip accuracy before plugging in live API data.

Replace the `make_synthetic_chain()` call with `client.fetch_chain()` once
the API parser is implemented.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

from autocall_pricer import (
    FlatRateCurve,
    OptionRecord,
    PricingEngine,
    VanillaAutocall,
    VolSurface,
    chain_to_surface,
)


# ---------------------------------------------------------------------------
# Synthetic option chain (replace with API call in production)
# ---------------------------------------------------------------------------

def bs_call(S, K, r, q, sigma, T):
    F = S * np.exp((r - q) * T)
    df = np.exp(-r * T)
    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


def bs_put(S, K, r, q, sigma, T):
    return bs_call(S, K, r, q, sigma, T) - S * np.exp(-q * T) + K * np.exp(-r * T)


def make_synthetic_chain(
    S0: float = 100.0,
    r: float = 0.05,
    q: float = 0.02,
    bid_ask_half_spread: float = 0.05,   # half spread as fraction of mid
) -> list[OptionRecord]:
    """Generate realistic option records from a known skewed vol surface.

    The 'true' vol surface has a simple linear skew:
        sigma(K, T) = 0.20 + 0.05 * (1 - K/S0) + 0.03 * T
    so we can verify the implied vol round-trip at the end.
    """
    records = []
    expiries = [1/12, 3/12, 6/12, 1.0, 2.0]   # 1m, 3m, 6m, 1y, 2y
    moneyness_range = np.linspace(-0.3, 0.3, 13)  # log-moneyness grid

    rng = np.random.default_rng(42)

    for T in expiries:
        F = S0 * np.exp((r - q) * T)
        for lm in moneyness_range:
            K = round(F * np.exp(lm), 2)
            true_sigma = 0.20 + 0.05 * (1 - K / S0) + 0.03 * T
            true_sigma = max(true_sigma, 0.05)

            for option_type in ("call", "put"):
                # Skip deep-ITM legs (use OTM for each side)
                if option_type == "call" and lm < -0.15:
                    continue
                if option_type == "put" and lm > 0.15:
                    continue

                pricer = bs_call if option_type == "call" else bs_put
                mid = pricer(S0, K, r, q, true_sigma, T)
                if mid < 0.01:
                    continue

                spread = mid * bid_ask_half_spread * 2
                noise = rng.normal(0, spread * 0.1)  # tiny quote noise
                bid = max(mid - spread / 2 + noise, 0.01)
                ask = mid + spread / 2 + noise

                option_id = f"SYN_{option_type.upper()[0]}{int(K)}_{int(T*12)}M"
                records.append(OptionRecord(
                    option_id=option_id,
                    underlying="SYN",
                    option_type=option_type,
                    strike=K,
                    expiry=T,
                    bid=round(bid, 4),
                    ask=round(ask, 4),
                    S0=S0,
                    r=r,
                    q=q,
                ))

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    S0, r, q = 100.0, 0.05, 0.02

    print("Step 1: Generate / fetch option chain")
    print("  (In production: replace with client.fetch_chain(...))")
    records = make_synthetic_chain(S0=S0, r=r, q=q)
    print(f"  {len(records)} raw option records")

    print("\nStep 2: Filter → solve IV → build VolSurface")
    surface = chain_to_surface(
        records,
        filter_kwargs={
            "max_spread_pct": 0.25,
            "min_bid": 0.01,
            "max_moneyness": 1.0,
            "min_expiry": 7 / 365,
        },
    )
    print(f"  Surface: {len(surface.K_grid)} strikes × {len(surface.T_grid)} expiries")
    print(f"  K range: {surface.K_grid.min():.1f} – {surface.K_grid.max():.1f}")
    print(f"  T range: {surface.T_grid.min():.3f} – {surface.T_grid.max():.2f} yr")
    print(f"  ATM 6m vol: {surface.implied_vol(S0, 0.5).squeeze():.4f}")

    print("\nStep 3: Price autocallable")
    rate = FlatRateCurve(rate=r, dividend=q)
    product = VanillaAutocall(
        S0=S0,
        notional=1000.0,
        observation_dates=[0.25, 0.5, 0.75, 1.0],
        autocall_barriers=[1.0, 1.0, 1.0, 1.0],
        coupon_amounts=[50.0, 50.0, 50.0, 50.0],
        maturity=1.0,
        capital_barrier=0.80,
    )
    engine = PricingEngine(surface, rate)
    result = engine.price(product)

    print(f"\n{'='*50}")
    print(f"  Price  : {result.price:>10.4f}")
    print(f"  Delta  : {result.delta:>10.6f}")
    print(f"  Gamma  : {result.gamma:>10.6f}")
    print(f"  Theta  : {result.theta:>10.4f}  (per year)")
    print(f"  Vega   : {result.vega:>10.4f}  (per 1% vol bump)")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
