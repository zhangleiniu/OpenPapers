import copy
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

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _time(value):
        return value.isoformat().replace("+00:00", "Z")

    def test_model_lists_every_catalog_venue_and_safe_schedule_times(self):
        targets = read_agent_state_summary(self.state)

        model = build_dashboard_model(
            load_venue_catalog(), targets, observed_at=NOW
        )

        self.assertEqual(model["venue_count"], 15)
        self.assertEqual(model["enrolled_venue_count"], 1)
        self.assertEqual(model["target_count"], 1)
        venues = {venue["venue_id"]: venue for venue in model["venues"]}
        self.assertEqual(venues["icml"]["targets"][0], {
            "year": 2026,
            "phase": "Waiting for date",
            "last_updated_at": self._time(NOW - timedelta(hours=1)),
            "next_attempt_at": self._time(NOW + timedelta(days=2)),
            "last_disposition": None,
            "report_status": None,
        })
        self.assertFalse(venues["jmlr"]["enrolled"])
        self.assertEqual(venues["jmlr"]["lifecycle_kind"], "continuous")
        self.assertEqual(venues["jmlr"]["source_monitor"], "Not configured")
        self.assertEqual(venues["icml"]["source_monitor"], "Configured")

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
