"""LiveJupiterBroker: real Solana swap execution via Jupiter's Swap API.

This is the only code path in this repository that can move real funds.
Every trade goes through the following safety sequence:

  1. Fetch the REAL on-chain wallet balance (never the paper portfolio's
     internal ledger) and refuse to size a trade beyond it.
  2. Get a Jupiter quote with the configured slippage tolerance.
  3. Build, sign (locally, with the keypair loaded from `SOLANA_PRIVATE_KEY`),
     and submit exactly one transaction -- this module never auto-retries a
     submission, which could otherwise double-submit.
  4. Poll for on-chain confirmation via `getSignatureStatuses`. A fill is
     NEVER recorded unless the transaction is confirmed; an unconfirmed
     (timed out) transaction raises `LiveTradeUnconfirmedError` instead of
     assuming either success or failure.
  5. Derive the actual fill quantity/proceeds from a real pre/post wallet
     balance snapshot (ground truth), not from the quote's estimate alone.

Constructing this class requires `settings.enable_live_trading` to be true
and a private (non-public) `SOLANA_RPC_URL` to already be configured; it
re-validates this itself as defense-in-depth even though `cli.py`'s go-live
gate already checks it before ever constructing a broker.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional, Tuple, Union

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from memecoin_bot.config import Settings
from memecoin_bot.trading.broker import Broker, Fill
from memecoin_bot.trading.jupiter import SOL_MINT, JupiterClient
from memecoin_bot.trading.solana_rpc import (
    BASE_TX_FEE_LAMPORTS,
    LAMPORTS_PER_SOL,
    SolanaRpcClient,
    is_public_rpc_url,
    mask_rpc_url,
)
from memecoin_bot.trading.wallet import load_keypair_from_env

logger = logging.getLogger(__name__)


class LiveBrokerConfigError(RuntimeError):
    """Raised when LiveJupiterBroker is misconfigured (missing/public RPC,
    live trading not enabled, missing/invalid private key, etc.).
    """


class LiveBrokerInsufficientBalanceError(RuntimeError):
    """Raised when a requested trade would exceed the real on-chain wallet
    balance. The paper portfolio's ledger is never trusted for this check.
    """


class LiveTradeFailedError(RuntimeError):
    """Raised when a submitted transaction failed on-chain, or confirmed
    without producing an observable balance change.
    """


class LiveTradeUnconfirmedError(RuntimeError):
    """Raised when a submitted transaction's outcome could not be confirmed
    within the timeout. The outcome is UNKNOWN: the caller must not assume
    success or failure, and must not blindly resubmit.
    """


class LiveJupiterBroker(Broker):
    """Real Solana swap execution via Jupiter. See module docstring."""

    def __init__(
        self,
        settings: Settings,
        rpc_client: Optional[SolanaRpcClient] = None,
        jupiter_client: Optional[JupiterClient] = None,
        keypair: Optional[Keypair] = None,
    ):
        if not settings.enable_live_trading:
            raise LiveBrokerConfigError(
                "Refusing to construct LiveJupiterBroker: ENABLE_LIVE_TRADING is not set to true."
            )
        if not settings.solana_rpc_url:
            raise LiveBrokerConfigError(
                "Refusing to construct LiveJupiterBroker: SOLANA_RPC_URL is not set."
            )
        if is_public_rpc_url(settings.solana_rpc_url):
            raise LiveBrokerConfigError(
                f"Refusing to construct LiveJupiterBroker: SOLANA_RPC_URL "
                f"({mask_rpc_url(settings.solana_rpc_url)}) is a known PUBLIC Solana RPC endpoint. "
                "Live trading requires a private RPC URL (e.g. Helius free tier: https://www.helius.dev/)."
            )

        self.settings = settings
        self._keypair = keypair or load_keypair_from_env()
        self.public_address = str(self._keypair.pubkey())
        self._rpc = rpc_client or SolanaRpcClient(settings.solana_rpc_url, timeout=settings.http_timeout_seconds)
        self._jupiter = jupiter_client or JupiterClient(
            quote_url=settings.jupiter_quote_url,
            swap_url=settings.jupiter_swap_url,
            timeout=settings.http_timeout_seconds,
        )
        self.slippage_bps = settings.live_slippage_bps
        self.priority_fee: Union[str, int] = settings.live_priority_fee
        self.confirmation_timeout_seconds = settings.live_confirmation_timeout_seconds
        self.wallet_balance_buffer_sol = settings.live_wallet_balance_buffer_sol

        logger.warning(
            "LiveJupiterBroker initialized for wallet %s via %s -- REAL FUNDS, real transactions.",
            self.public_address,
            mask_rpc_url(settings.solana_rpc_url),
        )

    # --- wallet balance (ground truth) --------------------------------------
    def get_wallet_sol_balance(self) -> float:
        """Real, current on-chain SOL balance (never the paper ledger)."""
        lamports = self._rpc.get_balance_lamports(self.public_address)
        return lamports / LAMPORTS_PER_SOL

    def get_wallet_balance_usd(self) -> Tuple[float, float]:
        """Returns (sol_balance, usd_balance) using a fresh SOL/USD price."""
        sol_balance = self.get_wallet_sol_balance()
        sol_price_usd = self._jupiter.get_sol_usd_price()
        return sol_balance, sol_balance * sol_price_usd

    # --- Broker interface ----------------------------------------------------
    def open_position(self, token_address: str, symbol: str, reference_price: float, size_usd: float) -> Fill:
        if size_usd <= 0:
            raise ValueError("size_usd must be positive")

        sol_price_usd = self._jupiter.get_sol_usd_price()
        wallet_lamports = self._rpc.get_balance_lamports(self.public_address)
        wallet_usd = wallet_lamports / LAMPORTS_PER_SOL * sol_price_usd
        buffer_usd = self.wallet_balance_buffer_sol * sol_price_usd
        spendable_usd = wallet_usd - buffer_usd

        if size_usd > spendable_usd:
            raise LiveBrokerInsufficientBalanceError(
                f"Refusing to open a ${size_usd:,.2f} position in {symbol}: real on-chain wallet balance "
                f"is ${wallet_usd:,.2f} ({wallet_lamports:,} lamports), leaving only "
                f"${max(spendable_usd, 0.0):,.2f} spendable after reserving "
                f"{self.wallet_balance_buffer_sol:g} SOL for fees/rent. The paper portfolio's internal "
                "ledger is never trusted for live sizing -- only the real chain balance."
            )

        lamports_to_spend = int((size_usd / sol_price_usd) * LAMPORTS_PER_SOL)
        if lamports_to_spend <= 0:
            raise ValueError("Computed swap amount is zero lamports; size_usd too small")

        quote = self._jupiter.get_quote(
            input_mint=SOL_MINT, output_mint=token_address, amount=lamports_to_spend, slippage_bps=self.slippage_bps
        )

        pre_token = self._rpc.get_token_balance(self.public_address, token_address)

        signature, swap_tx = self._sign_and_submit(quote)
        self._confirm_or_raise(signature)

        post_token = self._rpc.get_token_balance(self.public_address, token_address)
        quantity_atomic = post_token.amount_atomic - pre_token.amount_atomic
        decimals = post_token.decimals

        if quantity_atomic <= 0 or decimals <= 0:
            raise LiveTradeFailedError(
                f"Transaction {signature} was confirmed on-chain but no {symbol} balance increase was "
                f"observed for {self.public_address}. Refusing to record a fill -- check "
                f"https://solscan.io/tx/{signature} and the wallet's token balance manually."
            )

        quantity = quantity_atomic / (10**decimals)
        fee_lamports = BASE_TX_FEE_LAMPORTS + (swap_tx.prioritization_fee_lamports or 0)
        fee_usd = fee_lamports / LAMPORTS_PER_SOL * sol_price_usd
        price = (size_usd - fee_usd) / quantity

        logger.info(
            "LIVE OPEN %s: spent $%.2f (%.6f SOL) for %.6f tokens (tx=%s)",
            symbol,
            size_usd,
            lamports_to_spend / LAMPORTS_PER_SOL,
            quantity,
            signature,
        )
        return Fill(price=price, quantity=quantity, fee_usd=fee_usd, gross_usd=size_usd)

    def close_position(self, token_address: str, symbol: str, quantity: float, reference_price: float) -> Fill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        sol_price_usd = self._jupiter.get_sol_usd_price()
        real_balance = self._rpc.get_token_balance(self.public_address, token_address)

        if real_balance.amount_atomic <= 0:
            raise LiveBrokerInsufficientBalanceError(
                f"Refusing to sell {symbol}: real on-chain balance for this token is zero for "
                f"{self.public_address}. The paper ledger's recorded quantity is never trusted for "
                "live sizing -- only the real chain balance."
            )

        requested_atomic = int(quantity * (10**real_balance.decimals))
        # Ground truth caps the sale: never attempt to sell more than we
        # actually, verifiably hold on-chain right now.
        atomic_to_sell = min(requested_atomic, real_balance.amount_atomic)
        if atomic_to_sell <= 0:
            raise ValueError("Computed sell amount is zero atomic units")

        quote = self._jupiter.get_quote(
            input_mint=token_address, output_mint=SOL_MINT, amount=atomic_to_sell, slippage_bps=self.slippage_bps
        )

        pre_sol_lamports = self._rpc.get_balance_lamports(self.public_address)

        signature, swap_tx = self._sign_and_submit(quote)
        self._confirm_or_raise(signature)

        post_sol_lamports = self._rpc.get_balance_lamports(self.public_address)
        net_lamports_received = post_sol_lamports - pre_sol_lamports

        if net_lamports_received <= 0:
            raise LiveTradeFailedError(
                f"Transaction {signature} was confirmed on-chain but no SOL balance increase was "
                f"observed for {self.public_address}. Refusing to record a fill -- check "
                f"https://solscan.io/tx/{signature} manually."
            )

        fee_lamports = BASE_TX_FEE_LAMPORTS + (swap_tx.prioritization_fee_lamports or 0)
        gross_lamports = net_lamports_received + fee_lamports
        gross_usd = gross_lamports / LAMPORTS_PER_SOL * sol_price_usd
        fee_usd = fee_lamports / LAMPORTS_PER_SOL * sol_price_usd
        quantity_sold = atomic_to_sell / (10**real_balance.decimals)
        price = (gross_usd - fee_usd) / quantity_sold

        logger.info(
            "LIVE CLOSE %s: sold %.6f tokens for $%.2f (%.6f SOL) (tx=%s)",
            symbol,
            quantity_sold,
            gross_usd,
            net_lamports_received / LAMPORTS_PER_SOL,
            signature,
        )
        return Fill(price=price, quantity=quantity_sold, fee_usd=fee_usd, gross_usd=gross_usd)

    def mark_to_market(self, token_address: str, reference_price: float) -> float:
        # Same convention as PaperBroker: marks use fresh public market data
        # as-is; only actual fills apply slippage/fee adjustments.
        return reference_price

    # --- internal: sign + submit + confirm -----------------------------------
    def _sign_and_submit(self, quote: Dict[str, Any]):
        swap_tx = self._jupiter.get_swap_transaction(
            quote, user_public_key=self.public_address, priority_fee=self.priority_fee
        )
        signed_b64 = self._sign_transaction(swap_tx.swap_transaction_b64)
        signature = self._rpc.send_raw_transaction(signed_b64)
        return signature, swap_tx

    def _sign_transaction(self, unsigned_tx_b64: str) -> str:
        raw = base64.b64decode(unsigned_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        signature = self._keypair.sign_message(bytes(tx.message))
        signed_tx = VersionedTransaction.populate(tx.message, [signature])
        return base64.b64encode(bytes(signed_tx)).decode("ascii")

    def _confirm_or_raise(self, signature: str):
        confirmation = self._rpc.confirm_transaction(
            signature, timeout_seconds=self.confirmation_timeout_seconds
        )
        if confirmation.err is not None:
            raise LiveTradeFailedError(f"Transaction {signature} failed on-chain: {confirmation.err}")
        if not confirmation.confirmed:
            raise LiveTradeUnconfirmedError(
                f"Transaction {signature} was not confirmed on-chain within "
                f"{self.confirmation_timeout_seconds:.0f}s. Its outcome is UNKNOWN -- check "
                f"https://solscan.io/tx/{signature} manually before taking any further action. "
                "This bot will not assume a fill happened, and will not blindly resubmit."
            )
        return confirmation
