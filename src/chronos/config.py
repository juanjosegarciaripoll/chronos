from __future__ import annotations

import contextlib
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import cast

from chronos.domain import (
    GOOGLE_CALDAV_URL,
    AccountConfig,
    AppConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    GoogleCredential,
    KeyringCredential,
    OAuthCredential,
    PlaintextCredential,
)
from chronos.paths import default_mirror_path, expand_path

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
    credential = _parse_credential(
        account.get("credential"), f"{account_key}.credential"
    )
    # The "google" backend has well-known url + username defaults so
    # the user only has to write the OAuth client_id + client_secret.
    if isinstance(credential, GoogleCredential):
        url = (
            _optional_str(account, "url", account_key, default=GOOGLE_CALDAV_URL)
            or GOOGLE_CALDAV_URL
        )
        username = _optional_str(account, "username", account_key, default="") or ""
    else:
        url = _require_str(account, "url", account_key)
        username = _require_str(account, "username", account_key)
    mirror_path = _parse_mirror_path(account, name, account_key)
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
    if backend == "google":
        return GoogleCredential(
            client_id=_require_str(spec, "client_id", key),
            client_secret=_require_str(spec, "client_secret", key),
        )
    if backend == "oauth":
        token_path_raw = _optional_str(spec, "token_path", key, default=None)
        return OAuthCredential(
            client_id=_require_str(spec, "client_id", key),
            client_secret=_require_str(spec, "client_secret", key),
            scope=_optional_str(
                spec,
                "scope",
                key,
                default="https://www.googleapis.com/auth/calendar",
            )
            or "https://www.googleapis.com/auth/calendar",
            token_path=expand_path(token_path_raw)
            if token_path_raw is not None
            else None,
        )
    raise ConfigError(
        f"unknown credential backend '{backend}' "
        "(expected: plaintext, env, command, encrypted, oauth, google)",
        key,
    )


def _parse_mirror_path(
    account: dict[str, object], account_name: str, account_key: str
) -> Path:
    """Resolve `mirror_path` if present, else fall back to the platform default.

    The default is `paths.default_mirror_path(account_name)` — i.e. a
    subdirectory of the OS user-data directory named after the account.
    Users who want a custom location override the field; users who don't
    can omit it entirely.
    """
    raw = account.get("mirror_path")
    if raw is None:
        return default_mirror_path(account_name)
    if not isinstance(raw, str):
        raise ConfigError(
            f"'mirror_path' must be a string, got {type(raw).__name__}",
            account_key,
        )
    return expand_path(raw)


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


def _toml_str(v: str) -> str:
    return (
        '"'
        + v.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
        + '"'
    )


def _toml_scalar(v: str | int | bool) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return _toml_str(v)


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(s) for s in items) + "]"


def _toml_inline_table(d: dict[str, object]) -> str:
    pairs = [
        f"{k} = {_toml_array(cast(list[str], v)) if isinstance(v, list) else _toml_scalar(cast(str | int | bool, v))}"
        for k, v in d.items()
    ]
    return "{ " + ", ".join(pairs) + " }"


def _dumps_toml(data: dict[str, object]) -> str:
    lines: list[str] = []
    for k, v in data.items():
        if k == "accounts":
            continue
        lines.append(f"{k} = {_toml_scalar(cast(str | int | bool, v))}")
    for account in cast(list[dict[str, object]], data.get("accounts", [])):
        lines.append("")
        lines.append("[[accounts]]")
        for k, v in account.items():
            if isinstance(v, dict):
                lines.append(f"{k} = {_toml_inline_table(cast(dict[str, object], v))}")
            elif isinstance(v, list):
                lines.append(f"{k} = {_toml_array(cast(list[str], v))}")
            else:
                lines.append(f"{k} = {_toml_scalar(cast(str | int | bool, v))}")
    return "\n".join(lines) + "\n"


def dump(config: AppConfig) -> dict[str, object]:
    """Serialise AppConfig to a TOML-writeable dict.

    Round-trip invariant: `parse(dump(c)) == c` for any valid config
    (regex patterns round-trip through their source strings; paths round-trip
    through `str(path)` + re-expansion).
    """
    data: dict[str, object] = {
        "config_version": config.config_version,
        "use_utf8": config.use_utf8,
    }
    if config.editor is not None:
        data["editor"] = config.editor
    data["accounts"] = [_dump_account(a) for a in config.accounts]
    return data


def save(config: AppConfig, path: Path) -> None:
    """Write `config` to `path` atomically (temp-file + rename)."""
    payload = _dumps_toml(dump(config)).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _dump_account(account: AccountConfig) -> dict[str, object]:
    is_google = isinstance(account.credential, GoogleCredential)
    out: dict[str, object] = {"name": account.name}
    # The "google" backend defaults both fields, so omit them on dump
    # when they are at their default values; keeps round-tripped configs
    # as minimal as the hand-written form.
    if not (is_google and account.url == GOOGLE_CALDAV_URL):
        out["url"] = account.url
    if not (is_google and account.username == ""):
        out["username"] = account.username
    out["credential"] = _dump_credential(account.credential)
    out["trash_retention_days"] = account.trash_retention_days
    out["include"] = [p.pattern for p in account.include]
    out["exclude"] = [p.pattern for p in account.exclude]
    out["read_only"] = [p.pattern for p in account.read_only]
    # Only emit mirror_path when the user picked a non-default location;
    # accounts that took the default stay clean on round-trip through
    # `chronos account add` / `config edit`.
    if account.mirror_path != default_mirror_path(account.name):
        out["mirror_path"] = str(account.mirror_path)
    return out


def _dump_credential(spec: CredentialSpec) -> dict[str, object]:
    if isinstance(spec, PlaintextCredential):
        return {"backend": "plaintext", "password": spec.password}
    if isinstance(spec, EnvCredential):
        return {"backend": "env", "variable": spec.variable}
    if isinstance(spec, CommandCredential):
        return {"backend": "command", "command": list(spec.command)}
    if isinstance(spec, KeyringCredential):
        return {
            "backend": "encrypted",
            "service": spec.service,
            "username": spec.username,
        }
    if isinstance(spec, GoogleCredential):
        return {
            "backend": "google",
            "client_id": spec.client_id,
            "client_secret": spec.client_secret,
        }
    # spec narrows to OAuthCredential by elimination.
    out: dict[str, object] = {
        "backend": "oauth",
        "client_id": spec.client_id,
        "client_secret": spec.client_secret,
        "scope": spec.scope,
    }
    if spec.token_path is not None:
        out["token_path"] = str(spec.token_path)
    return out
