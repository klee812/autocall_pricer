# autocall_pricer

A Python pricing engine for **autocallable structured products**.

**Model:** Dupire local volatility calibrated from a market implied vol surface.
**Solver:** Crank-Nicolson PDE backward induction on a log-space grid.
**Dependencies:** NumPy, SciPy only — no external pricing libraries.

---

## Quick start

```python
import numpy as np
from autocall_pricer import (
    PricingEngine, VolSurface, FlatRateCurve, VanillaAutocall,
)

# 1. Market data
K_grid   = np.linspace(60, 140, 30)           # option strikes
T_grid   = np.linspace(0.1, 2.0, 20)          # expiries (years)
sigma_grid = np.full((30, 20), 0.20)           # flat 20% implied vol surface

surface = VolSurface(K_grid, T_grid, sigma_grid)
rate    = FlatRateCurve(rate=0.05, dividend=0.02)

# 2. Product definition
product = VanillaAutocall(
    S0=100.0,
    notional=1000.0,
    observation_dates=[0.25, 0.5, 0.75, 1.0],   # years
    autocall_barriers=[1.0, 1.0, 1.0, 1.0],      # fraction of S0
    coupon_amounts=[50.0, 50.0, 50.0, 50.0],      # $ paid on autocall
    maturity=1.0,
    capital_barrier=0.80,                          # 80% capital protection
)

# 3. Price
engine = PricingEngine(surface, rate)
result = engine.price(product)

print(f"Price : {result.price:.4f}")
print(f"Delta : {result.delta:.6f}")
print(f"Gamma : {result.gamma:.6f}")
print(f"Theta : {result.theta:.4f}  (per year)")
print(f"Vega  : {result.vega:.4f}  (per 1% vol bump)")
```

```
Price : 985.1646
Delta : 5.527075
Gamma : -0.360102
Theta : -432.8120  (per year)
Ega   : -337.7486  (per 1% vol bump)
```

---

## Installation

```bash
cd /path/to/autocall_pricer
pip install -e .            # base install
pip install -e ".[dev]"     # + pytest, matplotlib
```

Python ≥ 3.11 required.

---

## Performance

| Scenario | Grid | Steps/yr | Wall time |
|---|---|---|---|
| Full price + all greeks, 1-year | N_s=400 | 252 | **~0.32 s** |
| Full price + all greeks, 2-year | N_s=400 | 252 | **~0.64 s** |
| Fast mode (lower accuracy) | N_s=200 | 100 | **~0.10 s** |
| Calibration only | N_s=400 | 252 | ~0.13 s |
| Single PDE solve (base price only) | N_s=400 | 252 | ~0.02 s |

**The full greeks run costs 3 PDE solves** (base + theta + vega). Calibration is the
dominant cost. If you price many products against the same surface, calibrate once
and reuse the `DupireLocalVol` object directly via the low-level API (see below).

---

## API reference

### `VolSurface(K_grid, T_grid, sigma_grid)`

Implied vol surface with `RectBivariateSpline` interpolation.

| Parameter | Type | Description |
|---|---|---|
| `K_grid` | `ndarray (nK,)` | Sorted strike grid (same units as S0) |
| `T_grid` | `ndarray (nT,)` | Sorted expiry grid in years (> 0) |
| `sigma_grid` | `ndarray (nK, nT)` | Annualised BS implied vols (decimals, e.g. 0.20) |

**Important:** `K_grid` should cover the full range of plausible spot values during
the product's life. A range of **[0.5·S0, 2·S0]** is typically sufficient. Outside
this range, Dupire falls back to the surface's edge implied vol.

---

### `FlatRateCurve(rate, dividend=0.0)`

Flat continuously-compounded rate and dividend yield.

---

### `VanillaAutocall(**kwargs)`

Standard autocallable note. Autocalls on any observation date where `S ≥ barrier·S0`.
At maturity, investor receives `notional` if `S ≥ capital_barrier·S0`, otherwise
`notional · S/S0` (full downside participation).

| Parameter | Type | Description |
|---|---|---|
| `S0` | `float` | Initial spot |
| `notional` | `float` | Face value |
| `observation_dates` | `list[float]` | Years from today, sorted ascending |
| `autocall_barriers` | `list[float]` | Trigger level as fraction of S0, one per date |
| `coupon_amounts` | `list[float]` | Dollar coupon paid on autocall, one per date |
| `maturity` | `float` | Final maturity in years |
| `capital_barrier` | `float` | Downside protection level as fraction of S0 (default 1.0) |

---

### `PhoenixAutocall(**kwargs)`

Same as `VanillaAutocall` but with **memory coupons**: if the autocall barrier is
missed on earlier observation dates, those coupons accumulate and are paid when the
product eventually autocalls.

Autocall payout = `notional + coupon × (missed_dates + 1)`.

Same constructor parameters as `VanillaAutocall`.

---

### `StepDownAutocall(**kwargs)`

Same as `VanillaAutocall` but `autocall_barriers` must be **non-increasing** (the
barrier steps down over time, giving the product an increasing chance to autocall).

Raises `ValueError` if barriers are not non-increasing.

---

### `PricingEngine(vol_surface, rate_curve, N_s=400, min_steps_per_year=252, vol_bump=0.01)`

Top-level engine. Calibrates local vol and prices the product.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `vol_surface` | `VolSurface` | — | Market implied vol surface |
| `rate_curve` | `FlatRateCurve` | — | Risk-free rate + dividend yield |
| `N_s` | `int` | 400 | Spatial grid points. 200 is fine for quick estimates |
| `min_steps_per_year` | `int` | 252 | Time resolution (daily). Use 100 for fast mode |
| `vol_bump` | `float` | 0.01 | Parallel vol shift for vega (1%) |

#### `engine.price(product) → PricingResult`

| Field | Description |
|---|---|
| `price` | Fair value V(S0, t=0) in same units as notional |
| `delta` | ∂V/∂S — dollar change per unit S move |
| `gamma` | ∂²V/∂S² — change in delta per unit S move |
| `theta` | ∂V/∂t — dollar change per year of calendar time |
| `vega` | ∂V/∂σ — dollar change per 1% parallel vol shift |
| `value_grid` | Full V array on the spatial grid |
| `spot_grid` | Corresponding S values |

---

## Low-level API (advanced)

Skip the engine and call modules directly if you need to reuse a calibration or
inspect intermediate results:

```python
from autocall_pricer.pde.grid import LogSpaceGrid
from autocall_pricer.pde.solver import CrankNicolsonSolver
from autocall_pricer.models.local_vol import DupireLocalVol

# Calibrate once, reuse across many products
grid = LogSpaceGrid(S0=100.0, T=1.0)
T_cal = grid.t_grid[grid.t_grid > 0]
local_vol = DupireLocalVol(
    vol_surface=surface,
    rate_curve=rate,
    S0=100.0,
    S_grid=grid.S_grid,
    T_grid=T_cal,
)

# Price multiple products against the same local vol
solver = CrankNicolsonSolver()
for product in [product_a, product_b, product_c]:
    V = solver.solve(product, local_vol, rate, grid)
    # interpolate at S0 to get price
```

---

## Model notes

### Dupire local volatility

The local vol σ_loc(S, t) is calibrated from the implied vol surface using the
Dupire formula:

```
σ²_loc(K, T) = [∂C/∂T + (r−q)·K·∂C/∂K + q·C]
               / [½·K²·∂²C/∂K²]
```

All partials are computed via finite differences of Black-Scholes call prices. The
formula is applied only where it is numerically reliable: within **1.5σ√T** of the
forward in log-moneyness and above a minimum maturity threshold. Outside this region
the implied vol is used as a proxy (equal to local vol on a flat surface, a reasonable
approximation at the wings).

### Crank-Nicolson PDE

The BS PDE in log-space `x = log(S)`:

```
∂V/∂t + ½σ²·∂²V/∂x² + (r−q−½σ²)·∂V/∂x − rV = 0
```

is solved backward from maturity using the symmetric (θ=0.5) Crank-Nicolson scheme.
Boundary conditions:

- **Lower** (S_min = 0.1·S0): Dirichlet `V = 0`
- **Upper** (S_max = 5·S0): zero-gamma `∂²V/∂x² = 0`, implemented by eliminating
  the boundary node from the tridiagonal system and extrapolating post-solve. This is
  unconditionally stable for both bounded (autocall) and growing (call) payoffs.

### Autocall conditions

Discrete autocall barriers are applied at each observation date during backward
induction: `V[i] = autocall_value` wherever `S[i] ≥ barrier`. This is exact (no
smoothing needed) because the condition is applied at discrete time points.

### Greeks

| Greek | Method |
|---|---|
| Delta, Gamma | Central/second differences from the base `V` grid — **no extra PDE solve** |
| Theta | One additional PDE solve with maturity shortened by one time-step |
| Vega | One additional PDE solve with vol surface shifted up by `vol_bump` |

Total cost: **3 PDE solves** per `engine.price()` call.

### Out of scope (noted)

- **Worst-of basket autocallables** — require Monte Carlo; 2D/3D PDE is impractical.
- **Continuous barrier monitoring** — would require a two-state PDE or reflection.
- **Stochastic vol / local-stochastic vol** — Dupire local vol only.

---

## Running tests

```bash
pytest tests/ -v
```

16 tests across `test_products`, `test_local_vol`, and `test_pde_solver`.
Key sanity checks:
- European call PDE vs Black-Scholes closed form: < 5 bps error
- Local vol round-trip: flat surface → flat local vol
- Autocall with 200% barriers → price ≈ PV(notional)
- In-grid delta/gamma vs BS analytical greeks: < 5% / 10% relative error
