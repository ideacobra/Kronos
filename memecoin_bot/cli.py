"""Command-line entrypoint for memecoin_bot.

Subcommands:
  scan    - read-only market read: fetch + score candidates, print a ranked
            table. NO trading, NO posting.
  run     - continuous (or single-cycle) scan/score/manage/trade/post loop.
            Paper trading by default. `run --live` requires passing every
            safety gate documented in memecoin_bot/README.md "Going Live"
            before it will submit a single real transaction.
  stats   - print portfolio equity/P&L summary from storage.
  wallet  - `wallet new` generates a Solana keypair and writes its private
            key into your local .env; `wallet balance` reads real on-chain
            SOL + SPL token balances (read-only) for the configured wallet.

Usage:
  python -m memecoin_bot.cli scan
  python -m memecoin_bot.cli run --once --dry-run --no-post
  python -m memecoin_bot.cli run --interval 60
  python -m memecoin_bot.cli run --live --i-understand-live-trading-risk
  python -m memecoin_bot.cli stats
  python -m memecoin_bot.cli wallet new
  python -m memecoin_bot.cli wallet balance
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from memecoin_bot.bot import MemecoinBot, fetch_candidate_pairs
from memecoin_bot.config import Settings
from memecoin_bot.data.dexscreener import DexScreenerClient
from memecoin_bot.data.types import ScoredCandidate
from memecoin_bot.storage import Storage
from memecoin_bot.strategy.scoring import score_candidates
from memecoin_bot.trading.solana_rpc import LAMPORTS_PER_SOL, is_public_rpc_url, mask_rpc_url

logger = logging.getLogger(__name__)

# Recommended live-mode risk defaults for a small ($15-ish) budget. `run
# --live` applies these automatically, but ONLY for settings whose
# corresponding env var was not already explicitly set -- explicit env vars
# and CLI override flags (e.g. --max-positions) always win. See
# memecoin_bot/README.md "Going Live" and .env.example.
LIVE_RECOMMENDED_MAX_OPEN_POSITIONS = 1
LIVE_RECOMMENDED_POSITION_SIZE_PCT = 0.35
LIVE_RECOMMENDED_MAX_POSITION_USD = 5.5
LIVE_RECOMMENDED_DAILY_REALIZED_LOSS_LIMIT_USD = 6.5
LIVE_RECOMMENDED_STARTING_BALANCE_USD = 15.0


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memecoin_bot",
        description=(
            "Solana memecoin FOMO/momentum bot. Paper trading (simulated, no real funds) by default; "
            "`run --live` can trade with real funds only after passing every safety gate documented in "
            "README.md 'Going Live' -- see also `wallet new` / `wallet balance`."
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
        help="No-op flag documenting that paper trading always happens unless --live is also passed.",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Trade with REAL funds via Jupiter on Solana instead of paper trading. Refuses to start "
            "unless ENABLE_LIVE_TRADING=true, SOLANA_PRIVATE_KEY, and a private SOLANA_RPC_URL are all "
            "configured AND --i-understand-live-trading-risk is also passed. See README 'Going Live'."
        ),
    )
    run_parser.add_argument(
        "--i-understand-live-trading-risk",
        dest="live_risk_ack",
        action="store_true",
        help=(
            "Required alongside --live on every single invocation to acknowledge the risk of total "
            "capital loss. Never persisted/saved anywhere -- must be typed every time as a deliberate act."
        ),
    )

    subparsers.add_parser("stats", help="Print portfolio equity/P&L summary from storage.")

    wallet_parser = subparsers.add_parser(
        "wallet", help="Solana wallet management: generate a keypair, check real on-chain balances."
    )
    wallet_subparsers = wallet_parser.add_subparsers(dest="wallet_command", required=True)

    wallet_new_parser = wallet_subparsers.add_parser(
        "new",
        help="Generate a fresh Solana keypair and write its private key into .env (SOLANA_PRIVATE_KEY).",
    )
    wallet_new_parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to the .env file to create/update (default: .env in the current directory).",
    )
    wallet_new_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing SOLANA_PRIVATE_KEY in the .env file instead of refusing.",
    )

    wallet_balance_parser = wallet_subparsers.add_parser(
        "balance",
        help="Read-only: print the configured wallet's real on-chain SOL + SPL token balances via RPC.",
    )
    wallet_balance_parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Load env vars from this .env file instead of the default search (repo root).",
    )

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


def check_live_trading_gate(settings: Settings, live_risk_ack: bool) -> Optional[str]:
    """Return a refusal message if `run --live` must be blocked, else None.

    ALL of the following must hold before live trading may proceed:
      1. ENABLE_LIVE_TRADING=true
      2. SOLANA_PRIVATE_KEY is set
      3. SOLANA_RPC_URL is set and is NOT a known public RPC hostname
      4. --i-understand-live-trading-risk was passed on THIS invocation
    """
    if not settings.enable_live_trading:
        return (
            "ENABLE_LIVE_TRADING is not set to true in your environment/.env. This is a deliberate "
            "safety gate -- see memecoin_bot/README.md 'Going Live'."
        )
    if not settings.solana_private_key_present:
        return (
            "SOLANA_PRIVATE_KEY is not set. Run `python -m memecoin_bot.cli wallet new` to generate a "
            "wallet first."
        )
    if not settings.solana_rpc_url:
        return (
            "SOLANA_RPC_URL is not set. Get a private RPC URL (e.g. Helius free tier: "
            "https://www.helius.dev/) and set it in .env."
        )
    if is_public_rpc_url(settings.solana_rpc_url):
        return (
            f"SOLANA_RPC_URL ({mask_rpc_url(settings.solana_rpc_url)}) is a known PUBLIC Solana RPC "
            "endpoint. Live trading requires a private RPC URL (e.g. Helius free tier: "
            "https://www.helius.dev/) -- public endpoints are unreliable/rate-limited for submitting "
            "real transactions."
        )
    if not live_risk_ack:
        return (
            "You must also pass --i-understand-live-trading-risk on this specific invocation. This "
            "flag is never saved/persisted and must be typed every time as a deliberate acknowledgement "
            "of the risk of total capital loss."
        )
    return None


def _apply_live_recommended_defaults(settings: Settings) -> None:
    """Apply the recommended small-budget live risk defaults, but only for
    settings whose corresponding env var the user did not already set
    explicitly (env vars / CLI override flags always win). See
    memecoin_bot/README.md 'Going Live'.
    """
    if os.getenv("MAX_OPEN_POSITIONS") is None:
        settings.max_open_positions = LIVE_RECOMMENDED_MAX_OPEN_POSITIONS
    if os.getenv("POSITION_SIZE_PCT") is None:
        settings.position_size_pct = LIVE_RECOMMENDED_POSITION_SIZE_PCT
    if os.getenv("MAX_POSITION_USD") is None:
        settings.max_position_usd = LIVE_RECOMMENDED_MAX_POSITION_USD
    if os.getenv("DAILY_REALIZED_LOSS_LIMIT_USD") is None:
        settings.daily_realized_loss_limit_usd = LIVE_RECOMMENDED_DAILY_REALIZED_LOSS_LIMIT_USD
    if os.getenv("STARTING_BALANCE_USD") is None:
        settings.starting_balance_usd = LIVE_RECOMMENDED_STARTING_BALANCE_USD


def _print_live_warning_banner(settings: Settings, broker) -> None:
    sol_balance, usd_balance = broker.get_wallet_balance_usd()
    bar = "=" * 78
    print(f"\n{bar}")
    print("LIVE TRADING MODE -- THIS WILL SUBMIT REAL SOLANA TRANSACTIONS")
    print(bar)
    print(f"Wallet address:              {broker.public_address}")
    print(f"Current on-chain balance:    {sol_balance:.6f} SOL (~${usd_balance:,.2f})")
    print(f"RPC endpoint:                {mask_rpc_url(settings.solana_rpc_url)}")
    print()
    print("Risk limits for this run:")
    print(
        f"  Max concurrent positions:  {settings.max_open_positions}"
    )
    print(
        f"  Max position size:        ${settings.max_position_usd:,.2f} "
        f"({settings.position_size_pct * 100:.0f}% of equity)"
    )
    print(f"  Daily realized-loss limit: ${settings.daily_realized_loss_limit_usd:,.2f}")
    print(
        f"  Slippage tolerance:        {settings.live_slippage_bps} bps "
        f"({settings.live_slippage_bps / 100:.1f}%)"
    )
    print()
    print(
        "Real funds are at risk from this point forward. Ctrl+C now to abort before the first scan runs."
    )
    print("See memecoin_bot/README.md 'Going Live' for the full risk disclosure.")
    print(f"{bar}\n")


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if args.use_kronos is not None:
        settings.use_kronos_signal = args.use_kronos

    broker = None
    if args.live:
        gate_error = check_live_trading_gate(settings, args.live_risk_ack)
        if gate_error:
            print(f"\nRefusing to start live trading: {gate_error}\n")
            return 1

        _apply_live_recommended_defaults(settings)

        from memecoin_bot.trading.live_broker import LiveBrokerConfigError, LiveJupiterBroker

        try:
            broker = LiveJupiterBroker(settings)
        except LiveBrokerConfigError as exc:
            print(f"\nRefusing to start live trading: {exc}\n")
            return 1

    if args.max_positions is not None:
        settings.max_open_positions = args.max_positions
    if args.starting_balance is not None:
        settings.starting_balance_usd = args.starting_balance
    post = args.post if args.post is not None else True
    interval = args.interval if args.interval is not None else settings.scan_interval_seconds

    if broker is not None:
        _print_live_warning_banner(settings, broker)

    print(
        f"Starting memecoin_bot: chain={settings.chain_id} "
        f"mode={'LIVE' if broker is not None else 'paper'} "
        f"kronos={'on' if settings.use_kronos_signal else 'off'} "
        f"x_posting={'LIVE' if settings.x_posting_active else 'dry-run'} "
        f"db={settings.db_path}"
    )

    with MemecoinBot(settings, broker=broker) as bot:
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


def cmd_wallet_new(args: argparse.Namespace) -> int:
    from memecoin_bot import envfile
    from memecoin_bot.trading.wallet import SOLANA_PRIVATE_KEY_ENV_VAR, generate_keypair, keypair_to_base58

    env_path = Path(args.env_file) if args.env_file else Path.cwd() / ".env"
    example_path = Path(__file__).resolve().parent.parent / ".env.example"
    created = envfile.ensure_env_file(env_path, example_path if example_path.exists() else None)
    if created:
        print(f"Created {env_path} ({'from .env.example' if example_path.exists() else 'empty'}).")

    existing = envfile.get_env_var(env_path, SOLANA_PRIVATE_KEY_ENV_VAR)
    if existing and not args.force:
        print(
            f"A {SOLANA_PRIVATE_KEY_ENV_VAR} already exists in {env_path}. Refusing to overwrite it -- "
            "re-run with --force if you really want to replace it (make sure any funds have already "
            "been moved out of the old wallet first, or you will lose access to them)."
        )
        return 1

    keypair = generate_keypair()
    secret_b58 = keypair_to_base58(keypair)
    envfile.upsert_env_var(env_path, SOLANA_PRIVATE_KEY_ENV_VAR, secret_b58)
    public_address = str(keypair.pubkey())
    # Deliberately drop all references to the secret material now that it has
    # been written to disk; nothing below this line ever touches it again.
    del secret_b58, keypair

    print(f"Generated a new Solana wallet and saved its private key to {env_path} ({SOLANA_PRIVATE_KEY_ENV_VAR}).")
    print()
    print(f"Public address: {public_address}")
    print()
    print(
        "Fund this address with exactly the SOL you intend to risk (your stated budget: $15), then "
        "never share your .env file."
    )
    print("Check the funded balance any time with: python -m memecoin_bot.cli wallet balance")
    return 0


def cmd_wallet_balance(args: argparse.Namespace, settings: Settings) -> int:
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
        settings = Settings.from_env()

    if not settings.solana_private_key_present:
        print("SOLANA_PRIVATE_KEY is not set. Run `python -m memecoin_bot.cli wallet new` first.")
        return 1
    if not settings.solana_rpc_url:
        print(
            "SOLANA_RPC_URL is not set. Get a private RPC URL (e.g. Helius free tier: "
            "https://www.helius.dev/) and set it in .env."
        )
        return 1

    from memecoin_bot.trading.solana_rpc import SolanaRpcClient
    from memecoin_bot.trading.wallet import load_keypair_from_env

    if is_public_rpc_url(settings.solana_rpc_url):
        print(
            f"Warning: {mask_rpc_url(settings.solana_rpc_url)} is a known public Solana RPC endpoint. "
            "Balance checks work fine, but LIVE TRADING requires a private RPC URL (e.g. Helius free "
            "tier: https://www.helius.dev/).\n"
        )

    keypair = load_keypair_from_env()
    pubkey = str(keypair.pubkey())
    del keypair
    rpc = SolanaRpcClient(settings.solana_rpc_url, timeout=settings.http_timeout_seconds)

    lamports = rpc.get_balance_lamports(pubkey)
    sol_balance = lamports / LAMPORTS_PER_SOL
    print(f"Wallet:      {pubkey}")
    print(f"RPC:         {mask_rpc_url(settings.solana_rpc_url)}")
    print(f"SOL balance: {sol_balance:.6f} SOL ({lamports:,} lamports)")

    token_balances = rpc.get_all_token_balances(pubkey)
    if token_balances:
        print("\nSPL token balances:")
        for tb in token_balances:
            print(f"  {tb['mint']}: {tb['ui_amount']:,.6f}")
    else:
        print("\nNo SPL token balances found.")
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
    if args.command == "wallet":
        if args.wallet_command == "new":
            return cmd_wallet_new(args)
        if args.wallet_command == "balance":
            return cmd_wallet_balance(args, settings)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
