#!/usr/bin/env python3
"""Apply user-confirmed answers to stage-2 allocation JSON."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


ALLOWED_COLUMNS = {"hotel", "travel", "taxi", "meal", "mobile", "other"}
OPEN_STATUSES = {"open", "needs_confirmation", "draft"}
CLOSED_UNIT_STATUSES = {"confirmed", "fixed", "dropped", "excluded", "non_reimbursable"}
ADMIN_CODE = "CORP-2026-ADMIN"
ADMIN_FALLBACK_CLIENT = "项目、调研以外的其他费用"
MOBILE_CLIENT = "通讯费"

META_FIELDS = {
    "answer",
    "comment",
    "question_id",
    "question_ids",
    "reason",
    "unit_id",
    "unit_ids",
    "unit_no",
    "unit_nos",
}

ALLOWED_UNIT_FIELDS = {
    "amount",
    "approval_file",
    "approval_file_status",
    "approval_required",
    "admin_client_review_needed",
    "attendees",
    "business_reason",
    "city",
    "client_charge_code",
    "client_name",
    "confidence",
    "corrected_by_user",
    "corrected_fields",
    "correction_note",
    "destination",
    "destination_place_type",
    "date_question_reason",
    "date_is_provisional",
    "date_required",
    "date_source",
    "document_subtype",
    "expense_date",
    "expense_note",
    "expenses_nature",
    "final_note",
    "final_template_column",
    "check_in_date",
    "check_out_date",
    "hotel_city",
    "hotel_city_tier",
    "hotel_nights",
    "invoice_amount",
    "issue_date",
    "is_substitute_invoice",
    "issues",
    "match_reason",
    "manual_correction",
    "meal_context",
    "origin",
    "origin_place_type",
    "place_type_confidence",
    "place_type_needs_confirmation",
    "project_context_id",
    "reimbursable_amount",
    "room_share_note",
    "room_shared_with",
    "shared_room",
    "route",
    "source_category",
    "source_note",
    "status",
    "substitute_for",
}


CORRECTION_META_FIELDS = {
    "corrected_by_user",
    "corrected_fields",
    "correction_note",
    "manual_correction",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def list_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [clean(item) for item in value]
        return "、".join(part for part in parts if part)
    return clean(value)


def is_admin_code(value: Any) -> bool:
    return clean(value).upper() == ADMIN_CODE


def is_mobile_admin_unit(unit: dict[str, Any]) -> bool:
    return unit.get("source_category") == "mobile" or unit.get("final_template_column") == "mobile"


def is_shanghai_city(value: Any) -> bool:
    text = clean(value).lower()
    return "上海" in text or "shanghai" in text


def formal_meal_column(unit: dict[str, Any]) -> str:
    if clean(unit.get("source_category")) != "meal":
        return clean(unit.get("final_template_column"))
    city = clean(unit.get("city"))
    if city and "上海" in city:
        return "meal"
    if city:
        return "travel"
    return clean(unit.get("final_template_column")) or "meal"


def normalize_meal_column(unit: dict[str, Any]) -> None:
    if clean(unit.get("source_category")) == "meal":
        unit["final_template_column"] = formal_meal_column(unit)


def is_ride_unit(unit: dict[str, Any]) -> bool:
    return bool(
        clean(unit.get("origin"))
        or clean(unit.get("destination"))
        or clean(unit.get("source_item_id"))
        or clean(unit.get("document_subtype")) in {"didi_trip_report", "gaode_trip_report"}
    )


def formal_taxi_column(unit: dict[str, Any]) -> str:
    source_category = clean(unit.get("source_category"))
    if source_category not in {"taxi", "travel"} or not is_ride_unit(unit):
        return clean(unit.get("final_template_column"))
    city = clean(unit.get("city"))
    if city and not is_shanghai_city(city):
        return "travel"
    if city:
        return "taxi"
    return clean(unit.get("final_template_column")) or ("taxi" if source_category == "taxi" else "travel")


def normalize_taxi_column(unit: dict[str, Any]) -> None:
    if clean(unit.get("source_category")) in {"taxi", "travel"} and is_ride_unit(unit):
        unit["final_template_column"] = formal_taxi_column(unit)


def contains_place_type_placeholder(note: Any) -> bool:
    text = clean(note)
    return "出发地类型" in text or "目的地类型" in text


def strip_route_place(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"^[A-Z]{0,3}\d{1,5}\s*[,，]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(高铁|动车|火车|铁路|飞机|航班|机票)\s*", "", text)
    text = re.sub(r"\s*(二等座|一等座|商务座|硬座|软座|硬卧|软卧|经济舱|公务舱|头等舱).*$", "", text)
    return text.strip(" ,，;；。()（）")


def route_from_text(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    match = re.search(r"（([^（）]+)）", text)
    if match:
        text = match.group(1)
    for piece in re.split(r"[,，;；]", text):
        match = re.search(r"(.+?)\s*(?:->|—|~|至|到|-)\s*(.+)", piece)
        if match:
            origin = strip_route_place(match.group(1))
            destination = strip_route_place(match.group(2))
            if origin and destination:
                return f"{origin}-{destination}"
    match = re.search(r"(.+?)\s*(?:->|—|~|至|到|-)\s*(.+)", text)
    if match:
        origin = strip_route_place(match.group(1))
        destination = strip_route_place(match.group(2))
        if origin and destination:
            return f"{origin}-{destination}"
    return ""


def is_refund_fee(unit: dict[str, Any]) -> bool:
    text = clean(" ".join([
        unit.get("final_note", ""),
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("raw_remarks", ""),
        unit.get("line_item_name", ""),
        unit.get("seller_name", ""),
    ]))
    return any(keyword in text for keyword in ["退票费", "退票", "退款", "refund", "Refund", "cancellation"])


def is_rail_ticket(unit: dict[str, Any]) -> bool:
    subtype = clean(unit.get("document_subtype"))
    text = clean(" ".join([unit.get("source_note", ""), unit.get("expense_note", ""), unit.get("final_note", "")]))
    return subtype == "railway_e_ticket" or bool(re.match(r"^[GCDKZT]\d{1,5}\b", text, flags=re.IGNORECASE))


def is_flight_ticket(unit: dict[str, Any]) -> bool:
    text = clean(" ".join([
        unit.get("document_subtype", ""),
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("final_note", ""),
        unit.get("raw_remarks", ""),
    ])).lower()
    return any(keyword in text for keyword in ["飞机", "机票", "航班", "flight"])


def ticket_note(unit: dict[str, Any]) -> str:
    if clean(unit.get("origin")):
        return ""
    route = (
        route_from_text(unit.get("route"))
        or route_from_text(unit.get("source_note"))
        or route_from_text(unit.get("expense_note"))
        or route_from_text(unit.get("final_note"))
    )
    if not route:
        return ""
    if is_rail_ticket(unit):
        prefix = "高铁退票费" if is_refund_fee(unit) else "高铁"
    elif is_flight_ticket(unit):
        prefix = "飞机退票费" if is_refund_fee(unit) else "飞机"
    else:
        return ""
    return f"{prefix}（{route}）"


def contains_raw_ticket_evidence(note: Any) -> bool:
    text = clean(note)
    return bool(
        "->" in text
        or re.search(r"\b[GCDKZT]\d{1,5}\b", text, flags=re.IGNORECASE)
        or any(keyword in text for keyword in ["二等座", "一等座", "商务座", "经济舱", "公务舱", "头等舱"])
    )


def refresh_ticket_note(unit: dict[str, Any], update: dict[str, Any]) -> None:
    note = ticket_note(unit)
    if not note:
        return
    current = clean(unit.get("final_note"))
    source_note = clean(unit.get("source_note") or unit.get("expense_note"))
    if (
        "final_note" not in update
        or not current
        or current == source_note
        or contains_raw_ticket_evidence(current)
    ):
        unit["final_note"] = note


def taxi_note(unit: dict[str, Any]) -> str:
    origin_type = clean(unit.get("origin_place_type"))
    dest_type = clean(unit.get("destination_place_type"))
    if not origin_type or not dest_type:
        return clean(unit.get("final_note") or unit.get("expense_note") or unit.get("source_note"))
    suffix = "（加班）" if clean(unit.get("business_reason")) == "overtime" else ""
    return f"打车（{origin_type}-{dest_type}）{suffix}"


def refresh_taxi_note(unit: dict[str, Any], update: dict[str, Any]) -> None:
    category = clean(unit.get("source_category"))
    if category not in {"taxi", "travel"} or not clean(unit.get("origin")):
        return
    if not clean(unit.get("origin_place_type")) or not clean(unit.get("destination_place_type")):
        return
    note = clean(unit.get("final_note"))
    source_note = clean(unit.get("source_note") or unit.get("expense_note"))
    if (
        "final_note" not in update
        or not note
        or contains_place_type_placeholder(note)
        or note == source_note
        or "->" in note
    ):
        unit["final_note"] = taxi_note(unit)


def note_placeholder_errors(unit: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if contains_place_type_placeholder(unit.get("final_note")):
        errors.append("taxi note still contains place-type placeholders")
    if (
        clean(unit.get("status")) in {"confirmed", "fixed"}
        and clean(unit.get("source_category")) in {"taxi", "travel"}
        and clean(unit.get("origin"))
        and (not clean(unit.get("origin_place_type")) or not clean(unit.get("destination_place_type")))
    ):
        errors.append("confirmed taxi/travel item requires origin_place_type and destination_place_type")
    if clean(unit.get("status")) in {"confirmed", "fixed"} and (is_rail_ticket(unit) or is_flight_ticket(unit)):
        if not ticket_note(unit):
            errors.append("rail/flight item requires route evidence for the final note template")
        elif contains_raw_ticket_evidence(unit.get("final_note")):
            errors.append("rail/flight final_note must use the finance template, not raw ticket evidence")
    return errors


def mobile_accounting_errors(unit: dict[str, Any]) -> list[str]:
    source_category = clean(unit.get("source_category"))
    final_column = clean(unit.get("final_template_column"))
    client = clean(unit.get("client_name"))
    note = clean(unit.get("final_note") or unit.get("expense_note") or unit.get("source_note"))
    errors: list[str] = []
    if source_category != "mobile":
        if final_column == "mobile":
            errors.append("non-mobile expense cannot use the mobile amount column")
        if client == MOBILE_CLIENT:
            errors.append("non-mobile expense cannot use Client = 通讯费")
        if MOBILE_CLIENT in note:
            errors.append("non-mobile expense cannot use a 通讯费 note")
    project_expense_categories = {"hotel", "meal", "taxi", "travel"}
    if source_category in project_expense_categories or final_column in project_expense_categories:
        if is_admin_code(unit.get("client_charge_code")):
            errors.append("project expenses cannot be assigned to CORP-2026-ADMIN")
    return errors


def normalize_admin_client(unit: dict[str, Any]) -> None:
    if not is_admin_code(unit.get("client_charge_code")):
        return
    client = clean(unit.get("client_name"))
    placeholder = client.lower() in {"", "admin", ADMIN_CODE.lower()}
    if is_mobile_admin_unit(unit):
        if placeholder or client == ADMIN_FALLBACK_CLIENT:
            unit["client_name"] = MOBILE_CLIENT
        unit["admin_client_review_needed"] = False
        return
    if placeholder:
        unit["client_name"] = ADMIN_FALLBACK_CLIENT
        unit["admin_client_review_needed"] = True
    elif client == ADMIN_FALLBACK_CLIENT:
        unit["admin_client_review_needed"] = True
    elif client == MOBILE_CLIENT and not is_mobile_admin_unit(unit):
        unit["client_name"] = ADMIN_FALLBACK_CLIENT
        unit["admin_client_review_needed"] = True
    else:
        unit["admin_client_review_needed"] = False


def needs_admin_client_review(unit: dict[str, Any]) -> bool:
    return (
        is_admin_code(unit.get("client_charge_code"))
        and not is_mobile_admin_unit(unit)
        and clean(unit.get("client_name")) == ADMIN_FALLBACK_CLIENT
    )


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return clean(value).lower() in {"1", "true", "yes", "y", "provided"}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def units_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["unit_id"]: unit for unit in payload.get("allocation_units", [])}


def unit_no(unit: dict[str, Any]) -> str:
    if unit.get("user_no"):
        return clean(unit.get("user_no"))
    unit_id = clean(unit.get("unit_id"))
    if unit_id.startswith("UNIT-"):
        try:
            return str(int(unit_id.rsplit("-", 1)[1]))
        except ValueError:
            return unit_id
    return unit_id


def units_by_no(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit_no(unit): unit for unit in payload.get("allocation_units", [])}


def resolve_unit_ref(ref: Any, by_id: dict[str, dict[str, Any]], by_no: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    value = clean(ref)
    if value in by_id:
        return by_id[value]
    if value in by_no:
        return by_no[value]
    if value.isdigit():
        normalized = str(int(value))
        if normalized in by_no:
            return by_no[normalized]
        unit_id = f"UNIT-{int(value):03d}"
        return by_id.get(unit_id)
    return None


def normalize_answers(answers: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(answers, list):
        return [dict(item) for item in answers], [], []
    if not isinstance(answers, dict):
        raise ValueError("Answers must be a JSON object or a list of unit update objects.")

    unit_updates = [
        dict(item)
        for item in answers.get("unit_updates", answers.get("updates", answers.get("units", [])))
    ]
    for unit_id in as_list(answers.get("confirm_units")):
        unit_updates.append({"unit_id": unit_id, "status": "confirmed"})
    for unit_id in as_list(answers.get("drop_units")):
        unit_updates.append({"unit_id": unit_id, "status": "dropped"})
    for unit_id in as_list(answers.get("exclude_units")):
        unit_updates.append({"unit_id": unit_id, "status": "excluded"})

    question_updates = [
        dict(item)
        for item in answers.get("question_updates", [])
    ]
    context_updates = [
        dict(item)
        for item in answers.get("project_contexts", [])
    ]
    return unit_updates, question_updates, context_updates


def add_issue(unit: dict[str, Any], field: str, problem: str) -> None:
    issues = unit.setdefault("issues", [])
    issue = {"field": field, "problem": problem}
    if issue not in issues:
        issues.append(issue)


def validate_update(update: dict[str, Any], lenient: bool) -> list[str]:
    errors: list[str] = []
    for field in update:
        if field in META_FIELDS or field in ALLOWED_UNIT_FIELDS:
            continue
        message = f"Unknown unit update field: {field}"
        if lenient:
            continue
        errors.append(message)
    column = update.get("final_template_column")
    if column and column not in ALLOWED_COLUMNS:
        errors.append(f"Invalid final_template_column: {column}")
    return errors


def apply_unit_update(unit: dict[str, Any], update: dict[str, Any], lenient: bool) -> dict[str, Any]:
    errors = validate_update(update, lenient)
    if errors:
        raise ValueError("; ".join(errors))

    before = {field: unit.get(field) for field in ALLOWED_UNIT_FIELDS if field in update}
    for field, value in update.items():
        if field in META_FIELDS:
            continue
        if field not in ALLOWED_UNIT_FIELDS:
            continue
        if field in {"date_is_provisional", "date_required", "is_substitute_invoice", "place_type_needs_confirmation", "shared_room"}:
            value = as_bool(value)
        if field == "attendees":
            value = list_text(value)
        unit[field] = value

    normalize_meal_column(unit)
    normalize_taxi_column(unit)
    refresh_taxi_note(unit, update)
    refresh_ticket_note(unit, update)

    if "expense_date" in update and clean(unit.get("expense_date")):
        unit["date_required"] = False
        unit["date_is_provisional"] = False
        source = clean(unit.get("date_source"))
        if not source or source == "needs_user_date" or source.endswith("_provisional"):
            unit["date_source"] = "user_confirmed"

    if unit.get("is_substitute_invoice"):
        unit["approval_required"] = unit.get("approval_required") or "partner_approval_screenshot"
        approval_file = clean(unit.get("approval_file"))
        if approval_file:
            unit["approval_file_status"] = "provided" if Path(approval_file).exists() else "missing"
            if unit["approval_file_status"] == "missing":
                add_issue(unit, "approval_file", f"Substitute approval file not found: {approval_file}")
        else:
            unit["approval_file_status"] = unit.get("approval_file_status") or "missing"
            add_issue(unit, "approval_file", "Substitute invoice missing partner approval screenshot.")

    if unit.get("origin_place_type") and unit.get("destination_place_type"):
        unit["place_type_needs_confirmation"] = False
        unit["place_type_confidence"] = unit.get("place_type_confidence") or "confirmed"

    normalize_admin_client(unit)
    accounting_errors = mobile_accounting_errors(unit) + note_placeholder_errors(unit)
    if accounting_errors:
        raise ValueError(f"{unit.get('unit_id')} accounting conflict: " + "; ".join(accounting_errors))

    after = {field: unit.get(field) for field in ALLOWED_UNIT_FIELDS if field in update}
    changed_fields = [
        field for field in after
        if before.get(field) != after.get(field) and field not in CORRECTION_META_FIELDS
    ]
    if changed_fields and update.get("status") != "confirmed":
        unit["manual_correction"] = bool(update.get("manual_correction", unit.get("manual_correction", False)))
    if update.get("correction_note") or update.get("manual_correction"):
        unit["manual_correction"] = True
        unit["corrected_by_user"] = True
        existing = unit.get("corrected_fields") or []
        if not isinstance(existing, list):
            existing = [existing]
        unit["corrected_fields"] = sorted(set(existing + changed_fields))
    return {
        "unit_id": unit.get("unit_id"),
        "user_no": unit_no(unit),
        "question_ids": update.get("question_ids") or update.get("question_id") or [],
        "answer": update.get("answer") or update.get("comment") or update.get("reason") or "",
        "before": before,
        "after": after,
    }


def merge_contexts(payload: dict[str, Any], context_updates: list[dict[str, Any]]) -> None:
    if not context_updates:
        return
    contexts = payload.setdefault("project_contexts", [])
    by_id = {ctx.get("context_id"): ctx for ctx in contexts if ctx.get("context_id")}
    for idx, update in enumerate(context_updates, start=1):
        context_id = update.get("context_id") or f"CTX-{len(contexts) + idx:03d}"
        if context_id in by_id:
            by_id[context_id].update(update)
        else:
            item = dict(update)
            item["context_id"] = context_id
            item.setdefault("travel_buffer_days", 1)
            item.setdefault("status", "confirmed")
            contexts.append(item)
            by_id[context_id] = item


def apply_question_updates(payload: dict[str, Any], updates: list[dict[str, Any]]) -> None:
    questions = {q.get("question_id"): q for q in payload.get("questions", [])}
    for update in updates:
        question_id = update.get("question_id")
        if question_id not in questions:
            continue
        question = questions[question_id]
        question["status"] = update.get("status", "answered")
        if "answer" in update:
            question["answer"] = update["answer"]


def close_answered_questions(payload: dict[str, Any], touched_units: set[str]) -> None:
    unit_status = {
        unit.get("unit_id"): unit.get("status")
        for unit in payload.get("allocation_units", [])
    }
    for question in payload.get("questions", []):
        if question.get("status", "open") not in OPEN_STATUSES:
            continue
        unit_ids = set(question.get("unit_ids", []))
        if not unit_ids or not unit_ids.intersection(touched_units):
            continue
        if all(unit_status.get(unit_id) in CLOSED_UNIT_STATUSES for unit_id in unit_ids):
            question["status"] = "answered"


def sync_admin_client_advisories(payload: dict[str, Any]) -> None:
    questions = payload.setdefault("questions", [])
    existing = {
        q.get("unit_ids", [""])[0]: q
        for q in questions
        if q.get("question_type") == "admin_client_description" and q.get("unit_ids")
    }
    for unit in payload.get("allocation_units", []):
        unit_id = unit.get("unit_id", "")
        question = existing.get(unit_id)
        if needs_admin_client_review(unit):
            if question:
                if question.get("status") == "answered":
                    question["status"] = "advisory"
                continue
            questions.append({
                "question_id": f"Q-ADMIN-CLIENT-{unit_id}",
                "question_type": "admin_client_description",
                "unit_ids": [unit_id],
                "user_no": unit_no(unit),
                "question": (
                    f"第{unit_no(unit)}项已经归到 CORP-2026-ADMIN，Client 暂写为"
                    f"“{ADMIN_FALLBACK_CLIENT}”。如果其实是年会、半年会、客户会、"
                    "行业协会会议等具体事项，请直接告诉我要改成什么；不改也可以继续写表。"
                ),
                "why_it_matters": "Admin 的 Client 列用于说明事项，不能笼统写 Admin；事项名称缺失不是阻塞项。",
                "status": "advisory",
                "blocking": False,
            })
        elif question and question.get("status") == "advisory":
            question["status"] = "answered"


def build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Expense Allocation Process",
        "",
        f"Generated at: {payload['generated_at']}",
        f"Source extraction file: {payload['source_extraction_file']}",
        f"Allocation units: {len(payload['allocation_units'])}",
        f"Questions remaining: {sum(1 for q in payload['questions'] if q.get('status', 'open') == 'open')}",
        "",
        "## Project Contexts",
        "",
        "| Context ID | Date Range | City | Client | Code | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for ctx in payload.get("project_contexts", []):
        lines.append(
            f"| {ctx.get('context_id','')} | {ctx.get('date_start','')} - {ctx.get('date_end','')} | "
            f"{ctx.get('city','')} | {ctx.get('client_name','')} | {ctx.get('client_charge_code','')} | {ctx.get('project_description','')} |"
        )
    lines += [
        "",
        "## Allocation Draft",
        "",
        "| User No | Unit ID | Source File | Source | Date | City/Route | Invoice Amount | Reimbursable Amount | Category | Suggested Project | Code | Final Column | Confidence | Status |",
        "| ---: | --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for unit in payload["allocation_units"]:
        city_route = unit.get("city") or unit.get("route") or unit.get("source_note", "")
        lines.append(
            f"| {unit_no(unit)} | {unit['unit_id']} | {unit.get('source_filename','')} | "
            f"{unit.get('source_document_id','')} {unit.get('source_item_id') or ''} | "
            f"{unit.get('expense_date','')} | {city_route} | {unit.get('invoice_amount') or unit.get('amount','')} | "
            f"{unit.get('reimbursable_amount') or unit.get('amount','')} | {unit.get('source_category','')} | "
            f"{unit.get('client_name','')} | {unit.get('client_charge_code','')} | {unit.get('final_template_column','')} | "
            f"{unit.get('confidence','')} | {unit.get('status','')} |"
        )
    lines += [
        "",
        "## Questions For User",
        "",
        "| Question ID | Unit(s) | Status | Question | Answer | Why It Matters |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for question in payload["questions"]:
        lines.append(
            f"| {question['question_id']} | {', '.join(question.get('unit_ids', []))} | {question.get('status','open')} | "
            f"{question.get('question','')} | {question.get('answer','')} | {question.get('why_it_matters','')} |"
        )
    return "\n".join(lines) + "\n"


def print_open_questions(payload: dict[str, Any]) -> None:
    open_questions = [q for q in payload.get("questions", []) if q.get("status", "open") == "open"]
    if not open_questions:
        print("No open allocation questions remain.")
    else:
        print("")
        print("QUESTIONS STILL OPEN:")
        for idx, question in enumerate(open_questions, start=1):
            print(f"{idx}. {question.get('question', '')}")


def print_advisory_questions(payload: dict[str, Any]) -> None:
    advisory_questions = [q for q in payload.get("questions", []) if q.get("status") == "advisory"]
    if not advisory_questions:
        return
    print("")
    print("NON-BLOCKING PROMPTS TO SHOW IN CHAT:")
    print("These are optional refinements. They do not block Excel output if the default value is acceptable.")
    for idx, question in enumerate(advisory_questions, start=1):
        print(f"{idx}. {question.get('question', '')}")


def apply_answers(
    allocation_path: Path,
    answers_path: Path,
    output_path: Path,
    markdown_path: Path,
    lenient: bool,
) -> dict[str, Any]:
    payload = load_json(allocation_path)
    answers = load_json(answers_path)
    unit_updates, question_updates, context_updates = normalize_answers(answers)
    unit_lookup = units_by_id(payload)
    unit_no_lookup = units_by_no(payload)

    merge_contexts(payload, context_updates)
    changes = []
    touched_units: set[str] = set()
    for update in unit_updates:
        unit_refs = as_list(update.get("unit_ids") or update.get("unit_id") or update.get("unit_nos") or update.get("unit_no"))
        if not unit_refs:
            raise ValueError("Each unit update must include unit_no/unit_nos or unit_id/unit_ids.")
        for unit_ref in unit_refs:
            unit = resolve_unit_ref(unit_ref, unit_lookup, unit_no_lookup)
            if not unit:
                raise ValueError(f"Unknown unit reference: {unit_ref}")
            changes.append(apply_unit_update(unit, update, lenient))
            touched_units.add(unit["unit_id"])

    for unit in payload.get("allocation_units", []):
        normalize_admin_client(unit)

    apply_question_updates(payload, question_updates)
    close_answered_questions(payload, touched_units)
    sync_admin_client_advisories(payload)
    payload["generated_at"] = datetime.now().replace(microsecond=0).isoformat()
    payload.setdefault("change_log", []).append({
        "timestamp": payload["generated_at"],
        "script": "apply_allocation_answers.py",
        "answers_file": str(answers_path),
        "unit_update_count": len(unit_updates),
        "question_update_count": len(question_updates),
        "context_update_count": len(context_updates),
        "changes": changes,
    })

    if output_path.resolve() == allocation_path.resolve():
        backup = allocation_path.with_suffix(allocation_path.suffix + ".bak")
        shutil.copy2(allocation_path, backup)
    write_json(output_path, payload)
    markdown_path.write_text(build_markdown(payload), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply user answers to process/expense-allocation.json.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json.")
    parser.add_argument("--answers", required=True, help="JSON file with unit_updates/question_updates.")
    parser.add_argument("--output", help="Output allocation JSON. Defaults to overwriting --allocation with a .bak backup.")
    parser.add_argument("--md-output", help="Output allocation Markdown. Defaults to output JSON sibling expense-allocation.md.")
    parser.add_argument("--lenient", action="store_true", help="Ignore unknown update fields instead of failing.")
    args = parser.parse_args(argv)

    allocation_path = Path(args.allocation)
    output_path = Path(args.output) if args.output else allocation_path
    markdown_path = Path(args.md_output) if args.md_output else output_path.with_name("expense-allocation.md")
    payload = apply_answers(
        allocation_path=allocation_path,
        answers_path=Path(args.answers),
        output_path=output_path,
        markdown_path=markdown_path,
        lenient=args.lenient,
    )
    print(f"Wrote {output_path}")
    print(f"Wrote {markdown_path}")
    print_open_questions(payload)
    print_advisory_questions(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
