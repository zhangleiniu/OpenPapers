"""Uninstalled Codex CLI runner confined to an isolated Git worktree."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from automation.control_state import AgentRunClaim
from automation.due_policy import AgentRunResult, DuePolicy, complete_agent_run


RESULT_SCHEMA = Path(__file__).with_name("schemas") / "v1" / "agent-run-result.json"


@dataclass(frozen=True)
class CodexRunConfig:
    codex_binary: str = "codex"
    timeout_seconds: int = 3600
    max_output_bytes: int = 64_000


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
    def invoke(self, invocation: CodexInvocation) -> CodexProcessResult:
        completed = subprocess.run(
            invocation.argv,
            cwd=invocation.cwd,
            env=os.environ.copy(),
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


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(args)}")
    return completed.stdout.strip()


def _prompt(claim: AgentRunClaim) -> str:
    return f"""Handle {claim.venue_id} {claim.year} for OpenPapers.
Decide whether the canonical papers and PDFs are now downloadable. Investigate
the web and repository, reuse or repair the scraper, run the scrape and required
validation when ready, and leave all useful edits in this worktree. Never
commit, push, merge, deploy, edit Git metadata, or modify another checkout.
Return only the required structured result. Use success only after scrape and
validation complete; not_ready for unpublished data; needs_human for policy or
access ambiguity; failed for operational or code failure."""


def _parse_result(raw: str) -> AgentRunResult:
    try:
        body = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex returned malformed structured output") from exc
    required = {"disposition", "explanation", "suggested_retry_at", "failure_category"}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError("Codex result fields are invalid")
    suggested = body["suggested_retry_at"]
    if suggested is not None:
        try:
            suggested = datetime.fromisoformat(suggested.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("Codex retry time is invalid") from exc
    return AgentRunResult(
        body["disposition"], body["explanation"], suggested, body["failure_category"]
    )


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
    invocation = CodexInvocation(
        (
            config.codex_binary, "--ask-for-approval", "never", "exec",
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "workspace-write", "--cd", str(worktree),
            "--config", 'mcp_servers={}', "--config", 'web_search="cached"',
            "--output-schema", str(RESULT_SCHEMA), _prompt(claim),
        ),
        worktree,
        config.timeout_seconds,
    )
    timed_out = False
    returncode = None
    try:
        process = invoker.invoke(invocation)
        returncode = process.returncode
        if len(process.stdout.encode()) > config.max_output_bytes:
            result = AgentRunResult("failed", "Codex output exceeded the limit.", failure_category="output_limit")
        elif process.returncode != 0:
            result = AgentRunResult("failed", "Codex exited unsuccessfully.", failure_category="process_exit")
        else:
            try:
                result = _parse_result(process.stdout)
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
    changed = tuple(filter(None, _git(
        worktree, "status", "--porcelain=v1", "--untracked-files=all"
    ).splitlines()))
    complete_agent_run(state_path, claim, result, clock=clock, policy=policy)
    return CodexExecutionOutcome(result, worktree, branch, primary_head, changed, returncode, timed_out)
