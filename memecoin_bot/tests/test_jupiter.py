"""Tests for trading/jupiter.py: JupiterClient quote/swap requests. All HTTP
calls are mocked via `responses` -- no real network access.
"""
from __future__ import annotations

import pytest
import responses

from memecoin_bot.trading.jupiter import (
    SOL_MINT,
    USDC_MINT,
    JupiterApiError,
    JupiterClient,
)

QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
SWAP_URL = "https://api.jup.ag/swap/v1/swap"
MEMECOIN_MINT = "MemeCoinMint11111111111111111111111111111"


def _quote_payload(**overrides):
    payload = {
        "inputMint": SOL_MINT,
        "outputMint": MEMECOIN_MINT,
        "inAmount": "100000000",
        "outAmount": "5000000000",
        "otherAmountThreshold": "4800000000",
        "swapMode": "ExactIn",
        "slippageBps": 400,
        "priceImpactPct": "0.01",
        "routePlan": [],
    }
    payload.update(overrides)
    return payload


class TestGetQuote:
    @responses.activate
    def test_sends_expected_params(self):
        responses.add(responses.GET, QUOTE_URL, json=_quote_payload(), status=200)
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        quote = client.get_quote(input_mint=SOL_MINT, output_mint=MEMECOIN_MINT, amount=100_000_000, slippage_bps=400)

        assert quote["outAmount"] == "5000000000"
        sent_url = responses.calls[0].request.url
        assert "inputMint=" in sent_url
        assert "outputMint=" in sent_url
        assert "amount=100000000" in sent_url
        assert "slippageBps=400" in sent_url

    def test_rejects_non_positive_amount(self):
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        with pytest.raises(ValueError):
            client.get_quote(input_mint=SOL_MINT, output_mint=MEMECOIN_MINT, amount=0, slippage_bps=400)

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(responses.GET, QUOTE_URL, body="bad request", status=400)
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        with pytest.raises(JupiterApiError):
            client.get_quote(input_mint=SOL_MINT, output_mint=MEMECOIN_MINT, amount=100, slippage_bps=400)

    @responses.activate
    def test_raises_on_unexpected_payload(self):
        responses.add(responses.GET, QUOTE_URL, json={"unexpected": True}, status=200)
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        with pytest.raises(JupiterApiError):
            client.get_quote(input_mint=SOL_MINT, output_mint=MEMECOIN_MINT, amount=100, slippage_bps=400)


class TestGetSwapTransaction:
    @responses.activate
    def test_sends_priority_fee_auto_by_default(self):
        responses.add(
            responses.POST,
            SWAP_URL,
            json={"swapTransaction": "base64tx==", "lastValidBlockHeight": 12345, "prioritizationFeeLamports": 5000},
            status=200,
        )
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        result = client.get_swap_transaction(_quote_payload(), user_public_key="Wallet111111111111111111111111111111111")

        assert result.swap_transaction_b64 == "base64tx=="
        assert result.last_valid_block_height == 12345
        assert result.prioritization_fee_lamports == 5000

        import json as _json

        sent_body = _json.loads(responses.calls[0].request.body)
        assert sent_body["prioritizationFeeLamports"] == "auto"
        assert sent_body["userPublicKey"] == "Wallet111111111111111111111111111111111"

    @responses.activate
    def test_sends_fixed_priority_fee_when_given_an_int(self):
        responses.add(
            responses.POST,
            SWAP_URL,
            json={"swapTransaction": "base64tx==", "lastValidBlockHeight": 1, "prioritizationFeeLamports": 20000},
            status=200,
        )
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        client.get_swap_transaction(_quote_payload(), user_public_key="Wallet1", priority_fee=20000)

        import json as _json

        sent_body = _json.loads(responses.calls[0].request.body)
        assert sent_body["prioritizationFeeLamports"] == 20000

    @responses.activate
    def test_raises_on_missing_swap_transaction(self):
        responses.add(responses.POST, SWAP_URL, json={"lastValidBlockHeight": 1}, status=200)
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        with pytest.raises(JupiterApiError):
            client.get_swap_transaction(_quote_payload(), user_public_key="Wallet1")

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(responses.POST, SWAP_URL, body="server error", status=500)
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        with pytest.raises(JupiterApiError):
            client.get_swap_transaction(_quote_payload(), user_public_key="Wallet1")


class TestGetSolUsdPrice:
    @responses.activate
    def test_uses_swap_usd_value_when_present(self):
        responses.add(
            responses.GET,
            QUOTE_URL,
            json=_quote_payload(
                inputMint=SOL_MINT, outputMint=USDC_MINT, outAmount="150000000", swapUsdValue="150.25"
            ),
            status=200,
        )
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        assert client.get_sol_usd_price() == pytest.approx(150.25)

    @responses.activate
    def test_falls_back_to_out_amount_when_swap_usd_value_missing(self):
        responses.add(
            responses.GET,
            QUOTE_URL,
            json=_quote_payload(inputMint=SOL_MINT, outputMint=USDC_MINT, outAmount="150000000"),
            status=200,
        )
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        assert client.get_sol_usd_price() == pytest.approx(150.0)

    @responses.activate
    def test_requests_exactly_one_sol(self):
        responses.add(
            responses.GET,
            QUOTE_URL,
            json=_quote_payload(inputMint=SOL_MINT, outputMint=USDC_MINT, outAmount="100000000"),
            status=200,
        )
        client = JupiterClient(quote_url=QUOTE_URL, swap_url=SWAP_URL)
        client.get_sol_usd_price()
        sent_url = responses.calls[0].request.url
        assert "amount=1000000000" in sent_url
        assert f"inputMint={SOL_MINT}" in sent_url
