import ast
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from automation.configuration import (
    DEFAULT_POLICY_CONFIG,
    DEFAULT_VENUE_CATALOG,
    load_policy_config,
    load_venue_catalog,
)
from automation.contracts import (
    ContractName,
    ContractValidationError,
    artifact_fingerprint,
    load_schema,
    validate_contract,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase0"
PHASE2_FIXTURES = Path(__file__).with_name("fixtures") / "phase2"
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        path = PHASE2_FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8"))


class ContractTests(unittest.TestCase):
    VALID_FIXTURES = {
        ContractName.DISCOVERY_RESULT: "discovery-result.v1.json",
        ContractName.VERIFICATION_REQUEST: "verification-request.v2.json",
        ContractName.VERIFICATION_RESULT: "verification-result.v2.json",
        ContractName.CONFERENCE_STATE: "conference-state.v1.json",
        ContractName.CASE_STATE: "case-state.v1.json",
        ContractName.NOTIFICATION_INTENT: "notification-intent.v1.json",
        ContractName.JOB: "scrape-job.v1.json",
        ContractName.JOB_RESULT: "job-result.v1.json",
        ContractName.CODEX_RESULT: "codex-result.v1.json",
    }

    def test_every_schema_self_checks_and_valid_fixture_passes(self):
        for contract in ContractName:
            with self.subTest(contract=contract.value):
                self.assertEqual(load_schema(contract)["$schema"],
                                 "https://json-schema.org/draft/2020-12/schema")
        for contract, fixture_name in self.VALID_FIXTURES.items():
            with self.subTest(contract=contract.value):
                validate_contract(contract, load_fixture(fixture_name))
        for contract, fixture_name in (
            (ContractName.VERIFICATION_REQUEST, "verification-request.v1.json"),
            (ContractName.VERIFICATION_RESULT, "verification-result.v1.json"),
        ):
            with self.subTest(contract=contract.value, version=1):
                self.assertEqual(load_schema(contract, 2)["$schema"],
                                 "https://json-schema.org/draft/2020-12/schema")
                validate_contract(contract, load_fixture(fixture_name))

    def test_missing_and_unknown_execution_fields_are_rejected(self):
        for contract, fixture_name in self.VALID_FIXTURES.items():
            payload = load_fixture(fixture_name)
            required_field = next(
                field for field in load_schema(contract)["required"]
                if field != "schema_version")
            del payload[required_field]
            with self.subTest(contract=contract.value, kind="missing"), \
                    self.assertRaises(ContractValidationError):
                validate_contract(contract, payload)

            payload = load_fixture(fixture_name)
            payload["unexpected_execution_field"] = True
            with self.subTest(contract=contract.value, kind="unknown"), \
                    self.assertRaises(ContractValidationError):
                validate_contract(contract, payload)

        job = load_fixture("scrape-job.v1.json")
        job["payload"]["command"] = "python main.py icml 2026"
        with self.assertRaisesRegex(ContractValidationError, "command"):
            validate_contract(ContractName.JOB, job)

    def test_discovery_cannot_carry_an_action_or_command(self):
        discovery = load_fixture("discovery-result.v1.json")
        for field in ("action", "command"):
            candidate = deepcopy(discovery)
            candidate[field] = "queue_existing_scraper"
            with self.subTest(field=field), self.assertRaises(
                    ContractValidationError):
                validate_contract(ContractName.DISCOVERY_RESULT, candidate)

        candidate = load_fixture("discovery-result.v1.json")
        candidate["candidate_milestones"][0]["verified"] = True
        with self.assertRaises(ContractValidationError):
            validate_contract(ContractName.DISCOVERY_RESULT, candidate)

        for contract, fixture_name in (
            (ContractName.VERIFICATION_REQUEST, "verification-request.v1.json"),
            (ContractName.VERIFICATION_RESULT, "verification-result.v1.json"),
        ):
            for field in ("action", "command", "job", "transition"):
                candidate = load_fixture(fixture_name)
                candidate[field] = "queue_existing_scraper"
                with self.subTest(contract=contract, field=field), \
                        self.assertRaises(ContractValidationError):
                    validate_contract(contract, candidate)

    def test_invalid_datetime_and_unknown_schema_version_are_rejected(self):
        discovery = load_fixture("discovery-result.v1.json")
        discovery["checked_at"] = "sometime later"
        with self.assertRaisesRegex(ContractValidationError, "date-time"):
            validate_contract(ContractName.DISCOVERY_RESULT, discovery)

        discovery = load_fixture("discovery-result.v1.json")
        discovery["schema_version"] = 2
        with self.assertRaisesRegex(ContractValidationError, "unsupported"):
            validate_contract(ContractName.DISCOVERY_RESULT, discovery)

    def test_artifact_fingerprint_is_order_independent(self):
        first = {"schema_version": 1, "a": [1, 2], "b": "value"}
        second = {"b": "value", "a": [1, 2], "schema_version": 1}
        self.assertEqual(artifact_fingerprint(first),
                         artifact_fingerprint(second))


class ConfigurationTests(unittest.TestCase):
    def test_catalog_covers_every_registered_core_scraper(self):
        tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        imports = {
            alias.asname or alias.name: node.module
            for node in tree.body if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        scraper_registry = None
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "SCRAPERS" for target in node.targets)):
                scraper_registry = {
                    key.value: (imports[value.id], value.id)
                    for key, value in zip(node.value.keys, node.value.values)
                }
                break
        self.assertIsNotNone(scraper_registry)
        catalog = load_venue_catalog()
        by_id = {venue["venue_id"]: venue for venue in catalog["venues"]}
        self.assertEqual(set(by_id), set(scraper_registry))
        for venue_id, (module_name, class_name) in scraper_registry.items():
            with self.subTest(venue=venue_id):
                configured = by_id[venue_id]["scraper"]
                self.assertEqual(configured["class_name"], class_name)
                self.assertEqual(configured["module"], module_name)

    def test_continuous_publication_and_monitor_registration_are_explicit(self):
        venues = {
            venue["venue_id"]: venue
            for venue in load_venue_catalog()["venues"]
        }
        self.assertEqual(venues["jmlr"]["lifecycle"]["kind"], "continuous")
        self.assertEqual(venues["jmlr"]["lifecycle"], {"kind": "continuous"})
        self.assertTrue(all(
            set(venue["lifecycle"]) == {"kind"}
            for venue in venues.values()))
        monitored = {
            venue_id for venue_id, venue in venues.items()
            if venue["scraper"]["monitor_registered"]
        }
        self.assertEqual(monitored, {"icml", "aistats", "ijcai"})

        legacy = load_venue_catalog()
        legacy["venues"][0]["lifecycle"]["expected_check_months"] = [12]
        with self.assertRaises(ContractValidationError):
            validate_contract(ContractName.VENUE_CATALOG, legacy)

    def test_policy_defaults_require_crawl_and_publication_review(self):
        policy = load_policy_config()
        self.assertEqual(policy["crawl"]["default_classification"],
                         "review_required")
        self.assertEqual(policy["crawl"]["domains"], [])
        self.assertFalse(
            policy["publication"]["redistribute_pdf_without_review"])
        self.assertFalse(policy["codex_budget"]["allow_recursive_trigger"])
        self.assertEqual(
            policy["systemic_failure"]["venue_failure_threshold"], 3)
        self.assertEqual(
            policy["scheduling"]["unknown_schedule_interval_days"], 60)
        self.assertEqual(
            policy["scheduling"]["post_conference_release_backoff_days"],
            [0, 1, 3, 7, 14, 30])
        self.assertEqual(
            policy["discovery_budget"]["max_calls_per_venue_per_day"], 20)

    def test_duplicate_alias_and_invalid_reminder_windows_are_rejected(self):
        catalog = load_venue_catalog()
        catalog["venues"][1]["aliases"].append(
            catalog["venues"][0]["aliases"][0])
        policy = load_policy_config()
        policy["reminders"]["weekly_until_days"] = 90
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog_path = root / "catalog.json"
            policy_path = root / "policy.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ContractValidationError, "belongs"):
                load_venue_catalog(catalog_path)
            with self.assertRaisesRegex(ContractValidationError, "windows"):
                load_policy_config(policy_path)

    def test_unsorted_scheduling_backoff_is_rejected(self):
        policy = load_policy_config()
        policy["scheduling"]["post_conference_release_backoff_days"] = [0, 3, 1]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ContractValidationError, "sorted"):
                load_policy_config(path)

    def test_default_configuration_files_are_versioned(self):
        self.assertTrue(DEFAULT_VENUE_CATALOG.name.endswith(".v1.json"))
        self.assertTrue(DEFAULT_POLICY_CONFIG.name.endswith(".v1.json"))


if __name__ == "__main__":
    unittest.main()
