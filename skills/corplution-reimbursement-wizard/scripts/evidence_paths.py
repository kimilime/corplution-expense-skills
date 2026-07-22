"""Canonical path handling for user-supplied reimbursement evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def canonical_evidence_path(value: Any, process_dir: Path) -> str:
    """Resolve evidence paths against the workflow root (parent of process/)."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = process_dir.resolve().parent / path
    return str(path.resolve())


def normalize_approval_file(unit: dict[str, Any], process_dir: Path) -> str:
    normalized = canonical_evidence_path(unit.get("approval_file"), process_dir)
    unit["approval_file"] = normalized
    if normalized:
        unit["approval_file_status"] = "provided" if Path(normalized).is_file() else "missing"
    return normalized
