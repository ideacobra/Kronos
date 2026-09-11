"""Tests for trading/broker.py: PaperBroker fill math and the inert
LiveJupiterBroker stub.
"""
from __future__ import annotations

import pytest

from memecoin_bot.trading.broker import Broker, Fill, LiveJupiterBroker, PaperBroker


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


class TestLiveJupiterBrokerIsInert:
    def test_cannot_be_instantiated(self):
        with pytest.raises(NotImplementedError):
            LiveJupiterBroker()

    def test_is_a_broker_subclass(self):
        assert issubclass(LiveJupiterBroker, Broker)

    def test_methods_raise_if_somehow_called_on_the_class(self):
        # Even bypassing __init__, the methods themselves must refuse to run.
        instance = LiveJupiterBroker.__new__(LiveJupiterBroker)
        with pytest.raises(NotImplementedError):
            instance.open_position("addr", "SYM", 1.0, 100.0)
        with pytest.raises(NotImplementedError):
            instance.close_position("addr", "SYM", 100.0, 1.0)
        with pytest.raises(NotImplementedError):
            instance.mark_to_market("addr", 1.0)
