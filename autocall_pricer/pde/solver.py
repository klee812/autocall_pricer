"""Crank-Nicolson backward-induction PDE solver for autocallable products."""

from __future__ import annotations

import numpy as np
from scipy.linalg import solve_banded

from ..products.base import AutocallProduct
from ..models.local_vol import DupireLocalVol
from ..market.rates import FlatRateCurve
from .grid import LogSpaceGrid
from .operators import build_coefficients, apply_explicit, to_banded


class CrankNicolsonSolver:
    """Backward-induction Crank-Nicolson solver on a log-space grid.

    Usage
    -----
    solver = CrankNicolsonSolver()
    V = solver.solve(product, local_vol, rate_curve, grid)
    """

    def solve(
        self,
        product: AutocallProduct,
        local_vol: DupireLocalVol,
        rate_curve: FlatRateCurve,
        grid: LogSpaceGrid,
        theta: float = 0.5,
    ) -> np.ndarray:
        """Run the backward PDE and return the value vector at t=0.

        Parameters
        ----------
        product     : AutocallProduct instance
        local_vol   : calibrated DupireLocalVol
        rate_curve  : FlatRateCurve
        grid        : LogSpaceGrid
        theta       : Crank-Nicolson parameter (0.5)

        Returns
        -------
        V : 1-D array of shape (N_s,) — option value at t=0 on the spot grid.
        """
        r = rate_curve.rate
        q = rate_curve.dividend
        S = grid.S_grid
        dx = grid.dx
        dt = grid.dt
        N_s = grid.N_s

        # --- 1. Terminal condition ---
        V = product.terminal_payoff(S).astype(float)
        # Initialise upper boundary with zero-gamma extrapolation so the first
        # backward step uses a consistent value at the ghost boundary.
        V[-1] = 2.0 * V[-2] - V[-3]

        # Precompute autocall observation dates and barriers
        obs_dates = np.asarray(product.observation_dates)
        barriers = np.asarray(product.autocall_barriers) * product.S0  # absolute levels

        # Build a time-indexed set of observation date indices (in backward time)
        # t_grid is forward: t_grid[k] = k·dt;  backward step k means t = T − k·dt
        t_fwd = grid.t_grid  # shape (N_t+1,); t_fwd[-1] = T

        # For each obs_date find the closest time-grid index (in forward time)
        obs_t_idx = {}  # forward-time index → (date_idx, barrier, autocall_val)
        for i, t_obs in enumerate(obs_dates):
            if t_obs > grid.T + 1e-10:
                continue
            k = int(round(t_obs / dt))
            k = np.clip(k, 0, grid.N_t)
            obs_t_idx[k] = i  # if duplicate, later date wins (shouldn't happen)

        # Track coupons paid (for Phoenix memory feature)
        # coupons_paid[i] counts how many observation dates <= obs_dates[i]
        # have already triggered before the current one.  We walk backwards
        # so we track by global counter reset each solve.
        coupons_paid_counter = 0

        # --- 2. Backward induction ---
        for step in range(grid.N_t, 0, -1):
            # Current forward time (before this backward step) = t_fwd[step]
            t_current = t_fwd[step]
            t_next = t_fwd[step - 1]
            t_mid = 0.5 * (t_current + t_next)

            # Local vol at spatial nodes, evaluated at t_mid
            sigma = local_vol.sigma_local(S, np.full_like(S, t_mid))
            sigma = np.clip(sigma, local_vol.sigma_min, local_vol.sigma_max)

            # --- 2a. Apply autocall condition at observation dates ---
            if step in obs_t_idx:
                date_idx = obs_t_idx[step]
                barrier_level = barriers[date_idx]
                autocall_val = product.autocall_value(date_idx, coupons_paid_counter)
                triggered = S >= barrier_level
                V = np.where(triggered, autocall_val, V)
                if not np.any(triggered):
                    coupons_paid_counter += 1

            # --- 2b. Assemble and solve tridiagonal system ---
            a, b, c = build_coefficients(sigma, r, q, dx, dt, theta)
            rhs = apply_explicit(V, sigma, r, q, dx, dt, theta)

            # --- 2c. Boundary conditions ---
            # Lower BC: Dirichlet V → 0 as S → 0 (S_min = 0.1·S0)
            b[0] = 1.0
            a[0] = 0.0
            c[0] = 0.0
            rhs[0] = 0.0

            # Upper BC: zero-gamma (∂²V/∂x² = 0) in log-space.
            # V[N-1] = 2·V[N-2] − V[N-3]
            # Incorporate into the last interior row (N-2) by substitution,
            # then set the last row as an identity placeholder.
            # This avoids the instability of "old-value extrapolation" which
            # exponentially amplifies growing payoffs (e.g. European calls).
            b[-2] += 2.0 * c[-2]
            a[-2] -= c[-2]
            c[-2] = 0.0
            b[-1] = 1.0
            a[-1] = 0.0
            c[-1] = 0.0
            rhs[-1] = 0.0  # placeholder; will be overridden below

            ab = to_banded(a, b, c)
            V = solve_banded((1, 1), ab, rhs)
            # Apply zero-gamma BC using the newly computed interior values
            V[-1] = 2.0 * V[-2] - V[-3]

        return V
