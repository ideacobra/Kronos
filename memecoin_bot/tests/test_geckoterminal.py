"""Tests for data/geckoterminal.py: OHLCV response parsing, using the
`responses` library to mock HTTP -- no real network calls.
"""
from __future__ import annotations

import pytest
import responses

from memecoin_bot.data.geckoterminal import GeckoTerminalClient

BASE_URL = "https://api.geckoterminal.com/api/v2"

# GeckoTerminal returns candles newest-first: [timestamp, open, high, low, close, volume]
SAMPLE_RESPONSE = {
    "data": {
        "id": "abc",
        "type": "ohlcv_request_response",
        "attributes": {
            "ohlcv_list": [
                [1700000300, 1.05, 1.06, 1.04, 1.055, 100.0],
                [1700000000, 1.00, 1.02, 0.99, 1.010, 50.0],
            ]
        },
    }
}


class TestGeckoTerminalClient:
    @responses.activate
    def test_get_ohlcv_parses_and_sorts_ascending(self):
        client = GeckoTerminalClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/networks/solana/pools/examplepool/ohlcv/minute",
            json=SAMPLE_RESPONSE,
            status=200,
        )
        candles = client.get_ohlcv("examplepool", network="solana", timeframe="minute", aggregate=5, limit=10)

        assert len(candles) == 2
        # GeckoTerminal returns newest-first; client must re-sort ascending.
        assert candles[0].timestamp == 1700000000
        assert candles[1].timestamp == 1700000300
        assert candles[0].close == pytest.approx(1.010)
        assert candles[0].volume == pytest.approx(50.0)

    @responses.activate
    def test_get_ohlcv_passes_query_params(self):
        client = GeckoTerminalClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/networks/solana/pools/examplepool/ohlcv/hour",
            json=SAMPLE_RESPONSE,
            status=200,
        )
        client.get_ohlcv("examplepool", timeframe="hour", aggregate=4, limit=50)
        request_url = responses.calls[0].request.url
        assert "aggregate=4" in request_url
        assert "limit=50" in request_url

    def test_invalid_timeframe_raises(self):
        client = GeckoTerminalClient(base_url=BASE_URL)
        with pytest.raises(ValueError):
            client.get_ohlcv("examplepool", timeframe="fortnight")

    @responses.activate
    def test_malformed_rows_are_skipped(self):
        client = GeckoTerminalClient(base_url=BASE_URL)
        bad_response = {
            "data": {
                "attributes": {
                    "ohlcv_list": [
                        [1700000000, 1.0, 1.02, 0.99, 1.01, 50.0],
                        ["not", "a", "valid", "row"],
                        [1700000300],  # too short
                    ]
                }
            }
        }
        responses.add(
            responses.GET,
            f"{BASE_URL}/networks/solana/pools/examplepool/ohlcv/minute",
            json=bad_response,
            status=200,
        )
        candles = client.get_ohlcv("examplepool")
        assert len(candles) == 1
        assert candles[0].timestamp == 1700000000

    @responses.activate
    def test_empty_response_returns_empty_list(self):
        client = GeckoTerminalClient(base_url=BASE_URL)
        responses.add(
            responses.GET,
            f"{BASE_URL}/networks/solana/pools/examplepool/ohlcv/minute",
            json={"data": {"attributes": {"ohlcv_list": []}}},
            status=200,
        )
        assert client.get_ohlcv("examplepool") == []
