"""Small, dependency-free helpers for reading/writing a `.env` file line by
line, preserving every other line and comment untouched.

Used by `cli.py wallet new` to write a freshly generated `SOLANA_PRIVATE_KEY`
into the user's local `.env` without disturbing anything else already in it.
Nothing in this module ever logs a value it writes or reads -- callers are
responsible for not printing secrets themselves.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def ensure_env_file(env_path: Path, example_path: Optional[Path] = None) -> bool:
    """Make sure `env_path` exists, creating it from `example_path` if given
    and present, or as an empty file otherwise.

    Returns True if a new file was created, False if `env_path` already existed.
    """
    if env_path.exists():
        return False
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if example_path is not None and example_path.exists():
        env_path.write_text(example_path.read_text())
    else:
        env_path.write_text("")
    return True


def get_env_var(env_path: Path, key: str) -> Optional[str]:
    """Read a single `KEY=value` line's value from `env_path`, or None if
    the file doesn't exist or the key isn't set to a non-empty value.
    """
    if not env_path.exists():
        return None
    prefix = f"{key}="
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value or None
    return None


def upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Set `KEY=value` in `env_path`, replacing an existing (non-comment)
    `KEY=...` line in place if present, or appending a new line otherwise.
    Every other line is left byte-for-byte untouched.
    """
    prefix = f"{key}="
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        if line.strip().startswith(prefix):
            lines[i] = new_line
            replaced = True
            break

    if not replaced:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(new_line)

    env_path.write_text("\n".join(lines) + "\n")
