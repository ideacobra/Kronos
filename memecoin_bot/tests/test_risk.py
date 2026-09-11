"""Tests for strategy/risk.py: position sizing, entry gating (circuit
breaker, max positions, cash checks), and stop-loss/take-profit/time exits.
"""
from __future__ import annotations

import time

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.strategy.risk import EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, EXIT_TIME, RiskManager
from memecoin_bot.trading.portfolio import Portfolio, Position


def make_settings(**overrides) -> Settings:
    defaults = dict(
        position_size_pct=0.05,
        max_position_usd=100.0,
        stop_loss_pct=0.15,
        take_profit_pct=0.40,
        max_hold_minutes=180.0,
        max_open_positions=5,
        daily_realized_loss_limit_usd=150.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_position(**overrides) -> Position:
    defaults = dict(
        token_address="tok1",
        symbol="TEST",
        entry_price=1.0,
        quantity=100.0,
        size_usd=100.0,
        entry_fee_usd=0.3,
        opened_at=time.time(),
        stop_loss_price=0.85,
        take_profit_price=1.4,
        max_hold_minutes=180.0,
    )
    defaults.update(overrides)
    return Position(**defaults)


class TestPositionSizing:
    def test_size_is_pct_of_equity(self):
        rm = RiskManager(make_settings(position_size_pct=0.05, max_position_usd=1000.0))
        assert rm.position_size_usd(1000.0) == pytest.approx(50.0)

    def test_size_capped_at_max_position_usd(self):
        rm = RiskManager(make_settings(position_size_pct=0.5, max_position_usd=100.0))
        assert rm.position_size_usd(10_000.0) == pytest.approx(100.0)

    def test_size_is_never_negative(self):
        rm = RiskManager(make_settings())
        assert rm.position_size_usd(-500.0) == 0.0

    def test_stop_and_take_prices(self):
        rm = RiskManager(make_settings(stop_loss_pct=0.1, take_profit_pct=0.5))
        stop, take = rm.compute_stop_and_take_prices(2.0)
        assert stop == pytest.approx(1.8)
        assert take == pytest.approx(3.0)


class TestEntryGating:
    def test_approves_when_room_available(self):
        rm = RiskManager(make_settings(position_size_pct=0.05))
        portfolio = Portfolio(1000.0)
        decision = rm.evaluate_entry(portfolio, "tokA", equity_usd=1000.0)
        assert decision.approved is True
        assert decision.size_usd == pytest.approx(50.0)

    def test_rejects_when_already_holding(self):
        rm = RiskManager(make_settings())
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tokA", size_usd=50.0))
        decision = rm.evaluate_entry(portfolio, "tokA", equity_usd=1000.0)
        assert decision.approved is False
        assert "already holding" in decision.reason

    def test_rejects_when_max_positions_reached(self):
        rm = RiskManager(make_settings(max_open_positions=1))
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tokA", size_usd=50.0))
        decision = rm.evaluate_entry(portfolio, "tokB", equity_usd=1000.0)
        assert decision.approved is False
        assert "max open positions" in decision.reason

    def test_rejects_when_circuit_breaker_tripped(self):
        rm = RiskManager(make_settings(daily_realized_loss_limit_usd=50.0))
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tokA", size_usd=100.0))
        portfolio.close_position("tokA", exit_price=0.0, exit_fee_usd=0.0, exit_reason="stop_loss")
        assert portfolio.realized_pnl_usd == pytest.approx(-100.0)

        decision = rm.evaluate_entry(portfolio, "tokB", equity_usd=900.0)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason

    def test_circuit_breaker_disabled_when_limit_is_zero(self):
        rm = RiskManager(make_settings(daily_realized_loss_limit_usd=0.0))
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tokA", size_usd=100.0))
        portfolio.close_position("tokA", exit_price=0.0, exit_fee_usd=0.0, exit_reason="stop_loss")
        decision = rm.evaluate_entry(portfolio, "tokB", equity_usd=900.0)
        assert decision.approved is True

    def test_rejects_when_insufficient_cash(self):
        rm = RiskManager(make_settings(position_size_pct=1.0, max_position_usd=10_000.0))
        portfolio = Portfolio(100.0)
        portfolio.cash_usd = 0.0
        decision = rm.evaluate_entry(portfolio, "tokA", equity_usd=100.0)
        assert decision.approved is False
        assert "insufficient" in decision.reason

    def test_size_capped_at_available_cash(self):
        rm = RiskManager(make_settings(position_size_pct=0.5, max_position_usd=10_000.0))
        portfolio = Portfolio(100.0)
        portfolio.cash_usd = 30.0
        decision = rm.evaluate_entry(portfolio, "tokA", equity_usd=100.0)
        assert decision.approved is True
        assert decision.size_usd == pytest.approx(30.0)


class TestExitChecks:
    def test_stop_loss_triggers(self):
        rm = RiskManager(make_settings())
        position = make_position(stop_loss_price=0.85, take_profit_price=1.4)
        assert rm.check_exit(position, current_price=0.8) == EXIT_STOP_LOSS

    def test_stop_loss_triggers_exactly_at_boundary(self):
        rm = RiskManager(make_settings())
        position = make_position(stop_loss_price=0.85, take_profit_price=1.4)
        assert rm.check_exit(position, current_price=0.85) == EXIT_STOP_LOSS

    def test_take_profit_triggers(self):
        rm = RiskManager(make_settings())
        position = make_position(stop_loss_price=0.85, take_profit_price=1.4)
        assert rm.check_exit(position, current_price=1.5) == EXIT_TAKE_PROFIT

    def test_time_exit_triggers(self):
        rm = RiskManager(make_settings())
        position = make_position(opened_at=time.time() - 200 * 60, max_hold_minutes=180.0)
        assert rm.check_exit(position, current_price=1.0) == EXIT_TIME

    def test_no_exit_when_in_range(self):
        rm = RiskManager(make_settings())
        position = make_position(
            opened_at=time.time(), stop_loss_price=0.85, take_profit_price=1.4, max_hold_minutes=180.0
        )
        assert rm.check_exit(position, current_price=1.05) is None

    def test_stop_loss_takes_priority_over_time_exit(self):
        rm = RiskManager(make_settings())
        position = make_position(
            opened_at=time.time() - 200 * 60,
            stop_loss_price=0.85,
            take_profit_price=1.4,
            max_hold_minutes=180.0,
        )
        assert rm.check_exit(position, current_price=0.5) == EXIT_STOP_LOSS
