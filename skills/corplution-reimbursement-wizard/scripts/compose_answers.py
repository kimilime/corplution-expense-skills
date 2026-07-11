#!/usr/bin/env python3
"""Compose allocation-answers.json from compact batch decisions.

This is the bundled replacement for ad hoc "fill_answers" helper scripts:
agents kept rewriting the same tool every run and re-hitting the same traps
(encoding damage, guessed schemas, stale fingerprints, skill-dir pollution).
This tool handles every ceremonial part; the JUDGMENT — which expense belongs
to which project — still comes from the user/agent as plain decisions.

It is a front-end for the updater, not a bypass: output goes through
apply_allocation_answers.py --dry-run automatically, so every guard
(category flips, meal signals, note consistency, mojibake) still fires.

Usage — compact command-line specs (repeatable), unit numbers with ranges.
Use this form only for values without whitespace or shell-sensitive characters:
  python scripts/compose_answers.py \
      --allocation process/expense-allocation.json \
      --set "3,5,7-12: status=confirmed client=山西信托 city=太原 note=出差餐费" \
      --output process/allocation-answers.json

Usage — UTF-8 decisions file (use for any value containing spaces, quotes,
paths, long notes, or when terminal encoding is uncertain; also suitable for
very large batches):
  python scripts/compose_answers.py --allocation ... --decisions d.json
  # d.json: {"decisions": [{"units": "3,5-7", "set": {"status": "confirmed", ...}}]}

Field aliases accepted: client -> client_name, code/charge_code ->
client_charge_code, note -> final_note, date -> expense_date,
category -> source_category, context -> meal_context.

On success it prints the updater's dry-run result and the NEXT command to
apply for real. Nothing here writes any process JSON.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import integrity
import text_safety
from apply_allocation_answers import ALLOWED_UNIT_FIELDS, COMPUTED_FIELDS_TEACHING

FIELD_ALIASES = {
    "client": "client_name",
    "code": "client_charge_code",
    "charge_code": "client_charge_code",
    "note": "final_note",
    "date": "expense_date",
    "category": "source_category",
    "context": "meal_context",
}


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
    return FIELD_ALIASES.get(name.strip(), name.strip())


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


def load_decisions_file(path: Path) -> list[tuple[list[int], dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out: list[tuple[list[int], dict[str, Any]]] = []
    for idx, item in enumerate(data.get("decisions", []), start=1):
        raw_units = item.get("units")
        if isinstance(raw_units, list):
            units = [int(u) for u in raw_units]
        else:
            units = parse_unit_selector(str(raw_units))
        fields = {normalize_field(k): v for k, v in (item.get("set") or {}).items()}
        if not fields:
            raise ValueError(f"decision #{idx} sets no fields")
        out.append((units, fields))
    if not out:
        raise ValueError("decisions file contains no decisions")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compose allocation-answers.json from batch decisions.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json")
    parser.add_argument("--set", action="append", default=[], dest="specs",
                        help="Repeatable compact spec with no whitespace values; use --decisions for complex text.")
    parser.add_argument("--decisions", help="UTF-8 JSON decisions file (required for whitespace/complex text)")
    parser.add_argument("--output", default="process/allocation-answers.json",
                        help="Where to atomically publish the dry-run-validated answers file")
    args = parser.parse_args(argv)

    allocation_path = Path(args.allocation)
    allocation = json.loads(allocation_path.read_text(encoding="utf-8-sig"))
    integrity.require_valid(allocation, allocation_path)
    fingerprint = allocation.get("integrity", {}).get("fingerprint", "")

    known_units = {u.get("unit_no"): u.get("unit_id") for u in allocation.get("allocation_units", [])}

    decisions: list[tuple[list[int], dict[str, Any]]] = []
    try:
        for spec in args.specs:
            decisions.append(parse_set_spec(spec))
        if args.decisions:
            decisions.extend(load_decisions_file(Path(args.decisions)))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not decisions:
        print("ERROR: nothing to compose — pass --set specs and/or --decisions.", file=sys.stderr)
        return 2

    merged: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for units, fields in decisions:
        errors.extend(validate_fields(fields))
        for no in units:
            if no not in known_units:
                errors.append(
                    f"unit {no} does not exist in the CURRENT allocation "
                    f"(valid unit numbers: {min(known_units)}–{max(known_units)}). "
                    "If allocation was re-run, unit numbers may have shifted — check the review list."
                    if known_units else f"unit {no} does not exist (allocation has no units)"
                )
                continue
            merged.setdefault(no, {})
            merged[no].update(fields)

    mojibake = text_safety.find_suspect_text({str(k): v for k, v in merged.items()})
    for finding in mojibake:
        errors.append(
            f"encoding damage in decision values: {finding} — your Chinese text was likely mangled "
            "by a console pipeline. Put decisions in a UTF-8 --decisions file instead of inline args."
        )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        print(f"\nNothing written ({len(errors)} problem(s) above).", file=sys.stderr)
        return 2

    answers = {
        "schema_version": "allocation_answers.v1",
        "source_allocation_file": str(allocation_path),
        "source_allocation_fingerprint": fingerprint,
        "unit_updates": [
            {"unit_id": known_units[no], **fields} for no, fields in sorted(merged.items())
        ],
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = output_path.parent / f".{output_path.name}.compose-{uuid4().hex}.tmp"
    try:
        staging_path.write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dry = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "apply_allocation_answers.py"),
             "--allocation", str(allocation_path), "--answers", str(staging_path), "--dry-run"],
            capture_output=True, text=True,
        )
        sys.stdout.write(dry.stdout)
        sys.stderr.write(dry.stderr)
        if dry.returncode != 0:
            print("\nDry-run FAILED — fix the decisions per the errors above and re-run compose. "
                  "No answers file was published and nothing has been applied.", file=sys.stderr)
            return dry.returncode
        staging_path.replace(output_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()
    print(f"Composed and dry-run-validated {len(answers['unit_updates'])} unit update(s) -> {output_path}")
    print(f"\nDry-run passed. NEXT: python scripts/apply_allocation_answers.py "
          f"--allocation {allocation_path} --answers {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
