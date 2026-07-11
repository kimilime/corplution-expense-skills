#!/usr/bin/env python3
"""Compose canonical allocation answers from compact, UTF-8 decisions.

This is the only normal bridge from applicant/agent judgment to
allocation-answers.json. It resolves current user-facing item numbers, binds
the live allocation fingerprint, validates the decision schema, invokes the
official updater in dry-run mode, and publishes the answers file atomically.

It never generates a helper script and never mutates expense-allocation.json.
If composition fails, correct the same decisions input and rerun this tool.
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
import text_safety
from apply_allocation_answers import (
    ALLOWED_UNIT_FIELDS,
    COMPUTED_FIELDS_TEACHING,
    unit_no as current_unit_no,
)


DECISIONS_SCHEMA_VERSION = "allocation_decisions.v1"
DECISIONS_ROOT_FIELDS = {
    "schema_version",
    "decisions",
    "question_updates",
    "project_contexts",
    "confirm_units",
    "drop_units",
    "exclude_units",
}
DECISION_FIELDS = {"units", "set"}
QUESTION_UPDATE_FIELDS = {"question_id", "status", "answer"}
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


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def decisions_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "allocation-decisions-template.json"


def parse_unit_selector(selector: str) -> list[int]:
    units: list[int] = []
    for part in str(selector).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if hi_i < lo_i:
                raise ValueError(f"range {part!r} runs backwards")
            units.extend(range(lo_i, hi_i + 1))
        else:
            units.append(int(part))
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
    for token in shlex.split(body):
        if "=" not in token:
            raise ValueError(f"expected field=value, got {token!r} in --set {spec!r}")
        key, _, value = token.partition("=")
        fields[normalize_field(key)] = value
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    question_updates = require_object_list(data.get("question_updates"), "question_updates")
    context_updates = require_object_list(data.get("project_contexts"), "project_contexts")
    errors: list[str] = []
    known_questions = {
        str(item.get("question_id", "")).strip()
        for item in allocation.get("questions", [])
        if str(item.get("question_id", "")).strip()
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
    if errors:
        raise ValueError("; ".join(errors))
    return question_updates, context_updates


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
        if text in by_id:
            numbers.append(by_id[text])
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
    print(f"1. Correct {target} from the updater errors above.", file=sys.stderr)
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
        help="Repeatable compact spec with no whitespace values; use --decisions for complex text.",
    )
    parser.add_argument("--decisions", help="UTF-8 allocation_decisions.v1 JSON file")
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
        batches = [parse_set_spec(spec) for spec in args.specs]
        decision_data: dict[str, Any] = {}
        if decisions_path:
            decision_data = load_decisions_file(decisions_path)
            batches.extend(decision_batches(decision_data))
        question_updates, context_updates = validate_supplemental_actions(decision_data, allocation)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print_recovery(decisions_path)
        return 2

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
    }

    if not answers["unit_updates"] and not question_updates and not context_updates:
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
        return 2

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

    action_count = len(answers["unit_updates"]) + len(question_updates) + len(context_updates)
    print(f"Composed and updater-validated {action_count} action(s) -> {output_path}")
    print(
        "NEXT: python scripts/apply_allocation_answers.py "
        f"--allocation {allocation_path} --answers {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
