from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from chronos.config import ConfigError, load, parse
from chronos.domain import (
    CommandCredential,
    EnvCredential,
    KeyringCredential,
    PlaintextCredential,
)


def _toml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, content: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load(self.tmp / "nonexistent.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_load_invalid_toml(self) -> None:
        path = self._write("this is = = not valid")
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("TOML parse error", str(ctx.exception))

    def test_load_minimal_config(self) -> None:
        path = self._write(
            _toml("""
            config_version = 1
        """)
        )
        config = load(path)
        self.assertEqual(config.config_version, 1)
        self.assertFalse(config.use_utf8)
        self.assertIsNone(config.editor)
        self.assertEqual(config.accounts, ())


class ParseTopLevelTest(unittest.TestCase):
    def test_requires_config_version(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({})
        self.assertIn("config_version", str(ctx.exception))

    def test_config_version_must_be_int(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({"config_version": "1"})
        self.assertIn("config_version", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    def test_use_utf8_rejects_non_bool(self) -> None:
        with self.assertRaises(ConfigError):
            parse({"config_version": 1, "use_utf8": "yes"})

    def test_use_utf8_and_editor_parsed(self) -> None:
        config = parse({"config_version": 1, "use_utf8": True, "editor": "nvim"})
        self.assertTrue(config.use_utf8)
        self.assertEqual(config.editor, "nvim")

    def test_accounts_must_be_list(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({"config_version": 1, "accounts": "not a list"})
        self.assertIn("accounts", ctx.exception.key)


class ParseAccountTest(unittest.TestCase):
    BASE_ACCOUNT: dict[str, object] = {
        "name": "personal",
        "url": "https://caldav.example.com/dav/",
        "username": "user@example.com",
        "credential": {"backend": "env", "variable": "CHRONOS_PERSONAL_PW"},
        "mirror_path": "/tmp/chronos/personal",
    }

    def _wrap(self, account: dict[str, object]) -> dict[str, object]:
        return {"config_version": 1, "accounts": [account]}

    def test_minimal_valid_account(self) -> None:
        config = parse(self._wrap(dict(self.BASE_ACCOUNT)))
        self.assertEqual(len(config.accounts), 1)
        acct = config.accounts[0]
        self.assertEqual(acct.name, "personal")
        self.assertEqual(acct.url, "https://caldav.example.com/dav/")
        self.assertEqual(acct.username, "user@example.com")
        self.assertIsInstance(acct.credential, EnvCredential)
        self.assertEqual(acct.trash_retention_days, 30)
        self.assertEqual(len(acct.include), 1)
        self.assertEqual(acct.include[0].pattern, ".*")
        self.assertEqual(acct.exclude, ())
        self.assertEqual(acct.read_only, ())

    def test_missing_required_field(self) -> None:
        bad = dict(self.BASE_ACCOUNT)
        del bad["username"]
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(bad))
        self.assertIn("username", str(ctx.exception))

    def test_mirror_path_is_expanded(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "~/chronos-test-mirror"
        config = parse(self._wrap(acct_data))
        path = config.accounts[0].mirror_path
        self.assertNotIn("~", str(path))
        self.assertTrue(path.is_absolute())

    def test_include_regex_compiled(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["include"] = ["^personal-.*$", "^shared$"]
        config = parse(self._wrap(acct_data))
        patterns = config.accounts[0].include
        self.assertEqual(len(patterns), 2)
        self.assertIsNotNone(patterns[0].fullmatch("personal-work"))
        self.assertIsNone(patterns[0].fullmatch("other"))

    def test_invalid_regex_surfaces_key(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["include"] = ["["]
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(acct_data))
        self.assertIn("include", ctx.exception.key)

    def test_trash_retention_days_override(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["trash_retention_days"] = 7
        config = parse(self._wrap(acct_data))
        self.assertEqual(config.accounts[0].trash_retention_days, 7)


class ParseCredentialTest(unittest.TestCase):
    def _wrap(self, credential: object) -> dict[str, object]:
        return {
            "config_version": 1,
            "accounts": [
                {
                    "name": "a",
                    "url": "https://caldav.example.com/",
                    "username": "u@example.com",
                    "credential": credential,
                    "mirror_path": "/tmp/m",
                }
            ],
        }

    def test_plaintext(self) -> None:
        config = parse(self._wrap({"backend": "plaintext", "password": "s3cret"}))
        cred = config.accounts[0].credential
        self.assertIsInstance(cred, PlaintextCredential)
        assert isinstance(cred, PlaintextCredential)
        self.assertEqual(cred.password, "s3cret")

    def test_env(self) -> None:
        config = parse(self._wrap({"backend": "env", "variable": "X"}))
        cred = config.accounts[0].credential
        assert isinstance(cred, EnvCredential)
        self.assertEqual(cred.variable, "X")

    def test_command(self) -> None:
        config = parse(
            self._wrap({"backend": "command", "command": ["pass", "show", "x"]})
        )
        cred = config.accounts[0].credential
        assert isinstance(cred, CommandCredential)
        self.assertEqual(cred.command, ("pass", "show", "x"))

    def test_command_empty_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse(self._wrap({"backend": "command", "command": []}))

    def test_encrypted(self) -> None:
        config = parse(
            self._wrap({"backend": "encrypted", "service": "chronos", "username": "u"})
        )
        cred = config.accounts[0].credential
        assert isinstance(cred, KeyringCredential)
        self.assertEqual(cred.service, "chronos")
        self.assertEqual(cred.username, "u")

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "oauth"}))
        self.assertIn("unknown credential backend", str(ctx.exception))

    def test_missing_credential(self) -> None:
        with self.assertRaises(ConfigError):
            parse(
                {
                    "config_version": 1,
                    "accounts": [
                        {
                            "name": "a",
                            "url": "https://x/",
                            "username": "u@example.com",
                            "mirror_path": "/tmp/m",
                        }
                    ],
                }
            )
