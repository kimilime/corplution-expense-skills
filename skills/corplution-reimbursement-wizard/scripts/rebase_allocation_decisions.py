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
import text_safety


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
INACTIVE_UNIT_STATUSES = {"dropped", "excluded", "non_reimbursable"}
REMOVAL_RESOLUTION_SCHEMA = "rebase_removal_resolutions.v1"
REMOVAL_USER_ACTIONS = {"intentional_removal", "replacement_provided", "restore_required"}
REMOVAL_RESOLUTION_FIELDS = {"removal_ref", "action", "replacement_units", "note"}


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
        f"{unit.get('source_filename') or Path(str(unit.get('source_file') or '?')).name} | "
        f"{unit.get('amount', '?')} | "
        f"{unit.get('expense_date') or unit.get('source_category', '')}"
    )


def unit_token(unit: dict[str, Any]) -> str:
    return f"{unit_no(unit)}@{str(unit.get('unit_ref', '')).strip().lower()}"


def removed_evidence_entries(
    old_units: dict[str, dict[str, Any]],
    new_units: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for identity, old_unit in old_units.items():
        old_status = str(old_unit.get("status", "")).strip()
        if identity in new_units:
            continue
        requires_confirmation = old_status not in INACTIVE_UNIT_STATUSES
        source_file = str(old_unit.get("source_filename") or old_unit.get("source_file") or "").strip()
        entries.append({
            "removal_ref": f"M{len(entries) + 1}@{str(old_unit.get('unit_ref', '')).strip().lower()}",
            "unit_identity_sha256": identity,
            "prior_unit_ref": unit_token(old_unit),
            "source_sha256": str(old_unit.get("source_sha256", "")).strip().lower(),
            "source_filename": Path(source_file).name if source_file else "",
            "amount": str(old_unit.get("amount", "")),
            "expense_date": str(old_unit.get("expense_date", "")),
            "source_category": str(old_unit.get("source_category", "")),
            "prior_status": old_status,
            "requires_confirmation": requires_confirmation,
            "resolution_status": "open" if requires_confirmation else "resolved",
            "resolution_action": "" if requires_confirmation else "prior_closed_item_removed",
            "replacement_unit_ids": [],
            "replacement_unit_identities": [],
            "replacement_unit_refs": [],
            "resolution_note": (
                "The prior item was already closed as dropped/excluded/non-reimbursable."
                if not requires_confirmation else ""
            ),
        })
    return entries


def load_removal_resolutions(
    path: Path,
    *,
    source_fingerprint: str,
    target_fingerprint: str,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read rebase removal resolutions {path}: {exc}")
    if not isinstance(payload, dict):
        fail("rebase removal resolutions root must be an object")
    allowed_root = {
        "schema_version", "source_allocation_fingerprint",
        "target_allocation_fingerprint", "resolutions",
    }
    unknown_root = sorted(set(payload) - allowed_root)
    if unknown_root:
        fail("unsupported removal-resolution root field(s): " + ", ".join(unknown_root))
    if payload.get("schema_version") != REMOVAL_RESOLUTION_SCHEMA:
        fail(f"schema_version must be {REMOVAL_RESOLUTION_SCHEMA!r}")
    if str(payload.get("source_allocation_fingerprint", "")).strip().lower() != source_fingerprint.lower():
        fail("removal resolutions belong to a different source allocation generation")
    if str(payload.get("target_allocation_fingerprint", "")).strip().lower() != target_fingerprint.lower():
        fail("removal resolutions belong to a different target allocation generation")
    resolutions = payload.get("resolutions")
    if not isinstance(resolutions, list) or any(not isinstance(item, dict) for item in resolutions):
        fail("resolutions must be an array of objects")
    for index, item in enumerate(resolutions, start=1):
        unknown = sorted(set(item) - REMOVAL_RESOLUTION_FIELDS)
        if unknown:
            fail(f"removal resolution #{index} has unsupported field(s): {', '.join(unknown)}")
    findings = text_safety.find_suspect_text(payload, path="rebase_removal_resolutions")
    if findings:
        fail(
            "removal resolutions appear to contain encoding-damaged text; use a UTF-8 JSON file. "
            "Findings: " + "; ".join(findings)
        )
    return [dict(item) for item in resolutions]


def apply_removal_resolutions(
    entries: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    new_units_by_ref: dict[str, dict[str, Any]],
) -> None:
    required = {
        str(entry.get("removal_ref", "")).strip().lower(): entry
        for entry in entries if entry.get("requires_confirmation")
    }
    supplied: dict[str, dict[str, Any]] = {}
    for item in resolutions:
        removal_ref = str(item.get("removal_ref", "")).strip().lower()
        if not removal_ref or removal_ref not in required:
            fail(f"removal resolution references unknown or auto-closed item {removal_ref or '<missing>'}")
        if removal_ref in supplied:
            fail(f"removal resolution repeats {removal_ref}")
        supplied[removal_ref] = item
    missing = [entry["removal_ref"] for key, entry in required.items() if key not in supplied]
    if missing:
        fail("removal resolutions are incomplete; answer every current item: " + ", ".join(missing))

    for removal_ref, entry in required.items():
        item = supplied[removal_ref]
        action = str(item.get("action", "")).strip()
        note = str(item.get("note", "")).strip()
        if action not in REMOVAL_USER_ACTIONS:
            fail(
                f"{entry['removal_ref']} action must be one of: "
                + ", ".join(sorted(REMOVAL_USER_ACTIONS))
            )
        if not note:
            fail(f"{entry['removal_ref']} requires a short applicant/coordinator confirmation note")
        raw_replacements = item.get("replacement_units", [])
        if not isinstance(raw_replacements, list) or any(not isinstance(token, str) for token in raw_replacements):
            fail(f"{entry['removal_ref']} replacement_units must be an array of exact current N@ref tokens")
        if action == "replacement_provided" and not raw_replacements:
            fail(f"{entry['removal_ref']} replacement_provided requires at least one current N@ref token")
        if action != "replacement_provided" and raw_replacements:
            fail(f"{entry['removal_ref']} may list replacement_units only with replacement_provided")

        replacement_units: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for raw_token in raw_replacements:
            token = raw_token.strip().lower()
            if token in seen_tokens:
                fail(f"{entry['removal_ref']} repeats replacement token {raw_token}")
            seen_tokens.add(token)
            number, separator, ref = token.partition("@")
            candidate = new_units_by_ref.get(ref)
            if not separator or not number.isdigit() or candidate is None or unit_token(candidate) != token:
                fail(
                    f"{entry['removal_ref']} replacement {raw_token!r} is not an exact current N@ref token"
                )
            if str(candidate.get("status", "")).strip() in INACTIVE_UNIT_STATUSES:
                fail(f"{entry['removal_ref']} replacement {raw_token!r} is already inactive")
            replacement_units.append(candidate)

        entry["resolution_action"] = action
        entry["resolution_note"] = note
        entry["replacement_unit_ids"] = [str(unit.get("unit_id", "")) for unit in replacement_units]
        entry["replacement_unit_identities"] = [
            str(unit.get("unit_identity_sha256", "")).strip().lower() for unit in replacement_units
        ]
        entry["replacement_unit_refs"] = [unit_token(unit) for unit in replacement_units]
        entry["resolution_status"] = "pending_restore" if action == "restore_required" else "resolved"


def write_removal_resolution_template(
    path: Path,
    *,
    source_fingerprint: str,
    target_fingerprint: str,
    entries: list[dict[str, Any]],
) -> None:
    payload = {
        "schema_version": REMOVAL_RESOLUTION_SCHEMA,
        "source_allocation_fingerprint": source_fingerprint,
        "target_allocation_fingerprint": target_fingerprint,
        "resolutions": [
            {
                "removal_ref": entry["removal_ref"],
                "action": "",
                "replacement_units": [],
                "note": "",
            }
            for entry in entries if entry.get("requires_confirmation")
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validated_unit_index(
    allocation: dict[str, Any],
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_identity: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, dict[str, Any]] = {}
    units = allocation.get("allocation_units", [])
    if not isinstance(units, list) or (not units and not allow_empty):
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
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], list[str]]:
    old_hints = validated_hint_index(old_alloc, "old")
    new_hints = validated_hint_index(new_alloc, "new")
    old_units_by_id = {
        str(unit.get("unit_id", "")): unit for unit in old_units.values() if unit.get("unit_id")
    }
    carried: list[dict[str, Any]] = []
    changed: list[str] = []
    adjusted: list[str] = []
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
            inactive_links = 0
            for old_unit_id in old_record.get("matched_unit_ids", []):
                old_unit = old_units_by_id.get(str(old_unit_id))
                if old_unit is None:
                    links_changed = True
                    break
                if str(old_unit.get("status", "")).strip() in INACTIVE_UNIT_STATUSES:
                    inactive_links += 1
                    continue
                identity_key = str((old_unit or {}).get("unit_identity_sha256", "")).strip().lower()
                new_unit = new_units.get(identity_key)
                if new_unit is None:
                    links_changed = True
                    break
                token = f"{unit_no(new_unit)}@{str(new_unit.get('unit_ref', '')).strip().lower()}"
                if token not in new_tokens:
                    new_tokens.append(token)
            if links_changed:
                changed.append(f"{hint_token(new_record)} — 原关联发票已变化，需重新确认")
                continue
            if not new_tokens:
                changed.append(
                    f"{hint_token(new_record)}：原关联费用项均已 drop/exclude，"
                    "不迁移旧匹配，需重新确认"
                )
                continue
            if inactive_links:
                adjusted.append(
                    f"{hint_token(new_record)}：已自动移除 {inactive_links} 个上代已关闭关联，"
                    f"保留 {len(new_tokens)} 个有效费用项"
                )
            resolution["units"] = new_tokens
        carried.append(resolution)

    for identity, old_record in old_hints.items():
        if identity not in new_hints and str(old_record.get("resolution_action", "")).strip() in HINT_ACTIONS:
            orphaned.append(f"{old_record.get('hint_id') or identity[:8]} — 原记录已不在新一代")
    return carried, changed, adjusted, fresh, orphaned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebase decisions by immutable evidence identity.")
    parser.add_argument("--old", help="Previous stamped allocation; omitted means discover from lineage")
    parser.add_argument("--new", required=True, help="Current stamped allocation")
    parser.add_argument("--output", default="process/rebase-decisions.json")
    parser.add_argument(
        "--resolutions",
        help="Filled rebase_removal_resolutions.v1 JSON for evidence that disappeared from the new generation.",
    )
    parser.add_argument(
        "--resolution-template",
        help="Output path for the prefilled removal-resolution template (defaults beside --output).",
    )
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
    new_units, new_refs = validated_unit_index(new_alloc, "new", allow_empty=True)
    explicit_by_unit = explicit_fields_by_unit(old_alloc)
    old_shas = {
        str(unit.get("source_sha256", "")).strip().lower() for unit in old_units.values()
    }

    carried: list[dict[str, Any]] = []
    changed: list[str] = []
    fresh: list[str] = []
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

    hint_carried, hint_changed, hint_adjusted, hint_fresh, hint_orphaned = migrate_hint_resolutions(
        old_alloc, new_alloc, old_units, new_units
    )
    old_fingerprint = str(old_alloc.get("integrity", {}).get("fingerprint", ""))
    new_fingerprint = str(new_alloc.get("integrity", {}).get("fingerprint", ""))
    removed_evidence = removed_evidence_entries(old_units, new_units)
    if args.resolutions:
        resolutions = load_removal_resolutions(
            Path(args.resolutions),
            source_fingerprint=old_fingerprint,
            target_fingerprint=new_fingerprint,
        )
        apply_removal_resolutions(removed_evidence, resolutions, new_refs)
    removal_template = (
        Path(args.resolution_template)
        if args.resolution_template
        else Path(args.output).with_name("rebase-removal-resolutions.json")
    )
    removal_open = [entry for entry in removed_evidence if entry.get("resolution_status") == "open"]
    removal_pending_restore = [
        entry for entry in removed_evidence if entry.get("resolution_status") == "pending_restore"
    ]
    if removal_open and not args.resolutions:
        write_removal_resolution_template(
            removal_template,
            source_fingerprint=old_fingerprint,
            target_fingerprint=new_fingerprint,
            entries=removed_evidence,
        )
    decisions = {
        "schema_version": "allocation_decisions.v1",
        "for_allocation_fingerprint": new_fingerprint[:8],
        "decisions": carried,
        "expense_hint_resolutions": hint_carried,
        "removed_evidence": removed_evidence,
        "rebase_metadata": {
            "source_allocation_file": str(old_path),
            "source_allocation_fingerprint": old_fingerprint,
            "target_allocation_fingerprint": new_fingerprint,
            "carried_unit_count": len(carried),
            "carried_hint_count": len(hint_carried),
            "changed_unit_count": len(changed),
            "fresh_unit_count": len(fresh),
            "orphaned_unit_count": len(removed_evidence),
            "removed_evidence_count": len(removed_evidence),
            "removed_evidence_confirmation_required_count": sum(
                1 for entry in removed_evidence if entry.get("requires_confirmation")
            ),
            "removed_evidence_open_count": len(removal_open),
            "removed_evidence_pending_restore_count": len(removal_pending_restore),
            "removal_resolution_template": str(removal_template),
            "changed_hint_count": len(hint_changed),
            "adjusted_hint_count": len(hint_adjusted),
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
        f"{len(fresh)} 项新增/未定案；{len(removed_evidence)} 项原证据已移除。"
    )
    print(
        f"用户费用记录：{len(hint_carried)} 条决定已迁移；{len(hint_changed)} 条关联有变；"
        f"{len(hint_adjusted)} 条迁移时自动清理；"
        f"{len(hint_fresh)} 条新增/未定案；{len(hint_orphaned)} 条原记录已移除。"
    )
    for line in changed + fresh + hint_changed + hint_adjusted + hint_fresh + hint_orphaned:
        print(f"- {line}")
    for entry in removed_evidence:
        state = (
            "需确认"
            if entry.get("resolution_status") == "open"
            else ("待恢复证据" if entry.get("resolution_status") == "pending_restore" else "已对账")
        )
        print(
            f"- {entry['removal_ref']} [{state}] {entry.get('source_filename') or '?'} | "
            f"{entry.get('amount') or '?'} | {entry.get('expense_date') or entry.get('source_category') or '?'} | "
            f"上代状态 {entry.get('prior_status') or '?'}"
        )
    print("=== 报告结束 ===")
    print(f"Wrote {out} (bound to generation {new_fingerprint[:8]}).")
    if removal_open:
        print(
            "NEXT: ask the applicant whether each removed item was intentionally omitted, replaced by "
            "an exact current N@ref item, or must be restored. Fill " + str(removal_template)
            + " in UTF-8, then rerun this rebase with --resolutions " + str(removal_template) + "."
        )
    elif removal_pending_restore:
        print(
            "NEXT: restore/add the cited source evidence, rerun extraction and allocation, then rerun rebase. "
            "The current packet remains blocked while any removal is pending_restore."
        )
    elif carried or hint_carried or removed_evidence:
        print(
            "NEXT: run compose_answers.py --decisions " + str(out)
            + " and then the official updater; all guards still apply."
        )
    else:
        print("NEXT: no prior action was transferable; resolve the changed/new items with the user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
