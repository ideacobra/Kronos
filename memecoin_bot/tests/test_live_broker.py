"""Tests for trading/live_broker.py: LiveJupiterBroker.

All Jupiter HTTP calls and all Solana RPC calls are mocked (injected
directly as MagicMocks) -- this suite never touches the network, never
uses a real funded wallet, and never submits a real transaction.
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from memecoin_bot.config import Settings
from memecoin_bot.trading.jupiter import SOL_MINT, JupiterClient, SwapTransaction
from memecoin_bot.trading.live_broker import (
    LiveBrokerConfigError,
    LiveBrokerInsufficientBalanceError,
    LiveJupiterBroker,
    LiveTradeFailedError,
    LiveTradeUnconfirmedError,
)
from memecoin_bot.trading.solana_rpc import ConfirmationResult, SolanaRpcClient, TokenBalance
from memecoin_bot.trading.wallet import keypair_to_base58

MEMECOIN_MINT = "MemeCoinMint11111111111111111111111111111"
LAMPORTS_PER_SOL = 1_000_000_000


def _settings(**overrides) -> Settings:
    defaults = dict(
        enable_live_trading=True,
        solana_private_key_present=True,
        solana_rpc_url="https://mainnet.helius-rpc.com/?api-key=test-key",
        live_slippage_bps=400,
        live_priority_fee="auto",
        live_confirmation_timeout_seconds=5.0,
        live_wallet_balance_buffer_sol=0.01,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _unsigned_tx_b64(fee_payer) -> str:
    """A minimal, real, well-formed unsigned VersionedTransaction, with
    `fee_payer` as the required signer -- stands in for what Jupiter's
    `/swap` endpoint would actually return, so `_sign_transaction` can sign
    and serialize it exactly as it would in production.
    """
    to_pubkey = Keypair().pubkey()
    ix = transfer(TransferParams(from_pubkey=fee_payer, to_pubkey=to_pubkey, lamports=1_000))
    message = MessageV0.try_compile(fee_payer, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode("ascii")


def _make_broker(**settings_overrides):
    settings = _settings(**settings_overrides)
    keypair = Keypair()
    rpc = MagicMock()
    jupiter = MagicMock()
    broker = LiveJupiterBroker(settings, rpc_client=rpc, jupiter_client=jupiter, keypair=keypair)
    return broker, rpc, jupiter, keypair


class TestConstructionSafetyGates:
    def test_refuses_when_live_trading_disabled(self):
        with pytest.raises(LiveBrokerConfigError):
            LiveJupiterBroker(_settings(enable_live_trading=False), keypair=Keypair())

    def test_refuses_when_rpc_url_missing(self):
        with pytest.raises(LiveBrokerConfigError):
            LiveJupiterBroker(_settings(solana_rpc_url=None), keypair=Keypair())

    @pytest.mark.parametrize(
        "public_url",
        [
            "https://api.mainnet-beta.solana.com",
            "https://api.devnet.solana.com",
        ],
    )
    def test_refuses_when_rpc_url_is_public(self, public_url):
        with pytest.raises(LiveBrokerConfigError):
            LiveJupiterBroker(_settings(solana_rpc_url=public_url), keypair=Keypair())

    def test_succeeds_with_valid_config(self):
        broker = LiveJupiterBroker(
            _settings(), keypair=Keypair(), rpc_client=MagicMock(), jupiter_client=MagicMock()
        )
        assert broker.public_address

    def test_constructs_real_clients_when_not_injected(self, monkeypatch):
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(Keypair()))
        broker = LiveJupiterBroker(_settings())
        assert isinstance(broker._rpc, SolanaRpcClient)
        assert isinstance(broker._jupiter, JupiterClient)


class TestGetWalletBalanceUsd:
    def test_combines_sol_balance_and_price(self):
        broker, rpc, jupiter, _ = _make_broker()
        rpc.get_balance_lamports.return_value = int(2.0 * LAMPORTS_PER_SOL)
        jupiter.get_sol_usd_price.return_value = 150.0

        sol_balance, usd_balance = broker.get_wallet_balance_usd()

        assert sol_balance == pytest.approx(2.0)
        assert usd_balance == pytest.approx(300.0)


class TestOpenPosition:
    def test_rejects_non_positive_size(self):
        broker, *_ = _make_broker()
        with pytest.raises(ValueError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=0.0)

    def test_refuses_to_size_beyond_real_onchain_balance(self):
        broker, rpc, jupiter, _ = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        # ~0.01 SOL == ~$1.50 real balance; requesting a $5 trade must be refused.
        rpc.get_balance_lamports.return_value = int(0.01 * LAMPORTS_PER_SOL)

        with pytest.raises(LiveBrokerInsufficientBalanceError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=5.0)

        jupiter.get_quote.assert_not_called()

    def test_never_trusts_paper_ledger_size_over_real_balance(self):
        """Even if the caller (paper-ledger-derived risk sizing) asks for a
        trade the real wallet cannot afford, the broker itself must refuse.
        """
        broker, rpc, jupiter, _ = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_balance_lamports.return_value = 0

        with pytest.raises(LiveBrokerInsufficientBalanceError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=1.0)

    def test_passes_slippage_bps_and_priority_fee_to_jupiter(self):
        broker, rpc, jupiter, keypair = _make_broker(live_slippage_bps=350, live_priority_fee="auto")
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_balance_lamports.return_value = int(1.0 * LAMPORTS_PER_SOL)
        jupiter.get_quote.return_value = {"outAmount": "5000000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=5000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=True, err=None, status="confirmed", timed_out=False
        )
        rpc.get_token_balance.side_effect = [
            TokenBalance(amount_atomic=0, decimals=0),
            TokenBalance(amount_atomic=5_000_000_000, decimals=6),
        ]

        fill = broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=5.0)

        jupiter.get_quote.assert_called_once()
        quote_kwargs = jupiter.get_quote.call_args.kwargs
        assert quote_kwargs["slippage_bps"] == 350
        assert quote_kwargs["input_mint"] == SOL_MINT
        assert quote_kwargs["output_mint"] == MEMECOIN_MINT
        assert quote_kwargs["amount"] == int((5.0 / 150.0) * LAMPORTS_PER_SOL)

        jupiter.get_swap_transaction.assert_called_once()
        assert jupiter.get_swap_transaction.call_args.kwargs["priority_fee"] == "auto"
        assert jupiter.get_swap_transaction.call_args.kwargs["user_public_key"] == broker.public_address

        assert fill.quantity == pytest.approx(5000.0)
        assert fill.gross_usd == pytest.approx(5.0)
        assert fill.fee_usd > 0

    def test_never_records_a_fill_when_unconfirmed(self):
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_balance_lamports.return_value = int(1.0 * LAMPORTS_PER_SOL)
        jupiter.get_quote.return_value = {"outAmount": "5000000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=5000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=False, err=None, status=None, timed_out=True
        )

        with pytest.raises(LiveTradeUnconfirmedError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=5.0)

        # Exactly one submission -- never blindly resubmitted.
        rpc.send_raw_transaction.assert_called_once()

    def test_never_records_a_fill_when_transaction_fails_onchain(self):
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_balance_lamports.return_value = int(1.0 * LAMPORTS_PER_SOL)
        jupiter.get_quote.return_value = {"outAmount": "5000000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=5000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=False, err={"InstructionError": [0, "Custom"]}, status="errored", timed_out=False
        )

        with pytest.raises(LiveTradeFailedError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=5.0)

        rpc.send_raw_transaction.assert_called_once()

    def test_never_records_a_fill_when_no_balance_increase_observed(self):
        """Confirmed on-chain, but no token balance delta -- must not guess."""
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_balance_lamports.return_value = int(1.0 * LAMPORTS_PER_SOL)
        jupiter.get_quote.return_value = {"outAmount": "5000000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=5000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=True, err=None, status="confirmed", timed_out=False
        )
        rpc.get_token_balance.side_effect = [
            TokenBalance(amount_atomic=0, decimals=0),
            TokenBalance(amount_atomic=0, decimals=0),
        ]

        with pytest.raises(LiveTradeFailedError):
            broker.open_position(MEMECOIN_MINT, "FOO", reference_price=0.001, size_usd=5.0)


class TestClosePosition:
    def test_rejects_non_positive_quantity(self):
        broker, *_ = _make_broker()
        with pytest.raises(ValueError):
            broker.close_position(MEMECOIN_MINT, "FOO", quantity=0.0, reference_price=0.001)

    def test_refuses_when_real_onchain_balance_is_zero(self):
        broker, rpc, jupiter, _ = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_token_balance.return_value = TokenBalance(amount_atomic=0, decimals=6)

        with pytest.raises(LiveBrokerInsufficientBalanceError):
            broker.close_position(MEMECOIN_MINT, "FOO", quantity=100.0, reference_price=0.001)

        jupiter.get_quote.assert_not_called()

    def test_caps_sell_amount_at_real_onchain_balance(self):
        """The paper ledger may believe we hold more than we really do (or
        vice versa) -- the real balance is always the hard cap.
        """
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_token_balance.return_value = TokenBalance(amount_atomic=1_000_000, decimals=6)  # 1.0 real token
        jupiter.get_quote.return_value = {"outAmount": "1000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=1000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=True, err=None, status="confirmed", timed_out=False
        )
        rpc.get_balance_lamports.side_effect = [1_000_000_000, 1_000_995_000]

        # Ledger believes we hold 100 tokens; real balance is only 1.0.
        fill = broker.close_position(MEMECOIN_MINT, "FOO", quantity=100.0, reference_price=0.001)

        quote_kwargs = jupiter.get_quote.call_args.kwargs
        assert quote_kwargs["amount"] == 1_000_000  # capped, not 100 tokens' atomic equivalent
        assert fill.quantity == pytest.approx(1.0)

    def test_passes_slippage_bps_to_jupiter(self):
        broker, rpc, jupiter, keypair = _make_broker(live_slippage_bps=250)
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_token_balance.return_value = TokenBalance(amount_atomic=1_000_000, decimals=6)
        jupiter.get_quote.return_value = {"outAmount": "1000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=1000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=True, err=None, status="confirmed", timed_out=False
        )
        rpc.get_balance_lamports.side_effect = [1_000_000_000, 1_000_995_000]

        broker.close_position(MEMECOIN_MINT, "FOO", quantity=1.0, reference_price=0.001)

        assert jupiter.get_quote.call_args.kwargs["slippage_bps"] == 250

    def test_never_records_a_fill_when_unconfirmed(self):
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_token_balance.return_value = TokenBalance(amount_atomic=1_000_000, decimals=6)
        jupiter.get_quote.return_value = {"outAmount": "1000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=1000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=False, err=None, status=None, timed_out=True
        )

        with pytest.raises(LiveTradeUnconfirmedError):
            broker.close_position(MEMECOIN_MINT, "FOO", quantity=1.0, reference_price=0.001)

        rpc.send_raw_transaction.assert_called_once()

    def test_never_records_a_fill_when_no_sol_increase_observed(self):
        broker, rpc, jupiter, keypair = _make_broker()
        jupiter.get_sol_usd_price.return_value = 150.0
        rpc.get_token_balance.return_value = TokenBalance(amount_atomic=1_000_000, decimals=6)
        jupiter.get_quote.return_value = {"outAmount": "1000000"}
        jupiter.get_swap_transaction.return_value = SwapTransaction(
            swap_transaction_b64=_unsigned_tx_b64(keypair.pubkey()),
            last_valid_block_height=1000,
            prioritization_fee_lamports=1000,
        )
        rpc.send_raw_transaction.return_value = "sig1"
        rpc.confirm_transaction.return_value = ConfirmationResult(
            confirmed=True, err=None, status="confirmed", timed_out=False
        )
        rpc.get_balance_lamports.side_effect = [1_000_000_000, 1_000_000_000]  # no change

        with pytest.raises(LiveTradeFailedError):
            broker.close_position(MEMECOIN_MINT, "FOO", quantity=1.0, reference_price=0.001)


class TestMarkToMarket:
    def test_returns_reference_price_unchanged(self):
        broker, *_ = _make_broker()
        assert broker.mark_to_market(MEMECOIN_MINT, 1.2345) == pytest.approx(1.2345)
