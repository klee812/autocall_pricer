"""Dupire local volatility calibration from an implied vol surface."""

from __future__ import annotations

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.stats import norm

from ..market.vol_surface import VolSurface
from ..market.rates import FlatRateCurve


def _bs_call(S0: float, K: np.ndarray, r: float, q: float, sigma: np.ndarray, T: float) -> np.ndarray:
    """Black-Scholes European call price (vectorised over K and sigma)."""
    df = np.exp(-r * T)
    F = S0 * np.exp((r - q) * T)
    sqrtT = np.sqrt(max(T, 1e-10))
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrtT)
        d2 = d1 - sigma * sqrtT
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


class DupireLocalVol:
    """Dupire local volatility surface calibrated from an implied vol surface.

    Parameters
    ----------
    vol_surface : VolSurface
    rate_curve  : FlatRateCurve
    S0          : float
        Current spot price (reference for computing BS call prices in Dupire).
    S_grid      : 1-D array of spot prices for the calibration grid.
    T_grid      : 1-D array of expiries for the calibration grid.
    sigma_min   : lower clip for local vol (default 0.01 = 1%).
    sigma_max   : upper clip for local vol (default 5.0 = 500%).
    eps_K_rel   : relative finite-difference step for ∂/∂K (default 1e-3).
    eps_T_abs   : absolute finite-difference step for ∂/∂T (default 1e-3 yr).
    """

    def __init__(
        self,
        vol_surface: VolSurface,
        rate_curve: FlatRateCurve,
        S0: float,
        S_grid: np.ndarray,
        T_grid: np.ndarray,
        sigma_min: float = 0.01,
        sigma_max: float = 5.0,
        eps_K_rel: float = 1e-3,
        eps_T_abs: float = 1e-3,
    ) -> None:
        self.vol_surface = vol_surface
        self.rate_curve = rate_curve
        self.S0 = S0
        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self._eps_K_rel = eps_K_rel
        self._eps_T_abs = eps_T_abs

        self._S_grid = np.asarray(S_grid, dtype=float)
        self._T_grid = np.asarray(T_grid, dtype=float)

        sigma_loc_grid = self._calibrate(self._S_grid, self._T_grid)
        self._interp = RegularGridInterpolator(
            (self._S_grid, self._T_grid),
            sigma_loc_grid,
            method="linear",
            bounds_error=False,
            fill_value=None,
        )

    def sigma_local(self, S: np.ndarray, t: np.ndarray) -> np.ndarray:
        """Return local vol σ_loc(S, t) for arbitrary (S, t) pairs."""
        pts = np.stack(
            [np.asarray(S, dtype=float).ravel(), np.asarray(t, dtype=float).ravel()],
            axis=-1,
        )
        return self._interp(pts).reshape(np.asarray(S).shape)

    # ------------------------------------------------------------------
    # Internal calibration via numerical Dupire
    # ------------------------------------------------------------------

    def _surface_sigma(self, K: np.ndarray, T: float) -> np.ndarray:
        """Return implied vol from surface, clipped and clamped to surface bounds."""
        K_clipped = np.clip(K, self.vol_surface.K_grid.min(), self.vol_surface.K_grid.max())
        T_clipped = float(np.clip(T, self.vol_surface.T_grid.min(), self.vol_surface.T_grid.max()))
        return np.clip(
            self.vol_surface.implied_vol(K_clipped, np.full_like(K_clipped, T_clipped)),
            0.001, 5.0,
        )

    def _call_price(self, K: np.ndarray, T: float) -> np.ndarray:
        """BS call price C(K, T) using market implied vol."""
        sigma = self._surface_sigma(K, T)
        return _bs_call(self.S0, K, self.rate_curve.rate, self.rate_curve.dividend, sigma, T)

    def _calibrate(self, S_grid: np.ndarray, T_grid: np.ndarray) -> np.ndarray:
        """Return local vol on (len(S_grid), len(T_grid)) grid via Dupire formula.

        Dupire:
            σ²_loc(K, T) = [∂C/∂T + (r−q)·K·∂C/∂K + q·C]
                           / [½·K²·∂²C/∂K²]

        All partials computed by central finite differences of BS call prices.
        For K values outside the vol surface strike range, the Dupire formula
        breaks down (d²C/dK² → 0 for deep ITM/OTM), so we fall back to using
        the implied volatility as a proxy for local vol at those nodes.
        """
        r = self.rate_curve.rate
        q = self.rate_curve.dividend

        # Nodes where Dupire can be reliably applied.
        # Two conditions must both hold:
        #   1. K is strictly inside the vol surface range (avoid spline edge effects)
        #   2. K is within a reasonable moneyness band around the forward F(T):
        #      d²C/dK² ≈ 0 for deep ITM/OTM options → Dupire denominator blow-up.
        K_surf_min = self.vol_surface.K_grid.min() * 1.05
        K_surf_max = self.vol_surface.K_grid.max() * 0.95
        T_surf_min = float(self.vol_surface.T_grid.min())
        # Minimum T for reliable Dupire: needs a proper ∂C/∂T central difference
        # Below T_surf_min + eps_T, the backward step hits the surface boundary
        # and the finite difference becomes one-sided / incorrectly oriented.
        T_dupire_min = T_surf_min + 2.0 * self._eps_T_abs

        sigma_loc = np.empty((len(S_grid), len(T_grid)), dtype=float)

        for j, T in enumerate(T_grid):
            T = float(T)
            if T <= 0.0 or T < T_dupire_min:
                # Use implied vol directly for very short or zero maturities
                sigma_loc[:, j] = self._surface_sigma(S_grid, T)
                continue

            K = S_grid  # treat each spot grid node as the option strike

            eps_K = K * self._eps_K_rel
            eps_T = max(T * 1e-2, self._eps_T_abs)
            T_lo = max(T - eps_T, self.vol_surface.T_grid.min())
            T_hi = T + eps_T

            # Implied vol fallback (used for out-of-surface-range nodes)
            sigma_impl = self._surface_sigma(K, T)

            C = self._call_price(K, T)
            C_Kp = self._call_price(K + eps_K, T)
            C_Km = self._call_price(K - eps_K, T)
            C_Tp = self._call_price(K, T_hi)
            C_Tm = self._call_price(K, T_lo)

            dCdT = (C_Tp - C_Tm) / (T_hi - T_lo)
            dCdK = (C_Kp - C_Km) / (2.0 * eps_K)
            d2CdK2 = (C_Kp - 2.0 * C + C_Km) / eps_K**2

            numerator = dCdT + (r - q) * K * dCdK + q * C
            denominator = 0.5 * K**2 * d2CdK2

            # Forward for moneyness bound
            F = self.S0 * np.exp((r - q) * T)
            # Dupire is reliable only where d²C/dK² is meaningfully positive.
            # This requires the option to be close to at-the-money: we require
            # |ln(K/F)| ≤ n_sigma · σ_impl · √T (within n_sigma standard devs).
            # Outside this band, fall back to implied vol.
            n_sigma = 1.5
            log_moneyness = np.abs(np.log(K / F))
            atm_band = n_sigma * sigma_impl * np.sqrt(T)
            in_range = (
                (K >= K_surf_min) & (K <= K_surf_max)
                & (log_moneyness <= atm_band)
            )

            with np.errstate(divide="ignore", invalid="ignore"):
                var_dupire = np.where(
                    in_range & (denominator > 1e-12),
                    numerator / denominator,
                    sigma_impl**2,  # fallback: implied vol as proxy
                )

            var_loc = np.clip(var_dupire, self.sigma_min**2, self.sigma_max**2)
            sigma_loc[:, j] = np.sqrt(var_loc)

        return sigma_loc
