"""Render an isolated macOS deployment for the OpenPapers dashboard."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import stat
import sys
from pathlib import Path
from typing import Mapping, Sequence


DASHBOARD_LABEL = "org.openpapers.agent-dashboard"
PROXY_LABEL = "org.openpapers.agent-dashboard-proxy"
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_IDENTITY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_BCRYPT = re.compile(r"^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$")


class DashboardDeploymentError(ValueError):
    """Raised when a dashboard deployment input is unsafe or ambiguous."""


def _absolute(value: Path, *, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(character.isspace() for character in str(path)):
        raise DashboardDeploymentError(f"{field} must be an absolute path without spaces")
    return path


def _port(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
        raise DashboardDeploymentError(f"{field} must be an unprivileged port")
    return value


def render_caddyfile(
    *,
    hostname: str,
    bind_address: str,
    public_port: int,
    backend_port: int,
    username: str,
    password_hash: str,
) -> bytes:
    """Render private-CA HTTPS and authentication for one fixed endpoint."""
    if not isinstance(hostname, str) or not _HOSTNAME.fullmatch(hostname):
        raise DashboardDeploymentError("dashboard hostname is invalid")
    try:
        address = ipaddress.ip_address(bind_address)
    except ValueError as exc:
        raise DashboardDeploymentError("dashboard bind address is invalid") from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or address.is_link_local
        or address.version != 4
    ):
        raise DashboardDeploymentError("dashboard bind address must be private IPv4")
    public = _port(public_port, field="dashboard public port")
    backend = _port(backend_port, field="dashboard backend port")
    if public == backend:
        raise DashboardDeploymentError("dashboard ports must be distinct")
    if not isinstance(username, str) or not _IDENTITY.fullmatch(username):
        raise DashboardDeploymentError("dashboard username is invalid")
    if not isinstance(password_hash, str) or not _BCRYPT.fullmatch(password_hash):
        raise DashboardDeploymentError("dashboard password hash is invalid")
    document = f"""{{
    admin off
    auto_https disable_redirects
    skip_install_trust
}}

https://{hostname}:{public} {{
    bind {address}
    tls internal
    basic_auth {{
        {username} {password_hash}
    }}
    reverse_proxy 127.0.0.1:{backend}
    header {{
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        -Server
    }}
}}
"""
    return document.encode("utf-8")


def build_dashboard_plist(
    *,
    python: Path,
    runtime: Path,
    state: Path,
    role_user: str,
    role_group: str,
    backend_port: int,
) -> Mapping[str, object]:
    """Build the loopback dashboard LaunchDaemon document."""
    if not _IDENTITY.fullmatch(role_user) or not _IDENTITY.fullmatch(role_group):
        raise DashboardDeploymentError("dashboard role is invalid")
    executable = _absolute(python, field="dashboard Python")
    working = _absolute(runtime, field="dashboard runtime")
    database = _absolute(state, field="dashboard state")
    port = _port(backend_port, field="dashboard backend port")
    return {
        "Label": DASHBOARD_LABEL,
        "ProgramArguments": [
            str(executable), "-m", "automation.agent_dashboard",
            "--state", str(database), "--bind", "127.0.0.1",
            "--port", str(port),
        ],
        "WorkingDirectory": str(working),
        "UserName": role_user,
        "GroupName": role_group,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def build_proxy_plist(
    *,
    caddy: Path,
    caddyfile: Path,
    working_root: Path,
    role_user: str,
    role_group: str,
) -> Mapping[str, object]:
    """Build the isolated unprivileged Caddy LaunchDaemon document."""
    if not _IDENTITY.fullmatch(role_user) or not _IDENTITY.fullmatch(role_group):
        raise DashboardDeploymentError("dashboard role is invalid")
    executable = _absolute(caddy, field="dashboard Caddy")
    configuration = _absolute(caddyfile, field="dashboard Caddyfile")
    root = _absolute(working_root, field="dashboard working root")
    return {
        "Label": PROXY_LABEL,
        "ProgramArguments": [
            str(executable), "run", "--config", str(configuration),
            "--adapter", "caddyfile",
        ],
        "WorkingDirectory": str(root),
        "UserName": role_user,
        "GroupName": role_group,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {
            "HOME": str(root),
            "XDG_DATA_HOME": str(root / "caddy-data"),
            "XDG_CONFIG_HOME": str(root / "caddy-config"),
        },
    }


def _write_new(path: Path, encoded: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    with os.fdopen(descriptor, "wb") as file_obj:
        file_obj.write(encoded)
        file_obj.flush()
        os.fsync(file_obj.fileno())


def render_dashboard_deployment(
    output_root: Path,
    *,
    python: Path,
    runtime: Path,
    state: Path,
    caddy: Path,
    installed_caddy: Path,
    deployed_root: Path,
    role_user: str,
    role_group: str,
    hostname: str,
    bind_address: str,
    public_port: int,
    backend_port: int,
    username: str,
    password_hash: str,
) -> dict[str, object]:
    """Create a new private staging set and return a password-free manifest."""
    root = Path(output_root)
    if root.exists() or root.is_symlink():
        raise DashboardDeploymentError("dashboard staging root exists")
    parent = root.parent
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise DashboardDeploymentError("dashboard staging parent is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DashboardDeploymentError("dashboard staging parent is unsafe")
    root.mkdir(mode=0o700)
    try:
        caddyfile = render_caddyfile(
            hostname=hostname,
            bind_address=bind_address,
            public_port=public_port,
            backend_port=backend_port,
            username=username,
            password_hash=password_hash,
        )
        dashboard = plistlib.dumps(build_dashboard_plist(
            python=python,
            runtime=runtime,
            state=state,
            role_user=role_user,
            role_group=role_group,
            backend_port=backend_port,
        ), fmt=plistlib.FMT_XML, sort_keys=False)
        proxy = plistlib.dumps(build_proxy_plist(
            caddy=installed_caddy,
            caddyfile=deployed_root / "Caddyfile",
            working_root=deployed_root,
            role_user=role_user,
            role_group=role_group,
        ), fmt=plistlib.FMT_XML, sort_keys=False)
        source_caddy = _absolute(caddy, field="dashboard source Caddy")
        files = {
            "Caddyfile": (caddyfile, 0o600),
            f"{DASHBOARD_LABEL}.plist": (dashboard, 0o644),
            f"{PROXY_LABEL}.plist": (proxy, 0o644),
        }
        for name, (encoded, mode) in files.items():
            _write_new(root / name, encoded, mode)
        manifest = {
            "schema_version": 1,
            "hostname": hostname,
            "bind_address": bind_address,
            "public_port": public_port,
            "backend_port": backend_port,
            "caddy_sha256": hashlib.sha256(source_caddy.read_bytes()).hexdigest(),
            "files": {
                name: hashlib.sha256(encoded).hexdigest()
                for name, (encoded, _) in sorted(files.items())
            },
        }
        _write_new(
            root / "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            + b"\n",
            0o600,
        )
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, DashboardDeploymentError):
        for child in root.iterdir():
            child.unlink(missing_ok=True)
        root.rmdir()
        raise
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--caddy", type=Path, required=True)
    parser.add_argument("--installed-caddy", type=Path, required=True)
    parser.add_argument("--deployed-root", type=Path, required=True)
    parser.add_argument("--role-user", required=True)
    parser.add_argument("--role-group", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--bind-address", required=True)
    parser.add_argument("--public-port", type=int, default=8443)
    parser.add_argument("--backend-port", type=int, default=8765)
    parser.add_argument("--username", default="openpapers")
    args = parser.parse_args(argv)
    password_hash = sys.stdin.readline().rstrip("\n")
    try:
        manifest = render_dashboard_deployment(
            args.output_root,
            python=args.python,
            runtime=args.runtime,
            state=args.state,
            caddy=args.caddy,
            installed_caddy=args.installed_caddy,
            deployed_root=args.deployed_root,
            role_user=args.role_user,
            role_group=args.role_group,
            hostname=args.hostname,
            bind_address=args.bind_address,
            public_port=args.public_port,
            backend_port=args.backend_port,
            username=args.username,
            password_hash=password_hash,
        )
    except (DashboardDeploymentError, OSError):
        print(json.dumps({"dashboard_deployment": "blocked"}))
        return 2
    print(json.dumps({
        "dashboard_deployment": "rendered",
        "schema_version": manifest["schema_version"],
        "public_port": manifest["public_port"],
        "backend_port": manifest["backend_port"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
