import unittest

from automation.configuration import load_venue_catalog
from automation.discovery import (
    DiscoveryError,
    DiscoveryRequest,
    DiscoveryValidationError,
    ProviderError,
    RetryableProviderError,
    request_from_catalog,
    safe_error_summary,
)


class RequestFromCatalogTests(unittest.TestCase):
    def test_resolves_a_known_venue(self):
        request = request_from_catalog(load_venue_catalog(), "icml", 2026)

        self.assertIsInstance(request, DiscoveryRequest)
        self.assertEqual(request.venue_id, "icml")
        self.assertEqual(request.year, 2026)
        self.assertIn("icml.cc", request.official_domains)

    def test_unknown_venue_is_rejected(self):
        with self.assertRaisesRegex(DiscoveryValidationError, "unknown venue_id"):
            request_from_catalog(load_venue_catalog(), "not-a-venue", 2026)


class SafeErrorSummaryTests(unittest.TestCase):
    def test_provider_error_exposes_only_category_and_status(self):
        error = RetryableProviderError(
            "response text that must not be exposed",
            category="api_transient",
            status_code=503,
            diagnostics={"text_length": 42, "text_shape": "object"},
        )
        self.assertEqual(safe_error_summary(error), "api_transient:http_503")
        self.assertNotIn("response", safe_error_summary(error))

    def test_provider_error_without_a_status_code_omits_the_suffix(self):
        error = ProviderError("fixture", category="malformed_output")
        self.assertEqual(safe_error_summary(error), "malformed_output")

    def test_validation_error_uses_its_category(self):
        error = DiscoveryValidationError("fixture", category="venue_year_mismatch")
        self.assertEqual(safe_error_summary(error), "venue_year_mismatch")

    def test_unrecognized_error_falls_back_to_the_type_name(self):
        self.assertEqual(
            safe_error_summary(DiscoveryError("fixture")), "DiscoveryError"
        )


if __name__ == "__main__":
    unittest.main()
