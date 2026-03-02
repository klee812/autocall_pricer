"""Step-down autocallable: autocall barrier decreases over time."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .base import AutocallProduct


@dataclass
class StepDownAutocall(AutocallProduct):
    """Step-down autocallable note.

    The autocall barrier decreases at each observation date (specified via
    ``autocall_barriers`` which should be a decreasing sequence, e.g.
    [1.0, 0.95, 0.90, 0.85]).

    ``autocall_value`` and ``terminal_payoff`` are identical to vanilla —
    only the barriers differ.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        # Soft validation: warn if barriers are not non-increasing
        b = self.autocall_barriers
        if any(b[i] < b[i + 1] for i in range(len(b) - 1)):
            raise ValueError(
                "StepDownAutocall expects non-increasing autocall_barriers "
                f"(got {b})"
            )

    def terminal_payoff(self, S: np.ndarray) -> np.ndarray:
        barrier_level = self.capital_barrier * self.S0
        return np.where(S >= barrier_level, self.notional, self.notional * (S / self.S0))
