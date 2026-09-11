"""MemecoinBot: orchestrates one scan/score/manage/trade/post cycle.

Flow per cycle (`run_once`):
  1. Discover + score candidates (DexScreener boosts -> batch pair stats).
  2. Manage open positions: check stop-loss / take-profit / max-hold exits
     against fresh prices, closing (paper) positions as needed.
  3. Consider new entries from the scored candidates, subject to gating
     filters (strategy/scoring.py), risk limits (strategy/risk.py), and an
     optional Kronos confirmation signal (strategy/kronos_signal.py).
  4. Persist everything via storage.py and, if enabled, post transparent
     trade logs to X (social/x_client.py).

By default `self.broker` is a `PaperBroker`, which never touches the
network or a real wallet. `cli.py`'s `run --live` path constructs a real
`LiveJupiterBroker` (trading/live_broker.py) only after every safety gate
passes, and injects it via the `broker=` constructor parameter below --
this module never imports the live broker itself, so the default
paper-trading path never pulls in Solana/Jupiter dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from memecoin_bot.config import Settings
from memecoin_bot.data.dexscreener import DexScreenerClient
from memecoin_bot.data.geckoterminal import GeckoTerminalClient
from memecoin_bot.data.types import ScoredCandidate, TokenPair
from memecoin_bot.social.templates import (
    format_portfolio_summary,
    format_trade_closed,
    format_trade_opened,
)
from memecoin_bot.social.x_client import XPoster
from memecoin_bot.storage import Storage
from memecoin_bot.strategy.kronos_signal import KronosSignalProvider
from memecoin_bot.strategy.risk import RiskManager
from memecoin_bot.strategy.scoring import score_candidates
from memecoin_bot.trading.broker import Broker, PaperBroker
from memecoin_bot.trading.portfolio import Portfolio, Position

logger = logging.getLogger(__name__)


@dataclass
class RunCycleResult:
    """Summary of a single scan/score/manage/trade cycle, for CLI reporting."""

    scanned: int
    opened: int
    closed: int
    equity_usd: float
    cash_usd: float
    open_positions: int


def pick_best_pair_per_token(pairs: List[TokenPair]) -> List[TokenPair]:
    """A token can have multiple pools/pairs; keep only the most liquid one
    per base token address so scoring doesn't double-count a single token.
    """
    best: Dict[str, TokenPair] = {}
    for pair in pairs:
        existing = best.get(pair.base_token_address)
        if existing is None or pair.liquidity_usd > existing.liquidity_usd:
            best[pair.base_token_address] = pair
    return list(best.values())


def fetch_candidate_pairs(dex_client: DexScreenerClient, settings: Settings) -> List[TokenPair]:
    """Discover candidate tokens via DexScreener's boost feeds, then fetch
    full pair stats for them in batches.
    """
    boosts = dex_client.discover_candidate_addresses(chain_id=settings.chain_id)
    if not boosts:
        logger.info("No boosted %s tokens found on DexScreener this scan", settings.chain_id)
        return []
    addresses = list(boosts.keys())[: settings.max_candidates_per_scan]
    pairs = dex_client.get_pairs_for_tokens(addresses, chain_id=settings.chain_id)
    for pair in pairs:
        pair.boost_amount = boosts.get(pair.base_token_address, 0.0)
    return pick_best_pair_per_token(pairs)


class MemecoinBot:
    """Wires together data, strategy, trading, and social components."""

    def __init__(self, settings: Settings, broker: Optional[Broker] = None):
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.portfolio = Portfolio.from_storage(settings.starting_balance_usd, self.storage)
        self.broker = broker or PaperBroker(
            slippage_pct=settings.simulated_slippage_pct, fee_pct=settings.simulated_fee_pct
        )
        # True whenever a real (non-paper) broker was injected, e.g. `cli.py run
        # --live`'s LiveJupiterBroker. Only affects wording in social posts
        # (social/templates.py) -- risk/scoring/storage logic is unchanged.
        self.is_live = not isinstance(self.broker, PaperBroker)
        self.risk_manager = RiskManager(settings)
        self.dex_client = DexScreenerClient(
            base_url=settings.dexscreener_base_url, timeout=settings.http_timeout_seconds
        )
        self.gt_client = GeckoTerminalClient(
            base_url=settings.geckoterminal_base_url, timeout=settings.http_timeout_seconds
        )
        self.kronos_signal = KronosSignalProvider(settings, geckoterminal_client=self.gt_client)
        self.x_poster = XPoster(settings)
        self._last_summary_posted_at: Optional[float] = None

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "MemecoinBot":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    # --- read-only scan (used by `cli.py scan` and internally) --------------
    def scan(self) -> List[ScoredCandidate]:
        pairs = fetch_candidate_pairs(self.dex_client, self.settings)
        open_addresses = set(self.portfolio.positions.keys())
        return score_candidates(pairs, self.settings, open_addresses, self.portfolio.open_position_count)

    # --- internal helpers --------------------------------------------------
    def _fetch_prices_for_addresses(self, addresses: List[str]) -> Dict[str, float]:
        if not addresses:
            return {}
        pairs = self.dex_client.get_pairs_for_tokens(addresses, chain_id=self.settings.chain_id)
        best = pick_best_pair_per_token(pairs)
        return {p.base_token_address: p.price_usd for p in best}

    def _manage_open_positions(self, open_prices: Dict[str, float], post: bool) -> int:
        closed_count = 0
        now = time.time()
        for token_address in list(self.portfolio.positions.keys()):
            position = self.portfolio.positions.get(token_address)
            if position is None:
                continue
            price = open_prices.get(token_address)
            if price is None:
                logger.warning(
                    "No fresh price for open position %s (%s); skipping exit check this cycle",
                    position.symbol,
                    token_address,
                )
                continue
            mark_price = self.broker.mark_to_market(token_address, price)
            exit_reason = self.risk_manager.check_exit(position, mark_price, now)
            if exit_reason is None:
                continue

            fill = self.broker.close_position(token_address, position.symbol, position.quantity, mark_price)
            trade = self.portfolio.close_position(
                token_address,
                exit_price=fill.price,
                exit_fee_usd=fill.fee_usd,
                exit_reason=exit_reason,
                closed_at=now,
            )
            closed_count += 1
            logger.info(
                "Closed %s (%s): pnl=$%.2f (%.1f%%), hold=%.1fm",
                trade.symbol,
                exit_reason,
                trade.realized_pnl_usd,
                trade.realized_pnl_pct,
                trade.hold_minutes,
            )
            if post:
                text = format_trade_closed(
                    symbol=trade.symbol,
                    token_address=trade.token_address,
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price,
                    size_usd=trade.size_usd,
                    realized_pnl_usd=trade.realized_pnl_usd,
                    realized_pnl_pct=trade.realized_pnl_pct,
                    hold_minutes=trade.hold_minutes,
                    exit_reason=trade.exit_reason,
                    is_live=self.is_live,
                )
                self.x_poster.post(text)
        return closed_count

    def _open_new_positions(
        self, scored: List[ScoredCandidate], known_prices: Dict[str, float], post: bool
    ) -> int:
        opened_count = 0
        for candidate in scored:
            pair = candidate.pair

            if candidate.score < self.settings.min_score_to_buy:
                self.storage.record_scan(candidate, "skip", "score below buy threshold")
                continue

            equity = self.portfolio.equity_usd(known_prices)
            entry_decision = self.risk_manager.evaluate_entry(self.portfolio, pair.base_token_address, equity)
            if not entry_decision.approved:
                self.storage.record_scan(candidate, "skip", entry_decision.reason)
                continue

            kronos_confirmed: Optional[bool] = None
            if self.settings.use_kronos_signal:
                kronos_result = self.kronos_signal.evaluate(pair, pair.pair_address)
                if kronos_result.available:
                    kronos_confirmed = kronos_result.agrees
                    if self.settings.kronos_require_agreement and not kronos_result.agrees:
                        self.storage.record_scan(
                            candidate,
                            "skip",
                            f"kronos disagreement (predicted {kronos_result.predicted_direction}, "
                            f"{kronos_result.predicted_change_pct:+.2f}%)",
                        )
                        continue
                else:
                    logger.debug("Kronos signal unavailable for %s: %s", pair.symbol, kronos_result.reason)

            fill = self.broker.open_position(
                token_address=pair.base_token_address,
                symbol=pair.symbol,
                reference_price=pair.price_usd,
                size_usd=entry_decision.size_usd,
            )
            stop_loss_price, take_profit_price = self.risk_manager.compute_stop_and_take_prices(fill.price)
            position = Position(
                token_address=pair.base_token_address,
                symbol=pair.symbol,
                entry_price=fill.price,
                quantity=fill.quantity,
                size_usd=entry_decision.size_usd,
                entry_fee_usd=fill.fee_usd,
                opened_at=time.time(),
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                max_hold_minutes=self.settings.max_hold_minutes,
                kronos_confirmed=kronos_confirmed,
                score=candidate.score,
            )
            self.portfolio.open_position(position)
            self.storage.record_scan(candidate, "buy", "opened paper position")
            opened_count += 1
            logger.info(
                "Opened %s at $%.8f, size=$%.2f, score=%.1f",
                position.symbol,
                position.entry_price,
                position.size_usd,
                candidate.score,
            )

            if post:
                text = format_trade_opened(
                    symbol=position.symbol,
                    token_address=position.token_address,
                    entry_price=position.entry_price,
                    size_usd=position.size_usd,
                    score=candidate.score,
                    kronos_confirmed=kronos_confirmed,
                    is_live=self.is_live,
                )
                self.x_poster.post(text)

        return opened_count

    def _maybe_post_periodic_summary(self, current_prices: Dict[str, float], post: bool) -> None:
        if not post or not self.settings.x_post_periodic_summary:
            return
        now = time.time()
        interval_seconds = self.settings.x_summary_interval_minutes * 60.0
        if self._last_summary_posted_at is not None and now - self._last_summary_posted_at < interval_seconds:
            return

        stats = self.storage.get_summary_stats()
        text = format_portfolio_summary(
            equity_usd=self.portfolio.equity_usd(current_prices),
            cash_usd=self.portfolio.cash_usd,
            starting_balance_usd=self.settings.starting_balance_usd,
            realized_pnl_usd=self.portfolio.realized_pnl_usd,
            unrealized_pnl_usd=self.portfolio.unrealized_pnl_usd(current_prices),
            open_positions=self.portfolio.open_position_count,
            total_trades=stats["total_trades"],
            win_rate_pct=stats["win_rate_pct"],
            is_live=self.is_live,
        )
        result = self.x_poster.post(text)
        if result.posted or result.dry_run:
            self._last_summary_posted_at = now

    # --- the main cycle --------------------------------------------------------
    def run_once(self, post: bool = True) -> RunCycleResult:
        scored = self.scan()

        open_prices = self._fetch_prices_for_addresses(list(self.portfolio.positions.keys()))
        closed_count = self._manage_open_positions(open_prices, post=post)

        # Re-fetch remaining open-position prices (some may have just closed).
        remaining_prices = {
            addr: price for addr, price in open_prices.items() if addr in self.portfolio.positions
        }
        opened_count = self._open_new_positions(scored, remaining_prices, post=post)

        final_prices = dict(remaining_prices)
        for address, position in self.portfolio.positions.items():
            final_prices.setdefault(address, position.entry_price)

        self.portfolio.record_equity_point(final_prices)
        self._maybe_post_periodic_summary(final_prices, post=post)

        return RunCycleResult(
            scanned=len(scored),
            opened=opened_count,
            closed=closed_count,
            equity_usd=self.portfolio.equity_usd(final_prices),
            cash_usd=self.portfolio.cash_usd,
            open_positions=self.portfolio.open_position_count,
        )

    def run_forever(self, interval_seconds: float, post: bool = True) -> None:
        """Loop `run_once` until interrupted (Ctrl+C). A single cycle's error
        is logged and the loop continues after the next interval.
        """
        logger.info("Starting continuous run loop (interval=%.0fs)", interval_seconds)
        try:
            while True:
                try:
                    result = self.run_once(post=post)
                    logger.info(
                        "Cycle complete: scanned=%d opened=%d closed=%d equity=$%.2f cash=$%.2f open=%d",
                        result.scanned,
                        result.opened,
                        result.closed,
                        result.equity_usd,
                        result.cash_usd,
                        result.open_positions,
                    )
                except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                    logger.exception("Unhandled error during run cycle; will retry next interval")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Run loop interrupted by user, shutting down")
