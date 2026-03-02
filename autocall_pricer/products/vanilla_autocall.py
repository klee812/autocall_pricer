from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .base import AutocallProduct


@dataclass
class VanillaAutocall(AutocallProduct):
    """Standard (vanilla) autocallable note.

    Terminal payoff at maturity T:
      - If S >= capital_barrier · S0: investor receives ``notional``
      - Otherwise: investor receives ``notional · (S / S0)``  (full downside)

    On any observation date i where S >= autocall_barriers[i] · S0:
    investor receives ``notional + coupon_amounts[i]`` and the product terminates.
    """

    def terminal_payoff(self, S: np.ndarray) -> np.ndarray:
        barrier_level = self.capital_barrier * self.S0
        return np.where(S >= barrier_level, self.notional, self.notional * (S / self.S0))
