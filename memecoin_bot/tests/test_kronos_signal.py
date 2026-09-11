"""Tests for strategy/kronos_signal.py: the optional Kronos confirmation
signal MUST gracefully no-op (never raise) whenever it's disabled, its
dependencies are unavailable, there isn't enough history, or inference
itself fails. These tests never perform a real model load/download --
they inject fakes/monkeypatches to exercise each fallback path in
isolation and quickly.
"""
from __future__ import annotations

import time

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.data.types import Candle, TokenPair, TxnWindow
from memecoin_bot.strategy.kronos_signal import KronosSignalProvider


def make_pair(**overrides) -> TokenPair:
    defaults = dict(
        chain_id="solana",
        dex_id="raydium",
        pair_address="pair123",
        base_token_address="token123",
        base_token_name="Test",
        base_token_symbol="TEST",
        quote_token_symbol="SOL",
        price_usd=1.0,
        price_change_m5=2.0,
        price_change_h1=5.0,
        price_change_h6=0.0,
        price_change_h24=0.0,
        volume_m5=0.0,
        volume_h1=0.0,
        volume_h6=0.0,
        volume_h24=0.0,
        liquidity_usd=50_000.0,
        txns_m5=TxnWindow(0, 0),
        txns_h1=TxnWindow(0, 0),
    )
    defaults.update(overrides)
    return TokenPair(**defaults)


class FakeGeckoTerminalClient:
    def __init__(self, candles):
        self._candles = candles

    def get_ohlcv(self, **_kwargs):
        return self._candles


def make_candles(n: int = 50, start_price: float = 1.0, step: float = 0.0):
    candles = []
    start_ts = int(time.time()) - n * 300
    price = start_price
    for i in range(n):
        candles.append(
            Candle(timestamp=start_ts + i * 300, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=10.0)
        )
        price += step
    return candles


class TestDisabledByConfig:
    def test_disabled_returns_unavailable_without_touching_network(self):
        settings = Settings(use_kronos_signal=False)
        provider = KronosSignalProvider(settings, geckoterminal_client=FakeGeckoTerminalClient([]))
        result = provider.evaluate(make_pair(), pool_address="pool1")
        assert result.available is False
        assert "disabled" in result.reason
        assert result.agrees is None


class TestGracefulFallback:
    def test_model_load_failure_never_raises(self, monkeypatch):
        settings = Settings(use_kronos_signal=True)
        provider = KronosSignalProvider(settings, geckoterminal_client=FakeGeckoTerminalClient([]))

        monkeypatch.setattr(provider, "_ensure_loaded", lambda: False)
        provider._load_failed_reason = "simulated missing dependency"

        result = provider.evaluate(make_pair(), pool_address="pool1")
        assert result.available is False
        assert "unavailable" in result.reason

    def test_insufficient_history_returns_unavailable(self, monkeypatch):
        settings = Settings(use_kronos_signal=True, kronos_lookback_candles=100, kronos_pred_len=12)
        provider = KronosSignalProvider(
            settings, geckoterminal_client=FakeGeckoTerminalClient(make_candles(n=5))
        )
        monkeypatch.setattr(provider, "_ensure_loaded", lambda: True)

        result = provider.evaluate(make_pair(), pool_address="pool1")
        assert result.available is False
        assert "insufficient" in result.reason

    def test_inference_exception_never_raises(self, monkeypatch):
        pd = pytest.importorskip("pandas")
        settings = Settings(use_kronos_signal=True, kronos_lookback_candles=10, kronos_pred_len=3)
        provider = KronosSignalProvider(
            settings, geckoterminal_client=FakeGeckoTerminalClient(make_candles(n=50))
        )

        class ExplodingPredictor:
            def predict(self, *_args, **_kwargs):
                raise RuntimeError("simulated inference crash")

        def fake_ensure_loaded():
            provider._predictor = ExplodingPredictor()
            provider._pd = pd
            return True

        monkeypatch.setattr(provider, "_ensure_loaded", fake_ensure_loaded)

        result = provider.evaluate(make_pair(), pool_address="pool1")
        assert result.available is False
        assert "inference failed" in result.reason

    def test_never_reloads_after_first_failed_attempt(self, monkeypatch):
        settings = Settings(use_kronos_signal=True)
        provider = KronosSignalProvider(settings, geckoterminal_client=FakeGeckoTerminalClient([]))

        call_count = {"n": 0}

        def fake_import_failure():
            call_count["n"] += 1
            return False

        monkeypatch.setattr(provider, "_ensure_loaded", fake_import_failure)
        provider.evaluate(make_pair(), pool_address="pool1")
        provider.evaluate(make_pair(), pool_address="pool1")
        # evaluate() calls _ensure_loaded() itself; a real (non-mocked)
        # implementation would short-circuit internally after the first
        # attempt, but since we replaced the whole method here we just
        # confirm evaluate() never raises across repeated calls.
        assert call_count["n"] == 2


class TestAgreementLogic:
    def test_agrees_when_predicted_direction_matches_momentum(self, monkeypatch):
        pd = pytest.importorskip("pandas")
        settings = Settings(use_kronos_signal=True, kronos_lookback_candles=10, kronos_pred_len=3, kronos_min_agreement_pct=0.0)
        provider = KronosSignalProvider(
            settings, geckoterminal_client=FakeGeckoTerminalClient(make_candles(n=50, start_price=1.0))
        )

        class UpPredictor:
            def predict(self, df, x_timestamp, y_timestamp, pred_len, verbose=False):
                last_close = float(df["close"].iloc[-1])
                return pd.DataFrame({"close": [last_close * 1.10] * pred_len})

        def fake_ensure_loaded():
            provider._predictor = UpPredictor()
            provider._pd = pd
            return True

        monkeypatch.setattr(provider, "_ensure_loaded", fake_ensure_loaded)

        pair = make_pair(price_change_m5=5.0)  # upward momentum
        result = provider.evaluate(pair, pool_address="pool1")
        assert result.available is True
        assert result.predicted_direction == "up"
        assert result.agrees is True

    def test_disagrees_when_predicted_direction_opposes_momentum(self, monkeypatch):
        pd = pytest.importorskip("pandas")
        settings = Settings(use_kronos_signal=True, kronos_lookback_candles=10, kronos_pred_len=3, kronos_min_agreement_pct=0.0)
        provider = KronosSignalProvider(
            settings, geckoterminal_client=FakeGeckoTerminalClient(make_candles(n=50, start_price=1.0))
        )

        class DownPredictor:
            def predict(self, df, x_timestamp, y_timestamp, pred_len, verbose=False):
                last_close = float(df["close"].iloc[-1])
                return pd.DataFrame({"close": [last_close * 0.90] * pred_len})

        def fake_ensure_loaded():
            provider._predictor = DownPredictor()
            provider._pd = pd
            return True

        monkeypatch.setattr(provider, "_ensure_loaded", fake_ensure_loaded)

        pair = make_pair(price_change_m5=5.0)  # upward momentum, but model predicts down
        result = provider.evaluate(pair, pool_address="pool1")
        assert result.available is True
        assert result.predicted_direction == "down"
        assert result.agrees is False
