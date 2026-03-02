"""Phoenix autocallable: memory coupon accumulates across missed observation dates."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .base import AutocallProduct


@dataclass
class PhoenixAutocall(AutocallProduct):
    """Phoenix autocallable note with memory coupon feature.

    When the product autocalls at observation date i, the investor receives
    ``notional + coupon_amounts[i] × (missed_coupons + 1)`` — i.e. all
    previously unpaid coupons are recovered.

    The solver tracks ``coupons_paid`` (number of observation dates where the
    autocall barrier was *not* triggered) and passes it via ``autocall_value``.

    Terminal payoff is identical to vanilla.
    """

    def terminal_payoff(self, S: np.ndarray) -> np.ndarray:
        barrier_level = self.capital_barrier * self.S0
        return np.where(S >= barrier_level, self.notional, self.notional * (S / self.S0))

    def autocall_value(self, date_idx: int, coupons_paid: int = 0) -> float:
        """Return notional + coupon × (missed + 1)."""
        missed = date_idx - coupons_paid  # observation dates that did not trigger
        return self.notional + self.coupon_amounts[date_idx] * (missed + 1)
