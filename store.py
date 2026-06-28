"""JSON-backed persistence for content records and the structured audit log.

Two flat JSON files in the repo root keep state across restarts:
  - content_store.json : one record per submitted piece of content
  - audit_log.json     : append-only list of structured audit entries

This is intentionally simple (no DB) for a prototype. All writes go through a
process-level lock so concurrent Flask requests don't corrupt the files.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
CONTENT_FILE = os.path.join(_DIR, "content_store.json")
AUDIT_FILE = os.path.join(_DIR, "audit_log.json")

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --- Content records -------------------------------------------------------

def next_content_id() -> str:
    with _lock:
        records = _load(CONTENT_FILE, {})
        return f"c{len(records) + 1:04d}"


def save_content(record: dict) -> None:
    with _lock:
        records = _load(CONTENT_FILE, {})
        records[record["content_id"]] = record
        _save(CONTENT_FILE, records)


def get_content(content_id: str) -> dict | None:
    with _lock:
        return _load(CONTENT_FILE, {}).get(content_id)


def update_content(content_id: str, **changes) -> dict | None:
    with _lock:
        records = _load(CONTENT_FILE, {})
        rec = records.get(content_id)
        if rec is None:
            return None
        rec.update(changes)
        records[content_id] = rec
        _save(CONTENT_FILE, records)
        return rec


def content_under_review() -> list[dict]:
    with _lock:
        records = _load(CONTENT_FILE, {})
    return [r for r in records.values() if r.get("status") == "under_review"]


# --- Audit log -------------------------------------------------------------

def append_audit(entry: dict) -> dict:
    """Append a timestamped structured entry to the audit log."""
    entry = {"timestamp": _now(), **entry}
    with _lock:
        log = _load(AUDIT_FILE, [])
        entry["seq"] = len(log) + 1
        log.append(entry)
        _save(AUDIT_FILE, log)
    return entry


def read_audit() -> list[dict]:
    with _lock:
        return _load(AUDIT_FILE, [])
