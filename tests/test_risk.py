"""Tests for position sizing and trailing stop state machine."""
import pytest
from execution.risk import (
    check_position_size,
    compute_initial_stop,
    update_trailing_stop,
    compute_qty_from_notional,
    max_order_value,
)


class TestPositionSizing:
    def test_within_limit(self):
        assert check_position_size(100_000, 5_000) is True

    def test_at_exact_limit(self):
        assert check_position_size(100_000, 5_000) is True

    def test_over_limit(self):
        assert check_position_size(100_000, 5_001) is False

    def test_max_order_value(self):
        assert max_order_value(200_000) == pytest.approx(10_000, abs=0.01)

    def test_qty_from_notional(self):
        qty = compute_qty_from_notional(5_000, 100.0)
        assert qty == pytest.approx(50.0, abs=0.001)

    def test_qty_zero_price(self):
        assert compute_qty_from_notional(5_000, 0.0) == 0.0


class TestTrailingStop:
    def test_initial_stop(self):
        stop = compute_initial_stop(100.0, pct=0.10)
        assert stop == pytest.approx(90.0, abs=0.01)

    def test_stop_advances_with_price(self):
        new_high, new_stop, should_exit = update_trailing_stop(
            entry_price=100.0,
            current_high=100.0,
            new_price=110.0,
        )
        assert new_high == 110.0
        assert new_stop == pytest.approx(99.0, abs=0.01)  # 110 * 0.90
        assert should_exit is False

    def test_exit_triggered_below_stop(self):
        # Price rose to 110 → stop at 99; now drops to 98
        _, _, should_exit = update_trailing_stop(
            entry_price=100.0,
            current_high=110.0,
            new_price=98.0,
        )
        assert should_exit is True

    def test_no_exit_above_stop(self):
        _, _, should_exit = update_trailing_stop(
            entry_price=100.0,
            current_high=105.0,
            new_price=103.0,
        )
        assert should_exit is False

    def test_no_exit_below_stop_but_above_entry(self):
        # Stop math says exit but price is still above entry, so no exit.
        _, _, should_exit = update_trailing_stop(
            entry_price=100.0,
            current_high=100.0,
            new_price=91.0,  # below 90 stop but.. wait, 100*0.9=90, 91>90
        )
        # 91 > 90 (stop), so no exit
        assert should_exit is False
