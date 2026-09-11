"""Shared dataclasses for market data used across memecoin_bot.

These are intentionally plain, serialization-friendly dataclasses (no
pandas/numpy dependency) so they can be stored, logged, and compared easily.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TxnWindow:
    """Buy/sell counts for a given lookback window (e.g. m5, h1, h6, h24)."""

    buys: int = 0
    sells: int = 0

    @property
    def total(self) -> int:
        return self.buys + self.sells

    @property
    def buy_ratio(self) -> float:
        """Fraction of transactions that were buys, in [0, 1]. 0.5 if no txns."""
        if self.total == 0:
            return 0.5
        return self.buys / self.total


@dataclass
class TokenPair:
    """A single DEX trading pair for a token, normalized from DexScreener data.

    Field names intentionally mirror the DexScreener API response shape
    (see data/dexscreener.py) so parsing stays a thin, obvious mapping.
    """

    chain_id: str
    dex_id: str
    pair_address: str
    base_token_address: str
    base_token_name: str
    base_token_symbol: str
    quote_token_symbol: str
    price_usd: float
    price_change_m5: float
    price_change_h1: float
    price_change_h6: float
    price_change_h24: float
    volume_m5: float
    volume_h1: float
    volume_h6: float
    volume_h24: float
    liquidity_usd: float
    txns_m5: TxnWindow
    txns_h1: TxnWindow
    fdv: Optional[float] = None
    market_cap: Optional[float] = None
    pair_created_at_ms: Optional[int] = None
    url: Optional[str] = None
    boost_amount: float = 0.0

    @property
    def age_minutes(self) -> Optional[float]:
        if self.pair_created_at_ms is None:
            return None
        age_seconds = time.time() - (self.pair_created_at_ms / 1000.0)
        return max(age_seconds, 0.0) / 60.0

    @property
    def symbol(self) -> str:
        return self.base_token_symbol or self.base_token_address[:6]


@dataclass
class Candle:
    """A single OHLCV candle, epoch seconds timestamp (UTC)."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ScoredCandidate:
    """A TokenPair plus its computed FOMO/momentum score and component breakdown."""

    pair: TokenPair
    score: float
    volume_surge_score: float
    momentum_score: float
    buy_pressure_score: float
    boost_bonus_score: float
    reasons: list = field(default_factory=list)
