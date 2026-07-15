"""Separately authorized one-adapter live canaries for agent production."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from automation.agent_credentials import validate_agent_credential_context
from automation.codex_agent import (
    CodexInvocation,
    SubprocessCodexInvoker,
    parse_codex_result,
)
from automation.configuration import load_venue_catalog
from automation.discovery import request_from_catalog
from automation.local_service.agent_control import (
    validate_agent_source,
    validate_agent_production_root,
)
from automation.notifications import NotificationIntent, NotificationKind
from automation.providers.gemini import GeminiEventDateProvider
from automation.resend_notifications import (
    ResendNotificationTransport,
    recipient_fingerprint,
)


_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class AgentCanaryError(ValueError):
    """Raised before a live effect when canary authority is not exact."""


def _installed(args):
    configuration, secrets = validate_agent_production_root(
        args.internal_root, args.repository_root
    )
    if configuration.external_effects_enabled:
        raise AgentCanaryError("automatic external effects must remain disabled")
    return configuration, secrets


def _authorization(value: str) -> str:
    if not isinstance(value, str) or not _AUTHORIZATION_ID.fullmatch(value):
        raise AgentCanaryError("canary authorization identity is invalid")
    return value


def _gemini(args) -> dict[str, object]:
    if not args.authorize_gemini_live:
        raise AgentCanaryError("Gemini live canary is not authorized")
    configuration, _ = _installed(args)
    credentials = validate_agent_credential_context(
        args.internal_root, require_google_adc=True
    )
    provider = GeminiEventDateProvider.from_environment({
        "GCP_PROJECT_ID": configuration.agent.gemini_project_id,
        "AUTOMATION_GEMINI_LOCATION": configuration.agent.gemini_location,
        "AUTOMATION_GEMINI_MODEL": configuration.agent.gemini_model,
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials.google_adc),
    })
    try:
        request = request_from_catalog(load_venue_catalog(), args.venue, args.year)
        estimate = provider.estimate(request)
    finally:
        provider.close()
    return {
        "canary": "gemini", "status": "completed",
        "venue_id": args.venue, "year": args.year,
        "event_date": estimate.event_date.isoformat()
        if estimate.event_date else None,
    }


def _codex(args) -> dict[str, object]:
    if not args.authorize_codex_live:
        raise AgentCanaryError("Codex live canary is not authorized")
    authorization = _authorization(args.authorization_id)
    configuration, _ = _installed(args)
    credentials = validate_agent_credential_context(
        args.internal_root, require_codex_auth=True
    )
    source = validate_agent_source(
        Path(args.external_root) / "agent-source",
        configuration.agent_source_commit,
    )
    canary_root = Path(args.external_root).resolve() / "agent-canaries"
    canary_root.mkdir(mode=0o700, exist_ok=True)
    metadata = canary_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or canary_root.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AgentCanaryError("Codex canary root is unsafe")
    worktree = canary_root / authorization
    if worktree.exists():
        raise AgentCanaryError("Codex canary worktree already exists")
    completed = subprocess.run(
        ("git", "clone", "--no-hardlinks", "--quiet", str(source), str(worktree)),
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise AgentCanaryError("Codex canary clone failed")
    subprocess.run(
        ("git", "remote", "remove", "origin"), cwd=worktree,
        text=True, capture_output=True, check=True,
    )
    schema = Path(args.repository_root) / "automation" / "schemas" / "v1" \
        / "agent-run-result.json"
    prompt = (
        f"Handle {args.venue} {args.year} for OpenPapers as a live canary. "
        "Investigate readiness and leave useful edits only in this isolated "
        "checkout. Never commit, push, merge, deploy, or alter Git metadata. "
        "Return only the required structured result."
    )
    invocation = CodexInvocation((
        configuration.agent.codex.codex_binary,
        "--ask-for-approval", "never", "exec", "--ephemeral",
        "--ignore-user-config", "--ignore-rules", "--sandbox", "workspace-write",
        "--cd", str(worktree), "--config", "mcp_servers={}",
        "--config", 'web_search="cached"', "--output-schema", str(schema), prompt,
    ), worktree, configuration.agent.codex.timeout_seconds)
    process = SubprocessCodexInvoker(credentials.codex_environment()).invoke(invocation)
    if process.returncode != 0:
        raise AgentCanaryError("Codex live canary exited unsuccessfully")
    result = parse_codex_result(process.stdout)
    return {
        "canary": "codex", "status": "completed",
        "venue_id": args.venue, "year": args.year,
        "disposition": result.disposition,
        "worktree_retained": True,
    }


def _resend(args) -> dict[str, object]:
    if not args.authorize_resend_live:
        raise AgentCanaryError("Resend live canary is not authorized")
    authorization = _authorization(args.authorization_id)
    configuration, secrets = _installed(args)
    if secrets is None:
        raise AgentCanaryError("Resend secrets are not configured")
    if recipient_fingerprint(secrets.email_to) \
            != configuration.agent.resend_recipient_sha256:
        raise AgentCanaryError("Resend recipient approval changed")
    source_id = f"agent-canary.{authorization}"
    notification_id = "notification:immediate:" + hashlib.sha256(
        source_id.encode("utf-8")
    ).hexdigest()
    intent = NotificationIntent(
        notification_id=notification_id,
        kind=NotificationKind.IMMEDIATE,
        source_ids=(source_id,),
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        subject="OpenPapers agent-run email canary",
        body="This is one separately authorized OpenPapers Resend canary.",
        evidence_ids=(f"evidence.{authorization}",),
        run_ids=(),
    )
    receipt = ResendNotificationTransport(
        api_key=secrets.resend_api_key,
        email_from=secrets.email_from,
        email_to=secrets.email_to,
    ).send(intent, idempotency_key=notification_id)
    del receipt
    return {"canary": "resend", "status": "completed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one authorized agent adapter canary.")
    parser.add_argument("--internal-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--external-root", type=Path, required=True)
    modes = parser.add_subparsers(dest="mode", required=True)
    gemini = modes.add_parser("gemini")
    gemini.add_argument("--venue", required=True)
    gemini.add_argument("--year", type=int, required=True)
    gemini.add_argument("--authorize-gemini-live", action="store_true")
    codex = modes.add_parser("codex")
    codex.add_argument("--venue", required=True)
    codex.add_argument("--year", type=int, required=True)
    codex.add_argument("--authorization-id", required=True)
    codex.add_argument("--authorize-codex-live", action="store_true")
    resend = modes.add_parser("resend")
    resend.add_argument("--authorization-id", required=True)
    resend.add_argument("--authorize-resend-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runners = {"gemini": _gemini, "codex": _codex, "resend": _resend}
    result = runners[args.mode](args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
