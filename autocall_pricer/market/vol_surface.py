from __future__ import annotations

import numpy as np
from scipy.interpolate import RectBivariateSpline


class VolSurface:
    """Implied volatility surface with smooth bivariate spline interpolation.

    Parameters
    ----------
    K_grid : 1-D array, shape (nK,)
        Sorted strike grid (absolute levels, same units as S0).
    T_grid : 1-D array, shape (nT,)
        Sorted expiry grid in years (strictly positive).
    sigma_grid : 2-D array, shape (nK, nT)
        Implied Black-Scholes volatilities (annualised, as decimals).
    """

    def __init__(
        self,
        K_grid: np.ndarray,
        T_grid: np.ndarray,
        sigma_grid: np.ndarray,
    ) -> None:
        self.K_grid = np.asarray(K_grid, dtype=float)
        self.T_grid = np.asarray(T_grid, dtype=float)
        self.sigma_grid = np.asarray(sigma_grid, dtype=float)

        if self.sigma_grid.shape != (len(self.K_grid), len(self.T_grid)):
            raise ValueError(
                f"sigma_grid shape {self.sigma_grid.shape} does not match "
                f"(len(K_grid)={len(self.K_grid)}, len(T_grid)={len(self.T_grid)})"
            )

        # Spline on total implied variance W = σ² · T to ensure smooth ∂C/∂T
        self._spline = RectBivariateSpline(
            self.K_grid, self.T_grid, self.sigma_grid, kx=3, ky=3
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def implied_vol(self, K: float | np.ndarray, T: float | np.ndarray) -> np.ndarray:
        """Return implied vol σ(K, T) via spline; clipped to [0.001, 5]."""
        K = np.atleast_1d(K)
        T = np.atleast_1d(T)
        return np.clip(self._spline(K, T, grid=False), 0.001, 5.0)

    # ------------------------------------------------------------------
    # Derivatives used by Dupire formula
    # All derivatives are of the *call price* C(K, T) = BS(K, T, σ(K,T))
    # but we expose them via the underlying vol-spline derivatives so the
    # caller can construct C and its partials analytically.
    # ------------------------------------------------------------------

    def sigma_and_partials(
        self, K: np.ndarray, T: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return (σ, ∂σ/∂T, ∂σ/∂K, ∂²σ/∂K²) on a 2-D (K×T) grid.

        Parameters
        ----------
        K : 1-D array of length nK
        T : 1-D array of length nT
        Returns arrays shaped (nK, nT).
        """
        sigma = self._spline(K, T)
        dsigma_dT = self._spline(K, T, dy=1)
        dsigma_dK = self._spline(K, T, dx=1)
        d2sigma_dK2 = self._spline(K, T, dx=2)
        return sigma, dsigma_dT, dsigma_dK, d2sigma_dK2
