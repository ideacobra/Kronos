"""Trade execution abstraction: `Broker` (the interface) and `PaperBroker`
(the fully-simulated implementation used by default).

SAFETY: `PaperBroker` never touches the network or a real wallet -- it only
does arithmetic to simulate a fill (price impact via slippage + a fee)
against numbers the bot already fetched from public market-data APIs.

The real, network-touching implementation, `LiveJupiterBroker`, lives in
`trading/live_broker.py` (kept separate so importing this module -- and
running the bot in its default paper-trading mode -- never pulls in
`solders`/Solana RPC/Jupiter dependencies). It is only ever constructed by
`cli.py` after `run --live` passes every safety gate documented in
`memecoin_bot/README.md`'s "Going Live" section.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Fill:
    """The result of a (simulated or real) trade execution.

    `gross_usd` is the notional value of the trade before fees (for a buy:
    the USD amount committed; for a sell: quantity * fill price). `fee_usd`
    is subtracted from `gross_usd` to get the actual cash impact.
    """

    price: float
    quantity: float
    fee_usd: float
    gross_usd: float

    @property
    def net_usd(self) -> float:
        return self.gross_usd - self.fee_usd


class Broker(ABC):
    """Abstract execution interface. Implementations decide how a requested
    trade is actually filled (simulated or, in principle, real).
    """

    @abstractmethod
    def open_position(self, token_address: str, symbol: str, reference_price: float, size_usd: float) -> Fill:
        """Attempt to spend `size_usd` buying `token_address` near `reference_price`.

        Returns a Fill describing the simulated/actual execution.
        """
        raise NotImplementedError

    @abstractmethod
    def close_position(self, token_address: str, symbol: str, quantity: float, reference_price: float) -> Fill:
        """Attempt to sell `quantity` of `token_address` near `reference_price`."""
        raise NotImplementedError

    @abstractmethod
    def mark_to_market(self, token_address: str, reference_price: float) -> float:
        """Return the current fair-value price to use for unrealized P&L marks.

        This does not execute a trade; it's purely a valuation hook (a real
        broker could use an order-book mid-price instead of the last trade).
        """
        raise NotImplementedError


class PaperBroker(Broker):
    """Fully simulated broker: no network calls, no real funds.

    Applies a simple, symmetric slippage + fee model so paper P&L is not
    unrealistically generous compared to what a real (illiquid, memecoin)
    market would do to a market order.
    """

    def __init__(self, slippage_pct: float = 0.01, fee_pct: float = 0.003):
        if slippage_pct < 0 or fee_pct < 0:
            raise ValueError("slippage_pct and fee_pct must be non-negative")
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct

    def open_position(self, token_address: str, symbol: str, reference_price: float, size_usd: float) -> Fill:
        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if size_usd <= 0:
            raise ValueError("size_usd must be positive")
        # Buying pushes the price up against us.
        fill_price = reference_price * (1.0 + self.slippage_pct)
        fee_usd = size_usd * self.fee_pct
        quantity = (size_usd - fee_usd) / fill_price
        return Fill(price=fill_price, quantity=quantity, fee_usd=fee_usd, gross_usd=size_usd)

    def close_position(self, token_address: str, symbol: str, quantity: float, reference_price: float) -> Fill:
        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        # Selling pushes the price down against us.
        fill_price = reference_price * (1.0 - self.slippage_pct)
        gross_usd = quantity * fill_price
        fee_usd = gross_usd * self.fee_pct
        return Fill(price=fill_price, quantity=quantity, fee_usd=fee_usd, gross_usd=gross_usd)

    def mark_to_market(self, token_address: str, reference_price: float) -> float:
        return reference_price
