#!/usr/bin/env python3
"""Compose canonical allocation answers from compact, UTF-8 decisions.

This is the only normal bridge from applicant/agent judgment to
allocation-answers.json. It resolves current user-facing item and record numbers, binds
the live allocation fingerprint, validates the decision schema, invokes the
official updater in dry-run mode, and publishes the answers file atomically.

It never generates a helper script and never mutates expense-allocation.json.
For same-generation schema/value errors, correct the decisions input and rerun.
For generation/ref mismatches, re-read the current review and rebuild the stale entry.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import integrity
from exit_codes import ExitCode
from io_utils import configure_utf8_stdio as configure_stdio
import text_safety
import allocation_generations
from apply_allocation_answers import (
    ALLOWED_UNIT_FIELDS,
    COMPUTED_FIELDS_TEACHING,
    unit_no as current_unit_no,
)


DECISIONS_SCHEMA_VERSION = "allocation_decisions.v1"
DECISIONS_ROOT_FIELDS = {
    "schema_version",
    "for_allocation_fingerprint",
    "decisions",
    "expense_hint_resolutions",
    "question_updates",
    "project_contexts",
    "confirm_units",
    "drop_units",
    "exclude_units",
    "rebase_metadata",
    "removed_evidence",
    "integrity",
}
DECISION_FIELDS = {"units", "set"}
QUESTION_UPDATE_FIELDS = {"question_id", "status", "answer"}
HINT_RESOLUTION_FIELDS = {"question_id", "record_ref", "hint_id", "action", "units", "note"}
HINT_RESOLUTION_ACTIONS = {
    "matched_existing",
    "covered_by_invoice",
    "not_reimbursed",
    "pending_invoice",
}
CONTEXT_UPDATE_FIELDS = {
    "context_id",
    "date_start",
    "date_end",
    "city",
    "client_name",
    "client_charge_code",
    "project_description",
    "user_notes",
    "project_scope",
    "travel_buffer_days",
    "status",
    "meal_hints",
    "expense_hints",
}
NEW_CONTEXT_REQUIRED_FIELDS = {
    "date_start",
    "date_end",
    "city",
    "client_name",
    "client_charge_code",
}
ACTION_STATUSES = {
    "confirm_units": "confirmed",
    "drop_units": "dropped",
    "exclude_units": "excluded",
}

FIELD_ALIASES = {
    "client": "client_name",
    "code": "client_charge_code",
    "charge_code": "client_charge_code",
    "note": "final_note",
    "date": "expense_date",
    "category": "source_category",
    "context": "meal_context",
    "project_context": "project_context_id",
    "nights": "hotel_nights",
    "checkin": "check_in_date",
    "check_in": "check_in_date",
    "checkout": "check_out_date",
    "check_out": "check_out_date",
    "attendee": "attendees",
    "origin_type": "origin_place_type",
    "destination_type": "destination_place_type",
    "reimbursable": "reimbursable_amount",
}


def decisions_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "allocation-decisions-template.json"


# Generation-safe references: every user-facing unit selector, including
# --set, must be N@ref copied from the current review list. Display numbers
# and internal UNIT ids are generation-local; there is no same-session bypass.
# REF_CONTEXT carries current refs so every selector path verifies identity.
REF_CONTEXT: dict[str, Any] = {"refs_by_number": {}, "require_refs": False}


def split_ref_token(text: str) -> tuple[str, str | None]:
    if "@" in text:
        base, _, ref = text.partition("@")
        return base.strip(), ref.strip().lower()
    return text, None


def verify_ref(number: int, ref: str | None, field: str) -> None:
    refs = REF_CONTEXT["refs_by_number"]
    if ref is None:
        if REF_CONTEXT["require_refs"]:
            raise ValueError(
                f"{field}: unit selectors must use N@ref (e.g. 3@a1b2c3d4). "
                "Copy the [token] shown at the start of each line of the CURRENT Applicant "
                "Review List. Bare numbers and ranges are not accepted."
            )
        return
    if ref != refs.get(number, ""):
        raise ValueError(
            f"{field}: evidence ref for item {number} does not match the current allocation. "
            "Either this decisions file was written against another generation, or the "
            "evidence for this item changed (extraction corrections, re-parse). Do NOT edit "
            "the ref to make it pass - re-read the CURRENT Applicant Review List, verify "
            "which concrete expense this item now is, and rebuild the entry from what you "
            "see there."
        )


def parse_unit_selector(selector: str) -> list[int]:
    units: list[int] = []
    for part in str(selector).split(","):
        part = part.strip()
        if not part:
            continue
        part, ref = split_ref_token(part)
        if "-" in part:
            if ref is not None:
                raise ValueError(f"range {part!r} cannot carry an @ref; qualify each number individually")
            if REF_CONTEXT["require_refs"]:
                raise ValueError(
                    f"range {part!r}: decisions files must qualify each item as N@ref individually "
                    "(a range cannot prove which concrete expenses it refers to across generations)"
                )
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if hi_i < lo_i:
                raise ValueError(f"range {part!r} runs backwards")
            units.extend(range(lo_i, hi_i + 1))
        else:
            number = int(part)
            verify_ref(number, ref, "unit selector")
            units.append(number)
    if not units:
        raise ValueError(f"unit selector {selector!r} selects nothing")
    return units


def normalize_field(name: str) -> str:
    value = name.strip()
    return FIELD_ALIASES.get(value, value)


def parse_set_spec(spec: str) -> tuple[list[int], dict[str, Any]]:
    if ":" not in spec:
        raise ValueError(f"--set needs 'UNITS: field=value ...', got {spec!r}")
    selector, _, body = spec.partition(":")
    units = parse_unit_selector(selector)
    fields: dict[str, Any] = {}
    current_field = ""
    for token in shlex.split(body):
        if "=" in token:
            key, _, value = token.partition("=")
            current_field = normalize_field(key)
            if not current_field:
                raise ValueError(f"empty field name in --set {spec!r}")
            fields[current_field] = value
        elif current_field:
            fields[current_field] = (fields[current_field] + " " + token).strip()
        else:
            raise ValueError(f"expected field=value, got {token!r} in --set {spec!r}")
    if not fields:
        raise ValueError(f"--set {spec!r} sets no fields")
    return units, fields


def require_object_list(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be an array of objects")
    return [dict(item) for item in value]


def load_decisions_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read decisions JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("decisions JSON root must be an object")
    unknown = sorted(set(data) - DECISIONS_ROOT_FIELDS)
    if unknown:
        raise ValueError(
            "unsupported decisions root field(s): " + ", ".join(unknown)
            + "; use the bundled allocation-decisions template exactly"
        )
    if data.get("schema_version") != DECISIONS_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {DECISIONS_SCHEMA_VERSION!r}")
    return data


def validated_removed_evidence(
    value: Any,
    allocation: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(
            "official rebase decisions must contain a removed_evidence array; rerun Chief rebase"
        )
    current_by_identity = {
        str(unit.get("unit_identity_sha256", "")).strip().lower(): unit
        for unit in allocation.get("allocation_units", []) if isinstance(unit, dict)
    }
    current_by_token = {
        f"{unit.get('user_no') or unit.get('unit_no')}@{str(unit.get('unit_ref', '')).strip().lower()}": unit
        for unit in current_by_identity.values()
    }
    seen_removals: set[str] = set()
    seen_identities: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value, start=1):
        entry = dict(raw)
        removal_ref = str(entry.get("removal_ref", "")).strip().lower()
        identity = str(entry.get("unit_identity_sha256", "")).strip().lower()
        status = str(entry.get("resolution_status", "")).strip()
        action = str(entry.get("resolution_action", "")).strip()
        requires_confirmation = entry.get("requires_confirmation") is True
        if not removal_ref or not identity:
            raise ValueError(f"removed_evidence #{index} lacks its immutable reference or identity")
        if removal_ref in seen_removals or identity in seen_identities:
            raise ValueError(f"removed_evidence #{index} repeats an earlier removed item")
        seen_removals.add(removal_ref)
        seen_identities.add(identity)
        if identity in current_by_identity:
            raise ValueError(
                f"{entry.get('removal_ref')} is present in the current allocation and cannot be recorded as removed"
            )
        if status != "resolved":
            raise ValueError(
                f"{entry.get('removal_ref')} is {status or 'unresolved'}; ask the applicant to confirm "
                "the removal or restore the evidence, then rerun Chief rebase with --resolutions"
            )
        if requires_confirmation:
            if action not in {"intentional_removal", "replacement_provided"}:
                raise ValueError(f"{entry.get('removal_ref')} lacks an explicit applicant removal decision")
            if not str(entry.get("resolution_note", "")).strip():
                raise ValueError(f"{entry.get('removal_ref')} lacks its confirmation note")
        elif action != "prior_closed_item_removed":
            raise ValueError(f"{entry.get('removal_ref')} has an invalid automatic removal action")
        replacement_refs = entry.get("replacement_unit_refs", [])
        replacement_ids = entry.get("replacement_unit_ids", [])
        replacement_identities = entry.get("replacement_unit_identities", [])
        if any(not isinstance(items, list) for items in (replacement_refs, replacement_ids, replacement_identities)):
            raise ValueError(f"{entry.get('removal_ref')} replacement fields must be arrays")
        if action == "replacement_provided":
            if not replacement_refs or not (
                len(replacement_refs) == len(replacement_ids) == len(replacement_identities)
            ):
                raise ValueError(f"{entry.get('removal_ref')} has incomplete replacement bindings")
            for position, token in enumerate(replacement_refs):
                unit = current_by_token.get(str(token).strip().lower())
                if unit is None:
                    raise ValueError(f"{entry.get('removal_ref')} references stale replacement {token!r}")
                if (
                    str(unit.get("unit_id", "")) != str(replacement_ids[position])
                    or str(unit.get("unit_identity_sha256", "")).strip().lower()
                    != str(replacement_identities[position]).strip().lower()
                    or str(unit.get("status", "")).strip() in {"dropped", "excluded", "non_reimbursable"}
                ):
                    raise ValueError(f"{entry.get('removal_ref')} replacement binding is stale or inactive")
        elif replacement_refs or replacement_ids or replacement_identities:
            raise ValueError(f"{entry.get('removal_ref')} unexpectedly contains replacement bindings")
        normalized.append(entry)

    declared_count = metadata.get("removed_evidence_count")
    if not isinstance(declared_count, int) or declared_count != len(normalized):
        raise ValueError("rebase removed-evidence count does not match its metadata; rerun Chief rebase")
    if metadata.get("removed_evidence_open_count") != 0 or metadata.get("removed_evidence_pending_restore_count") != 0:
        raise ValueError("rebase metadata still reports unresolved removed evidence")
    return normalized


def decision_batches(data: dict[str, Any]) -> list[tuple[list[int], dict[str, Any]]]:
    items = require_object_list(data.get("decisions"), "decisions")
    out: list[tuple[list[int], dict[str, Any]]] = []
    for idx, item in enumerate(items, start=1):
        unknown = sorted(set(item) - DECISION_FIELDS)
        if unknown:
            raise ValueError(f"decision #{idx} unsupported field(s): {', '.join(unknown)}")
        raw_units = item.get("units")
        if isinstance(raw_units, list):
            units: list[int] = []
            for selector in raw_units:
                units.extend(parse_unit_selector(str(selector)))
        else:
            units = parse_unit_selector(str(raw_units))
        raw_fields = item.get("set")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise ValueError(f"decision #{idx} set must be a non-empty object")
        fields = {normalize_field(str(k)): v for k, v in raw_fields.items()}
        out.append((units, fields))
    return out


def validate_fields(fields: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name in fields:
        if name in COMPUTED_FIELDS_TEACHING:
            errors.append(COMPUTED_FIELDS_TEACHING[name])
        elif name not in ALLOWED_UNIT_FIELDS:
            errors.append(
                f"unknown field {name!r}; allowed fields: {', '.join(sorted(ALLOWED_UNIT_FIELDS))} "
                f"(aliases: {', '.join(f'{a}->{b}' for a, b in sorted(FIELD_ALIASES.items()))})"
            )
    return errors


def validate_supplemental_actions(
    data: dict[str, Any],
    allocation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    question_updates = require_object_list(data.get("question_updates"), "question_updates")
    context_updates = require_object_list(data.get("project_contexts"), "project_contexts")
    hint_resolutions = require_object_list(
        data.get("expense_hint_resolutions"), "expense_hint_resolutions"
    )
    errors: list[str] = []
    known_questions = {
        str(item.get("question_id", "")).strip()
        for item in allocation.get("questions", [])
        if str(item.get("question_id", "")).strip()
    }
    questions_by_id = {
        str(item.get("question_id", "")).strip(): item
        for item in allocation.get("questions", [])
        if isinstance(item, dict) and str(item.get("question_id", "")).strip()
    }
    known_contexts = {
        str(item.get("context_id", "")).strip(): item
        for item in allocation.get("project_contexts", [])
        if isinstance(item, dict) and str(item.get("context_id", "")).strip()
    }
    for idx, item in enumerate(question_updates, start=1):
        unknown = sorted(set(item) - QUESTION_UPDATE_FIELDS)
        if unknown:
            errors.append(f"question_updates[{idx - 1}] unsupported field(s): {', '.join(unknown)}")
        question_id = str(item.get("question_id", "")).strip()
        if not question_id:
            errors.append(f"question_updates[{idx - 1}] requires question_id")
        elif question_id not in known_questions:
            errors.append(f"question_updates[{idx - 1}] references unknown question_id {question_id!r}")
        elif (
            questions_by_id[question_id].get("question_type") == "expense_hint_reconciliation"
            and str(item.get("status", "answered")).strip() not in {"open", "needs_confirmation", "draft"}
        ):
            errors.append(
                f"question_updates[{idx - 1}] cannot close expense-record question {question_id!r}; "
                "use expense_hint_resolutions so pending invoices remain blocking"
            )
    for idx, item in enumerate(context_updates, start=1):
        unknown = sorted(set(item) - CONTEXT_UPDATE_FIELDS)
        if unknown:
            errors.append(f"project_contexts[{idx - 1}] unsupported field(s): {', '.join(unknown)}")
        context_id = str(item.get("context_id", "")).strip()
        if not context_id or context_id not in known_contexts:
            missing = sorted(
                field for field in NEW_CONTEXT_REQUIRED_FIELDS
                if not str(item.get(field, "")).strip()
            )
            if missing:
                errors.append(
                    f"project_contexts[{idx - 1}] creates a new context but is missing: "
                    + ", ".join(missing)
                )
        candidate = {**known_contexts.get(context_id, {}), **item}
        parsed_dates: dict[str, date] = {}
        for field in ("date_start", "date_end"):
            value = str(candidate.get(field, "")).strip()
            if not value:
                continue
            try:
                parsed_dates[field] = date.fromisoformat(value)
            except ValueError:
                errors.append(f"project_contexts[{idx - 1}].{field} must be YYYY-MM-DD")
        if (
            "date_start" in parsed_dates
            and "date_end" in parsed_dates
            and parsed_dates["date_end"] < parsed_dates["date_start"]
        ):
            errors.append(f"project_contexts[{idx - 1}].date_end cannot be earlier than date_start")
    records = [
        item for item in allocation.get("expense_hint_reconciliation", [])
        if isinstance(item, dict)
    ]
    by_hint_id = {
        str(item.get("hint_id", "")).strip(): item
        for item in records if str(item.get("hint_id", "")).strip()
    }
    by_question_ref = {
        (
            str(item.get("question_id", "")).strip(),
            str(item.get("display_ref", "")).strip().upper(),
        ): item
        for item in records
        if str(item.get("question_id", "")).strip() and str(item.get("display_ref", "")).strip()
    }
    seen_hints: set[str] = set()
    normalized_hint_resolutions: list[dict[str, Any]] = []
    known_units, known_ids = current_unit_maps(allocation)
    for idx, item in enumerate(hint_resolutions, start=1):
        unknown = sorted(set(item) - HINT_RESOLUTION_FIELDS)
        if unknown:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] unsupported field(s): {', '.join(unknown)}"
            )
        question_id = str(item.get("question_id", "")).strip()
        raw_record_ref = str(item.get("record_ref", "")).strip()
        record_base, record_evidence_ref = split_ref_token(raw_record_ref)
        record_ref = record_base.upper()
        hint_id = str(item.get("hint_id", "")).strip()
        if not question_id or not record_ref or not record_evidence_ref:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] must identify the current record with "
                "question_id plus the full R@ref token copied from the CURRENT question "
                "(for example R1@a1b2c3d4); bare R numbers and hint_id-only lookups are refused"
            )
            continue
        record = by_question_ref.get((question_id, record_ref))
        if record is None:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] references unknown current record "
                f"{question_id}/{record_ref}"
            )
            continue
        current_hint_ref = str(record.get("hint_ref", "")).strip().lower()
        if record_evidence_ref.lower() != current_hint_ref:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}]: record ref for {record_ref} does not "
                "match the current applicant record. The R number shifted or the record content "
                "changed. Do not edit the ref to make it pass; re-read the CURRENT R@ref line "
                "and rebuild this resolution from the applicant's actual answer."
            )
            continue
        if hint_id and hint_id != str(record.get("hint_id", "")).strip():
            errors.append(
                f"expense_hint_resolutions[{idx - 1}].hint_id does not match {raw_record_ref!r}"
            )
            continue
        canonical_hint_id = str(record.get("hint_id", "")).strip()
        if canonical_hint_id in seen_hints:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] duplicates record {canonical_hint_id!r}"
            )
            continue
        seen_hints.add(canonical_hint_id)
        action = str(item.get("action", "")).strip()
        if action not in HINT_RESOLUTION_ACTIONS:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}].action must be one of: "
                + ", ".join(sorted(HINT_RESOLUTION_ACTIONS))
            )
            continue
        try:
            numbers = action_numbers(item.get("units"), known_units, known_ids, "expense_hint_resolutions.units")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if action in {"matched_existing", "covered_by_invoice"} and not numbers:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] action {action!r} requires current item number(s) in units"
            )
        if action in {"not_reimbursed", "pending_invoice"} and numbers:
            errors.append(
                f"expense_hint_resolutions[{idx - 1}] action {action!r} must not reference units"
            )
        normalized_hint_resolutions.append({
            "question_id": str(record.get("question_id", "")).strip(),
            "record_ref": str(record.get("display_ref", "")).strip(),
            "hint_id": canonical_hint_id,
            "action": action,
            "unit_ids": [known_units[number] for number in numbers],
            "note": str(item.get("note", "")).strip(),
        })
    if errors:
        raise ValueError("; ".join(errors))
    return question_updates, context_updates, normalized_hint_resolutions


def current_unit_maps(allocation: dict[str, Any]) -> tuple[dict[int, str], dict[str, int]]:
    by_number: dict[int, str] = {}
    by_id: dict[str, int] = {}
    errors: list[str] = []
    for unit in allocation.get("allocation_units", []):
        unit_id = str(unit.get("unit_id", "")).strip()
        shown = current_unit_no(unit)
        if not unit_id:
            errors.append("allocation contains a unit without unit_id")
            continue
        if not shown.isdigit():
            errors.append(f"{unit_id} has non-numeric user-facing item number {shown!r}")
            continue
        number = int(shown)
        if number in by_number:
            errors.append(f"duplicate user-facing item number {number}")
            continue
        by_number[number] = unit_id
        by_id[unit_id] = number
    if errors:
        raise ValueError("; ".join(errors))
    return by_number, by_id


def action_numbers(value: Any, by_number: dict[int, str], by_id: dict[str, int], field: str) -> list[int]:
    if value is None:
        return []
    selectors = value if isinstance(value, list) else [value]
    numbers: list[int] = []
    for selector in selectors:
        text = str(selector).strip()
        base, ref = split_ref_token(text)
        if base in by_id:
            number = by_id[base]
            verify_ref(number, ref, field)
            numbers.append(number)
            continue
        try:
            numbers.extend(parse_unit_selector(text))
        except ValueError as exc:
            raise ValueError(f"{field} has invalid unit reference {selector!r}: {exc}") from exc
    missing = sorted({number for number in numbers if number not in by_number})
    if missing:
        raise ValueError(f"{field} references missing current item number(s): {', '.join(map(str, missing))}")
    return numbers


def print_recovery(decisions_path: Path | None) -> None:
    target = str(decisions_path) if decisions_path else "the --set input"
    print("", file=sys.stderr)
    print("RECOVERY (stay on the canonical path):", file=sys.stderr)
    print(
        f"1. Follow the specific error above. Correct {target} only for same-generation "
        "schema/value errors; for stale generation or ref mismatch, rebuild the entry from "
        "the CURRENT review/question token.",
        file=sys.stderr,
    )
    print(f"2. Follow the UTF-8 structure in {decisions_template_path()}.", file=sys.stderr)
    print("3. Rerun Composer, then run the official updater after Composer succeeds.", file=sys.stderr)
    print(
        "Do not generate/fill an allocation-answers template, create fill_answers.py or patch scripts, "
        "import a launcher, or edit any process JSON directly.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Compose allocation-answers.json from canonical decisions.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="specs",
        help="Repeatable compact spec using N@ref selectors; use --decisions for complex text.",
    )
    decision_source = parser.add_mutually_exclusive_group()
    decision_source.add_argument("--decisions", help="UTF-8 allocation_decisions.v1 JSON file")
    parser.add_argument(
        "--output",
        default="process/allocation-answers.json",
        help="Where to atomically publish the updater-validated answers file",
    )
    args = parser.parse_args(argv)

    allocation_path = Path(args.allocation)
    allocation = json.loads(allocation_path.read_text(encoding="utf-8-sig"))
    integrity.require_valid(allocation, allocation_path)
    fingerprint = allocation.get("integrity", {}).get("fingerprint", "")
    decisions_path = Path(args.decisions) if args.decisions else None

    try:
        known_units, known_ids = current_unit_maps(allocation)
        refs_by_number: dict[int, str] = {}
        numbers_by_ref: dict[str, int] = {}
        for unit in allocation.get("allocation_units", []):
            raw_number = str(unit.get("user_no") or unit.get("unit_no") or "")
            ref = str(unit.get("unit_ref", "")).strip().lower()
            identity = str(unit.get("unit_identity_sha256", "")).strip().lower()
            if not raw_number.isdigit() or not ref or not identity:
                raise ValueError(
                    "current allocation contains an item without a display number, short ref, or "
                    "full evidence identity; regenerate Stage 2 before composing decisions"
                )
            number = int(raw_number)
            if number in refs_by_number:
                raise ValueError(f"current allocation repeats display item number {number}")
            if ref in numbers_by_ref:
                raise ValueError(
                    f"current allocation repeats evidence ref {ref} on items "
                    f"{numbers_by_ref[ref]} and {number}; regenerate Stage 2"
                )
            refs_by_number[number] = ref
            numbers_by_ref[ref] = number
        REF_CONTEXT["refs_by_number"] = refs_by_number
        REF_CONTEXT["require_refs"] = True
        batches = [parse_set_spec(spec) for spec in args.specs]
        decision_data: dict[str, Any] = {}
        if decisions_path:
            decision_data = load_decisions_file(decisions_path)
            if decision_data.get("proposal_review"):
                raise ValueError(
                    "proposal_review metadata is no longer supported; the promoted-proposal flow was "
                    "retired. Provide plain allocation_decisions.v1 --decisions input."
                )
            declared = str(decision_data.get("for_allocation_fingerprint", "")).strip().lower()
            current = str(allocation.get("integrity", {}).get("fingerprint", "")).lower()
            if not declared:
                raise ValueError(
                    "decisions file is missing for_allocation_fingerprint. Set it to the "
                    "'Allocation generation' code printed by the allocate run (also shown in "
                    "the review list header), so this file is bound to its generation."
                )
            generation_matches = len(declared) >= 8 and current.startswith(declared)
            if not generation_matches:
                raise ValueError(
                    "this decisions file belongs to an OLD allocation generation; every display "
                    "number and R-reference in it is now meaningless. Do NOT just swap the "
                    "fingerprint value - per-item @refs would still refuse. Create a NEW "
                    "decisions file: re-read the CURRENT Applicant Review List, re-verify every "
                    "item by source file, amount, date and route, and rebind confirmed facts to "
                    "the new N@ref tokens. If invoices were added or removed, run "
                    "rebase_allocation_decisions.py first ONLY when effective project contexts "
                    "and policy are unchanged; otherwise review the regenerated allocation from scratch."
                )
            batches.extend(decision_batches(decision_data))
        question_updates, context_updates, hint_resolutions = validate_supplemental_actions(
            decision_data, allocation
        )
        rebase_metadata = decision_data.get("rebase_metadata")
        removed_evidence: list[dict[str, Any]] = []
        lineage_rebase: dict[str, Any] | None = None
        if "removed_evidence" in decision_data and not rebase_metadata:
            raise ValueError(
                "removed_evidence is reserved for an officially stamped Chief rebase packet"
            )
        if rebase_metadata:
            rebase_ok, rebase_reason = integrity.check(decision_data)
            if not rebase_ok or str(decision_data.get("integrity", {}).get("stamped_by", "")) != "rebase_allocation_decisions.py":
                raise ValueError(
                    "rebase decisions are missing or fail their official integrity stamp "
                    f"({rebase_reason}); rerun Chief rebase instead of editing the file"
                )
            if not isinstance(rebase_metadata, dict):
                raise ValueError("rebase_metadata must be an object")
            removed_evidence = validated_removed_evidence(
                decision_data.get("removed_evidence"), allocation, rebase_metadata
            )
        if not allocation.get("change_log"):
            source_path, source_alloc, lineage_reason = allocation_generations.discover_rebase_source(
                allocation_path, allocation
            )
            if allocation_generations.is_lineage_integrity_error(lineage_reason):
                raise ValueError(
                    f"{lineage_reason}. Do not continue on a broken generation chain; recover the "
                    "missing stamped archive or perform the documented clean sibling-batch rebuild"
                )
            if source_path is not None and source_alloc is not None:
                if not isinstance(rebase_metadata, dict):
                    raise ValueError(
                        "this fresh allocation has a prior same-basis generation containing official "
                        "user decisions. Ordinary decisions are blocked until Chief runs rebase and "
                        "Composer compiles process/rebase-decisions.json"
                    )
                expected_source = str(source_alloc.get("integrity", {}).get("fingerprint", ""))
                declared_source = str(rebase_metadata.get("source_allocation_fingerprint", ""))
                declared_target = str(rebase_metadata.get("target_allocation_fingerprint", ""))
                if declared_source != expected_source or declared_target != fingerprint:
                    raise ValueError(
                        "rebase metadata does not bind the lineage source selected by Chief and the "
                        "current allocation generation; rerun Chief rebase instead of editing metadata"
                    )
                lineage_rebase = {
                    "source_allocation_file": str(source_path),
                    "source_allocation_fingerprint": expected_source,
                    "target_allocation_fingerprint": fingerprint,
                    "removed_evidence": removed_evidence,
                }
            elif rebase_metadata:
                raise ValueError(
                    "rebase metadata was supplied but the current allocation has no eligible lineage "
                    "source; review the current generation normally"
                )
        elif rebase_metadata:
            raise ValueError("this allocation generation already has applied decisions; stale rebase metadata is refused")
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print_recovery(decisions_path)
        return ExitCode.COMMAND_ERROR

    merged: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for units, fields in batches:
        errors.extend(validate_fields(fields))
        for number in units:
            if number not in known_units:
                valid = (
                    f"{min(known_units)}-{max(known_units)}" if known_units else "<allocation has no units>"
                )
                errors.append(
                    f"item {number} does not exist in the current allocation (valid displayed range: {valid})"
                )
                continue
            merged.setdefault(number, {}).update(fields)

    for action, status in ACTION_STATUSES.items():
        try:
            numbers = action_numbers(decision_data.get(action), known_units, known_ids, action)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for number in numbers:
            current_status = str(merged.get(number, {}).get("status", "")).strip()
            if current_status and current_status != status:
                errors.append(
                    f"item {number} has conflicting statuses {current_status!r} and {status!r}"
                )
                continue
            merged.setdefault(number, {})["status"] = status

    answers = {
        "schema_version": "allocation_answers.v1",
        "source_allocation_file": str(allocation_path),
        "source_allocation_fingerprint": fingerprint,
        "unit_updates": [
            {"unit_id": known_units[number], **fields}
            for number, fields in sorted(merged.items())
        ],
        "question_updates": question_updates,
        "project_contexts": context_updates,
        "expense_hint_resolutions": hint_resolutions,
    }
    if lineage_rebase:
        answers["lineage_rebase"] = lineage_rebase

    if not answers["unit_updates"] and not question_updates and not context_updates and not hint_resolutions and not lineage_rebase:
        errors.append("decisions input contains no actionable updates")
    for finding in text_safety.find_suspect_text(answers, path="decisions"):
        errors.append(
            f"encoding damage in decision values: {finding}. Recreate the affected value from trusted "
            "UTF-8 source text; do not pass Chinese through a PowerShell inline command or console pipeline."
        )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"\nNothing written ({len(errors)} problem(s) above).", file=sys.stderr)
        print_recovery(decisions_path)
        return ExitCode.COMMAND_ERROR

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = output_path.parent / f".{output_path.name}.compose-{uuid4().hex}.tmp"
    try:
        staging_path.write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dry = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).resolve().parent / "apply_allocation_answers.py"),
                "--allocation",
                str(allocation_path),
                "--answers",
                str(staging_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        sys.stdout.write(dry.stdout)
        sys.stderr.write(dry.stderr)
        if dry.returncode != 0:
            print(
                "\nComposer reached the updater, but its dry-run failed. No answers file was published "
                "and nothing was applied.",
                file=sys.stderr,
            )
            print_recovery(decisions_path)
            return dry.returncode
        staging_path.replace(output_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()

    action_count = (
        len(answers["unit_updates"]) + len(question_updates) + len(context_updates)
        + len(hint_resolutions)
    )
    print(f"Composed and updater-validated {action_count} action(s) -> {output_path}")
    print(
        "NEXT: python scripts/apply_allocation_answers.py "
        f"--allocation {allocation_path} --answers {output_path}"
    )
    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
