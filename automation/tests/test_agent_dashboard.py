import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from automation.agent_dashboard import (
    AgentDashboardError,
    _remaining_label,
    _resolve_editions,
    build_dashboard_model,
    create_dashboard_server,
    load_venue_editions,
    render_dashboard,
)
from automation.agent_status import read_agent_state_summary
from automation.configuration import load_venue_catalog
from automation.control_state import ControlStateRepository
from automation.domain import Writer


NOW = datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)
TODAY = NOW.date()


class VenueEditionTests(unittest.TestCase):
    def test_curated_file_loads_and_rejects_malformed_entries(self):
        editions = load_venue_editions()
        self.assertIn("icml", editions)
        self.assertTrue(all(
            isinstance(item["start_date"], date)
            for entries in editions.values() for item in entries
        ))
        self.assertTrue(all(
            item["source_url"].startswith("https://")
            and isinstance(item["verified_on"], date)
            and item["date_scope"] in {
                "event_start", "main_program_start", "volume_start",
            }
            for entries in editions.values() for item in entries
        ))
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "editions.json"
            bad.write_text(json.dumps({
                "schema_version": 2,
                "editions": [{"venue_id": "icml", "year": 2026}],
            }))
            with self.assertRaisesRegex(AgentDashboardError, "invalid"):
                load_venue_editions(bad)

            bad.write_text(json.dumps({
                "schema_version": 2,
                "editions": [{
                    "venue_id": "icml", "year": 2026,
                    "start_date": "2026-07-07",
                    "date_scope": "main_program_start",
                    "source_url": "https://example.com/unrelated",
                    "verified_on": "2026-07-18",
                }],
            }))
            with self.assertRaisesRegex(AgentDashboardError, "invalid"):
                load_venue_editions(bad)

    def test_resolution_prefers_curated_over_control_state_for_same_year(self):
        curated = [{"year": 2026, "start_date": date(2026, 7, 2), "label": None}]
        last, next_edition = _resolve_editions(
            "acl", {"kind": "annual"}, curated,
            {2026: "2026-07-27"},  # the stale operator estimate
            TODAY,
        )
        self.assertEqual(last["start_date"], date(2026, 7, 2))
        self.assertFalse(last["approx"])
        # No known future edition: cadence approximation, month precision.
        self.assertEqual(next_edition["year"], 2027)
        self.assertTrue(next_edition["approx"])
        self.assertEqual(next_edition["start_date"], date(2027, 7, 1))

    def test_resolution_honors_periodic_cadence_for_the_approximation(self):
        curated = [{"year": 2025, "start_date": date(2025, 10, 19), "label": None}]
        last, next_edition = _resolve_editions(
            "iccv",
            {"kind": "annual", "interval_years": 2, "cycle_anchor_year": 2025},
            curated, {}, TODAY,
        )
        self.assertEqual(last["year"], 2025)
        self.assertEqual(next_edition["year"], 2027)
        self.assertTrue(next_edition["approx"])

    def test_future_control_state_estimate_becomes_next_edition(self):
        last, next_edition = _resolve_editions(
            "uai", {"kind": "annual"},
            [{"year": 2025, "start_date": date(2025, 7, 21), "label": None}],
            {2026: "2026-08-17"}, TODAY,
        )
        self.assertEqual(last["year"], 2025)
        self.assertEqual(next_edition["year"], 2026)
        self.assertFalse(next_edition["approx"])

    def test_countdown_buckets_by_urgency(self):
        cases = [
            (timedelta(minutes=-5), "due now", "due"),
            (timedelta(hours=5), "in 5h", "due"),
            (timedelta(days=3), "in 3d", "soon"),
            (timedelta(days=12), "in 12d", "later"),
            (timedelta(days=90), "in 3mo", "far"),
        ]
        for delta, text, category in cases:
            with self.subTest(delta=delta):
                self.assertEqual(
                    _remaining_label(NOW + delta, NOW), (text, category)
                )


class AgentDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ):
            pass
        next_check = self._time(NOW + timedelta(days=2))
        with sqlite3.connect(self.state) as connection:
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('icml', 2026, 'pending', ?, NULL, NULL, NULL, NULL, NULL, "
                "0, NULL, NULL, ?)",
                (next_check, self._time(NOW - timedelta(hours=1))),
            )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _time(value):
        return value.isoformat().replace("+00:00", "Z")

    def _model(self, targets=None, editions=None):
        if targets is None:
            targets = read_agent_state_summary(self.state)
        return build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW,
            editions=editions if editions is not None else load_venue_editions(),
        )

    def test_model_gives_every_venue_cycle_columns(self):
        model = self._model()

        self.assertEqual(model["venue_count"], 15)
        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        icml = venues["icml"]
        self.assertEqual(icml["current_target"]["next_attempt_at"],
                         self._time(NOW + timedelta(days=2)))
        self.assertEqual(icml["last_edition"]["name"], "ICML 2026")
        self.assertEqual(icml["last_edition"]["date"], "2026-07-07")
        self.assertEqual(icml["next_edition"]["name"], "ICML 2027")
        self.assertTrue(icml["next_edition"]["approx"])
        self.assertEqual(icml["progress"]["label"], "in 2d")
        self.assertEqual(icml["progress"]["category"], "soon")
        # An unenrolled venue still shows its cycle from curated data alone.
        naacl = venues["naacl"]
        self.assertIsNone(naacl["current_target"])
        self.assertEqual(naacl["last_edition"]["name"], "NAACL 2025")
        self.assertEqual(naacl["next_edition"]["date"], "2027-06-01")
        self.assertEqual(naacl["progress"]["label"], "next: NAACL 2027")
        # JMLR uses its curated volume label.
        self.assertEqual(venues["jmlr"]["last_edition"]["name"], "v27")

    def test_completed_venue_counts_down_to_its_next_edition(self):
        with sqlite3.connect(self.state) as connection:
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('acl', 2026, 'scheduled', ?, '2026-07-27', ?, 'operator', "
                "'operator', 'operator', 0, NULL, NULL, ?)",
                (self._time(NOW - timedelta(days=1)),
                 self._time(NOW - timedelta(days=1)),
                 self._time(NOW - timedelta(days=1))),
            )
            connection.execute(
                "INSERT INTO agent_schedule VALUES "
                "('acl', 2026, 'completed', NULL, 0, NULL, 0, NULL, NULL, "
                "NULL, NULL, ?)",
                (self._time(NOW - timedelta(days=1)),),
            )
        model = self._model()

        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        acl = venues["acl"]
        self.assertEqual(acl["current_target"]["phase"], "Collected")
        self.assertIsNone(acl["current_target"]["next_attempt_at"])
        # Cycle continues: countdown targets the (approximate) next edition.
        self.assertEqual(acl["progress"]["label"], "next: ACL 2027")
        self.assertEqual(acl["status"]["text"], "Collected")
        # Curated verified date beats the operator estimate for the same year.
        self.assertEqual(acl["last_edition"]["date"], "2026-07-02")

    def test_completed_status_suppresses_stale_failed_disposition(self):
        targets = [{
            "venue_id": "iclr",
            "year": 2026,
            "event_date": None,
            "agent": {
                "status": "completed",
                "next_check_at": None,
                "last_disposition": "failed",
                "updated_at": self._time(NOW),
            },
            "latest_attempt": None,
            "latest_report": None,
        }]

        model = self._model(targets=targets)

        iclr = next(v for v in model["venues"] if v["venue_id"] == "iclr")
        self.assertEqual(iclr["status"]["text"], "Collected")

    def test_older_actionable_target_beats_newer_terminal_target(self):
        targets = [
            {
                "venue_id": "icml",
                "year": 2026,
                "event_date": None,
                "agent": {
                    "status": "scheduled",
                    "next_check_at": self._time(NOW + timedelta(days=2)),
                    "last_disposition": "not_ready",
                    "updated_at": self._time(NOW),
                },
                "latest_attempt": None,
                "latest_report": None,
            },
            {
                "venue_id": "icml",
                "year": 2027,
                "event_date": None,
                "agent": {
                    "status": "completed",
                    "next_check_at": None,
                    "last_disposition": "success",
                    "updated_at": self._time(NOW),
                },
                "latest_attempt": None,
                "latest_report": None,
            },
        ]

        model = self._model(targets=targets)

        icml = next(v for v in model["venues"] if v["venue_id"] == "icml")
        self.assertEqual(icml["current_target"]["year"], 2026)
        self.assertEqual(icml["status"]["text"], "Scheduled · not_ready")

    def test_edition_day_uses_chicago_calendar_not_utc_calendar(self):
        after_utc_midnight = datetime(2026, 7, 18, 1, tzinfo=timezone.utc)
        model = build_dashboard_model(
            load_venue_catalog(), [], observed_at=after_utc_midnight,
            editions={"icml": [{
                "year": 2026,
                "start_date": date(2026, 7, 18),
                "label": None,
            }]},
        )

        icml = next(v for v in model["venues"] if v["venue_id"] == "icml")
        self.assertIsNone(icml["last_edition"])
        self.assertEqual(icml["next_edition"]["date"], "2026-07-18")

    def test_report_delivery_problem_surfaces_as_warning_only(self):
        model = self._model()
        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertIsNone(venues["icml"]["status"]["warning"])

        targets = [dict(read_agent_state_summary(self.state)[0])]
        targets[0]["latest_report"] = {
            "attempt_number": 1, "status": "retryable", "attempt_count": 1,
            "delivered_at": None, "last_failure_category": "transient",
        }
        model = self._model(targets=targets)
        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertEqual(
            venues["icml"]["status"]["warning"], "report delivery retryable"
        )
        document = render_dashboard(model)
        self.assertIn("report delivery retryable", document)

    def test_rows_sort_action_first_then_cycle_waits(self):
        model = self._model()
        order = [venue["venue_id"] for venue in model["venues"]]
        # icml is the only venue with a real next attempt: it sorts first;
        # everyone else orders by next expected edition.
        self.assertEqual(order[0], "icml")
        cycle = [venues for venues in model["venues"][1:]]
        dates = [venue["next_edition"]["iso_date"] for venue in cycle]
        self.assertEqual(dates, sorted(dates))

    def test_renderer_escapes_content_and_has_only_the_timezone_script(self):
        catalog = copy.deepcopy(load_venue_catalog())
        catalog["venues"][0]["display_name"] = "<script>alert('x')</script>"
        model = build_dashboard_model(
            catalog, [], observed_at=NOW, editions={},
        )

        document = render_dashboard(model)

        self.assertIn("&lt;script&gt;alert", document)
        self.assertNotIn("<script>alert", document)
        # Exactly one active script: the inline timezone selector.
        self.assertEqual(document.count("<script>"), 1)
        self.assertIn('id="tz-select"', document)
        self.assertIn("America/Chicago", document)
        self.assertNotIn("http://", document)
        self.assertNotIn("https://", document)
        self.assertIn("This\npage performs no action", document)

    def test_renderer_defaults_timestamps_to_chicago(self):
        model = self._model()
        document = render_dashboard(model)
        # 2026-07-18T16:30Z == 11:30 in America/Chicago (CDT); the next
        # attempt for icml (NOW+2d 16:30Z) renders as 11:30 local.
        self.assertIn('data-utc="2026-07-18T16:30:00Z">2026-07-18 11:30', document)

    def test_loopback_server_rereads_without_mutating_state(self):
        before = self.state.read_bytes()
        server = create_dashboard_server(
            self.state, port=0, clock=lambda: NOW
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/", timeout=3) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.headers["X-Content-Type-Options"], "nosniff"
                )
                policy = response.headers["Content-Security-Policy"]
                self.assertIn("default-src 'none'", policy)
                self.assertIn("script-src 'unsafe-inline'", policy)
            self.assertIn("International Conference on Machine Learning", body)
            self.assertIn("ICML 2026", body)
            with urlopen(base + "/healthz", timeout=3) as response:
                self.assertEqual(response.read(), b'{"status":"ok"}\n')
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/", method="POST"), timeout=3)
            self.assertEqual(error.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(self.state.read_bytes(), before)

    def test_non_loopback_or_unsafe_state_is_rejected_before_listen(self):
        with self.assertRaisesRegex(AgentDashboardError, "127.0.0.1"):
            create_dashboard_server(self.state, bind="0.0.0.0", port=0)
        relative = Path(self.state.name)
        with self.assertRaisesRegex(AgentDashboardError, "unavailable|unsafe"):
            create_dashboard_server(relative, port=0)


if __name__ == "__main__":
    unittest.main()
