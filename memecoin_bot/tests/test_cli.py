"""Tests for cli.py: wallet subcommands and the `run --live` safety gate.

Wallet tests use isolated temp .env files (never the developer's real
`.env`). Live-gate tests use monkeypatched env vars and a mocked
LiveJupiterBroker / MemecoinBot -- no real network, funds, or keys are
ever touched.
"""
from __future__ import annotations

import argparse
import re
from unittest.mock import MagicMock, patch

from solders.keypair import Keypair

from memecoin_bot import cli
from memecoin_bot.bot import RunCycleResult
from memecoin_bot.config import Settings
from memecoin_bot.trading.wallet import keypair_to_base58

HELIUS_LIKE_RPC = "https://mainnet.helius-rpc.com/?api-key=test-key-not-real"


class TestCheckLiveTradingGate:
    def test_blocks_when_live_trading_not_enabled(self):
        settings = Settings(enable_live_trading=False)
        error = cli.check_live_trading_gate(settings, live_risk_ack=True)
        assert error is not None and "ENABLE_LIVE_TRADING" in error

    def test_blocks_when_private_key_missing(self):
        settings = Settings(enable_live_trading=True, solana_private_key_present=False)
        error = cli.check_live_trading_gate(settings, live_risk_ack=True)
        assert error is not None and "SOLANA_PRIVATE_KEY" in error

    def test_blocks_when_rpc_url_missing(self):
        settings = Settings(enable_live_trading=True, solana_private_key_present=True, solana_rpc_url=None)
        error = cli.check_live_trading_gate(settings, live_risk_ack=True)
        assert error is not None and "SOLANA_RPC_URL" in error

    def test_blocks_when_rpc_url_is_public(self):
        settings = Settings(
            enable_live_trading=True,
            solana_private_key_present=True,
            solana_rpc_url="https://api.mainnet-beta.solana.com",
        )
        error = cli.check_live_trading_gate(settings, live_risk_ack=True)
        assert error is not None and "PUBLIC" in error

    def test_blocks_when_risk_ack_flag_missing(self):
        settings = Settings(
            enable_live_trading=True, solana_private_key_present=True, solana_rpc_url=HELIUS_LIKE_RPC
        )
        error = cli.check_live_trading_gate(settings, live_risk_ack=False)
        assert error is not None and "--i-understand-live-trading-risk" in error

    def test_passes_when_everything_is_configured(self):
        settings = Settings(
            enable_live_trading=True, solana_private_key_present=True, solana_rpc_url=HELIUS_LIKE_RPC
        )
        assert cli.check_live_trading_gate(settings, live_risk_ack=True) is None


class TestApplyLiveRecommendedDefaults:
    def test_applies_defaults_when_env_vars_unset(self, monkeypatch):
        for var in (
            "MAX_OPEN_POSITIONS",
            "POSITION_SIZE_PCT",
            "MAX_POSITION_USD",
            "DAILY_REALIZED_LOSS_LIMIT_USD",
            "STARTING_BALANCE_USD",
        ):
            monkeypatch.delenv(var, raising=False)
        settings = Settings()

        cli._apply_live_recommended_defaults(settings)

        assert settings.max_open_positions == cli.LIVE_RECOMMENDED_MAX_OPEN_POSITIONS
        assert settings.position_size_pct == cli.LIVE_RECOMMENDED_POSITION_SIZE_PCT
        assert settings.max_position_usd == cli.LIVE_RECOMMENDED_MAX_POSITION_USD
        assert settings.daily_realized_loss_limit_usd == cli.LIVE_RECOMMENDED_DAILY_REALIZED_LOSS_LIMIT_USD
        assert settings.starting_balance_usd == cli.LIVE_RECOMMENDED_STARTING_BALANCE_USD

    def test_does_not_override_an_explicitly_set_env_var(self, monkeypatch):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
        settings = Settings(max_open_positions=3)

        cli._apply_live_recommended_defaults(settings)

        assert settings.max_open_positions == 3


def _mock_bot(result: RunCycleResult) -> MagicMock:
    mock_bot_instance = MagicMock()
    mock_bot_instance.__enter__.return_value = mock_bot_instance
    mock_bot_instance.run_once.return_value = result
    return mock_bot_instance


_SAMPLE_RESULT = RunCycleResult(scanned=0, opened=0, closed=0, equity_usd=15.0, cash_usd=15.0, open_positions=0)


class TestCmdRunLiveSafetyGate:
    def test_refuses_without_any_required_env_vars(self, monkeypatch, capsys):
        monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
        monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("SOLANA_RPC_URL", raising=False)

        with patch("memecoin_bot.cli.MemecoinBot") as MockBot:
            exit_code = cli.main(["run", "--once", "--live", "--i-understand-live-trading-risk"])

        assert exit_code == 1
        MockBot.assert_not_called()
        assert "Refusing to start live trading" in capsys.readouterr().out

    def test_refuses_without_risk_ack_flag(self, monkeypatch, capsys):
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(Keypair()))
        monkeypatch.setenv("SOLANA_RPC_URL", HELIUS_LIKE_RPC)

        with patch("memecoin_bot.cli.MemecoinBot") as MockBot:
            exit_code = cli.main(["run", "--once", "--live"])  # no --i-understand-live-trading-risk

        assert exit_code == 1
        MockBot.assert_not_called()
        assert "--i-understand-live-trading-risk" in capsys.readouterr().out

    def test_refuses_with_public_rpc_url(self, monkeypatch, capsys):
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(Keypair()))
        monkeypatch.setenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

        with patch("memecoin_bot.cli.MemecoinBot") as MockBot:
            exit_code = cli.main(["run", "--once", "--live", "--i-understand-live-trading-risk"])

        assert exit_code == 1
        MockBot.assert_not_called()
        assert "PUBLIC" in capsys.readouterr().out

    def test_proceeds_when_everything_is_configured(self, monkeypatch, capsys):
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(Keypair()))
        monkeypatch.setenv("SOLANA_RPC_URL", HELIUS_LIKE_RPC)

        mock_broker_instance = MagicMock()
        mock_broker_instance.public_address = "TestPublicAddress111111111111111111111111"
        mock_broker_instance.get_wallet_balance_usd.return_value = (0.1, 15.0)
        mock_bot_instance = _mock_bot(_SAMPLE_RESULT)

        with patch(
            "memecoin_bot.trading.live_broker.LiveJupiterBroker", return_value=mock_broker_instance
        ) as MockBroker, patch("memecoin_bot.cli.MemecoinBot", return_value=mock_bot_instance) as MockBot:
            exit_code = cli.main(
                ["run", "--once", "--live", "--i-understand-live-trading-risk", "--no-post"]
            )

        assert exit_code == 0
        MockBroker.assert_called_once()
        MockBot.assert_called_once()
        assert MockBot.call_args.kwargs["broker"] is mock_broker_instance
        mock_bot_instance.run_once.assert_called_once()

        output = capsys.readouterr().out
        assert "LIVE TRADING MODE" in output
        assert "TestPublicAddress111111111111111111111111" in output

    def test_paper_mode_passes_broker_none(self, capsys):
        mock_bot_instance = _mock_bot(_SAMPLE_RESULT)
        with patch("memecoin_bot.cli.MemecoinBot", return_value=mock_bot_instance) as MockBot:
            exit_code = cli.main(["run", "--once", "--no-post"])

        assert exit_code == 0
        assert MockBot.call_args.kwargs["broker"] is None
        assert "LIVE TRADING MODE" not in capsys.readouterr().out


class TestCmdWalletNew:
    def test_generates_wallet_and_never_prints_the_secret(self, tmp_path, capsys):
        env_path = tmp_path / ".env"
        args = argparse.Namespace(env_file=str(env_path), force=False)

        exit_code = cli.cmd_wallet_new(args)

        assert exit_code == 0
        output = capsys.readouterr().out
        content = env_path.read_text()
        match = re.search(r"SOLANA_PRIVATE_KEY=(\S+)", content)
        assert match is not None
        secret = match.group(1)
        assert len(secret) > 40
        assert secret not in output
        assert "Public address:" in output

    def test_refuses_to_overwrite_existing_key_without_force(self, tmp_path, capsys):
        env_path = tmp_path / ".env"
        env_path.write_text("SOLANA_PRIVATE_KEY=existingvalue\n")
        args = argparse.Namespace(env_file=str(env_path), force=False)

        exit_code = cli.cmd_wallet_new(args)

        assert exit_code == 1
        assert "existingvalue" in env_path.read_text()

    def test_force_overwrites_existing_key(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOLANA_PRIVATE_KEY=existingvalue\n")
        args = argparse.Namespace(env_file=str(env_path), force=True)

        exit_code = cli.cmd_wallet_new(args)

        assert exit_code == 0
        assert "existingvalue" not in env_path.read_text()

    def test_preserves_other_existing_env_vars(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("CHAIN_ID=solana\nMAX_OPEN_POSITIONS=5\n")
        args = argparse.Namespace(env_file=str(env_path), force=False)

        cli.cmd_wallet_new(args)

        content = env_path.read_text()
        assert "CHAIN_ID=solana" in content
        assert "MAX_OPEN_POSITIONS=5" in content
        assert "SOLANA_PRIVATE_KEY=" in content


class TestCmdWalletBalance:
    def test_refuses_when_private_key_missing(self, capsys):
        settings = Settings(solana_private_key_present=False)
        args = argparse.Namespace(env_file=None)

        exit_code = cli.cmd_wallet_balance(args, settings)

        assert exit_code == 1
        assert "SOLANA_PRIVATE_KEY" in capsys.readouterr().out

    def test_refuses_when_rpc_url_missing(self, capsys):
        settings = Settings(solana_private_key_present=True, solana_rpc_url=None)
        args = argparse.Namespace(env_file=None)

        exit_code = cli.cmd_wallet_balance(args, settings)

        assert exit_code == 1
        assert "SOLANA_RPC_URL" in capsys.readouterr().out

    def test_prints_balances_with_mocked_rpc(self, monkeypatch, capsys):
        keypair = Keypair()
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", keypair_to_base58(keypair))
        settings = Settings(solana_private_key_present=True, solana_rpc_url=HELIUS_LIKE_RPC)
        args = argparse.Namespace(env_file=None)

        mock_rpc_instance = MagicMock()
        mock_rpc_instance.get_balance_lamports.return_value = 2_500_000_000
        mock_rpc_instance.get_all_token_balances.return_value = [
            {"mint": "SomeMint111111111111111111111111111111111", "ui_amount": 42.0}
        ]

        with patch("memecoin_bot.trading.solana_rpc.SolanaRpcClient", return_value=mock_rpc_instance):
            exit_code = cli.cmd_wallet_balance(args, settings)

        assert exit_code == 0
        output = capsys.readouterr().out
        assert str(keypair.pubkey()) in output
        assert "2.500000 SOL" in output
        assert "SomeMint111111111111111111111111111111111" in output
