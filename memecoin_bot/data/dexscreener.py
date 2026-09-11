"""Client for the public, unauthenticated DexScreener API.

No API key is required for any of these endpoints. See
https://docs.dexscreener.com/api/reference for the (community) reference.

Endpoints used:
  - GET /token-boosts/latest/v1          -> recently "boosted" tokens (hype candidates)
  - GET /token-boosts/top/v1             -> top boosted tokens by amount
  - GET /latest/dex/search?q=            -> free-text pair search
  - GET /tokens/v1/{chainId}/{addresses} -> batch pair stats (<=30 addresses)

All responses are parsed defensively: DexScreener's public API is not
versioned/guaranteed, so every field access uses `.get()` with a default
and numeric fields are coerced with `float()`/`int()` guards.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import requests

from memecoin_bot.data.types import TokenPair, TxnWindow

logger = logging.getLogger(__name__)

_MAX_BATCH_ADDRESSES = 30


def _f(d: Dict[str, Any], key: str, default: float = 0.0) -> float:
    val = d.get(key, default)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _opt_f(d: Dict[str, Any], key: str) -> Optional[float]:
    val = d.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_i(d: Dict[str, Any], key: str) -> Optional[int]:
    val = d.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _txn_window(txns: Dict[str, Any], key: str) -> TxnWindow:
    w = txns.get(key) or {}
    try:
        buys = int(w.get("buys", 0) or 0)
    except (TypeError, ValueError):
        buys = 0
    try:
        sells = int(w.get("sells", 0) or 0)
    except (TypeError, ValueError):
        sells = 0
    return TxnWindow(buys=buys, sells=sells)


def parse_pair(raw: Dict[str, Any], boost_amount: float = 0.0) -> Optional[TokenPair]:
    """Parse one DexScreener pair object into a TokenPair, or None if malformed."""
    try:
        base_token = raw.get("baseToken") or {}
        quote_token = raw.get("quoteToken") or {}
        txns = raw.get("txns") or {}
        volume = raw.get("volume") or {}
        price_change = raw.get("priceChange") or {}
        liquidity = raw.get("liquidity") or {}

        price_usd = _opt_f(raw, "priceUsd")
        if price_usd is None:
            return None
        base_address = base_token.get("address")
        if not base_address:
            return None

        return TokenPair(
            chain_id=raw.get("chainId", ""),
            dex_id=raw.get("dexId", ""),
            pair_address=raw.get("pairAddress", ""),
            base_token_address=base_address,
            base_token_name=base_token.get("name", ""),
            base_token_symbol=base_token.get("symbol", ""),
            quote_token_symbol=quote_token.get("symbol", ""),
            price_usd=price_usd,
            price_change_m5=_f(price_change, "m5"),
            price_change_h1=_f(price_change, "h1"),
            price_change_h6=_f(price_change, "h6"),
            price_change_h24=_f(price_change, "h24"),
            volume_m5=_f(volume, "m5"),
            volume_h1=_f(volume, "h1"),
            volume_h6=_f(volume, "h6"),
            volume_h24=_f(volume, "h24"),
            liquidity_usd=_f(liquidity, "usd"),
            txns_m5=_txn_window(txns, "m5"),
            txns_h1=_txn_window(txns, "h1"),
            fdv=_opt_f(raw, "fdv"),
            market_cap=_opt_f(raw, "marketCap"),
            pair_created_at_ms=_opt_i(raw, "pairCreatedAt"),
            url=raw.get("url"),
            boost_amount=boost_amount,
        )
    except Exception:  # pragma: no cover - defensive: never let bad data crash a scan
        logger.warning("Failed to parse DexScreener pair payload: %r", raw, exc_info=True)
        return None


class DexScreenerClient:
    """Thin wrapper around the public DexScreener HTTP API."""

    def __init__(self, base_url: str = "https://api.dexscreener.com", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_latest_boosted_tokens(self) -> List[Dict[str, Any]]:
        """GET /token-boosts/latest/v1 -- recently boosted tokens (any chain)."""
        data = self._get("/token-boosts/latest/v1")
        return data if isinstance(data, list) else []

    def get_top_boosted_tokens(self) -> List[Dict[str, Any]]:
        """GET /token-boosts/top/v1 -- top boosted tokens by active boost amount."""
        data = self._get("/token-boosts/top/v1")
        return data if isinstance(data, list) else []

    def search_pairs(self, query: str) -> List[TokenPair]:
        """GET /latest/dex/search?q=<query> -- free text pair search."""
        data = self._get("/latest/dex/search", params={"q": query})
        pairs = (data or {}).get("pairs") or []
        parsed = [parse_pair(p) for p in pairs]
        return [p for p in parsed if p is not None]

    def get_pairs_for_tokens(
        self, token_addresses: Iterable[str], chain_id: str = "solana"
    ) -> List[TokenPair]:
        """GET /tokens/v1/{chainId}/{addresses} -- batch pair stats.

        DexScreener accepts up to 30 comma-separated addresses per call, so
        this method chunks the input transparently.
        """
        addresses = [a for a in dict.fromkeys(token_addresses) if a]  # de-dupe, keep order
        results: List[TokenPair] = []
        for i in range(0, len(addresses), _MAX_BATCH_ADDRESSES):
            chunk = addresses[i : i + _MAX_BATCH_ADDRESSES]
            joined = ",".join(chunk)
            data = self._get(f"/tokens/v1/{chain_id}/{joined}")
            if not isinstance(data, list):
                continue
            for raw in data:
                pair = parse_pair(raw)
                if pair is not None:
                    results.append(pair)
        return results

    def discover_candidate_addresses(self, chain_id: str = "solana") -> Dict[str, float]:
        """Combine latest+top boosted tokens into a {address: boost_amount} map,
        filtered to the requested chain.
        """
        boosts: Dict[str, float] = {}
        for fetcher in (self.get_latest_boosted_tokens, self.get_top_boosted_tokens):
            try:
                items = fetcher()
            except requests.RequestException:
                logger.warning("DexScreener boost feed request failed", exc_info=True)
                continue
            for item in items:
                if item.get("chainId") != chain_id:
                    continue
                address = item.get("tokenAddress")
                if not address:
                    continue
                amount = _f(item, "amount", _f(item, "totalAmount", 0.0))
                boosts[address] = max(boosts.get(address, 0.0), amount)
        return boosts
