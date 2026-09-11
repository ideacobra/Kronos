"""XPoster: transparent trade-log posting to X (Twitter).

SAFETY (see also memecoin_bot/config.py and the root README):
  - Defaults to DRY mode: posts are only logged (to console + returned as a
    PostResult), never sent over the network, unless BOTH:
      1. all four X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN /
         X_ACCESS_TOKEN_SECRET credentials are present in the environment, AND
      2. ENABLE_X_POSTING=true is explicitly set.
    See `Settings.x_posting_active`.
  - Rate-limited and deduped: a configurable minimum number of seconds must
    pass between posts, a configurable maximum number of posts per hour is
    enforced, and an identical repeat of the last post is skipped outright.
  - `tweepy` is only imported lazily, inside `_ensure_client`, so importing
    this module (and running the rest of the bot in dry mode) never requires
    tweepy to be installed.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from memecoin_bot.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class PostResult:
    """Outcome of an `XPoster.post()` call."""

    posted: bool  # True only if a real network call succeeded
    dry_run: bool
    text: str
    reason: str
    tweet_id: Optional[str] = None


class XPoster:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._client_init_attempted = False
        self._client_init_failed_reason: Optional[str] = None
        self._post_times: Deque[float] = deque()
        self._last_post_text: Optional[str] = None
        self._last_post_time: Optional[float] = None

    @property
    def dry_run(self) -> bool:
        """True unless the user has explicitly opted in AND supplied all creds."""
        return not self.settings.x_posting_active

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if self._client_init_attempted:
            return False
        self._client_init_attempted = True
        try:
            import tweepy  # optional dependency, only needed for real posting

            self._client = tweepy.Client(
                consumer_key=self.settings.x_api_key,
                consumer_secret=self.settings.x_api_secret,
                access_token=self.settings.x_access_token,
                access_token_secret=self.settings.x_access_token_secret,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - never crash the bot over posting
            self._client_init_failed_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Failed to initialize X client, falling back to dry mode: %s",
                self._client_init_failed_reason,
            )
            return False

    def _rate_limit_reason(self, now: float) -> Optional[str]:
        if self._last_post_time is not None:
            elapsed = now - self._last_post_time
            if elapsed < self.settings.x_min_seconds_between_posts:
                return (
                    f"rate limit: only {elapsed:.0f}s since last post "
                    f"(min {self.settings.x_min_seconds_between_posts:.0f}s)"
                )
        while self._post_times and now - self._post_times[0] > 3600.0:
            self._post_times.popleft()
        if len(self._post_times) >= self.settings.x_max_posts_per_hour:
            return f"rate limit: {len(self._post_times)} posts in the last hour (max {self.settings.x_max_posts_per_hour})"
        return None

    def post(self, text: str, now: Optional[float] = None) -> PostResult:
        """Post (or, in dry mode, just log) a piece of trade-log text.

        Always returns a PostResult; never raises. A failed real post is
        logged and reported in the result, not raised, so a flaky X API
        never interrupts trading.
        """
        now = now if now is not None else time.time()

        if text == self._last_post_text:
            return PostResult(False, self.dry_run, text, "deduped: identical to last post")

        blocked_reason = self._rate_limit_reason(now)
        if blocked_reason is not None:
            return PostResult(False, self.dry_run, text, blocked_reason)

        if self.dry_run:
            logger.info("[X dry-run] Would post:\n%s", text)
            self._record_post(text, now)
            return PostResult(False, True, text, "dry-run mode (no network call made)")

        if not self._ensure_client():
            logger.info("[X dry-run fallback] Would post:\n%s", text)
            self._record_post(text, now)
            return PostResult(False, True, text, f"client unavailable: {self._client_init_failed_reason}")

        try:
            response = self._client.create_tweet(text=text)
            tweet_id = None
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                tweet_id = data.get("id")
            self._record_post(text, now)
            logger.info("Posted to X (id=%s)", tweet_id)
            return PostResult(True, False, text, "posted", tweet_id=tweet_id)
        except Exception as exc:  # noqa: BLE001 - a failed tweet must never crash the bot
            logger.warning("Failed to post to X: %s", exc, exc_info=True)
            return PostResult(False, False, text, f"post failed: {exc}")

    def _record_post(self, text: str, now: float) -> None:
        self._last_post_text = text
        self._last_post_time = now
        self._post_times.append(now)
