from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path


def expand_path(raw: str | Path) -> Path:
    text = str(raw)
    text = os.path.expandvars(text)
    text = os.path.expanduser(text)
    return Path(text)


def _resolve_data_dir(platform: str, env: Mapping[str, str], home: Path) -> Path:
    if platform == "win32":
        appdata = env.get("APPDATA")
        if appdata:
            return Path(appdata) / "chronos"
        return home / "AppData" / "Roaming" / "chronos"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "chronos"
    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "chronos"
    return home / ".local" / "share" / "chronos"


def _resolve_config_dir(platform: str, env: Mapping[str, str], home: Path) -> Path:
    if platform == "win32":
        appdata = env.get("APPDATA")
        if appdata:
            return Path(appdata) / "chronos"
        return home / "AppData" / "Roaming" / "chronos"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "chronos"
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "chronos"
    return home / ".config" / "chronos"


def _resolve_cache_dir(platform: str, env: Mapping[str, str], home: Path) -> Path:
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        if local:
            return Path(local) / "chronos" / "Cache"
        return home / "AppData" / "Local" / "chronos" / "Cache"
    if platform == "darwin":
        return home / "Library" / "Caches" / "chronos"
    xdg = env.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "chronos"
    return home / ".cache" / "chronos"


def user_data_dir() -> Path:
    return _resolve_data_dir(sys.platform, os.environ, Path.home())


def user_config_dir() -> Path:
    return _resolve_config_dir(sys.platform, os.environ, Path.home())


def user_cache_dir() -> Path:
    return _resolve_cache_dir(sys.platform, os.environ, Path.home())


def default_config_path() -> Path:
    return user_config_dir() / "config.toml"


def default_mirror_dir() -> Path:
    return user_data_dir() / "mirror"


def default_mirror_path(account_name: str) -> Path:
    """Per-account mirror root used as the default in `config.toml`.

    Matches the runtime layout `<default_mirror_dir>/<account>/...`,
    so accounts that use this default produce no on-disk surprise.
    """
    return default_mirror_dir() / account_name


def default_index_path() -> Path:
    return user_data_dir() / "index.sqlite3"


def default_tui_state_path() -> Path:
    """Tiny file that persists TUI state (last view, etc.) across sessions."""
    return user_data_dir() / "tui_state"


def oauth_token_dir() -> Path:
    """Directory holding OAuth access/refresh tokens per account.

    Uses the platform-standard user data directory (APPDATA on Windows,
    Library/Application Support on macOS, XDG_DATA_HOME on Linux).
    """
    return user_data_dir() / "tokens"


def oauth_token_path(account_name: str) -> Path:
    return oauth_token_dir() / f"{account_name}.json"


def sync_lock_path() -> Path:
    """Path to the lockfile that gates concurrent `chronos sync` runs.

    Lives in the user data dir alongside the index and mirror so it
    shares their lifecycle (kept across reboots, scoped per-user).
    """
    return user_data_dir() / "sync.lock"


def bundled_docs_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if not isinstance(base, str):
        return None
    return Path(base) / "docs"
