#!/usr/bin/env python3
"""Carry verified user decisions across allocation regenerations.

The tool matches immutable evidence identities, never display numbers. It is
valid only across allocations with identical project contexts, reimbursement
policy, and allocation engine revision. Output remains an ordinary
``allocation_decisions.v1`` file and must pass Composer + updater.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import allocation_generations
import integrity


# Only applicant/agent judgments belong here. Evidence-side values such as
# invoice_amount, issue_date, source_note, confidence, and computed columns are
# deliberately regenerated. The change log decides which of these fields were
# actually set through the official updater in the source generation.
USER_DECISION_FIELDS = {
    "admin_client_review_needed",
    "approval_file",
    "approval_required",
    "attendees",
    "business_reason",
    "check_in_date",
    "check_out_date",
    "city",
    "client_charge_code",
    "client_name",
    "correction_note",
    "destination",
    "destination_place_type",
    "expense_date",
    "final_note",
    "hotel_city",
    "hotel_nights",
    "is_substitute_invoice",
    "manual_correction",
    "meal_context",
    "origin",
    "origin_place_type",
    "project_context_id",
    "reimbursable_amount",
    "room_share_note",
    "room_shared_with",
    "route",
    "shared_room",
    "source_category",
    "status",
    "substitute_for",
}
AUDIT_FIELDS = {"corrected_by_user", "corrected_fields", "correction_note", "manual_correction"}
FINAL_STATUSES = allocation_generations.FINAL_UNIT_STATUSES
HINT_ACTIONS = {"matched_existing", "covered_by_invoice", "not_reimbursed", "pending_invoice"}


def fail(message: str, code: int = 2) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_stamped(path: Path, label: str) -> dict[str, Any]:
    payload, reason = allocation_generations.load_stamped(path)
    if payload is None:
        fail(
            f"{label} allocation {path} is not a valid stamped generation ({reason}). "
            "Rebase refuses to launder modified process data; regenerate or recover a valid generation.",
            4,
        )
    return payload


def require_same_business_basis(old_alloc: dict[str, Any], new_alloc: dict[str, Any]) -> None:
    reason = allocation_generations.business_basis_error(old_alloc, new_alloc)
    if reason:
        fail(
            f"{reason}. Rebase is only a seat-number relocation tool for unchanged business rules. "
            "Review the regenerated allocation from scratch instead of carrying historical decisions."
        )


def unit_no(unit: dict[str, Any]) -> str:
    return str(unit.get("user_no") or unit.get("unit_no") or "?")


def brief(unit: dict[str, Any]) -> str:
    return (
        f"{unit.get('source_filename', '?')} | {unit.get('amount', '?')} | "
        f"{unit.get('expense_date') or unit.get('source_category', '')}"
    )


def validated_unit_index(
    allocation: dict[str, Any],
    label: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_identity: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, dict[str, Any]] = {}
    units = allocation.get("allocation_units", [])
    if not isinstance(units, list) or not units:
        fail(f"{label} allocation has no allocation units")
    for unit in units:
        identity = str(unit.get("unit_identity_sha256", "")).strip().lower()
        ref = str(unit.get("unit_ref", "")).strip().lower()
        source_sha = str(unit.get("source_sha256", "")).strip().lower()
        if not identity or not ref or not source_sha:
            fail(
                f"{label} item {unit_no(unit)} lacks full evidence identity, short ref, or source SHA-256. "
                "It predates the safe generation protocol and cannot be rebased."
            )
        if identity in by_identity:
            fail(f"{label} allocation has duplicate full evidence identity {identity}")
        if ref in by_ref:
            fail(f"{label} allocation has duplicate short evidence ref {ref}")
        by_identity[identity] = unit
        by_ref[ref] = unit
    return by_identity, by_ref


def explicit_fields_by_unit(allocation: dict[str, Any]) -> dict[str, set[str]]:
    explicit: dict[str, set[str]] = {}
    for entry in allocation.get("change_log", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue
            unit_id = str(change.get("unit_id", "")).strip()
            after = change.get("after", {})
            if not unit_id or not isinstance(after, dict):
                continue
            explicit.setdefault(unit_id, set()).update(set(after) & USER_DECISION_FIELDS)
    return explicit


def carried_fields(unit: dict[str, Any], explicit: set[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {"status": unit.get("status")}
    for field in sorted(explicit - {"status"}):
        if field in unit:
            # Empty values are meaningful when the user explicitly cleared a field.
            fields[field] = unit[field]
    if explicit:
        for field in AUDIT_FIELDS:
            if field in unit:
                fields[field] = unit[field]
    return fields


def validated_hint_index(
    allocation: dict[str, Any],
    label: str,
) -> dict[str, dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}
    refs: set[str] = set()
    for record in allocation.get("expense_hint_reconciliation", []):
        if not isinstance(record, dict):
            continue
        identity = str(record.get("hint_identity_sha256", "")).strip().lower()
        ref = str(record.get("hint_ref", "")).strip().lower()
        if not identity or not ref:
            fail(
                f"{label} applicant record {record.get('hint_id') or '?'} lacks full/short hint identity; "
                "regenerate and review it rather than rebasing"
            )
        if identity in by_identity:
            fail(f"{label} allocation has duplicate applicant-record identity {identity}")
        if ref in refs:
            fail(f"{label} allocation has duplicate applicant-record short ref {ref}")
        by_identity[identity] = record
        refs.add(ref)
    return by_identity


def hint_token(record: dict[str, Any]) -> str:
    display = str(record.get("display_ref", "")).strip()
    ref = str(record.get("hint_ref", "")).strip()
    return str(record.get("display_token", "")).strip() or f"{display}@{ref}"


def migrate_hint_resolutions(
    old_alloc: dict[str, Any],
    new_alloc: dict[str, Any],
    old_units: dict[str, dict[str, Any]],
    new_units: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    old_hints = validated_hint_index(old_alloc, "old")
    new_hints = validated_hint_index(new_alloc, "new")
    old_units_by_id = {
        str(unit.get("unit_id", "")): unit for unit in old_units.values() if unit.get("unit_id")
    }
    carried: list[dict[str, Any]] = []
    changed: list[str] = []
    fresh: list[str] = []
    orphaned: list[str] = []

    for identity, new_record in new_hints.items():
        if str(new_record.get("resolution_status", "")).strip() in {"not_required", "resolved"}:
            continue
        old_record = old_hints.get(identity)
        action = str((old_record or {}).get("resolution_action", "")).strip()
        if old_record is None or action not in HINT_ACTIONS:
            fresh.append(f"{hint_token(new_record)} — 新增或上代未定案")
            continue
        resolution: dict[str, Any] = {
            "question_id": str(new_record.get("question_id", "")).strip(),
            "record_ref": hint_token(new_record),
            "action": action,
            "note": str(old_record.get("resolution_answer", "")).strip(),
        }
        if action in {"matched_existing", "covered_by_invoice"}:
            new_tokens: list[str] = []
            links_changed = False
            for old_unit_id in old_record.get("matched_unit_ids", []):
                old_unit = old_units_by_id.get(str(old_unit_id))
                identity_key = str((old_unit or {}).get("unit_identity_sha256", "")).strip().lower()
                new_unit = new_units.get(identity_key)
                if old_unit is None or new_unit is None:
                    links_changed = True
                    break
                new_tokens.append(
                    f"{unit_no(new_unit)}@{str(new_unit.get('unit_ref', '')).strip().lower()}"
                )
            if links_changed or not new_tokens:
                changed.append(f"{hint_token(new_record)} — 原关联发票已变化，需重新确认")
                continue
            resolution["units"] = new_tokens
        carried.append(resolution)

    for identity, old_record in old_hints.items():
        if identity not in new_hints and str(old_record.get("resolution_action", "")).strip() in HINT_ACTIONS:
            orphaned.append(f"{old_record.get('hint_id') or identity[:8]} — 原记录已不在新一代")
    return carried, changed, fresh, orphaned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebase decisions by immutable evidence identity.")
    parser.add_argument("--old", help="Previous stamped allocation; omitted means discover from lineage")
    parser.add_argument("--new", required=True, help="Current stamped allocation")
    parser.add_argument("--output", default="process/rebase-decisions.json")
    args = parser.parse_args(argv)

    new_path = Path(args.new)
    new_alloc = load_stamped(new_path, "new")
    if args.old:
        old_path = Path(args.old)
        old_alloc = load_stamped(old_path, "old")
    else:
        old_path, old_alloc, reason = allocation_generations.discover_rebase_source(new_path, new_alloc)
        if old_path is None or old_alloc is None:
            fail(f"no safe prior decision generation was found ({reason})")
        print(f"Discovered prior decided generation: {old_path}")
    require_same_business_basis(old_alloc, new_alloc)

    old_units, _old_refs = validated_unit_index(old_alloc, "old")
    new_units, _new_refs = validated_unit_index(new_alloc, "new")
    explicit_by_unit = explicit_fields_by_unit(old_alloc)
    old_shas = {
        str(unit.get("source_sha256", "")).strip().lower() for unit in old_units.values()
    }

    carried: list[dict[str, Any]] = []
    changed: list[str] = []
    fresh: list[str] = []
    orphaned: list[str] = []
    for identity, new_unit in new_units.items():
        old_unit = old_units.get(identity)
        if old_unit is None:
            if str(new_unit.get("source_sha256", "")).strip().lower() in old_shas:
                changed.append(f"第{unit_no(new_unit)}项 [{new_unit.get('unit_ref')}] {brief(new_unit)} — 证据已变更")
            else:
                fresh.append(f"第{unit_no(new_unit)}项 [{new_unit.get('unit_ref')}] {brief(new_unit)} — 新增")
            continue
        if str(old_unit.get("status", "")).strip() not in FINAL_STATUSES:
            fresh.append(f"第{unit_no(new_unit)}项 [{new_unit.get('unit_ref')}] {brief(new_unit)} — 上代未定案")
            continue
        fields = carried_fields(
            old_unit,
            explicit_by_unit.get(str(old_unit.get("unit_id", "")), set()),
        )
        carried.append({
            "units": f"{unit_no(new_unit)}@{str(new_unit.get('unit_ref', '')).strip().lower()}",
            "set": fields,
        })

    for identity, old_unit in old_units.items():
        if identity not in new_units and str(old_unit.get("status", "")).strip() in FINAL_STATUSES:
            orphaned.append(f"[{old_unit.get('unit_ref')}] {brief(old_unit)} — 证据已移除，原决定作废")

    hint_carried, hint_changed, hint_fresh, hint_orphaned = migrate_hint_resolutions(
        old_alloc, new_alloc, old_units, new_units
    )
    old_fingerprint = str(old_alloc.get("integrity", {}).get("fingerprint", ""))
    new_fingerprint = str(new_alloc.get("integrity", {}).get("fingerprint", ""))
    decisions = {
        "schema_version": "allocation_decisions.v1",
        "for_allocation_fingerprint": new_fingerprint[:8],
        "decisions": carried,
        "expense_hint_resolutions": hint_carried,
        "rebase_metadata": {
            "source_allocation_file": str(old_path),
            "source_allocation_fingerprint": old_fingerprint,
            "target_allocation_fingerprint": new_fingerprint,
            "carried_unit_count": len(carried),
            "carried_hint_count": len(hint_carried),
            "changed_unit_count": len(changed),
            "fresh_unit_count": len(fresh),
            "orphaned_unit_count": len(orphaned),
            "changed_hint_count": len(hint_changed),
            "fresh_hint_count": len(hint_fresh),
            "orphaned_hint_count": len(hint_orphaned),
        },
    }
    integrity.stamp(decisions, "rebase_allocation_decisions.py")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== 请将以下迁移报告原样转发给用户 ===")
    print(
        f"费用项：{len(carried)} 项原决定已迁移；{len(changed)} 项证据有变；"
        f"{len(fresh)} 项新增/未定案；{len(orphaned)} 项原证据已移除。"
    )
    print(
        f"用户费用记录：{len(hint_carried)} 条决定已迁移；{len(hint_changed)} 条关联有变；"
        f"{len(hint_fresh)} 条新增/未定案；{len(hint_orphaned)} 条原记录已移除。"
    )
    for line in changed + fresh + orphaned + hint_changed + hint_fresh + hint_orphaned:
        print(f"- {line}")
    print("=== 报告结束 ===")
    print(f"Wrote {out} (bound to generation {new_fingerprint[:8]}).")
    if carried or hint_carried:
        print(
            "NEXT: run compose_answers.py --decisions " + str(out)
            + " and then the official updater; all guards still apply."
        )
    else:
        print("NEXT: no prior action was transferable; resolve the changed/new items with the user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
