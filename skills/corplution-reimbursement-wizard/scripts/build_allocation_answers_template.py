#!/usr/bin/env python3
"""Build a canonical allocation-answers template from stage-2 allocation JSON."""

from __future__ import annotations

import argparse
import json
import integrity
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


OPEN_QUESTION_STATUSES = {"open", "needs_confirmation", "draft"}
CLOSED_UNIT_STATUSES = {"confirmed", "fixed", "dropped", "excluded", "non_reimbursable"}
PROJECT_CATEGORIES = {"hotel", "meal", "taxi", "travel", "other", "unknown"}
RELIABLE_DATE_SOURCES = {
    "railway_travel_date",
    "flight_travel_date",
    "trip_report_datetime",
    "didi_trip_report_datetime",
    "gaode_trip_report_datetime",
    "hotel_check_out_date",
    "mobile_month_end",
    "user_confirmed",
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return clean(value).lower() in {"1", "true", "yes", "y", "provided"}


def user_no(unit: dict[str, Any]) -> int | str:
    value = unit.get("user_no")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            return clean(value)
    unit_id = clean(unit.get("unit_id"))
    if unit_id.startswith("UNIT-"):
        try:
            return int(unit_id.rsplit("-", 1)[1])
        except ValueError:
            return unit_id
    return unit_id


def unit_no_text(unit: dict[str, Any]) -> str:
    return str(user_no(unit))


def reliable_or_current_date(unit: dict[str, Any]) -> str:
    return clean(unit.get("expense_date"))


def date_needs_user(unit: dict[str, Any]) -> bool:
    date_source = clean(unit.get("date_source"))
    if as_bool(unit.get("date_required")) or not clean(unit.get("expense_date")):
        return True
    if date_source in RELIABLE_DATE_SOURCES:
        return False
    if date_source.endswith("_provisional") or date_source in {"invoice_issue_date", "needs_user_date"}:
        return True
    return clean(unit.get("source_category")) in {"meal", "hotel", "unknown"}


def is_ride_unit(unit: dict[str, Any]) -> bool:
    return bool(
        clean(unit.get("origin"))
        or clean(unit.get("destination"))
        or clean(unit.get("source_item_id"))
        or clean(unit.get("document_subtype")) in {"didi_trip_report", "gaode_trip_report"}
    )


def question_maps(payload: dict[str, Any], include_advisory: bool) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    questions: list[dict[str, Any]] = []
    by_unit: dict[str, list[str]] = {}
    for question in payload.get("questions", []):
        status = clean(question.get("status") or "open")
        blocking = as_bool(question.get("blocking"))
        selected = status in OPEN_QUESTION_STATUSES or (include_advisory and status == "advisory")
        if not selected:
            continue
        if not blocking and not include_advisory and status != "open":
            continue
        questions.append(question)
        for unit_id in question.get("unit_ids", []):
            by_unit.setdefault(clean(unit_id), []).append(clean(question.get("question_id")))
    return questions, by_unit


def selected_units(payload: dict[str, Any], question_ids_by_unit: dict[str, list[str]]) -> list[dict[str, Any]]:
    units = payload.get("allocation_units", [])
    by_id = {clean(unit.get("unit_id")): unit for unit in units}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for question in payload.get("questions", []):
        for unit_id in question.get("unit_ids", []):
            key = clean(unit_id)
            if key in question_ids_by_unit and key in by_id and key not in seen:
                selected.append(by_id[key])
                seen.add(key)

    for unit in units:
        key = clean(unit.get("unit_id"))
        if key in seen:
            continue
        if clean(unit.get("status")) not in CLOSED_UNIT_STATUSES:
            selected.append(unit)
            seen.add(key)
    return selected


def value_or_placeholder(value: Any, placeholder: str) -> str:
    text = clean(value)
    return text if text else placeholder


def build_unit_update(unit: dict[str, Any], question_ids: list[str]) -> dict[str, Any]:
    category = clean(unit.get("source_category"))
    update: dict[str, Any] = {
        "unit_no": user_no(unit),
        "question_ids": question_ids,
        "status": "<confirmed|fixed|dropped|excluded|non_reimbursable>",
        "answer": "<summarize the user's confirmation for this item>",
    }

    if category in PROJECT_CATEGORIES and category != "mobile":
        update["client_name"] = value_or_placeholder(unit.get("client_name"), "<客户名称或事项名称>")
        update["client_charge_code"] = value_or_placeholder(unit.get("client_charge_code"), "<Client Charge Code>")
        if clean(unit.get("project_context_id")):
            update["project_context_id"] = clean(unit.get("project_context_id"))

    if date_needs_user(unit):
        update["expense_date"] = value_or_placeholder(unit.get("expense_date"), "<YYYY-MM-DD>")
        update["date_source"] = "user_confirmed"
        update["date_is_provisional"] = False
        update["date_required"] = False
    elif reliable_or_current_date(unit):
        update["expense_date"] = reliable_or_current_date(unit)

    # final_template_column is intentionally NOT offered: the visible amount
    # column is computed from source_category + city (Shanghai meal -> meal
    # column, out-of-town trip meal -> travel column, etc.) and re-normalized
    # on every apply. Offering it here invited models to set it and then
    # watch normalize silently override their value. To change a column, fix
    # source_category or city instead.
    if category == "hotel":
        update["hotel_nights"] = value_or_placeholder(unit.get("hotel_nights"), "<nights>")
        update["check_in_date"] = value_or_placeholder(unit.get("check_in_date"), "<YYYY-MM-DD>")
        update["check_out_date"] = value_or_placeholder(unit.get("check_out_date"), "<YYYY-MM-DD>")
        update["shared_room"] = as_bool(unit.get("shared_room"))
        update["room_shared_with"] = clean(unit.get("room_shared_with"))
        update["room_share_note"] = clean(unit.get("room_share_note"))

    if category in {"taxi", "travel"} and is_ride_unit(unit):
        update["origin_place_type"] = value_or_placeholder(
            unit.get("origin_place_type"),
            "<公司|家|机场|火车站|酒店|客户|餐厅|其他>",
        )
        update["destination_place_type"] = value_or_placeholder(
            unit.get("destination_place_type"),
            "<公司|家|机场|火车站|酒店|客户|餐厅|其他>",
        )

    if category == "meal":
        update["attendees"] = clean(unit.get("attendees"))
        update["final_note"] = value_or_placeholder(
            unit.get("final_note"),
            "<出差餐费|加班餐费|出差餐费（高铁站/机场）>",
        )

    if category in {"other", "unknown"}:
        update["final_note"] = value_or_placeholder(unit.get("final_note"), "<用户提供的备注>")

    if clean(unit.get("reimbursable_amount")):
        update["reimbursable_amount"] = clean(unit.get("reimbursable_amount"))

    if as_bool(unit.get("is_substitute_invoice")) or clean(unit.get("approval_required")):
        update["is_substitute_invoice"] = as_bool(unit.get("is_substitute_invoice"))
        update["substitute_for"] = clean(unit.get("substitute_for"))
        update["approval_file"] = value_or_placeholder(unit.get("approval_file"), "<approval_screenshot_path>")

    return update


def review_context(unit: dict[str, Any], question_ids: list[str]) -> dict[str, Any]:
    return {
        "unit_no": user_no(unit),
        "unit_id": clean(unit.get("unit_id")),
        "question_ids": question_ids,
        "source_filename": clean(unit.get("source_filename")),
        "supporting_invoice_filename": clean(unit.get("supporting_invoice_filename")),
        "supporting_schedule_filename": clean(unit.get("supporting_schedule_filename")),
        "invoice_no": clean(unit.get("invoice_no")),
        "seller_or_provider": clean(unit.get("seller_name") or unit.get("line_item_name")),
        "date": clean(unit.get("expense_date") or unit.get("issue_date")),
        "date_source": clean(unit.get("date_source")),
        "amount": clean(unit.get("amount") or unit.get("invoice_amount")),
        "category": clean(unit.get("source_category")),
        "final_template_column": clean(unit.get("final_template_column")),
        "suggested_client": clean(unit.get("client_name")),
        "suggested_charge_code": clean(unit.get("client_charge_code")),
        "status": clean(unit.get("status")),
        "evidence": clean(unit.get("source_note") or unit.get("expense_note") or unit.get("route")),
    }


def build_template(payload: dict[str, Any], allocation_path: Path, include_advisory: bool) -> dict[str, Any]:
    _questions, question_ids_by_unit = question_maps(payload, include_advisory)
    units = selected_units(payload, question_ids_by_unit)
    return {
        "schema_version": "allocation_answers.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "source_allocation_file": str(allocation_path),
        "fill_instructions": [
            "Fill this canonical template from the user's natural-language answers.",
            "Replace every <...> placeholder before running apply_allocation_answers.py.",
            "Keep unit_updates as the only place for per-item changes; do not invent answers[].allocations or patch expense-allocation.json directly.",
            "Use unit_no in generated answers. Do not expose internal unit_id values to the applicant in chat.",
        ],
        "review_context": [
            review_context(unit, question_ids_by_unit.get(clean(unit.get("unit_id")), []))
            for unit in units
        ],
        "unit_updates": [
            build_unit_update(unit, question_ids_by_unit.get(clean(unit.get("unit_id")), []))
            for unit in units
        ],
        "question_updates": [],
        "project_contexts": [],
    }


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Build a canonical allocation answers template.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json.")
    parser.add_argument("--output", required=True, help="Path to write allocation-answers.template.json.")
    parser.add_argument("--include-advisory", action="store_true", help="Also include advisory questions.")
    args = parser.parse_args(argv)

    allocation_path = Path(args.allocation)
    payload = load_json(allocation_path)
    integrity.require_valid(payload, allocation_path)
    template = build_template(payload, allocation_path, args.include_advisory)
    template["source_allocation_fingerprint"] = payload.get("integrity", {}).get("fingerprint", "")
    output_path = Path(args.output)
    write_json(output_path, template)
    print(f"Wrote {output_path}")
    print(f"Template unit updates: {len(template['unit_updates'])}")
    if not template["unit_updates"]:
        print("No open or draft allocation units need an answers template.")
    else:
        print("Fill placeholders, save as process/allocation-answers.json, then run apply_allocation_answers.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
