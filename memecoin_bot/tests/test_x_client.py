"""Tests for social/x_client.py: safe dry-run default gating, and the
rate-limit/dedupe logic. No real network calls are made -- `tweepy` is
never even imported unless posting is truly active.
"""
from __future__ import annotations

import time

from memecoin_bot.config import Settings
from memecoin_bot.social.x_client import XPoster


def make_settings(**overrides) -> Settings:
    defaults = dict(
        enable_x_posting=False,
        x_min_seconds_between_posts=60.0,
        x_max_posts_per_hour=10,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestDryRunGating:
    def test_dry_run_when_flag_off_even_with_creds(self):
        settings = make_settings(
            enable_x_posting=False,
            x_api_key="k",
            x_api_secret="s",
            x_access_token="t",
            x_access_token_secret="ts",
        )
        assert XPoster(settings).dry_run is True

    def test_dry_run_when_flag_on_but_missing_creds(self):
        settings = make_settings(enable_x_posting=True)
        assert XPoster(settings).dry_run is True

    def test_dry_run_when_only_some_creds_present(self):
        settings = make_settings(enable_x_posting=True, x_api_key="k", x_api_secret="s")
        assert XPoster(settings).dry_run is True

    def test_active_only_when_flag_and_all_four_creds_present(self):
        settings = make_settings(
            enable_x_posting=True,
            x_api_key="k",
            x_api_secret="s",
            x_access_token="t",
            x_access_token_secret="ts",
        )
        assert XPoster(settings).dry_run is False

    def test_dry_run_post_makes_no_network_call_and_reports_dry_run(self):
        poster = XPoster(make_settings())
        result = poster.post("hello world")
        assert result.dry_run is True
        assert result.posted is False
        assert "dry-run" in result.reason


class TestRateLimitAndDedupe:
    def test_dedupes_identical_consecutive_post(self):
        poster = XPoster(make_settings(x_min_seconds_between_posts=0.0))
        now = time.time()
        first = poster.post("same text", now=now)
        second = poster.post("same text", now=now + 1)
        assert "deduped" not in first.reason
        assert "deduped" in second.reason

    def test_enforces_min_seconds_between_posts(self):
        poster = XPoster(make_settings(x_min_seconds_between_posts=60.0))
        now = time.time()
        poster.post("first", now=now)
        result = poster.post("second", now=now + 10)
        assert "rate limit" in result.reason

    def test_allows_post_after_min_seconds_elapsed(self):
        poster = XPoster(make_settings(x_min_seconds_between_posts=60.0))
        now = time.time()
        poster.post("first", now=now)
        result = poster.post("second", now=now + 61)
        assert "rate limit" not in result.reason

    def test_enforces_max_posts_per_hour(self):
        poster = XPoster(make_settings(x_min_seconds_between_posts=0.0, x_max_posts_per_hour=2))
        now = time.time()
        poster.post("one", now=now)
        poster.post("two", now=now + 1)
        result = poster.post("three", now=now + 2)
        assert "rate limit" in result.reason

    def test_hourly_window_rolls_off_old_posts(self):
        poster = XPoster(make_settings(x_min_seconds_between_posts=0.0, x_max_posts_per_hour=1))
        now = time.time()
        poster.post("one", now=now)
        # More than an hour later, the earlier post should have rolled off.
        result = poster.post("two", now=now + 3700)
        assert "rate limit" not in result.reason
