"""Tests for product classes: vanilla, phoenix, step-down."""

import numpy as np
import pytest

from autocall_pricer.products.vanilla_autocall import VanillaAutocall
from autocall_pricer.products.phoenix_autocall import PhoenixAutocall
from autocall_pricer.products.stepdown_autocall import StepDownAutocall


S0 = 100.0
NOTIONAL = 1000.0
COUPON = 50.0
OBS = [0.25, 0.5, 0.75, 1.0]
BARRIERS = [1.0, 1.0, 1.0, 1.0]  # 100% of S0
COUPONS = [COUPON] * 4
T = 1.0


class TestVanillaAutocall:
    def _make(self, capital_barrier=1.0):
        return VanillaAutocall(
            S0=S0, notional=NOTIONAL, observation_dates=OBS,
            autocall_barriers=BARRIERS, coupon_amounts=COUPONS,
            maturity=T, capital_barrier=capital_barrier,
        )

    def test_terminal_payoff_above_barrier(self):
        p = self._make(capital_barrier=0.8)
        S = np.array([80.0, 100.0, 120.0])
        result = p.terminal_payoff(S)
        # All above 0.8*S0=80 → full notional (80 is exactly on barrier → notional)
        np.testing.assert_array_equal(result, np.array([NOTIONAL, NOTIONAL, NOTIONAL]))

    def test_terminal_payoff_below_barrier(self):
        p = self._make(capital_barrier=0.8)
        S = np.array([60.0, 70.0])
        result = p.terminal_payoff(S)
        expected = NOTIONAL * S / S0
        np.testing.assert_allclose(result, expected)

    def test_autocall_value(self):
        p = self._make()
        assert p.autocall_value(0) == NOTIONAL + COUPON
        assert p.autocall_value(3) == NOTIONAL + COUPON

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            VanillaAutocall(
                S0=S0, notional=NOTIONAL, observation_dates=[0.5],
                autocall_barriers=[1.0, 1.0], coupon_amounts=[50.0],
                maturity=T,
            )


class TestPhoenixAutocall:
    def _make(self):
        return PhoenixAutocall(
            S0=S0, notional=NOTIONAL, observation_dates=OBS,
            autocall_barriers=BARRIERS, coupon_amounts=COUPONS,
            maturity=T,
        )

    def test_autocall_value_no_missed(self):
        p = self._make()
        # date_idx=0, coupons_paid=0 → missed=0 → notional + coupon*(0+1)
        assert p.autocall_value(0, coupons_paid=0) == NOTIONAL + COUPON * 1

    def test_autocall_value_with_missed(self):
        p = self._make()
        # date_idx=2, coupons_paid=1 → missed=2-1=1 → notional + coupon*2
        assert p.autocall_value(2, coupons_paid=1) == NOTIONAL + COUPON * 2

    def test_terminal_payoff_same_as_vanilla(self):
        p = self._make()
        v = VanillaAutocall(
            S0=S0, notional=NOTIONAL, observation_dates=OBS,
            autocall_barriers=BARRIERS, coupon_amounts=COUPONS, maturity=T,
        )
        S = np.linspace(50, 150, 20)
        np.testing.assert_array_equal(p.terminal_payoff(S), v.terminal_payoff(S))


class TestStepDownAutocall:
    def test_valid_step_down(self):
        p = StepDownAutocall(
            S0=S0, notional=NOTIONAL, observation_dates=OBS,
            autocall_barriers=[1.0, 0.95, 0.90, 0.85],
            coupon_amounts=COUPONS, maturity=T,
        )
        assert p.autocall_value(0) == NOTIONAL + COUPON

    def test_non_decreasing_raises(self):
        with pytest.raises(ValueError, match="non-increasing"):
            StepDownAutocall(
                S0=S0, notional=NOTIONAL, observation_dates=OBS,
                autocall_barriers=[0.85, 0.90, 0.95, 1.0],  # increasing — wrong
                coupon_amounts=COUPONS, maturity=T,
            )

    def test_terminal_payoff(self):
        p = StepDownAutocall(
            S0=S0, notional=NOTIONAL, observation_dates=OBS,
            autocall_barriers=[1.0, 0.95, 0.90, 0.85],
            coupon_amounts=COUPONS, maturity=T, capital_barrier=0.8,
        )
        S = np.array([75.0, 90.0, 100.0])
        result = p.terminal_payoff(S)
        expected = np.array([NOTIONAL * 75 / S0, NOTIONAL, NOTIONAL])
        np.testing.assert_allclose(result, expected)
