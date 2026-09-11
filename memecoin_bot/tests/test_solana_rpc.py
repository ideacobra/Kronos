"""Tests for trading/solana_rpc.py: the minimal Solana JSON-RPC client,
public-RPC detection, and URL masking. All HTTP calls are mocked via
`responses` -- no real network access.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import responses

from memecoin_bot.trading.solana_rpc import (
    BASE_TX_FEE_LAMPORTS,
    ConfirmationResult,
    SolanaRpcClient,
    SolanaRpcError,
    TokenBalance,
    is_public_rpc_url,
    mask_rpc_url,
)

RPC_URL = "https://mainnet.helius-rpc.com/?api-key=super-secret-key-do-not-leak"
OWNER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class TestIsPublicRpcUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.mainnet-beta.solana.com",
            "https://api.devnet.solana.com",
            "https://api.testnet.solana.com",
            "https://solana-api.projectserum.com",
            "https://foo.solana.com/rpc",
        ],
    )
    def test_detects_known_public_hostnames(self, url):
        assert is_public_rpc_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://mainnet.helius-rpc.com/?api-key=abc123",
            "https://solana-mainnet.g.alchemy.com/v2/abc123",
            "https://rpc.ankr.com/solana/abc123",
        ],
    )
    def test_allows_private_provider_hostnames(self, url):
        assert is_public_rpc_url(url) is False

    def test_none_and_empty_are_not_public(self):
        assert is_public_rpc_url(None) is False
        assert is_public_rpc_url("") is False


class TestMaskRpcUrl:
    def test_strips_path_and_query(self):
        masked = mask_rpc_url(RPC_URL)
        assert "super-secret-key-do-not-leak" not in masked
        assert masked == "https://mainnet.helius-rpc.com"

    def test_none_is_not_set(self):
        assert mask_rpc_url(None) == "(not set)"


class TestSolanaRpcClientCall:
    def test_requires_rpc_url(self):
        with pytest.raises(ValueError):
            SolanaRpcClient("")

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(responses.POST, RPC_URL, status=500, body="server error")
        client = SolanaRpcClient(RPC_URL)
        with pytest.raises(SolanaRpcError):
            client.get_balance_lamports(OWNER)

    @responses.activate
    def test_raises_on_rpc_error_payload(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid params"}},
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        with pytest.raises(SolanaRpcError):
            client.get_balance_lamports(OWNER)


class TestGetBalanceLamports:
    @responses.activate
    def test_returns_lamports(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 1_500_000_000}},
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        assert client.get_balance_lamports(OWNER) == 1_500_000_000


class TestGetTokenBalance:
    @responses.activate
    def test_returns_amount_and_decimals(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "context": {"slot": 1},
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "tokenAmount": {
                                                "amount": "5000000",
                                                "decimals": 6,
                                                "uiAmount": 5.0,
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    ],
                },
            },
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        balance = client.get_token_balance(OWNER, MINT)
        assert balance.amount_atomic == 5_000_000
        assert balance.decimals == 6
        assert balance.ui_amount == pytest.approx(5.0)

    @responses.activate
    def test_returns_zero_when_no_account_exists(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": []}},
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        balance = client.get_token_balance(OWNER, MINT)
        assert balance.amount_atomic == 0


class TestGetAllTokenBalances:
    @responses.activate
    def test_filters_out_zero_balances(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "context": {"slot": 1},
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": "MintNonZero111111111111111111111111111111",
                                            "tokenAmount": {"amount": "1000", "decimals": 2, "uiAmount": 10.0},
                                        }
                                    }
                                }
                            }
                        },
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": "MintZero1111111111111111111111111111111111",
                                            "tokenAmount": {"amount": "0", "decimals": 4, "uiAmount": 0.0},
                                        }
                                    }
                                }
                            }
                        },
                    ],
                },
            },
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        balances = client.get_all_token_balances(OWNER)
        assert len(balances) == 1
        assert balances[0]["mint"] == "MintNonZero111111111111111111111111111111"
        assert balances[0]["ui_amount"] == pytest.approx(10.0)


class TestSendRawTransaction:
    @responses.activate
    def test_returns_signature_and_disables_rpc_retries(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "result": "3xSignatureExample"},
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        signature = client.send_raw_transaction("base64tx==")
        assert signature == "3xSignatureExample"

        sent_body = responses.calls[0].request.body
        assert b'"maxRetries": 0' in sent_body or b'"maxRetries":0' in sent_body
        assert b'"encoding": "base64"' in sent_body or b'"encoding":"base64"' in sent_body


class TestGetSignatureStatuses:
    @responses.activate
    def test_returns_status_list(self):
        responses.add(
            responses.POST,
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"context": {"slot": 1}, "value": [{"err": None, "confirmationStatus": "confirmed"}]},
            },
            status=200,
        )
        client = SolanaRpcClient(RPC_URL)
        statuses = client.get_signature_statuses(["sig1"])
        assert statuses[0]["confirmationStatus"] == "confirmed"


class TestConfirmTransaction:
    def test_confirmed_on_first_poll(self):
        client = SolanaRpcClient(RPC_URL)
        with patch.object(
            client, "get_signature_statuses", return_value=[{"err": None, "confirmationStatus": "confirmed"}]
        ):
            result = client.confirm_transaction("sig1", timeout_seconds=5.0, poll_interval_seconds=0.01)
        assert result == ConfirmationResult(confirmed=True, err=None, status="confirmed", timed_out=False)

    def test_errored_transaction_is_not_confirmed(self):
        client = SolanaRpcClient(RPC_URL)
        with patch.object(
            client,
            "get_signature_statuses",
            return_value=[{"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": None}],
        ):
            result = client.confirm_transaction("sig1", timeout_seconds=5.0, poll_interval_seconds=0.01)
        assert result.confirmed is False
        assert result.err is not None
        assert result.timed_out is False

    def test_times_out_without_asserting_success_or_failure(self):
        client = SolanaRpcClient(RPC_URL)
        with patch.object(client, "get_signature_statuses", return_value=[None]), patch(
            "memecoin_bot.trading.solana_rpc.time.sleep"
        ):
            result = client.confirm_transaction("sig1", timeout_seconds=0.05, poll_interval_seconds=0.02)
        assert result.confirmed is False
        assert result.timed_out is True
        assert result.err is None

    def test_never_resubmits_the_transaction(self):
        """confirm_transaction must only ever call getSignatureStatuses --
        never sendTransaction -- regardless of outcome.
        """
        client = SolanaRpcClient(RPC_URL)
        with patch.object(client, "get_signature_statuses", return_value=[None]) as mock_statuses, patch.object(
            client, "send_raw_transaction"
        ) as mock_send, patch("memecoin_bot.trading.solana_rpc.time.sleep"):
            client.confirm_transaction("sig1", timeout_seconds=0.05, poll_interval_seconds=0.02)
        assert mock_statuses.called
        mock_send.assert_not_called()


def test_base_tx_fee_lamports_is_reasonable():
    # Sanity check: a single-signature legacy fee, not zero, not huge.
    assert 0 < BASE_TX_FEE_LAMPORTS < 100_000


def test_token_balance_ui_amount_zero_decimals():
    balance = TokenBalance(amount_atomic=42, decimals=0)
    assert balance.ui_amount == 42
