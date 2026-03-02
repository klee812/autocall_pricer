"""Log-space spatial grid for the Black-Scholes PDE."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class LogSpaceGrid:
    """Uniform grid in log(S) space.

    Parameters
    ----------
    S0 : float
        Current spot; used to centre the grid.
    T : float
        Maturity; controls number of time steps.
    N_s : int
        Number of spatial points (default 400).
    S_min_factor : float
        ``S_min = S_min_factor * S0`` (default 0.1 — 10 × downside).
    S_max_factor : float
        ``S_max = S_max_factor * S0`` (default 5.0 — 5 × upside).
    dt_target : float
        Target time-step size in years.  Overridden by ``min_steps_per_year``
        to ensure a minimum resolution.
    min_steps_per_year : int
        Minimum number of time steps per year (default 252, approx daily).
    """

    S0: float
    T: float
    N_s: int = 400
    S_min_factor: float = 0.1
    S_max_factor: float = 5.0
    min_steps_per_year: int = 252

    # Derived attributes filled in __post_init__
    S_grid: np.ndarray = field(init=False)
    x_grid: np.ndarray = field(init=False)
    dx: float = field(init=False)
    N_t: int = field(init=False)
    dt: float = field(init=False)
    t_grid: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        x_min = np.log(self.S_min_factor * self.S0)
        x_max = np.log(self.S_max_factor * self.S0)

        self.x_grid = np.linspace(x_min, x_max, self.N_s)
        self.dx = self.x_grid[1] - self.x_grid[0]
        self.S_grid = np.exp(self.x_grid)

        # Number of time steps: at least min_steps_per_year * T
        self.N_t = max(int(np.ceil(self.min_steps_per_year * self.T)), 100)
        self.dt = self.T / self.N_t
        # t_grid[0]=0, t_grid[-1]=T (forward time, but we step backward)
        self.t_grid = np.linspace(0.0, self.T, self.N_t + 1)
