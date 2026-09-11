"""Tests for trading/broker.py: PaperBroker fill math.

See test_live_broker.py for LiveJupiterBroker's tests (mocked Jupiter/RPC).
"""
from __future__ import annotations

import pytest

from memecoin_bot.trading.broker import Broker, Fill, PaperBroker


class TestPaperBrokerIsABroker:
    def test_is_a_broker_subclass(self):
        assert issubclass(PaperBroker, Broker)


class TestPaperBrokerOpenPosition:
    def test_applies_slippage_against_buyer(self):
        broker = PaperBroker(slippage_pct=0.02, fee_pct=0.0)
        fill = broker.open_position("addr", "SYM", reference_price=1.0, size_usd=100.0)
        assert fill.price == pytest.approx(1.02)

    def test_applies_fee_reducing_quantity(self):
        broker = PaperBroker(slippage_pct=0.0, fee_pct=0.01)
        fill = broker.open_position("addr", "SYM", reference_price=1.0, size_usd=100.0)
        assert fill.fee_usd == pytest.approx(1.0)
        assert fill.quantity == pytest.approx(99.0)  # (100 - 1 fee) / price 1.0

    def test_rejects_non_positive_size(self):
        broker = PaperBroker()
        with pytest.raises(ValueError):
            broker.open_position("addr", "SYM", reference_price=1.0, size_usd=0.0)

    def test_rejects_non_positive_reference_price(self):
        broker = PaperBroker()
        with pytest.raises(ValueError):
            broker.open_position("addr", "SYM", reference_price=0.0, size_usd=100.0)


class TestPaperBrokerClosePosition:
    def test_applies_slippage_against_seller(self):
        broker = PaperBroker(slippage_pct=0.02, fee_pct=0.0)
        fill = broker.close_position("addr", "SYM", quantity=100.0, reference_price=1.0)
        assert fill.price == pytest.approx(0.98)
        assert fill.gross_usd == pytest.approx(98.0)

    def test_fee_reduces_net_proceeds(self):
        broker = PaperBroker(slippage_pct=0.0, fee_pct=0.01)
        fill = broker.close_position("addr", "SYM", quantity=100.0, reference_price=1.0)
        assert fill.fee_usd == pytest.approx(1.0)
        assert fill.net_usd == pytest.approx(99.0)

    def test_rejects_non_positive_quantity(self):
        broker = PaperBroker()
        with pytest.raises(ValueError):
            broker.close_position("addr", "SYM", quantity=0.0, reference_price=1.0)


class TestPaperBrokerMarkToMarket:
    def test_returns_reference_price_unchanged(self):
        broker = PaperBroker()
        assert broker.mark_to_market("addr", 1.2345) == pytest.approx(1.2345)


class TestFill:
    def test_net_usd_property(self):
        fill = Fill(price=1.0, quantity=10.0, fee_usd=2.0, gross_usd=10.0)
        assert fill.net_usd == pytest.approx(8.0)
