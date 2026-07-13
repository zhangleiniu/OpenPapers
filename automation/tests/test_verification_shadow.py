import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from automation.configuration import load_venue_catalog
from automation.verification import FetchResponse
from automation.verification_shadow import (
    ShadowVerificationError,
    latest_discovery_artifacts,
    load_discovery_artifact,
    load_shadow_policy,
    plan_shadow_targets,
    prepare_shadow_root,
    run_shadow_review,
)


FIXTURE = (
    Path(__file__).with_name("fixtures")
    / "phase2"
    / "html"
    / "ijcai-2026-accepted.html"
)
NOW = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/fixture"
PDF_REDIRECT = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/pdf-fixture"
)
FINAL = "https://2026.ijcai.org/accepted-papers/"


def discovery_artifact(*, include_pdf=False):
    claims = [{
        "claim_id": "claim:ijcai:2026:conference",
        "claim_kind": "conference",
        "statement": "IJCAI 2026 occurs in August.",
        "evidence_urls": [REDIRECT],
        "source_type": "official",
        "published_at": None,
    }]
    sources = [{"uri": REDIRECT, "domain": "ijcai.org", "title": "IJCAI"}]
    if include_pdf:
        claims.append({
            "claim_id": "claim:ijcai:2026:pdf",
            "claim_kind": "pdf",
            "statement": "IJCAI 2026 PDFs are ready.",
            "evidence_urls": [PDF_REDIRECT],
            "source_type": "official",
            "published_at": None,
        })
        sources.append({
            "uri": PDF_REDIRECT,
            "domain": "ijcai.org",
            "title": "IJCAI PDFs",
        })
    result = {
        "schema_version": 1,
        "discovery_id": "discovery:ijcai:2026:p2s-fixture",
        "venue_id": "ijcai",
        "year": 2026,
        "checked_at": "2026-07-13T20:00:00Z",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "prompt_version": "v1",
        "conference_status": "scheduled",
        "paper_list_status": "unknown",
        "metadata_status": "unknown",
        "pdf_status": "ready" if include_pdf else "unknown",
        "proceedings_status": "unknown",
        "claims": claims,
        "candidate_milestones": [{
            "milestone_id": "milestone:ijcai:2026:start",
            "milestone_type": "conference_start",
            "scope": "conference",
            "date": "2026-08-15",
            "evidence_urls": [REDIRECT],
            "source_type": "official",
        }],
        "confidence": 0.9,
        "uncertainties": [],
        "evidence_fingerprint": "a" * 64,
    }
    return {
        "artifact_version": 1,
        "request_fingerprint": "b" * 64,
        "provider_role": "primary",
        "result": result,
        "grounding": {"sources": sources, "search_queries": ["fixture"]},
    }


def write_artifact(root, payload, *, suffix="a"):
    path = root / "artifacts" / "fixture-provider" / "ijcai" / f"2026-{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class MappingFetcher:
    def __init__(self):
        self.requests = []

    def fetch(self, request):
        self.requests.append(request)
        if request.url == REDIRECT:
            return FetchResponse(
                requested_url=REDIRECT,
                status_code=302,
                headers={"Location": FINAL, "Content-Type": "text/html"},
                body=b"",
                fetched_at="2026-07-13T22:00:00Z",
            )
        if request.url == FINAL:
            return FetchResponse(
                requested_url=FINAL,
                status_code=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=FIXTURE.read_bytes(),
                fetched_at="2026-07-13T22:00:01Z",
            )
        raise AssertionError(f"unexpected request: {request.url}")


class ShadowPolicyTests(unittest.TestCase):
    def test_policy_is_separate_conservative_and_grants_no_redistribution(self):
        policy = load_shadow_policy()
        domains = {item["domain"]: item for item in policy["crawl"]["domains"]}
        self.assertEqual(domains["ecva.net"]["classification"], "review_required")
        self.assertEqual(domains["ecva.net"]["allowed_permissions"], [])
        self.assertEqual(domains["vertexaisearch.cloud.google.com"]["classification"], "approved")
        for item in domains.values():
            self.assertNotIn("redistribute_pdf", item["allowed_permissions"])
            self.assertNotIn("redistribute_metadata", item["allowed_permissions"])
            self.assertEqual(item["max_concurrency"], 1)

    def test_unknown_shadow_policy_fields_and_permissions_fail_closed(self):
        source = Path(__file__).resolve().parents[1] / "config" / "p2s_shadow_policy.v1.json"
        review = json.loads(source.read_text(encoding="utf-8"))
        cases = []
        unknown = deepcopy(review)
        unknown["production"] = True
        cases.append(unknown)
        redistribution = deepcopy(review)
        redistribution["domains"][0]["allowed_permissions"].append("redistribute_pdf")
        cases.append(redistribution)
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(cases):
                path = Path(directory) / f"policy-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(ShadowVerificationError):
                    load_shadow_policy(path)


class DiscoveryPlanningTests(unittest.TestCase):
    def test_latest_artifact_and_catalog_bounded_targets_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = discovery_artifact(include_pdf=True)
            newer = deepcopy(older)
            newer["result"]["checked_at"] = "2026-07-13T21:00:00Z"
            newer["result"]["evidence_fingerprint"] = "c" * 64
            write_artifact(root, older, suffix="older")
            selected_path = write_artifact(root, newer, suffix="newer")
            selected = latest_discovery_artifacts(
                root, venue_ids=["ijcai"], year=2026
            )["ijcai"]
            self.assertEqual(selected.path, selected_path.resolve())
            targets = plan_shadow_targets(selected, load_venue_catalog())
            self.assertEqual(
                [(item.verification_kind, item.selected_urls) for item in targets],
                [
                    ("conference_milestone", (REDIRECT,)),
                    ("pdf", (PDF_REDIRECT,)),
                ],
            )

    def test_missing_grounding_and_unrelated_sources_fail_or_stay_unselected(self):
        payload = discovery_artifact()
        payload["grounding"]["sources"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = write_artifact(Path(directory), payload)
            with self.assertRaises(ShadowVerificationError):
                load_discovery_artifact(path)


class ShadowRunTests(unittest.TestCase):
    def test_fixture_run_writes_only_isolated_state_snapshots_and_inert_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            discovery = base / "discovery"
            output = base / "p2s-shadow"
            write_artifact(discovery, discovery_artifact())
            fetcher = MappingFetcher()
            summary = run_shadow_review(
                discovery_root=discovery,
                output_root=output,
                venue_ids=["ijcai"],
                year=2026,
                fetcher=fetcher,
                observed_at=NOW,
            )
            self.assertEqual([item.url for item in fetcher.requests], [REDIRECT, FINAL])
            self.assertEqual(summary["venue_count"], 1)
            self.assertEqual(summary["effects"], {
                "jobs_created": 0,
                "scrapers_executed": 0,
                "notifications_sent": 0,
                "production_state_writes": 0,
            })
            target = summary["venues"][0]["targets"][0]
            self.assertEqual(target["overall_status"], "verified")
            self.assertEqual(target["verified_milestone_count"], 1)
            self.assertEqual(
                summary["venues"][0]["shadow_state"]["lifecycle_state"],
                "scheduled",
            )
            self.assertTrue((output / "control" / "state.sqlite3").is_file())
            self.assertTrue((output / "shadow-summary.v1.json").is_file())
            self.assertTrue(any((output / "snapshots" / "manifests").glob("*.json")))

            class BombFetcher:
                def fetch(self, request):
                    raise AssertionError("completed replay must not fetch")

            replay = run_shadow_review(
                discovery_root=discovery,
                output_root=output,
                venue_ids=["ijcai"],
                year=2026,
                fetcher=BombFetcher(),
                observed_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            )
            self.assertEqual(replay, summary)

            summary_path = output / "shadow-summary.v1.json"
            tampered = json.loads(summary_path.read_text(encoding="utf-8"))
            tampered["effects"]["jobs_created"] = 1
            summary_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(ShadowVerificationError):
                run_shadow_review(
                    discovery_root=discovery,
                    output_root=output,
                    venue_ids=["ijcai"],
                    year=2026,
                    fetcher=BombFetcher(),
                    observed_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
                )

    def test_live_fetch_failure_becomes_review_data_without_an_effect(self):
        class FailingFetcher:
            def fetch(self, request):
                from automation.verification import FetchBoundaryError
                raise FetchBoundaryError("fixture transport failure")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            discovery = base / "discovery"
            output = base / "shadow-failure"
            write_artifact(discovery, discovery_artifact())
            summary = run_shadow_review(
                discovery_root=discovery,
                output_root=output,
                venue_ids=["ijcai"],
                year=2026,
                fetcher=FailingFetcher(),
                observed_at=NOW,
            )
            target = summary["venues"][0]["targets"][0]
            self.assertEqual(target["overall_status"], "review_required")
            self.assertEqual(
                target["fetch_errors"], [{"error_type": "FetchBoundaryError"}]
            )
            self.assertEqual(summary["effects"]["jobs_created"], 0)
            self.assertEqual(summary["effects"]["production_state_writes"], 0)

    def test_roots_must_be_isolated_and_existing_unmarked_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ShadowVerificationError):
                prepare_shadow_root(root / "discovery" / "output", root / "discovery", NOW)
            with self.assertRaises(ShadowVerificationError):
                prepare_shadow_root(root / "plain-output", root / "discovery", NOW)
            output = root / "output"
            output.mkdir()
            (output / "unrelated.txt").write_text("user data", encoding="utf-8")
            with self.assertRaises(ShadowVerificationError):
                prepare_shadow_root(output, root / "discovery", NOW)


if __name__ == "__main__":
    unittest.main()
