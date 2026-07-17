"""Serve a loopback-only, read-only view of venue automation state."""

from __future__ import annotations

import argparse
import html
import json
import os
import stat
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from automation.agent_status import AgentStatusError, read_agent_state_summary
from automation.configuration import load_venue_catalog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import METADATA_DIR  # noqa: E402


_BIND = "127.0.0.1"
_MAX_TARGETS = 100
_ACTIVE_PHASES = frozenset({"Agent running", "Date lookup running"})
_ATTENTION_PHASES = frozenset({"Needs human", "Paused"})
_PROGRESS_COLORS = {
    "active": "#a78bfa",
    "due": "#fb923c",
    "soon": "#f2c14e",
    "later": "#60a5fa",
    "far": "#64748b",
    "attention": "#f87171",
    "done": "#4ade80",
    "none": "#3a4763",
}


def _remaining_label(next_attempt: datetime, observed_at: datetime) -> tuple[str, str]:
    """Return (human countdown text, urgency color category) for a next check."""
    remaining = next_attempt - observed_at
    seconds = remaining.total_seconds()
    if seconds <= 0:
        return "due now", "due"
    days = remaining.days
    hours = int(seconds // 3600)
    if days >= 60:
        text = f"in {days // 30}mo"
    elif days >= 2:
        text = f"in {days}d"
    elif hours >= 1:
        text = f"in {hours}h"
    else:
        text = "in <1h"
    if days < 1:
        return text, "due"
    if days < 7:
        return text, "soon"
    if days < 30:
        return text, "later"
    return text, "far"


class AgentDashboardError(ValueError):
    """Raised when dashboard input or listener configuration is unsafe."""


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise AgentDashboardError("dashboard clock is invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise AgentDashboardError("dashboard timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentDashboardError("dashboard timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AgentDashboardError("dashboard timestamp is invalid")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _latest_timestamp(*values: object) -> str | None:
    timestamps = tuple(filter(None, (_timestamp(value) for value in values)))
    return max(timestamps) if timestamps else None


def _target_phase(target: Mapping[str, object]) -> str:
    agent = target.get("agent")
    event_date = target.get("event_date")
    if isinstance(agent, Mapping):
        status = agent.get("status")
        labels = {
            "scheduled": "Scheduled",
            "active": "Agent running",
            "completed": "Completed",
            "needs_human": "Needs human",
            "paused": "Paused",
        }
        if status not in labels:
            raise AgentDashboardError("dashboard agent status is invalid")
        return labels[str(status)]
    if isinstance(event_date, Mapping):
        status = event_date.get("status")
        labels = {
            "pending": "Waiting for date",
            "active": "Date lookup running",
            "scheduled": "Waiting for agent schedule",
        }
        if status not in labels:
            raise AgentDashboardError("dashboard date status is invalid")
        return labels[str(status)]
    raise AgentDashboardError("dashboard target has no lifecycle state")


def _scan_last_downloaded(metadata_root: Path, venue_id: str) -> dict[str, object] | None:
    """Return the most recent scraped year and its file mtime, or None.

    Reads only directory entries and ``stat()`` mtimes under
    ``metadata_root/<venue_id>/`` (never file contents), mirroring the glob
    pattern ``postprocessing/generate_statistics.py::scan`` already uses.
    Any filesystem problem degrades to ``None`` rather than raising, since
    this is supplementary dataset evidence, not authoritative control state.
    """
    try:
        candidates = list((Path(metadata_root) / venue_id).glob("*.json"))
    except OSError:
        return None
    best_year: int | None = None
    best_mtime: float | None = None
    for path in candidates:
        try:
            year = int(path.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best_year is None or year > best_year:
            best_year, best_mtime = year, mtime
    if best_year is None or best_mtime is None:
        return None
    return {
        "year": best_year,
        "observed_at": datetime.fromtimestamp(best_mtime, tz=timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    }


def _urgency_rank(current_target: Mapping[str, object] | None) -> tuple[int, str]:
    """Rank rows so the venue closest to producing new data sorts first."""
    if current_target is None:
        return (4, "")
    phase = str(current_target["phase"])
    next_attempt = current_target.get("next_attempt_at")
    if phase in _ACTIVE_PHASES:
        return (0, "")
    if next_attempt is not None:
        return (1, str(next_attempt))
    if phase in _ATTENTION_PHASES:
        return (2, "")
    if phase == "Completed":
        return (3, "")
    return (4, "")


def _progress(
    current_target: Mapping[str, object] | None, observed_at: datetime,
) -> dict[str, object]:
    """Summarize a row as a countdown to its next check plus a color category.

    ``label`` is what the bar's caption shows: the time remaining until the
    next check for waiting rows ("in 3d" / "due now"), or the phase itself
    for rows with no future check (running, completed, needs human, paused,
    not enrolled). ``category`` picks the bar color: urgency buckets for
    waiting rows (due <1d, soon <7d, later <30d, far beyond), fixed colors
    otherwise. ``fraction`` is the remaining time itself on a 30-day scale
    (full = a month or more away, empty = due now), so the bar drains as
    the check approaches, like a countdown.
    """
    if current_target is None:
        return {"fraction": 0.0, "category": "none", "label": "Not enrolled"}
    phase = str(current_target["phase"])
    if phase in _ACTIVE_PHASES:
        return {"fraction": 1.0, "category": "active", "label": phase}
    if phase in _ATTENTION_PHASES:
        return {"fraction": 1.0, "category": "attention", "label": phase}
    if phase == "Completed":
        return {"fraction": 1.0, "category": "done", "label": phase}
    next_attempt = current_target.get("next_attempt_at")
    if not isinstance(next_attempt, str):
        return {"fraction": 0.0, "category": "none", "label": phase}
    end = datetime.fromisoformat(next_attempt.replace("Z", "+00:00"))
    text, category = _remaining_label(end, observed_at)
    remaining = (end - observed_at).total_seconds()
    fraction = max(0.0, min(1.0, remaining / (30 * 86400)))
    return {"fraction": fraction, "category": category, "label": text}


def build_dashboard_model(
    catalog: Mapping[str, object],
    targets: Sequence[Mapping[str, object]],
    *,
    observed_at: datetime,
    metadata_root: Path | None = None,
) -> dict[str, object]:
    """Join bounded state to every catalog venue without adding authority."""
    venues = catalog.get("venues") if isinstance(catalog, Mapping) else None
    if not isinstance(venues, list) or not venues or len(venues) > 100 \
            or len(targets) > _MAX_TARGETS:
        raise AgentDashboardError("dashboard catalog or target bound is invalid")
    by_id: dict[str, dict[str, object]] = {}
    for venue in venues:
        if not isinstance(venue, Mapping):
            raise AgentDashboardError("dashboard catalog is invalid")
        venue_id = venue.get("venue_id")
        display_name = venue.get("display_name")
        lifecycle = venue.get("lifecycle")
        scraper = venue.get("scraper")
        if not isinstance(venue_id, str) or not venue_id \
                or not isinstance(display_name, str) or not display_name \
                or len(display_name) > 256 or not isinstance(lifecycle, Mapping) \
                or lifecycle.get("kind") not in {"annual", "continuous"} \
                or not isinstance(scraper, Mapping) \
                or type(scraper.get("monitor_registered")) is not bool \
                or venue_id in by_id:
            raise AgentDashboardError("dashboard catalog is invalid")
        by_id[venue_id] = {
            "venue_id": venue_id,
            "display_name": display_name,
            "lifecycle_kind": lifecycle["kind"],
            "source_monitor": (
                "Configured" if scraper["monitor_registered"] else "Not configured"
            ),
            "targets": [],
        }

    seen: set[tuple[str, int]] = set()
    for target in targets:
        if not isinstance(target, Mapping):
            raise AgentDashboardError("dashboard target is invalid")
        venue_id = target.get("venue_id")
        year = target.get("year")
        if venue_id not in by_id or not isinstance(year, int) \
                or isinstance(year, bool) or not 2020 <= year <= 2200 \
                or (str(venue_id), year) in seen:
            raise AgentDashboardError("dashboard target identity is invalid")
        seen.add((str(venue_id), year))
        event_date = target.get("event_date")
        agent = target.get("agent")
        attempt = target.get("latest_attempt")
        report = target.get("latest_report")
        for value in (event_date, agent, attempt, report):
            if value is not None and not isinstance(value, Mapping):
                raise AgentDashboardError("dashboard target state is invalid")
        next_attempt = None
        if isinstance(agent, Mapping):
            # Once an agent schedule exists, its next_check_at is the sole
            # executable clock (may legitimately be None: active/completed/
            # needs_human/paused all mean "no automatic next check"). Only
            # fall back to the immutable pre-handoff event-date schedule when
            # no agent schedule has been created yet.
            next_attempt = agent.get("next_check_at")
        elif isinstance(event_date, Mapping):
            next_attempt = event_date.get("next_check_at")
        last_updated = _latest_timestamp(
            event_date.get("updated_at") if isinstance(event_date, Mapping) else None,
            agent.get("updated_at") if isinstance(agent, Mapping) else None,
            attempt.get("completed_at") if isinstance(attempt, Mapping) else None,
            attempt.get("started_at") if isinstance(attempt, Mapping) else None,
            report.get("delivered_at") if isinstance(report, Mapping) else None,
        )
        disposition = (
            agent.get("last_disposition") if isinstance(agent, Mapping) else None
        ) or (
            attempt.get("disposition") if isinstance(attempt, Mapping) else None
        )
        if disposition is not None and disposition not in {
            "success", "not_ready", "needs_human", "failed", "active"
        }:
            raise AgentDashboardError("dashboard disposition is invalid")
        report_status = report.get("status") if isinstance(report, Mapping) else None
        if report_status is not None and report_status not in {
            "pending", "in_flight", "retryable", "delivered", "permanent_failure"
        }:
            raise AgentDashboardError("dashboard report status is invalid")
        by_id[str(venue_id)]["targets"].append({
            "year": year,
            "phase": _target_phase(target),
            "last_updated_at": last_updated,
            "next_attempt_at": _timestamp(next_attempt),
            "last_disposition": disposition,
            "report_status": report_status,
        })

    resolved = []
    for venue_id in sorted(by_id):
        venue = by_id[venue_id]
        target_rows = sorted(venue.pop("targets"), key=lambda item: int(item["year"]))
        current = target_rows[-1] if target_rows else None
        venue["enrolled"] = bool(target_rows)
        venue["current_target"] = current
        venue["last_downloaded"] = (
            _scan_last_downloaded(metadata_root, venue_id)
            if metadata_root is not None else None
        )
        venue["urgency_rank"] = _urgency_rank(current)
        venue["progress"] = _progress(current, observed_at)
        resolved.append(venue)
    resolved.sort(key=lambda venue: venue["urgency_rank"])
    return {
        "observed_at": _utc_text(observed_at),
        "venue_count": len(resolved),
        "enrolled_venue_count": sum(bool(item["enrolled"]) for item in resolved),
        "target_count": len(targets),
        "venues": resolved,
    }


def _cell(value: object, *, css: str = "") -> str:
    text = "—" if value is None else str(value)
    class_name = f' class="{html.escape(css, quote=True)}"' if css else ""
    return f"<td{class_name}>{html.escape(text)}</td>"


def _date_cell(value_iso: object, *, tooltip_prefix: str = "") -> str:
    if not isinstance(value_iso, str):
        return "<td>—</td>"
    text = html.escape(value_iso[:10])
    tooltip = html.escape(f"{tooltip_prefix}{value_iso}", quote=True)
    return f'<td title="{tooltip}">{text}</td>'


def _progress_cell(progress: Mapping[str, object], phase_label: str) -> str:
    category = str(progress.get("category", "none"))
    color = _PROGRESS_COLORS.get(category, _PROGRESS_COLORS["none"])
    try:
        fraction = float(progress.get("fraction", 0.0))
    except (TypeError, ValueError):
        fraction = 0.0
    width = round(max(0.0, min(1.0, fraction)) * 100, 1)
    bar = (
        '<div class="bar-track">'
        f'<div class="bar-fill" style="width:{width}%;background:{color};"></div>'
        "</div>"
    )
    text = str(progress.get("label") or phase_label)
    # The countdown carries the urgency color; a phase-only label stays muted.
    styled = f' style="color:{color};"' if text != phase_label else ""
    label = (
        f'<div class="phase-label"{styled} '
        f'title="{html.escape(phase_label, quote=True)}">'
        f"{html.escape(text)}</div>"
    )
    return f"<td>{bar}{label}</td>"


def render_dashboard(model: Mapping[str, object]) -> str:
    """Render one standalone escaped document with no external resources."""
    venues = model.get("venues")
    if not isinstance(venues, list):
        raise AgentDashboardError("dashboard model is invalid")
    rows: list[str] = []
    for venue in venues:
        if not isinstance(venue, Mapping):
            raise AgentDashboardError("dashboard model is invalid")
        current = venue.get("current_target")
        if current is not None and not isinstance(current, Mapping):
            raise AgentDashboardError("dashboard model is invalid")
        progress = venue.get("progress")
        if not isinstance(progress, Mapping):
            raise AgentDashboardError("dashboard model is invalid")
        last_downloaded = venue.get("last_downloaded")
        if last_downloaded is not None and not isinstance(last_downloaded, Mapping):
            raise AgentDashboardError("dashboard model is invalid")

        venue_id = str(venue.get("venue_id"))
        display_name = str(venue.get("display_name"))
        phase = str(current["phase"]) if isinstance(current, Mapping) else "Not enrolled"
        badge = (
            ' <span class="badge-warn" '
            'title="No deterministic monitor source configured">&#9888;</span>'
            if venue.get("source_monitor") == "Not configured" else ""
        )
        venue_cell = (
            f'<td class="venue" title="{html.escape(display_name, quote=True)}">'
            f"{html.escape(venue_id.upper())}{badge}</td>"
        )
        last_downloaded_at = (
            last_downloaded.get("observed_at") if isinstance(last_downloaded, Mapping)
            else None
        )
        last_downloaded_prefix = (
            f"{venue_id.upper()} {last_downloaded.get('year')} — "
            if isinstance(last_downloaded, Mapping) else ""
        )
        next_attempt_at = (
            current.get("next_attempt_at") if isinstance(current, Mapping) else None
        )
        next_attempt_prefix = (
            f"{venue_id.upper()} {current.get('year')} — "
            if isinstance(current, Mapping) else ""
        )
        row = "".join((
            venue_cell,
            _progress_cell(progress, phase),
            _date_cell(last_downloaded_at, tooltip_prefix=last_downloaded_prefix),
            _date_cell(next_attempt_at, tooltip_prefix=next_attempt_prefix),
            _cell(current.get("last_disposition") if isinstance(current, Mapping) else None),
            _cell(current.get("report_status") if isinstance(current, Mapping) else None),
        ))
        rows.append(f"<tr>{row}</tr>")
    observed_at = html.escape(str(model.get("observed_at", "unknown")))
    counts = (
        f"{int(model.get('enrolled_venue_count', 0))} enrolled venues · "
        f"{int(model.get('target_count', 0))} venue/year targets · "
        f"{int(model.get('venue_count', 0))} catalog venues"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>OpenPapers automation status</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ margin: 0; background: #0b1020; color: #e8edf7; }}
main {{ max-width: 1100px; margin: auto; padding: 32px 24px; }}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
.meta {{ color: #aab6ca; margin-bottom: 24px; }}
.notice {{ border: 1px solid #334363; background: #111a30; border-radius: 10px;
  padding: 12px 14px; margin-bottom: 18px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #283653; border-radius: 12px; }}
table {{ width: 100%; border-collapse: collapse; background: #10182b; }}
th, td {{ text-align: left; padding: 11px 12px; border-bottom: 1px solid #26344f;
  white-space: nowrap; }}
th {{ color: #aab6ca; font-size: 12px; text-transform: uppercase;
  letter-spacing: .05em; background: #151f36; position: sticky; top: 0; }}
tr:last-child td {{ border-bottom: 0; }}
.venue {{ font-weight: 700; color: #8bc6ff; font-size: 15px; letter-spacing: .02em; }}
.badge-warn {{ color: #f2c14e; cursor: help; }}
.bar-track {{ width: 130px; height: 6px; border-radius: 3px; background: #202b46;
  overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 3px; }}
.phase-label {{ margin-top: 5px; font-size: 12px; color: #aab6ca; white-space: nowrap; }}
footer {{ color: #8491a8; margin-top: 18px; font-size: 13px; }}
</style>
</head>
<body><main>
<h1>OpenPapers automation status</h1>
<div class="meta">{html.escape(counts)} · observed {observed_at}</div>
<div class="notice">Read-only local view. Dates are scheduling hints; only the
coding agent decides publication readiness. Rows are ordered by which venue is
likely to produce new data soonest. This page performs no action.</div>
<div class="table-wrap"><table>
<thead><tr><th>Venue</th><th>Next check</th><th>Last downloaded (UTC)</th>
<th>Next attempt (UTC)</th><th>Disposition</th><th>Report</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
<footer>Refreshes every 60 seconds from immutable SQLite reads plus a
read-only scan of the local scraped-dataset tree.</footer>
</main></body></html>"""


def build_dashboard_document(
    state_path: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    metadata_root: Path = METADATA_DIR,
) -> bytes:
    """Build the current page from catalog plus one immutable state read."""
    model = build_dashboard_model(
        load_venue_catalog(),
        read_agent_state_summary(Path(state_path)),
        observed_at=clock(),
        metadata_root=metadata_root,
    )
    return render_dashboard(model).encode("utf-8")


class _DashboardServer(HTTPServer):
    state_path: Path
    clock: Callable[[], datetime]
    metadata_root: Path


class _DashboardHandler(BaseHTTPRequestHandler):
    server: _DashboardServer

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        route = urlsplit(self.path).path
        if route == "/healthz":
            self._send(200, b'{"status":"ok"}\n', "application/json")
            return
        if route not in {"/", "/index.html"}:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            body = build_dashboard_document(
                self.server.state_path, clock=self.server.clock,
                metadata_root=self.server.metadata_root,
            )
        except (AgentDashboardError, AgentStatusError, OSError, ValueError):
            self._send(503, b"status unavailable\n", "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/html; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_dashboard_server(
    state_path: Path,
    *,
    bind: str = _BIND,
    port: int = 8765,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    metadata_root: Path = METADATA_DIR,
) -> HTTPServer:
    """Create a loopback-only reader; no request can mutate scheduler state."""
    path = Path(state_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentDashboardError("dashboard state is unavailable") from exc
    if not path.is_absolute() or path.is_symlink() \
            or not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.R_OK):
        raise AgentDashboardError("dashboard state is unsafe")
    if bind != _BIND:
        raise AgentDashboardError("dashboard must bind to 127.0.0.1")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise AgentDashboardError("dashboard port is invalid")
    server = _DashboardServer((bind, port), _DashboardHandler)
    server.state_path = path
    server.clock = clock
    server.metadata_root = Path(metadata_root)
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--bind", default=_BIND)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--metadata-root", default=METADATA_DIR, type=Path)
    args = parser.parse_args(argv)
    try:
        server = create_dashboard_server(
            args.state, bind=args.bind, port=args.port,
            metadata_root=args.metadata_root,
        )
    except AgentDashboardError:
        print(json.dumps({"dashboard": "blocked", "reason": "unsafe_configuration"}))
        return 2
    host, port = server.server_address
    print(json.dumps({
        "dashboard": "listening",
        "bind": host,
        "port": port,
        "read_only": True,
    }, sort_keys=True), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
