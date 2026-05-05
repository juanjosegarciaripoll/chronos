from __future__ import annotations

import re
import tempfile
import textwrap
import tomllib
import unittest
import unittest.mock
from pathlib import Path

from chronos.config import ConfigError, dump, load, parse, save
from chronos.domain import (
    AccountConfig,
    AppConfig,
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

    def test_mirror_path_defaults_to_platform_path_when_omitted(self) -> None:
        from chronos.paths import default_mirror_path

        acct_data = dict(self.BASE_ACCOUNT)
        del acct_data["mirror_path"]
        config = parse(self._wrap(acct_data))
        self.assertEqual(
            config.accounts[0].mirror_path, default_mirror_path("personal")
        )

    def test_mirror_path_must_be_a_string_when_present(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = 42
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(acct_data))
        self.assertIn("mirror_path", str(ctx.exception))
        self.assertIn("string", str(ctx.exception))

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

    def test_google_account_omits_url_and_username(self) -> None:
        from chronos.domain import GOOGLE_CALDAV_URL, GoogleCredential

        config = parse(
            {
                "config_version": 1,
                "accounts": [
                    {
                        "name": "google",
                        "credential": {
                            "backend": "google",
                            "client_id": "cid.apps.googleusercontent.com",
                            "client_secret": "GOCSPX-x",
                        },
                    }
                ],
            }
        )
        acct = config.accounts[0]
        self.assertIsInstance(acct.credential, GoogleCredential)
        self.assertEqual(acct.url, GOOGLE_CALDAV_URL)
        self.assertEqual(acct.username, "")

    def test_non_google_account_still_requires_url(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(
                {
                    "config_version": 1,
                    "accounts": [
                        {
                            "name": "personal",
                            "username": "u@example.com",
                            "credential": {"backend": "env", "variable": "X"},
                        }
                    ],
                }
            )
        self.assertIn("url", str(ctx.exception))

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

    def test_oauth_minimal(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "oauth",
                    "client_id": "1234.apps.googleusercontent.com",
                    "client_secret": "secret-value",
                }
            )
        )
        from chronos.domain import OAuthCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "secret-value")
        self.assertEqual(cred.scope, "https://www.googleapis.com/auth/calendar")
        self.assertIsNone(cred.token_path)

    def test_oauth_with_custom_scope_and_token_path(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "oauth",
                    "client_id": "cid",
                    "client_secret": "cs",
                    "scope": "https://example/scope",
                    "token_path": "/tmp/mytokens.json",
                }
            )
        )
        from chronos.domain import OAuthCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.scope, "https://example/scope")
        self.assertEqual(cred.token_path, Path("/tmp/mytokens.json"))

    def test_oauth_missing_client_id_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "oauth", "client_secret": "cs"}))
        self.assertIn("client_id", str(ctx.exception))

    def test_google_minimal(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "google",
                    "client_id": "1234.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-secret",
                }
            )
        )
        from chronos.domain import GoogleCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, GoogleCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")

    def test_google_missing_client_id_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "google", "client_secret": "cs"}))
        self.assertIn("client_id", str(ctx.exception))

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "saml2"}))
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


def _sample_config(**overrides: object) -> AppConfig:
    account = AccountConfig(
        name=str(overrides.get("name", "personal")),
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=EnvCredential(variable="CHRONOS_PERSONAL_PW"),
        mirror_path=Path("/tmp/chronos/personal"),
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=(account,),
    )


class DumpTest(unittest.TestCase):
    def test_dump_produces_valid_toml(self) -> None:
        from chronos.config import _dumps_toml  # noqa: PLC0415

        config = _sample_config()
        rendered = _dumps_toml(dump(config))
        # Round-trip through tomllib to prove the output is valid TOML.
        reparsed = tomllib.loads(rendered)
        self.assertEqual(reparsed["config_version"], 1)
        self.assertEqual(len(reparsed["accounts"]), 1)
        self.assertEqual(reparsed["accounts"][0]["name"], "personal")

    def test_dump_omits_none_editor(self) -> None:
        data = dump(_sample_config())
        self.assertNotIn("editor", data)

    def test_dump_omits_mirror_path_when_default(self) -> None:
        from chronos.paths import default_mirror_path

        config = _sample_config()
        config = AppConfig(
            config_version=config.config_version,
            use_utf8=config.use_utf8,
            editor=config.editor,
            accounts=(
                AccountConfig(
                    name=config.accounts[0].name,
                    url=config.accounts[0].url,
                    username=config.accounts[0].username,
                    credential=config.accounts[0].credential,
                    mirror_path=default_mirror_path("personal"),
                    trash_retention_days=config.accounts[0].trash_retention_days,
                    include=config.accounts[0].include,
                    exclude=config.accounts[0].exclude,
                    read_only=config.accounts[0].read_only,
                ),
            ),
        )
        data = dump(config)
        accounts = data["accounts"]
        assert isinstance(accounts, list)
        self.assertNotIn("mirror_path", accounts[0])
        # And the round-trip recovers the same path via the default.
        reparsed = parse(data)
        self.assertEqual(
            reparsed.accounts[0].mirror_path, default_mirror_path("personal")
        )

    def test_dump_omits_url_and_username_for_google_defaults(self) -> None:
        from chronos.domain import GoogleCredential
        from chronos.paths import default_mirror_path

        account = AccountConfig(
            name="google",
            url="https://apidata.googleusercontent.com/caldav/v2/",
            username="",
            credential=GoogleCredential(client_id="cid", client_secret="cs"),
            mirror_path=default_mirror_path("google"),
            trash_retention_days=30,
            include=(re.compile(".*"),),
            exclude=(),
            read_only=(),
        )
        config = AppConfig(
            config_version=1, use_utf8=False, editor=None, accounts=(account,)
        )
        data = dump(config)
        from typing import cast as _cast

        accounts = _cast(list[dict[str, object]], data["accounts"])
        self.assertNotIn("url", accounts[0])
        self.assertNotIn("username", accounts[0])
        # Round-trip recovers the same account from the minimal form.
        reparsed = parse(data)
        self.assertEqual(reparsed.accounts[0], account)

    def test_dump_keeps_mirror_path_when_custom(self) -> None:
        config = _sample_config()
        # _sample_config uses /tmp/chronos/personal which is NOT the
        # platform default; dump should keep it. Path serialisation is
        # platform-specific (str() of a WindowsPath uses backslashes),
        # so compare via Path equality.
        data = dump(config)
        accounts = data["accounts"]
        assert isinstance(accounts, list)
        self.assertEqual(
            Path(str(accounts[0]["mirror_path"])), Path("/tmp/chronos/personal")
        )

    def test_dump_includes_editor_when_set(self) -> None:
        config = _sample_config()
        config = AppConfig(
            config_version=config.config_version,
            use_utf8=config.use_utf8,
            editor="nvim",
            accounts=config.accounts,
        )
        data = dump(config)
        self.assertEqual(data["editor"], "nvim")

    def test_credential_roundtrip(self) -> None:
        from chronos.domain import GoogleCredential, OAuthCredential

        for cred in (
            PlaintextCredential(password="s3cret"),
            EnvCredential(variable="MY_VAR"),
            CommandCredential(command=("pass", "show", "chronos")),
            KeyringCredential(service="chronos", username="u@example.com"),
            OAuthCredential(
                client_id="cid",
                client_secret="cs",
                scope="https://example/scope",
            ),
            OAuthCredential(
                client_id="cid2",
                client_secret="cs2",
                scope="https://example/scope",
                token_path=Path("/tmp/t.json"),
            ),
            GoogleCredential(
                client_id="g.apps.googleusercontent.com",
                client_secret="GOCSPX-x",
            ),
        ):
            with self.subTest(backend=type(cred).__name__):
                config = _sample_config()
                config = AppConfig(
                    config_version=config.config_version,
                    use_utf8=config.use_utf8,
                    editor=None,
                    accounts=(
                        AccountConfig(
                            name=config.accounts[0].name,
                            url=config.accounts[0].url,
                            username=config.accounts[0].username,
                            credential=cred,
                            mirror_path=config.accounts[0].mirror_path,
                            trash_retention_days=config.accounts[
                                0
                            ].trash_retention_days,
                            include=config.accounts[0].include,
                            exclude=config.accounts[0].exclude,
                            read_only=config.accounts[0].read_only,
                        ),
                    ),
                )
                reparsed = parse(dump(config))
                self.assertEqual(reparsed.accounts[0].credential, cred)


class SaveRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_save_then_load_produces_equal_config(self) -> None:
        original = _sample_config()
        path = self.tmp / "config.toml"
        save(original, path)
        self.assertTrue(path.exists())
        loaded = load(path)
        # Regex patterns aren't directly comparable -- check .pattern.
        self.assertEqual(loaded.config_version, original.config_version)
        self.assertEqual(loaded.use_utf8, original.use_utf8)
        self.assertEqual(loaded.editor, original.editor)
        self.assertEqual(len(loaded.accounts), 1)
        orig_a = original.accounts[0]
        load_a = loaded.accounts[0]
        self.assertEqual(load_a.name, orig_a.name)
        self.assertEqual(load_a.url, orig_a.url)
        self.assertEqual(load_a.username, orig_a.username)
        self.assertEqual(load_a.credential, orig_a.credential)
        self.assertEqual(load_a.trash_retention_days, orig_a.trash_retention_days)
        self.assertEqual(
            [p.pattern for p in load_a.include],
            [p.pattern for p in orig_a.include],
        )

    def test_save_creates_parent_directory(self) -> None:
        path = self.tmp / "nested" / "deep" / "config.toml"
        save(_sample_config(), path)
        self.assertTrue(path.exists())

    def test_save_is_atomic_no_tempfile_leftovers(self) -> None:
        path = self.tmp / "config.toml"
        save(_sample_config(), path)
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_save_keyboard_interrupt_preserves_prior_file(self) -> None:
        # Mid-save Ctrl-C must leave the previous config intact and
        # not strand a `.tmp-*` file. Otherwise the user's next
        # `chronos sync` could find no config or a half-written one.
        path = self.tmp / "config.toml"
        save(_sample_config(name="original-account"), path)
        original_bytes = path.read_bytes()
        with (
            unittest.mock.patch(
                "chronos.config.os.replace", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            save(_sample_config(name="overwriting-account"), path)
        self.assertEqual(path.read_bytes(), original_bytes)
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])
