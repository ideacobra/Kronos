"""Factual-only tweet templates for transparent trade logging.

Design rules (see the repo-level safety constraints in the task/README):
  - Every template is rendered *after* a trade already happened (simulated
    or, in live mode, a real confirmed on-chain trade). Nothing here is
    predictive or promotional about any token.
  - Every template includes an explicit simulated-trade or live-trade
    disclaimer (`is_live=True` switches the wording; see `bot.py`, which
    sets this based on whether a real broker was injected).
  - Every template only reports already-known, factual numbers: action,
    token, entry/exit price, USD size, P&L%, hold time.

`contains_hype_language()` is an honesty guardrail: it flags known
hype/promotional buzzwords so a future template edit can't accidentally
turn a factual log into promotion. The test suite asserts every template's
rendered output passes this guardrail, in both paper and live wording.
"""
from __future__ import annotations

from typing import Optional

SIMULATED_DISCLAIMER = "📝 Simulated trade — not financial advice."
PAPER_TRADING_TAG = "#PaperTrading"
LIVE_DISCLAIMER = "⚠️ Live trade — real funds, not financial advice."
LIVE_TRADING_TAG = "#LiveTrading"

# Substrings checked case-insensitively against rendered tweet text. Kept
# deliberately broad (better to over-flag in tests than let hype slip in).
HYPE_BUZZWORDS = [
    "to the moon",
    "moonshot",
    "moon soon",
    "pump it",
    "pump alert",
    "buy now",
    "buy the dip now",
    "guaranteed",
    "guarantee",
    "100x",
    "1000x",
    "get in now",
    "last chance",
    "don't miss out",
    "dont miss out",
    "ape in",
    "aping in",
    "lfg",
    "next big thing",
    "financial freedom",
    "easy money",
    "can't lose",
    "cant lose",
    "risk-free",
    "risk free",
    "sure thing",
    "must buy",
    "act fast",
    "🚀",
    "🌙",
]


def contains_hype_language(text: str) -> bool:
    """Return True if `text` contains any known hype/promotional buzzword."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in HYPE_BUZZWORDS)


def _fmt_usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _fmt_price(value: float) -> str:
    # Memecoin prices are often sub-cent; show enough precision to be useful.
    if value < 0.01:
        return f"${value:.8f}"
    return f"${value:,.4f}"


def _fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _fmt_hold_minutes(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60.0:.1f}h"


def _short_address(token_address: str) -> str:
    if len(token_address) <= 10:
        return token_address
    return f"{token_address[:4]}…{token_address[-4:]}"


def format_trade_opened(
    symbol: str,
    token_address: str,
    entry_price: float,
    size_usd: float,
    score: float,
    kronos_confirmed: Optional[bool] = None,
    is_live: bool = False,
) -> str:
    """Factual log of a newly opened position (paper by default, or a real
    confirmed live trade when `is_live=True`)."""
    action_label = "Live trade OPENED" if is_live else "Paper trade OPENED"
    size_label = "Size" if is_live else "Virtual size"
    lines = [
        f"{action_label}: ${symbol} ({_short_address(token_address)})",
        f"Entry price: {_fmt_price(entry_price)}",
        f"{size_label}: {_fmt_usd(size_usd)}",
        f"FOMO/momentum score: {score:.0f}/100",
    ]
    if kronos_confirmed is not None:
        lines.append(f"Kronos model agreement: {'yes' if kronos_confirmed else 'no'}")
    lines.append(LIVE_DISCLAIMER if is_live else SIMULATED_DISCLAIMER)
    lines.append(LIVE_TRADING_TAG if is_live else PAPER_TRADING_TAG)
    return "\n".join(lines)


def format_trade_closed(
    symbol: str,
    token_address: str,
    entry_price: float,
    exit_price: float,
    size_usd: float,
    realized_pnl_usd: float,
    realized_pnl_pct: float,
    hold_minutes: float,
    exit_reason: str,
    is_live: bool = False,
) -> str:
    """Factual log of a closed position (paper by default, or a real
    confirmed live trade when `is_live=True`), win or loss."""
    outcome = "WIN" if realized_pnl_usd > 0 else "LOSS"
    exit_reason_label = {
        "stop_loss": "stop-loss",
        "take_profit": "take-profit",
        "time_exit": "max hold time",
    }.get(exit_reason, exit_reason)

    action_label = "Live trade CLOSED" if is_live else "Paper trade CLOSED"
    size_label = "Size" if is_live else "Virtual size"
    lines = [
        f"{action_label} ({outcome}): ${symbol} ({_short_address(token_address)})",
        f"Entry: {_fmt_price(entry_price)} -> Exit: {_fmt_price(exit_price)}",
        f"{size_label}: {_fmt_usd(size_usd)} | P&L: {_fmt_usd(realized_pnl_usd)} ({_fmt_pct(realized_pnl_pct)})",
        f"Hold time: {_fmt_hold_minutes(hold_minutes)} | Exit reason: {exit_reason_label}",
        LIVE_DISCLAIMER if is_live else SIMULATED_DISCLAIMER,
        LIVE_TRADING_TAG if is_live else PAPER_TRADING_TAG,
    ]
    return "\n".join(lines)


def format_portfolio_summary(
    equity_usd: float,
    cash_usd: float,
    starting_balance_usd: float,
    realized_pnl_usd: float,
    unrealized_pnl_usd: float,
    open_positions: int,
    total_trades: int,
    win_rate_pct: float,
    is_live: bool = False,
) -> str:
    """Factual periodic summary of the whole portfolio (paper by default, or
    the real live wallet-backed portfolio when `is_live=True`)."""
    total_pnl_usd = equity_usd - starting_balance_usd
    total_pnl_pct = (total_pnl_usd / starting_balance_usd * 100.0) if starting_balance_usd else 0.0

    lines = [
        "Live portfolio summary" if is_live else "Paper portfolio summary",
        f"Equity: {_fmt_usd(equity_usd)} ({_fmt_pct(total_pnl_pct)} since start)",
        f"Cash: {_fmt_usd(cash_usd)} | Open positions: {open_positions}",
        f"Realized P&L: {_fmt_usd(realized_pnl_usd)} | Unrealized P&L: {_fmt_usd(unrealized_pnl_usd)}",
        f"Closed trades: {total_trades} | Win rate: {win_rate_pct:.1f}%",
        LIVE_DISCLAIMER if is_live else SIMULATED_DISCLAIMER,
        LIVE_TRADING_TAG if is_live else PAPER_TRADING_TAG,
    ]
    return "\n".join(lines)
