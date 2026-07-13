#!/usr/bin/env python3
"""Allocation generation lineage and rebase eligibility helpers.

Allocations are immutable generations for audit purposes even though the
canonical working filename remains ``expense-allocation.json``. Before an
official writer replaces that file, the current stamped generation is archived
under its integrity fingerprint and the new generation records a pointer to it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import integrity


# Increment this only when allocation/rebase semantics change. Policy-only
# changes are already guarded by source_policy_sha256.
ALLOCATION_ENGINE_REVISION = "expense-allocation-engine.v2"
FINAL_UNIT_STATUSES = {"confirmed", "fixed", "dropped", "excluded", "non_reimbursable"}
MAX_LINEAGE_DEPTH = 100
LINEAGE_INTEGRITY_PREFIX = "lineage integrity failure:"


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_stamped(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read allocation JSON: {exc}"
    if not isinstance(value, dict):
        return None, "allocation root is not an object"
    ok, reason = integrity.check(value)
    if not ok:
        return None, f"integrity failed: {reason}"
    return value, "ok"


def business_basis_error(old_alloc: dict[str, Any], new_alloc: dict[str, Any]) -> str | None:
    if canonical_sha(old_alloc.get("project_contexts", [])) != canonical_sha(
        new_alloc.get("project_contexts", [])
    ):
        return "effective project contexts changed"

    old_policy = str(old_alloc.get("source_policy_sha256", "")).strip().lower()
    new_policy = str(new_alloc.get("source_policy_sha256", "")).strip().lower()
    if not old_policy or not new_policy:
        return "one allocation lacks source_policy_sha256"
    if old_policy != new_policy:
        return "reimbursement policy changed"

    old_engine = str(old_alloc.get("allocation_engine_revision", "")).strip()
    new_engine = str(new_alloc.get("allocation_engine_revision", "")).strip()
    if not old_engine or not new_engine:
        return "one allocation lacks allocation_engine_revision"
    if old_engine != new_engine:
        return "allocation engine revision changed"
    return None


def generation_archive_path(process_dir: Path, fingerprint: str) -> Path:
    return process_dir / "allocation-generations" / f"expense-allocation.{fingerprint}.json"


def archive_current_generation(allocation_path: Path) -> tuple[Path, str] | None:
    """Archive a valid current allocation without overwriting prior history."""
    if not allocation_path.is_file():
        return None
    payload, reason = load_stamped(allocation_path)
    if payload is None:
        raise ValueError(
            f"cannot archive current allocation before replacement ({reason}). "
            "Regenerate it from trusted upstream data; do not preserve a tampered generation."
        )
    fingerprint = str(payload.get("integrity", {}).get("fingerprint", "")).strip().lower()
    if not fingerprint:
        raise ValueError("current allocation has no integrity fingerprint")
    archive = generation_archive_path(allocation_path.parent, fingerprint)
    archive.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = allocation_path.read_bytes()
    if archive.exists():
        if archive.read_bytes() != source_bytes:
            raise ValueError(
                f"generation archive collision at {archive}; the same fingerprint has different bytes"
            )
    else:
        temporary = archive.with_name(archive.name + ".tmp")
        temporary.write_bytes(source_bytes)
        temporary.replace(archive)
    return archive.resolve(), fingerprint


def record_previous_generation(payload: dict[str, Any], archived: tuple[Path, str] | None) -> None:
    if not archived:
        payload.pop("previous_allocation_file", None)
        payload.pop("previous_allocation_fingerprint", None)
        return
    path, fingerprint = archived
    payload["previous_allocation_file"] = str(path)
    payload["previous_allocation_fingerprint"] = fingerprint


def previous_generation_path(allocation_path: Path, allocation: dict[str, Any]) -> Path | None:
    recorded = str(allocation.get("previous_allocation_file", "")).strip()
    if recorded:
        path = Path(recorded).expanduser()
        return path if path.is_absolute() else allocation_path.parent / path

    # One-time compatibility for an early v2.6 preview. New writes never use it.
    legacy = allocation_path.with_suffix(allocation_path.suffix + ".bak")
    return legacy if legacy.is_file() else None


def has_explicit_user_decisions(allocation: dict[str, Any]) -> bool:
    if allocation.get("change_log"):
        return True
    return any(
        str(record.get("resolution_action", "")).strip()
        for record in allocation.get("expense_hint_reconciliation", [])
        if isinstance(record, dict)
    )


def discover_rebase_source(
    new_path: Path,
    new_alloc: dict[str, Any],
) -> tuple[Path | None, dict[str, Any] | None, str]:
    """Find the nearest same-basis ancestor containing official user decisions.

    Fresh allocator reruns have an empty change log. Skipping those ancestors
    preserves the last genuinely decided generation even after repeated reruns.
    """
    current_path = new_path
    current = new_alloc
    seen: set[Path] = set()
    saw_ancestor = False
    for _depth in range(1, MAX_LINEAGE_DEPTH + 1):
        previous = previous_generation_path(current_path, current)
        if previous is None:
            break
        path = previous.resolve()
        if path in seen:
            return None, None, f"{LINEAGE_INTEGRITY_PREFIX} cycle detected at {path}"
        seen.add(path)
        candidate, load_reason = load_stamped(path)
        if candidate is None:
            return None, None, f"{LINEAGE_INTEGRITY_PREFIX} {path} is unavailable or invalid ({load_reason})"
        saw_ancestor = True
        basis_error = business_basis_error(candidate, new_alloc)
        if basis_error:
            return None, None, basis_error
        if has_explicit_user_decisions(candidate):
            return path, candidate, "ok"
        current_path, current = path, candidate
    else:
        return None, None, f"{LINEAGE_INTEGRITY_PREFIX} exceeds {MAX_LINEAGE_DEPTH} generations"
    if saw_ancestor:
        return None, None, "generation lineage contains no explicit user decisions"
    return None, None, "no previous allocation generation is recorded"


def is_lineage_integrity_error(reason: str) -> bool:
    return str(reason).startswith(LINEAGE_INTEGRITY_PREFIX)
