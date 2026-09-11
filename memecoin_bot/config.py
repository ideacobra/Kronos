"""Central configuration for memecoin_bot.

All values are documented, have sensible defaults, and can be overridden
via environment variables (loaded from a `.env` file if present). See
`.env.example` at the repo root for the full list of variables.

Nothing in this module ever reads or stores private key material. The
only secrets it is aware of are X (Twitter) API credentials, which are
only used for posting transparent trade logs (see social/x_client.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv

    # Load a .env file from the current working directory (or any parent),
    # if one exists. This is a no-op if python-dotenv can't find a file.
    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is an additive dependency
    pass


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    val = os.getenv(name)
    if val is None:
        return default
    return val


def _opt_str(name: str) -> Optional[str]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return None
    return val


@dataclass
class Settings:
    """All tunable knobs for the bot. Construct with `Settings.from_env()`."""

    # --- Chain / market data -------------------------------------------------
    chain_id: str = "solana"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    http_timeout_seconds: float = 10.0

    # --- Candidate discovery / gating filters --------------------------------
    min_liquidity_usd: float = 10_000.0
    min_token_age_minutes: float = 10.0
    max_token_age_hours: float = 72.0
    max_candidates_per_scan: int = 40

    # --- Scoring weights (see strategy/scoring.py for the formula) ----------
    weight_volume_surge: float = 0.35
    weight_price_momentum: float = 0.30
    weight_buy_pressure: float = 0.20
    weight_boost_bonus: float = 0.15
    min_score_to_buy: float = 60.0

    # --- Kronos confirmation signal ------------------------------------------
    use_kronos_signal: bool = False
    kronos_model_name: str = "NeoQuasar/Kronos-small"
    kronos_tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_device: str = "cpu"
    kronos_lookback_candles: int = 100
    kronos_pred_len: int = 12
    kronos_timeframe: str = "minute"
    kronos_timeframe_aggregate: int = 5
    kronos_min_agreement_pct: float = 0.0
    kronos_require_agreement: bool = True

    # --- Risk management ------------------------------------------------------
    starting_balance_usd: float = 1_000.0
    position_size_pct: float = 0.05
    max_position_usd: float = 100.0
    stop_loss_pct: float = 0.15
    take_profit_pct: float = 0.40
    max_hold_minutes: float = 180.0
    max_open_positions: int = 5
    daily_realized_loss_limit_usd: float = 150.0
    simulated_slippage_pct: float = 0.01
    simulated_fee_pct: float = 0.003

    # --- Storage ----------------------------------------------------------------
    db_path: str = "memecoin_bot.db"

    # --- Loop timing ---------------------------------------------------------
    scan_interval_seconds: float = 60.0

    # --- X (Twitter) posting ---------------------------------------------------
    enable_x_posting: bool = False
    x_api_key: Optional[str] = None
    x_api_secret: Optional[str] = None
    x_access_token: Optional[str] = None
    x_access_token_secret: Optional[str] = None
    x_min_seconds_between_posts: float = 60.0
    x_max_posts_per_hour: int = 10
    x_post_periodic_summary: bool = True
    x_summary_interval_minutes: float = 240.0

    @property
    def x_credentials_present(self) -> bool:
        return all(
            [self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_token_secret]
        )

    @property
    def x_posting_active(self) -> bool:
        """True only when the user has explicitly opted in AND supplied all creds.

        This is the single gate that decides whether XPoster will ever attempt
        a real network call. See social/x_client.py.
        """
        return self.enable_x_posting and self.x_credentials_present

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            chain_id=_str("CHAIN_ID", cls.chain_id),
            dexscreener_base_url=_str("DEXSCREENER_BASE_URL", cls.dexscreener_base_url),
            geckoterminal_base_url=_str("GECKOTERMINAL_BASE_URL", cls.geckoterminal_base_url),
            http_timeout_seconds=_float("HTTP_TIMEOUT_SECONDS", cls.http_timeout_seconds),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", cls.min_liquidity_usd),
            min_token_age_minutes=_float("MIN_TOKEN_AGE_MINUTES", cls.min_token_age_minutes),
            max_token_age_hours=_float("MAX_TOKEN_AGE_HOURS", cls.max_token_age_hours),
            max_candidates_per_scan=_int("MAX_CANDIDATES_PER_SCAN", cls.max_candidates_per_scan),
            weight_volume_surge=_float("WEIGHT_VOLUME_SURGE", cls.weight_volume_surge),
            weight_price_momentum=_float("WEIGHT_PRICE_MOMENTUM", cls.weight_price_momentum),
            weight_buy_pressure=_float("WEIGHT_BUY_PRESSURE", cls.weight_buy_pressure),
            weight_boost_bonus=_float("WEIGHT_BOOST_BONUS", cls.weight_boost_bonus),
            min_score_to_buy=_float("MIN_SCORE_TO_BUY", cls.min_score_to_buy),
            use_kronos_signal=_bool("USE_KRONOS_SIGNAL", cls.use_kronos_signal),
            kronos_model_name=_str("KRONOS_MODEL_NAME", cls.kronos_model_name),
            kronos_tokenizer_name=_str("KRONOS_TOKENIZER_NAME", cls.kronos_tokenizer_name),
            kronos_device=_str("KRONOS_DEVICE", cls.kronos_device),
            kronos_lookback_candles=_int("KRONOS_LOOKBACK_CANDLES", cls.kronos_lookback_candles),
            kronos_pred_len=_int("KRONOS_PRED_LEN", cls.kronos_pred_len),
            kronos_timeframe=_str("KRONOS_TIMEFRAME", cls.kronos_timeframe),
            kronos_timeframe_aggregate=_int(
                "KRONOS_TIMEFRAME_AGGREGATE", cls.kronos_timeframe_aggregate
            ),
            kronos_min_agreement_pct=_float(
                "KRONOS_MIN_AGREEMENT_PCT", cls.kronos_min_agreement_pct
            ),
            kronos_require_agreement=_bool(
                "KRONOS_REQUIRE_AGREEMENT", cls.kronos_require_agreement
            ),
            starting_balance_usd=_float("STARTING_BALANCE_USD", cls.starting_balance_usd),
            position_size_pct=_float("POSITION_SIZE_PCT", cls.position_size_pct),
            max_position_usd=_float("MAX_POSITION_USD", cls.max_position_usd),
            stop_loss_pct=_float("STOP_LOSS_PCT", cls.stop_loss_pct),
            take_profit_pct=_float("TAKE_PROFIT_PCT", cls.take_profit_pct),
            max_hold_minutes=_float("MAX_HOLD_MINUTES", cls.max_hold_minutes),
            max_open_positions=_int("MAX_OPEN_POSITIONS", cls.max_open_positions),
            daily_realized_loss_limit_usd=_float(
                "DAILY_REALIZED_LOSS_LIMIT_USD", cls.daily_realized_loss_limit_usd
            ),
            simulated_slippage_pct=_float("SIMULATED_SLIPPAGE_PCT", cls.simulated_slippage_pct),
            simulated_fee_pct=_float("SIMULATED_FEE_PCT", cls.simulated_fee_pct),
            db_path=_str("DB_PATH", cls.db_path),
            scan_interval_seconds=_float("SCAN_INTERVAL_SECONDS", cls.scan_interval_seconds),
            enable_x_posting=_bool("ENABLE_X_POSTING", cls.enable_x_posting),
            x_api_key=_opt_str("X_API_KEY"),
            x_api_secret=_opt_str("X_API_SECRET"),
            x_access_token=_opt_str("X_ACCESS_TOKEN"),
            x_access_token_secret=_opt_str("X_ACCESS_TOKEN_SECRET"),
            x_min_seconds_between_posts=_float(
                "X_MIN_SECONDS_BETWEEN_POSTS", cls.x_min_seconds_between_posts
            ),
            x_max_posts_per_hour=_int("X_MAX_POSTS_PER_HOUR", cls.x_max_posts_per_hour),
            x_post_periodic_summary=_bool(
                "X_POST_PERIODIC_SUMMARY", cls.x_post_periodic_summary
            ),
            x_summary_interval_minutes=_float(
                "X_SUMMARY_INTERVAL_MINUTES", cls.x_summary_interval_minutes
            ),
        )


def default_db_path(settings: Settings) -> Path:
    """Resolve db_path to an absolute Path, relative to the current working dir."""
    p = Path(settings.db_path)
    return p if p.is_absolute() else Path.cwd() / p
