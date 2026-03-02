"""Abstract base class for autocallable structured products."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


@dataclass
class AutocallProduct(ABC):
    """Base class for single-underlying autocallable products.

    Parameters
    ----------
    S0 : float
        Initial spot price.
    notional : float
        Notional amount (face value of the note).
    observation_dates : list[float]
        Autocall observation dates as years from today, sorted ascending.
    autocall_barriers : list[float]
        Autocall trigger levels as a fraction of S0 per observation date.
        Must have the same length as ``observation_dates``.
    coupon_amounts : list[float]
        Absolute dollar coupon paid if autocalled at each observation date.
        Must have the same length as ``observation_dates``.
    maturity : float
        Final maturity in years.
    capital_barrier : float
        Downside capital protection barrier as fraction of S0.  If spot at
        maturity is below this level the investor receives ``notional · S/S0``
        rather than full principal (European observation only).
    """

    S0: float
    notional: float
    observation_dates: list[float]
    autocall_barriers: list[float]
    coupon_amounts: list[float]
    maturity: float
    capital_barrier: float = 1.0  # default: no capital protection

    def __post_init__(self) -> None:
        n = len(self.observation_dates)
        if len(self.autocall_barriers) != n:
            raise ValueError("autocall_barriers must have the same length as observation_dates")
        if len(self.coupon_amounts) != n:
            raise ValueError("coupon_amounts must have the same length as observation_dates")

    @abstractmethod
    def terminal_payoff(self, S: np.ndarray) -> np.ndarray:
        """Payoff at maturity for each spot value in the PDE grid."""

    def autocall_value(self, date_idx: int, coupons_paid: int = 0) -> float:
        """Value received by the investor when the product autocalls.

        Parameters
        ----------
        date_idx : int
            Index into ``observation_dates``.
        coupons_paid : int
            Number of previously missed coupons (used by PhoenixAutocall).
        """
        return self.notional + self.coupon_amounts[date_idx]
