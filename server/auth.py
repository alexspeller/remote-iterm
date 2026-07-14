"""Persistent shared-key management for Remote iTerm."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


DEFAULT_KEY_PATH = (
    Path.home() / "Library" / "Application Support" /
    "remote-iterm" / "access-key"
)


def access_key_path() -> Path:
    override = os.environ.get("REMOTE_ITERM_KEY_FILE")
    return Path(override).expanduser() if override else DEFAULT_KEY_PATH


def load_or_create_key(path: Path | None = None) -> str:
    """Return the machine-stable key, creating it securely on first use."""
    key_path = path or access_key_path()
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(key_path.parent, 0o700)

    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        key = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8") as key_file:
            key_file.write(key + "\n")

    os.chmod(key_path, 0o600)
    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"Remote iTerm access key is empty: {key_path}")
    return key


def is_valid_key(expected: str | None, supplied) -> bool:
    return (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and secrets.compare_digest(supplied, expected)
    )


if __name__ == "__main__":
    print(load_or_create_key())
