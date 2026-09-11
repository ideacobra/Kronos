"""Optional Kronos-model confirmation signal for the FOMO strategy.

This module is fully optional and MUST NEVER crash the bot: if torch,
pandas, or the pretrained Kronos weights are unavailable (not installed,
no internet, Hugging Face download fails, inference errors, etc.) every
public method degrades gracefully to an "unavailable" result and logs a
warning. Callers should always be prepared to fall back to the pure FOMO
score (see strategy/scoring.py) when `KronosSignalResult.available` is
False.

Toggle with `Settings.use_kronos_signal` (env var `USE_KRONOS_SIGNAL`).
Heavy dependencies (torch, pandas, the `model` package) are imported
lazily inside `_ensure_loaded`, never at module import time, so importing
this module -- and running the rest of the bot -- works fine even in an
environment where torch/pandas are not installed at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from memecoin_bot.config import Settings
from memecoin_bot.data.geckoterminal import GeckoTerminalClient
from memecoin_bot.data.types import Candle, TokenPair

logger = logging.getLogger(__name__)

_TIMEFRAME_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


@dataclass
class KronosSignalResult:
    """Outcome of asking Kronos to confirm/deny a candidate's momentum direction.

    `available=False` means the signal could not be computed for any reason
    (disabled, missing deps, download failure, insufficient history,
    inference error) -- callers should treat this exactly like "no opinion"
    and fall back to the pure FOMO score.
    """

    available: bool
    agrees: Optional[bool] = None
    predicted_direction: Optional[str] = None
    predicted_change_pct: Optional[float] = None
    reason: str = ""


class KronosSignalProvider:
    """Lazily loads the Kronos tokenizer/model on first use and reuses them."""

    def __init__(self, settings: Settings, geckoterminal_client: Optional[GeckoTerminalClient] = None):
        self.settings = settings
        self.gt_client = geckoterminal_client or GeckoTerminalClient(
            base_url=settings.geckoterminal_base_url, timeout=settings.http_timeout_seconds
        )
        self._predictor = None
        self._pd = None
        self._load_attempted = False
        self._load_failed_reason: Optional[str] = None

    @property
    def is_loaded(self) -> bool:
        return self._predictor is not None

    def _ensure_loaded(self) -> bool:
        """Best-effort, one-shot attempt to import torch/pandas and load the
        pretrained Kronos tokenizer + model. Never raises.
        """
        if self._predictor is not None:
            return True
        if self._load_attempted:
            # Don't hammer the network / retry expensive loads every scan.
            return False
        self._load_attempted = True
        try:
            import pandas as pd  # heavy optional dependency

            from model import Kronos, KronosPredictor, KronosTokenizer  # repo's own model package

            tokenizer = KronosTokenizer.from_pretrained(self.settings.kronos_tokenizer_name)
            model = Kronos.from_pretrained(self.settings.kronos_model_name)
            tokenizer.eval()
            model.eval()
            self._predictor = KronosPredictor(model, tokenizer, device=self.settings.kronos_device, max_context=512)
            self._pd = pd
            logger.info(
                "Kronos signal model loaded (%s / %s)",
                self.settings.kronos_model_name,
                self.settings.kronos_tokenizer_name,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - must never crash the bot
            self._load_failed_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Kronos signal unavailable, falling back to pure FOMO score (%s)",
                self._load_failed_reason,
            )
            self._predictor = None
            return False

    def _candles_to_frame(self, candles: List[Candle]):
        pd = self._pd
        return pd.DataFrame(
            {
                "timestamps": pd.to_datetime([c.timestamp for c in candles], unit="s"),
                "open": [c.open for c in candles],
                "high": [c.high for c in candles],
                "low": [c.low for c in candles],
                "close": [c.close for c in candles],
                "volume": [c.volume for c in candles],
            }
        )

    def _timeframe_seconds(self) -> int:
        unit = _TIMEFRAME_UNIT_SECONDS.get(self.settings.kronos_timeframe, 60)
        return unit * max(1, self.settings.kronos_timeframe_aggregate)

    def evaluate(self, pair: TokenPair, pool_address: str) -> KronosSignalResult:
        """Ask Kronos whether it agrees with the pair's short-term momentum
        direction. Always returns a result; never raises.
        """
        if not self.settings.use_kronos_signal:
            return KronosSignalResult(available=False, reason="Kronos signal disabled via config")

        if not self._ensure_loaded():
            return KronosSignalResult(available=False, reason=f"model unavailable: {self._load_failed_reason}")

        try:
            candles = self.gt_client.get_ohlcv(
                pool_address=pool_address,
                network=self.settings.chain_id,
                timeframe=self.settings.kronos_timeframe,
                aggregate=self.settings.kronos_timeframe_aggregate,
                limit=self.settings.kronos_lookback_candles,
            )
            min_history = max(30, self.settings.kronos_pred_len * 2)
            if len(candles) < min_history:
                return KronosSignalResult(
                    available=False,
                    reason=f"insufficient candle history ({len(candles)} < {min_history})",
                )

            pd = self._pd
            df = self._candles_to_frame(candles)
            x_timestamp = df["timestamps"]
            step = pd.Timedelta(seconds=self._timeframe_seconds())
            pred_len = self.settings.kronos_pred_len
            y_timestamp = pd.Series([x_timestamp.iloc[-1] + step * (i + 1) for i in range(pred_len)])

            pred_df = self._predictor.predict(
                df=df[["open", "high", "low", "close", "volume"]],
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=pred_len,
                verbose=False,
            )

            last_close = float(df["close"].iloc[-1])
            predicted_close = float(pred_df["close"].iloc[-1])
            if last_close <= 0:
                return KronosSignalResult(available=False, reason="invalid last close price")

            change_pct = (predicted_close - last_close) / last_close * 100.0
            direction = "up" if change_pct > 0 else ("down" if change_pct < 0 else "flat")
            momentum_direction = "up" if pair.price_change_m5 >= 0 else "down"
            agrees = direction == momentum_direction or abs(change_pct) < self.settings.kronos_min_agreement_pct

            return KronosSignalResult(
                available=True,
                agrees=agrees,
                predicted_direction=direction,
                predicted_change_pct=change_pct,
                reason="ok",
            )
        except Exception as exc:  # noqa: BLE001 - must never crash the bot
            logger.warning(
                "Kronos inference failed for %s, falling back to pure FOMO score: %s",
                pair.symbol,
                exc,
                exc_info=True,
            )
            return KronosSignalResult(available=False, reason=f"inference failed: {exc}")
