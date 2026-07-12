# Automation and Monitoring

The automation layer is a control plane around the existing deterministic
scrapers. It detects source changes cheaply and emits structured events; it
does not publish datasets or execute LLM-generated code by itself.

## Registry and runtime state

`automation/conferences.json` is versioned configuration. Each conference-year
lists candidate sources and detector settings. Frequently changing state is
stored separately in `$SCRAPER_DATA_ROOT/monitor/state.sqlite3`.

```bash
python automation/monitor.py
python automation/monitor.py --venue ijcai --year 2026
python automation/monitor.py --no-write
```

Each JSON-line event reports source status, item count, content hash, whether
it changed since the previous observation, diagnostic detail, and the most
recent immutable snapshot path. Raw HTML/JSON is saved on first observation
and whenever the source changes, providing a reproducible fixture for repair.
Supported detectors are:

- `openreview_api`: hashes the sorted accepted-note IDs.
- `official_html`: hashes normalized text for a configured repeated item.
- `pmlr_volume`: detects a matching proceedings listing.

## Orchestration boundary

The monitor and scraper remain plain Python commands. Prefect can later wrap
them for schedules, retries, concurrency, logs, notifications, and downstream
deployments without moving parsing logic into Prefect tasks.

An agent repair workflow should consume only a change or validation-failure
event plus a saved source snapshot. Generated parser changes must include a
fixture and tests and pass review/CI before execution. Web content must be
treated as untrusted input; an extraction agent should not receive deployment
credentials or unrestricted code-execution authority.
