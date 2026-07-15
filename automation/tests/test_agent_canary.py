import argparse
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.agent_canary import AgentCanaryError, _codex, _gemini, _resend
from automation.codex_agent import CodexProcessResult, CodexRunConfig
from automation.event_dates import EventDateEstimate
from automation.notifications import TransportReceipt
from automation.resend_notifications import recipient_fingerprint


class AgentCanaryTests(unittest.TestCase):
    def test_each_mode_requires_its_own_live_authorization_before_loading_install(self):
        cases = (
            (_gemini, argparse.Namespace(authorize_gemini_live=False)),
            (_codex, argparse.Namespace(authorize_codex_live=False)),
            (_resend, argparse.Namespace(authorize_resend_live=False)),
        )
        for runner, arguments in cases:
            with self.subTest(runner=runner.__name__), patch(
                "automation.agent_canary._installed"
            ) as installed:
                with self.assertRaisesRegex(AgentCanaryError, "not authorized"):
                    runner(arguments)
                installed.assert_not_called()

    def test_authorization_for_one_mode_does_not_satisfy_another(self):
        arguments = argparse.Namespace(
            authorize_gemini_live=True,
            authorize_codex_live=False,
            authorize_resend_live=False,
        )
        with self.assertRaisesRegex(AgentCanaryError, "Codex live"):
            _codex(arguments)
        with self.assertRaisesRegex(AgentCanaryError, "Resend live"):
            _resend(arguments)

    def test_gemini_mode_constructs_only_date_provider(self):
        provider = SimpleNamespace(
            estimate=lambda request: EventDateEstimate(date(2026, 7, 7), "fixture"),
            close=lambda: None,
        )
        configuration = SimpleNamespace(agent=SimpleNamespace(
            gemini_project_id="project", gemini_location="global",
            gemini_model="model",
        ))
        arguments = argparse.Namespace(
            authorize_gemini_live=True, internal_root=Path("/private"),
            venue="icml", year=2026,
        )
        with patch("automation.agent_canary._installed", return_value=(configuration, None)), \
                patch("automation.agent_canary.validate_agent_credential_context",
                      return_value=SimpleNamespace(google_adc=Path("/private/adc"))), \
                patch("automation.agent_canary.GeminiEventDateProvider.from_environment",
                      return_value=provider) as factory, \
                patch("automation.agent_canary.load_venue_catalog", return_value={}), \
                patch("automation.agent_canary.request_from_catalog", return_value=object()):
            result = _gemini(arguments)
        self.assertEqual(result["event_date"], "2026-07-07")
        factory.assert_called_once()

    def test_codex_mode_uses_isolated_clone_and_one_invoker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = SimpleNamespace(
                external_effects_enabled=False,
                agent_source_commit="a" * 40,
                agent=SimpleNamespace(codex=CodexRunConfig(
                    codex_binary="/usr/bin/false", timeout_seconds=60
                )),
            )
            context = SimpleNamespace(codex_environment=lambda: {"CODEX_HOME": "/private"})
            invoker = SimpleNamespace(invoke=lambda invocation: CodexProcessResult(
                0, json.dumps({
                    "disposition": "not_ready", "explanation": "fixture",
                    "suggested_retry_at": None, "failure_category": None,
                }), ""
            ))
            arguments = argparse.Namespace(
                authorize_codex_live=True, authorization_id="fixture-1",
                internal_root=root / "internal", repository_root=root / "runtime",
                external_root=root / "external", venue="icml", year=2026,
            )
            arguments.external_root.mkdir()
            with patch("automation.agent_canary._installed", return_value=(configuration, None)), \
                    patch("automation.agent_canary.validate_agent_credential_context",
                          return_value=context), \
                    patch("automation.agent_canary.validate_agent_source",
                          return_value=root / "source"), \
                    patch("automation.agent_canary.subprocess.run") as run, \
                    patch("automation.agent_canary.SubprocessCodexInvoker",
                          return_value=invoker) as factory:
                run.return_value.returncode = 0
                result = _codex(arguments)
            self.assertEqual(result["disposition"], "not_ready")
            self.assertEqual(run.call_count, 2)
            factory.assert_called_once()

    def test_resend_mode_constructs_only_one_transport(self):
        recipient = "to@example.test"
        configuration = SimpleNamespace(agent=SimpleNamespace(
            resend_recipient_sha256=recipient_fingerprint(recipient)
        ))
        secrets = SimpleNamespace(
            resend_api_key="placeholder", email_from="from@example.test",
            email_to=recipient,
        )
        transport = SimpleNamespace(send=lambda intent, idempotency_key: TransportReceipt(
            "receipt:fixture"
        ))
        arguments = argparse.Namespace(
            authorize_resend_live=True, authorization_id="fixture-1"
        )
        with patch("automation.agent_canary._installed",
                   return_value=(configuration, secrets)), \
                patch("automation.agent_canary.ResendNotificationTransport",
                      return_value=transport) as factory:
            result = _resend(arguments)
        self.assertEqual(result, {"canary": "resend", "status": "completed"})
        factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
