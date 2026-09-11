"""SQLite persistence for memecoin_bot: scans, trades, and the equity curve.

Uses only the Python standard library (`sqlite3`) -- no ORM, no extra
dependency. The schema is created automatically on first use.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from memecoin_bot.data.types import ScoredCandidate
from memecoin_bot.trading.portfolio import ClosedTrade, Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT,
    score REAL,
    price_usd REAL,
    liquidity_usd REAL,
    volume_h1 REAL,
    decision TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    symbol TEXT,
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity REAL NOT NULL,
    size_usd REAL NOT NULL,
    entry_fee_usd REAL NOT NULL DEFAULT 0,
    exit_fee_usd REAL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    exit_reason TEXT,
    stop_loss_price REAL,
    take_profit_price REAL,
    max_hold_minutes REAL,
    realized_pnl_usd REAL,
    realized_pnl_pct REAL,
    kronos_confirmed INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    equity_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    unrealized_pnl_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    open_positions INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_equity_timestamp ON equity_curve(timestamp);
"""


def _kronos_to_int(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


class Storage:
    """A small synchronous SQLite wrapper. Not thread-safe by design -- the
    bot runs a single-threaded scan/trade loop.
    """

    def __init__(self, db_path: Union[str, Path] = "memecoin_bot.db"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    # --- scans ------------------------------------------------------------
    def record_scan(self, candidate: ScoredCandidate, decision: str, reason: str = "") -> int:
        pair = candidate.pair
        cur = self._conn.execute(
            """
            INSERT INTO scans (timestamp, token_address, symbol, score, price_usd, liquidity_usd, volume_h1, decision, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                pair.base_token_address,
                pair.symbol,
                candidate.score,
                pair.price_usd,
                pair.liquidity_usd,
                pair.volume_h1,
                decision,
                reason,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_recent_scans(self, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM scans ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    # --- trades ------------------------------------------------------------
    def record_trade_open(self, position: Position) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO trades (
                token_address, symbol, entry_price, quantity, size_usd,
                entry_fee_usd, opened_at, stop_loss_price, take_profit_price,
                max_hold_minutes, kronos_confirmed, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                position.token_address,
                position.symbol,
                position.entry_price,
                position.quantity,
                position.size_usd,
                position.entry_fee_usd,
                position.opened_at,
                position.stop_loss_price,
                position.take_profit_price,
                position.max_hold_minutes,
                _kronos_to_int(position.kronos_confirmed),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_trade_close(self, trade: ClosedTrade) -> None:
        if trade.trade_id is not None:
            self._conn.execute(
                """
                UPDATE trades
                SET exit_price = ?, exit_fee_usd = ?, closed_at = ?, exit_reason = ?,
                    realized_pnl_usd = ?, realized_pnl_pct = ?, status = 'closed'
                WHERE id = ?
                """,
                (
                    trade.exit_price,
                    trade.exit_fee_usd,
                    trade.closed_at,
                    trade.exit_reason,
                    trade.realized_pnl_usd,
                    trade.realized_pnl_pct,
                    trade.trade_id,
                ),
            )
        else:
            # No matching open row (e.g. constructed outside the normal
            # open->close flow); insert a fully-closed record instead.
            self._conn.execute(
                """
                INSERT INTO trades (
                    token_address, symbol, entry_price, exit_price, quantity, size_usd,
                    entry_fee_usd, exit_fee_usd, opened_at, closed_at, exit_reason,
                    realized_pnl_usd, realized_pnl_pct, kronos_confirmed, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed')
                """,
                (
                    trade.token_address,
                    trade.symbol,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.size_usd,
                    trade.entry_fee_usd,
                    trade.exit_fee_usd,
                    trade.opened_at,
                    trade.closed_at,
                    trade.exit_reason,
                    trade.realized_pnl_usd,
                    trade.realized_pnl_pct,
                    _kronos_to_int(trade.kronos_confirmed),
                ),
            )
        self._conn.commit()

    def get_open_trades(self) -> List[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at ASC")
        return [dict(row) for row in cur.fetchall()]

    def get_recent_trades(self, limit: int = 20, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None:
            cur = self._conn.execute(
                "SELECT * FROM trades WHERE status = ? ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM trades ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in cur.fetchall()]

    # --- equity curve --------------------------------------------------------
    def record_equity_point(
        self,
        timestamp: float,
        equity_usd: float,
        cash_usd: float,
        unrealized_pnl_usd: float,
        realized_pnl_usd: float,
        open_positions: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO equity_curve (timestamp, equity_usd, cash_usd, unrealized_pnl_usd, realized_pnl_usd, open_positions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp, equity_usd, cash_usd, unrealized_pnl_usd, realized_pnl_usd, open_positions),
        )
        self._conn.commit()

    def get_equity_curve(self, limit: int = 1000) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM equity_curve ORDER BY timestamp ASC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_latest_equity_point(self) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM equity_curve ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    # --- summary stats (for `cli.py stats`) --------------------------------
    def get_summary_stats(self) -> Dict[str, Any]:
        closed = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN realized_pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(realized_pnl_usd), 0.0) AS total_realized_pnl_usd,
                COALESCE(AVG(realized_pnl_pct), 0.0) AS avg_realized_pnl_pct,
                COALESCE(MAX(realized_pnl_usd), 0.0) AS best_trade_usd,
                COALESCE(MIN(realized_pnl_usd), 0.0) AS worst_trade_usd
            FROM trades WHERE status = 'closed'
            """
        ).fetchone()
        open_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status = 'open'"
        ).fetchone()["c"]
        latest_equity = self.get_latest_equity_point()

        total_trades = closed["total_trades"] or 0
        wins = closed["wins"] or 0
        win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": closed["losses"] or 0,
            "win_rate_pct": win_rate,
            "total_realized_pnl_usd": closed["total_realized_pnl_usd"],
            "avg_realized_pnl_pct": closed["avg_realized_pnl_pct"],
            "best_trade_usd": closed["best_trade_usd"],
            "worst_trade_usd": closed["worst_trade_usd"],
            "open_positions": open_count,
            "latest_equity": latest_equity,
        }
