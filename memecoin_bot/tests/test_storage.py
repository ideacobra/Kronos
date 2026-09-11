"""Tests for storage.py: scans, trade lifecycle, equity curve, and the
summary stats used by `cli.py stats`.
"""
from __future__ import annotations

import time

import pytest

from memecoin_bot.data.types import ScoredCandidate, TokenPair, TxnWindow
from memecoin_bot.storage import Storage
from memecoin_bot.trading.portfolio import Position


def make_pair(**overrides) -> TokenPair:
    defaults = dict(
        chain_id="solana",
        dex_id="raydium",
        pair_address="pair123",
        base_token_address="token123",
        base_token_name="Test",
        base_token_symbol="TEST",
        quote_token_symbol="SOL",
        price_usd=0.01,
        price_change_m5=0.0,
        price_change_h1=0.0,
        price_change_h6=0.0,
        price_change_h24=0.0,
        volume_m5=0.0,
        volume_h1=100.0,
        volume_h6=0.0,
        volume_h24=0.0,
        liquidity_usd=20_000.0,
        txns_m5=TxnWindow(0, 0),
        txns_h1=TxnWindow(0, 0),
    )
    defaults.update(overrides)
    return TokenPair(**defaults)


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "test.db"))
    yield s
    s.close()


class TestScans:
    def test_record_and_read_back_scan(self, storage):
        candidate = ScoredCandidate(
            pair=make_pair(),
            score=75.5,
            volume_surge_score=80.0,
            momentum_score=70.0,
            buy_pressure_score=75.0,
            boost_bonus_score=0.0,
        )
        storage.record_scan(candidate, decision="buy", reason="opened paper position")

        rows = storage.get_recent_scans(limit=10)
        assert len(rows) == 1
        assert rows[0]["token_address"] == "token123"
        assert rows[0]["decision"] == "buy"
        assert rows[0]["score"] == pytest.approx(75.5)


class TestTradeLifecycle:
    def test_open_then_close_updates_same_row(self, storage):
        position = Position(
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
        trade_id = storage.record_trade_open(position)
        position.trade_id = trade_id

        assert len(storage.get_open_trades()) == 1

        from memecoin_bot.trading.portfolio import ClosedTrade

        trade = ClosedTrade(
            token_address="tok1",
            symbol="TEST",
            entry_price=1.0,
            exit_price=1.2,
            quantity=100.0,
            size_usd=100.0,
            entry_fee_usd=0.3,
            exit_fee_usd=0.4,
            opened_at=position.opened_at,
            closed_at=time.time(),
            exit_reason="take_profit",
            realized_pnl_usd=19.6,
            realized_pnl_pct=19.6,
            trade_id=trade_id,
        )
        storage.record_trade_close(trade)

        assert storage.get_open_trades() == []
        closed = storage.get_recent_trades(status="closed")
        assert len(closed) == 1
        assert closed[0]["id"] == trade_id
        assert closed[0]["exit_reason"] == "take_profit"
        assert closed[0]["realized_pnl_usd"] == pytest.approx(19.6)


class TestEquityCurve:
    def test_record_and_read_equity_points(self, storage):
        storage.record_equity_point(
            timestamp=time.time(), equity_usd=1000.0, cash_usd=1000.0, unrealized_pnl_usd=0.0,
            realized_pnl_usd=0.0, open_positions=0,
        )
        storage.record_equity_point(
            timestamp=time.time() + 1, equity_usd=1010.0, cash_usd=900.0, unrealized_pnl_usd=10.0,
            realized_pnl_usd=0.0, open_positions=1,
        )
        curve = storage.get_equity_curve()
        assert len(curve) == 2
        assert curve[-1]["equity_usd"] == pytest.approx(1010.0)

        latest = storage.get_latest_equity_point()
        assert latest["equity_usd"] == pytest.approx(1010.0)


class TestSummaryStats:
    def test_empty_db_reports_zeros(self, storage):
        stats = storage.get_summary_stats()
        assert stats["total_trades"] == 0
        assert stats["win_rate_pct"] == 0.0
        assert stats["open_positions"] == 0
        assert stats["latest_equity"] is None

    def test_win_rate_and_totals(self, storage):
        from memecoin_bot.trading.portfolio import ClosedTrade

        def make_trade(pnl_usd, trade_id):
            return ClosedTrade(
                token_address=f"tok{trade_id}",
                symbol="TEST",
                entry_price=1.0,
                exit_price=1.0 + pnl_usd / 100.0,
                quantity=100.0,
                size_usd=100.0,
                entry_fee_usd=0.0,
                exit_fee_usd=0.0,
                opened_at=time.time(),
                closed_at=time.time(),
                exit_reason="take_profit" if pnl_usd > 0 else "stop_loss",
                realized_pnl_usd=pnl_usd,
                realized_pnl_pct=pnl_usd,
                trade_id=None,
            )

        storage.record_trade_close(make_trade(20.0, 1))
        storage.record_trade_close(make_trade(-10.0, 2))
        storage.record_trade_close(make_trade(30.0, 3))

        stats = storage.get_summary_stats()
        assert stats["total_trades"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate_pct"] == pytest.approx(2 / 3 * 100.0)
        assert stats["total_realized_pnl_usd"] == pytest.approx(40.0)
