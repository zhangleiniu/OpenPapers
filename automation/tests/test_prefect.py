import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from automation.prefect_flows import (
    _emit_source_events, notify_source_event, update_conference_flow,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class PrefectFlowTests(unittest.TestCase):
    def test_change_event_contains_stable_resource(self):
        events = [{
            "venue": "icml", "year": 2026, "status": "available",
            "changed": True, "source_key": "openreview:x", "item_count": 3,
            "detail": "", "snapshot_path": "/tmp/x.json",
        }]
        with patch("automation.prefect_flows.emit_event") as emit:
            _emit_source_events(events)

        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["event"], "openpapers.source.changed")
        self.assertEqual(
            kwargs["resource"]["prefect.resource.id"],
            "openpapers.source.icml.2026")

    def test_update_flow_requires_explicit_approval(self):
        with patch("automation.prefect_flows.emit_event") as emit:
            result = update_conference_flow.fn("icml", 2026)

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(
            emit.call_args.kwargs["event"],
            "openpapers.update.approval-required")

    def test_notification_loads_email_credentials_block(self):
        source = {
            "venue": "icml", "year": 2026, "status": "available",
            "changed": True, "source_key": "openreview:x", "item_count": 3,
            "detail": "", "snapshot_path": "/tmp/x.json",
        }
        with patch(
                "automation.prefect_flows.EmailServerCredentials.load"
                ) as load, patch(
                    "automation.prefect_flows.email_send_message.fn",
                    new_callable=AsyncMock) as send:
            notify_source_event.fn(
                source, "resend-smtp", "alerts@example.com",
                "zhanglei@niu.edu")

        load.assert_called_once_with("resend-smtp")
        self.assertEqual(
            send.call_args.kwargs["email_to"], "zhanglei@niu.edu")
        self.assertIn("icml", send.call_args.kwargs["msg_plain"].lower())

    def test_deployment_files_are_self_contained_and_portable(self):
        deployment = REPO_ROOT / "automation" / "deployment"
        cloudbuild = (deployment / "cloudbuild.yaml").read_text()
        dockerfile = (deployment / "Dockerfile").read_text()

        self.assertIn("$PROJECT_ID", cloudbuild)
        self.assertNotIn("llmcon", cloudbuild)
        self.assertIn(
            "automation/deployment/requirements.txt", dockerfile)


if __name__ == "__main__":
    unittest.main()
