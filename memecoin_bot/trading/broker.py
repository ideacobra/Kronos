"""Trade execution abstraction: `Broker`, `PaperBroker`, and the explicit
live-trading stub `LiveJupiterBroker`.

SAFETY: `PaperBroker` is the only broker implemented here, and it never
touches the network or a real wallet -- it only does arithmetic to
simulate a fill (price impact via slippage + a fee) against numbers the
bot already fetched from public market-data APIs. `LiveJupiterBroker` is
an intentionally inert stub: it cannot be instantiated, let alone place a
real trade. See its docstring for what real implementation would require.
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


class LiveJupiterBroker(Broker):
    """STUB. Not implemented. Do not implement without reading this fully.

    This class is an explicit extension point for someone who later wants
    to place *real* trades on Solana via Jupiter's aggregator, using a
    *real* funded wallet. It is intentionally inert: constructing it always
    raises `NotImplementedError`, and every method also raises so there is
    no path -- accidental or otherwise -- through which this codebase can
    move real funds today.

    To implement this for real, you would need at minimum:
      1. A Solana keypair with actual SOL/SPL token balances, loaded from a
         secure secret store (e.g. an OS keychain or an HSM) -- never a
         plaintext env var or a file committed to a repo. This bot's config
         layer (memecoin_bot/config.py) deliberately has no concept of a
         private key.
      2. Integration with Jupiter's Swap API
         (https://station.jup.ag/docs/apis/swap-api) to fetch a route/quote
         and build an unsigned swap transaction for the desired token pair
         and amount.
      3. Priority fee / compute-unit-price handling so transactions land
         reliably during congestion, typically via Jupiter's `prioritizationFeeLamports`
         option or a separate fee-estimation service.
      4. Slippage protection: a maximum acceptable `slippageBps` (or Jupiter's
         dynamic slippage estimator) plus a check that the returned quote's
         price impact is within an acceptable bound before ever signing.
      5. Transaction signing (with the keypair from #1) and submission via a
         Solana RPC client (e.g. `solana-py` / `solders`), with confirmation
         polling, retry-with-backoff, and handling for dropped/expired
         blockhashes.
      6. Careful handling of partial fills, failed simulations, and dust
         amounts, plus real accounting reconciliation against on-chain
         balances rather than assumed fills.

    None of the above exists in this repository, by design.
    """

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "LiveJupiterBroker is a documented stub and is not implemented. "
            "This bot is paper-trading only. See this class's docstring for "
            "what real implementation would require (Solana keypair custody, "
            "Jupiter Swap API integration, priority fees, slippage protection, "
            "transaction signing/submission)."
        )

    def open_position(self, token_address: str, symbol: str, reference_price: float, size_usd: float) -> Fill:
        raise NotImplementedError("LiveJupiterBroker.open_position is not implemented. See class docstring.")

    def close_position(self, token_address: str, symbol: str, quantity: float, reference_price: float) -> Fill:
        raise NotImplementedError("LiveJupiterBroker.close_position is not implemented. See class docstring.")

    def mark_to_market(self, token_address: str, reference_price: float) -> float:
        raise NotImplementedError("LiveJupiterBroker.mark_to_market is not implemented. See class docstring.")
