#!/usr/bin/env python3
"""Corrections overlay for process/invoice-extraction.json.

Why this exists: the extraction script's keyword classifier has low recall on
photos and screenshots, so documents an agent (with vision) or the user can
identify — an invoice photo, a partner approval screenshot, an Alipay payment
receipt — often land as role "unknown". The sanctioned way to fix that is a
correction entry here, NOT hand-editing invoice-extraction.json: hand edits
are wiped whenever extract_invoices.py re-runs. Corrections live in their own
overlay file (process/extraction-corrections.json) keyed by durable evidence
selectors, and the extractor REPLAYS the overlay automatically after every
re-run, so a correction survives any number of re-extractions.

Every file is evidence until the user explicitly says to drop it. A drop is a
correction entry too ({"action": "exclude", "reason": ...}) so the exclusion
and its reason stay on the audit trail.
"""

from __future__ import annotations

import json
from datetime import datetime

import integrity
from pathlib import Path
from typing import Any

OVERLAY_FILENAME = "extraction-corrections.json"

ALLOWED_ROLES = {"invoice", "supporting_schedule", "supporting_document", "unknown"}
ALLOWED_SET_FIELDS = {
    "document_role",
    "document_subtype",
    "needs_review",
    "invoice",
    "classification",
    # Support-document packaging metadata. A supporting_document (payment
    # receipt, partner approval screenshot, or other user-provided evidence)
    # is packaged only when it names the invoice it backs.
    "support_type",            # free-text label shown in the package (付款小票/审批截图/…)
    "supports_document_id",    # document_id of the invoice/proof this evidence supports
}
ALLOWED_ACTIONS = {"correct", "exclude"}
ALLOWED_SOURCES = {"agent_vision", "agent_ocr", "user", "user_transcription"}
ALLOWED_INPUT_ACTIONS = {"exclude", "converted"}


def overlay_path(process_dir: Path) -> Path:
    return process_dir / OVERLAY_FILENAME


def load_overlay(process_dir: Path) -> dict[str, Any]:
    path = overlay_path(process_dir)
    if not path.exists():
        return {
            "schema_version": "extraction_corrections.v1",
            "corrections": [],
            "input_resolutions": [],
        }
    overlay = json.loads(path.read_text(encoding="utf-8-sig"))
    # Keep older, otherwise-valid overlays replayable while ensuring callers can
    # append the new resolution type without special casing them.
    overlay.setdefault("corrections", [])
    overlay.setdefault("input_resolutions", [])
    return overlay


def save_overlay(process_dir: Path, overlay: dict[str, Any]) -> Path:
    integrity.stamp(overlay, "apply_extraction_corrections.py")
    path = overlay_path(process_dir)
    path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def validate_correction(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    match = entry.get("match") or {}
    if not (match.get("sha256") or match.get("document_id") or match.get("source_file")):
        errors.append("correction needs a match key: sha256 (preferred), document_id, or source_file")
    action = entry.get("action", "correct")
    if action not in ALLOWED_ACTIONS:
        errors.append(f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {action!r}")
    if action == "exclude" and not str(entry.get("reason", "")).strip():
        errors.append("exclude requires a reason (the user's stated grounds for dropping this file)")
    if action == "correct":
        set_fields = entry.get("set") or {}
        if not set_fields:
            errors.append("correct requires a non-empty 'set' object")
        unknown = set(set_fields) - ALLOWED_SET_FIELDS
        if unknown:
            errors.append(f"unknown set fields {sorted(unknown)}; allowed: {sorted(ALLOWED_SET_FIELDS)}")
        role = set_fields.get("document_role")
        if role is not None and role not in ALLOWED_ROLES:
            errors.append(f"document_role must be one of {sorted(ALLOWED_ROLES)}, got {role!r}")
        for key in ("support_type", "supports_document_id"):
            value = set_fields.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"{key} must be a string, got {type(value).__name__}")
        # Whether supports_document_id resolves to a real invoice is validated at
        # Stage 3 (write), where the final proof groups exist; an unresolved link
        # surfaces there as a hard block rather than a correction-time error.
    source = entry.get("corrected_by", "user")
    if source not in ALLOWED_SOURCES:
        errors.append(f"corrected_by must be one of {sorted(ALLOWED_SOURCES)}, got {source!r}")
    return errors


def validate_input_resolution(entry: dict[str, Any]) -> list[str]:
    """Validate a decision for a file the extractor cannot process.

    Unsupported inputs are evidence until the user explicitly excludes them or
    supplies a converted replacement.  Hash matching is preferred because a
    filename can occur more than once in a folder tree.
    """
    errors: list[str] = []
    match = entry.get("match") or {}
    if not (match.get("sha256") or match.get("source_file")):
        errors.append("input resolution needs a match key: sha256 (preferred) or source_file")
    action = entry.get("action")
    if action not in ALLOWED_INPUT_ACTIONS:
        errors.append(f"input resolution action must be one of {sorted(ALLOWED_INPUT_ACTIONS)}, got {action!r}")
    if not str(entry.get("reason", "")).strip():
        errors.append("input resolution requires a reason recording the user's decision")
    if action == "converted" and not str(entry.get("replacement_file", "")).strip():
        errors.append("converted input resolution requires replacement_file (the readable replacement supplied)")
    source = entry.get("corrected_by", "user")
    if source not in ALLOWED_SOURCES:
        errors.append(f"corrected_by must be one of {sorted(ALLOWED_SOURCES)}, got {source!r}")
    return errors


def _source_file_matches(recorded: Any, requested: Any) -> bool:
    recorded_path = Path(str(recorded or ""))
    requested_path = Path(str(requested or ""))
    if requested_path.is_absolute():
        return recorded_path.resolve(strict=False) == requested_path.resolve(strict=False)
    return recorded_path.name == requested_path.name


def _match_doc(entry: dict[str, Any], doc: dict[str, Any]) -> bool:
    match = entry.get("match") or {}
    checks: list[bool] = []
    if match.get("sha256"):
        checks.append(doc.get("sha256") == match["sha256"])
    if match.get("document_id"):
        checks.append(doc.get("document_id") == match["document_id"])
    if match.get("source_file"):
        checks.append(_source_file_matches(doc.get("source_file"), match["source_file"]))
    return bool(checks) and all(checks)


def _match_input(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    match = entry.get("match") or {}
    checks: list[bool] = []
    if match.get("sha256"):
        checks.append(item.get("sha256") == match["sha256"])
    if match.get("source_file"):
        checks.append(_source_file_matches(item.get("source_file"), match["source_file"]))
    return bool(checks) and all(checks)


def apply_overlay(payload: dict[str, Any], overlay: dict[str, Any]) -> list[str]:
    """Apply corrections to an extraction payload in place.

    Returns human-readable log lines (also used by the extractor after
    re-runs to show which corrections were replayed).
    """
    log: list[str] = []
    for entry in overlay.get("corrections", []):
        matched = [doc for doc in payload.get("documents", []) if _match_doc(entry, doc)]
        if not matched:
            log.append(f"WARNING: correction {entry.get('match')} matched no document (file removed or renamed?)")
            continue
        match = entry.get("match") or {}
        if len(matched) > 1:
            log.append(
                f"ERROR: correction selector {match!r} matched {len(matched)} documents; "
                "nothing was applied. SHA-256 identifies byte content, not one physical copy. "
                "For exact duplicates, combine the shared sha256 with the intended copy's exact "
                "source_file (preferred), or use a unique current document_id."
            )
            continue
        for doc in matched:
            if entry.get("action", "correct") == "exclude":
                doc["excluded_by_user"] = True
                doc["exclusion_reason"] = str(entry.get("reason", "")).strip()
                doc["needs_review"] = False
                log.append(f"{doc.get('document_id')}: excluded ({doc['exclusion_reason']})")
                continue
            set_fields = entry.get("set") or {}
            for key, value in set_fields.items():
                if key in {"invoice", "classification"} and isinstance(value, dict):
                    target = doc.get(key) or {}
                    target.update(value)
                    doc[key] = target
                else:
                    doc[key] = value
            doc["corrected_by"] = entry.get("corrected_by", "user")
            doc["corrected_at"] = entry.get("corrected_at", datetime.now().replace(microsecond=0).isoformat())
            if entry.get("reason"):
                doc["correction_note"] = str(entry["reason"]).strip()
            if "needs_review" not in set_fields and set_fields.get("document_role") in ALLOWED_ROLES - {"unknown"}:
                doc["needs_review"] = False
            if doc.get("needs_review"):
                # Filling missing fields (e.g. invoice number/date/seller) does NOT
                # auto-clear needs_review unless the correction also (re)sets a known
                # document_role. Tell the caller how to clear it explicitly.
                log.append(
                    f"{doc.get('document_id')}: still flagged needs_review after this correction. "
                    "If the required fields are now complete, add \"needs_review\": false to this "
                    "correction's set to clear it."
                )
            log.append(f"{doc.get('document_id')}: corrected -> role={doc.get('document_role')} ({doc.get('corrected_by')})")
    return log


def apply_input_resolutions(payload: dict[str, Any], overlay: dict[str, Any]) -> list[str]:
    """Apply durable user decisions for unsupported input files in place."""
    log: list[str] = []
    for entry in overlay.get("input_resolutions", []):
        matched = [item for item in payload.get("unresolved_input_files", []) if _match_input(entry, item)]
        if not matched:
            log.append(f"WARNING: input resolution {entry.get('match')} matched no unsupported input")
            continue
        match = entry.get("match") or {}
        if len(matched) > 1:
            log.append(
                f"ERROR: input resolution selector {match!r} matched {len(matched)} files; "
                "nothing was applied. For byte-identical inputs, combine the shared sha256 "
                "with the intended copy's exact source_file."
            )
            continue
        for item in matched:
            action = entry["action"]
            item["status"] = action
            item["resolution"] = str(entry["reason"]).strip()
            item["resolved_by"] = entry.get("corrected_by", "user")
            item["resolved_at"] = entry.get("corrected_at", datetime.now().replace(microsecond=0).isoformat())
            if action == "converted":
                item["replacement_file"] = str(entry["replacement_file"]).strip()
            log.append(f"unsupported input {item.get('filename')}: {action} ({item['resolution']})")
    return log
