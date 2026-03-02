"""Build a VolSurface from a filtered, IV-solved option chain.

Pipeline
--------
    raw records (OptionRecord)
        → filter_chain()            # remove bad quotes
        → solve_iv()                # compute implied vol per contract
        → build_vol_surface()       # grid + interpolate → VolSurface
"""

from __future__ import annotations

import logging
import warnings
from typing import Sequence

import numpy as np

from .iv_solver import implied_vol
from .option_chain import OptionRecord, filter_chain
from .vol_surface import VolSurface

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: solve implied vol for every record
# ---------------------------------------------------------------------------

def solve_iv(
    records: list[OptionRecord],
    prefer_otm: bool = True,
) -> list[OptionRecord]:
    """Compute implied vol for each record in-place and return the list.

    Parameters
    ----------
    records    : filtered option chain
    prefer_otm : if True and both a call and put exist at the same (K, T),
                 keep only the OTM leg (lower bid/ask spread, more liquid).
                 If False, average calls and puts via put-call parity.
    """
    solved = []
    for rec in records:
        iv = implied_vol(
            price=rec.mid,
            S=rec.S0,
            K=rec.strike,
            r=rec.r,
            T=rec.expiry,
            q=rec.q,
            option_type=rec.option_type,
        )
        if iv is None:
            log.debug("No IV solution for %s (mid=%.4f, K=%.1f, T=%.3f)",
                      rec.option_id, rec.mid, rec.strike, rec.expiry)
            continue
        rec.implied_vol = iv
        solved.append(rec)

    log.info("IV solved for %d / %d records", len(solved), len(records))
    return solved


# ---------------------------------------------------------------------------
# Step 2: organise into a (K, T) grid and build VolSurface
# ---------------------------------------------------------------------------

def build_vol_surface(
    records: list[OptionRecord],
    K_grid: np.ndarray | None = None,
    T_grid: np.ndarray | None = None,
    min_strikes_per_expiry: int = 3,
    min_expiries: int = 2,
) -> VolSurface:
    """Fit a VolSurface from IV-solved option records.

    If K_grid / T_grid are not provided they are derived automatically from
    the data (unique expiries; strikes interpolated onto a regular grid).

    Parameters
    ----------
    records                : list with `implied_vol` already populated
    K_grid                 : explicit strike grid to interpolate onto
    T_grid                 : explicit expiry grid to use as surface pillars
    min_strikes_per_expiry : drop expiry slices with too few liquid strikes
    min_expiries           : raise if the surface has fewer than this many expiries

    Returns
    -------
    VolSurface
    """
    # Collect (K, T, iv) triples
    points: list[tuple[float, float, float]] = []
    for rec in records:
        if rec.implied_vol is not None:
            points.append((rec.strike, rec.expiry, rec.implied_vol))

    if not points:
        raise ValueError("No valid IV points — cannot build surface.")

    K_arr = np.array([p[0] for p in points])
    T_arr = np.array([p[1] for p in points])
    iv_arr = np.array([p[2] for p in points])

    # --- Expiry grid ---
    if T_grid is None:
        T_unique = np.unique(np.round(T_arr, 6))
        T_grid = T_unique
    T_grid = np.asarray(T_grid, dtype=float)

    # --- Strike grid ---
    if K_grid is None:
        K_min = K_arr.min()
        K_max = K_arr.max()
        # 30-point grid spanning the data range
        K_grid = np.linspace(K_min, K_max, 30)
    K_grid = np.asarray(K_grid, dtype=float)

    # --- Fill sigma_grid by slice-wise interpolation ---
    sigma_grid = np.full((len(K_grid), len(T_grid)), np.nan)

    for j, T in enumerate(T_grid):
        # Find records closest to this expiry (exact match or interpolate)
        mask = np.isclose(T_arr, T, atol=1e-5)
        if mask.sum() < min_strikes_per_expiry:
            log.warning("Expiry T=%.4f has only %d IV points (need %d) — skipped",
                        T, mask.sum(), min_strikes_per_expiry)
            continue

        K_slice = K_arr[mask]
        iv_slice = iv_arr[mask]
        order = np.argsort(K_slice)
        K_slice = K_slice[order]
        iv_slice = iv_slice[order]

        # Interpolate onto the target K_grid (linear, flat extrapolation)
        sigma_grid[:, j] = np.interp(K_grid, K_slice, iv_slice,
                                      left=iv_slice[0], right=iv_slice[-1])

    # Drop columns (expiries) that have no data
    valid_cols = ~np.all(np.isnan(sigma_grid), axis=0)
    if valid_cols.sum() < min_expiries:
        raise ValueError(
            f"Only {valid_cols.sum()} expiry slices have enough data "
            f"(need {min_expiries}). Check filtering thresholds."
        )

    T_grid = T_grid[valid_cols]
    sigma_grid = sigma_grid[:, valid_cols]

    # Fill any remaining NaNs by forward/backward fill along the T axis
    for i in range(len(K_grid)):
        row = sigma_grid[i, :]
        if np.any(np.isnan(row)):
            sigma_grid[i, :] = _fill_nans(row)

    return VolSurface(K_grid, T_grid, sigma_grid)


def _fill_nans(arr: np.ndarray) -> np.ndarray:
    """Forward-fill then backward-fill NaNs in a 1-D array."""
    out = arr.copy()
    # Forward fill
    last = np.nan
    for i in range(len(out)):
        if not np.isnan(out[i]):
            last = out[i]
        elif not np.isnan(last):
            out[i] = last
    # Backward fill
    last = np.nan
    for i in range(len(out) - 1, -1, -1):
        if not np.isnan(out[i]):
            last = out[i]
        elif not np.isnan(last):
            out[i] = last
    return out


# ---------------------------------------------------------------------------
# Convenience: run the full pipeline from raw records
# ---------------------------------------------------------------------------

def chain_to_surface(
    records: list[OptionRecord],
    filter_kwargs: dict | None = None,
    K_grid: np.ndarray | None = None,
    T_grid: np.ndarray | None = None,
) -> VolSurface:
    """Run the full pipeline: filter → solve IV → build surface.

    Parameters
    ----------
    records       : raw OptionRecord list from the API client
    filter_kwargs : keyword args forwarded to filter_chain()
    K_grid        : optional explicit strike grid
    T_grid        : optional explicit expiry grid

    Returns
    -------
    VolSurface ready for PricingEngine
    """
    filtered = filter_chain(records, **(filter_kwargs or {}))
    log.info("%d records after filtering (was %d)", len(filtered), len(records))

    solved = solve_iv(filtered)
    return build_vol_surface(solved, K_grid=K_grid, T_grid=T_grid)
