"""Tests for strategy/scoring.py: composite FOMO score components and
gating filters.
"""
from __future__ import annotations

import time

import pytest

from memecoin_bot.config import Settings
from memecoin_bot.data.types import TokenPair, TxnWindow
from memecoin_bot.strategy.scoring import (
    boost_bonus_score,
    buy_pressure_score,
    compute_score,
    momentum_score,
    passes_gating_filters,
    score_candidates,
    volume_surge_score,
)


def make_pair(**overrides) -> TokenPair:
    defaults = dict(
        chain_id="solana",
        dex_id="raydium",
        pair_address="pair123",
        base_token_address="token123",
        base_token_name="Test Token",
        base_token_symbol="TEST",
        quote_token_symbol="SOL",
        price_usd=0.001,
        price_change_m5=0.0,
        price_change_h1=0.0,
        price_change_h6=0.0,
        price_change_h24=0.0,
        volume_m5=0.0,
        volume_h1=0.0,
        volume_h6=0.0,
        volume_h24=0.0,
        liquidity_usd=50_000.0,
        txns_m5=TxnWindow(0, 0),
        txns_h1=TxnWindow(0, 0),
        pair_created_at_ms=int((time.time() - 3600) * 1000),  # 1 hour old
    )
    defaults.update(overrides)
    return TokenPair(**defaults)


class TestVolumeSurgeScore:
    def test_no_baseline_no_volume_scores_zero(self):
        pair = make_pair(volume_h1=0.0, volume_m5=0.0)
        assert volume_surge_score(pair) == 0.0

    def test_no_baseline_with_volume_scores_max(self):
        pair = make_pair(volume_h1=0.0, volume_m5=500.0)
        assert volume_surge_score(pair) == 100.0

    def test_in_line_with_baseline_scores_fifty(self):
        pair = make_pair(volume_h1=1200.0, volume_m5=100.0)  # ratio = 1.0
        assert volume_surge_score(pair) == pytest.approx(50.0)

    def test_double_baseline_scores_max(self):
        pair = make_pair(volume_h1=1200.0, volume_m5=200.0)  # ratio = 2.0
        assert volume_surge_score(pair) == pytest.approx(100.0)

    def test_below_baseline_scores_less_than_fifty(self):
        pair = make_pair(volume_h1=1200.0, volume_m5=50.0)  # ratio = 0.5
        assert volume_surge_score(pair) == pytest.approx(25.0)


class TestMomentumScore:
    def test_flat_scores_fifty(self):
        pair = make_pair(price_change_m5=0.0, price_change_h1=0.0)
        assert momentum_score(pair) == pytest.approx(50.0)

    def test_positive_change_scores_above_fifty(self):
        pair = make_pair(price_change_m5=5.0, price_change_h1=10.0)
        assert momentum_score(pair) > 50.0

    def test_negative_change_scores_below_fifty(self):
        pair = make_pair(price_change_m5=-5.0, price_change_h1=-10.0)
        assert momentum_score(pair) < 50.0

    def test_extreme_positive_change_caps_at_100(self):
        pair = make_pair(price_change_m5=1000.0, price_change_h1=1000.0)
        assert momentum_score(pair) == pytest.approx(100.0)

    def test_extreme_negative_change_floors_at_zero(self):
        pair = make_pair(price_change_m5=-1000.0, price_change_h1=-1000.0)
        assert momentum_score(pair) == pytest.approx(0.0)


class TestBuyPressureScore:
    def test_all_buys_scores_100(self):
        pair = make_pair(txns_m5=TxnWindow(buys=10, sells=0), txns_h1=TxnWindow(buys=50, sells=0))
        assert buy_pressure_score(pair) == pytest.approx(100.0)

    def test_all_sells_scores_zero(self):
        pair = make_pair(txns_m5=TxnWindow(buys=0, sells=10), txns_h1=TxnWindow(buys=0, sells=50))
        assert buy_pressure_score(pair) == pytest.approx(0.0)

    def test_balanced_scores_fifty(self):
        pair = make_pair(txns_m5=TxnWindow(buys=5, sells=5), txns_h1=TxnWindow(buys=25, sells=25))
        assert buy_pressure_score(pair) == pytest.approx(50.0)

    def test_no_txns_scores_neutral_fifty(self):
        pair = make_pair(txns_m5=TxnWindow(0, 0), txns_h1=TxnWindow(0, 0))
        assert buy_pressure_score(pair) == pytest.approx(50.0)


class TestBoostBonusScore:
    def test_no_boost_scores_zero(self):
        pair = make_pair(boost_amount=0.0)
        assert boost_bonus_score(pair) == 0.0

    def test_partial_boost_scores_between(self):
        pair = make_pair(boost_amount=250.0)
        assert boost_bonus_score(pair, saturation_amount=500.0) == pytest.approx(50.0)

    def test_large_boost_caps_at_100(self):
        pair = make_pair(boost_amount=10_000.0)
        assert boost_bonus_score(pair, saturation_amount=500.0) == pytest.approx(100.0)


class TestCompositeScore:
    def test_strong_candidate_scores_high(self):
        settings = Settings()
        pair = make_pair(
            volume_h1=1200.0,
            volume_m5=400.0,
            price_change_m5=10.0,
            price_change_h1=30.0,
            txns_m5=TxnWindow(buys=20, sells=2),
            txns_h1=TxnWindow(buys=80, sells=20),
            boost_amount=500.0,
        )
        result = compute_score(pair, settings)
        assert result.score > 70.0

    def test_weak_candidate_scores_low(self):
        settings = Settings()
        pair = make_pair(
            volume_h1=1200.0,
            volume_m5=10.0,
            price_change_m5=-10.0,
            price_change_h1=-30.0,
            txns_m5=TxnWindow(buys=1, sells=20),
            txns_h1=TxnWindow(buys=5, sells=80),
            boost_amount=0.0,
        )
        result = compute_score(pair, settings)
        assert result.score < 30.0

    def test_score_is_clamped_to_0_100(self):
        settings = Settings()
        pair = make_pair(volume_h1=1200.0, volume_m5=10_000.0, price_change_m5=500.0, price_change_h1=500.0)
        result = compute_score(pair, settings)
        assert 0.0 <= result.score <= 100.0


class TestGatingFilters:
    def setup_method(self):
        self.settings = Settings(
            min_liquidity_usd=10_000.0,
            min_token_age_minutes=10.0,
            max_token_age_hours=72.0,
            max_open_positions=5,
        )

    def test_passes_with_default_pair(self):
        passed, _reason = passes_gating_filters(make_pair(), self.settings, [], 0)
        assert passed is True

    def test_fails_low_liquidity(self):
        pair = make_pair(liquidity_usd=1_000.0)
        passed, reason = passes_gating_filters(pair, self.settings, [], 0)
        assert passed is False
        assert "liquidity" in reason

    def test_fails_too_young(self):
        pair = make_pair(pair_created_at_ms=int((time.time() - 60) * 1000))  # 1 minute old
        passed, reason = passes_gating_filters(pair, self.settings, [], 0)
        assert passed is False
        assert "age" in reason

    def test_fails_too_old(self):
        pair = make_pair(pair_created_at_ms=int((time.time() - 100 * 3600) * 1000))  # 100 hours old
        passed, reason = passes_gating_filters(pair, self.settings, [], 0)
        assert passed is False
        assert "age" in reason

    def test_fails_already_holding(self):
        pair = make_pair(base_token_address="token123")
        passed, reason = passes_gating_filters(pair, self.settings, ["token123"], 1)
        assert passed is False
        assert "already holding" in reason

    def test_fails_max_positions_reached(self):
        passed, reason = passes_gating_filters(make_pair(), self.settings, [], 5)
        assert passed is False
        assert "max open positions" in reason

    def test_unknown_age_pair_not_rejected_on_age(self):
        pair = make_pair(pair_created_at_ms=None)
        passed, _reason = passes_gating_filters(pair, self.settings, [], 0)
        assert passed is True

    def test_score_candidates_filters_and_sorts_best_first(self):
        good = make_pair(
            base_token_address="good",
            volume_h1=1200.0,
            volume_m5=400.0,
            price_change_m5=10.0,
            price_change_h1=20.0,
            txns_m5=TxnWindow(20, 2),
            txns_h1=TxnWindow(80, 20),
        )
        mediocre = make_pair(base_token_address="mediocre")
        bad_liquidity = make_pair(base_token_address="bad_liq", liquidity_usd=500.0)

        scored = score_candidates([bad_liquidity, mediocre, good], self.settings, [], 0)

        assert [c.pair.base_token_address for c in scored] == ["good", "mediocre"]
        assert scored[0].score >= scored[1].score
