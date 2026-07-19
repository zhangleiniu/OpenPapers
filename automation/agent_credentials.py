"""Private credential layout for the installed agent-control adapters."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


AGENT_CREDENTIAL_DIRECTORY = "agent-credentials.v1"
CODEX_HOME_DIRECTORY = "codex"
GOOGLE_DIRECTORY = "google"
GOOGLE_ADC_FILE = "application_default_credentials.json"


class AgentCredentialError(ValueError):
    """Raised when a dedicated-role credential path is absent or unsafe."""


@dataclass(frozen=True, repr=False)
class AgentCredentialContext:
    home: Path
    codex_home: Path
    google_adc: Path

    def codex_environment(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment.update({
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
        })
        return environment


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentCredentialError("agent credential directory is unavailable") from exc
    # Group read/traverse is a deliberate, trusted exception (2026-07-19 —
    # this host's staff group is a small trusted set of accounts, not the
    # public); group write and any "other" access remain forbidden.
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO) \
            or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise AgentCredentialError("agent credential directory is unsafe")


def _private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentCredentialError("agent credential file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO) \
            or not 2 <= metadata.st_size <= 1_048_576:
        raise AgentCredentialError("agent credential file is unsafe")


def prepare_agent_credential_context(internal_root: Path) -> AgentCredentialContext:
    """Create only private credential directories; never create credential files."""
    internal = Path(internal_root)
    _private_directory(internal)
    root = internal / AGENT_CREDENTIAL_DIRECTORY
    codex = root / CODEX_HOME_DIRECTORY
    google = root / GOOGLE_DIRECTORY
    for path in (root, codex, google):
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            raise AgentCredentialError(
                "agent credential directory preparation failed"
            ) from exc
        _private_directory(path)
    return AgentCredentialContext(root, codex, google / GOOGLE_ADC_FILE)


def validate_agent_credential_context(
    internal_root: Path,
    *,
    require_codex_auth: bool = False,
    require_google_adc: bool = False,
) -> AgentCredentialContext:
    """Validate the fixed credential layout without reading secret contents."""
    root = Path(internal_root) / AGENT_CREDENTIAL_DIRECTORY
    context = AgentCredentialContext(
        root, root / CODEX_HOME_DIRECTORY,
        root / GOOGLE_DIRECTORY / GOOGLE_ADC_FILE,
    )
    _private_directory(context.home)
    _private_directory(context.codex_home)
    _private_directory(context.google_adc.parent)
    if require_codex_auth:
        _private_file(context.codex_home / "auth.json")
    if require_google_adc:
        _private_file(context.google_adc)
    return context


def _executable(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise AgentCredentialError("credential helper executable is invalid")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare dedicated-role credentials.")
    parser.add_argument("--internal-root", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare")
    status = actions.add_parser("status")
    status.add_argument("--require-codex", action="store_true")
    status.add_argument("--require-google", action="store_true")
    codex = actions.add_parser("codex-login")
    codex.add_argument("--codex-binary", required=True)
    codex.add_argument("--method", choices=("device", "api-key"), default="device")
    google = actions.add_parser("google-adc-login")
    google.add_argument("--gcloud-binary", required=True)
    google.add_argument("--impersonate-service-account", required=True)
    resend = actions.add_parser("configure-resend")
    resend.add_argument("--repository-root", type=Path, required=True)
    resend.add_argument("--confirm-service-stopped", action="store_true")
    resend.add_argument("--recipient-count", type=int, default=1)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        prepare_agent_credential_context(args.internal_root)
        print(json.dumps({"credential_layout": "prepared"}, sort_keys=True))
        return 0
    context = validate_agent_credential_context(
        args.internal_root,
        require_codex_auth=getattr(args, "require_codex", False),
        require_google_adc=getattr(args, "require_google", False),
    )
    if args.action == "status":
        print(json.dumps({
            "codex_auth_present": (context.codex_home / "auth.json").is_file(),
            "google_adc_present": context.google_adc.is_file(),
        }, sort_keys=True))
        return 0
    if args.action == "configure-resend":
        if not args.confirm_service_stopped:
            raise AgentCredentialError("stopped service confirmation is required")
        if not 1 <= args.recipient_count <= 10:
            raise AgentCredentialError("Resend recipient count must be 1-10")
        from automation.local_service.agent_control import (
            replace_disabled_agent_resend,
        )
        api_key = getpass.getpass("Resend API key: ")
        email_from = input("Resend sender: ").strip()
        email_to = tuple(
            input(f"Resend recipient {index + 1}: ").strip()
            for index in range(args.recipient_count)
        )
        replace_disabled_agent_resend(
            args.internal_root,
            args.repository_root,
            api_key=api_key,
            email_from=email_from,
            email_to=email_to,
        )
        print(json.dumps({"resend_configuration": "installed"}, sort_keys=True))
        return 0
    if args.action == "codex-login":
        binary = _executable(args.codex_binary)
        arguments = [
            str(binary), "--config", 'cli_auth_credentials_store="file"', "login"
        ]
        arguments.append("--device-auth" if args.method == "device" else "--with-api-key")
        os.execve(str(binary), arguments, context.codex_environment())
    binary = _executable(args.gcloud_binary)
    environment = context.codex_environment()
    environment["CLOUDSDK_CONFIG"] = str(context.google_adc.parent)
    arguments = [
        str(binary), "auth", "application-default", "login",
        f"--impersonate-service-account={args.impersonate_service_account}",
    ]
    os.execve(str(binary), arguments, environment)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
