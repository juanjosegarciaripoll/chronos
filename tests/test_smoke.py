import unittest

from chronos import __version__


class SmokeTest(unittest.TestCase):
    def test_version_is_string(self) -> None:
        self.assertIsInstance(__version__, str)
        self.assertEqual(__version__, "0.1.0")
