from __future__ import annotations

import io
import subprocess
import tempfile
import tomllib
import unittest
from collections.abc import Callable
from pathlib import Path

from chronos.bootstrap import (
    TEMPLATE_TOML,
    offer_bootstrap,
    write_template,
)
from chronos.config import load as load_config


class TemplateContentTest(unittest.TestCase):
    """The template is the first thing a user sees; assert its shape."""

    def test_parses_as_toml_with_no_active_accounts(self) -> None:
        data = tomllib.loads(TEMPLATE_TOML.decode())
        self.assertEqual(data["config_version"], 1)
        # All example accounts are commented out, so a freshly-written
        # template parses to zero accounts.
        self.assertNotIn("accounts", data)

    def test_loads_through_chronos_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            write_template(path)
            config = load_config(path)
        self.assertEqual(config.config_version, 1)
        self.assertEqual(config.accounts, ())

    def test_includes_both_basic_and_oauth_examples(self) -> None:
        body = TEMPLATE_TOML.decode()
        self.assertIn('backend = "plaintext"', body)
        self.assertIn('backend = "oauth"', body)
        self.assertIn("Nextcloud", body)
        self.assertIn("Google", body)


class WriteTemplateTest(unittest.TestCase):
    def test_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "deeper" / "config.toml"
            write_template(target)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), TEMPLATE_TOML)

    def test_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            target.write_bytes(b"old contents")
            write_template(target)
            self.assertEqual(target.read_bytes(), TEMPLATE_TOML)


def _scripted_prompt(answers: list[str]) -> Callable[[str], str]:
    """Return a prompt function that pops successive canned answers."""
    iterator = iter(answers)

    def prompt(_message: str) -> str:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("prompt called more times than expected") from exc

    return prompt


class OfferBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.tmp / "config.toml"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.editor_called_with: list[Path] = []

    def _editor(self, path: Path) -> None:
        self.editor_called_with.append(path)

    def test_user_accepts_both_prompts_writes_and_edits(self) -> None:
        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["y", ""]),
            open_editor=self._editor,
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(self.editor_called_with, [self.config_path])
        self.assertIn("Wrote template", self.stdout.getvalue())
        self.assertIn("Next steps", self.stdout.getvalue())

    def test_empty_response_defaults_to_yes(self) -> None:
        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["", "n"]),  # create yes, edit no
            open_editor=self._editor,
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(self.editor_called_with, [])

    def test_user_declines_creation(self) -> None:
        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["n"]),
            open_editor=self._editor,
        )
        self.assertEqual(code, 1)
        self.assertFalse(self.config_path.exists())
        self.assertIn("Skipped", self.stdout.getvalue())

    def test_existing_file_returns_error_without_prompting(self) -> None:
        self.config_path.write_text("config_version = 1\n", encoding="utf-8")

        def fail_prompt(_message: str) -> str:  # pragma: no cover
            raise AssertionError("prompt should not be called")

        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=fail_prompt,
            open_editor=self._editor,
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", self.stderr.getvalue())

    def test_editor_failure_keeps_zero_exit_with_advice(self) -> None:
        def broken_editor(_path: Path) -> None:
            raise subprocess.CalledProcessError(1, ["vim"])

        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["y", "y"]),
            open_editor=broken_editor,
        )
        # Template is on disk; the editor failure isn't fatal.
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())
        self.assertIn("editor exited non-zero", self.stderr.getvalue())
        self.assertIn("config edit", self.stdout.getvalue())

    def test_editor_not_found_keeps_zero_exit_with_advice(self) -> None:
        def missing_editor(_path: Path) -> None:
            raise FileNotFoundError("$EDITOR not set")

        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["y", "y"]),
            open_editor=missing_editor,
        )
        self.assertEqual(code, 0)
        self.assertIn("editor not found", self.stderr.getvalue())

    def test_editor_writes_invalid_toml_returns_error(self) -> None:
        def corrupting_editor(path: Path) -> None:
            path.write_text("not valid toml [[[", encoding="utf-8")

        code = offer_bootstrap(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            prompt=_scripted_prompt(["y", "y"]),
            open_editor=corrupting_editor,
        )
        self.assertEqual(code, 1)
        self.assertIn("config has errors", self.stderr.getvalue())
        # File is still on disk so the user can re-edit.
        self.assertTrue(self.config_path.exists())
