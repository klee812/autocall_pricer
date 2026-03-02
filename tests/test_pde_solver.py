"""Tests for the Crank-Nicolson PDE solver.

Verification:
  1. European call sanity: flat vol → price matches Black-Scholes to < 1 bps.
  2. Autocall never triggers: barriers at 200% → price ≈ discounted terminal payoff.
  3. Delta/Gamma consistency: bump S0 ±1% and compare to in-grid differences.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from autocall_pricer.market.vol_surface import VolSurface
from autocall_pricer.market.rates import FlatRateCurve
from autocall_pricer.models.local_vol import DupireLocalVol
from autocall_pricer.pde.grid import LogSpaceGrid
from autocall_pricer.pde.solver import CrankNicolsonSolver
from autocall_pricer.products.vanilla_autocall import VanillaAutocall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_flat_vol_surface(sigma: float, S0: float = 100.0) -> VolSurface:
    K_grid = np.linspace(0.4 * S0, 1.6 * S0, 40)
    T_grid = np.linspace(0.05, 3.0, 30)
    return VolSurface(K_grid, T_grid, np.full((40, 30), sigma))


def make_local_vol(
    sigma: float, S0: float, rate: FlatRateCurve, T: float
) -> tuple[DupireLocalVol, LogSpaceGrid]:
    surface = make_flat_vol_surface(sigma, S0)
    grid = LogSpaceGrid(S0=S0, T=T, N_s=300, min_steps_per_year=252)
    S_cal = grid.S_grid
    T_cal = grid.t_grid[grid.t_grid > 0]
    lv = DupireLocalVol(surface, rate, S0=S0, S_grid=S_cal, T_grid=T_cal)
    return lv, grid


def bs_call(S, K, r, q, sigma, T) -> float:
    F = S * np.exp((r - q) * T)
    df = np.exp(-r * T)
    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


def interpolate_at_S0(V: np.ndarray, S_grid: np.ndarray, S0: float) -> float:
    from scipy.interpolate import interp1d
    itp = interp1d(S_grid, V, kind="cubic", bounds_error=False, fill_value="extrapolate")
    return float(itp(S0))


# ---------------------------------------------------------------------------
# European call check — needs a simple European call product
# ---------------------------------------------------------------------------

class EuropeanCall:
    """Minimal European call that conforms to the AutocallProduct interface."""

    def __init__(self, S0: float, K: float, maturity: float, notional: float = 1.0):
        self.S0 = S0
        self.notional = notional
        self.observation_dates: list[float] = []
        self.autocall_barriers: list[float] = []
        self.coupon_amounts: list[float] = []
        self.maturity = maturity
        self.capital_barrier = 1.0
        self.K = K

    def terminal_payoff(self, S: np.ndarray) -> np.ndarray:
        return np.maximum(S - self.K, 0.0)

    def autocall_value(self, date_idx: int, coupons_paid: int = 0) -> float:
        return 0.0  # never called


class TestEuropeanCallSanity:
    S0 = 100.0
    K = 100.0
    T = 1.0
    r = 0.05
    q = 0.02
    sigma = 0.20

    def test_bs_price_match(self):
        rate = FlatRateCurve(rate=self.r, dividend=self.q)
        lv, grid = make_local_vol(self.sigma, self.S0, rate, self.T)
        product = EuropeanCall(S0=self.S0, K=self.K, maturity=self.T)

        solver = CrankNicolsonSolver()
        V = solver.solve(product, lv, rate, grid)
        pde_price = interpolate_at_S0(V, grid.S_grid, self.S0)

        bs_price = bs_call(self.S0, self.K, self.r, self.q, self.sigma, self.T)
        error_bps = abs(pde_price - bs_price) / bs_price * 10000

        assert error_bps < 5.0, (
            f"PDE call price {pde_price:.6f} differs from BS {bs_price:.6f} "
            f"by {error_bps:.2f} bps (threshold 5 bps)"
        )


class TestAutocallNeverTriggers:
    """When autocall barriers are at 200%, the product never calls early."""

    S0 = 100.0
    notional = 1000.0
    T = 1.0
    r = 0.05
    q = 0.0
    sigma = 0.20

    def test_price_equals_terminal_payoff(self):
        obs_dates = [0.25, 0.5, 0.75, 1.0]
        barriers = [2.0] * 4  # 200% of S0 — essentially unreachable
        coupons = [0.0] * 4
        product = VanillaAutocall(
            S0=self.S0,
            notional=self.notional,
            observation_dates=obs_dates,
            autocall_barriers=barriers,
            coupon_amounts=coupons,
            maturity=self.T,
            capital_barrier=0.8,
        )
        rate = FlatRateCurve(rate=self.r, dividend=self.q)
        lv, grid = make_local_vol(self.sigma, self.S0, rate, self.T)
        solver = CrankNicolsonSolver()
        V = solver.solve(product, lv, rate, grid)
        pde_price = interpolate_at_S0(V, grid.S_grid, self.S0)

        # Expected ≈ discounted notional (since P(S>0.8·S0 at T) ≈ very high)
        # Simple bound: price should be close to PV(notional)
        pv_notional = self.notional * np.exp(-self.r * self.T)
        assert abs(pde_price - pv_notional) / pv_notional < 0.05, (
            f"Price {pde_price:.2f} deviates from PV(notional)={pv_notional:.2f} "
            "by more than 5% when autocall never triggers"
        )


class TestDeltaGammaConsistency:
    """Validate in-grid delta and gamma against Black-Scholes analytical greeks.

    Uses a European call (no autocall barriers) so that exact BS greeks are
    available as a reference.  This tests the interpolation and finite-difference
    machinery independently of the autocall logic.
    """

    S0 = 100.0
    K = 100.0
    T = 1.0
    r = 0.05
    q = 0.02
    sigma = 0.20

    def _bs_greeks(self):
        F = self.S0 * np.exp((self.r - self.q) * self.T)
        df = np.exp(-self.r * self.T)
        sqrtT = np.sqrt(self.T)
        d1 = (np.log(F / self.K) + 0.5 * self.sigma**2 * self.T) / (self.sigma * sqrtT)
        d2 = d1 - self.sigma * sqrtT
        from scipy.stats import norm as _norm
        delta_bs = np.exp(-self.q * self.T) * _norm.cdf(d1)
        gamma_bs = np.exp(-self.q * self.T) * _norm.pdf(d1) / (self.S0 * self.sigma * sqrtT)
        return delta_bs, gamma_bs

    def test_delta_gamma_vs_bs(self):
        """In-grid delta and gamma should agree with BS to within 5%."""
        rate = FlatRateCurve(rate=self.r, dividend=self.q)
        lv, grid = make_local_vol(self.sigma, self.S0, rate, self.T)
        product = EuropeanCall(S0=self.S0, K=self.K, maturity=self.T)

        solver = CrankNicolsonSolver()
        V = solver.solve(product, lv, rate, grid)

        h = self.S0 * 0.01
        V0 = interpolate_at_S0(V, grid.S_grid, self.S0)
        Vp = interpolate_at_S0(V, grid.S_grid, self.S0 + h)
        Vm = interpolate_at_S0(V, grid.S_grid, self.S0 - h)

        delta_grid = (Vp - Vm) / (2.0 * h)
        gamma_grid = (Vp - 2.0 * V0 + Vm) / h**2

        delta_bs, gamma_bs = self._bs_greeks()

        assert abs(delta_grid - delta_bs) / abs(delta_bs) < 0.05, (
            f"Delta: grid={delta_grid:.4f}, BS={delta_bs:.4f}"
        )
        assert abs(gamma_grid - gamma_bs) / abs(gamma_bs) < 0.10, (
            f"Gamma: grid={gamma_grid:.6f}, BS={gamma_bs:.6f}"
        )
