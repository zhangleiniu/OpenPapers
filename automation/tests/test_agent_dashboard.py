import copy
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from automation.agent_dashboard import (
    AgentDashboardError,
    build_dashboard_model,
    create_dashboard_server,
    render_dashboard,
)
from automation.agent_status import read_agent_state_summary
from automation.configuration import load_venue_catalog
from automation.control_state import ControlStateRepository
from automation.domain import Writer


NOW = datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)


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
        self.metadata_root = self.root / "metadata"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _time(value):
        return value.isoformat().replace("+00:00", "Z")

    def _write_metadata(self, venue_id, year, *, mtime=None):
        directory = self.metadata_root / venue_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{venue_id}_{year}.json"
        path.write_text("[]", encoding="utf-8")
        if mtime is not None:
            os_mtime = mtime.timestamp()
            os.utime(path, (os_mtime, os_mtime))
        return path

    def test_model_lists_every_catalog_venue_and_safe_schedule_times(self):
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW
        )

        self.assertEqual(model["venue_count"], 15)
        self.assertEqual(model["enrolled_venue_count"], 1)
        self.assertEqual(model["target_count"], 1)
        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertEqual(venues["icml"]["current_target"], {
            "year": 2026,
            "phase": "Waiting for date",
            "last_updated_at": self._time(NOW - timedelta(hours=1)),
            "next_attempt_at": self._time(NOW + timedelta(days=2)),
            "last_disposition": None,
            "report_status": None,
        })
        self.assertFalse(venues["jmlr"]["enrolled"])
        self.assertEqual(venues["jmlr"]["lifecycle_kind"], "continuous")
        self.assertEqual(venues["jmlr"]["source_monitor"], "Configured")
        self.assertEqual(venues["icml"]["source_monitor"], "Configured")
        self.assertEqual(venues["naacl"]["source_monitor"], "Configured")
        self.assertIsNone(venues["icml"]["last_downloaded"])

    def test_current_target_selects_the_maximum_enrolled_year(self):
        base_targets = read_agent_state_summary(self.state)
        extra = dict(base_targets[0])
        extra["year"] = 2025
        extra["event_date"] = dict(extra["event_date"])

        model = build_dashboard_model(
            load_venue_catalog(), [extra, base_targets[0]], observed_at=NOW
        )

        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertEqual(venues["icml"]["current_target"]["year"], 2026)
        self.assertTrue(venues["icml"]["enrolled"])

    def test_last_downloaded_reads_the_highest_year_file_and_degrades_safely(self):
        self._write_metadata(
            "icml", 2025, mtime=datetime(2025, 6, 1, tzinfo=timezone.utc)
        )
        self._write_metadata(
            "icml", 2026, mtime=datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
        )
        # A malformed filename (no trailing _<year>) must be ignored, not raise.
        (self.metadata_root / "icml" / "notes.json").write_text("{}", encoding="utf-8")
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW,
            metadata_root=self.metadata_root,
        )

        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertEqual(venues["icml"]["last_downloaded"], {
            "year": 2026,
            "observed_at": "2026-07-12T09:00:00Z",
        })
        # A venue with no metadata directory at all degrades to None, not an error.
        self.assertIsNone(venues["neurips"]["last_downloaded"])

    def test_last_downloaded_is_none_when_metadata_root_is_unreadable(self):
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW,
            metadata_root=self.root / "does-not-exist",
        )

        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertIsNone(venues["icml"]["last_downloaded"])
        self.assertEqual(model["venue_count"], 15)

    def test_urgency_rank_orders_active_before_waiting_before_unenrolled(self):
        with sqlite3.connect(self.state) as connection:
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('aistats', 2026, 'active', ?, NULL, NULL, NULL, NULL, NULL, "
                "1, ?, NULL, ?)",
                (
                    self._time(NOW + timedelta(days=30)),
                    "attempt:1",
                    self._time(NOW - timedelta(minutes=5)),
                ),
            )
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW
        )

        order = [venue["venue_id"] for venue in model["venues"]]
        # "Date lookup running" (aistats) outranks "Waiting for date" (icml),
        # which outranks every unenrolled venue.
        self.assertEqual(order[0], "aistats")
        self.assertEqual(order[1], "icml")
        self.assertTrue(set(order[2:]) == {
            venue["venue_id"] for venue in model["venues"]
        } - {"aistats", "icml"})
        unenrolled_ranks = {
            venue["venue_id"]: venue["urgency_rank"][0]
            for venue in model["venues"] if venue["venue_id"] not in {"aistats", "icml"}
        }
        self.assertTrue(all(rank == 4 for rank in unenrolled_ranks.values()))

    def test_progress_fraction_is_bounded_and_categorized(self):
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW
        )

        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        # The icml fixture's next check is NOW + 2 days: within the <7d
        # "soon" urgency bucket, with a countdown label rather than a phase,
        # and a bar showing the remaining time on the 30-day scale (2/30).
        waiting = venues["icml"]["progress"]
        self.assertEqual(waiting["category"], "soon")
        self.assertEqual(waiting["label"], "in 2d")
        self.assertAlmostEqual(waiting["fraction"], 2 / 30, places=3)
        unenrolled = venues["neurips"]["progress"]
        self.assertEqual(
            unenrolled,
            {"fraction": 0.0, "category": "none", "label": "Not enrolled"},
        )

    def test_remaining_label_buckets_by_urgency(self):
        from automation.agent_dashboard import _remaining_label

        cases = [
            (timedelta(minutes=-5), "due now", "due"),
            (timedelta(minutes=30), "in <1h", "due"),
            (timedelta(hours=5), "in 5h", "due"),
            (timedelta(days=3), "in 3d", "soon"),
            (timedelta(days=12), "in 12d", "later"),
            (timedelta(days=45), "in 45d", "far"),
            (timedelta(days=90), "in 3mo", "far"),
        ]
        for delta, text, category in cases:
            with self.subTest(delta=delta):
                self.assertEqual(
                    _remaining_label(NOW + delta, NOW), (text, category)
                )

    def test_renderer_escapes_catalog_text_and_contains_no_external_resource(self):
        catalog = copy.deepcopy(load_venue_catalog())
        catalog["venues"][0]["display_name"] = "<script>alert('x')</script>"
        model = build_dashboard_model(catalog, [], observed_at=NOW)

        document = render_dashboard(model)

        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;alert", document)
        self.assertNotIn("http://", document)
        self.assertNotIn("https://", document)
        self.assertIn("This page performs no action", document)

    def test_renderer_shows_venue_abbreviation_and_columns(self):
        targets = read_agent_state_summary(self.state)
        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW
        )

        document = render_dashboard(model)

        self.assertIn(">ICML<", document)
        self.assertNotIn("<th>Name</th>", document)
        self.assertNotIn("<th>Lifecycle</th>", document)
        self.assertNotIn("<th>Source monitor</th>", document)
        self.assertNotIn("<th>Year</th>", document)

    def test_renderer_badges_a_venue_with_no_monitor_source(self):
        catalog = copy.deepcopy(load_venue_catalog())
        catalog["venues"][0]["scraper"]["monitor_registered"] = False
        model = build_dashboard_model(catalog, [], observed_at=NOW)

        document = render_dashboard(model)

        self.assertIn("No deterministic monitor source configured", document)

    def test_loopback_server_rereads_without_mutating_state(self):
        before = self.state.read_bytes()
        server = create_dashboard_server(
            self.state, port=0, clock=lambda: NOW,
            metadata_root=self.metadata_root,
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
                self.assertIn("default-src 'none'", response.headers[
                    "Content-Security-Policy"
                ])
            self.assertIn("International Conference on Machine Learning", body)
            self.assertIn("Waiting for date", body)
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
