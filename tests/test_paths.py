from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from chronos.paths import (
    _resolve_cache_dir,
    _resolve_config_dir,
    _resolve_data_dir,
    bundled_docs_path,
    default_config_path,
    default_index_path,
    default_mirror_dir,
    expand_path,
)


class ExpandPathTest(unittest.TestCase):
    def test_tilde_expands_to_home(self) -> None:
        expanded = expand_path("~/chronos/mirror")
        self.assertTrue(expanded.is_absolute())
        self.assertIn("chronos", str(expanded))
        self.assertNotIn("~", str(expanded))

    def test_env_var_dollar_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHRONOS_TEST_BASE"] = tmp
            try:
                expanded = expand_path("$CHRONOS_TEST_BASE/sub")
                self.assertEqual(expanded, Path(tmp) / "sub")
            finally:
                del os.environ["CHRONOS_TEST_BASE"]

    def test_env_var_percent_expansion_on_windows(self) -> None:
        if sys.platform != "win32":
            self.skipTest("percent-style vars are only expanded on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHRONOS_TEST_BASE"] = tmp
            try:
                expanded = expand_path("%CHRONOS_TEST_BASE%/sub")
                self.assertEqual(expanded, Path(tmp) / "sub")
            finally:
                del os.environ["CHRONOS_TEST_BASE"]

    def test_literal_path_passes_through(self) -> None:
        self.assertEqual(expand_path("/no/expansion/here"), Path("/no/expansion/here"))


class ResolveDirTest(unittest.TestCase):
    HOME = Path("/home/user")

    def test_linux_uses_xdg_data_home_when_set(self) -> None:
        env = {"XDG_DATA_HOME": "/custom/data"}
        self.assertEqual(
            _resolve_data_dir("linux", env, self.HOME),
            Path("/custom/data/chronos"),
        )

    def test_linux_falls_back_to_home_local_share(self) -> None:
        self.assertEqual(
            _resolve_data_dir("linux", {}, self.HOME),
            self.HOME / ".local" / "share" / "chronos",
        )

    def test_linux_config_uses_xdg_config_home_when_set(self) -> None:
        env = {"XDG_CONFIG_HOME": "/custom/config"}
        self.assertEqual(
            _resolve_config_dir("linux", env, self.HOME),
            Path("/custom/config/chronos"),
        )

    def test_linux_config_falls_back_to_home_config(self) -> None:
        self.assertEqual(
            _resolve_config_dir("linux", {}, self.HOME),
            self.HOME / ".config" / "chronos",
        )

    def test_linux_cache_uses_xdg_cache_home_when_set(self) -> None:
        env = {"XDG_CACHE_HOME": "/custom/cache"}
        self.assertEqual(
            _resolve_cache_dir("linux", env, self.HOME),
            Path("/custom/cache/chronos"),
        )

    def test_linux_cache_falls_back(self) -> None:
        self.assertEqual(
            _resolve_cache_dir("linux", {}, self.HOME),
            self.HOME / ".cache" / "chronos",
        )

    def test_darwin_uses_library_paths(self) -> None:
        self.assertEqual(
            _resolve_data_dir("darwin", {}, self.HOME),
            self.HOME / "Library" / "Application Support" / "chronos",
        )
        self.assertEqual(
            _resolve_config_dir("darwin", {}, self.HOME),
            self.HOME / "Library" / "Application Support" / "chronos",
        )
        self.assertEqual(
            _resolve_cache_dir("darwin", {}, self.HOME),
            self.HOME / "Library" / "Caches" / "chronos",
        )

    def test_windows_uses_appdata_when_set(self) -> None:
        env = {"APPDATA": "C:\\Users\\u\\AppData\\Roaming"}
        self.assertEqual(
            _resolve_data_dir("win32", env, self.HOME),
            Path("C:\\Users\\u\\AppData\\Roaming") / "chronos",
        )

    def test_windows_falls_back_when_appdata_missing(self) -> None:
        self.assertEqual(
            _resolve_data_dir("win32", {}, self.HOME),
            self.HOME / "AppData" / "Roaming" / "chronos",
        )

    def test_windows_cache_uses_localappdata(self) -> None:
        env = {"LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local"}
        self.assertEqual(
            _resolve_cache_dir("win32", env, self.HOME),
            Path("C:\\Users\\u\\AppData\\Local") / "chronos" / "Cache",
        )


class DefaultPathsTest(unittest.TestCase):
    def test_defaults_are_rooted_under_user_dirs(self) -> None:
        self.assertTrue(str(default_config_path()).endswith("config.toml"))
        self.assertTrue(str(default_index_path()).endswith("index.sqlite3"))
        self.assertTrue(str(default_mirror_dir()).endswith("mirror"))


class BundledDocsPathTest(unittest.TestCase):
    def test_returns_none_when_not_frozen(self) -> None:
        self.assertIsNone(bundled_docs_path())
