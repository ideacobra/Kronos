"""FOMO/momentum composite scoring for candidate token pairs.

The score (0-100) blends four signals:
  - volume surge:   is 5-minute volume unusually high vs. the last-hour pace?
  - price momentum: is the price accelerating upward recently?
  - buy pressure:   are buys outnumbering sells in recent transactions?
  - boost bonus:    is the token currently being "boosted" on DexScreener?

Gating filters (liquidity, token age, position limits) are hard pass/fail
checks applied *before* scoring -- they are not part of the composite score.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from memecoin_bot.config import Settings
from memecoin_bot.data.types import ScoredCandidate, TokenPair


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _normalize_pct_change(pct_change: float, cap: float) -> float:
    """Map a percent change in [-cap, cap] onto a 0-100 scale (50 = flat)."""
    if cap <= 0:
        return 50.0
    bounded = max(-cap, min(cap, pct_change))
    return (bounded + cap) / (2.0 * cap) * 100.0


def volume_surge_score(pair: TokenPair) -> float:
    """Compare 5-minute volume against the implied 5-minute baseline from
    the last hour (volume_h1 / 12). A ratio of 1.0 (in line with the recent
    average) scores 50; 2x+ the average pace scores 100.
    """
    baseline_5m = pair.volume_h1 / 12.0
    if baseline_5m <= 1e-9:
        # No meaningful hourly baseline (brand-new pair): any real volume
        # right now is itself a signal, but we can't compute a ratio.
        return 100.0 if pair.volume_m5 > 0 else 0.0
    ratio = pair.volume_m5 / baseline_5m
    return _clamp(ratio * 50.0)


def momentum_score(pair: TokenPair, m5_cap: float = 15.0, h1_cap: float = 60.0) -> float:
    """Blend the very-recent (m5) and recent (h1) percent price change into
    a 0-100 momentum score, weighting the most recent window higher so
    accelerating moves score above steady ones.
    """
    m5_component = _normalize_pct_change(pair.price_change_m5, m5_cap)
    h1_component = _normalize_pct_change(pair.price_change_h1, h1_cap)
    return _clamp(0.6 * m5_component + 0.4 * h1_component)


def buy_pressure_score(pair: TokenPair) -> float:
    """Blend m5 and h1 buy/sell transaction ratios into a 0-100 score."""
    blended_ratio = 0.6 * pair.txns_m5.buy_ratio + 0.4 * pair.txns_h1.buy_ratio
    return _clamp(blended_ratio * 100.0)


def boost_bonus_score(pair: TokenPair, saturation_amount: float = 500.0) -> float:
    """DexScreener "boosts" are a paid hype signal; a small bonus for tokens
    currently boosted, saturating at `saturation_amount`.
    """
    if pair.boost_amount <= 0:
        return 0.0
    return _clamp(pair.boost_amount / saturation_amount * 100.0)


def compute_score(pair: TokenPair, settings: Settings) -> ScoredCandidate:
    """Compute the full composite FOMO score for a single pair."""
    vs = volume_surge_score(pair)
    ms = momentum_score(pair)
    bp = buy_pressure_score(pair)
    bb = boost_bonus_score(pair)

    weights = (
        settings.weight_volume_surge,
        settings.weight_price_momentum,
        settings.weight_buy_pressure,
        settings.weight_boost_bonus,
    )
    weight_sum = sum(weights)
    if weight_sum <= 0:
        composite = 0.0
    else:
        composite = (
            vs * settings.weight_volume_surge
            + ms * settings.weight_price_momentum
            + bp * settings.weight_buy_pressure
            + bb * settings.weight_boost_bonus
        ) / weight_sum

    reasons = [
        f"volume_surge={vs:.1f}",
        f"momentum={ms:.1f}",
        f"buy_pressure={bp:.1f}",
        f"boost_bonus={bb:.1f}",
    ]

    return ScoredCandidate(
        pair=pair,
        score=_clamp(composite),
        volume_surge_score=vs,
        momentum_score=ms,
        buy_pressure_score=bp,
        boost_bonus_score=bb,
        reasons=reasons,
    )


def passes_gating_filters(
    pair: TokenPair,
    settings: Settings,
    open_position_addresses: Iterable[str],
    open_position_count: int,
) -> Tuple[bool, str]:
    """Hard pass/fail checks applied before scoring. Returns (passed, reason)."""
    if pair.liquidity_usd < settings.min_liquidity_usd:
        return False, f"liquidity ${pair.liquidity_usd:,.0f} below min ${settings.min_liquidity_usd:,.0f}"

    age_minutes = pair.age_minutes
    if age_minutes is not None:
        if age_minutes < settings.min_token_age_minutes:
            return False, f"token age {age_minutes:.1f}m below min {settings.min_token_age_minutes:.1f}m"
        max_age_minutes = settings.max_token_age_hours * 60.0
        if age_minutes > max_age_minutes:
            return False, f"token age {age_minutes / 60.0:.1f}h above max {settings.max_token_age_hours:.1f}h"

    if pair.base_token_address in set(open_position_addresses):
        return False, "already holding a position in this token"

    if open_position_count >= settings.max_open_positions:
        return False, "max open positions reached"

    return True, "ok"


def score_candidates(
    pairs: List[TokenPair],
    settings: Settings,
    open_position_addresses: Iterable[str] = (),
    open_position_count: int = 0,
) -> List[ScoredCandidate]:
    """Apply gating filters then score survivors, sorted best-first."""
    open_addresses = set(open_position_addresses)
    scored: List[ScoredCandidate] = []
    for pair in pairs:
        passed, _reason = passes_gating_filters(pair, settings, open_addresses, open_position_count)
        if not passed:
            continue
        scored.append(compute_score(pair, settings))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored
