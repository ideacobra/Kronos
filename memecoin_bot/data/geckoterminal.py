"""Client for the public, unauthenticated GeckoTerminal API (v2).

Used to fetch OHLCV candles for a specific Solana pool so we can build a
pandas DataFrame in the shape Kronos expects (open/high/low/close/volume).
No API key is required. See https://www.geckoterminal.com/dex-api.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from memecoin_bot.data.types import Candle

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = {"day", "hour", "minute"}


class GeckoTerminalClient:
    """Thin wrapper around the public GeckoTerminal HTTP API."""

    def __init__(
        self,
        base_url: str = "https://api.geckoterminal.com/api/v2",
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json;version=20230302"}
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_ohlcv(
        self,
        pool_address: str,
        network: str = "solana",
        timeframe: str = "minute",
        aggregate: int = 5,
        limit: int = 200,
        currency: str = "usd",
    ) -> List[Candle]:
        """GET /networks/{network}/pools/{pool_address}/ohlcv/{timeframe}.

        Returns candles sorted ascending by timestamp (oldest first), which is
        the order pandas/Kronos expect. GeckoTerminal itself returns newest
        first.
        """
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {VALID_TIMEFRAMES}, got {timeframe!r}")

        path = f"/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
        params = {"aggregate": aggregate, "limit": limit, "currency": currency}
        data = self._get(path, params=params)

        attributes = ((data or {}).get("data") or {}).get("attributes") or {}
        rows = attributes.get("ohlcv_list") or []

        candles: List[Candle] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                candles.append(
                    Candle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (TypeError, ValueError):
                logger.warning("Skipping malformed OHLCV row: %r", row)
                continue

        candles.sort(key=lambda c: c.timestamp)
        return candles
