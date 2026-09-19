"""Persistent shared-key management for Remote iTerm."""

from __future__ import annotations

import http.cookies
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


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


# A browser that has authenticated once is handed the key back as an HttpOnly
# cookie so it keeps working without the page needing the key in localStorage.
# Safari deletes a site's script-writable storage (localStorage included) after
# seven days of Safari use without visiting the site, which is exactly the
# pattern of a phone that only opens remote-iterm from the occasional
# notification tap; a cookie set by the server is exempt from that purge.
# Cookies are scoped by host, not port, so one set by the API (7291) is also
# sent by the page served from Vite (7292) — and is never readable by scripts.
COOKIE_NAME = "remote_iterm_key"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60


def key_from_cookie_header(cookie_header) -> str | None:
    """The key carried by the auth cookie in a raw Cookie header, if any."""
    if not isinstance(cookie_header, str) or not cookie_header:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel is not None and morsel.value else None


def _hostname(host: str | None) -> str | None:
    if not host:
        return None
    try:
        return urlsplit(f"//{host}").hostname
    except ValueError:
        return None


def is_trusted_origin(origin, host_header) -> bool:
    """Whether a browser origin may make credentialed requests to this server.

    The client is served from another port of the same machine (Vite on 7292,
    this API on 7291), and it always talks to the API at the hostname the page
    itself was loaded from — so an origin is trusted exactly when its host
    matches the Host header, whatever the port. Echoing any origin would let an
    arbitrary website drive the terminal with the cookie; matching the host
    keeps that to pages served by this Mac.
    """
    if not isinstance(origin, str) or not isinstance(host_header, str):
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    server_host = _hostname(host_header)
    return server_host is not None and parts.hostname.lower() == server_host.lower()


if __name__ == "__main__":
    print(load_or_create_key())
