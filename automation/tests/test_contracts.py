import ast
import json
import tempfile
import unittest
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


class ContractTests(unittest.TestCase):
    def test_active_schemas_self_check_and_saved_fixtures_pass(self):
        fixtures = {
            ContractName.DISCOVERY_RESULT: "discovery-result.v1.json",
            ContractName.NOTIFICATION_INTENT: "notification-intent.v1.json",
        }
        for contract in ContractName:
            with self.subTest(contract=contract.value):
                self.assertEqual(
                    load_schema(contract)["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
        for contract, name in fixtures.items():
            payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            validate_contract(contract, payload)

    def test_unknown_version_missing_and_extra_fields_are_rejected(self):
        payload = json.loads(
            (FIXTURES / "discovery-result.v1.json").read_text(encoding="utf-8")
        )
        for mutation in (
            lambda item: item.update(schema_version=2),
            lambda item: item.pop("discovery_id"),
            lambda item: item.update(unexpected=True),
        ):
            candidate = json.loads(json.dumps(payload))
            mutation(candidate)
            with self.assertRaises(ContractValidationError):
                validate_contract(ContractName.DISCOVERY_RESULT, candidate)

    def test_artifact_fingerprint_is_order_independent(self):
        self.assertEqual(
            artifact_fingerprint({"a": 1, "b": [2, 3]}),
            artifact_fingerprint({"b": [2, 3], "a": 1}),
        )


class ConfigurationTests(unittest.TestCase):
    def test_catalog_covers_registered_scrapers_and_lifecycle_cadence(self):
        catalog = load_venue_catalog()
        venues = {item["venue_id"]: item for item in catalog["venues"]}
        tree = ast.parse(
            (Path(__file__).resolve().parents[2] / "main.py").read_text(
                encoding="utf-8"
            )
        )
        scraper_assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SCRAPERS"
                    for target in node.targets)
        )
        scraper_ids = {
            key.value for key in scraper_assignment.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertEqual(set(venues), scraper_ids)
        self.assertEqual(venues["iccv"]["lifecycle"]["interval_years"], 2)
        self.assertEqual(venues["eccv"]["lifecycle"]["cycle_anchor_year"], 2024)
        self.assertTrue(all(item["scraper"]["monitor_registered"] for item in venues.values()))

    def test_duplicate_alias_is_rejected(self):
        catalog = load_venue_catalog()
        catalog["venues"][1]["aliases"].append(catalog["venues"][0]["aliases"][0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ContractValidationError, "belongs"):
                load_venue_catalog(path)

    def test_policy_contains_only_active_discovery_budget(self):
        policy = load_policy_config()
        self.assertEqual(set(policy), {"schema_version", "discovery_budget"})
        self.assertEqual(policy["discovery_budget"]["max_concurrency"], 2)

    def test_cross_field_discovery_budget_limits_fail_closed(self):
        policy = load_policy_config()
        policy["discovery_budget"]["max_calls_per_venue_per_day"] = 21
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ContractValidationError, "per-venue"):
                load_policy_config(path)

    def test_default_configuration_files_are_explicitly_versioned(self):
        self.assertTrue(DEFAULT_VENUE_CATALOG.name.endswith(".v1.json"))
        self.assertTrue(DEFAULT_POLICY_CONFIG.name.endswith(".v1.json"))


if __name__ == "__main__":
    unittest.main()
