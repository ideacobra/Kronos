"""Tests for trading/portfolio.py: open/close accounting, realized vs.
unrealized P&L, equity curve, and reconstruction from storage.
"""
from __future__ import annotations

import time

import pytest

from memecoin_bot.storage import Storage
from memecoin_bot.trading.portfolio import Portfolio, Position


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


class TestOpenPosition:
    def test_open_deducts_cash(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(size_usd=100.0))
        assert portfolio.cash_usd == pytest.approx(900.0)
        assert portfolio.open_position_count == 1
        assert portfolio.has_position("tok1")

    def test_open_duplicate_token_raises(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1"))
        with pytest.raises(ValueError):
            portfolio.open_position(make_position(token_address="tok1"))

    def test_open_insufficient_cash_raises(self):
        portfolio = Portfolio(50.0)
        with pytest.raises(ValueError):
            portfolio.open_position(make_position(size_usd=100.0))


class TestClosePosition:
    def test_close_profitable_trade(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", entry_price=1.0, quantity=100.0, size_usd=100.0))
        trade = portfolio.close_position("tok1", exit_price=1.5, exit_fee_usd=1.0, exit_reason="take_profit")

        # gross proceeds = 100 * 1.5 = 150; net = 149; pnl = 149 - 100 = 49
        assert trade.realized_pnl_usd == pytest.approx(49.0)
        assert trade.realized_pnl_pct == pytest.approx(49.0)
        assert portfolio.cash_usd == pytest.approx(900.0 + 149.0)
        assert portfolio.realized_pnl_usd == pytest.approx(49.0)
        assert not portfolio.has_position("tok1")
        assert portfolio.closed_trades[-1] is trade

    def test_close_losing_trade(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", entry_price=1.0, quantity=100.0, size_usd=100.0))
        trade = portfolio.close_position("tok1", exit_price=0.7, exit_fee_usd=0.5, exit_reason="stop_loss")

        # gross = 70; net = 69.5; pnl = 69.5 - 100 = -30.5
        assert trade.realized_pnl_usd == pytest.approx(-30.5)
        assert portfolio.cash_usd == pytest.approx(900.0 + 69.5)

    def test_close_missing_position_raises(self):
        portfolio = Portfolio(1000.0)
        with pytest.raises(KeyError):
            portfolio.close_position("missing", exit_price=1.0, exit_fee_usd=0.0, exit_reason="manual")

    def test_hold_minutes_recorded(self):
        portfolio = Portfolio(1000.0)
        opened_at = time.time() - 3600  # 1 hour ago
        portfolio.open_position(make_position(token_address="tok1", opened_at=opened_at))
        trade = portfolio.close_position("tok1", exit_price=1.0, exit_fee_usd=0.0, exit_reason="time_exit", closed_at=time.time())
        assert trade.hold_minutes == pytest.approx(60.0, abs=0.1)


class TestUnrealizedAndEquity:
    def test_unrealized_pnl_reflects_current_price(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", entry_price=1.0, quantity=100.0, size_usd=100.0))
        unrealized = portfolio.unrealized_pnl_usd({"tok1": 1.2})
        assert unrealized == pytest.approx(20.0)

    def test_equity_combines_cash_and_positions(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", entry_price=1.0, quantity=100.0, size_usd=100.0))
        equity = portfolio.equity_usd({"tok1": 1.2})
        assert equity == pytest.approx(1020.0)  # 900 cash + 120 market value

    def test_equity_falls_back_to_entry_price_if_no_current_price(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", entry_price=1.0, quantity=100.0, size_usd=100.0))
        equity = portfolio.equity_usd({})
        assert equity == pytest.approx(1000.0)

    def test_equity_with_no_positions_equals_cash(self):
        portfolio = Portfolio(1000.0)
        assert portfolio.equity_usd({}) == pytest.approx(1000.0)


class TestRealizedPnlToday:
    def test_only_counts_trades_closed_today(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", size_usd=100.0))
        portfolio.close_position("tok1", exit_price=1.5, exit_fee_usd=0.0, exit_reason="take_profit", closed_at=time.time())
        assert portfolio.realized_pnl_today_usd() == pytest.approx(50.0)

    def test_excludes_trades_closed_before_today(self):
        portfolio = Portfolio(1000.0)
        portfolio.open_position(make_position(token_address="tok1", size_usd=100.0))
        two_days_ago = time.time() - 2 * 86400
        portfolio.close_position(
            "tok1", exit_price=1.5, exit_fee_usd=0.0, exit_reason="take_profit", closed_at=two_days_ago
        )
        assert portfolio.realized_pnl_today_usd() == pytest.approx(0.0)


class TestFromStorage:
    def test_reconstructs_open_positions_and_cash(self, tmp_path):
        db_path = tmp_path / "test.db"
        storage = Storage(str(db_path))
        try:
            portfolio = Portfolio(1000.0, storage=storage)
            portfolio.open_position(make_position(token_address="tok1", size_usd=100.0))
            portfolio.close_position("tok1", exit_price=1.5, exit_fee_usd=0.0, exit_reason="take_profit")
            portfolio.open_position(make_position(token_address="tok2", size_usd=50.0))

            reloaded = Portfolio.from_storage(1000.0, storage)

            assert reloaded.has_position("tok2")
            assert not reloaded.has_position("tok1")
            # cash = 1000 (start) + 50 (realized pnl from tok1) - 50 (tok2 cost basis, still open)
            assert reloaded.cash_usd == pytest.approx(1000.0)
            assert reloaded.realized_pnl_usd == pytest.approx(50.0)

            restored_position = reloaded.get_position("tok2")
            assert restored_position.stop_loss_price == pytest.approx(0.85)
            assert restored_position.take_profit_price == pytest.approx(1.4)
        finally:
            storage.close()

    def test_fresh_db_reconstructs_to_starting_balance(self, tmp_path):
        db_path = tmp_path / "empty.db"
        storage = Storage(str(db_path))
        try:
            reloaded = Portfolio.from_storage(500.0, storage)
            assert reloaded.cash_usd == pytest.approx(500.0)
            assert reloaded.open_position_count == 0
        finally:
            storage.close()
