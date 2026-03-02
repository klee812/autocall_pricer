"""Tests for DupireLocalVol calibration.

Verification:
  1. Flat surface round-trip: calibrate from flat implied vol → local vol must be
     approximately flat everywhere.
"""

import numpy as np
import pytest

from autocall_pricer.market.vol_surface import VolSurface
from autocall_pricer.market.rates import FlatRateCurve
from autocall_pricer.models.local_vol import DupireLocalVol


def make_flat_surface(sigma: float, S0: float = 100.0) -> VolSurface:
    K_grid = np.linspace(0.5 * S0, 1.5 * S0, 30)
    T_grid = np.linspace(0.1, 2.0, 20)
    sigma_grid = np.full((len(K_grid), len(T_grid)), sigma)
    return VolSurface(K_grid, T_grid, sigma_grid)


class TestDupireLocalVol:
    S0 = 100.0
    sigma_flat = 0.20

    def _calibrate(self, sigma: float = None, r: float = 0.05, q: float = 0.0):
        if sigma is None:
            sigma = self.sigma_flat
        surface = make_flat_surface(sigma, S0=self.S0)
        rate = FlatRateCurve(rate=r, dividend=q)
        S_grid = np.linspace(60, 140, 50)
        T_grid = np.linspace(0.1, 2.0, 20)
        return DupireLocalVol(surface, rate, S0=self.S0, S_grid=S_grid, T_grid=T_grid)

    def test_flat_surface_round_trip(self):
        """Local vol should be approximately σ_flat when implied vol is flat."""
        lv = self._calibrate()
        S_test = np.array([80.0, 100.0, 120.0])
        T_test = np.array([0.5, 1.0, 1.5])
        for S in S_test:
            for T in T_test:
                sig = lv.sigma_local(np.array([S]), np.array([T]))[0]
                assert abs(sig - self.sigma_flat) < 0.03, (
                    f"Local vol {sig:.4f} deviates more than 3% from flat "
                    f"σ={self.sigma_flat} at S={S}, T={T}"
                )

    def test_sigma_clipped_above_min(self):
        """Local vol must never fall below sigma_min."""
        lv = self._calibrate()
        S_grid = lv._S_grid
        T_grid = lv._T_grid
        pts_S, pts_T = np.meshgrid(S_grid, T_grid, indexing="ij")
        sigma = lv.sigma_local(pts_S.ravel(), pts_T.ravel())
        assert np.all(sigma >= lv.sigma_min - 1e-10), "Local vol below sigma_min"

    def test_different_rates(self):
        """Calibration should work with non-zero rate and dividend."""
        lv = self._calibrate(r=0.03, q=0.02)
        sig = lv.sigma_local(np.array([self.S0]), np.array([1.0]))[0]
        assert 0.01 < sig < 1.0, f"Unexpected local vol value: {sig}"
