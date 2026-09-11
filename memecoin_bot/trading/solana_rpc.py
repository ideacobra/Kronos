"""Minimal Solana JSON-RPC client used by live trading (`live_broker.py`)
and the `wallet balance` CLI command.

Deliberately avoids the heavier `solana-py` package -- only the handful of
RPC methods actually needed here are implemented, using plain `requests`
calls against the standard Solana JSON-RPC 2.0 interface. This keeps the
dependency footprint small and every method easy to unit test with mocked
HTTP responses (no live network access required for tests).

SAFETY NOTES:
  - `send_raw_transaction` submits a transaction exactly once per call and
    disables the RPC node's own internal rebroadcast (`maxRetries=0`).
    Callers are responsible for deciding whether/when to resubmit; this
    module never silently retries a submission, which could otherwise
    double-submit a transaction.
  - `confirm_transaction` only ever *observes* a transaction's status via
    `getSignatureStatuses`; it never assumes success without an explicit
    on-chain confirmation, and never re-sends anything itself.
  - `mask_rpc_url` / `is_public_rpc_url` exist so a private RPC URL (which
    typically embeds an API key as a query parameter, e.g. Helius) is never
    logged or printed in full, and so live trading can refuse to run
    against a known public/foundation-run RPC endpoint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

LAMPORTS_PER_SOL = 1_000_000_000
BASE_TX_FEE_LAMPORTS = 5_000  # a single-signature legacy/v0 tx base fee

# Well-known public/foundation-run Solana RPC endpoints. Live trading refuses
# to run against any of these -- they are rate-limited, unreliable for
# submitting real transactions, and never intended for production use.
PUBLIC_RPC_HOSTNAMES = frozenset(
    {
        "api.mainnet-beta.solana.com",
        "api.devnet.solana.com",
        "api.testnet.solana.com",
        "solana-api.projectserum.com",
    }
)

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def is_public_rpc_url(url: Optional[str]) -> bool:
    """True if `url` points at a known public/foundation-run Solana RPC.

    Live trading must refuse to run against these (see README "Going Live").
    Any hostname directly under the `solana.com` domain is treated as public
    too, since that domain is Solana Foundation-owned.
    """
    if not url:
        return False
    hostname = (urlparse(url).hostname or "").lower()
    if not hostname:
        return False
    return hostname in PUBLIC_RPC_HOSTNAMES or hostname.endswith(".solana.com")


def mask_rpc_url(url: Optional[str]) -> str:
    """Return `scheme://hostname` only -- never the path/query, which for
    providers like Helius embeds a secret API key. Safe to log/print.
    """
    if not url:
        return "(not set)"
    parsed = urlparse(url)
    if not parsed.hostname:
        return "(invalid URL)"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class SolanaRpcError(RuntimeError):
    """Raised for any RPC-level error response or transport failure."""


@dataclass
class TokenBalance:
    """An SPL token balance for a single (owner, mint) pair."""

    amount_atomic: int
    decimals: int

    @property
    def ui_amount(self) -> float:
        return self.amount_atomic / (10**self.decimals) if self.decimals >= 0 else float(self.amount_atomic)


@dataclass
class ConfirmationResult:
    """Outcome of polling `getSignatureStatuses` for a submitted transaction."""

    confirmed: bool
    err: Optional[Any]
    status: Optional[str]
    timed_out: bool


class SolanaRpcClient:
    """Thin synchronous JSON-RPC 2.0 client for the small set of methods
    LiveJupiterBroker and `wallet balance` need.
    """

    def __init__(self, rpc_url: str, timeout: float = 20.0):
        if not rpc_url:
            raise ValueError("rpc_url must be set")
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._request_id = 0

    def _call(self, method: str, params: List[Any]) -> Any:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        try:
            response = requests.post(self.rpc_url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SolanaRpcError(f"RPC transport error calling {method}: {exc}") from exc

        if response.status_code != 200:
            raise SolanaRpcError(f"RPC HTTP {response.status_code} calling {method}: {response.text[:500]}")

        body = response.json()
        if "error" in body:
            raise SolanaRpcError(f"RPC error calling {method}: {body['error']}")
        return body.get("result")

    # --- balances -----------------------------------------------------------
    def get_balance_lamports(self, pubkey: str) -> int:
        result = self._call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(result["value"])

    def get_token_balance(self, owner_pubkey: str, mint: str) -> TokenBalance:
        """Real on-chain SPL token balance for `mint` held by `owner_pubkey`.

        Returns amount_atomic=0, decimals=0 if the owner has no token
        account for this mint yet (e.g. they have never received it).
        """
        result = self._call(
            "getTokenAccountsByOwner",
            [owner_pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        accounts = result.get("value", [])
        total_atomic = 0
        decimals = 0
        for account in accounts:
            parsed = account["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total_atomic += int(parsed["amount"])
            decimals = int(parsed["decimals"])
        return TokenBalance(amount_atomic=total_atomic, decimals=decimals)

    def get_all_token_balances(self, owner_pubkey: str) -> List[Dict[str, Any]]:
        """All nonzero SPL token balances for `owner_pubkey` (for `wallet balance`)."""
        result = self._call(
            "getTokenAccountsByOwner",
            [owner_pubkey, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        balances: List[Dict[str, Any]] = []
        for account in result.get("value", []):
            info = account["account"]["data"]["parsed"]["info"]
            token_amount = info["tokenAmount"]
            amount_atomic = int(token_amount["amount"])
            if amount_atomic <= 0:
                continue
            decimals = int(token_amount["decimals"])
            balances.append(
                {
                    "mint": info["mint"],
                    "amount_atomic": amount_atomic,
                    "decimals": decimals,
                    "ui_amount": amount_atomic / (10**decimals) if decimals else float(amount_atomic),
                }
            )
        return balances

    # --- transaction submission + confirmation -------------------------------
    def send_raw_transaction(self, signed_tx_b64: str) -> str:
        """Submit a base64-encoded, already-signed transaction exactly once.

        `maxRetries=0` disables the RPC node's own rebroadcast loop -- retry
        policy is the caller's responsibility, and this method itself never
        retries, so it can never cause a double-submit on its own.
        """
        result = self._call(
            "sendTransaction",
            [
                signed_tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 0,
                },
            ],
        )
        return str(result)

    def get_signature_statuses(self, signatures: List[str]) -> List[Optional[Dict[str, Any]]]:
        result = self._call("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}])
        return result["value"]

    def confirm_transaction(
        self,
        signature: str,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 2.0,
    ) -> ConfirmationResult:
        """Poll for on-chain confirmation. Never resubmits -- only observes.

        Returns as soon as the signature is seen with an error (failed) or a
        confirmed/finalized status. If neither happens before the timeout,
        returns `timed_out=True` with `confirmed=False`; the caller must
        treat this as "unknown outcome", not "failed", and must not blindly
        resend (the original transaction may still land).
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            statuses = self.get_signature_statuses([signature])
            status = statuses[0] if statuses else None
            if status is not None:
                if status.get("err") is not None:
                    return ConfirmationResult(confirmed=False, err=status["err"], status="errored", timed_out=False)
                confirmation_status = status.get("confirmationStatus")
                if confirmation_status in ("confirmed", "finalized"):
                    return ConfirmationResult(
                        confirmed=True, err=None, status=confirmation_status, timed_out=False
                    )
            if time.monotonic() >= deadline:
                return ConfirmationResult(confirmed=False, err=None, status=None, timed_out=True)
            time.sleep(poll_interval_seconds)
