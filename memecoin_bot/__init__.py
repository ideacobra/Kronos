"""memecoin_bot: a Solana memecoin FOMO/momentum PAPER-TRADING bot.

Reads public, unauthenticated market data (DexScreener, GeckoTerminal),
scores tokens for short-term FOMO/momentum, optionally asks the Kronos
foundation model for a directional confirmation, and manages a fully
simulated paper portfolio. Optionally posts transparent, factual trade
logs to X (Twitter) after trades happen.

SAFETY: This package is paper-trading only. It never holds a real wallet
key and never submits a real on-chain transaction. See trading/broker.py
for the explicit, documented stub for a future live broker implementation.
"""

__version__ = "0.1.0"
