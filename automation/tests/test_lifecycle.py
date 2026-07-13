import ast
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.control_plane import ControlPlaneError, consume_verification_record
from automation.control_state import (
    ControlStateRepository,
    LeaseLostError,
)
from automation.domain import ActionType, BlockerCode, LifecycleState
from automation.lifecycle import (
    LifecycleReductionError,
    initial_conference_state,
    reduce_verification,
)
from automation.verification import (
    build_verification_request,
    build_verification_result,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase2"
MODULES = (
    Path(__file__).resolve().parents[1] / "lifecycle.py",
    Path(__file__).resolve().parents[1] / "control_plane.py",
)
NOW = datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def load_matrix():
    return json.loads(
        (FIXTURES / "lifecycle-replay.v1.json").read_text(encoding="utf-8")
    )


def venue(catalog, venue_id):
    return next(item for item in catalog["venues"] if item["venue_id"] == venue_id)


def source_url(catalog, venue_id, suffix):
    domain = venue(catalog, venue_id)["official_domains"][0]
    return f"https://{domain}/openpapers-fixture/{venue_id}/{suffix}"


def discovery(catalog, venue_id, year):
    html_url = source_url(catalog, venue_id, "index.html")
    pdf_url = source_url(catalog, venue_id, "paper.pdf")
    claims = []
    for kind in ("paper_list", "metadata", "proceedings", "pdf"):
        claims.append(
            {
                "claim_id": f"claim:{venue_id}:{year}:{kind}",
                "claim_kind": kind,
                "statement": f"Sanitized {kind} fixture for lifecycle replay.",
                "evidence_urls": [pdf_url if kind == "pdf" else html_url],
                "source_type": "official",
                "published_at": None,
            }
        )
    lifecycle_kind = venue(catalog, venue_id)["lifecycle"]["kind"]
    milestones = []
    if lifecycle_kind == "annual":
        milestones.append(
            {
                "milestone_id": f"milestone:{venue_id}:{year}:end",
                "milestone_type": "conference_end",
                "scope": "conference",
                "date": "2026-07-10",
                "evidence_urls": [html_url],
                "source_type": "official",
            }
        )
    return {
        "schema_version": 1,
        "discovery_id": f"discovery:{venue_id}:{year}:lifecycle-fixture",
        "venue_id": venue_id,
        "year": year,
        "checked_at": "2026-07-13T20:00:00Z",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "prompt_version": "v1",
        "conference_status": "unknown",
        "paper_list_status": "released",
        "metadata_status": "ready",
        "pdf_status": "ready",
        "proceedings_status": "archival",
        "claims": claims,
        "candidate_milestones": milestones,
        "confidence": 0.9,
        "uncertainties": [],
        "evidence_fingerprint": (venue_id.encode().hex() + "0" * 64)[:64],
    }


def observation(venue_id, url, *, permission, trust="official", suffix="html"):
    return {
        "source_id": f"source:{venue_id}:{suffix}",
        "url": url,
        "redirect_target_url": None,
        "source_trust": trust,
        "policy_decision": "allowed",
        "policy_domain": url.split("/", 3)[2],
        "permission": permission,
        "fetch_status": "fetched",
        "http_status": 200,
        "snapshot_id": f"snapshot:{venue_id}:{suffix}",
        "observed_at": "2026-07-13T20:30:00Z",
        "reason_code": "source_observed",
    }


def finding(request, target_id, kind, evidence_ids, *, status="verified"):
    return {
        "finding_id": f"finding:{target_id.rsplit(':', 1)[0]}:{kind}",
        "target_kind": (
            "candidate_milestone" if kind == "conference_milestone" else "claim"
        ),
        "target_id": target_id,
        "verification_kind": kind,
        "status": status,
        "source_ids": [evidence_ids[0]],
        "evidence_ids": list(evidence_ids),
        "reason_code": "supported" if status == "verified" else "conflicting_evidence",
        "metrics": {"paper_count": 3} if kind == "paper_list" else None,
    }


def bundles(catalog, venue_id, year):
    item = discovery(catalog, venue_id, year)
    html_url = source_url(catalog, venue_id, "index.html")
    html_claims = [
        f"claim:{venue_id}:{year}:{kind}"
        for kind in ("paper_list", "metadata", "proceedings")
    ]
    milestone_ids = [
        milestone["milestone_id"] for milestone in item["candidate_milestones"]
    ]
    html_request = build_verification_request(
        item,
        requested_at="2026-07-13T20:05:00Z",
        claim_ids=html_claims,
        candidate_milestone_ids=milestone_ids,
    )
    html_observation = observation(
        venue_id, html_url, permission="metadata_fetch", suffix="html"
    )
    html_evidence = (
        html_observation["source_id"],
        html_observation["snapshot_id"],
    )
    html_findings = [
        finding(html_request, claim_id, kind, html_evidence)
        for claim_id, kind in zip(
            html_claims, ("paper_list", "metadata", "proceedings"), strict=True
        )
    ]
    verified_milestones = []
    if milestone_ids:
        html_findings.append(
            finding(
                html_request,
                milestone_ids[0],
                "conference_milestone",
                html_evidence,
            )
        )
        verified_milestones.append(
            {
                "candidate_milestone_id": milestone_ids[0],
                "milestone_type": "conference_end",
                "scope": "conference",
                "date": "2026-07-10",
                "source_type": "official",
                "source_url": html_url,
                "evidence_ids": list(html_evidence),
            }
        )
    html_result = build_verification_result(
        html_request,
        item,
        overall_status="verified",
        verified_at="2026-07-13T20:30:00Z",
        source_observations=[html_observation],
        findings=html_findings,
        verified_facets={
            "conference_status": None,
            "paper_list_status": {
                "value": "released",
                "evidence_ids": list(html_evidence),
            },
            "metadata_status": {
                "value": "ready",
                "evidence_ids": list(html_evidence),
            },
            "pdf_status": None,
            "proceedings_status": {
                "value": "archival",
                "evidence_ids": list(html_evidence),
            },
        },
        verified_milestones=verified_milestones,
    )

    pdf_claim = f"claim:{venue_id}:{year}:pdf"
    pdf_request = build_verification_request(
        item,
        requested_at="2026-07-13T20:35:00Z",
        claim_ids=[pdf_claim],
        candidate_milestone_ids=[],
    )
    pdf_observation = observation(
        venue_id,
        source_url(catalog, venue_id, "paper.pdf"),
        permission="pdf_fetch_for_processing",
        suffix="pdf",
    )
    pdf_evidence = (
        pdf_observation["source_id"],
        pdf_observation["snapshot_id"],
    )
    pdf_result = build_verification_result(
        pdf_request,
        item,
        overall_status="verified",
        verified_at="2026-07-13T20:45:00Z",
        source_observations=[pdf_observation],
        findings=[finding(pdf_request, pdf_claim, "pdf", pdf_evidence)],
        verified_facets={
            "conference_status": None,
            "paper_list_status": None,
            "metadata_status": None,
            "pdf_status": {
                "value": "ready",
                "evidence_ids": list(pdf_evidence),
            },
            "proceedings_status": None,
        },
    )
    return (
        (item, html_request, html_result),
        (item, pdf_request, pdf_result),
    )


class LifecycleReducerTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_venue_catalog()
        self.policy = load_policy_config()

    def test_authoritative_facets_transition_schedule_and_route_without_effect(self):
        html_bundle, pdf_bundle = bundles(self.catalog, "icml", 2026)
        state = initial_conference_state(
            self.catalog, "icml", 2026, at="2026-07-13T20:30:00Z"
        )
        html = reduce_verification(
            state,
            *html_bundle,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(html.state["lifecycle_state"], "metadata_ready")
        self.assertEqual(html.state["facets"]["proceedings_status"], "archival")
        self.assertEqual(
            html.state["milestones"]["conference_end"]["at"],
            "2026-07-10T00:00:00Z",
        )
        self.assertEqual(
            html.state["milestones"]["paper_list_released"]["at"],
            "2026-07-13T20:30:00Z",
        )
        self.assertIn(BlockerCode.NO_PDF.value, html.state["blockers"])
        self.assertNotIn(
            ActionType.QUEUE_EXISTING_SCRAPER,
            {action.action_type for action in html.actions},
        )

        item, request, result = html_bundle
        recheck_observation = deepcopy(result["source_observations"][0])
        recheck_observation["source_id"] = "source:icml:html-recheck"
        recheck_observation["snapshot_id"] = "snapshot:icml:html-recheck"
        recheck_observation["observed_at"] = "2026-07-13T20:35:00Z"
        recheck_evidence = (
            recheck_observation["source_id"],
            recheck_observation["snapshot_id"],
        )
        recheck_findings = deepcopy(result["findings"])
        for item_finding in recheck_findings:
            item_finding["source_ids"] = [recheck_evidence[0]]
            item_finding["evidence_ids"] = list(recheck_evidence)
        recheck_facets = deepcopy(result["verified_facets"])
        for facet in recheck_facets.values():
            if facet is not None:
                facet["evidence_ids"] = list(recheck_evidence)
        recheck_milestones = deepcopy(result["verified_milestones"])
        for milestone in recheck_milestones:
            milestone["evidence_ids"] = list(recheck_evidence)
        recheck_result = build_verification_result(
            request,
            item,
            overall_status="verified",
            verified_at="2026-07-13T20:35:00Z",
            source_observations=[recheck_observation],
            findings=recheck_findings,
            verified_facets=recheck_facets,
            verified_milestones=recheck_milestones,
        )
        html_recheck = reduce_verification(
            html.state,
            item,
            request,
            recheck_result,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertNotIn(
            BlockerCode.HUMAN_REVIEW_REQUIRED.value,
            html_recheck.state["blockers"],
        )
        self.assertEqual(
            html_recheck.state["milestones"]["paper_list_released"]["at"],
            "2026-07-13T20:30:00Z",
        )

        pdf = reduce_verification(
            html_recheck.state,
            *pdf_bundle,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(pdf.state["lifecycle_state"], "pdf_ready")
        self.assertEqual(pdf.state["blockers"], [])
        action_types = [action.action_type for action in pdf.actions]
        self.assertIn(ActionType.RECHECK_AT, action_types)
        self.assertIn(ActionType.NOTIFY_TRANSITION, action_types)
        self.assertIn(ActionType.QUEUE_EXISTING_SCRAPER, action_types)
        queue = next(
            action
            for action in pdf.actions
            if action.action_type is ActionType.QUEUE_EXISTING_SCRAPER
        )
        serialized = queue.as_dict()
        self.assertEqual(serialized["payload"]["readiness"], "pdf_ready")
        serialized_text = json.dumps(serialized).lower()
        for forbidden in ("command", "job", "submitted", "executed"):
            self.assertNotIn(forbidden, serialized_text)

        replay = reduce_verification(
            pdf.state,
            *pdf_bundle,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertFalse(replay.consumed)
        self.assertEqual(replay.state, pdf.state)
        self.assertEqual(replay.actions, ())

        item, request, result = pdf_bundle
        partial_facets = deepcopy(result["verified_facets"])
        partial_facets["pdf_status"]["value"] = "partial"
        partial = build_verification_result(
            request,
            item,
            overall_status="verified",
            verified_at="2026-07-13T20:50:00Z",
            source_observations=result["source_observations"],
            findings=result["findings"],
            verified_facets=partial_facets,
        )
        older_readiness = reduce_verification(
            pdf.state,
            item,
            request,
            partial,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(older_readiness.state["facets"]["pdf_status"], "ready")
        self.assertNotIn(
            ActionType.QUEUE_EXISTING_SCRAPER,
            {action.action_type for action in older_readiness.actions},
        )

    def test_untrusted_positive_or_conflicting_evidence_never_queues(self):
        _, pdf_bundle = bundles(self.catalog, "icml", 2026)
        item, request, result = pdf_bundle
        untrusted = deepcopy(result)
        untrusted["source_observations"][0]["source_trust"] = "untrusted"
        rebuilt = build_verification_result(
            request,
            item,
            overall_status="verified",
            verified_at=result["verified_at"],
            source_observations=untrusted["source_observations"],
            findings=untrusted["findings"],
            verified_facets=untrusted["verified_facets"],
        )
        state = initial_conference_state(
            self.catalog, "icml", 2026, at=result["verified_at"]
        )
        outcome = reduce_verification(
            state,
            item,
            request,
            rebuilt,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(outcome.state["lifecycle_state"], "unknown")
        self.assertIn(
            BlockerCode.HUMAN_REVIEW_REQUIRED.value, outcome.state["blockers"]
        )
        self.assertNotIn(
            ActionType.QUEUE_EXISTING_SCRAPER,
            {action.action_type for action in outcome.actions},
        )

        observation_item = result["source_observations"]
        conflict_finding = deepcopy(result["findings"][0])
        conflict_finding["status"] = "conflicting"
        conflict_finding["reason_code"] = "conflicting_evidence"
        conflicting = build_verification_result(
            request,
            item,
            overall_status="conflicting",
            verified_at="2026-07-13T20:46:00Z",
            source_observations=observation_item,
            findings=[conflict_finding],
            verified_facets=result["verified_facets"],
        )
        conflict = reduce_verification(
            state,
            item,
            request,
            conflicting,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(conflict.state["lifecycle_state"], "pdf_ready")
        self.assertIn(
            BlockerCode.HUMAN_REVIEW_REQUIRED.value, conflict.state["blockers"]
        )
        self.assertNotIn(
            ActionType.QUEUE_EXISTING_SCRAPER,
            {action.action_type for action in conflict.actions},
        )
        unresolved = reduce_verification(
            conflict.state,
            item,
            request,
            result,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertIn(
            BlockerCode.HUMAN_REVIEW_REQUIRED.value,
            unresolved.state["blockers"],
        )
        self.assertNotIn(
            ActionType.QUEUE_EXISTING_SCRAPER,
            {action.action_type for action in unresolved.actions},
        )

    def test_semantically_valid_v1_bundle_remains_replayable(self):
        discovery_v1 = json.loads(
            (FIXTURES.parent / "phase0" / "discovery-result.v1.json").read_text(
                encoding="utf-8"
            )
        )
        request_v1 = json.loads(
            (FIXTURES / "verification-request.v1.json").read_text(encoding="utf-8")
        )
        result_v1 = json.loads(
            (FIXTURES / "verification-result.v1.json").read_text(encoding="utf-8")
        )
        state = initial_conference_state(
            self.catalog, "icml", 2026, at=result_v1["verified_at"]
        )
        outcome = reduce_verification(
            state,
            discovery_v1,
            request_v1,
            result_v1,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertEqual(outcome.state["lifecycle_state"], "scheduled")
        self.assertEqual(
            outcome.state["milestones"]["conference_end"]["at"],
            "2026-07-18T00:00:00Z",
        )

    def test_continuous_venue_rejects_conference_facet_and_identity_mismatch(self):
        _, pdf_bundle = bundles(self.catalog, "jmlr", 2026)
        item, _, result = pdf_bundle
        item = deepcopy(item)
        conference_claim = "claim:jmlr:2026:conference"
        item["claims"].append(
            {
                "claim_id": conference_claim,
                "claim_kind": "conference",
                "statement": "Sanitized unsupported conference fixture.",
                "evidence_urls": [result["source_observations"][0]["url"]],
                "source_type": "official",
                "published_at": None,
            }
        )
        item["conference_status"] = "ended"
        pdf_claim = "claim:jmlr:2026:pdf"
        request = build_verification_request(
            item,
            requested_at="2026-07-13T20:35:00Z",
            claim_ids=[conference_claim, pdf_claim],
            candidate_milestone_ids=[],
        )
        forged_facets = deepcopy(result["verified_facets"])
        forged_facets["conference_status"] = {
            "value": "ended",
            "evidence_ids": deepcopy(
                result["verified_facets"]["pdf_status"]["evidence_ids"]
            ),
        }
        forged = build_verification_result(
            request,
            item,
            overall_status="verified",
            verified_at=result["verified_at"],
            source_observations=result["source_observations"],
            findings=[
                finding(
                    request,
                    conference_claim,
                    "source_identity",
                    (
                        result["source_observations"][0]["source_id"],
                        result["source_observations"][0]["snapshot_id"],
                    ),
                ),
                finding(
                    request,
                    pdf_claim,
                    "pdf",
                    (
                        result["source_observations"][0]["source_id"],
                        result["source_observations"][0]["snapshot_id"],
                    ),
                ),
            ],
            verified_facets=forged_facets,
        )
        state = initial_conference_state(
            self.catalog, "jmlr", 2026, at=result["verified_at"]
        )
        outcome = reduce_verification(
            state,
            item,
            request,
            forged,
            catalog=self.catalog,
            policy=self.policy,
        )
        self.assertNotEqual(
            outcome.state["lifecycle_state"], LifecycleState.CONFERENCE_ENDED.value
        )
        self.assertEqual(outcome.state["facets"]["conference_status"], "unknown")
        self.assertIn(
            BlockerCode.HUMAN_REVIEW_REQUIRED.value, outcome.state["blockers"]
        )

        wrong_state = deepcopy(state)
        wrong_state["venue_id"] = "icml"
        with self.assertRaisesRegex(LifecycleReductionError, "identity"):
            reduce_verification(
                wrong_state,
                item,
                request,
                forged,
                catalog=self.catalog,
                policy=self.policy,
            )


class PersistentReplayTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_venue_catalog()
        self.policy = load_policy_config()
        self.matrix = load_matrix()

    def _run_replay(self, root):
        clock = MutableClock()
        with ControlStateRepository(root / "state.sqlite3", clock=clock) as store:
            lease = store.acquire_lease("p2-5-fixture-replay", ttl_seconds=3600)
            for fixture_venue in self.matrix["venues"]:
                for offset, bundle in enumerate(
                    bundles(
                        self.catalog,
                        fixture_venue["venue_id"],
                        self.matrix["year"],
                    )
                ):
                    store.accept_verification(
                        *bundle,
                        lease=lease,
                        received_at=NOW + timedelta(seconds=offset),
                    )
            action_ids = []
            for record in store.replay_verifications():
                consumed = consume_verification_record(
                    store,
                    record,
                    catalog=self.catalog,
                    policy=self.policy,
                    lease=lease,
                )
                action_ids.extend(
                    action.action_id for action in consumed.reduction.actions
                )
            states = {}
            for fixture_venue in self.matrix["venues"]:
                venue_id = fixture_venue["venue_id"]
                current = store.get_conference_state(venue_id, self.matrix["year"])
                states[venue_id] = current.state
                self.assertEqual(current.state["lifecycle_state"], "pdf_ready")
                self.assertEqual(len(store.conference_state_history(
                    venue_id, self.matrix["year"]
                )), 2)
            replay_outcomes = [
                consume_verification_record(
                    store,
                    record,
                    catalog=self.catalog,
                    policy=self.policy,
                    lease=lease,
                )
                for record in store.replay_verifications()
            ]
            self.assertTrue(
                all(not item.reduction.consumed for item in replay_outcomes)
            )
            self.assertTrue(
                all(not item.state_write.applied for item in replay_outcomes)
            )
            return states, action_ids

    def test_every_catalog_venue_and_lifecycle_shape_replays_identically(self):
        catalog_shapes = {
            item["venue_id"]: item["lifecycle"]["kind"]
            for item in self.catalog["venues"]
        }
        fixture_shapes = {
            item["venue_id"]: item["lifecycle_kind"]
            for item in self.matrix["venues"]
        }
        self.assertEqual(fixture_shapes, catalog_shapes)
        with tempfile.TemporaryDirectory() as first_directory:
            first = self._run_replay(Path(first_directory))
        with tempfile.TemporaryDirectory() as second_directory:
            second = self._run_replay(Path(second_directory))
        self.assertEqual(first, second)
        states, action_ids = first
        self.assertIsNone(states["jmlr"]["milestones"]["conference_end"])
        queue_actions = {
            action.action_id
            for fixture_venue in self.matrix["venues"]
            for bundle in [bundles(
                self.catalog, fixture_venue["venue_id"], self.matrix["year"]
            )[1]]
            for action in reduce_verification(
                initial_conference_state(
                    self.catalog,
                    fixture_venue["venue_id"],
                    self.matrix["year"],
                    at=bundle[2]["verified_at"],
                ),
                *bundle,
                catalog=self.catalog,
                policy=self.policy,
            ).actions
            if action.action_type is ActionType.QUEUE_EXISTING_SCRAPER
        }
        self.assertEqual(len(queue_actions), len(self.matrix["venues"]))
        self.assertTrue(queue_actions.issubset(set(action_ids)))

    def test_coordinator_requires_a_live_repository_lease(self):
        bundle = bundles(self.catalog, "icml", 2026)[0]
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as store:
                lease = store.acquire_lease("p2-5-expiry", ttl_seconds=1)
                store.accept_verification(
                    *bundle, lease=lease, received_at=NOW
                )
                record = store.replay_verifications()[0]
                with self.assertRaisesRegex(ControlPlaneError, "retained"):
                    consume_verification_record(
                        store,
                        replace(record, received_at="2026-07-13T21:00:01Z"),
                        catalog=self.catalog,
                        policy=self.policy,
                        lease=lease,
                    )
                self.assertIsNone(store.get_conference_state("icml", 2026))
                clock.value += timedelta(seconds=1)
                with self.assertRaises(LeaseLostError):
                    consume_verification_record(
                        store,
                        record,
                        catalog=self.catalog,
                        policy=self.policy,
                        lease=lease,
                    )
                self.assertIsNone(store.get_conference_state("icml", 2026))


class ScopeBoundaryTests(unittest.TestCase):
    def test_modules_have_no_network_or_effectful_runtime_dependency(self):
        imports = set()
        for module in MODULES:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            imports.update(
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            )
            imports.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
        roots = {name.split(".", 1)[0] for name in imports}
        self.assertTrue(
            {
                "requests",
                "urllib3",
                "prefect",
                "google",
                "subprocess",
                "scrapers",
            }.isdisjoint(roots)
        )


if __name__ == "__main__":
    unittest.main()
