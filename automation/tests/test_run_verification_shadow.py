import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.run_verification_shadow import main
from automation.tests.test_verification_shadow import (
    MappingFetcher,
    discovery_artifact,
    write_artifact,
)


class ShadowCommandTests(unittest.TestCase):
    def test_command_refuses_without_live_before_constructing_fetcher(self):
        constructed = []

        def factory():
            constructed.append(True)
            return MappingFetcher()

        with self.assertRaises(SystemExit) as caught, patch("sys.stderr"):
            main(
                [
                    "--discovery-root", "/nonexistent/discovery",
                    "--output-root", "/nonexistent/output",
                ],
                fetcher_factory=factory,
            )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(constructed, [])

    def test_explicit_live_fake_run_is_shadow_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery"
            output = root / "shadow"
            write_artifact(discovery, discovery_artifact())
            with patch("builtins.print") as printed:
                code = main(
                    [
                        "--live",
                        "--discovery-root", str(discovery),
                        "--output-root", str(output),
                        "--venue", "ijcai",
                        "--year", "2026",
                    ],
                    fetcher_factory=MappingFetcher,
                )
            self.assertEqual(code, 0)
            rendered = printed.call_args.args[0]
            self.assertIn('"shadow_only": true', rendered)
            self.assertIn('"venue_count": 1', rendered)
            self.assertTrue((output / "shadow-summary.v1.json").is_file())


if __name__ == "__main__":
    unittest.main()
