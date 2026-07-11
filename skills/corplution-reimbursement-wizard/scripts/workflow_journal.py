#!/usr/bin/env python3
"""Append-only, privacy-minimized workflow journal for orchestrated runs.

The journal is observational, never authoritative. Integrity decisions remain
with the existing process stamps, workbook hash, and package manifest. A
journal write failure must not change a business script's exit code.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "reimbursement_workflow_journal_event.v1"
EVENTS = {"started", "completed", "failed", "blocked"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def process_artifact(path: Path, count_fields: dict[str, str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    record: dict[str, Any] = {
        "file_sha256": sha256_file(path),
    }
    payload = load_json(path)
    if payload is None:
        record["json_readable"] = False
        return record
    record.update({
        "json_readable": True,
        "schema_version": str(payload.get("schema_version", "")),
        "integrity_fingerprint": str((payload.get("integrity") or {}).get("fingerprint", "")),
    })
    for source_key, output_key in count_fields.items():
        value = payload.get(source_key, [])
        record[output_key] = len(value) if isinstance(value, list) else 0
    return record


def snapshot_artifacts(process_dir: str | Path, output_root: str | Path) -> dict[str, Any]:
    """Return hashes and counts only; omit business text, filenames, and paths."""
    pdir = Path(process_dir)
    root = Path(output_root)
    snapshot: dict[str, Any] = {}

    specs = [
        ("extraction", pdir / "invoice-extraction.json", {
            "documents": "document_count",
            "unresolved_input_files": "unresolved_input_count",
        }),
        ("allocation", pdir / "expense-allocation.json", {
            "allocation_units": "unit_count",
            "questions": "question_count",
        }),
        ("final_rows", pdir / "final-expense-rows.json", {
            "rows": "row_count",
        }),
    ]
    for name, path, count_fields in specs:
        record = process_artifact(path, count_fields)
        if record is not None:
            snapshot[name] = record

    final_rows = load_json(pdir / "final-expense-rows.json")
    if final_rows:
        recorded_sha = str(final_rows.get("workbook_sha256", ""))
        workbook_value = str(final_rows.get("workbook", ""))
        workbook = Path(workbook_value).expanduser() if workbook_value else None
        workbook_record: dict[str, Any] = {"recorded_sha256": recorded_sha}
        if workbook and workbook.is_file():
            workbook_record["actual_sha256"] = sha256_file(workbook)
        snapshot["workbook"] = workbook_record

    manifests = sorted(
        root.glob("**/package-manifest.json"),
        key=lambda item: item.stat().st_mtime_ns,
    ) if root.exists() else []
    if manifests:
        latest = manifests[-1]
        manifest_record = process_artifact(latest, {
            "invoice_files": "invoice_count",
            "support_files": "support_count",
            "issues": "issue_count",
        }) or {}
        manifest_record["manifest_count"] = len(manifests)
        snapshot["package_manifest"] = manifest_record

    return snapshot


def workflow_id(process_dir: str | Path) -> str:
    normalized = str(Path(process_dir).resolve()).casefold().encode("utf-8")
    return "wf-" + hashlib.sha256(normalized).hexdigest()[:16]


def build_event(
    *,
    process_dir: str | Path,
    run_id: str,
    stage: str,
    script: str,
    event: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    input_artifacts: dict[str, Any] | None = None,
    output_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if event not in EVENTS:
        raise ValueError(f"unsupported journal event: {event}")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid4()),
        "workflow_id": workflow_id(process_dir),
        "run_id": run_id,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stage": stage,
        "script": Path(script).name,
        "event": event,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "input_artifacts": input_artifacts or {},
        "output_artifacts": output_artifacts or {},
    }


def append_event(path: str | Path, event: dict[str, Any]) -> None:
    journal_path = Path(path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with journal_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def record_event(path: str | Path, **kwargs: Any) -> str | None:
    """Best-effort journal write; return a warning instead of raising."""
    try:
        append_event(path, build_event(**kwargs))
    except Exception as exc:  # Logging must never override a stage result.
        return f"workflow journal write failed: {exc}"
    return None

