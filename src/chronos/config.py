from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import cast

from chronos.domain import (
    AccountConfig,
    AppConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    KeyringCredential,
    PlaintextCredential,
)
from chronos.paths import expand_path

_DEFAULT_TRASH_RETENTION_DAYS = 30
_DEFAULT_INCLUDE = (".*",)


class ConfigError(ValueError):
    def __init__(self, message: str, key: str = "") -> None:
        self.key = key
        super().__init__(f"[{key}] {message}" if key else message)


def load(path: Path) -> AppConfig:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML parse error: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file is not UTF-8: {exc}") from exc
    return parse(cast(dict[str, object], data))


def parse(data: dict[str, object]) -> AppConfig:
    config_version = _require_int(data, "config_version", "")
    use_utf8 = _optional_bool(data, "use_utf8", "", default=False)
    editor = _optional_str(data, "editor", "", default=None)
    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be an array of tables", "accounts")
    accounts_list = cast(list[object], accounts_raw)
    accounts = tuple(
        _parse_account(a, f"accounts[{i}]") for i, a in enumerate(accounts_list)
    )
    return AppConfig(
        config_version=config_version,
        use_utf8=use_utf8,
        editor=editor,
        accounts=accounts,
    )


def _parse_account(data: object, key: str) -> AccountConfig:
    if not isinstance(data, dict):
        raise ConfigError("must be a table", key)
    account = cast(dict[str, object], data)
    name = _require_str(account, "name", key)
    account_key = f"{key}[{name}]"
    url = _require_str(account, "url", account_key)
    username = _require_str(account, "username", account_key)
    credential = _parse_credential(
        account.get("credential"), f"{account_key}.credential"
    )
    mirror_path = expand_path(_require_str(account, "mirror_path", account_key))
    trash_retention_days = _optional_int(
        account,
        "trash_retention_days",
        account_key,
        default=_DEFAULT_TRASH_RETENTION_DAYS,
    )
    include = _parse_regex_list(
        account.get("include", list(_DEFAULT_INCLUDE)), f"{account_key}.include"
    )
    exclude = _parse_regex_list(account.get("exclude", []), f"{account_key}.exclude")
    read_only = _parse_regex_list(
        account.get("read_only", []), f"{account_key}.read_only"
    )
    return AccountConfig(
        name=name,
        url=url,
        username=username,
        credential=credential,
        mirror_path=mirror_path,
        trash_retention_days=trash_retention_days,
        include=include,
        exclude=exclude,
        read_only=read_only,
    )


def _parse_credential(data: object, key: str) -> CredentialSpec:
    if data is None:
        raise ConfigError("missing required field", key)
    if not isinstance(data, dict):
        raise ConfigError("must be an inline table", key)
    spec = cast(dict[str, object], data)
    backend = _require_str(spec, "backend", key)
    if backend == "plaintext":
        return PlaintextCredential(password=_require_str(spec, "password", key))
    if backend == "env":
        return EnvCredential(variable=_require_str(spec, "variable", key))
    if backend == "command":
        cmd_raw = spec.get("command")
        if not isinstance(cmd_raw, list) or not cmd_raw:
            raise ConfigError("'command' must be a non-empty array of strings", key)
        cmd_parts: list[str] = []
        for i, part in enumerate(cast(list[object], cmd_raw)):
            if not isinstance(part, str):
                raise ConfigError("must be a string", f"{key}.command[{i}]")
            cmd_parts.append(part)
        return CommandCredential(command=tuple(cmd_parts))
    if backend == "encrypted":
        return KeyringCredential(
            service=_require_str(spec, "service", key),
            username=_require_str(spec, "username", key),
        )
    raise ConfigError(
        f"unknown credential backend '{backend}' "
        "(expected: plaintext, env, command, encrypted)",
        key,
    )


def _parse_regex_list(data: object, key: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(data, list):
        raise ConfigError("must be an array of regex strings", key)
    patterns: list[re.Pattern[str]] = []
    for i, item in enumerate(cast(list[object], data)):
        if not isinstance(item, str):
            raise ConfigError("must be a string", f"{key}[{i}]")
        try:
            patterns.append(re.compile(item))
        except re.error as exc:
            raise ConfigError(f"invalid regex '{item}': {exc}", f"{key}[{i}]") from exc
    return tuple(patterns)


def _require_str(data: dict[str, object], field: str, key: str) -> str:
    if field not in data:
        raise ConfigError(f"missing required field '{field}'", key)
    value = data[field]
    if not isinstance(value, str):
        raise ConfigError(
            f"'{field}' must be a string, got {type(value).__name__}", key
        )
    return value


def _require_int(data: dict[str, object], field: str, key: str) -> int:
    if field not in data:
        raise ConfigError(f"missing required field '{field}'", key)
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be an integer, got {type(value).__name__}", key
        )
    return value


def _optional_bool(
    data: dict[str, object], field: str, key: str, *, default: bool
) -> bool:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be a boolean, got {type(value).__name__}", key
        )
    return value


def _optional_str(
    data: dict[str, object], field: str, key: str, *, default: str | None
) -> str | None:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise ConfigError(
            f"'{field}' must be a string, got {type(value).__name__}", key
        )
    return value


def _optional_int(
    data: dict[str, object], field: str, key: str, *, default: int
) -> int:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be an integer, got {type(value).__name__}", key
        )
    return value
