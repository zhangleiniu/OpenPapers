"""Uninstalled Codex CLI runner confined to an isolated Git worktree."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    AgentRunClaim,
    ControlStateRepository,
)
from automation.configuration import load_venue_catalog
from automation.domain import Writer
from automation.due_policy import AgentRunResult, DuePolicy, complete_agent_run


RESULT_SCHEMA = Path(__file__).with_name("schemas") / "v1" / "agent-run-result.json"


@dataclass(frozen=True)
class CodexRunConfig:
    codex_binary: str = "codex"
    timeout_seconds: int = 3600
    max_output_bytes: int = 64_000
    max_changed_files: int = 500

    def __post_init__(self) -> None:
        for value, field in (
            (self.timeout_seconds, "timeout_seconds"),
            (self.max_output_bytes, "max_output_bytes"),
            (self.max_changed_files, "max_changed_files"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class CodexInvocation:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodexProcessResult:
    returncode: int
    stdout: str
    stderr: str


class CodexInvoker(Protocol):
    def invoke(self, invocation: CodexInvocation) -> CodexProcessResult:
        """Run one bounded Codex process or raise TimeoutExpired."""


class SubprocessCodexInvoker:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = (
            os.environ.copy() if environment is None else dict(environment)
        )

    def invoke(self, invocation: CodexInvocation) -> CodexProcessResult:
        completed = subprocess.run(
            invocation.argv,
            cwd=invocation.cwd,
            env=self._environment,
            text=True,
            capture_output=True,
            timeout=invocation.timeout_seconds,
            check=False,
        )
        return CodexProcessResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class CodexExecutionOutcome:
    result: AgentRunResult
    worktree_path: Path
    branch_name: str
    base_commit: str
    changed_files: tuple[str, ...]
    returncode: int | None
    timed_out: bool


def _catalog_venue(venue_id: str) -> Mapping[str, object]:
    catalog = load_venue_catalog()
    venue = next(
        (item for item in catalog["venues"] if item["venue_id"] == venue_id),
        None,
    )
    if venue is None:
        raise ValueError("agent venue is not in the catalog")
    return venue


def _network_arguments(venue_id: str) -> tuple[str, ...]:
    """Return a workspace-sandbox network allowlist for one catalog venue."""
    venue = _catalog_venue(venue_id)
    domains = sorted(set(venue["official_domains"] + venue["archival_domains"]))
    rules = ", ".join(f'"{domain}"="allow"' for domain in domains)
    return (
        "--config", "sandbox_workspace_write.network_access=true",
        "--config", "features.network_proxy.enabled=true",
        "--config", f"features.network_proxy.domains={{ {rules} }}",
    )


def _is_continuous(venue_id: str) -> bool:
    return _catalog_venue(venue_id)["lifecycle"]["kind"] == "continuous"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(args)}")
    return completed.stdout.strip()


def _prompt(claim: AgentRunClaim, policy: DuePolicy, *, is_continuous: bool) -> str:
    started = datetime.fromisoformat(claim.started_at.replace("Z", "+00:00"))
    earliest = (started + policy.minimum_retry_delay).astimezone(timezone.utc)
    latest = (started + policy.max_suggested_retry_delay).astimezone(timezone.utc)
    earliest_text = earliest.isoformat().replace("+00:00", "Z")
    latest_text = latest.isoformat().replace("+00:00", "Z")
    continuous_note = ""
    if is_continuous:
        continuous_note = f"""
{claim.venue_id} publishes continuously with no discrete edition: success means
this check's scrape is complete and up to date as of now, not that the venue is
permanently done. Expect to be invoked again later ({policy.recurring_recheck_interval}
after this success) to pick up newly published items since this check.
"""
    return f"""Handle {claim.venue_id} {claim.year} for OpenPapers.
Decide whether the canonical papers and PDFs are now downloadable. Investigate
the web and repository, reuse or repair the scraper, run the scrape and required
validation when ready, and leave all useful edits in this worktree. Never
commit, push, merge, deploy, edit Git metadata, or modify another checkout.
Return only the required structured result. Use success only after scrape and
validation complete; not_ready for unpublished data; needs_human for policy or
access ambiguity; failed for operational or code failure.
{continuous_note}

For not_ready, investigate the most relevant next publication signal and set
suggested_retry_at to a concrete UTC timestamp between {earliest_text} and
{latest_text} when there is a credible timing basis. Check actively during a
conference or partial/rapid official release; otherwise use an announced
camera-ready, revision, or proceedings date when available. Do not minimize
normal agent use merely to save calls: a venue may release thousands of papers.
Use null only when no defensible time in that window exists. Explain the timing
basis briefly, but do not treat a date or source change as readiness proof."""


def parse_codex_result(raw: str) -> AgentRunResult:
    try:
        body = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex returned malformed structured output") from exc
    required = {"disposition", "explanation", "suggested_retry_at", "failure_category"}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError("Codex result fields are invalid")
    disposition = body["disposition"]
    explanation = body["explanation"]
    failure = body["failure_category"]
    if (
        disposition not in {"success", "not_ready", "needs_human", "failed"}
        or not isinstance(explanation, str)
        or not explanation.strip()
        or len(explanation) > 4000
        or (failure is not None and not isinstance(failure, str))
        or (isinstance(failure, str) and (not failure.strip() or len(failure) > 200))
        or (disposition == "failed" and not isinstance(failure, str))
    ):
        raise ValueError("Codex result values are invalid")
    suggested = body["suggested_retry_at"]
    if suggested is not None:
        try:
            suggested = datetime.fromisoformat(suggested.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("Codex retry time is invalid") from exc
        if suggested.tzinfo is None or suggested.utcoffset() is None:
            raise ValueError("Codex retry time is invalid")
        suggested = suggested.astimezone(timezone.utc)
    if disposition != "not_ready" and suggested is not None:
        raise ValueError("Codex retry suggestion is inconsistent")
    if disposition != "failed":
        failure = None
    return AgentRunResult(disposition, explanation, suggested, failure)


def run_claimed_codex_agent(
    state_path: Path,
    repository_root: Path,
    runs_root: Path,
    claim: AgentRunClaim,
    *,
    clock: Callable[[], datetime],
    invoker: CodexInvoker,
    policy: DuePolicy = DuePolicy(),
    config: CodexRunConfig = CodexRunConfig(),
) -> CodexExecutionOutcome:
    """Run one claimed Codex task and apply its validated due-state result."""
    repository_root = Path(repository_root).resolve()
    runs_root = Path(runs_root).resolve()
    primary_head = _git(repository_root, "rev-parse", "HEAD")
    primary_status = _git(repository_root, "status", "--porcelain=v1", "--untracked-files=all")
    if primary_status:
        raise RuntimeError("primary checkout must be clean before agent execution")
    suffix = claim.run_id.split(":", 1)[-1][:16]
    worktree = runs_root / suffix
    branch = f"automation/agent/{suffix}"
    runs_root.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise RuntimeError("agent worktree path already exists")
    _git(repository_root, "worktree", "add", "-b", branch, str(worktree), primary_head)
    artifact_started = clock()
    with ControlStateRepository(
        Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE,
        clock=lambda: artifact_started,
    ) as repository:
        lease = repository.acquire_lease(
            "agent-execution", ttl_seconds=DEFAULT_LEASE_TTL_SECONDS
        )
        try:
            repository.begin_agent_execution_artifact(
                claim,
                runs_root=runs_root,
                worktree_path=worktree,
                branch_name=branch,
                base_commit=primary_head,
                started_at=artifact_started,
                lease=lease,
            )
        finally:
            repository.release_lease(lease)
    is_continuous = _is_continuous(claim.venue_id)
    invocation = CodexInvocation(
        (
            config.codex_binary, "--ask-for-approval", "never", "exec",
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "workspace-write", "--cd", str(worktree),
            "--config", 'mcp_servers={}', "--config", 'web_search="cached"',
            *_network_arguments(claim.venue_id),
            "--output-schema", str(RESULT_SCHEMA),
            _prompt(claim, policy, is_continuous=is_continuous),
        ),
        worktree,
        config.timeout_seconds,
    )
    timed_out = False
    returncode = None
    try:
        process = invoker.invoke(invocation)
        returncode = process.returncode
        if len(process.stdout.encode()) + len(process.stderr.encode()) \
                > config.max_output_bytes:
            result = AgentRunResult("failed", "Codex output exceeded the limit.", failure_category="output_limit")
        elif process.returncode != 0:
            result = AgentRunResult("failed", "Codex exited unsuccessfully.", failure_category="process_exit")
        else:
            try:
                result = parse_codex_result(process.stdout)
            except ValueError:
                result = AgentRunResult("failed", "Codex returned invalid structured output.", failure_category="invalid_result")
    except subprocess.TimeoutExpired:
        timed_out = True
        result = AgentRunResult("failed", "Codex exceeded its execution timeout.", failure_category="timeout")
    if _git(worktree, "rev-parse", "HEAD") != primary_head:
        result = AgentRunResult("needs_human", "Codex changed Git HEAD; automatic handling stopped.")
    if _git(repository_root, "rev-parse", "HEAD") != primary_head or _git(
        repository_root, "status", "--porcelain=v1", "--untracked-files=all"
    ) != primary_status:
        raise RuntimeError("primary checkout changed during agent execution")
    all_changed = tuple(filter(None, _git(
        worktree, "status", "--porcelain=v1", "--untracked-files=all"
    ).splitlines()))
    changed = all_changed[:config.max_changed_files]
    if len(all_changed) > config.max_changed_files:
        result = AgentRunResult(
            "failed", "Changed-file inventory exceeded the retained limit.",
            failure_category="inventory_limit",
        )
    complete_agent_run(
        state_path, claim, result, clock=clock, policy=policy,
        changed_files=changed, returncode=returncode, timed_out=timed_out,
        is_continuous=is_continuous,
    )
    return CodexExecutionOutcome(result, worktree, branch, primary_head, changed, returncode, timed_out)
