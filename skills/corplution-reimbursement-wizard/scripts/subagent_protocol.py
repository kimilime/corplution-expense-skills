#!/usr/bin/env python3
"""Version-bound, read-only handoff protocol for preferred subagent checkpoints.

This module never launches an LLM and never mutates extraction/allocation JSON.
It serializes a path-free snapshot for a fresh subagent, validates the returned
JSON, and stamps only derived analysis/review artifacts. The coordinator still
uses Composer + Updater for every accepted allocation change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib

import integrity
import text_safety
import allocation_generations
from apply_allocation_answers import ALLOWED_UNIT_FIELDS, validate_update


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "assets" / "policy.toml"

TASK_SCHEMA = "subagent_task.v1"
ANALYSIS_SCHEMA = "allocation_analysis.v1"
REVIEW_SCHEMA = "stage3_independent_review.v1"
PROPOSAL_SCHEMA = "allocation_proposals.v1"
ANALYSIS_CONTRACT = "allocation-analysis.v1"
REVIEW_CONTRACT = "stage3-preflight-review.v1"

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "allocation_analyst": {
        "codename": "otako",
        "display_name": "Otako - Allocation Analyst",
        "role_title": "Otako, the Allocation Analyst",
        "reference": SKILL_DIR / "references" / "otako-allocation-analyst.md",
        "contract_version": ANALYSIS_CONTRACT,
        "coverage": [
            "project_identity",
            "journey_timeline",
            "transport_transfers",
            "local_project_guard",
            "meal_and_hint_matching",
            "hotel_and_other",
            "unresolved_items",
        ],
    },
    "independent_reviewer": {
        "codename": "kaede",
        "display_name": "Kaede - Independent Reviewer",
        "role_title": "Kaede, the Independent Reviewer",
        "reference": SKILL_DIR / "references" / "kaede-independent-reviewer.md",
        "contract_version": REVIEW_CONTRACT,
        "coverage": [
            "material_completeness",
            "project_allocation",
            "form_over_substance",
            "final_notes",
            "policy_prerequisites",
            "open_gates",
            "accounting_readiness",
        ],
    },
}

RAW_TEXT_KEYS = {
    "raw_text",
    "ocr_text",
    "extracted_text",
    "page_text",
    "pdf_text",
    "text_layer",
}
DROP_KEYS = {
    "integrity",
    "change_log",
    "previous_allocation_file",
    "source_project_context_file",
}
PATH_KEYS = {
    "source_file",
    "source_files",
    "file",
    "files",
    "path",
    "replacement_path",
    "approval_screenshot_path",
    "supporting_document_path",
    "invoice_path",
}
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
EMBEDDED_ABSOLUTE_PATHS = (
    re.compile(r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/](?:[^\\/\r\n\"'<>|]+[\\/])*[^\r\n\"'<>|,;]*)"),
    re.compile(r"(?<![\\/])(?:\\\\[^\\/\s\r\n\"'<>|]+[\\/][^\\/\s\r\n\"'<>|]+(?:[\\/][^\r\n\"'<>|,;]*)?)"),
    re.compile(r"(?<![A-Za-z0-9_.])(?:/(?:[^/\s\r\n\"'<>|]+/)+[^\r\n\"'<>|,;]*)"),
)


class ProtocolError(ValueError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(encoded.encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    """Create a generation artifact once; never replace different bytes in place."""
    data = _json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ProtocolError(f"immutable accepted-result archive already exists with different bytes: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _basename(value: Any) -> Any:
    if isinstance(value, list):
        return [_basename(item) for item in value]
    if not isinstance(value, str):
        return value
    return Path(value.replace("\\", "/")).name


def _scrub_embedded_paths(value: str) -> str:
    scrubbed = value
    for pattern in EMBEDDED_ABSOLUTE_PATHS:
        scrubbed = pattern.sub("[local-path]", scrubbed)
    return scrubbed


def sanitize_snapshot(value: Any, key: str = "") -> Any:
    """Remove mutable local paths, integrity metadata, and bulky OCR bodies."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for child_key, child in value.items():
            normalized_key = str(child_key)
            if normalized_key in DROP_KEYS or normalized_key in RAW_TEXT_KEYS:
                continue
            if normalized_key in PATH_KEYS or normalized_key.endswith("_path"):
                output[normalized_key] = _basename(child)
            else:
                output[normalized_key] = sanitize_snapshot(child, normalized_key)
        return output
    if isinstance(value, list):
        return [sanitize_snapshot(item, key) for item in value]
    if isinstance(value, str):
        if key.endswith("_file") or WINDOWS_ABSOLUTE.match(value):
            return _basename(value)
        scrubbed = _scrub_embedded_paths(value)
        return scrubbed if len(scrubbed) <= 4000 else scrubbed[:4000] + " [truncated]"
    return value


def _full_fingerprint(payload: dict[str, Any], label: str) -> str:
    ok, reason = integrity.check(payload)
    if not ok:
        raise ProtocolError(f"{label} integrity failed: {reason}")
    fingerprint = clean((payload.get("integrity") or {}).get("fingerprint")).lower()
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise ProtocolError(f"{label} does not have a full SHA-256 integrity fingerprint")
    return fingerprint


def role_spec(role: str) -> dict[str, Any]:
    try:
        return ROLE_SPECS[role]
    except KeyError as exc:
        raise ProtocolError(f"unknown role {role!r}; choose {', '.join(ROLE_SPECS)}") from exc


def _require_canonical_process_dir(process_dir: Path, allocation_path: Path) -> None:
    if process_dir.resolve() != allocation_path.parent.resolve():
        raise ProtocolError(
            "process_dir must be the canonical directory containing allocation; "
            "cross-batch subagent artifacts are forbidden"
        )


def _policy_snapshot(expected_sha: str) -> tuple[dict[str, Any], str]:
    if not POLICY_PATH.is_file():
        raise ProtocolError(f"policy file is missing: {POLICY_PATH}")
    actual_sha = sha256_file(POLICY_PATH)
    if expected_sha and actual_sha != expected_sha:
        raise ProtocolError(
            "policy.toml changed after allocation was generated; rerun Stage 2 before delegating analysis"
        )
    with POLICY_PATH.open("rb") as handle:
        policy = tomllib.load(handle)
    return sanitize_snapshot(policy), actual_sha


def _validate_project_context(allocation: dict[str, Any], allocation_path: Path) -> None:
    expected_sha = clean(allocation.get("source_project_context_sha256"))
    if not expected_sha:
        return
    recorded = clean(allocation.get("source_project_context_file"))
    if not recorded:
        raise ProtocolError("allocation has a project-context hash but no recorded context file")
    path = Path(recorded).expanduser()
    if not path.is_absolute():
        path = allocation_path.parent.parent / path
    if not path.is_file():
        raise ProtocolError("the project context used by allocation is missing; restore it before delegation")
    if sha256_file(path) != expected_sha:
        raise ProtocolError("project context changed after allocation; rerun Stage 2 before delegation")


def _unit_tokens(allocation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tokens: dict[str, dict[str, Any]] = {}
    for unit in allocation.get("allocation_units", []):
        if not isinstance(unit, dict):
            raise ProtocolError("allocation contains a malformed unit")
        number = clean(unit.get("user_no") or unit.get("unit_no"))
        ref = clean(unit.get("unit_ref")).lower()
        if not number.isdigit() or not ref:
            raise ProtocolError("allocation unit is missing its current display number or @ref")
        token = f"{int(number)}@{ref}"
        if token in tokens:
            raise ProtocolError(f"allocation repeats current unit token {token}")
        tokens[token] = unit
    return tokens


def _review_readiness_errors(allocation: dict[str, Any], allocation_path: Path) -> list[str]:
    errors: list[str] = []
    open_questions = [
        item for item in allocation.get("questions", [])
        if isinstance(item, dict) and clean(item.get("status") or "open") == "open"
    ]
    if open_questions:
        errors.append(f"{len(open_questions)} open allocation question(s)")
    unfinished = [
        clean(item.get("unit_id") or item.get("user_no") or "?")
        for item in allocation.get("allocation_units", [])
        if isinstance(item, dict)
        and clean(item.get("status"))
        not in {"confirmed", "fixed", "dropped", "excluded", "non_reimbursable"}
    ]
    if unfinished:
        errors.append(f"{len(unfinished)} unfinished allocation unit(s): {', '.join(unfinished[:5])}")
    unresolved_hints = [
        item for item in allocation.get("expense_hint_reconciliation", [])
        if isinstance(item, dict)
        and clean(item.get("resolution_status")) not in {"not_required", "resolved"}
    ]
    if unresolved_hints:
        errors.append(f"{len(unresolved_hints)} unresolved applicant expense record(s)")
    contexts_have_hints = any(
        context.get(field)
        for context in allocation.get("project_contexts", [])
        if isinstance(context, dict)
        for field in ("meal_hints", "expense_hints")
    )
    if contexts_have_hints and "expense_hint_reconciliation" not in allocation:
        errors.append("applicant expense records exist but the reconciliation ledger is missing")

    allocation_fp = clean((allocation.get("integrity") or {}).get("fingerprint"))
    answers_path = allocation_path.parent / "allocation-answers.json"
    if answers_path.is_file():
        try:
            answers = load_json(answers_path)
        except ProtocolError:
            answers = {}
        action_fields = (
            "unit_updates", "expense_hint_resolutions", "question_updates", "project_contexts",
            "confirm_units", "drop_units", "exclude_units",
        )
        action_count = sum(
            len(answers.get(field, [])) if isinstance(answers.get(field), list) else 0
            for field in action_fields
        )
        action_count += bool(isinstance(answers.get("lineage_rebase"), dict) and answers.get("lineage_rebase"))
        if (
            action_count
            and answers.get("schema_version") == "allocation_answers.v1"
            and clean(answers.get("source_allocation_fingerprint")) == allocation_fp
        ):
            errors.append(f"{action_count} current Composer/Updater action(s) remain unapplied")

    rebase_path = allocation_path.parent / "rebase-decisions.json"
    if rebase_path.is_file():
        try:
            rebase = load_json(rebase_path)
        except ProtocolError:
            rebase = {}
        metadata = rebase.get("rebase_metadata") if isinstance(rebase.get("rebase_metadata"), dict) else {}
        rebase_ok, _reason = integrity.check(rebase) if rebase else (False, "missing")
        declared = clean(rebase.get("for_allocation_fingerprint")).lower()
        if (
            rebase_ok
            and clean((rebase.get("integrity") or {}).get("stamped_by")) == "rebase_allocation_decisions.py"
            and declared
            and allocation_fp.lower().startswith(declared)
            and clean(metadata.get("target_allocation_fingerprint")).lower() == allocation_fp.lower()
        ):
            errors.append("a current lineage-rebase decision still needs Composer/Updater application")

    if not allocation.get("change_log"):
        source_path, _source, lineage_reason = allocation_generations.discover_rebase_source(
            allocation_path,
            allocation,
        )
        if source_path is not None:
            errors.append("a transferable prior allocation generation still requires lineage rebase")
        elif allocation_generations.is_lineage_integrity_error(lineage_reason):
            errors.append(lineage_reason)
    return errors


def _build_task(
    role: str,
    allocation_path: Path,
    extraction_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = role_spec(role)
    allocation = load_json(allocation_path)
    extraction = load_json(extraction_path)
    allocation_fp = _full_fingerprint(allocation, "allocation")
    extraction_fp = _full_fingerprint(extraction, "extraction")
    if clean(allocation.get("source_extraction_fingerprint")) != extraction_fp:
        raise ProtocolError("allocation belongs to a different extraction generation; rerun Stage 2")
    _validate_project_context(allocation, allocation_path)
    if role == "independent_reviewer":
        readiness_errors = _review_readiness_errors(allocation, allocation_path)
        if readiness_errors:
            raise ProtocolError(
                "independent review is premature because Stage 2 is not ready: "
                + "; ".join(readiness_errors)
            )

    reference_path = Path(spec["reference"])
    role_instructions = reference_path.read_text(encoding="utf-8-sig")
    role_sha = sha256_bytes(role_instructions.encode("utf-8"))
    policy, policy_sha = _policy_snapshot(clean(allocation.get("source_policy_sha256")))
    basis = {
        "role_id": role,
        "contract_version": spec["contract_version"],
        "source_allocation_fingerprint": allocation_fp,
        "source_extraction_fingerprint": extraction_fp,
        "source_project_context_sha256": clean(allocation.get("source_project_context_sha256")),
        "source_policy_sha256": policy_sha,
        "allocation_engine_revision": clean(allocation.get("allocation_engine_revision")),
        "role_instructions_sha256": role_sha,
    }
    task_id = canonical_sha(basis)
    task = {
        "schema_version": TASK_SCHEMA,
        "task_id": task_id,
        "role_id": role,
        "codename": spec["codename"],
        "display_name": spec["display_name"],
        "role_title": spec["role_title"],
        "contract_version": spec["contract_version"],
        "source_generation": basis,
        "role_instructions": _scrub_embedded_paths(role_instructions),
        "required_coverage": list(spec["coverage"]),
        "allowed_unit_update_fields": sorted(ALLOWED_UNIT_FIELDS),
        "policy": policy,
        "project_contexts": sanitize_snapshot(allocation.get("project_contexts", [])),
        "allocation_units": sanitize_snapshot(allocation.get("allocation_units", [])),
        "allocation_questions": sanitize_snapshot(allocation.get("questions", [])),
        "expense_hint_reconciliation": sanitize_snapshot(
            allocation.get("expense_hint_reconciliation", [])
        ),
        "evidence_index": sanitize_snapshot(extraction.get("documents", [])),
        "unresolved_input_files": sanitize_snapshot(extraction.get("unresolved_input_files", [])),
        "handoff_rules": [
            "Use only this packet; do not access files, tools, scripts, or prior-agent reasoning.",
            "Return exactly one JSON object and no Markdown.",
            "Do not mutate reimbursement artifacts or claim that a proposal was applied.",
            "Bind the result to the exact task and allocation fingerprints in the result template.",
        ],
    }
    integrity.stamp(task, "subagent_protocol.py")
    return task, allocation


def result_template(task: dict[str, Any]) -> dict[str, Any]:
    task_fp = clean((task.get("integrity") or {}).get("fingerprint"))
    allocation_fp = clean((task.get("source_generation") or {}).get("source_allocation_fingerprint"))
    extraction_fp = clean((task.get("source_generation") or {}).get("source_extraction_fingerprint"))
    coverage = [
        {"check_id": check_id, "status": "pending", "notes": ""}
        for check_id in task.get("required_coverage", [])
    ]
    common = {
        "task_id": task.get("task_id"),
        "source_task_fingerprint": task_fp,
        "source_allocation_fingerprint": allocation_fp,
        "source_extraction_fingerprint": extraction_fp,
        "agent_id": task.get("role_id"),
        "agent_display_name": task.get("display_name"),
        "coverage": coverage,
        "summary": "",
    }
    if task.get("role_id") == "allocation_analyst":
        return {
            "schema_version": ANALYSIS_SCHEMA,
            **common,
            "proposals": [],
            "user_questions": [],
            "warnings": [],
        }
    return {
        "schema_version": REVIEW_SCHEMA,
        "review_contract_version": REVIEW_CONTRACT,
        **common,
        "outcome": "pass",
        "findings": [],
    }


def task_paths(process_dir: Path, task: dict[str, Any]) -> dict[str, Path]:
    role = clean(task.get("role_id"))
    task_id = clean(task.get("task_id"))
    analysis_root = process_dir.parent / "analysis" / "subagent-pilot"
    return {
        "task": analysis_root / "tasks" / f"{role}-{task_id}.json",
        "template": analysis_root / "tasks" / f"{role}-{task_id}.result-template.json",
        "analyst_result": analysis_root / "results" / f"otako-allocation-analysis-{task_id}.json",
        "proposal": analysis_root / "proposals" / f"otako-allocation-proposals-{task_id}.unreviewed.json",
        "promoted_proposal": analysis_root / "proposals" / f"otako-allocation-proposals-{task_id}.reviewed.json",
        "review_result": process_dir / "stage3-independent-review.json",
        "review_history": analysis_root / "review-history",
        "review_archive_dir": process_dir / "subagent-review-generations" / task_id,
    }


def prepare_task(
    role: str,
    allocation_path: Path,
    extraction_path: Path,
    process_dir: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _require_canonical_process_dir(process_dir, allocation_path)
    task, _allocation = _build_task(role, allocation_path, extraction_path)
    paths = task_paths(process_dir, task)
    write_json(paths["task"], task)
    write_json(paths["template"], result_template(task))
    return task, paths


def _validate_root(candidate: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(candidate) - allowed)
    if unknown:
        raise ProtocolError(f"result contains unknown root fields: {', '.join(unknown)}")


def _validate_binding(candidate: dict[str, Any], task: dict[str, Any]) -> None:
    expected = {
        "task_id": clean(task.get("task_id")),
        "source_task_fingerprint": clean((task.get("integrity") or {}).get("fingerprint")),
        "source_allocation_fingerprint": clean(
            (task.get("source_generation") or {}).get("source_allocation_fingerprint")
        ),
        "source_extraction_fingerprint": clean(
            (task.get("source_generation") or {}).get("source_extraction_fingerprint")
        ),
        "agent_id": clean(task.get("role_id")),
        "agent_display_name": clean(task.get("display_name")),
    }
    for field, value in expected.items():
        if clean(candidate.get(field)) != value:
            raise ProtocolError(f"result {field} does not exactly match the current task")


def _validate_coverage(candidate: dict[str, Any], task: dict[str, Any]) -> None:
    coverage = candidate.get("coverage")
    if not isinstance(coverage, list):
        raise ProtocolError("result coverage must be a list")
    expected = list(task.get("required_coverage", []))
    seen: dict[str, str] = {}
    for item in coverage:
        if not isinstance(item, dict) or set(item) - {"check_id", "status", "notes"}:
            raise ProtocolError("each coverage entry may contain only check_id, status, and notes")
        if any(not isinstance(item.get(field, ""), str) for field in ("check_id", "status", "notes")):
            raise ProtocolError("coverage check_id, status, and notes must be strings")
        check_id = clean(item.get("check_id"))
        status = clean(item.get("status"))
        if check_id in seen:
            raise ProtocolError(f"coverage repeats check {check_id}")
        if status not in {"completed", "not_applicable"}:
            raise ProtocolError(f"coverage check {check_id or '?'} is still pending or invalid")
        seen[check_id] = status
    if set(seen) != set(expected):
        missing = sorted(set(expected) - set(seen))
        extra = sorted(set(seen) - set(expected))
        raise ProtocolError(f"coverage mismatch; missing={missing}, extra={extra}")


def _validate_ref_list(value: Any, known: set[str], label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProtocolError(f"{label} must be a list of exact N@ref strings")
    refs = [clean(item).lower() for item in value]
    if not allow_empty and not refs:
        raise ProtocolError(f"{label} cannot be empty")
    if len(refs) != len(set(refs)):
        raise ProtocolError(f"{label} repeats a unit reference")
    unknown = [item for item in refs if item not in known]
    if unknown:
        raise ProtocolError(f"{label} contains stale or unknown refs: {', '.join(unknown)}")
    return refs


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in value
    ):
        raise ProtocolError(f"{label} must be a list of non-empty strings")
    return [clean(item) for item in value]


def _known_evidence_refs(task: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for document in task.get("evidence_index", []):
        if not isinstance(document, dict):
            continue
        for field in ("document_id", "source_file", "source_sha256", "sha256"):
            value = clean(document.get(field))
            if value:
                refs.add(value)
        invoice = document.get("invoice")
        if isinstance(invoice, dict):
            number = clean(invoice.get("invoice_number"))
            if number:
                refs.add(number)
    for record in task.get("expense_hint_reconciliation", []):
        if not isinstance(record, dict):
            continue
        for field in ("hint_id", "hint_ref", "display_ref", "display_token", "question_id"):
            value = clean(record.get(field))
            if value:
                refs.add(value)
    return refs


def _evidence_list(value: Any, known: set[str], label: str) -> list[str]:
    refs = _string_list(value, label)
    unknown = [item for item in refs if item not in known]
    if unknown:
        raise ProtocolError(f"{label} contains unknown evidence refs: {', '.join(unknown)}")
    return refs


def _validate_analysis(
    candidate: dict[str, Any],
    task: dict[str, Any],
    known_refs: set[str],
    known_evidence: set[str],
) -> None:
    allowed = {
        "schema_version", "task_id", "source_task_fingerprint",
        "source_allocation_fingerprint", "source_extraction_fingerprint",
        "agent_id", "agent_display_name", "coverage", "summary",
        "proposals", "user_questions", "warnings",
    }
    _validate_root(candidate, allowed)
    if candidate.get("schema_version") != ANALYSIS_SCHEMA:
        raise ProtocolError(f"analyst result schema_version must be {ANALYSIS_SCHEMA}")
    proposals = candidate.get("proposals")
    if not isinstance(proposals, list):
        raise ProtocolError("proposals must be a list")
    proposal_ids: set[str] = set()
    for item in proposals:
        if not isinstance(item, dict):
            raise ProtocolError("each proposal must be an object")
        unknown = set(item) - {
            "proposal_id", "unit_refs", "set", "confidence", "reason", "evidence_refs"
        }
        if unknown:
            raise ProtocolError(f"proposal contains unknown fields: {', '.join(sorted(unknown))}")
        proposal_id = clean(item.get("proposal_id"))
        if not isinstance(item.get("proposal_id"), str) or not proposal_id or proposal_id in proposal_ids:
            raise ProtocolError("proposal_id must be present and unique")
        proposal_ids.add(proposal_id)
        _validate_ref_list(item.get("unit_refs"), known_refs, f"proposal {proposal_id} unit_refs", allow_empty=False)
        updates = item.get("set")
        if not isinstance(updates, dict) or not updates:
            raise ProtocolError(f"proposal {proposal_id} set must be a non-empty object")
        unsupported = sorted(set(updates) - ALLOWED_UNIT_FIELDS)
        if unsupported:
            raise ProtocolError(
                f"proposal {proposal_id} uses unsupported updater fields: {', '.join(unsupported)}"
            )
        update_errors = validate_update(updates, lenient=False)
        if update_errors:
            raise ProtocolError(
                f"proposal {proposal_id} contains invalid updater values: " + "; ".join(update_errors)
            )
        if not isinstance(item.get("confidence"), str) or clean(item.get("confidence")) not in {"high", "medium", "low"}:
            raise ProtocolError(f"proposal {proposal_id} confidence must be high, medium, or low")
        if not isinstance(item.get("reason"), str) or not clean(item.get("reason")):
            raise ProtocolError(f"proposal {proposal_id} requires a reason")
        _evidence_list(
            item.get("evidence_refs", []), known_evidence, f"proposal {proposal_id} evidence_refs"
        )

    questions = candidate.get("user_questions")
    if not isinstance(questions, list):
        raise ProtocolError("user_questions must be a list")
    question_ids: set[str] = set()
    for item in questions:
        if not isinstance(item, dict):
            raise ProtocolError("each user question must be an object")
        unknown = set(item) - {"question_id", "unit_refs", "question", "reason", "blocking"}
        if unknown:
            raise ProtocolError(f"user question contains unknown fields: {', '.join(sorted(unknown))}")
        question_id = clean(item.get("question_id"))
        if not isinstance(item.get("question_id"), str) or not question_id or question_id in question_ids:
            raise ProtocolError("question_id must be present and unique")
        question_ids.add(question_id)
        _validate_ref_list(item.get("unit_refs"), known_refs, f"question {question_id} unit_refs")
        if (
            not isinstance(item.get("question"), str)
            or not isinstance(item.get("reason"), str)
            or not clean(item.get("question"))
            or not clean(item.get("reason"))
        ):
            raise ProtocolError(f"question {question_id} requires question and reason text")
        if not isinstance(item.get("blocking"), bool):
            raise ProtocolError(f"question {question_id} blocking must be true or false")
    _string_list(candidate.get("warnings", []), "warnings")


def _validate_review(
    candidate: dict[str, Any],
    task: dict[str, Any],
    known_refs: set[str],
    known_evidence: set[str],
) -> None:
    allowed = {
        "schema_version", "review_contract_version", "task_id",
        "source_task_fingerprint", "source_allocation_fingerprint",
        "source_extraction_fingerprint", "agent_id", "agent_display_name",
        "coverage", "summary", "outcome", "findings",
    }
    _validate_root(candidate, allowed)
    if candidate.get("schema_version") != REVIEW_SCHEMA:
        raise ProtocolError(f"review schema_version must be {REVIEW_SCHEMA}")
    if candidate.get("review_contract_version") != REVIEW_CONTRACT:
        raise ProtocolError(f"review_contract_version must be {REVIEW_CONTRACT}")
    if not isinstance(candidate.get("outcome"), str):
        raise ProtocolError("review outcome must be a string")
    outcome = clean(candidate.get("outcome"))
    if outcome not in {"pass", "advisory", "block", "unavailable"}:
        raise ProtocolError("review outcome must be pass, advisory, block, or unavailable")
    findings = candidate.get("findings")
    if not isinstance(findings, list):
        raise ProtocolError("review findings must be a list")
    finding_ids: set[str] = set()
    blocking = 0
    advisory = 0
    for item in findings:
        if not isinstance(item, dict):
            raise ProtocolError("each review finding must be an object")
        unknown = set(item) - {
            "finding_id", "severity", "code", "message", "unit_refs",
            "evidence_refs", "recommended_action",
        }
        if unknown:
            raise ProtocolError(f"review finding contains unknown fields: {', '.join(sorted(unknown))}")
        finding_id = clean(item.get("finding_id"))
        if not isinstance(item.get("finding_id"), str) or not finding_id or finding_id in finding_ids:
            raise ProtocolError("finding_id must be present and unique")
        finding_ids.add(finding_id)
        for field in ("severity", "code", "message", "recommended_action"):
            if not isinstance(item.get(field), str):
                raise ProtocolError(f"finding {finding_id} {field} must be a string")
        severity = clean(item.get("severity"))
        if severity not in {"blocking", "advisory"}:
            raise ProtocolError(f"finding {finding_id} severity must be blocking or advisory")
        unit_refs = _validate_ref_list(
            item.get("unit_refs", []), known_refs, f"finding {finding_id} unit_refs"
        )
        evidence_refs = _evidence_list(
            item.get("evidence_refs", []), known_evidence, f"finding {finding_id} evidence_refs"
        )
        if severity == "blocking" and not unit_refs and not evidence_refs:
            raise ProtocolError(f"blocking finding {finding_id} requires a unit or evidence reference")
        if not clean(item.get("code")) or not clean(item.get("message")):
            raise ProtocolError(f"finding {finding_id} requires code and message")
        if not clean(item.get("recommended_action")):
            raise ProtocolError(f"finding {finding_id} requires recommended_action")
        blocking += severity == "blocking"
        advisory += severity == "advisory"
    expected_outcome = "block" if blocking else ("advisory" if advisory else "pass")
    if outcome == "unavailable":
        if findings or not clean(candidate.get("summary")):
            raise ProtocolError("unavailable review must have no findings and must explain why in summary")
    elif outcome != expected_outcome:
        raise ProtocolError(
            f"review outcome {outcome!r} conflicts with findings; expected {expected_outcome!r}"
        )


def validate_result(candidate: dict[str, Any], task: dict[str, Any], allocation: dict[str, Any]) -> None:
    task_ok, task_reason = integrity.check(task)
    if not task_ok or clean((task.get("integrity") or {}).get("stamped_by")) != "subagent_protocol.py":
        raise ProtocolError(f"task packet is not an official valid packet: {task_reason}")
    _validate_binding(candidate, task)
    _validate_coverage(candidate, task)
    if not isinstance(candidate.get("summary"), str) or not clean(candidate.get("summary")):
        raise ProtocolError("result summary cannot be empty")
    text_issues = text_safety.find_suspect_text(candidate, path="subagent_result")
    if text_issues:
        raise ProtocolError("result contains suspect encoding damage: " + "; ".join(text_issues))
    known_refs = set(_unit_tokens(allocation))
    known_evidence = _known_evidence_refs(task)
    if task.get("role_id") == "allocation_analyst":
        _validate_analysis(candidate, task, known_refs, known_evidence)
    else:
        _validate_review(candidate, task, known_refs, known_evidence)


def _unreviewed_proposals(accepted: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": PROPOSAL_SCHEMA,
        "review_status": "unreviewed",
        "source_analysis_fingerprint": clean((accepted.get("integrity") or {}).get("fingerprint")),
        "source_allocation_fingerprint": accepted.get("source_allocation_fingerprint"),
        "proposals": accepted.get("proposals", []),
    }
    integrity.stamp(payload, "subagent_protocol.py")
    return payload


def _archive_review(path: Path, history_dir: Path) -> None:
    if not path.is_file():
        return
    data = path.read_bytes()
    history_dir.mkdir(parents=True, exist_ok=True)
    archive = history_dir / f"stage3-independent-review-{sha256_bytes(data)}.json"
    if not archive.exists():
        archive.write_bytes(data)


def accept_result(
    role: str,
    allocation_path: Path,
    extraction_path: Path,
    process_dir: Path,
    result_path: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _require_canonical_process_dir(process_dir, allocation_path)
    try:
        result_path.resolve().relative_to(process_dir.resolve())
    except ValueError:
        pass
    else:
        raise ProtocolError(
            "raw subagent results are untrusted inputs and must stay outside process/; "
            "save the response under analysis/ or another session path"
        )
    task, allocation = _build_task(role, allocation_path, extraction_path)
    candidate = load_json(result_path)
    validate_result(candidate, task, allocation)
    accepted = dict(candidate)
    accepted["accepted_at"] = datetime.now().isoformat(timespec="microseconds")
    accepted["accepted_by"] = "subagent_protocol.py"
    integrity.stamp(accepted, "subagent_protocol.py")
    paths = task_paths(process_dir, task)
    if role == "allocation_analyst":
        write_json(paths["analyst_result"], accepted)
        write_json(paths["proposal"], _unreviewed_proposals(accepted))
    else:
        result_fingerprint = clean((accepted.get("integrity") or {}).get("fingerprint"))
        archive = paths["review_archive_dir"] / f"{result_fingerprint}.json"
        write_immutable_json(archive, accepted)
        _archive_review(paths["review_result"], paths["review_history"])
        write_json(paths["review_result"], accepted)
        paths["review_archive"] = archive
    return accepted, paths


def _validated_current_analysis(
    process_dir: Path,
    allocation_path: Path,
    extraction_path: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _require_canonical_process_dir(process_dir, allocation_path)
    task, _allocation = _build_task("allocation_analyst", allocation_path, extraction_path)
    paths = task_paths(process_dir, task)
    state = analysis_state(process_dir, allocation_path, extraction_path)
    if not state.get("current"):
        raise ProtocolError(
            "there is no current accepted Otako analysis to promote; prepare and accept a fresh result"
        )
    report = load_json(paths["analyst_result"])
    return report, paths


def promote_proposals(
    allocation_path: Path,
    extraction_path: Path,
    process_dir: Path,
    selected_ids: list[str],
    *,
    select_all: bool,
    reviewed_by: str,
    review_note: str,
    output_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    report, paths = _validated_current_analysis(process_dir, allocation_path, extraction_path)
    proposals = report.get("proposals", []) if isinstance(report.get("proposals"), list) else []
    by_id = {clean(item.get("proposal_id")): item for item in proposals if isinstance(item, dict)}
    if len(by_id) != len(proposals):
        raise ProtocolError("accepted analysis has missing or duplicate proposal IDs")
    if select_all:
        chosen_ids = list(by_id)
    else:
        chosen_ids = [clean(value) for value in selected_ids if clean(value)]
    if not chosen_ids:
        raise ProtocolError("choose at least one proposal ID, or pass --all explicitly")
    if len(chosen_ids) != len(set(chosen_ids)):
        raise ProtocolError("the promotion selection repeats a proposal ID")
    unknown = [proposal_id for proposal_id in chosen_ids if proposal_id not in by_id]
    if unknown:
        raise ProtocolError(f"unknown proposal IDs: {', '.join(unknown)}")
    if reviewed_by not in {"coordinator", "applicant"}:
        raise ProtocolError("reviewed_by must be coordinator or applicant")
    decisions = [
        {
            "units": ",".join(by_id[proposal_id].get("unit_refs", [])),
            "set": by_id[proposal_id].get("set", {}),
        }
        for proposal_id in chosen_ids
    ]
    decision_sha = canonical_sha(decisions)
    payload = {
        "schema_version": "allocation_decisions.v1",
        "for_allocation_fingerprint": report.get("source_allocation_fingerprint"),
        "decisions": decisions,
        "expense_hint_resolutions": [],
        "question_updates": [],
        "project_contexts": [],
        "confirm_units": [],
        "drop_units": [],
        "exclude_units": [],
        "proposal_review": {
            "source_analysis_fingerprint": clean((report.get("integrity") or {}).get("fingerprint")),
            "selected_proposal_ids": chosen_ids,
            "selected_decisions_sha256": decision_sha,
            "reviewed_by": reviewed_by,
            "review_note": clean(review_note),
            "reviewed_at": datetime.now().replace(microsecond=0).isoformat(),
        },
    }
    integrity.stamp(payload, "subagent_protocol.py")
    destination = output_path or paths["promoted_proposal"]
    try:
        destination.resolve().relative_to(paths["promoted_proposal"].parent.resolve())
    except ValueError:
        raise ProtocolError(
            "reviewed proposal output must stay inside analysis/subagent-pilot/proposals/"
        )
    if not destination.name.endswith(".reviewed.json"):
        raise ProtocolError("reviewed proposal output filename must end with .reviewed.json")
    write_json(destination, payload)
    return payload, destination


def validate_promoted_proposal(payload: dict[str, Any], allocation_path: Path) -> None:
    ok, reason = integrity.check(payload)
    if not ok or clean((payload.get("integrity") or {}).get("stamped_by")) != "subagent_protocol.py":
        raise ProtocolError(f"proposal file is not an officially promoted result: {reason}")
    metadata = payload.get("proposal_review")
    if not isinstance(metadata, dict):
        raise ProtocolError("proposal file has no promotion review metadata")
    allocation = load_json(allocation_path)
    current_fp = _full_fingerprint(allocation, "allocation")
    if clean(payload.get("for_allocation_fingerprint")) != current_fp:
        raise ProtocolError("promoted proposal belongs to a different allocation generation")
    extraction_path = allocation_path.parent / "invoice-extraction.json"
    state = analysis_state(allocation_path.parent, allocation_path, extraction_path)
    if not state.get("current"):
        raise ProtocolError("the Otako analysis behind this proposal is no longer current")
    if clean(metadata.get("source_analysis_fingerprint")) != clean(state.get("result_fingerprint")):
        raise ProtocolError("proposal promotion does not bind the current accepted Otako analysis")
    selected_ids = metadata.get("selected_proposal_ids")
    if not isinstance(selected_ids, list) or not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise ProtocolError("proposal promotion has an invalid selected_proposal_ids list")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(selected_ids):
        raise ProtocolError("proposal decision count does not match the reviewed selection")
    if clean(metadata.get("selected_decisions_sha256")) != canonical_sha(decisions):
        raise ProtocolError("proposal decisions changed after selection")
    if clean(metadata.get("reviewed_by")) not in {"coordinator", "applicant"}:
        raise ProtocolError("proposal review metadata has an invalid reviewed_by value")
    task, _allocation = _build_task("allocation_analyst", allocation_path, extraction_path)
    report_path = task_paths(allocation_path.parent, task)["analyst_result"]
    report = load_json(report_path)
    proposals = report.get("proposals", []) if isinstance(report.get("proposals"), list) else []
    by_id = {clean(item.get("proposal_id")): item for item in proposals if isinstance(item, dict)}
    if any(proposal_id not in by_id for proposal_id in selected_ids):
        raise ProtocolError("proposal selection names an ID absent from the current accepted analysis")
    expected_decisions = [
        {
            "units": ",".join(by_id[proposal_id].get("unit_refs", [])),
            "set": by_id[proposal_id].get("set", {}),
        }
        for proposal_id in selected_ids
    ]
    if decisions != expected_decisions:
        raise ProtocolError("promoted decisions do not exactly match the selected Otako proposals")


def _validate_accepted_review(
    report: dict[str, Any],
    task: dict[str, Any],
    allocation: dict[str, Any],
) -> None:
    ok, reason = integrity.check(report)
    if not ok or clean((report.get("integrity") or {}).get("stamped_by")) != "subagent_protocol.py":
        raise ProtocolError(f"review integrity failed: {reason}")
    if clean(report.get("accepted_by")) != "subagent_protocol.py" or not clean(report.get("accepted_at")):
        raise ProtocolError("review is not an officially accepted result")
    try:
        datetime.fromisoformat(clean(report.get("accepted_at")))
    except ValueError as exc:
        raise ProtocolError("review accepted_at is invalid") from exc
    candidate = dict(report)
    candidate.pop("integrity", None)
    candidate.pop("accepted_at", None)
    candidate.pop("accepted_by", None)
    validate_result(candidate, task, allocation)


def review_state(
    process_dir: Path,
    allocation: dict[str, Any],
    allocation_path: Path | None = None,
    extraction_path: Path | None = None,
) -> dict[str, Any]:
    canonical_path = process_dir / "stage3-independent-review.json"
    state: dict[str, Any] = {
        "path": str(canonical_path),
        "status": "missing",
        "current": False,
        "outcome": "not_run",
        "blocking_count": 0,
        "advisory_count": 0,
        "result_fingerprint": "",
        "findings": [],
        "reason": "no accepted current-task review exists; deterministic preflight remains authoritative",
    }
    allocation_path = allocation_path or process_dir / "expense-allocation.json"
    extraction_path = extraction_path or process_dir / "invoice-extraction.json"
    try:
        _require_canonical_process_dir(process_dir, allocation_path)
        task, current_allocation = _build_task(
            "independent_reviewer",
            allocation_path,
            extraction_path,
        )
    except ProtocolError as exc:
        return {**state, "status": "unavailable", "reason": str(exc)}
    if clean((allocation.get("integrity") or {}).get("fingerprint")) != clean(
        (current_allocation.get("integrity") or {}).get("fingerprint")
    ):
        return {
            **state,
            "status": "invalid",
            "reason": "review lookup allocation does not match the canonical process allocation",
        }

    paths = task_paths(process_dir, task)
    archive_paths = sorted(paths["review_archive_dir"].glob("*.json"))
    candidate_paths = list(archive_paths)
    if canonical_path.is_file():
        candidate_paths.append(canonical_path)
    valid: dict[str, tuple[dict[str, Any], Path]] = {}
    errors: list[str] = []
    for candidate_path in candidate_paths:
        try:
            report = load_json(candidate_path)
            _validate_accepted_review(report, task, current_allocation)
        except ProtocolError as exc:
            errors.append(f"{candidate_path.name}: {exc}")
            continue
        fingerprint = clean((report.get("integrity") or {}).get("fingerprint"))
        valid[fingerprint] = (report, candidate_path)

    if not valid:
        archive_root = process_dir / "subagent-review-generations"
        has_older_archive = archive_root.is_dir() and any(archive_root.glob("*/*.json"))
        if candidate_paths:
            stale = any("current task" in error or "different allocation" in error for error in errors)
            return {
                **state,
                "status": "stale" if stale else "invalid",
                "reason": "; ".join(errors) or "no valid accepted review result",
            }
        if has_older_archive:
            return {
                **state,
                "status": "stale",
                "reason": "accepted reviews exist only for an older task or rule generation",
            }
        return state

    report, selected_path = max(
        valid.values(),
        key=lambda item: (
            clean(item[0].get("accepted_at")),
            {"unavailable": 0, "pass": 1, "advisory": 2, "block": 3}.get(
                clean(item[0].get("outcome")),
                -1,
            ),
            clean((item[0].get("integrity") or {}).get("fingerprint")),
        ),
    )
    outcome = clean(report.get("outcome"))
    findings = report.get("findings", [])
    blocking = sum(1 for finding in findings if finding.get("severity") == "blocking")
    advisory = sum(1 for finding in findings if finding.get("severity") == "advisory")
    recovered = selected_path != canonical_path
    return {
        **state,
        "path": str(selected_path),
        "canonical_path": str(canonical_path),
        "status": "current",
        "current": True,
        "outcome": outcome,
        "blocking_count": blocking,
        "advisory_count": advisory,
        "result_fingerprint": clean((report.get("integrity") or {}).get("fingerprint")),
        "findings": findings,
        "summary": clean(report.get("summary")),
        "task_id": clean(task.get("task_id")),
        "source_task_fingerprint": clean((task.get("integrity") or {}).get("fingerprint")),
        "recovered_from_archive": recovered,
        "reason": "recovered from immutable accepted-review archive" if recovered else "ok",
    }


def analysis_state(
    process_dir: Path,
    allocation_path: Path,
    extraction_path: Path,
) -> dict[str, Any]:
    """Inspect the current Otako report without making it a workflow gate."""
    try:
        _require_canonical_process_dir(process_dir, allocation_path)
        task, _allocation = _build_task("allocation_analyst", allocation_path, extraction_path)
    except ProtocolError as exc:
        return {
            "status": "unavailable",
            "current": False,
            "reason": str(exc),
            "role": "allocation_analyst",
        }
    paths = task_paths(process_dir, task)
    state: dict[str, Any] = {
        "status": "missing",
        "current": False,
        "reason": "no current allocation analysis result",
        "role": "allocation_analyst",
        "task_id": task.get("task_id"),
        "task_path": str(paths["task"]),
        "result_template_path": str(paths["template"]),
        "result_path": str(paths["analyst_result"]),
        "proposal_path": str(paths["proposal"]),
        "source_allocation_fingerprint": clean(
            (task.get("source_generation") or {}).get("source_allocation_fingerprint")
        ),
    }
    path = paths["analyst_result"]
    if not path.exists():
        return state
    try:
        report = load_json(path)
    except ProtocolError as exc:
        return {**state, "status": "invalid", "reason": str(exc)}
    ok, reason = integrity.check(report)
    if not ok or clean((report.get("integrity") or {}).get("stamped_by")) != "subagent_protocol.py":
        return {**state, "status": "invalid", "reason": f"analysis integrity failed: {reason}"}
    if report.get("schema_version") != ANALYSIS_SCHEMA:
        return {**state, "status": "invalid", "reason": "analysis schema version is unsupported"}
    if clean(report.get("task_id")) != clean(task.get("task_id")):
        return {**state, "status": "stale", "reason": "analysis belongs to a different task"}
    if clean(report.get("source_task_fingerprint")) != clean(
        (task.get("integrity") or {}).get("fingerprint")
    ):
        return {**state, "status": "stale", "reason": "analysis task fingerprint is stale"}
    proposals = report.get("proposals", []) if isinstance(report.get("proposals"), list) else []
    questions = report.get("user_questions", []) if isinstance(report.get("user_questions"), list) else []
    return {
        **state,
        "status": "current",
        "current": True,
        "reason": "ok",
        "result_fingerprint": clean((report.get("integrity") or {}).get("fingerprint")),
        "proposal_count": len(proposals),
        "question_count": len(questions),
        "summary": clean(report.get("summary")),
    }


def review_record(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status", "missing"),
        "outcome": state.get("outcome", "not_run"),
        "result_fingerprint": state.get("result_fingerprint", ""),
        "blocking_count": int(state.get("blocking_count", 0) or 0),
        "advisory_count": int(state.get("advisory_count", 0) or 0),
        "summary": state.get("summary", ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and validate read-only subagent handoffs.")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Create a path-free immutable task packet.")
    prepare.add_argument("--role", choices=sorted(ROLE_SPECS), required=True)
    prepare.add_argument("--allocation", required=True)
    prepare.add_argument("--extraction", required=True)
    prepare.add_argument("--process-dir", default="process")

    accept = commands.add_parser("accept", help="Validate and stamp a returned subagent JSON result.")
    accept.add_argument("--role", choices=sorted(ROLE_SPECS), required=True)
    accept.add_argument("--allocation", required=True)
    accept.add_argument("--extraction", required=True)
    accept.add_argument("--process-dir", default="process")
    accept.add_argument("--result", required=True)

    promote = commands.add_parser(
        "promote",
        help="Promote explicitly reviewed Otako proposal IDs into canonical decisions.",
    )
    promote.add_argument("--allocation", required=True)
    promote.add_argument("--extraction", required=True)
    promote.add_argument("--process-dir", default="process")
    selection = promote.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--select",
        action="append",
        default=[],
        help="Comma-separated proposal IDs; repeatable.",
    )
    selection.add_argument("--all", action="store_true", help="Explicitly promote every proposal.")
    promote.add_argument("--reviewed-by", choices=["coordinator", "applicant"], required=True)
    promote.add_argument("--note", default="")
    promote.add_argument("--output")

    inspect_review = commands.add_parser("inspect-review", help="Inspect the current Stage 3 review sidecar.")
    inspect_review.add_argument("--allocation", required=True)
    inspect_review.add_argument("--process-dir", default="process")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            task, paths = prepare_task(
                args.role,
                Path(args.allocation),
                Path(args.extraction),
                Path(args.process_dir),
            )
            print(f"Prepared {task.get('display_name')} task {task.get('task_id')}")
            print(f"Task packet: {paths['task']}")
            print(f"Result template: {paths['template']}")
            print("SUBAGENT HANDOFF: start a fresh read-only subagent with the COMPLETE JSON contents "
                  "of both files. Do not give it filesystem paths or mutation tools. Save its exact JSON "
                  "response outside process/, then run Chief accept-agent.")
            return 0
        if args.command == "accept":
            accepted, paths = accept_result(
                args.role,
                Path(args.allocation),
                Path(args.extraction),
                Path(args.process_dir),
                Path(args.result),
            )
            print(f"Accepted {accepted.get('agent_display_name')} result.")
            if args.role == "allocation_analyst":
                print(f"Analysis report: {paths['analyst_result']}")
                print(f"Unreviewed proposals: {paths['proposal']}")
                print("NEXT: review proposal IDs with the applicant/coordinator, then run Chief promote-proposals. "
                      "The unreviewed file is intentionally rejected by Composer.")
            else:
                print(f"Independent review: {paths['review_result']}")
                print(f"Outcome: {accepted.get('outcome')}")
                print("NEXT: run Chief status; a current blocking review prevents Stage 3.")
            return 0
        if args.command == "promote":
            selected_ids = [
                item.strip()
                for group in args.select
                for item in group.split(",")
                if item.strip()
            ]
            _payload, destination = promote_proposals(
                Path(args.allocation),
                Path(args.extraction),
                Path(args.process_dir),
                selected_ids,
                select_all=bool(args.all),
                reviewed_by=args.reviewed_by,
                review_note=args.note,
                output_path=Path(args.output) if args.output else None,
            )
            print(f"Promoted reviewed proposals: {destination}")
            print("NEXT: run Chief compose --proposal with this exact stamped file, then apply normally.")
            return 0
        allocation = load_json(Path(args.allocation))
        _full_fingerprint(allocation, "allocation")
        print(json.dumps(review_state(Path(args.process_dir), allocation), ensure_ascii=False, indent=2))
        return 0
    except ProtocolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("NEXT: regenerate the task from the current allocation and return JSON matching its generated template.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
