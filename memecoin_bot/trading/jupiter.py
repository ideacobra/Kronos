"""Client for Jupiter's Swap API (quote + swap transaction building).

Only handles the read-only "get a quote" and "build an unsigned swap
transaction" steps -- signing and submission are the responsibility of
`trading/live_broker.py`, which owns the private key. This module never
sees, needs, or touches a private key.

Endpoints default to Jupiter's public `api.jup.ag/swap/v1` base (no API key
required for the low-volume usage this bot needs); both are overridable via
Settings/env vars if Jupiter's URLs change or a user has a paid API key.
See https://dev.jup.ag/docs/swap-api/ for full API docs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

import requests

# Wrapped SOL mint address -- used as the input/output mint whenever the bot
# is trading against native SOL.
SOL_MINT = "So11111111111111111111111111111111111111112"
# USDC mint -- used only as a stable reference asset to derive a SOL/USD
# price via a quote; never traded directly by this bot.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

DEFAULT_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
DEFAULT_SWAP_URL = "https://api.jup.ag/swap/v1/swap"


class JupiterApiError(RuntimeError):
    """Raised for any non-2xx response or unexpected payload from Jupiter."""


@dataclass
class SwapTransaction:
    """An unsigned swap transaction returned by Jupiter's `/swap` endpoint."""

    swap_transaction_b64: str
    last_valid_block_height: int
    prioritization_fee_lamports: Optional[int]


class JupiterClient:
    def __init__(
        self,
        quote_url: str = DEFAULT_QUOTE_URL,
        swap_url: str = DEFAULT_SWAP_URL,
        timeout: float = 20.0,
        api_key: Optional[str] = None,
    ):
        self.quote_url = quote_url
        self.swap_url = swap_url
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int,
    ) -> Dict[str, Any]:
        """GET a swap quote. Read-only; moves no funds, requires no signing."""
        if amount <= 0:
            raise ValueError("amount must be a positive integer (atomic units)")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
            "slippageBps": int(slippage_bps),
            "swapMode": "ExactIn",
        }
        try:
            response = requests.get(
                self.quote_url, params=params, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise JupiterApiError(f"Transport error calling Jupiter quote endpoint: {exc}") from exc

        if response.status_code != 200:
            raise JupiterApiError(f"Jupiter quote HTTP {response.status_code}: {response.text[:500]}")

        quote = response.json()
        if "outAmount" not in quote:
            raise JupiterApiError(f"Unexpected Jupiter quote response (no outAmount): {quote}")
        return quote

    def get_swap_transaction(
        self,
        quote: Dict[str, Any],
        user_public_key: str,
        priority_fee: Union[str, int] = "auto",
    ) -> SwapTransaction:
        """POST a quote + wallet pubkey, get back an unsigned swap transaction.

        `priority_fee`: "auto" uses Jupiter's recommended/estimated priority
        fee (the API default); an int pins an exact lamports amount instead.
        """
        prioritization_fee: Union[str, int] = "auto" if priority_fee == "auto" else int(priority_fee)
        body = {
            "userPublicKey": user_public_key,
            "quoteResponse": quote,
            "prioritizationFeeLamports": prioritization_fee,
            "dynamicComputeUnitLimit": True,
        }
        try:
            response = requests.post(self.swap_url, json=body, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise JupiterApiError(f"Transport error calling Jupiter swap endpoint: {exc}") from exc

        if response.status_code != 200:
            raise JupiterApiError(f"Jupiter swap HTTP {response.status_code}: {response.text[:500]}")

        payload = response.json()
        if "swapTransaction" not in payload:
            raise JupiterApiError(f"Unexpected Jupiter swap response (no swapTransaction): {payload}")

        return SwapTransaction(
            swap_transaction_b64=payload["swapTransaction"],
            last_valid_block_height=int(payload["lastValidBlockHeight"]),
            prioritization_fee_lamports=(
                int(payload["prioritizationFeeLamports"])
                if payload.get("prioritizationFeeLamports") is not None
                else None
            ),
        )

    def get_sol_usd_price(self) -> float:
        """Derive a SOL/USD price via a 1 SOL -> USDC quote.

        Uses the quote's `swapUsdValue` field when present (Jupiter's own
        USD valuation of the input amount), falling back to computing it
        from `outAmount` (USDC has 6 decimals) if that field is absent.
        """
        quote = self.get_quote(
            input_mint=SOL_MINT, output_mint=USDC_MINT, amount=1_000_000_000, slippage_bps=50
        )
        swap_usd_value = quote.get("swapUsdValue")
        if swap_usd_value:
            try:
                return float(swap_usd_value)
            except (TypeError, ValueError):
                pass
        return int(quote["outAmount"]) / (10**USDC_DECIMALS)
