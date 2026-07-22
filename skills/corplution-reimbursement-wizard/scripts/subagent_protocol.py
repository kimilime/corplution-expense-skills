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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib

import integrity
from exit_codes import ExitCode
from io_utils import configure_utf8_stdio as configure_stdio, sha256_file
import json_io
import text_safety
import allocation_generations
from text_utils import strip_scalar as clean
import time_utils
import value_utils
from apply_allocation_answers import ALLOWED_UNIT_FIELDS, validate_update


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "assets" / "policy.toml"

TASK_SCHEMA = "subagent_task.v2"
# Both subagent roles return the same audit shape: outcome + findings. Each role
# binds its own contract_version (below) so a result cannot be replayed as the
# other role's audit.
AUDIT_SCHEMA = "subagent_audit.v2"

# Inline handoff cap. The packet is pasted whole into a fresh Agent/Task-tool prompt
# (Claude Code has no attachment channel), so this bounds the inline prompt cost, not a
# transport limit. At ~10 KiB fixed + ~870 B/compacted-unit, 384 KiB clears ~430 units,
# well past any realistic reimbursement; a packet still over this fails open to the
# deterministic Stage-3 preflight (see HandoffTooLarge) rather than being hand-assembled.
MAX_HANDOFF_PACKET_BYTES = 384 * 1024
PACKET_TEXT_LIMIT = 480
PACKET_NESTED_LIST_LIMIT = 24
COVERAGE_STATUSES = ("completed", "not_applicable")
COVERAGE_FIELDS = ("check_id", "status", "notes")
FINDING_FIELDS = (
    "finding_id", "severity", "code", "message", "unit_refs",
    "evidence_refs", "recommended_action",
)

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "mirror_warden": {
        "codename": "otako",
        "display_name": "Otako - Mirror Warden",
        "role_title": "Otako, the Mirror Warden",
        "reference": SKILL_DIR / "references" / "otako-mirror-warden.md",
        "contract_version": "mirror-warden-audit.v2",
        "coverage": [
            "evidence_attribution",
            "journey_coherence",
            "date_route_consistency",
            "amount_evidence_match",
            "duplicate_claim",
            "claimed_evidence_completeness",
            "unaccounted_material",
        ],
    },
    "gate_challenger": {
        "codename": "kaede",
        "display_name": "Kaede - Gate Challenger",
        "role_title": "Kaede, the Gate Challenger",
        "reference": SKILL_DIR / "references" / "kaede-gate-challenger.md",
        "contract_version": "gate-challenger-audit.v2",
        "coverage": [
            "policy_treatment",
            "approval_sufficiency",
            "business_claimability",
            "admin_client_semantics",
            "substitute_invoice_compliance",
        ],
    },
}

# The task packet exposes these rules to the subagent and the accept path enforces
# them. Descriptions are intentionally short: role references carry the reasoning
# boundaries, while this map is the machine-checkable vocabulary.
FINDING_CODE_RULES: dict[str, dict[str, dict[str, Any]]] = {
    "mirror_warden": {
        "attribution_conflict": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "Concrete evidence contradicts the assigned project/client/code.",
        },
        "journey_conflict": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "Cited journey legs are chronologically or geographically incoherent.",
        },
        "date_route_conflict": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "Printed dates/routes directly conflict with the allocation.",
        },
        "amount_evidence_conflict": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "minimum_evidence_refs": 1,
            "description": "The unit amount conflicts with its source evidence, excluding a lower reimbursable amount.",
        },
        "claim_exceeds_evidence": {
            "severities": ("blocking",),
            "minimum_unit_refs": 1,
            "description": "The reimbursable amount is numerically greater than the invoice/evidence amount.",
        },
        "duplicate_claim": {
            "severities": ("blocking", "advisory"),
            "minimum_duplicate_subjects": 2,
            "description": "Two or more cited units/evidence records claim the same economic expense.",
        },
        "claimed_evidence_missing": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "A claimed unit lacks evidence required for that claimed expense itself.",
        },
        "unresolved_material": {
            "severities": ("blocking", "advisory"),
            "minimum_evidence_refs": 1,
            "description": "Active evidence or an applicant hint is silently unaccounted for.",
        },
    },
    "gate_challenger": {
        "policy_treatment_conflict": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "The claim treatment conflicts with an explicit Corplution policy rule.",
        },
        "missing_required_approval": {
            "severities": ("blocking",),
            "minimum_unit_refs": 1,
            "description": "The packet marks an approval as required and that approval is absent.",
        },
        "plainly_non_reimbursable": {
            "severities": ("blocking",),
            "minimum_unit_refs": 1,
            "description": "Direct evidence makes the cited expense plainly personal or non-reimbursable.",
        },
        "admin_semantics_conflict": {
            "severities": ("advisory",),
            "minimum_unit_refs": 1,
            "description": "Admin/client wording conflicts with configured semantics but does not block submission.",
        },
        "substitute_invoice_noncompliance": {
            "severities": ("blocking", "advisory"),
            "minimum_unit_refs": 1,
            "description": "A substitute invoice is not marked or supported as policy requires.",
        },
        "declared_policy_exception": {
            "severities": ("advisory",),
            "minimum_unit_refs": 1,
            "description": "An applicant-declared exception exceeds the standing policy and is informational only.",
        },
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

COMMON_UNIT_PACKET_FIELDS = (
    "unit_id", "user_no", "unit_no", "unit_ref", "source_document_id", "source_doc_id",
    "source_item_id", "source_file", "source_filename", "source_sha256", "document_subtype",
    "source_category", "invoice_no", "seller_name",
    "status", "confidence", "needs_user_confirmation", "amount", "invoice_amount",
    "reimbursable_amount", "expense_date",
    "date_source", "date_required", "date_is_provisional", "city", "formal_city", "hotel_city",
    "origin", "destination", "route", "train_no", "travel_date", "departure_time",
    "client_name", "client_charge_code", "project_context_id", "final_template_column",
    "supporting_invoice_document_id", "supporting_invoice_filename",
    "supporting_schedule_document_id", "supporting_schedule_filename",
)
# Mirror Warden reconciles attribution + journey coherence against evidence:
# hint-match, journey-chain, amounts, dates, and invoice identity for dedup.
MIRROR_WARDEN_UNIT_PACKET_FIELDS = COMMON_UNIT_PACKET_FIELDS + (
    "expense_note", "match_reason", "auto_project_match", "hint_match_score",
    "hint_match_summary", "hint_match_reasons", "matched_expense_hint_ids", "hint_candidates",
    "journey_chain_id", "journey_chain_position", "journey_chain_route",
    "journey_chain_assignment_rule", "journey_chain_confidence", "journey_chain_status",
    "meal_context", "attendees", "business_reason", "check_in_date", "check_out_date",
    "hotel_nights", "is_refund_fee", "refund_fee_amount", "is_substitute_invoice",
    "substitute_for", "invoice_no", "issues",
)
# Gate Challenger is the narrow policy gate: treatment, explicit approval rules,
# plain non-reimbursability, Admin semantics, and substitute-invoice compliance.
GATE_CHALLENGER_UNIT_PACKET_FIELDS = COMMON_UNIT_PACKET_FIELDS + (
    "final_note", "expense_note", "meal_context", "attendees", "business_reason",
    "check_in_date", "check_out_date", "hotel_nights", "hotel_shared_with", "shared_room_with",
    "is_refund_fee", "refund_fee_amount", "is_substitute_invoice", "substitute_for",
    "approval_required", "approval_file_status", "partner_approval_document_id",
    "approval_screenshot_document_id", "support_type", "supports_document_id",
    "journey_chain_id", "journey_chain_route", "journey_chain_assignment_rule", "issues",
)
PROJECT_CONTEXT_PACKET_FIELDS = (
    "context_id", "date_start", "date_end", "city", "cities", "client_name",
    "client_charge_code", "project_description", "user_notes", "travel_buffer_days", "status",
    "local_match_keywords", "meal_hints", "expense_hints", "meal_standards",
)
QUESTION_PACKET_FIELDS = (
    "question_id", "question_type", "status", "blocking", "unit_ids", "user_nos", "hint_ids",
    "required_answer_tokens", "question", "why_it_matters", "reason",
)
HINT_PACKET_FIELDS = (
    "hint_id", "hint_ref", "display_ref", "display_token", "source_field", "source_fields",
    "source_index", "project_context_id", "client_name", "client_charge_code", "source_category",
    "summary", "match_status", "resolution_status", "matched_unit_ids", "matched_user_nos",
    "candidate_units", "question_id", "resolution_answer", "resolution_action",
)
DOCUMENT_PACKET_FIELDS = (
    "document_id", "source_file", "source_sha256", "sha256", "document_role", "document_subtype",
    "needs_review", "review_status", "excluded_by_user", "excluded_reason",
    "linked_invoice_document_id", "linked_trip_report_document_id", "linked_document_ids",
    "supporting_document_ids", "source_category", "classification_summary",
)
INVOICE_PACKET_FIELDS = (
    "invoice_number", "seller_name", "issue_date", "total_amount", "amount", "currency",
    "invoice_type", "invoice_kind", "line_item_name",
)
CLASSIFICATION_PACKET_FIELDS = (
    "expense_category", "source_category", "document_role", "document_subtype", "city", "route",
    "origin", "destination", "travel_date", "departure_time", "railway_leg", "refund_fee_amount",
    "hotel_city", "check_in_date", "check_out_date", "hotel_nights",
)
UNRESOLVED_INPUT_PACKET_FIELDS = (
    "source_file", "filename", "source_sha256", "sha256", "reason", "status", "resolution_status",
)


class ProtocolError(ValueError):
    INVALID_RESULT = "invalid_result"
    STALE_TASK = "stale_task"

    def __init__(self, message: str, *, code: str = INVALID_RESULT) -> None:
        super().__init__(message)
        self.code = code


class HandoffTooLarge(ProtocolError):
    """The compact packet exceeds the inline cap.

    This is a fail-open DEGRADE to the deterministic Stage-3 preflight, not a defect to
    hand-fix. The coordinator must never hand-assemble a packet, split it manually, or
    manually accept a result for a degraded task — that bypasses the fingerprint binding
    and immutable archive that make the audit trustworthy. `prepare` treats it as a clean
    degrade (exit 0); `accept` treats it as a refusal.
    """

    def __init__(self, display_name: str, packet_bytes: int) -> None:
        self.display_name = display_name
        self.packet_bytes = packet_bytes
        super().__init__(
            f"{display_name} compact handoff is {packet_bytes:,} bytes, over the "
            f"{MAX_HANDOFF_PACKET_BYTES:,}-byte inline cap. Fail open: this audit degrades to the "
            "deterministic Stage-3 preflight, which stays authoritative. Do not hand-assemble a "
            "packet, split it manually, or manually accept a result — proceed to Stage 3 write."
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json_io.read_json_object(path)
    except json_io.JsonReadError as exc:
        raise ProtocolError(str(exc)) from exc


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


def _compact_packet_value(value: Any) -> Any:
    """Bound selected snapshot values without dropping top-level evidence records."""
    if isinstance(value, str):
        return value if len(value) <= PACKET_TEXT_LIMIT else value[:PACKET_TEXT_LIMIT] + " [truncated]"
    if isinstance(value, list):
        items = [_compact_packet_value(item) for item in value[:PACKET_NESTED_LIST_LIMIT]]
        if len(value) > PACKET_NESTED_LIST_LIMIT:
            items.append(f"[{len(value) - PACKET_NESTED_LIST_LIMIT} additional item(s) omitted]")
        return items
    if isinstance(value, dict):
        return {str(key): _compact_packet_value(item) for key, item in value.items()}
    return value


def _compact_record(record: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {"invalid_record": _compact_packet_value(sanitize_snapshot(record))}
    output: dict[str, Any] = {}
    for field in fields:
        if field not in record:
            continue
        value = record.get(field)
        if value in (None, "", [], {}):
            continue
        output[field] = _compact_packet_value(sanitize_snapshot(value, field))
    return output


def _compact_document(document: Any) -> dict[str, Any]:
    output = _compact_record(document, DOCUMENT_PACKET_FIELDS)
    if not isinstance(document, dict):
        return output
    invoice = document.get("invoice")
    if isinstance(invoice, dict):
        output["invoice"] = _compact_record(invoice, INVOICE_PACKET_FIELDS)
    classification = document.get("classification")
    if isinstance(classification, dict):
        output["classification"] = _compact_record(classification, CLASSIFICATION_PACKET_FIELDS)
    return output


def _compact_task_snapshot(
    role: str,
    allocation: dict[str, Any],
    extraction: dict[str, Any],
) -> dict[str, Any]:
    unit_fields = MIRROR_WARDEN_UNIT_PACKET_FIELDS if role == "mirror_warden" else GATE_CHALLENGER_UNIT_PACKET_FIELDS
    units = [
        _compact_record(unit, unit_fields)
        for unit in allocation.get("allocation_units", [])
    ]
    documents = [
        _compact_document(document)
        for document in extraction.get("documents", [])
    ]
    questions = [
        _compact_record(question, QUESTION_PACKET_FIELDS)
        for question in allocation.get("questions", [])
    ]
    hints = [
        _compact_record(hint, HINT_PACKET_FIELDS)
        for hint in allocation.get("expense_hint_reconciliation", [])
    ]
    unresolved_inputs = [
        _compact_record(item, UNRESOLVED_INPUT_PACKET_FIELDS)
        for item in extraction.get("unresolved_input_files", [])
    ]
    contexts = [
        _compact_record(context, PROJECT_CONTEXT_PACKET_FIELDS)
        for context in allocation.get("project_contexts", [])
    ]
    return {
        "snapshot_mode": "role_scoped_compact.v1",
        "snapshot_limits": {
            "text_field_char_limit": PACKET_TEXT_LIMIT,
            "nested_list_item_limit": PACKET_NESTED_LIST_LIMIT,
            "max_packet_bytes": MAX_HANDOFF_PACKET_BYTES,
        },
        "snapshot_summary": {
            "allocation_unit_count": len(units),
            "evidence_document_count": len(documents),
            "allocation_question_count": len(questions),
            "expense_hint_record_count": len(hints),
            "unresolved_input_file_count": len(unresolved_inputs),
        },
        "project_contexts": contexts,
        "allocation_units": units,
        "allocation_questions": questions,
        "expense_hint_reconciliation": hints,
        "evidence_index": documents,
        "unresolved_input_files": unresolved_inputs,
    }


def task_packet_size_bytes(task: dict[str, Any]) -> int:
    return len(_json_bytes(task))


def _enforce_handoff_cap(task: dict[str, Any]) -> int:
    """Return the packet size, raising HandoffTooLarge when it exceeds the inline cap.

    Kept out of _build_task on purpose: size only matters for the inline handoff, not for
    the fingerprint validation that accept/audit_state rebuild the task for. Only prepare
    and accept gate on it.
    """
    packet_bytes = task_packet_size_bytes(task)
    if packet_bytes > MAX_HANDOFF_PACKET_BYTES:
        raise HandoffTooLarge(clean(task.get("display_name")) or "subagent", packet_bytes)
    return packet_bytes


def finding_code_rules(role: str) -> dict[str, dict[str, Any]]:
    try:
        return FINDING_CODE_RULES[role]
    except KeyError as exc:
        raise ProtocolError(f"no finding-code contract exists for role {role!r}") from exc


def response_contract(role: str, coverage: list[str]) -> dict[str, Any]:
    code_rules = finding_code_rules(role)
    contract: dict[str, Any] = {
        "return_format": "Return exactly one UTF-8 JSON object with no Markdown.",
        "coverage_entry": {
            "required_and_only_fields": list(COVERAGE_FIELDS),
            "allowed_statuses": list(COVERAGE_STATUSES),
            "rule": (
                "Every required check must appear once. Do not use pass, advisory, block, "
                "unavailable, or pending as a coverage status."
            ),
        },
        "required_coverage_check_ids": coverage,
        "structured_output_hint": (
            "When the host supports JSON Schema response mode, use response_json_schema. "
            "Otherwise start from the supplied result template exactly."
        ),
        "interaction_rule": (
            "Blocking findings require applicant action. Advisory findings are information only: "
            "do not phrase them as questions or request a decision."
        ),
    }
    contract.update({
        "outcome": {
            "allowed_values": ["pass", "advisory", "block", "unavailable"],
            "rule": "Put the overall conclusion only here, never in coverage[].status.",
        },
        "finding": {
            "required_and_only_fields": list(FINDING_FIELDS),
            "severity_values": ["blocking", "advisory"],
            "blocking_reference_rule": "A blocking finding needs a current unit_refs or evidence_refs entry.",
            "allowed_codes": {
                code: {
                    "allowed_severities": list(rule["severities"]),
                    "description": rule["description"],
                }
                for code, rule in code_rules.items()
            },
        },
    })
    return contract


def response_json_schema(role: str, coverage: list[str]) -> dict[str, Any]:
    code_rules = finding_code_rules(role)
    coverage_item = {
        "type": "object",
        "additionalProperties": False,
        "required": list(COVERAGE_FIELDS),
        "properties": {
            "check_id": {"type": "string", "enum": coverage},
            "status": {"type": "string", "enum": list(COVERAGE_STATUSES)},
            "notes": {"type": "string"},
        },
    }
    properties: dict[str, Any] = {
        "schema_version": {"type": "string", "const": AUDIT_SCHEMA},
        "task_id": {"type": "string"},
        "source_task_fingerprint": {"type": "string"},
        "source_allocation_fingerprint": {"type": "string"},
        "source_extraction_fingerprint": {"type": "string"},
        "agent_id": {"type": "string"},
        "agent_display_name": {"type": "string"},
        "coverage": {
            "type": "array",
            "minItems": len(coverage),
            "maxItems": len(coverage),
            "items": coverage_item,
        },
        "summary": {"type": "string", "minLength": 1},
    }
    required = list(properties)
    properties.update({
        "audit_contract_version": {"type": "string", "const": role_spec(role)["contract_version"]},
        "outcome": {"type": "string", "enum": ["pass", "advisory", "block", "unavailable"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FINDING_FIELDS),
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "enum": ["blocking", "advisory"]},
                    "code": {"type": "string", "enum": sorted(code_rules)},
                    "message": {"type": "string", "minLength": 1},
                    "unit_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "recommended_action": {"type": "string", "minLength": 1},
                },
            },
        },
    })
    required.extend(["audit_contract_version", "outcome", "findings"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


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
    # Both audits run at the same pre-Stage-3 checkpoint on a confirmed allocation.
    readiness_errors = _review_readiness_errors(allocation, allocation_path)
    if readiness_errors:
        raise ProtocolError(
            "subagent audit is premature because Stage 2 is not ready: "
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
    task_id = allocation_generations.canonical_sha(basis)
    snapshot = _compact_task_snapshot(role, allocation, extraction)
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
        "result_contract": response_contract(role, list(spec["coverage"])),
        "response_json_schema": response_json_schema(role, list(spec["coverage"])),
        **snapshot,
        "handoff_rules": [
            "Use only this packet; do not access files, tools, scripts, or prior-agent reasoning.",
            (
                "The coordinator must pass this packet and the result template directly through a read-only "
                "attachment/resource handoff when the host supports it. Never ask the subagent to locate or read "
                "workspace files."
            ),
            "When the host supports structured JSON output, use response_json_schema; otherwise fill the result template exactly.",
            "Return exactly one JSON object and no Markdown.",
            "Do not mutate reimbursement artifacts or claim that any artifact was changed; you only audit.",
            "Bind the result to the exact task and allocation fingerprints in the result template.",
            "Optimize for precision, not finding count; silence is correct when no concrete material defect exists.",
            "Only blocking findings require applicant action. Advisory findings are informational and must not ask for a decision.",
        ],
    }
    integrity.stamp(task, "subagent_protocol.py")
    # Size is not enforced here: accept/audit_state rebuild the task only to recompute
    # fingerprints, and a large allocation must not make those paths fail. prepare and
    # accept call _enforce_handoff_cap themselves to gate the actual inline handoff.
    return task, allocation


def result_template(task: dict[str, Any]) -> dict[str, Any]:
    task_fp = clean((task.get("integrity") or {}).get("fingerprint"))
    allocation_fp = clean((task.get("source_generation") or {}).get("source_allocation_fingerprint"))
    extraction_fp = clean((task.get("source_generation") or {}).get("source_extraction_fingerprint"))
    coverage = [
        {"check_id": check_id, "status": "completed", "notes": ""}
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
    return {
        "schema_version": AUDIT_SCHEMA,
        "audit_contract_version": clean(task.get("contract_version")),
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
        "audit_result": process_dir / f"{role}-audit.json",
        "audit_history": analysis_root / "audit-history" / role,
        "audit_archive_dir": process_dir / "subagent-audit-generations" / role / task_id,
    }


def prepare_task(
    role: str,
    allocation_path: Path,
    extraction_path: Path,
    process_dir: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _require_canonical_process_dir(process_dir, allocation_path)
    task, _allocation = _build_task(role, allocation_path, extraction_path)
    # Gate before writing anything: an over-cap packet degrades (HandoffTooLarge) and must
    # leave no half-written handoff behind for a coordinator to pick up and accept.
    _enforce_handoff_cap(task)
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
            raise ProtocolError(
                f"result {field} does not exactly match the current task",
                code=ProtocolError.STALE_TASK,
            )


def _validate_coverage(candidate: dict[str, Any], task: dict[str, Any]) -> None:
    coverage = candidate.get("coverage")
    if not isinstance(coverage, list):
        raise ProtocolError("result coverage must be a list")
    expected = list(task.get("required_coverage", []))
    seen: dict[str, str] = {}
    for item in coverage:
        if not isinstance(item, dict) or set(item) - set(COVERAGE_FIELDS):
            raise ProtocolError(
                "each coverage entry may contain only check_id, status, and notes; "
                "status must be completed or not_applicable"
            )
        if any(not isinstance(item.get(field, ""), str) for field in ("check_id", "status", "notes")):
            raise ProtocolError("coverage check_id, status, and notes must be strings")
        check_id = clean(item.get("check_id"))
        status = clean(item.get("status"))
        if check_id in seen:
            raise ProtocolError(f"coverage repeats check {check_id}")
        if status not in COVERAGE_STATUSES:
            role_hint = (
                "Put pass/advisory/block/unavailable only in outcome and concrete defects in findings."
            )
            raise ProtocolError(
                f"coverage[{check_id or '?'}].status={status!r} is invalid; only completed or "
                f"not_applicable are allowed. {role_hint}"
            )
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
        raise ProtocolError(
            f"{label} contains stale or unknown refs: {', '.join(unknown)}",
            code=ProtocolError.STALE_TASK,
        )
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


def _decimal_amount(value: Any) -> Decimal | None:
    raw = clean(value).replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _unit_claim_exceeds_evidence(unit: dict[str, Any]) -> bool:
    invoice = _decimal_amount(
        value_utils.first_nonblank(unit.get("invoice_amount"), unit.get("amount"))
    )
    claim = _decimal_amount(
        value_utils.first_nonblank(unit.get("reimbursable_amount"), unit.get("amount"))
    )
    return invoice is not None and claim is not None and claim > invoice


def _unit_is_only_lower_reimbursement(unit: dict[str, Any]) -> bool:
    invoice = _decimal_amount(unit.get("invoice_amount"))
    amount = _decimal_amount(unit.get("amount"))
    claim = _decimal_amount(unit.get("reimbursable_amount"))
    return (
        invoice is not None
        and amount is not None
        and claim is not None
        and amount == invoice
        and claim < invoice
    )


def _unit_lacks_claim_evidence(unit: dict[str, Any], known_evidence: set[str]) -> bool:
    def present(field: str) -> bool:
        value = clean(unit.get(field))
        return bool(value and value in known_evidence)

    if clean(unit.get("source_category")) == "taxi":
        return not (
            present("supporting_invoice_document_id")
            and present("supporting_schedule_document_id")
        )
    return not (
        present("supporting_invoice_document_id")
        or present("source_document_id")
        or present("source_doc_id")
    )


def _unit_lacks_required_approval(unit: dict[str, Any]) -> bool:
    required = clean(unit.get("approval_required")).lower()
    if required in {"", "0", "false", "no", "none", "not_required"}:
        return False
    status = clean(unit.get("approval_file_status")).lower()
    if status in {"provided", "linked", "attached", "available", "complete"}:
        return False
    return not any(
        clean(unit.get(field))
        for field in ("partner_approval_document_id", "approval_screenshot_document_id")
    )


def _truthy(value: Any) -> bool:
    return clean(value).lower() not in {"", "0", "false", "no", "none", "null"}


def _resolved_hint_evidence_refs(task: dict[str, Any]) -> set[str]:
    resolved: set[str] = set()
    closed_actions = {"matched_existing", "covered_by_invoice", "not_reimbursed"}
    closed_statuses = {"resolved", "not_required"}
    for record in task.get("expense_hint_reconciliation", []):
        if not isinstance(record, dict):
            continue
        action = clean(record.get("resolution_action")).lower()
        status = clean(record.get("resolution_status")).lower()
        if action not in closed_actions and status not in closed_statuses:
            continue
        for field in ("hint_id", "hint_ref", "display_ref", "display_token", "question_id"):
            value = clean(record.get(field))
            if value:
                resolved.add(value)
    return resolved


def _evidence_subject_aliases(task: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for index, document in enumerate(task.get("evidence_index", []), 1):
        if not isinstance(document, dict):
            continue
        subject = clean(document.get("document_id") or document.get("source_sha256") or f"doc-{index}")
        for field in ("document_id", "source_file", "source_sha256", "sha256"):
            value = clean(document.get(field))
            if value:
                aliases[value] = f"document:{subject}"
        invoice = document.get("invoice")
        if isinstance(invoice, dict):
            number = clean(invoice.get("invoice_number"))
            if number:
                aliases[number] = f"document:{subject}"
    for index, record in enumerate(task.get("expense_hint_reconciliation", []), 1):
        if not isinstance(record, dict):
            continue
        subject = clean(record.get("hint_id") or record.get("hint_ref") or f"hint-{index}")
        for field in ("hint_id", "hint_ref", "display_ref", "display_token", "question_id"):
            value = clean(record.get(field))
            if value:
                aliases[value] = f"hint:{subject}"
    return aliases


def _accounted_material_refs(
    task: dict[str, Any],
    known_units: dict[str, dict[str, Any]],
) -> set[str]:
    accounted = _resolved_hint_evidence_refs(task)
    linked_document_ids = {
        clean(unit.get(field))
        for unit in known_units.values()
        for field in (
            "source_document_id", "source_doc_id", "supporting_invoice_document_id",
            "supporting_schedule_document_id", "partner_approval_document_id",
            "approval_screenshot_document_id",
        )
        if clean(unit.get(field))
    }
    for document in task.get("evidence_index", []):
        if not isinstance(document, dict):
            continue
        document_id = clean(document.get("document_id"))
        if document_id not in linked_document_ids and not _truthy(document.get("excluded_by_user")):
            continue
        for field in ("document_id", "source_file", "source_sha256", "sha256"):
            value = clean(document.get(field))
            if value:
                accounted.add(value)
        invoice = document.get("invoice")
        if isinstance(invoice, dict):
            number = clean(invoice.get("invoice_number"))
            if number:
                accounted.add(number)
    return accounted


def _validate_finding_rule(
    *,
    role: str,
    finding_id: str,
    severity: str,
    code: str,
    unit_refs: list[str],
    evidence_refs: list[str],
    known_units: dict[str, dict[str, Any]],
    known_evidence: set[str],
    task: dict[str, Any],
) -> None:
    rules = finding_code_rules(role)
    if code not in rules:
        raise ProtocolError(
            f"finding {finding_id} code {code!r} is outside the {role} role; "
            f"allowed codes: {', '.join(sorted(rules))}"
        )
    rule = rules[code]
    if severity not in rule["severities"]:
        allowed = ", ".join(rule["severities"])
        raise ProtocolError(
            f"finding {finding_id} code {code!r} cannot use severity {severity!r}; "
            f"allowed: {allowed}"
        )
    minimum_units = int(rule.get("minimum_unit_refs", 0) or 0)
    minimum_evidence = int(rule.get("minimum_evidence_refs", 0) or 0)
    if len(unit_refs) < minimum_units:
        raise ProtocolError(
            f"finding {finding_id} code {code!r} requires at least {minimum_units} unit reference(s)"
        )
    if len(evidence_refs) < minimum_evidence:
        raise ProtocolError(
            f"finding {finding_id} code {code!r} requires at least {minimum_evidence} evidence reference(s)"
        )
    if rule.get("minimum_duplicate_subjects"):
        required = int(rule["minimum_duplicate_subjects"])
        evidence_aliases = _evidence_subject_aliases(task)
        evidence_subjects = {evidence_aliases.get(ref, ref) for ref in evidence_refs}
        if len(unit_refs) < required and len(evidence_subjects) < required:
            raise ProtocolError(
                f"finding {finding_id} duplicate_claim requires at least {required} distinct "
                "unit refs or evidence subjects; aliases of one document cannot duplicate itself"
            )

    units = [known_units[ref] for ref in unit_refs]
    if code == "claim_exceeds_evidence" and not any(
        _unit_claim_exceeds_evidence(unit) for unit in units
    ):
        raise ProtocolError(
            f"finding {finding_id} claim_exceeds_evidence is not supported by the cited numeric amounts"
        )
    if code == "amount_evidence_conflict" and units and all(
        _unit_is_only_lower_reimbursement(unit) for unit in units
    ):
        raise ProtocolError(
            f"finding {finding_id} treats a lower reimbursable amount as an evidence conflict; "
            "partial reimbursement is valid and Stage 3 records the invoice/claim difference"
        )
    if code == "claimed_evidence_missing" and units and not any(
        _unit_lacks_claim_evidence(unit, known_evidence) for unit in units
    ):
        raise ProtocolError(
            f"finding {finding_id} cites units whose own invoice/schedule evidence is present. "
            "An unclaimed company-booked flight/rail/hotel is contextual travel, not a required personal invoice"
        )
    if code == "missing_required_approval" and units and not any(
        _unit_lacks_required_approval(unit) for unit in units
    ):
        raise ProtocolError(
            f"finding {finding_id} does not cite a unit marked as requiring a missing approval"
        )
    if code == "substitute_invoice_noncompliance" and units and not any(
        _truthy(unit.get("is_substitute_invoice")) for unit in units
    ):
        raise ProtocolError(
            f"finding {finding_id} does not cite a unit marked as a substitute invoice"
        )
    if code == "unresolved_material":
        accounted_refs = _accounted_material_refs(task, known_units)
        if evidence_refs and all(ref in accounted_refs for ref in evidence_refs):
            raise ProtocolError(
                f"finding {finding_id} cites only material already allocated, excluded, resolved, or marked not reimbursed"
            )


def _validate_audit(
    candidate: dict[str, Any],
    task: dict[str, Any],
    known_units: dict[str, dict[str, Any]],
    known_evidence: set[str],
) -> None:
    allowed = {
        "schema_version", "audit_contract_version", "task_id",
        "source_task_fingerprint", "source_allocation_fingerprint",
        "source_extraction_fingerprint", "agent_id", "agent_display_name",
        "coverage", "summary", "outcome", "findings",
    }
    _validate_root(candidate, allowed)
    if candidate.get("schema_version") != AUDIT_SCHEMA:
        raise ProtocolError(f"audit schema_version must be {AUDIT_SCHEMA}")
    if clean(candidate.get("audit_contract_version")) != clean(task.get("contract_version")):
        raise ProtocolError("audit_contract_version does not match this role's contract")
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
        missing = set(FINDING_FIELDS) - set(item)
        unknown = set(item) - set(FINDING_FIELDS)
        if missing:
            raise ProtocolError(
                "review finding is missing required fields: " + ", ".join(sorted(missing))
                + ". A finding must contain exactly: " + ", ".join(FINDING_FIELDS)
            )
        if unknown:
            raise ProtocolError(
                f"review finding contains unknown fields: {', '.join(sorted(unknown))}. "
                "A finding may contain only: " + ", ".join(FINDING_FIELDS)
            )
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
            item.get("unit_refs", []), set(known_units), f"finding {finding_id} unit_refs"
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
        _validate_finding_rule(
            role=clean(task.get("role_id")),
            finding_id=finding_id,
            severity=severity,
            code=clean(item.get("code")),
            unit_refs=unit_refs,
            evidence_refs=evidence_refs,
            known_units=known_units,
            known_evidence=known_evidence,
            task=task,
        )
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
    known_units = _unit_tokens(allocation)
    known_evidence = _known_evidence_refs(task)
    _validate_audit(candidate, task, known_units, known_evidence)


def _archive_previous_audit(path: Path, history_dir: Path) -> None:
    if not path.is_file():
        return
    data = path.read_bytes()
    history_dir.mkdir(parents=True, exist_ok=True)
    archive = history_dir / f"audit-{sha256_bytes(data)}.json"
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
    # A degraded (over-cap) task never produced a real subagent handoff, so any result for
    # it was hand-assembled. Refuse it and keep the deterministic preflight authoritative.
    _enforce_handoff_cap(task)
    candidate = load_json(result_path)
    validate_result(candidate, task, allocation)
    accepted = dict(candidate)
    accepted["accepted_at"] = time_utils.iso_now(timespec="microseconds")
    accepted["accepted_by"] = "subagent_protocol.py"
    integrity.stamp(accepted, "subagent_protocol.py")
    paths = task_paths(process_dir, task)
    result_fingerprint = clean((accepted.get("integrity") or {}).get("fingerprint"))
    archive = paths["audit_archive_dir"] / f"{result_fingerprint}.json"
    write_immutable_json(archive, accepted)
    _archive_previous_audit(paths["audit_result"], paths["audit_history"])
    write_json(paths["audit_result"], accepted)
    paths["audit_archive"] = archive
    return accepted, paths


def _validate_accepted_audit(
    report: dict[str, Any],
    task: dict[str, Any],
    allocation: dict[str, Any],
) -> None:
    ok, reason = integrity.check(report)
    if not ok or clean((report.get("integrity") or {}).get("stamped_by")) != "subagent_protocol.py":
        raise ProtocolError(f"audit integrity failed: {reason}")
    if clean(report.get("accepted_by")) != "subagent_protocol.py" or not clean(report.get("accepted_at")):
        raise ProtocolError("audit is not an officially accepted result")
    try:
        datetime.fromisoformat(clean(report.get("accepted_at")))
    except ValueError as exc:
        raise ProtocolError("audit accepted_at is invalid") from exc
    candidate = dict(report)
    candidate.pop("integrity", None)
    candidate.pop("accepted_at", None)
    candidate.pop("accepted_by", None)
    validate_result(candidate, task, allocation)


def audit_state(
    role: str,
    process_dir: Path,
    allocation: dict[str, Any],
    allocation_path: Path | None = None,
    extraction_path: Path | None = None,
) -> dict[str, Any]:
    canonical_path = process_dir / f"{role}-audit.json"
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
            role,
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
    archive_paths = sorted(paths["audit_archive_dir"].glob("*.json"))
    candidate_paths = list(archive_paths)
    if canonical_path.is_file():
        candidate_paths.append(canonical_path)
    valid: dict[str, tuple[dict[str, Any], Path]] = {}
    errors: list[str] = []
    error_codes: list[str] = []
    for candidate_path in candidate_paths:
        try:
            report = load_json(candidate_path)
            _validate_accepted_audit(report, task, current_allocation)
        except ProtocolError as exc:
            errors.append(f"{candidate_path.name}: {exc}")
            error_codes.append(exc.code)
            continue
        fingerprint = clean((report.get("integrity") or {}).get("fingerprint"))
        valid[fingerprint] = (report, candidate_path)

    if not valid:
        archive_root = process_dir / "subagent-audit-generations" / role
        has_older_archive = archive_root.is_dir() and any(archive_root.glob("*/*.json"))
        if candidate_paths:
            stale = ProtocolError.STALE_TASK in error_codes
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


def review_record(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status", "missing"),
        "outcome": state.get("outcome", "not_run"),
        "result_fingerprint": state.get("result_fingerprint", ""),
        "blocking_count": int(state.get("blocking_count", 0) or 0),
        "advisory_count": int(state.get("advisory_count", 0) or 0),
        "summary": state.get("summary", ""),
    }


def chat_review_summary(report: dict[str, Any]) -> str:
    """Render one deterministic applicant-facing triage block.

    The coordinator may relay this block, but must not promote advisory findings
    into questions. Keeping the interaction rule in generated output helps hosts
    whose model does not reliably retain the prose workflow instruction.
    """
    findings = [item for item in report.get("findings", []) if isinstance(item, dict)]
    blocking = [item for item in findings if clean(item.get("severity")) == "blocking"]
    advisory = [item for item in findings if clean(item.get("severity")) == "advisory"]

    def finding_line(item: dict[str, Any]) -> str:
        refs = [
            *[clean(ref) for ref in item.get("unit_refs", []) if clean(ref)],
            *[clean(ref) for ref in item.get("evidence_refs", []) if clean(ref)],
        ]
        ref_text = f" ({', '.join(refs)})" if refs else ""
        return f"- [{clean(item.get('code'))}]{ref_text} {clean(item.get('message'))}"

    lines = ["SUBAGENT REVIEW SUMMARY TO SHOW IN CHAT"]
    if blocking:
        lines.append("需要处理（阻断；只就以下事项向申请人提问）：")
        lines.extend(finding_line(item) for item in blocking)
    else:
        lines.append("需要处理：无。不要向申请人提出审计问题，继续标准流程。")
    if advisory:
        lines.append("供参考（无需回复；默认保持当前处理，不得改写成待决问题）：")
        lines.extend(finding_line(item) for item in advisory)
    else:
        lines.append("供参考：无。")
    lines.append(
        "INTERACTION RULE: advisory 不是用户待办，不阻断、不追问、不自动修改；"
        "只有 blocking 才进入问答并通过 Composer/Updater 修正。"
    )
    return "\n".join(lines)


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

    inspect_audit = commands.add_parser("inspect-audit", help="Inspect the current Stage 3 audit sidecar for a role.")
    inspect_audit.add_argument("--role", choices=sorted(ROLE_SPECS), required=True)
    inspect_audit.add_argument("--allocation", required=True)
    inspect_audit.add_argument("--process-dir", default="process")
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
            print(
                f"Task packet: {paths['task']} "
                f"({task_packet_size_bytes(task):,} bytes; cap {MAX_HANDOFF_PACKET_BYTES:,})"
            )
            print(f"Result template: {paths['template']}")
            print(
                "SUBAGENT HANDOFF: start a fresh read-only subagent. Attach the compact task packet and "
                "result template as host resources when available; otherwise include their complete JSON contents "
                "in its initial message. Do not give filesystem paths or mutation tools, and never ask it to locate "
                "workspace files. Use response_json_schema when the host supports structured output. Save its exact "
                "JSON response outside process/, then run Chief accept-agent."
            )
            return ExitCode.SUCCESS
        if args.command == "accept":
            accepted, paths = accept_result(
                args.role,
                Path(args.allocation),
                Path(args.extraction),
                Path(args.process_dir),
                Path(args.result),
            )
            print(f"Accepted {accepted.get('agent_display_name')} result.")
            print(f"Audit result: {paths['audit_result']}")
            print(f"Outcome: {accepted.get('outcome')}")
            print(chat_review_summary(accepted))
            print("NEXT: run Chief status; a current blocking audit from either role prevents Stage 3.")
            return ExitCode.SUCCESS
        allocation = load_json(Path(args.allocation))
        _full_fingerprint(allocation, "allocation")
        print(json.dumps(audit_state(args.role, Path(args.process_dir), allocation), ensure_ascii=False, indent=2))
        return ExitCode.SUCCESS
    except HandoffTooLarge as degrade:
        # Over-cap is a designed fail-open, not a failure to fix. For prepare it degrades
        # cleanly (exit 0) so Chief keeps going straight to the deterministic Stage-3
        # preflight; for accept it is a refusal (a result for a degraded task was hand-made).
        if args.command == "prepare":
            print(f"DEGRADE: {degrade}")
            print(
                "NEXT: skip this subagent audit and run Chief write; the deterministic Stage-3 "
                "preflight is authoritative. Do not hand-assemble a packet or manually accept a result."
            )
            return ExitCode.SUCCESS
        print(f"REFUSED: {degrade}", file=sys.stderr)
        return ExitCode.COMMAND_ERROR
    except ProtocolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("NEXT: regenerate the task from the current allocation and return JSON matching its generated template.", file=sys.stderr)
        return ExitCode.COMMAND_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
