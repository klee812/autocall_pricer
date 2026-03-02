"""Data model for an option chain record and basic filtering utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class OptionRecord:
    """A single option contract with market quote and reference data.

    Attributes
    ----------
    option_id   : exchange/broker identifier (e.g. 'AAPL 250117C00150000')
    underlying  : underlying ticker or asset identifier
    option_type : 'call' or 'put'
    strike      : absolute strike price (same currency as S0)
    expiry      : time to expiry in years from pricing date
    bid         : best bid price
    ask         : best ask price
    S0          : current spot price of the underlying
    r           : risk-free rate (continuously compounded)
    q           : dividend yield (continuously compounded)
    implied_vol : filled in by the surface builder; None until solved
    """

    option_id: str
    underlying: str
    option_type: Literal["call", "put"]
    strike: float
    expiry: float           # years
    bid: float
    ask: float
    S0: float
    r: float
    q: float = 0.0
    implied_vol: float | None = field(default=None, repr=False)

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def moneyness(self) -> float:
        """ln(K/F) where F is the forward."""
        import math
        F = self.S0 * math.exp((self.r - self.q) * self.expiry)
        return math.log(self.strike / F)


def filter_chain(
    records: list[OptionRecord],
    max_spread_pct: float = 0.20,
    min_bid: float = 0.01,
    max_moneyness: float = 1.5,
    min_expiry: float = 7 / 365,
) -> list[OptionRecord]:
    """Remove stale, illiquid, or extreme-moneyness quotes.

    Parameters
    ----------
    records         : raw option chain
    max_spread_pct  : drop if (ask-bid)/mid > this fraction (default 20%)
    min_bid         : drop if bid < this value (effectively zero-bid)
    max_moneyness   : drop if |ln(K/F)| > this value (default 1.5 ≈ very deep)
    min_expiry      : drop if expiry < this value in years (default 1 week)
    """
    out = []
    for r in records:
        if r.expiry < min_expiry:
            continue
        if r.bid < min_bid:
            continue
        if r.mid > 0 and r.spread / r.mid > max_spread_pct:
            continue
        if abs(r.moneyness) > max_moneyness:
            continue
        out.append(r)
    return out
