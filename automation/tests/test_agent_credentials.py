import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.agent_credentials import (
    AgentCredentialError,
    prepare_agent_credential_context,
    validate_agent_credential_context,
)
from automation.codex_agent import CodexInvocation, SubprocessCodexInvoker


class AgentCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.internal = Path(self.temp.name) / "internal"
        self.internal.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_creates_private_layout_without_secret_files(self):
        context = prepare_agent_credential_context(self.internal)
        self.assertFalse(context.google_adc.exists())
        self.assertFalse((context.codex_home / "auth.json").exists())
        for path in (context.home, context.codex_home, context.google_adc.parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(str(context.home), repr(context))

    def test_validation_requires_private_requested_credentials(self):
        context = prepare_agent_credential_context(self.internal)
        with self.assertRaisesRegex(AgentCredentialError, "file is unavailable"):
            validate_agent_credential_context(
                self.internal, require_codex_auth=True
            )
        (context.codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
        context.google_adc.write_text("{}\n", encoding="utf-8")
        os.chmod(context.codex_home / "auth.json", 0o600)
        os.chmod(context.google_adc, 0o600)
        validate_agent_credential_context(
            self.internal, require_codex_auth=True, require_google_adc=True
        )
        os.chmod(context.google_adc, 0o640)
        with self.assertRaisesRegex(AgentCredentialError, "file is unsafe"):
            validate_agent_credential_context(
                self.internal, require_google_adc=True
            )

    def test_codex_invoker_uses_only_supplied_process_environment(self):
        invocation = CodexInvocation(("codex", "exec"), Path.cwd(), 10)
        with patch("automation.codex_agent.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            run.return_value.stderr = ""
            SubprocessCodexInvoker({"HOME": "/private", "CODEX_HOME": "/codex"}).invoke(
                invocation
            )
        self.assertEqual(run.call_args.kwargs["env"], {
            "HOME": "/private", "CODEX_HOME": "/codex"
        })


if __name__ == "__main__":
    unittest.main()
