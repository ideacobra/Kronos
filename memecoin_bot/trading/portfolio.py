"""Paper portfolio accounting: cash, open positions, realized/unrealized P&L,
and the equity curve. Persists through an injected `Storage` (see storage.py).

This module has no knowledge of *how* a fill was produced (see
trading/broker.py) -- it only records the state changes that result from a
fill, so it can be unit tested without any HTTP/model dependency.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _utc_day_start_epoch(now: Optional[float] = None) -> float:
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


@dataclass
class Position:
    """An open paper position."""

    token_address: str
    symbol: str
    entry_price: float
    quantity: float
    size_usd: float  # cost basis: total cash committed (post entry-fee)
    entry_fee_usd: float
    opened_at: float  # epoch seconds
    stop_loss_price: float
    take_profit_price: float
    max_hold_minutes: float
    kronos_confirmed: Optional[bool] = None
    score: float = 0.0
    trade_id: Optional[int] = None

    def unrealized_pnl_usd(self, current_price: float) -> float:
        return self.quantity * current_price - self.size_usd

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.size_usd <= 0:
            return 0.0
        return self.unrealized_pnl_usd(current_price) / self.size_usd * 100.0

    def hold_minutes(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return (now - self.opened_at) / 60.0


@dataclass
class ClosedTrade:
    """A completed round-trip trade, produced by `Portfolio.close_position`."""

    token_address: str
    symbol: str
    entry_price: float
    exit_price: float
    quantity: float
    size_usd: float
    entry_fee_usd: float
    exit_fee_usd: float
    opened_at: float
    closed_at: float
    exit_reason: str
    realized_pnl_usd: float
    realized_pnl_pct: float
    kronos_confirmed: Optional[bool] = None
    trade_id: Optional[int] = None

    @property
    def hold_minutes(self) -> float:
        return (self.closed_at - self.opened_at) / 60.0


class Portfolio:
    """Tracks paper cash, open positions, and closed-trade history."""

    def __init__(self, starting_balance_usd: float, storage=None):
        self.starting_balance_usd = starting_balance_usd
        self.cash_usd = starting_balance_usd
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[ClosedTrade] = []
        self.realized_pnl_usd = 0.0
        self.storage = storage

    @classmethod
    def from_storage(cls, starting_balance_usd: float, storage) -> "Portfolio":
        """Reconstruct a Portfolio's cash/positions from persisted trade history.

        This lets `cli.py run` resume correctly across process restarts
        instead of silently re-crediting `starting_balance_usd` every time.
        Cash is derived as: starting balance + all historical realized P&L -
        cost basis of positions that are still open (i.e. not yet returned
        to cash by a close).
        """
        portfolio = cls(starting_balance_usd, storage=storage)
        open_rows = storage.get_open_trades()
        closed_rows = storage.get_recent_trades(limit=10_000_000, status="closed")

        realized_total = sum(row["realized_pnl_usd"] or 0.0 for row in closed_rows)
        open_cost_basis = sum(row["size_usd"] or 0.0 for row in open_rows)
        portfolio.cash_usd = starting_balance_usd + realized_total - open_cost_basis
        portfolio.realized_pnl_usd = realized_total

        for row in open_rows:
            position = Position(
                token_address=row["token_address"],
                symbol=row["symbol"] or "",
                entry_price=row["entry_price"],
                quantity=row["quantity"],
                size_usd=row["size_usd"],
                entry_fee_usd=row["entry_fee_usd"] or 0.0,
                opened_at=row["opened_at"],
                stop_loss_price=row["stop_loss_price"],
                take_profit_price=row["take_profit_price"],
                max_hold_minutes=row["max_hold_minutes"],
                kronos_confirmed=(None if row["kronos_confirmed"] is None else bool(row["kronos_confirmed"])),
                trade_id=row["id"],
            )
            portfolio.positions[position.token_address] = position
        return portfolio

    # --- queries --------------------------------------------------------
    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    def has_position(self, token_address: str) -> bool:
        return token_address in self.positions

    def get_position(self, token_address: str) -> Optional[Position]:
        return self.positions.get(token_address)

    def unrealized_pnl_usd(self, current_prices: Dict[str, float]) -> float:
        total = 0.0
        for address, position in self.positions.items():
            price = current_prices.get(address, position.entry_price)
            total += position.unrealized_pnl_usd(price)
        return total

    def equity_usd(self, current_prices: Dict[str, float]) -> float:
        market_value = 0.0
        for address, position in self.positions.items():
            price = current_prices.get(address, position.entry_price)
            market_value += position.quantity * price
        return self.cash_usd + market_value

    def realized_pnl_today_usd(self, now: Optional[float] = None) -> float:
        day_start = _utc_day_start_epoch(now)
        return sum(t.realized_pnl_usd for t in self.closed_trades if t.closed_at >= day_start)

    # --- mutations --------------------------------------------------------
    def open_position(self, position: Position) -> Position:
        if position.token_address in self.positions:
            raise ValueError(f"Already holding a position in {position.token_address}")
        if position.size_usd > self.cash_usd + 1e-6:
            raise ValueError("Insufficient paper cash to open this position")
        self.cash_usd -= position.size_usd
        self.positions[position.token_address] = position
        if self.storage is not None:
            position.trade_id = self.storage.record_trade_open(position)
        return position

    def close_position(
        self,
        token_address: str,
        exit_price: float,
        exit_fee_usd: float,
        exit_reason: str,
        closed_at: Optional[float] = None,
    ) -> ClosedTrade:
        if token_address not in self.positions:
            raise KeyError(f"No open position for {token_address}")
        position = self.positions.pop(token_address)
        closed_at = closed_at if closed_at is not None else time.time()

        gross_proceeds = position.quantity * exit_price
        net_proceeds = gross_proceeds - exit_fee_usd
        realized_pnl_usd = net_proceeds - position.size_usd
        realized_pnl_pct = (
            (realized_pnl_usd / position.size_usd * 100.0) if position.size_usd > 0 else 0.0
        )

        self.cash_usd += net_proceeds
        self.realized_pnl_usd += realized_pnl_usd

        trade = ClosedTrade(
            token_address=token_address,
            symbol=position.symbol,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            size_usd=position.size_usd,
            entry_fee_usd=position.entry_fee_usd,
            exit_fee_usd=exit_fee_usd,
            opened_at=position.opened_at,
            closed_at=closed_at,
            exit_reason=exit_reason,
            realized_pnl_usd=realized_pnl_usd,
            realized_pnl_pct=realized_pnl_pct,
            kronos_confirmed=position.kronos_confirmed,
            trade_id=position.trade_id,
        )
        self.closed_trades.append(trade)
        if self.storage is not None:
            self.storage.record_trade_close(trade)
        return trade

    def record_equity_point(self, current_prices: Dict[str, float], timestamp: Optional[float] = None) -> None:
        timestamp = timestamp if timestamp is not None else time.time()
        equity = self.equity_usd(current_prices)
        unrealized = self.unrealized_pnl_usd(current_prices)
        if self.storage is not None:
            self.storage.record_equity_point(
                timestamp=timestamp,
                equity_usd=equity,
                cash_usd=self.cash_usd,
                unrealized_pnl_usd=unrealized,
                realized_pnl_usd=self.realized_pnl_usd,
                open_positions=self.open_position_count,
            )
