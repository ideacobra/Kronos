"""Tests for envfile.py: safe, surgical .env file read/write helpers."""
from __future__ import annotations

from memecoin_bot import envfile


class TestEnsureEnvFile:
    def test_creates_from_example_when_missing(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text("FOO=bar\nBAZ=qux\n")
        env_path = tmp_path / "sub" / ".env"

        created = envfile.ensure_env_file(env_path, example)

        assert created is True
        assert env_path.read_text() == "FOO=bar\nBAZ=qux\n"

    def test_creates_empty_when_no_example(self, tmp_path):
        env_path = tmp_path / ".env"
        created = envfile.ensure_env_file(env_path, None)
        assert created is True
        assert env_path.read_text() == ""

    def test_does_not_overwrite_existing_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=1\n")
        example = tmp_path / ".env.example"
        example.write_text("FOO=bar\n")

        created = envfile.ensure_env_file(env_path, example)

        assert created is False
        assert env_path.read_text() == "EXISTING=1\n"


class TestGetEnvVar:
    def test_returns_none_for_missing_file(self, tmp_path):
        assert envfile.get_env_var(tmp_path / ".env", "FOO") is None

    def test_returns_value_when_present(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("FOO=bar\nBAZ=qux\n")
        assert envfile.get_env_var(env_path, "FOO") == "bar"
        assert envfile.get_env_var(env_path, "BAZ") == "qux"

    def test_returns_none_for_empty_value(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("FOO=\n")
        assert envfile.get_env_var(env_path, "FOO") is None

    def test_ignores_commented_lines(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# FOO=commented_out\nFOO=real_value\n")
        assert envfile.get_env_var(env_path, "FOO") == "real_value"


class TestUpsertEnvVar:
    def test_appends_when_missing(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=1\n")

        envfile.upsert_env_var(env_path, "NEW_KEY", "new_value")

        content = env_path.read_text()
        assert "EXISTING=1" in content
        assert "NEW_KEY=new_value" in content

    def test_replaces_existing_value_in_place(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\nSOLANA_PRIVATE_KEY=oldvalue\nB=2\n")

        envfile.upsert_env_var(env_path, "SOLANA_PRIVATE_KEY", "newvalue")

        lines = env_path.read_text().splitlines()
        assert lines[0] == "A=1"
        assert lines[1] == "SOLANA_PRIVATE_KEY=newvalue"
        assert lines[2] == "B=2"
        assert "oldvalue" not in env_path.read_text()

    def test_preserves_comments_and_other_lines_untouched(self, tmp_path):
        env_path = tmp_path / ".env"
        original = "# a comment\nFOO=bar\n\n# another section\nBAZ=qux\n"
        env_path.write_text(original)

        envfile.upsert_env_var(env_path, "NEW_KEY", "value")

        content = env_path.read_text()
        assert "# a comment" in content
        assert "FOO=bar" in content
        assert "# another section" in content
        assert "BAZ=qux" in content

    def test_creates_file_if_missing(self, tmp_path):
        env_path = tmp_path / "nested" / ".env"
        env_path.parent.mkdir(parents=True)

        envfile.upsert_env_var(env_path, "FOO", "bar")

        assert env_path.read_text().strip() == "FOO=bar"

    def test_does_not_touch_a_similarly_named_key(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOLANA_PRIVATE_KEY_BACKUP=untouched\n")

        envfile.upsert_env_var(env_path, "SOLANA_PRIVATE_KEY", "newvalue")

        content = env_path.read_text()
        assert "SOLANA_PRIVATE_KEY_BACKUP=untouched" in content
        assert "SOLANA_PRIVATE_KEY=newvalue" in content
