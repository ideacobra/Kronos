"""Command-line entrypoint for memecoin_bot.

Subcommands:
  scan   - read-only market read: fetch + score candidates, print a ranked
           table. NO trading, NO posting.
  run    - continuous (or single-cycle) scan/score/manage/trade/post loop.
  stats  - print portfolio equity/P&L summary from storage.

Usage:
  python -m memecoin_bot.cli scan
  python -m memecoin_bot.cli run --once --dry-run --no-post
  python -m memecoin_bot.cli run --interval 60
  python -m memecoin_bot.cli stats
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from memecoin_bot.bot import MemecoinBot, fetch_candidate_pairs
from memecoin_bot.config import Settings
from memecoin_bot.data.dexscreener import DexScreenerClient
from memecoin_bot.data.types import ScoredCandidate
from memecoin_bot.storage import Storage
from memecoin_bot.strategy.scoring import score_candidates

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memecoin_bot",
        description=(
            "Solana memecoin FOMO/momentum bot -- PAPER TRADING ONLY. "
            "Reads public market data and simulates trades; never moves real funds."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan", help="Read-only: fetch + score candidates. No trading, no posting."
    )
    scan_parser.add_argument("--limit", type=int, default=20, help="Max rows to print (default: 20).")

    run_parser = subparsers.add_parser("run", help="Run the scan/score/manage/trade/post loop.")
    run_parser.add_argument(
        "--interval", type=float, default=None, help="Seconds between cycles (default: from config/env)."
    )
    run_parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    post_group = run_parser.add_mutually_exclusive_group()
    post_group.add_argument(
        "--post",
        dest="post",
        action="store_true",
        default=None,
        help="Attempt X posting this run (still safely dry-run unless ENABLE_X_POSTING + all X_* creds are set).",
    )
    post_group.add_argument(
        "--no-post", dest="post", action="store_false", help="Disable all X posting for this run."
    )
    kronos_group = run_parser.add_mutually_exclusive_group()
    kronos_group.add_argument(
        "--use-kronos",
        dest="use_kronos",
        action="store_true",
        default=None,
        help="Enable the optional Kronos confirmation signal for this run.",
    )
    kronos_group.add_argument(
        "--no-kronos",
        dest="use_kronos",
        action="store_false",
        help="Disable the Kronos confirmation signal for this run.",
    )
    run_parser.add_argument(
        "--max-positions", type=int, default=None, help="Override max concurrent open positions."
    )
    run_parser.add_argument(
        "--starting-balance",
        type=float,
        default=None,
        help="Override starting paper balance (USD). Only affects a brand-new database.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No-op flag documenting that this bot never places real trades; paper trading always happens.",
    )

    subparsers.add_parser("stats", help="Print portfolio equity/P&L summary from storage.")

    return parser


def _print_scan_table(scored: List[ScoredCandidate], limit: int) -> None:
    if not scored:
        print("No candidates passed the gating filters this scan.")
        return
    header = (
        f"{'#':>3} {'SYMBOL':<12} {'SCORE':>6} {'PRICE($)':>14} {'LIQ($)':>12} "
        f"{'VOL5m($)':>10} {'CHG5m%':>8} {'CHG1h%':>8} {'AGE(m)':>8}"
    )
    print(header)
    print("-" * len(header))
    for i, candidate in enumerate(scored[:limit], start=1):
        pair = candidate.pair
        age = pair.age_minutes
        age_str = f"{age:.0f}" if age is not None else "?"
        print(
            f"{i:>3} {pair.symbol:<12.12} {candidate.score:>6.1f} {pair.price_usd:>14.8f} "
            f"{pair.liquidity_usd:>12,.0f} {pair.volume_m5:>10,.0f} {pair.price_change_m5:>8.1f} "
            f"{pair.price_change_h1:>8.1f} {age_str:>8}"
        )


def cmd_scan(args: argparse.Namespace, settings: Settings) -> int:
    storage = Storage(settings.db_path)
    try:
        open_addresses = {row["token_address"] for row in storage.get_open_trades()}
        dex_client = DexScreenerClient(
            base_url=settings.dexscreener_base_url, timeout=settings.http_timeout_seconds
        )
        pairs = fetch_candidate_pairs(dex_client, settings)
        scored = score_candidates(pairs, settings, open_addresses, len(open_addresses))
        print(
            f"Scanned {len(pairs)} candidate pair(s) on chain={settings.chain_id}; "
            f"{len(scored)} passed gating filters (min_score_to_buy={settings.min_score_to_buy:.0f}).\n"
        )
        _print_scan_table(scored, args.limit)
    finally:
        storage.close()
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if args.use_kronos is not None:
        settings.use_kronos_signal = args.use_kronos
    if args.max_positions is not None:
        settings.max_open_positions = args.max_positions
    if args.starting_balance is not None:
        settings.starting_balance_usd = args.starting_balance
    post = args.post if args.post is not None else True
    interval = args.interval if args.interval is not None else settings.scan_interval_seconds

    print(
        f"Starting memecoin_bot: chain={settings.chain_id} "
        f"kronos={'on' if settings.use_kronos_signal else 'off'} "
        f"x_posting={'LIVE' if settings.x_posting_active else 'dry-run'} "
        f"db={settings.db_path}"
    )

    with MemecoinBot(settings) as bot:
        if args.once:
            result = bot.run_once(post=post)
            print(
                f"Cycle complete: scanned={result.scanned} opened={result.opened} closed={result.closed} "
                f"equity=${result.equity_usd:,.2f} cash=${result.cash_usd:,.2f} "
                f"open_positions={result.open_positions}"
            )
        else:
            bot.run_forever(interval_seconds=interval, post=post)
    return 0


def cmd_stats(_args: argparse.Namespace, settings: Settings) -> int:
    storage = Storage(settings.db_path)
    try:
        stats = storage.get_summary_stats()
    finally:
        storage.close()

    print("Paper portfolio stats")
    print("-" * 40)
    print(f"Starting balance:      ${settings.starting_balance_usd:,.2f}")
    latest_equity = stats["latest_equity"]
    if latest_equity:
        print(f"Latest equity:         ${latest_equity['equity_usd']:,.2f}")
        print(f"Cash:                  ${latest_equity['cash_usd']:,.2f}")
        print(f"Unrealized P&L:        ${latest_equity['unrealized_pnl_usd']:,.2f}")
    else:
        print("Latest equity:         (no equity points recorded yet -- run `run --once` first)")
    print(f"Realized P&L (total):  ${stats['total_realized_pnl_usd']:,.2f}")
    print(f"Closed trades:         {stats['total_trades']} (win rate {stats['win_rate_pct']:.1f}%)")
    print(f"  wins / losses:       {stats['wins']} / {stats['losses']}")
    print(f"  best / worst trade:  ${stats['best_trade_usd']:,.2f} / ${stats['worst_trade_usd']:,.2f}")
    print(f"Open positions:        {stats['open_positions']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    settings = Settings.from_env()

    if args.command == "scan":
        return cmd_scan(args, settings)
    if args.command == "run":
        return cmd_run(args, settings)
    if args.command == "stats":
        return cmd_stats(args, settings)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
