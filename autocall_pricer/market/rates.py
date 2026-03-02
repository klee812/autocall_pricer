from dataclasses import dataclass
import numpy as np


@dataclass
class FlatRateCurve:
    """Flat continuously-compounded interest rate and dividend yield curve."""

    rate: float       # risk-free rate r
    dividend: float = 0.0  # continuous dividend yield q

    def discount_factor(self, T: float) -> float:
        return np.exp(-self.rate * T)

    def forward(self, S0: float, T: float) -> float:
        return S0 * np.exp((self.rate - self.dividend) * T)
