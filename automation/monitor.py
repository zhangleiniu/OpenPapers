"""Cheap, deterministic source monitoring driven by ``conferences.json``."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DATA_ROOT  # noqa: E402
from scrapers.openreview import OpenReviewClient  # noqa: E402
from utils import RobustSession  # noqa: E402


DEFAULT_REGISTRY = Path(__file__).with_name("conferences.json")
DEFAULT_STATE = DATA_ROOT / "monitor" / "state.sqlite3"


def _digest(values):
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_registry(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("unsupported registry version")
    entries = payload.get("conference_years")
    if not isinstance(entries, list):
        raise ValueError("conference_years must be a list")
    seen = set()
    for entry in entries:
        key = (entry.get("venue"), entry.get("year"))
        if not key[0] or not isinstance(key[1], int) or key in seen:
            raise ValueError(f"invalid or duplicate conference-year: {key}")
        if not entry.get("sources"):
            raise ValueError(f"no sources configured for {key}")
        seen.add(key)
    return entries


class StateStore:
    """SQLite-backed runtime state, deliberately separate from the registry."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS source_state (
                venue TEXT NOT NULL,
                year INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                detail TEXT NOT NULL,
                snapshot_path TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (venue, year, source_key)
            )
        """)
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(source_state)")
        }
        if "snapshot_path" not in columns:
            self.connection.execute(
                "ALTER TABLE source_state ADD COLUMN snapshot_path TEXT NOT NULL DEFAULT ''")

    def get(self, venue, year, source_key):
        row = self.connection.execute(
            "SELECT status, content_hash, item_count, detail, snapshot_path "
            "FROM source_state "
            "WHERE venue=? AND year=? AND source_key=?",
            (venue, year, source_key),
        ).fetchone()
        if not row:
            return None
        return dict(zip(
            ("status", "content_hash", "item_count", "detail", "snapshot_path"),
            row))

    def put(self, event):
        self.connection.execute("""
            INSERT INTO source_state
                (venue, year, source_key, checked_at, status, content_hash,
                item_count, detail, snapshot_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, year, source_key) DO UPDATE SET
                checked_at=excluded.checked_at,
                status=excluded.status,
                content_hash=excluded.content_hash,
                item_count=excluded.item_count,
                detail=excluded.detail,
                snapshot_path=excluded.snapshot_path
        """, (
            event["venue"], event["year"], event["source_key"],
            event["checked_at"], event["status"], event["content_hash"],
            event["item_count"], event.get("detail", ""),
            event.get("snapshot_path", ""),
        ))
        self.connection.commit()


class Monitor:
    def __init__(self, session=None):
        self.session = session or RobustSession(delay=0.2)
        self.openreview = OpenReviewClient(self.session)
        self.snapshot = b""
        self.snapshot_suffix = ".json"

    def check(self, venue, year, source):
        self.snapshot = b""
        self.snapshot_suffix = ".json"
        source_type = source["type"]
        if source_type == "openreview_api":
            return self._openreview(source)
        if source_type == "official_html":
            return self._official_html(source)
        if source_type == "pmlr_volume":
            return self._pmlr_volume(source)
        raise ValueError(f"unknown detector type: {source_type}")

    def _openreview(self, source):
        notes = self.openreview.get_notes(
            source["invitation"], source["venue_id"])
        self.snapshot = json.dumps(
            notes, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.snapshot_suffix = ".json"
        ids = sorted(note.get("id", "") for note in notes)
        minimum = int(source.get("minimum_count", 1))
        status = "available" if len(ids) >= minimum else "unavailable"
        detail = "" if status == "available" else f"expected at least {minimum} notes"
        return status, len(ids), _digest(ids), detail

    def _official_html(self, source):
        response = self.session.get(
            source["url"], quiet_404=source.get("optional_until_available", False))
        if response is None:
            if source.get("optional_until_available"):
                return "unavailable", 0, _digest([]), "source not published yet"
            raise RuntimeError(f"request failed: {source['url']}")
        soup = BeautifulSoup(response.content, "html.parser")
        self.snapshot = response.content
        self.snapshot_suffix = ".html"
        texts = [
            re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            for node in soup.select(source["item_selector"])
        ]
        minimum = int(source.get("minimum_count", 1))
        status = "available" if len(texts) >= minimum else "unavailable"
        detail = "" if status == "available" else f"expected at least {minimum} items"
        return status, len(texts), _digest(texts), detail

    def _pmlr_volume(self, source):
        response = self.session.get(source["url"])
        if response is None:
            raise RuntimeError(f"request failed: {source['url']}")
        soup = BeautifulSoup(response.content, "html.parser")
        self.snapshot = response.content
        self.snapshot_suffix = ".html"
        pattern = re.compile(re.escape(source["label"]), re.IGNORECASE)
        matches = []
        for link in soup.find_all("a", href=True):
            parent_text = link.parent.get_text(" ", strip=True) if link.parent else ""
            if pattern.search(parent_text):
                matches.append({"text": parent_text, "href": link["href"]})
        return ("available" if matches else "unavailable", len(matches),
                _digest(matches), "")


def source_key(source):
    identity = source.get("url") or source.get("invitation") or ""
    return f"{source['type']}:{identity}"


def save_snapshot(state_path: Path, event, source_type, content, suffix):
    """Persist an immutable first/changed source response for agent diagnosis."""
    if not content or not event.get("content_hash"):
        return ""
    directory = (state_path.parent / "snapshots" / event["venue"] /
                 str(event["year"]))
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_hash = hashlib.sha256(content).hexdigest()
    path = directory / f"{source_type}-{snapshot_hash}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return str(path)


def run(registry_path=DEFAULT_REGISTRY, state_path=DEFAULT_STATE,
        venue=None, year=None, write_state=True):
    entries = load_registry(Path(registry_path))
    state_path = Path(state_path)
    store = StateStore(state_path) if write_state else None
    monitor = Monitor()
    events = []
    for entry in entries:
        if venue and entry["venue"] != venue:
            continue
        if year and entry["year"] != year:
            continue
        for source in entry["sources"]:
            key = source_key(source)
            previous = store.get(entry["venue"], entry["year"], key) if store else None
            event = {
                "venue": entry["venue"], "year": entry["year"],
                "source_key": key,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                status, count, content_hash, detail = monitor.check(
                    entry["venue"], entry["year"], source)
                event.update(status=status, item_count=count,
                             content_hash=content_hash, detail=detail)
            except Exception as exc:
                event.update(status="error", item_count=0, content_hash="",
                             detail=str(exc))
            event["changed"] = bool(
                previous and (
                    previous["status"] != event["status"] or
                    previous["content_hash"] != event["content_hash"] or
                    previous["item_count"] != event["item_count"]
                ))
            event["first_observation"] = previous is None
            event["snapshot_path"] = (
                save_snapshot(
                    state_path, event, source["type"], monitor.snapshot,
                    monitor.snapshot_suffix)
                if store and (
                    event["first_observation"] or event["changed"] or
                    not (previous or {}).get("snapshot_path"))
                else (previous.get("snapshot_path", "") if previous else "")
            )
            if store:
                store.put(event)
            events.append(event)
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--venue")
    parser.add_argument("--year", type=int)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        events = run(args.registry, args.state, args.venue, args.year,
                     write_state=not args.no_write)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for event in events:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))
    return 1 if any(event["status"] == "error" for event in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
