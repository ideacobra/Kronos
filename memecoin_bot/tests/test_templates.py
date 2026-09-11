"""Tests for social/templates.py: factual content, disclaimer, and the
anti-hype-language honesty guardrail.
"""
from __future__ import annotations

import pytest

from memecoin_bot.social.templates import (
    LIVE_DISCLAIMER,
    LIVE_TRADING_TAG,
    PAPER_TRADING_TAG,
    SIMULATED_DISCLAIMER,
    contains_hype_language,
    format_portfolio_summary,
    format_trade_closed,
    format_trade_opened,
)


class TestHypeGuardrail:
    @pytest.mark.parametrize(
        "phrase",
        [
            "to the moon!",
            "PUMP IT now",
            "buy now!!!",
            "guaranteed 100x gains",
            "LFG",
            "this is risk-free",
            "🚀🚀🚀",
        ],
    )
    def test_detects_known_buzzwords(self, phrase):
        assert contains_hype_language(phrase) is True

    def test_factual_text_is_clean(self):
        text = "Paper trade OPENED: $FOO. Entry price: $0.001. Simulated trade — not financial advice."
        assert contains_hype_language(text) is False


class TestTradeOpenedTemplate:
    def _render(self, **overrides):
        params = dict(
            symbol="FOO",
            token_address="TokenAddress1234567890",
            entry_price=0.00042,
            size_usd=50.0,
            score=72.5,
        )
        params.update(overrides)
        return format_trade_opened(**params)

    def test_contains_disclaimer(self):
        assert SIMULATED_DISCLAIMER in self._render()

    def test_contains_factual_fields(self):
        text = self._render()
        assert "FOO" in text
        assert "50.00" in text  # virtual size
        assert "72" in text  # score
        assert "TokenAddress1234567890"[:4] in text  # shortened address present

    def test_no_hype_language(self):
        assert contains_hype_language(self._render()) is False

    def test_kronos_confirmation_included_when_present(self):
        text = self._render(kronos_confirmed=True)
        assert "Kronos" in text
        assert contains_hype_language(text) is False

    def test_kronos_line_omitted_when_not_provided(self):
        text = self._render(kronos_confirmed=None)
        assert "Kronos" not in text

    def test_live_wording_replaces_paper_wording(self):
        text = self._render(is_live=True)
        assert "Live trade OPENED" in text
        assert "Paper trade" not in text
        assert LIVE_DISCLAIMER in text
        assert SIMULATED_DISCLAIMER not in text
        assert LIVE_TRADING_TAG in text
        assert PAPER_TRADING_TAG not in text

    def test_live_wording_still_contains_factual_fields(self):
        text = self._render(is_live=True)
        assert "FOO" in text
        assert "50.00" in text
        assert "72" in text
        assert "TokenAddress1234567890"[:4] in text

    def test_live_wording_passes_hype_guardrail(self):
        assert contains_hype_language(self._render(is_live=True)) is False

    def test_default_is_paper_wording(self):
        text = self._render()
        assert "Paper trade OPENED" in text
        assert "Live trade" not in text


class TestTradeClosedTemplate:
    def _render(self, **overrides):
        params = dict(
            symbol="FOO",
            token_address="TokenAddress1234567890",
            entry_price=0.001,
            exit_price=0.0014,
            size_usd=100.0,
            realized_pnl_usd=40.0,
            realized_pnl_pct=40.0,
            hold_minutes=35.0,
            exit_reason="take_profit",
        )
        params.update(overrides)
        return format_trade_closed(**params)

    def test_contains_disclaimer(self):
        assert SIMULATED_DISCLAIMER in self._render()

    def test_contains_factual_fields(self):
        text = self._render()
        assert "FOO" in text
        assert "+40.0%" in text
        assert "35m" in text
        assert "take-profit" in text

    def test_win_labeled_win(self):
        text = self._render(realized_pnl_usd=40.0, realized_pnl_pct=40.0)
        assert "WIN" in text

    def test_loss_labeled_loss(self):
        text = self._render(realized_pnl_usd=-20.0, realized_pnl_pct=-20.0, exit_reason="stop_loss")
        assert "LOSS" in text
        assert "stop-loss" in text

    def test_no_hype_language_win_or_loss(self):
        assert contains_hype_language(self._render(realized_pnl_usd=40.0, realized_pnl_pct=40.0)) is False
        assert contains_hype_language(self._render(realized_pnl_usd=-20.0, realized_pnl_pct=-20.0)) is False

    def test_long_hold_reported_in_hours(self):
        text = self._render(hold_minutes=185.0)
        assert "3.1h" in text

    def test_live_wording_replaces_paper_wording(self):
        text = self._render(is_live=True)
        assert "Live trade CLOSED" in text
        assert "Paper trade" not in text
        assert LIVE_DISCLAIMER in text
        assert SIMULATED_DISCLAIMER not in text
        assert LIVE_TRADING_TAG in text
        assert PAPER_TRADING_TAG not in text

    def test_live_wording_still_contains_factual_fields(self):
        text = self._render(is_live=True)
        assert "FOO" in text
        assert "+40.0%" in text
        assert "35m" in text
        assert "take-profit" in text

    def test_live_wording_passes_hype_guardrail_win_and_loss(self):
        assert contains_hype_language(self._render(is_live=True, realized_pnl_usd=40.0, realized_pnl_pct=40.0)) is False
        assert contains_hype_language(self._render(is_live=True, realized_pnl_usd=-20.0, realized_pnl_pct=-20.0)) is False


class TestPortfolioSummaryTemplate:
    def _render(self, **overrides):
        params = dict(
            equity_usd=1050.0,
            cash_usd=900.0,
            starting_balance_usd=1000.0,
            realized_pnl_usd=60.0,
            unrealized_pnl_usd=-10.0,
            open_positions=2,
            total_trades=5,
            win_rate_pct=60.0,
        )
        params.update(overrides)
        return format_portfolio_summary(**params)

    def test_contains_disclaimer(self):
        assert SIMULATED_DISCLAIMER in self._render()

    def test_contains_factual_fields(self):
        text = self._render()
        assert "1,050.00" in text
        assert "900.00" in text
        assert "60.0%" in text  # win rate

    def test_no_hype_language(self):
        assert contains_hype_language(self._render()) is False

    def test_live_wording_replaces_paper_wording(self):
        text = self._render(is_live=True)
        assert "Live portfolio summary" in text
        assert "Paper portfolio summary" not in text
        assert LIVE_DISCLAIMER in text
        assert LIVE_TRADING_TAG in text

    def test_live_wording_still_contains_factual_fields(self):
        text = self._render(is_live=True)
        assert "1,050.00" in text
        assert "900.00" in text
        assert "60.0%" in text

    def test_live_wording_passes_hype_guardrail(self):
        assert contains_hype_language(self._render(is_live=True)) is False
