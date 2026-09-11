"""Risk management: position sizing, stop-loss/take-profit, max hold
duration, max concurrent positions, and a daily realized-loss circuit
breaker that halts new entries for the rest of the (UTC) day if breached.

This module never touches the network; it is pure decision logic over a
`Portfolio` snapshot, which keeps it fully unit-testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from memecoin_bot.config import Settings
from memecoin_bot.trading.portfolio import Portfolio, Position

# Exit reasons used consistently across risk checks, storage, and templates.
EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_TIME = "time_exit"


@dataclass
class EntryDecision:
    """Whether a new position may be opened, and at what paper size."""

    approved: bool
    size_usd: float
    reason: str


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    # --- sizing -----------------------------------------------------------
    def position_size_usd(self, equity_usd: float) -> float:
        """Percent-of-equity sizing, capped at a fixed max USD per trade."""
        size = max(0.0, equity_usd) * self.settings.position_size_pct
        return min(size, self.settings.max_position_usd)

    def compute_stop_and_take_prices(self, entry_price: float) -> tuple[float, float]:
        stop_loss_price = entry_price * (1.0 - self.settings.stop_loss_pct)
        take_profit_price = entry_price * (1.0 + self.settings.take_profit_pct)
        return stop_loss_price, take_profit_price

    # --- circuit breaker ----------------------------------------------------
    def circuit_breaker_tripped(self, portfolio: Portfolio, now: Optional[float] = None) -> bool:
        """True once today's realized losses reach the configured limit."""
        limit = abs(self.settings.daily_realized_loss_limit_usd)
        if limit <= 0:
            return False
        return portfolio.realized_pnl_today_usd(now) <= -limit

    # --- entry gating --------------------------------------------------------
    def evaluate_entry(
        self,
        portfolio: Portfolio,
        token_address: str,
        equity_usd: float,
        now: Optional[float] = None,
    ) -> EntryDecision:
        """Decide whether to open a new position and at what paper size.

        Assumes DexScreener-level gating filters (liquidity, age, "already
        scored") were already applied by strategy/scoring.py -- this focuses
        on *risk* limits: exposure count, daily loss circuit breaker, and
        available paper cash.
        """
        if portfolio.has_position(token_address):
            return EntryDecision(False, 0.0, "already holding a position in this token")

        if portfolio.open_position_count >= self.settings.max_open_positions:
            return EntryDecision(False, 0.0, "max open positions reached")

        if self.circuit_breaker_tripped(portfolio, now):
            return EntryDecision(
                False,
                0.0,
                f"daily realized-loss circuit breaker tripped (limit ${self.settings.daily_realized_loss_limit_usd:,.2f})",
            )

        size_usd = self.position_size_usd(equity_usd)
        size_usd = min(size_usd, portfolio.cash_usd)
        if size_usd <= 0:
            return EntryDecision(False, 0.0, "insufficient paper cash")

        return EntryDecision(True, size_usd, "ok")

    # --- exit checks -----------------------------------------------------------
    def check_exit(self, position: Position, current_price: float, now: Optional[float] = None) -> Optional[str]:
        """Return an exit reason string if the position should be closed now,
        else None. Priority: stop-loss > take-profit > max hold duration.
        """
        now = now if now is not None else time.time()

        if current_price <= position.stop_loss_price:
            return EXIT_STOP_LOSS
        if current_price >= position.take_profit_price:
            return EXIT_TAKE_PROFIT
        if position.hold_minutes(now) >= position.max_hold_minutes:
            return EXIT_TIME
        return None
