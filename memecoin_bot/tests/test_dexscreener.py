"""Tests for data/dexscreener.py: response parsing and endpoint wrappers,
using the `responses` library to mock HTTP -- no real network calls.
"""
from __future__ import annotations

import pytest
import responses

from memecoin_bot.data.dexscreener import DexScreenerClient, parse_pair
from memecoin_bot.data.types import TokenPair

BASE_URL = "https://api.dexscreener.com"

# A realistic sample pair payload, shaped like a real DexScreener response.
SAMPLE_PAIR = {
    "chainId": "solana",
    "dexId": "pumpswap",
    "url": "https://dexscreener.com/solana/examplepair",
    "pairAddress": "ExamplePairAddress111",
    "baseToken": {
        "address": "ExampleBaseTokenAddress222",
        "name": "Example Token",
        "symbol": "EXPL",
    },
    "quoteToken": {
        "address": "So11111111111111111111111111111111111111112",
        "name": "Wrapped SOL",
        "symbol": "SOL",
    },
    "priceNative": "0.0000006465",
    "priceUsd": "0.00006560",
    "txns": {
        "m5": {"buys": 5, "sells": 2},
        "h1": {"buys": 22, "sells": 23},
        "h6": {"buys": 169, "sells": 159},
        "h24": {"buys": 1333, "sells": 1039},
    },
    "volume": {"h24": 95130.51, "h6": 12093.48, "h1": 1470.86, "m5": 64.89},
    "priceChange": {"m5": -1.16, "h1": -0.19, "h6": 3.06, "h24": 60.8},
    "liquidity": {"usd": 26013, "base": 214674925, "quote": 117.5612},
    "fdv": 40530,
    "marketCap": 40530,
    "pairCreatedAt": 1700000000000,
}


class TestParsePair:
    def test_parses_all_fields(self):
        pair = parse_pair(SAMPLE_PAIR)
        assert isinstance(pair, TokenPair)
        assert pair.chain_id == "solana"
        assert pair.dex_id == "pumpswap"
        assert pair.base_token_address == "ExampleBaseTokenAddress222"
        assert pair.base_token_symbol == "EXPL"
        assert pair.price_usd == pytest.approx(0.00006560)
        assert pair.liquidity_usd == pytest.approx(26013)
        assert pair.txns_m5.buys == 5
        assert pair.txns_m5.sells == 2
        assert pair.txns_h1.buy_ratio == pytest.approx(22 / 45)
        assert pair.volume_h1 == pytest.approx(1470.86)
        assert pair.price_change_h24 == pytest.approx(60.8)
        assert pair.pair_created_at_ms == 1700000000000
        assert pair.fdv == pytest.approx(40530)

    def test_returns_none_for_missing_price(self):
        broken = dict(SAMPLE_PAIR)
        broken.pop("priceUsd")
        assert parse_pair(broken) is None

    def test_returns_none_for_missing_base_address(self):
        broken = {**SAMPLE_PAIR, "baseToken": {}}
        assert parse_pair(broken) is None

    def test_boost_amount_defaults_to_zero(self):
        pair = parse_pair(SAMPLE_PAIR)
        assert pair.boost_amount == 0.0

    def test_boost_amount_passthrough(self):
        pair = parse_pair(SAMPLE_PAIR, boost_amount=250.0)
        assert pair.boost_amount == 250.0

    def test_tolerates_missing_optional_sections(self):
        minimal = {
            "chainId": "solana",
            "baseToken": {"address": "addr", "symbol": "X"},
            "priceUsd": "1.23",
        }
        pair = parse_pair(minimal)
        assert pair is not None
        assert pair.liquidity_usd == 0.0
        assert pair.txns_m5.buys == 0
        assert pair.fdv is None


class TestDexScreenerClient:
    @responses.activate
    def test_get_pairs_for_tokens(self):
        client = DexScreenerClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/tokens/v1/solana/ExampleBaseTokenAddress222",
            json=[SAMPLE_PAIR],
            status=200,
        )
        pairs = client.get_pairs_for_tokens(["ExampleBaseTokenAddress222"], chain_id="solana")
        assert len(pairs) == 1
        assert pairs[0].base_token_symbol == "EXPL"

    @responses.activate
    def test_search_pairs(self):
        client = DexScreenerClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/latest/dex/search",
            json={"schemaVersion": "1.0.0", "pairs": [SAMPLE_PAIR]},
            status=200,
        )
        pairs = client.search_pairs("EXPL")
        assert len(pairs) == 1
        assert pairs[0].base_token_symbol == "EXPL"

    @responses.activate
    def test_search_pairs_returns_empty_on_no_matches(self):
        client = DexScreenerClient(base_url=BASE_URL)
        responses.add(
            responses.GET, f"{BASE_URL}/latest/dex/search", json={"schemaVersion": "1.0.0"}, status=200
        )
        assert client.search_pairs("nonexistenttoken") == []

    @responses.activate
    def test_discover_candidate_addresses_filters_by_chain_and_takes_max(self):
        client = DexScreenerClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/token-boosts/latest/v1",
            json=[
                {"chainId": "solana", "tokenAddress": "sol_token", "amount": 100},
                {"chainId": "ethereum", "tokenAddress": "eth_token", "amount": 500},
            ],
            status=200,
        )
        responses.add(
            responses.GET,
            f"{BASE_URL}/token-boosts/top/v1",
            json=[{"chainId": "solana", "tokenAddress": "sol_token", "amount": 300}],
            status=200,
        )
        boosts = client.discover_candidate_addresses(chain_id="solana")
        assert boosts == {"sol_token": 300}  # max of the two observed amounts, ethereum excluded

    @responses.activate
    def test_get_pairs_for_tokens_chunks_large_batches(self):
        client = DexScreenerClient(base_url=BASE_URL)
        addresses = [f"addr{i}" for i in range(35)]
        first_batch = ",".join(addresses[:30])
        second_batch = ",".join(addresses[30:])
        responses.add(
            responses.GET, f"{BASE_URL}/tokens/v1/solana/{first_batch}", json=[SAMPLE_PAIR], status=200
        )
        responses.add(responses.GET, f"{BASE_URL}/tokens/v1/solana/{second_batch}", json=[], status=200)

        pairs = client.get_pairs_for_tokens(addresses, chain_id="solana")
        assert len(pairs) == 1
        assert len(responses.calls) == 2

    @responses.activate
    def test_get_pairs_for_tokens_dedupes_addresses(self):
        client = DexScreenerClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/tokens/v1/solana/ExampleBaseTokenAddress222",
            json=[SAMPLE_PAIR],
            status=200,
        )
        pairs = client.get_pairs_for_tokens(
            ["ExampleBaseTokenAddress222", "ExampleBaseTokenAddress222"], chain_id="solana"
        )
        assert len(pairs) == 1
        assert len(responses.calls) == 1
