import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.configuration import load_venue_catalog
from automation.discovery import ProviderError, request_from_catalog
from automation.providers.gemini import GeminiSearchGroundingProvider


FIXTURE = (
    Path(__file__).with_name("fixtures")
    / "phase1"
    / "gemini-grounded-response.v1.json"
)


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)
        self.closed = False

    def close(self):
        self.closed = True


def sdk_response(*, include_metadata=True, text=None, usage_metadata=None):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    chunks = [
        SimpleNamespace(web=SimpleNamespace(**source))
        for source in fixture["grounding_sources"]
    ]
    metadata = SimpleNamespace(
        grounding_chunks=chunks,
        web_search_queries=fixture["search_queries"],
        grounding_supports=[
            SimpleNamespace(
                segment=SimpleNamespace(
                    text=("ICML 2026 is scheduled for July 13 through "
                          "July 18, 2026."),
                ),
                grounding_chunk_indices=[0],
            )
        ],
    )
    candidate = SimpleNamespace(
        grounding_metadata=metadata if include_metadata else None)
    return SimpleNamespace(
        parsed=None,
        text=text if text is not None else json.dumps(fixture["body"]),
        candidates=[candidate],
        usage_metadata=usage_metadata,
    )


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.request = request_from_catalog(
            load_venue_catalog(), "icml", 2026)

    def test_adapter_requests_structured_google_search_grounding(self):
        client = FakeClient(sdk_response())
        provider = GeminiSearchGroundingProvider(client, "fixture-gemini")
        response = provider.discover(self.request)

        self.assertEqual(response.body["venue_id"], "icml")
        self.assertEqual(response.body["year"], 2026)
        self.assertEqual(response.grounding_sources[0].domain, "icml.cc")
        self.assertEqual(len(client.models.calls), 2)
        search_call, structure_call = client.models.calls
        self.assertEqual(search_call["model"], "fixture-gemini")
        self.assertIn("exact venue ID is icml", search_call["contents"])
        self.assertIn("exact conference year is 2026", search_call["contents"])
        self.assertIn("lifecycle kind: annual", search_call["contents"])
        search_config = search_call["config"]
        self.assertIsNone(search_config.response_mime_type)
        self.assertEqual(len(search_config.tools), 1)
        self.assertIsNotNone(search_config.tools[0].google_search)

        self.assertIn('"allowed_evidence_sources"',
                      structure_call["contents"])
        self.assertIn('"source_id": "s1"',
                      structure_call["contents"])
        self.assertIn('"allowed_source_type": "official"',
                      structure_call["contents"])
        self.assertIn('"grounded_excerpts"', structure_call["contents"])
        self.assertIn('"source_ids": ["s1"]',
                      structure_call["contents"])
        self.assertIn("do not copy URI values", structure_call["contents"])
        structure_config = structure_call["config"]
        self.assertIn(
            "publicly accessible now",
            str(structure_config.system_instruction),
        )
        self.assertEqual(structure_config.response_mime_type,
                         "application/json")
        self.assertIsNotNone(structure_config.response_json_schema)
        self.assertIsNone(structure_config.tools)
        serialized_schema = json.dumps(structure_config.response_json_schema)
        self.assertNotIn("additionalProperties", serialized_schema)
        self.assertNotIn("maxItems", serialized_schema)
        self.assertIn("candidate_milestones",
                      structure_config.response_json_schema["properties"])
        self.assertEqual(structure_config.thinking_config.thinking_budget, 0)
        self.assertEqual(provider.prompt_version, "v14")
        self.assertEqual(provider.attempt_cost, 2)

    def test_missing_grounding_and_malformed_json_fail_closed(self):
        cases = [
            (sdk_response(include_metadata=False), "grounding metadata"),
            (sdk_response(text="not json"), "malformed structured output"),
        ]
        for response, message in cases:
            provider = GeminiSearchGroundingProvider(FakeClient(response))
            with self.subTest(message=message), self.assertRaisesRegex(
                    ProviderError, message) as raised:
                provider.discover(self.request)
            if message == "malformed structured output":
                self.assertEqual(
                    raised.exception.diagnostics["text_shape"], "other")

    def test_malformed_diagnostics_use_secret_safe_usage_count_names(self):
        response = sdk_response(
            text='{"venue_id": "unterminated',
            usage_metadata=SimpleNamespace(
                prompt_token_count=123,
                candidates_token_count=456,
                thoughts_token_count=789,
            ),
        )
        provider = GeminiSearchGroundingProvider(FakeClient(response))

        with self.assertRaises(ProviderError) as raised:
            provider.discover(self.request)

        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["input_token_count"], 123)
        self.assertEqual(diagnostics["output_token_count"], 456)
        self.assertEqual(diagnostics["internal_reasoning_token_count"], 789)
        self.assertNotIn("prompt_tokens", diagnostics)
        self.assertNotIn("candidate_tokens", diagnostics)

    def test_single_json_code_fence_is_parsed_but_extra_prose_is_rejected(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fenced_text = f"```json\n{json.dumps(fixture['body'])}\n```"
        provider = GeminiSearchGroundingProvider(
            FakeClient(sdk_response(text=fenced_text)))
        self.assertEqual(provider.discover(self.request).body["venue_id"],
                         "icml")

        prose = f"Here is the result:\n{fenced_text}"
        with self.assertRaisesRegex(ProviderError, "malformed"):
            GeminiSearchGroundingProvider(
                FakeClient(sdk_response(text=prose))).discover(self.request)

    def test_direct_url_maps_to_unique_grounding_redirect_by_domain(self):
        response = sdk_response()
        metadata = response.candidates[0].grounding_metadata
        metadata.grounding_chunks[0].web.uri = (
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/id")
        provider_response = GeminiSearchGroundingProvider(
            FakeClient(response)).discover(self.request)
        redirect = metadata.grounding_chunks[0].web.uri
        self.assertEqual(
            provider_response.body["claims"][0]["evidence_urls"],
            [redirect],
        )
        self.assertEqual(
            provider_response.body["candidate_milestones"][0]
            ["evidence_urls"],
            [redirect],
        )

    def test_short_source_ids_map_to_exact_grounding_uris(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["body"]["claims"][0]["evidence_urls"] = ["s1"]
        fixture["body"]["candidate_milestones"][0]["evidence_urls"] = [
            "s1"]
        response = sdk_response(text=json.dumps(fixture["body"]))

        provider_response = GeminiSearchGroundingProvider(
            FakeClient(response)).discover(self.request)

        expected = fixture["grounding_sources"][0]["uri"]
        self.assertEqual(
            provider_response.body["claims"][0]["evidence_urls"],
            [expected],
        )
        self.assertEqual(
            provider_response.body["candidate_milestones"][0]
            ["evidence_urls"],
            [expected],
        )

    def test_source_type_is_derived_from_cited_catalog_domains(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["body"]["claims"][0]["source_type"] = "secondary"
        fixture["body"]["candidate_milestones"][0]["source_type"] = (
            "secondary")
        response = sdk_response(text=json.dumps(fixture["body"]))

        provider_response = GeminiSearchGroundingProvider(
            FakeClient(response)).discover(self.request)

        self.assertEqual(
            provider_response.body["claims"][0]["source_type"], "official")
        self.assertEqual(
            provider_response.body["candidate_milestones"][0]["source_type"],
            "official",
        )

    def test_unsupported_status_is_downgraded_to_unknown(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["body"]["pdf_status"] = "ready"
        response = sdk_response(text=json.dumps(fixture["body"]))

        provider_response = GeminiSearchGroundingProvider(
            FakeClient(response)).discover(self.request)

        self.assertEqual(provider_response.body["pdf_status"], "unknown")
        self.assertTrue(any(
            "pdf_status was downgraded" in uncertainty
            for uncertainty in provider_response.body["uncertainties"]
        ))

    def test_environment_construction_requires_project_without_api_key(self):
        with self.assertRaisesRegex(ProviderError, "GCP_PROJECT_ID") as raised:
            GeminiSearchGroundingProvider.from_environment({})
        self.assertEqual(raised.exception.category,
                         "configuration_missing_project")

    def test_automation_defaults_do_not_inherit_core_scraper_model(self):
        with patch("google.genai.Client") as client_class:
            provider = GeminiSearchGroundingProvider.from_environment({
                "GCP_PROJECT_ID": "fixture-project",
                "GEMINI_MODEL": "unrelated-core-model",
                "GCP_LOCATION": "unrelated-core-location",
            })
        self.assertEqual(provider.model, "gemini-2.5-flash")
        self.assertEqual(client_class.call_args.kwargs["location"], "global")

    def test_automation_specific_model_and_location_can_be_overridden(self):
        with patch("google.genai.Client") as client_class:
            provider = GeminiSearchGroundingProvider.from_environment({
                "GCP_PROJECT_ID": "fixture-project",
                "AUTOMATION_GEMINI_MODEL": "fixture-model",
                "AUTOMATION_GEMINI_LOCATION": "us-central1",
            })
        self.assertEqual(provider.model, "fixture-model")
        self.assertEqual(client_class.call_args.kwargs["location"],
                         "us-central1")

    def test_close_releases_sdk_client(self):
        client = FakeClient(sdk_response())
        GeminiSearchGroundingProvider(client).close()
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
