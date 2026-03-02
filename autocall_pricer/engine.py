"""Top-level pricing engine: calibrates local vol and computes Greeks."""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import interp1d

from .market.vol_surface import VolSurface
from .market.rates import FlatRateCurve
from .models.local_vol import DupireLocalVol
from .products.base import AutocallProduct
from .pde.grid import LogSpaceGrid
from .pde.solver import CrankNicolsonSolver


@dataclass
class PricingResult:
    """Greeks and value grid returned by PricingEngine."""

    price: float          # V(S0, t=0)
    delta: float          # ∂V/∂S  (central difference from value grid)
    gamma: float          # ∂²V/∂S² (second difference from value grid)
    theta: float          # ∂V/∂t  (one additional backward step)
    vega: float           # ∂V/∂σ  (parallel vol bump +1%)
    value_grid: np.ndarray
    spot_grid: np.ndarray


class PricingEngine:
    """Calibrates Dupire local vol and prices a single-underlying autocallable.

    Parameters
    ----------
    vol_surface : VolSurface
        Market implied vol surface.
    rate_curve : FlatRateCurve
        Risk-free rate + dividend yield.
    N_s : int
        Number of spatial grid points (default 400).
    min_steps_per_year : int
        Minimum PDE time steps per year (default 252).
    vol_bump : float
        Parallel vol shift for vega (default 0.01 = +1%).
    """

    def __init__(
        self,
        vol_surface: VolSurface,
        rate_curve: FlatRateCurve,
        N_s: int = 400,
        min_steps_per_year: int = 252,
        vol_bump: float = 0.01,
    ) -> None:
        self.vol_surface = vol_surface
        self.rate_curve = rate_curve
        self.N_s = N_s
        self.min_steps_per_year = min_steps_per_year
        self.vol_bump = vol_bump

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def price(self, product: AutocallProduct) -> PricingResult:
        """Price the product and compute all first-order Greeks.

        Parameters
        ----------
        product : AutocallProduct

        Returns
        -------
        PricingResult
        """
        grid = LogSpaceGrid(
            S0=product.S0,
            T=product.maturity,
            N_s=self.N_s,
            min_steps_per_year=self.min_steps_per_year,
        )

        # 1. Calibrate local vol
        local_vol = self._calibrate_local_vol(product, grid)

        # 2. Base price
        solver = CrankNicolsonSolver()
        V = solver.solve(product, local_vol, self.rate_curve, grid)
        price_val = self._interpolate(V, grid.S_grid, product.S0)

        # 3. Delta and Gamma from the value grid (central differences in S)
        delta_val, gamma_val = self._delta_gamma(V, grid.S_grid, product.S0)

        # 4. Theta: one additional backward step (advance t by dt)
        theta_val = self._theta(product, local_vol, grid, price_val)

        # 5. Vega: parallel vol bump
        vega_val = self._vega(product, grid, price_val)

        return PricingResult(
            price=price_val,
            delta=delta_val,
            gamma=gamma_val,
            theta=theta_val,
            vega=vega_val,
            value_grid=V,
            spot_grid=grid.S_grid,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _calibrate_local_vol(
        self, product: AutocallProduct, grid: LogSpaceGrid
    ) -> DupireLocalVol:
        S_cal = grid.S_grid
        T_cal = grid.t_grid[grid.t_grid > 0]  # exclude t=0
        return DupireLocalVol(
            vol_surface=self.vol_surface,
            rate_curve=self.rate_curve,
            S0=product.S0,
            S_grid=S_cal,
            T_grid=T_cal,
        )

    @staticmethod
    def _interpolate(V: np.ndarray, S_grid: np.ndarray, S0: float) -> float:
        itp = interp1d(S_grid, V, kind="cubic", bounds_error=False, fill_value="extrapolate")
        return float(itp(S0))

    @staticmethod
    def _delta_gamma(
        V: np.ndarray, S_grid: np.ndarray, S0: float
    ) -> tuple[float, float]:
        """Central difference delta and gamma using the two grid points nearest S0."""
        itp = interp1d(S_grid, V, kind="cubic", bounds_error=False, fill_value="extrapolate")
        h = S0 * 0.01
        Vp = float(itp(S0 + h))
        Vm = float(itp(S0 - h))
        V0 = float(itp(S0))
        delta = (Vp - Vm) / (2.0 * h)
        gamma = (Vp - 2.0 * V0 + Vm) / h**2
        return delta, gamma

    def _theta(
        self,
        product: AutocallProduct,
        local_vol: DupireLocalVol,
        grid: LogSpaceGrid,
        price_0: float,
    ) -> float:
        """Theta via a one-step shorter maturity solve (calendar time by dt)."""
        if grid.dt <= 0:
            return 0.0
        # Shrink maturity by one dt and reprice
        mod_product = copy.copy(product)
        mod_product.maturity = max(product.maturity - grid.dt, grid.dt)
        short_grid = LogSpaceGrid(
            S0=product.S0,
            T=mod_product.maturity,
            N_s=grid.N_s,
            min_steps_per_year=self.min_steps_per_year,
        )
        solver = CrankNicolsonSolver()
        V_short = solver.solve(mod_product, local_vol, self.rate_curve, short_grid)
        price_short = self._interpolate(V_short, short_grid.S_grid, product.S0)
        return (price_short - price_0) / grid.dt  # ∂V/∂t per year

    def _vega(
        self,
        product: AutocallProduct,
        grid: LogSpaceGrid,
        price_0: float,
    ) -> float:
        """Vega via parallel +1% vol bump and reprice."""
        bumped_sigma = self.vol_surface.sigma_grid + self.vol_bump
        bumped_surface = VolSurface(
            self.vol_surface.K_grid,
            self.vol_surface.T_grid,
            bumped_sigma,
        )
        bumped_lv = DupireLocalVol(
            vol_surface=bumped_surface,
            rate_curve=self.rate_curve,
            S0=product.S0,
            S_grid=grid.S_grid,
            T_grid=grid.t_grid[grid.t_grid > 0],
        )
        solver = CrankNicolsonSolver()
        V_bump = solver.solve(product, bumped_lv, self.rate_curve, grid)
        price_bump = self._interpolate(V_bump, grid.S_grid, product.S0)
        return (price_bump - price_0) / self.vol_bump
