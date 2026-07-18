"""Serve a loopback-only, read-only view of venue automation state.

Each catalog venue renders as one perpetual-cycle row: its last held
edition, the next expected edition, and the scheduler's next attempt, with
a color-coded countdown to whichever check comes next. Edition dates merge
the control state's own estimated event dates with the curated
``automation/config/venue_editions.v1.json`` (verified dates win) and fall
back to a cadence approximation marked with ``~``. Timestamps default to
America/Chicago; a client-side selector re-renders them in other zones
(inline script only — the strict no-external-resource CSP still applies).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import stat
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from automation.agent_status import AgentStatusError, read_agent_state_summary
from automation.configuration import load_venue_catalog


_BIND = "127.0.0.1"
_MAX_TARGETS = 100
_DEFAULT_TIMEZONE = "America/Chicago"
_ACTIVE_PHASES = frozenset({"Agent running", "Date lookup running"})
_ATTENTION_PHASES = frozenset({"Needs human", "Paused"})
_PROGRESS_COLORS = {
    "active": "#a78bfa",
    "due": "#fb923c",
    "soon": "#f2c14e",
    "later": "#60a5fa",
    "far": "#64748b",
    "attention": "#f87171",
    "none": "#3a4763",
}
DEFAULT_VENUE_EDITIONS = (
    Path(__file__).with_name("config") / "venue_editions.v1.json"
)


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


def load_venue_editions(
    path: Path = DEFAULT_VENUE_EDITIONS,
) -> dict[str, list[dict[str, Any]]]:
    """Load the curated per-venue edition dates, strictly validated."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentDashboardError("venue editions are unavailable") from exc
    if not isinstance(payload, dict) \
            or payload.get("schema_version") != 1 \
            or not isinstance(payload.get("editions"), list) \
            or len(payload["editions"]) > 500:
        raise AgentDashboardError("venue editions are invalid")
    editions: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int]] = set()
    for item in payload["editions"]:
        if not isinstance(item, dict) \
                or not {"venue_id", "year", "start_date"} <= set(item) \
                or set(item) - {"venue_id", "year", "start_date", "label"}:
            raise AgentDashboardError("venue editions are invalid")
        venue_id, year = item["venue_id"], item["year"]
        if not isinstance(venue_id, str) or not isinstance(year, int) \
                or isinstance(year, bool) or not 2000 <= year <= 2200 \
                or (venue_id, year) in seen:
            raise AgentDashboardError("venue editions are invalid")
        start = date.fromisoformat(item["start_date"])
        label = item.get("label")
        if label is not None and (
            not isinstance(label, str) or not 1 <= len(label) <= 32
        ):
            raise AgentDashboardError("venue editions are invalid")
        seen.add((venue_id, year))
        editions.setdefault(venue_id, []).append({
            "year": year, "start_date": start, "label": label,
        })
    return editions


def _resolve_editions(
    venue_id: str,
    lifecycle: Mapping[str, Any],
    curated: Sequence[Mapping[str, Any]],
    db_dates: Mapping[int, str],
    today: date,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Merge curated and control-state edition dates into last/next.

    Curated (web-verified) entries win over the control state's estimates
    for the same year. When no future edition is known, approximate one
    from the last edition plus the venue's cadence, month precision only.
    """
    merged: dict[int, dict[str, Any]] = {}
    for year, iso in db_dates.items():
        merged[year] = {
            "year": year, "start_date": date.fromisoformat(iso),
            "label": None, "approx": False,
        }
    for item in curated:
        merged[item["year"]] = {
            "year": item["year"], "start_date": item["start_date"],
            "label": item["label"], "approx": False,
        }
    ordered = sorted(merged.values(), key=lambda item: item["start_date"])
    last = None
    next_edition = None
    for item in ordered:
        if item["start_date"] <= today:
            last = item
        elif next_edition is None:
            next_edition = item
    if next_edition is None and last is not None:
        interval = lifecycle.get("interval_years") or 1
        approx_year = last["year"] + interval
        next_edition = {
            "year": approx_year,
            "start_date": date(
                approx_year, last["start_date"].month, 1
            ),
            "label": None,
            "approx": True,
        }
    return last, next_edition


def _edition_view(
    venue_id: str, edition: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if edition is None:
        return None
    name = edition["label"] or f"{venue_id.upper()} {edition['year']}"
    start = edition["start_date"]
    return {
        "name": name,
        "date": f"~{start.strftime('%Y-%m')}" if edition["approx"]
        else start.isoformat(),
        "iso_date": start.isoformat(),
        "approx": bool(edition["approx"]),
    }


def _target_phase(target: Mapping[str, object]) -> str:
    agent = target.get("agent")
    event_date = target.get("event_date")
    if isinstance(agent, Mapping):
        status = agent.get("status")
        labels = {
            "scheduled": "Scheduled",
            "active": "Agent running",
            "completed": "Collected",
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


def _remaining_label(next_attempt: datetime, observed_at: datetime) -> tuple[str, str]:
    """Return (human countdown text, urgency color category) for a check."""
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


def _cycle_progress(
    current_target: Mapping[str, object] | None,
    next_edition: Mapping[str, Any] | None,
    observed_at: datetime,
) -> dict[str, object]:
    """Summarize the row as a countdown plus a color category.

    Rows with a scheduled next attempt count down to it. Rows whose current
    collection finished (or that are not enrolled yet) count down to the
    next expected edition instead — the cycle never dead-ends. Active and
    needs-attention states keep fixed colors. ``fraction`` is the remaining
    time on a 30-day scale (full = a month or more away, empty = due now).
    """
    phase = str(current_target["phase"]) if current_target else None
    if phase in _ACTIVE_PHASES:
        return {"fraction": 1.0, "category": "active", "label": phase}
    if phase in _ATTENTION_PHASES:
        return {"fraction": 1.0, "category": "attention", "label": phase}
    next_attempt = (
        current_target.get("next_attempt_at") if current_target else None
    )
    if isinstance(next_attempt, str):
        end = datetime.fromisoformat(next_attempt.replace("Z", "+00:00"))
        text, category = _remaining_label(end, observed_at)
        remaining = (end - observed_at).total_seconds()
        return {
            "fraction": max(0.0, min(1.0, remaining / (30 * 86400))),
            "category": category,
            "label": text,
        }
    if next_edition is not None:
        end = datetime.combine(
            date.fromisoformat(str(next_edition["iso_date"])),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        text, category = _remaining_label(end, observed_at)
        return {
            "fraction": max(
                0.0, min(1.0, (end - observed_at).total_seconds() / (30 * 86400))
            ),
            "category": category,
            "label": f"next: {next_edition['name']}",
        }
    return {"fraction": 0.0, "category": "none", "label": phase or "No cycle data"}


def _status_view(current_target: Mapping[str, object] | None) -> dict[str, Any]:
    """Merge phase, last disposition, and report-delivery health."""
    if current_target is None:
        return {"text": "Not enrolled", "warning": None}
    phase = str(current_target["phase"])
    disposition = current_target.get("last_disposition")
    text = phase if disposition is None else f"{phase} · {disposition}"
    report_status = current_target.get("report_status")
    warning = None
    if report_status in {"retryable", "permanent_failure"}:
        warning = f"report delivery {report_status}"
    return {"text": text, "warning": warning}


def build_dashboard_model(
    catalog: Mapping[str, object],
    targets: Sequence[Mapping[str, object]],
    *,
    observed_at: datetime,
    editions: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, object]:
    """Join bounded state to every catalog venue without adding authority."""
    venues = catalog.get("venues") if isinstance(catalog, Mapping) else None
    if not isinstance(venues, list) or not venues or len(venues) > 100 \
            or len(targets) > _MAX_TARGETS:
        raise AgentDashboardError("dashboard catalog or target bound is invalid")
    editions = editions or {}
    today = observed_at.astimezone(timezone.utc).date()
    by_id: dict[str, dict[str, object]] = {}
    lifecycles: dict[str, Mapping[str, Any]] = {}
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
        lifecycles[venue_id] = lifecycle
        by_id[venue_id] = {
            "venue_id": venue_id,
            "display_name": display_name,
            "lifecycle_kind": lifecycle["kind"],
            "source_monitor": (
                "Configured" if scraper["monitor_registered"] else "Not configured"
            ),
            "targets": [],
            "db_dates": {},
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
            # executable clock (legitimately None for active/completed/
            # needs_human/paused). Only fall back to the pre-handoff
            # event-date schedule when no agent schedule exists yet.
            next_attempt = agent.get("next_check_at")
        elif isinstance(event_date, Mapping):
            next_attempt = event_date.get("next_check_at")
        estimated = (
            event_date.get("estimated_event_date")
            if isinstance(event_date, Mapping) else None
        )
        if estimated is not None:
            if not isinstance(estimated, str):
                raise AgentDashboardError("dashboard event date is invalid")
            date.fromisoformat(estimated)
            by_id[str(venue_id)]["db_dates"][year] = estimated
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
        db_dates = venue.pop("db_dates")
        last, next_edition = _resolve_editions(
            venue_id, lifecycles[venue_id], editions.get(venue_id, ()),
            db_dates, today,
        )
        venue["enrolled"] = bool(target_rows)
        venue["current_target"] = current
        venue["last_edition"] = _edition_view(venue_id, last)
        venue["next_edition"] = _edition_view(venue_id, next_edition)
        venue["progress"] = _cycle_progress(
            current, venue["next_edition"], observed_at
        )
        venue["status"] = _status_view(current)
        resolved.append(venue)

    def _sort_key(venue: Mapping[str, Any]) -> tuple:
        current = venue["current_target"]
        phase = str(current["phase"]) if current else None
        if phase in _ACTIVE_PHASES:
            return (0, "")
        if isinstance(current, Mapping) and current.get("next_attempt_at"):
            return (1, current["next_attempt_at"])
        if phase in _ATTENTION_PHASES:
            return (2, "")
        next_edition = venue["next_edition"]
        if next_edition is not None:
            return (3, next_edition["iso_date"])
        return (4, venue["venue_id"])

    resolved.sort(key=_sort_key)
    return {
        "observed_at": _utc_text(observed_at),
        "venue_count": len(resolved),
        "enrolled_venue_count": sum(bool(item["enrolled"]) for item in resolved),
        "target_count": len(targets),
        "venues": resolved,
    }


def _chicago_text(iso_utc: str, *, with_time: bool) -> str:
    parsed = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    local = parsed.astimezone(ZoneInfo(_DEFAULT_TIMEZONE))
    return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def _attempt_cell(value_iso: object) -> str:
    """A timestamp cell: Chicago text by default, retargetable client-side."""
    if not isinstance(value_iso, str):
        return "<td>—</td>"
    text = html.escape(_chicago_text(value_iso, with_time=True))
    utc = html.escape(value_iso, quote=True)
    return f'<td><span data-utc="{utc}">{text}</span></td>'


def _edition_cell(edition: object) -> str:
    if not isinstance(edition, Mapping):
        return "<td>—</td>"
    name = html.escape(str(edition["name"]))
    when = html.escape(str(edition["date"]))
    return f'<td><span class="edition-name">{name}</span> · {when}</td>'


def _progress_cell(progress: Mapping[str, object]) -> str:
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
    label = (
        f'<div class="phase-label" style="color:{color};">'
        f"{html.escape(str(progress.get('label') or '')) or '&#8212;'}</div>"
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
        progress = venue.get("progress")
        status = venue.get("status")
        if (current is not None and not isinstance(current, Mapping)) \
                or not isinstance(progress, Mapping) \
                or not isinstance(status, Mapping):
            raise AgentDashboardError("dashboard model is invalid")
        venue_id = str(venue.get("venue_id"))
        display_name = str(venue.get("display_name"))
        badge = (
            ' <span class="badge-warn" '
            'title="No deterministic monitor source configured">&#9888;</span>'
            if venue.get("source_monitor") == "Not configured" else ""
        )
        warning = status.get("warning")
        status_html = html.escape(str(status.get("text", "")))
        if warning:
            status_html += (
                ' <span class="badge-warn" '
                f'title="{html.escape(str(warning), quote=True)}">&#9993;</span>'
            )
        row = "".join((
            f'<td class="venue" title="{html.escape(display_name, quote=True)}">'
            f"{html.escape(venue_id.upper())}{badge}</td>",
            _progress_cell(progress),
            _edition_cell(venue.get("last_edition")),
            _edition_cell(venue.get("next_edition")),
            _attempt_cell(current.get("next_attempt_at")
                          if isinstance(current, Mapping) else None),
            f"<td>{status_html}</td>",
        ))
        rows.append(f"<tr>{row}</tr>")
    observed_at_iso = str(model.get("observed_at", ""))
    observed = html.escape(_chicago_text(observed_at_iso, with_time=True)) \
        if observed_at_iso else "unknown"
    observed_attr = html.escape(observed_at_iso, quote=True)
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
<meta http-equiv="refresh" content="300">
<title>OpenPapers automation status</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ margin: 0; background: #0b1020; color: #e8edf7; }}
main {{ max-width: 1200px; margin: auto; padding: 32px 24px; }}
.topbar {{ display: flex; justify-content: space-between; align-items: baseline; }}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
.tz-picker {{ color: #aab6ca; font-size: 13px; }}
.tz-picker select {{ background: #151f36; color: #e8edf7; border: 1px solid #334363;
  border-radius: 6px; padding: 3px 6px; font-size: 13px; }}
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
.edition-name {{ color: #cdd7e8; font-weight: 600; }}
.badge-warn {{ color: #f2c14e; cursor: help; }}
.bar-track {{ width: 130px; height: 6px; border-radius: 3px; background: #202b46;
  overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 3px; }}
.phase-label {{ margin-top: 5px; font-size: 12px; color: #aab6ca; white-space: nowrap; }}
footer {{ color: #8491a8; margin-top: 18px; font-size: 13px; }}
</style>
</head>
<body><main>
<div class="topbar">
<h1>OpenPapers automation status</h1>
<div class="tz-picker">Timezone
<select id="tz-select">
<option value="America/Chicago">Chicago</option>
<option value="UTC">UTC</option>
<option value="America/New_York">New York</option>
<option value="America/Los_Angeles">Los Angeles</option>
<option value="Europe/London">London</option>
<option value="Europe/Berlin">Berlin</option>
<option value="Asia/Shanghai">Shanghai</option>
<option value="Asia/Tokyo">Tokyo</option>
</select></div>
</div>
<div class="meta">{html.escape(counts)} · observed
<span data-utc="{observed_attr}">{observed}</span></div>
<div class="notice">Read-only local view. Edition dates are calendar facts or
estimates (&#126; marks a cadence approximation); attempt times are the
scheduler's clock. Only the coding agent decides publication readiness. This
page performs no action.</div>
<div class="table-wrap"><table>
<thead><tr><th>Venue</th><th>Next check</th><th>Last edition</th>
<th>Next edition</th><th>Next attempt</th><th>Status</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
<footer>Refreshes every 5 minutes from immutable SQLite reads plus the
curated edition calendar. Timestamps shown in the selected timezone;
edition dates are timezone-free calendar dates.</footer>
<script>
(function () {{
  "use strict";
  var select = document.getElementById("tz-select");
  var stored = null;
  try {{ stored = window.localStorage.getItem("openpapers-tz"); }} catch (e) {{}}
  var zone = stored || "America/Chicago";
  function apply(zoneName) {{
    var nodes = document.querySelectorAll("[data-utc]");
    for (var i = 0; i < nodes.length; i += 1) {{
      var node = nodes[i];
      var parsed = new Date(node.getAttribute("data-utc"));
      if (isNaN(parsed)) {{ continue; }}
      try {{
        node.textContent = new Intl.DateTimeFormat("sv-SE", {{
          timeZone: zoneName, year: "numeric", month: "2-digit",
          day: "2-digit", hour: "2-digit", minute: "2-digit",
        }}).format(parsed);
      }} catch (e) {{ return; }}
    }}
  }}
  for (var j = 0; j < select.options.length; j += 1) {{
    if (select.options[j].value === zone) {{ select.selectedIndex = j; }}
  }}
  if (zone !== "America/Chicago") {{ apply(zone); }}
  select.addEventListener("change", function () {{
    try {{ window.localStorage.setItem("openpapers-tz", select.value); }}
    catch (e) {{}}
    apply(select.value);
  }});
}})();
</script>
</main></body></html>"""


def build_dashboard_document(
    state_path: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    editions_path: Path = DEFAULT_VENUE_EDITIONS,
) -> bytes:
    """Build the current page from catalog plus one immutable state read."""
    model = build_dashboard_model(
        load_venue_catalog(),
        read_agent_state_summary(Path(state_path)),
        observed_at=clock(),
        editions=load_venue_editions(editions_path),
    )
    return render_dashboard(model).encode("utf-8")


class _DashboardServer(HTTPServer):
    state_path: Path
    clock: Callable[[], datetime]
    editions_path: Path


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
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; base-uri 'none'; "
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
                editions_path=self.server.editions_path,
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
    editions_path: Path = DEFAULT_VENUE_EDITIONS,
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
    server.editions_path = Path(editions_path)
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--bind", default=_BIND)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--editions", default=DEFAULT_VENUE_EDITIONS, type=Path)
    # Retained so an installed launch configuration that passes it keeps
    # starting; the dataset-mtime column it fed was replaced by editions.
    parser.add_argument("--metadata-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        server = create_dashboard_server(
            args.state, bind=args.bind, port=args.port,
            editions_path=args.editions,
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
