"""Solana keypair generation and loading.

SAFETY: functions here that touch a real secret key never log or print it.
`generate_keypair()` / `keypair_to_base58()` are used exactly once by
`cli.py wallet new` to create a brand-new wallet and immediately persist its
secret into the user's local `.env` file -- the secret is never returned to
any caller that might print it, and it is never written anywhere except
directly into that file (see `memecoin_bot/envfile.py`).

`load_keypair_from_env()` reads `SOLANA_PRIVATE_KEY` directly from
`os.environ` rather than going through `config.Settings`, so the raw key
material never becomes a field on a dataclass instance that could be
accidentally logged/printed/repr'd elsewhere in the codebase.
"""
from __future__ import annotations

import os
from typing import Optional

import base58
from solders.keypair import Keypair

SOLANA_PRIVATE_KEY_ENV_VAR = "SOLANA_PRIVATE_KEY"


class WalletConfigError(RuntimeError):
    """Raised when the configured wallet secret is missing or malformed."""


def generate_keypair() -> Keypair:
    """Generate a brand-new random Solana keypair (ed25519)."""
    return Keypair()


def keypair_to_base58(keypair: Keypair) -> str:
    """Base58-encode a keypair's 64-byte secret key (the common wallet
    export format, e.g. compatible with Phantom/Solflare "export private
    key"). Caller is responsible for never printing/logging the result.
    """
    return base58.b58encode(bytes(keypair)).decode("ascii")


def keypair_from_base58(secret_b58: str) -> Keypair:
    """Inverse of `keypair_to_base58`. Raises WalletConfigError on malformed input."""
    try:
        return Keypair.from_base58_string(secret_b58.strip())
    except Exception as exc:  # noqa: BLE001 - normalize any parse error
        raise WalletConfigError(f"SOLANA_PRIVATE_KEY is not a valid base58-encoded keypair: {exc}") from exc


def private_key_present(env_var: str = SOLANA_PRIVATE_KEY_ENV_VAR) -> bool:
    """Existence check only -- never returns or stores the value itself."""
    value = os.environ.get(env_var)
    return bool(value and value.strip())


def load_keypair_from_env(env_var: str = SOLANA_PRIVATE_KEY_ENV_VAR) -> Keypair:
    """Load the wallet keypair from an environment variable.

    Raises WalletConfigError with a clear (secret-free) message if unset or
    invalid. Never logs the raw value.
    """
    raw: Optional[str] = os.environ.get(env_var)
    if not raw or not raw.strip():
        raise WalletConfigError(
            f"{env_var} is not set. Run `python -m memecoin_bot.cli wallet new` to generate a wallet first."
        )
    return keypair_from_base58(raw)
