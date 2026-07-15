import ast
import unittest
from pathlib import Path

from automation.grounding_resolution import resolve_known_grounding_redirect


MODULE = Path(__file__).resolve().parents[1] / "grounding_resolution.py"
WRAPPER = (
    "https://vertexaisearch.cloud.google.com/"
    "grounding-api-redirect/sanitized-fixture"
)


class GroundingResolutionTests(unittest.TestCase):
    def test_exact_reviewed_colt_sources_resolve_without_a_request(self):
        self.assertEqual(
            resolve_known_grounding_redirect(
                venue_id="colt",
                year=2025,
                provider_uri=WRAPPER,
                source_domain="proceedings.mlr.press",
            ),
            "https://proceedings.mlr.press/v291/",
        )
        self.assertEqual(
            resolve_known_grounding_redirect(
                venue_id="colt",
                year=2025,
                provider_uri=WRAPPER,
                source_domain="learningtheory.org",
            ),
            "https://learningtheory.org/colt2025/",
        )

    def test_unknown_or_unsafe_shapes_never_infer_a_url(self):
        cases = (
            ("icml", 2025, "proceedings.mlr.press", WRAPPER),
            ("colt", 2026, "proceedings.mlr.press", WRAPPER),
            ("colt", 2025, "evil.proceedings.mlr.press", WRAPPER),
            ("colt", 2025, "proceedings.mlr.press", WRAPPER + "?signed=yes"),
            (
                "colt",
                2025,
                "proceedings.mlr.press",
                "https://vertexaisearch.cloud.google.com:444/"
                "grounding-api-redirect/id",
            ),
            (
                "colt",
                2025,
                "proceedings.mlr.press",
                "https://example.test/grounding-api-redirect/id",
            ),
            ("colt", 2025, "proceedings.mlr.press", "https://[invalid/id"),
        )
        for venue_id, year, domain, uri in cases:
            with self.subTest(venue_id=venue_id, year=year, domain=domain, uri=uri):
                self.assertIsNone(resolve_known_grounding_redirect(
                    venue_id=venue_id,
                    year=year,
                    provider_uri=uri,
                    source_domain=domain,
                ))

    def test_module_has_no_network_or_effect_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        for forbidden in (
            "requests",
            "urllib3",
            "automation.execution_dispatch",
            "automation.execution_pipeline",
            "automation.live_fetch",
            "automation.local_service",
            "automation.mac_worker",
            "automation.staging_executor",
            "subprocess",
        ):
            self.assertNotIn(forbidden, imported)


if __name__ == "__main__":
    unittest.main()
