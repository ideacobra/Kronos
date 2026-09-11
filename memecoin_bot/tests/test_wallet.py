"""Tests for trading/wallet.py: keypair generation/loading.

CRITICAL SAFETY TEST: `test_generation_never_logs_the_secret_key` captures
all stdout/logging during wallet generation and asserts the raw secret
material is never present in it.
"""
from __future__ import annotations

import io
import logging
from contextlib import redirect_stdout

import pytest
from solders.keypair import Keypair

from memecoin_bot.trading.wallet import (
    WalletConfigError,
    generate_keypair,
    keypair_from_base58,
    keypair_to_base58,
    load_keypair_from_env,
    private_key_present,
)


class TestGenerateKeypair:
    def test_returns_a_keypair(self):
        keypair = generate_keypair()
        assert isinstance(keypair, Keypair)

    def test_each_call_is_random(self):
        a = generate_keypair()
        b = generate_keypair()
        assert str(a.pubkey()) != str(b.pubkey())


class TestKeypairBase58RoundTrip:
    def test_round_trips(self):
        keypair = generate_keypair()
        secret_b58 = keypair_to_base58(keypair)
        restored = keypair_from_base58(secret_b58)
        assert str(restored.pubkey()) == str(keypair.pubkey())

    def test_invalid_base58_raises_wallet_config_error(self):
        with pytest.raises(WalletConfigError):
            keypair_from_base58("not-valid-base58-!!!")


class TestPrivateKeyPresent:
    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
        assert private_key_present() is False

    def test_true_when_set(self, monkeypatch):
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", "somevalue")
        assert private_key_present() is True

    def test_false_when_blank(self, monkeypatch):
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", "   ")
        assert private_key_present() is False


class TestLoadKeypairFromEnv:
    def test_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
        with pytest.raises(WalletConfigError):
            load_keypair_from_env()

    def test_loads_valid_key(self, monkeypatch):
        keypair = generate_keypair()
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(keypair))
        loaded = load_keypair_from_env()
        assert str(loaded.pubkey()) == str(keypair.pubkey())

    def test_raises_on_malformed_value(self, monkeypatch):
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", "garbage")
        with pytest.raises(WalletConfigError):
            load_keypair_from_env()


class TestSecretNeverLogged:
    def test_generation_never_logs_the_secret_key(self, caplog):
        """The single most important test in this file: generating a wallet
        and rendering its secret must never leak the raw key material into
        any captured stdout or logging output.
        """
        stdout_capture = io.StringIO()
        with caplog.at_level(logging.DEBUG):
            with redirect_stdout(stdout_capture):
                keypair = generate_keypair()
                secret_b58 = keypair_to_base58(keypair)
                public_address = str(keypair.pubkey())
                # Simulate what cli.py's `wallet new` actually prints.
                print(f"Public address: {public_address}")
                print("Fund this address with exactly the SOL you intend to risk, then never share your .env file.")
                logging.getLogger("memecoin_bot.trading.wallet").info(
                    "Generated a wallet for %s", public_address
                )

        stdout_text = stdout_capture.getvalue()
        log_text = "\n".join(record.getMessage() for record in caplog.records)

        assert secret_b58 not in stdout_text
        assert secret_b58 not in log_text
        # Sanity: the address (non-secret) legitimately appears.
        assert public_address in stdout_text
