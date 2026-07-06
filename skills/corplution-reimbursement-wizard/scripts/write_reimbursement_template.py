#!/usr/bin/env python3
"""Write confirmed expense allocations into the reimbursement Excel workbook."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib


C = {
    "summary": "\u6c47\u603b",
    "local": "\u672c\u5730",
    "trip": "\u51fa\u5dee",
    "substitute": "\uff08\u62b5\uff09",
    "trip_meal": "\u51fa\u5dee\u9910\u8d39",
    "overtime_meal": "\u52a0\u73ed\u9910\u8d39",
    "business_trip_meal_policy": "\u51fa\u5dee\u9910\u8d39",
    "local_overtime_meal_policy": "\u672c\u5730\u52a0\u73ed\u9910\u8d39",
    "hotel_policy_first_tier": "\u9152\u5e97\uff08\u5317\u4e0a\u5e7f\u6df1\uff09",
    "hotel_policy_other_city": "\u9152\u5e97\uff08\u5176\u4ed6\u57ce\u5e02\uff09",
    "invoice_amount": "\u53d1\u7968\u91d1\u989d",
    "reimbursable_amount": "\u5b9e\u9645\u62a5\u9500",
    "meal_cap_ok": "\u672a\u8d85\u6807",
    "meal_cap_over_with_attendees": "\u8d85\u6807\uff0c\u5df2\u6709\u591a\u4eba\u4fe1\u606f\uff0c\u4ec5\u63d0\u793a\u590d\u6838",
    "meal_cap_over_needs_confirmation": "\u8d85\u6807\uff0c\u9700\u786e\u8ba4\u65e5\u671f/\u591a\u4eba/\u5b9e\u62a5\u91d1\u989d",
    "hotel_cap_missing_nights": "\u7f3a\u5c11\u665a\u6570\uff0c\u9700\u786e\u8ba4\u5165\u4f4f/\u79bb\u5e97/\u665a\u6570",
    "hotel_cap_missing_city": "\u7f3a\u5c11\u57ce\u5e02\uff0c\u9700\u786e\u8ba4\u57ce\u5e02\u6863\u4f4d",
    "hotel_cap_over_with_shared_room": "\u8d85\u6807\uff0c\u5df2\u6709\u540c\u4f4f/\u6807\u95f4\u4fe1\u606f\uff0c\u4ec5\u63d0\u793a\u590d\u6838",
    "hotel_cap_over_needs_confirmation": "\u8d85\u6807\uff0c\u9700\u786e\u8ba4\u665a\u6570/\u540c\u4f4f/\u5b9e\u62a5\u91d1\u989d",
}

BUSINESS_TRIP_MEAL_DAILY_CAP = Decimal("150.00")
LOCAL_OVERTIME_MEAL_DAILY_CAP = Decimal("60.00")
FIRST_TIER_HOTEL_CAP = Decimal("800.00")
OTHER_CITY_HOTEL_CAP = Decimal("600.00")
ADMIN_CODE = "CORP-2026-ADMIN"
ADMIN_FALLBACK_CLIENT = "\u9879\u76ee\u3001\u8c03\u7814\u4ee5\u5916\u7684\u5176\u4ed6\u8d39\u7528"
MOBILE_CLIENT = "\u901a\u8baf\u8d39"
FIRST_TIER_CITIES = {
    "\u5317\u4eac",
    "\u4e0a\u6d77",
    "\u5e7f\u5dde",
    "\u6df1\u5733",
    "beijing",
    "shanghai",
    "guangzhou",
    "shenzhen",
}


AMOUNT_COLUMNS = {
    "hotel": "G",
    "travel": "H",
    "taxi": "I",
    "meal": "J",
    "mobile": "K",
    "other": "L",
}


PROOF_ORDER = {
    "flight": 1,
    "rail": 1,
    "railway": 1,
    "railway_e_ticket": 1,
    "hotel": 2,
    "taxi_didi": 3,
    "taxi": 3,
    "didi": 3,
    "gaode": 4,
    "meal": 5,
    "mobile": 6,
    "other": 7,
    "travel": 8,
    "unknown": 99,
}


ROW_ORDER = {
    "flight": 1,
    "rail": 1,
    "railway": 1,
    "railway_e_ticket": 1,
    "hotel": 2,
    "taxi_didi": 3,
    "taxi": 3,
    "didi": 3,
    "gaode": 4,
    "meal": 5,
    "mobile": 6,
    "other": 7,
    "travel": 8,
}


def bundled_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "reimbursement-template.xlsx"


def bundled_layout_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "reimbursement-workbook-layout.toml"


def resolve_template_arg(value: str | None) -> Path | None:
    if not value:
        return None
    if value.lower() == "bundled":
        return bundled_template_path()
    return Path(value)


def resolve_layout_arg(value: str | None) -> Path:
    return Path(value) if value else bundled_layout_path()


def load_layout(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        layout = tomllib.load(handle)
    required_sections = [
        "sheet",
        "columns",
        "rows",
        "fonts",
        "formats",
        "colors",
        "styles",
        "sample_rows",
        "labels",
        "instruction_row",
        "header_row",
    ]
    missing = [section for section in required_sections if section not in layout]
    if missing:
        raise ValueError(f"Workbook layout is missing sections: {', '.join(missing)}")
    expected_columns = int(layout["columns"]["count"])
    for row_name in ("instruction_row", "header_row"):
        values = layout[row_name].get("values", [])
        if len(values) != expected_columns:
            raise ValueError(f"{row_name}.values must contain {expected_columns} entries, got {len(values)}")
    return layout


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def normalized_client_name(unit: dict[str, Any]) -> str:
    client = clean(unit.get("client_name"))
    code = clean(unit.get("client_charge_code")).upper()
    if code != ADMIN_CODE:
        return client
    placeholder = client.lower() in {"", "admin", ADMIN_CODE.lower()}
    if unit.get("source_category") == "mobile" or unit.get("final_template_column") == "mobile":
        return MOBILE_CLIENT if placeholder or client == ADMIN_FALLBACK_CLIENT else client
    return ADMIN_FALLBACK_CLIENT if placeholder else client


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
        if clean(unit.get("client_charge_code")).upper() == ADMIN_CODE:
            errors.append("project expenses cannot be assigned to CORP-2026-ADMIN")
    return errors


def formal_meal_column(unit: dict[str, Any]) -> str:
    if clean(unit.get("source_category")) != "meal":
        return clean(unit.get("final_template_column"))
    city = clean(unit.get("city"))
    if city and "上海" in city:
        return "meal"
    if city:
        return "travel"
    return clean(unit.get("final_template_column")) or "meal"


def contains_place_type_placeholder(note: Any) -> bool:
    text = clean(note)
    return "出发地类型" in text or "目的地类型" in text


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
    return errors


def money(value: Any) -> str:
    if value in (None, ""):
        return "0.00"
    try:
        return f"{Decimal(str(value).replace(',', '')):.2f}"
    except InvalidOperation:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return f"{Decimal(match.group(0)):.2f}" if match else "0.00"


def invoice_amount(unit: dict[str, Any]) -> str:
    return money(unit.get("invoice_amount") or unit.get("amount"))


def reimbursable_amount(unit: dict[str, Any]) -> str:
    return money(unit.get("reimbursable_amount") or unit.get("amount"))


def date_yyyymmdd(value: str) -> str:
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value or "")
    if not match:
        compact = re.sub(r"\D", "", value or "")
        return compact[:8]
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def parse_date(value: Any) -> date | None:
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", clean(value))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    except ValueError:
        return None


def date_in_context(unit_date: Any, ctx: dict[str, Any]) -> bool:
    d = parse_date(unit_date)
    start = parse_date(ctx.get("date_start", ""))
    end = parse_date(ctx.get("date_end", ""))
    if not d or not start or not end:
        return False
    buffer = int(ctx.get("travel_buffer_days") or 0)
    return start - timedelta(days=buffer) <= d <= end + timedelta(days=buffer)


def route_endpoints(unit: dict[str, Any]) -> tuple[str, str]:
    route = clean(unit.get("route") or unit.get("source_note") or unit.get("expense_note") or unit.get("final_note"))
    if not route:
        return "", ""
    match = re.search(r"（([^（）]+)）", route)
    if match:
        route = match.group(1)
    parts = re.split(r"\s*(?:->|-|—|~|至|到)\s*", route, maxsplit=1)
    if len(parts) == 2:
        return clean(parts[0]), clean(parts[1])
    return "", ""


def assignment_matches_context(unit: dict[str, Any], ctx: dict[str, Any]) -> bool:
    if clean(unit.get("project_context_id")) and clean(unit.get("project_context_id")) == clean(ctx.get("context_id")):
        return True
    return (
        clean(unit.get("client_name")) == clean(ctx.get("client_name"))
        and clean(unit.get("client_charge_code")) == clean(ctx.get("client_charge_code"))
    )


def travel_destination_context(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if clean(unit.get("source_category")) != "travel" and clean(unit.get("document_subtype")) != "railway_e_ticket":
        return None
    _, destination = route_endpoints(unit)
    if not destination:
        return None
    candidates = [
        ctx for ctx in contexts
        if date_in_context(unit.get("expense_date", ""), ctx)
        and clean(ctx.get("city"))
        and clean(ctx.get("city")) in destination
    ]
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def require_ready(allocation: dict[str, Any], allow_unconfirmed: bool) -> list[str]:
    errors: list[str] = []
    contexts = allocation.get("project_contexts", [])
    open_questions = [q for q in allocation.get("questions", []) if q.get("status", "open") == "open"]
    if open_questions and not allow_unconfirmed:
        errors.append(f"{len(open_questions)} open allocation question(s) remain.")
    for unit in allocation.get("allocation_units", []):
        status = unit.get("status", "")
        if status in {"dropped", "excluded", "non_reimbursable"}:
            continue
        if not allow_unconfirmed and status not in {"confirmed", "fixed"}:
            errors.append(f"{unit.get('unit_id')} is not confirmed or fixed.")
        if not allow_unconfirmed and unit.get("date_required"):
            errors.append(f"{unit.get('unit_id')} still requires a user-confirmed expense date.")
        if (
            not allow_unconfirmed
            and unit.get("date_is_provisional")
            and unit.get("source_category") != "other"
        ):
            errors.append(f"{unit.get('unit_id')} has a provisional date but is not an other expense.")
        for accounting_error in mobile_accounting_errors(unit):
            errors.append(f"{unit.get('unit_id')} accounting conflict: {accounting_error}.")
        for note_error in note_placeholder_errors(unit):
            errors.append(f"{unit.get('unit_id')} note conflict: {note_error}.")
        destination_ctx = travel_destination_context(unit, contexts)
        if destination_ctx and not assignment_matches_context(unit, destination_ctx):
            errors.append(
                f"{unit.get('unit_id')} travel route destination points to "
                f"{destination_ctx.get('client_name', '')}/{destination_ctx.get('client_charge_code', '')}; "
                "travel between project cities should belong to the project being traveled to."
            )
        for field in ["client_name", "client_charge_code", "final_template_column", "amount", "expense_date"]:
            if field == "client_name" and clean(unit.get("client_charge_code")).upper() == ADMIN_CODE:
                continue
            if not unit.get(field):
                errors.append(f"{unit.get('unit_id')} missing {field}.")
    return errors


def included_units(allocation: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for unit in allocation.get("allocation_units", []):
        if unit.get("status") in {"dropped", "excluded", "non_reimbursable"}:
            continue
        if Decimal(reimbursable_amount(unit)) == 0:
            continue
        out.append(dict(unit))
    return out


def proof_type(unit: dict[str, Any]) -> str:
    subtype = unit.get("document_subtype", "")
    source = unit.get("source_category", "")
    seller = clean(unit.get("seller_name", ""))
    source_doc = clean(unit.get("source_note", ""))
    if subtype == "railway_e_ticket" or source == "rail" or "高铁" in source_doc:
        return "rail"
    if source == "hotel":
        return "hotel"
    if "高德" in seller or "高德" in source_doc:
        return "gaode"
    if unit.get("source_item_id") or "滴滴" in seller or "Didi" in source_doc:
        return "taxi_didi"
    if source == "taxi":
        return "taxi"
    if source == "meal":
        return "meal"
    if source == "mobile":
        return "mobile"
    if source == "travel":
        return "travel"
    return source or "other"


def proof_key(unit: dict[str, Any]) -> str:
    ptype = proof_type(unit)
    if ptype in {"taxi_didi", "gaode"}:
        return unit.get("supporting_invoice_document_id") or unit.get("supporting_schedule_document_id") or unit.get("source_document_id")
    return unit.get("supporting_invoice_document_id") or unit.get("source_document_id")


def assign_proof_numbers(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for unit in units:
        ptype = proof_type(unit)
        key = proof_key(unit) or unit["unit_id"]
        gkey = (ptype, key)
        group = groups.setdefault(gkey, {
            "proof_group_id": f"PROOF-{len(groups)+1:03d}",
            "proof_type": ptype,
            "proof_key": key,
            "source_document_ids": [],
            "support_document_ids": [],
            "source_item_ids": [],
            "source_invoice_no": unit.get("invoice_no", ""),
            "amount_total": Decimal("0.00"),
            "min_date": unit.get("expense_date", ""),
            "units": [],
        })
        group["units"].append(unit["unit_id"])
        group["amount_total"] += Decimal(reimbursable_amount(unit))
        group["min_date"] = min([d for d in [group["min_date"], unit.get("expense_date", "")] if d] or [""])
        for field, target in [
            ("supporting_invoice_document_id", "source_document_ids"),
            ("source_document_id", "source_document_ids"),
            ("supporting_schedule_document_id", "support_document_ids"),
        ]:
            value = unit.get(field)
            if value and value not in group[target]:
                group[target].append(value)
        if unit.get("source_item_id") and unit["source_item_id"] not in group["source_item_ids"]:
            group["source_item_ids"].append(unit["source_item_id"])

    ordered = sorted(
        groups.values(),
        key=lambda g: (PROOF_ORDER.get(g["proof_type"], 99), g.get("min_date", ""), g["proof_key"]),
    )
    for idx, group in enumerate(ordered, start=1):
        group["proof_no"] = idx
        group["amount_total"] = money(group["amount_total"])
    proof_by_unit = {}
    for group in ordered:
        for unit_id in group["units"]:
            proof_by_unit[unit_id] = group["proof_no"]
    for unit in units:
        unit["proof_no"] = proof_by_unit[unit["unit_id"]]
    return ordered


def final_note(unit: dict[str, Any]) -> str:
    note = clean(unit.get("final_note") or unit.get("expense_note") or unit.get("source_note"))
    if unit.get("is_substitute_invoice") and C["substitute"] not in note:
        note += C["substitute"]
    invoice = Decimal(invoice_amount(unit))
    reimbursable = Decimal(reimbursable_amount(unit))
    if reimbursable != invoice and C["invoice_amount"] not in note:
        note += f"\uff08{C['invoice_amount']}{money(invoice)}/{C['reimbursable_amount']}{money(reimbursable)}\uff09"
    return note


def expense_nature(unit: dict[str, Any]) -> str:
    city = clean(unit.get("city"))
    amount_column = formal_meal_column(unit) if unit.get("source_category") == "meal" else unit.get("final_template_column", "")
    if amount_column == "mobile":
        return C["local"]
    if amount_column in {"hotel", "travel"}:
        return C["trip"]
    if city:
        return C["local"] if "\u4e0a\u6d77" in city else C["trip"]
    value = clean(unit.get("expenses_nature"))
    if value:
        return value
    return C["local"]


def make_rows(units: list[dict[str, Any]], requester: str) -> list[dict[str, Any]]:
    rows = []
    for unit in units:
        amount_col = formal_meal_column(unit) if unit.get("source_category") == "meal" else unit.get("final_template_column")
        amount_col = amount_col or "other"
        if amount_col not in AMOUNT_COLUMNS:
            amount_col = "other"
        rows.append({
            "date": date_yyyymmdd(unit.get("expense_date", "")),
            "requester": requester,
            "client": normalized_client_name(unit),
            "client_charge_code": unit.get("client_charge_code", ""),
            "expenses_nature": expense_nature(unit),
            "note": final_note(unit),
            "amount_column": amount_col,
            "amount": reimbursable_amount(unit),
            "invoice_amount": invoice_amount(unit),
            "reimbursable_amount": reimbursable_amount(unit),
            "proof_no": unit.get("proof_no"),
            "user_no": unit.get("user_no", ""),
            "source_unit_id": unit.get("unit_id"),
            "source_document_id": unit.get("source_document_id", ""),
            "source_item_id": unit.get("source_item_id", ""),
            "source_category": unit.get("source_category", ""),
            "source_filename": unit.get("source_filename", ""),
            "supporting_invoice_filename": unit.get("supporting_invoice_filename", ""),
            "issue_date": unit.get("issue_date", ""),
            "date_source": unit.get("date_source", ""),
            "date_is_provisional": unit.get("date_is_provisional", False),
            "date_required": unit.get("date_required", False),
            "seller_name": unit.get("seller_name", ""),
            "attendees": unit.get("attendees", ""),
            "meal_context": unit.get("meal_context", ""),
            "hotel_city": unit.get("hotel_city", ""),
            "hotel_city_tier": unit.get("hotel_city_tier", ""),
            "hotel_nights": unit.get("hotel_nights", ""),
            "check_in_date": unit.get("check_in_date", ""),
            "check_out_date": unit.get("check_out_date", ""),
            "shared_room": unit.get("shared_room", False),
            "room_shared_with": unit.get("room_shared_with", ""),
            "room_share_note": unit.get("room_share_note", ""),
            "is_substitute_invoice": bool(unit.get("is_substitute_invoice")),
            "substitute_for": unit.get("substitute_for", ""),
            "approval_required": unit.get("approval_required", ""),
            "approval_file": unit.get("approval_file", ""),
            "approval_file_status": unit.get("approval_file_status", ""),
            "manual_correction": bool(unit.get("manual_correction")),
            "correction_note": unit.get("correction_note", ""),
            "corrected_fields": unit.get("corrected_fields", []),
            "row_order_type": proof_type(unit),
            "expense_date": unit.get("expense_date", ""),
        })
    return rows


def meal_cap_policy(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("source_category") != "meal":
        return None
    note = clean(row.get("note"))
    meal_context = clean(row.get("meal_context"))
    is_trip_meal = (
        row.get("amount_column") == "travel"
        or note.startswith(C["trip_meal"])
        or meal_context in {"travel", "business_trip", "station_airport"}
    )
    if is_trip_meal:
        return {
            "policy": "business_trip_meal",
            "policy_name": C["business_trip_meal_policy"],
            "cap": BUSINESS_TRIP_MEAL_DAILY_CAP,
        }
    is_local_overtime_meal = meal_context == "overtime" or note.startswith(C["overtime_meal"])
    if is_local_overtime_meal:
        return {
            "policy": "local_overtime_meal",
            "policy_name": C["local_overtime_meal_policy"],
            "cap": LOCAL_OVERTIME_MEAL_DAILY_CAP,
        }
    return None


def meal_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_no": row.get("user_no", ""),
        "proof_no": row.get("proof_no", ""),
        "source_unit_id": row.get("source_unit_id", ""),
        "source_filename": row.get("source_filename") or row.get("supporting_invoice_filename") or "",
        "seller_name": row.get("seller_name", ""),
        "invoice_amount": row.get("invoice_amount", "0.00"),
        "reimbursable_amount": row.get("reimbursable_amount", row.get("amount", "0.00")),
        "attendees": row.get("attendees", ""),
        "note": row.get("note", ""),
    }


def suggest_meal_adjustments(day_rows: list[dict[str, Any]], over_by: Decimal) -> list[dict[str, Any]]:
    remaining = over_by
    adjustments: list[dict[str, Any]] = []
    candidates = sorted(
        day_rows,
        key=lambda row: (Decimal(money(row.get("amount"))), clean(row.get("source_unit_id"))),
        reverse=True,
    )
    for row in candidates:
        if remaining <= 0:
            break
        current = Decimal(money(row.get("amount")))
        if current <= 0:
            continue
        reduction = min(current, remaining)
        suggested = current - reduction
        adjustments.append({
            "user_no": row.get("user_no", ""),
            "proof_no": row.get("proof_no", ""),
            "source_unit_id": row.get("source_unit_id", ""),
            "source_filename": row.get("source_filename") or row.get("supporting_invoice_filename") or "",
            "current_reimbursable_amount": money(current),
            "suggested_reimbursable_amount": money(suggested),
            "reduce_by": money(reduction),
        })
        remaining -= reduction
    return adjustments


def meal_daily_cap_checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_policy_date: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    policies: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        policy = meal_cap_policy(row)
        if not policy:
            continue
        date_value = row.get("date") or date_yyyymmdd(row.get("expense_date", ""))
        key = (policy["policy"], date_value)
        by_policy_date[key].append(row)
        policies[key] = policy

    checks: list[dict[str, Any]] = []
    for key, day_rows in sorted(by_policy_date.items(), key=lambda item: (item[0][1], item[0][0])):
        policy = policies[key]
        date_value = key[1]
        cap = policy["cap"]
        total = sum((Decimal(money(row.get("amount"))) for row in day_rows), Decimal("0.00"))
        over_by = total - cap
        has_attendees = any(clean(row.get("attendees")) for row in day_rows)
        status = C["meal_cap_ok"]
        severity = "ok"
        requires_confirmation = False
        suggestions: list[dict[str, Any]] = []
        if over_by > 0:
            if has_attendees:
                status = C["meal_cap_over_with_attendees"]
                severity = "advisory"
            else:
                status = C["meal_cap_over_needs_confirmation"]
                severity = "blocking"
                requires_confirmation = True
                suggestions = suggest_meal_adjustments(day_rows, over_by)
        checks.append({
            "policy": policy["policy"],
            "policy_name": policy["policy_name"],
            "date": date_value,
            "cap": money(cap),
            "total": money(total),
            "over_by": money(over_by) if over_by > 0 else "0.00",
            "status": status,
            "severity": severity,
            "advisory": severity == "advisory",
            "has_attendees": has_attendees,
            "requires_user_confirmation": requires_confirmation,
            "items": [meal_item(row) for row in day_rows],
            "suggested_adjustments": suggestions,
        })
    return checks


def print_meal_cap_check(checks: list[dict[str, Any]]) -> None:
    print("\nMEAL DAILY CAP CHECK TO SHOW IN CHAT")
    print("Copy or summarize this check in the conversation before final submission.")
    if not checks:
        print("No meal rows requiring daily cap checks found.")
        return
    for check in checks:
        print(
            f"- {check['date']} [{check.get('policy_name', '')}]: total {check['total']} / cap {check['cap']} / "
            f"over {check['over_by']} / {check['status']} / severity {check.get('severity', 'ok')}"
        )
        for item in check["items"]:
            print(
                f"  item {item.get('user_no') or '-'} | proof {item.get('proof_no') or '-'} | "
                f"{item.get('source_filename') or '-'} | invoice {item['invoice_amount']} | "
                f"reimburse {item['reimbursable_amount']} | attendees {item.get('attendees') or '-'}"
            )
        if check["suggested_adjustments"]:
            print("  suggested adjustment to fit cap:")
            for adjustment in check["suggested_adjustments"]:
                print(
                    f"  item {adjustment.get('user_no') or '-'} "
                    f"{adjustment['current_reimbursable_amount']} -> "
                    f"{adjustment['suggested_reimbursable_amount']} "
                    f"(reduce {adjustment['reduce_by']})"
                )


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return clean(value).lower() in {"1", "true", "yes", "y", "provided", "\u662f", "\u6709", "\u540c\u4f4f"}


def parse_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    parsed = int(match.group(0))
    return parsed if parsed > 0 else None


def parse_date_object(value: Any) -> date | None:
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", clean(value))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    except ValueError:
        return None


def date_strings(value: Any) -> list[str]:
    return [
        f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        for match in re.finditer(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", clean(value))
    ]


def hotel_city_policy(row: dict[str, Any]) -> dict[str, Any] | None:
    tier = clean(row.get("hotel_city_tier")).lower()
    if tier in {"first_tier", "tier1", "1", "\u4e00\u7ebf", "\u5317\u4e0a\u5e7f\u6df1"}:
        return {"city_tier": "first_tier", "policy_name": C["hotel_policy_first_tier"], "cap_per_night": FIRST_TIER_HOTEL_CAP}
    if tier in {"other", "non_first_tier", "2", "\u5176\u4ed6", "\u975e\u4e00\u7ebf"}:
        return {"city_tier": "other", "policy_name": C["hotel_policy_other_city"], "cap_per_night": OTHER_CITY_HOTEL_CAP}
    city = clean(row.get("hotel_city") or row.get("city"))
    if not city:
        return None
    city_lower = city.lower()
    if any(name in city or name in city_lower for name in FIRST_TIER_CITIES):
        return {"city_tier": "first_tier", "policy_name": C["hotel_policy_first_tier"], "cap_per_night": FIRST_TIER_HOTEL_CAP}
    return {"city_tier": "other", "policy_name": C["hotel_policy_other_city"], "cap_per_night": OTHER_CITY_HOTEL_CAP}


def hotel_stay_details(row: dict[str, Any]) -> dict[str, Any]:
    note = clean(row.get("note"))
    nights = parse_positive_int(row.get("hotel_nights") or row.get("nights"))
    if not nights:
        night_match = re.search(r"(\d+)\s*\u665a", note)
        if night_match:
            nights = int(night_match.group(1))
    check_in = clean(row.get("check_in_date"))
    check_out = clean(row.get("check_out_date"))
    date_candidates = date_strings(note)
    if not check_in and date_candidates:
        check_in = date_candidates[0]
    if not check_out and len(date_candidates) >= 2:
        check_out = date_candidates[1]
    if not nights and check_in and check_out:
        start = parse_date_object(check_in)
        end = parse_date_object(check_out)
        if start and end and end > start:
            nights = (end - start).days
    return {"nights": nights, "check_in_date": check_in, "check_out_date": check_out}


def has_hotel_shared_room(row: dict[str, Any]) -> bool:
    return boolish(row.get("shared_room")) or bool(clean(row.get("room_shared_with") or row.get("room_share_note") or row.get("attendees")))


def hotel_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_no": row.get("user_no", ""),
        "proof_no": row.get("proof_no", ""),
        "source_unit_id": row.get("source_unit_id", ""),
        "source_filename": row.get("source_filename") or row.get("supporting_invoice_filename") or "",
        "seller_name": row.get("seller_name", ""),
        "invoice_amount": row.get("invoice_amount", "0.00"),
        "reimbursable_amount": row.get("reimbursable_amount", row.get("amount", "0.00")),
        "note": row.get("note", ""),
        "room_shared_with": row.get("room_shared_with", ""),
        "room_share_note": row.get("room_share_note", ""),
    }


def hotel_cap_checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source_category") != "hotel" and row.get("amount_column") != "hotel":
            continue
        stay = hotel_stay_details(row)
        policy = hotel_city_policy(row)
        amount = Decimal(money(row.get("amount")))
        has_shared = has_hotel_shared_room(row)
        status = C["meal_cap_ok"]
        severity = "ok"
        requires_confirmation = False
        suggested_adjustments: list[dict[str, Any]] = []
        cap_total: Decimal | None = None
        over_by: Decimal | None = None

        if not stay["nights"]:
            status = C["hotel_cap_missing_nights"]
            severity = "blocking"
            requires_confirmation = True
        elif not policy:
            status = C["hotel_cap_missing_city"]
            severity = "blocking"
            requires_confirmation = True
        else:
            cap_total = policy["cap_per_night"] * Decimal(stay["nights"])
            over_by = amount - cap_total
            if over_by > 0:
                if has_shared:
                    status = C["hotel_cap_over_with_shared_room"]
                    severity = "advisory"
                else:
                    status = C["hotel_cap_over_needs_confirmation"]
                    severity = "blocking"
                    requires_confirmation = True
                    suggested_adjustments.append({
                        "user_no": row.get("user_no", ""),
                        "proof_no": row.get("proof_no", ""),
                        "source_unit_id": row.get("source_unit_id", ""),
                        "source_filename": row.get("source_filename") or row.get("supporting_invoice_filename") or "",
                        "current_reimbursable_amount": money(amount),
                        "suggested_reimbursable_amount": money(cap_total),
                        "reduce_by": money(over_by),
                    })

        checks.append({
            "policy": "hotel_cap",
            "policy_name": policy["policy_name"] if policy else "",
            "city_tier": policy["city_tier"] if policy else "",
            "city": clean(row.get("hotel_city") or row.get("city")),
            "date": row.get("date") or date_yyyymmdd(row.get("expense_date", "")),
            "check_in_date": stay["check_in_date"],
            "check_out_date": stay["check_out_date"],
            "nights": stay["nights"] or "",
            "cap_per_night": money(policy["cap_per_night"]) if policy else "",
            "cap_total": money(cap_total) if cap_total is not None else "",
            "total": money(amount),
            "over_by": money(over_by) if over_by and over_by > 0 else "0.00",
            "status": status,
            "severity": severity,
            "advisory": severity == "advisory",
            "has_shared_room": has_shared,
            "requires_user_confirmation": requires_confirmation,
            "items": [hotel_item(row)],
            "suggested_adjustments": suggested_adjustments,
        })
    return checks


def print_hotel_cap_check(checks: list[dict[str, Any]]) -> None:
    print("\nHOTEL CAP CHECK TO SHOW IN CHAT")
    print("Copy or summarize this check in the conversation before final submission.")
    if not checks:
        print("No hotel rows requiring cap checks found.")
        return
    for check in checks:
        print(
            f"- item {check['items'][0].get('user_no') or '-'} | {check.get('city') or '-'} "
            f"[{check.get('policy_name') or 'city tier unknown'}] | "
            f"{check.get('nights') or '?'} night(s) | total {check['total']} / "
            f"cap {check.get('cap_total') or '?'} / over {check['over_by']} / {check['status']} / "
            f"severity {check.get('severity', 'ok')}"
        )
        item = check["items"][0]
        print(
            f"  proof {item.get('proof_no') or '-'} | {item.get('source_filename') or '-'} | "
            f"invoice {item['invoice_amount']} | reimburse {item['reimbursable_amount']} | "
            f"shared {item.get('room_shared_with') or item.get('room_share_note') or '-'}"
        )
        if check["suggested_adjustments"]:
            print("  suggested adjustment to fit cap:")
            for adjustment in check["suggested_adjustments"]:
                print(
                    f"  item {adjustment.get('user_no') or '-'} "
                    f"{adjustment['current_reimbursable_amount']} -> "
                    f"{adjustment['suggested_reimbursable_amount']} "
                    f"(reduce {adjustment['reduce_by']})"
                )


def copy_row_style(ws: Any, source_row: int, target_row: int) -> None:
    for col in range(1, 14):
        src = ws.cell(source_row, col)
        dst = ws.cell(target_row, col)
        if src.has_style:
            dst._style = copy.copy(src._style)
        if src.number_format:
            dst.number_format = src.number_format
        if src.alignment:
            dst.alignment = copy.copy(src.alignment)
        if src.border:
            dst.border = copy.copy(src.border)
        if src.fill:
            dst.fill = copy.copy(src.fill)
        if src.font:
            dst.font = copy.copy(src.font)


def font(name: str, bold: bool = False, size: int = 9, color: str | None = None) -> Font:
    return Font(name=name, size=size, bold=bold, color=color)


def fill(color: str | None = None) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=color) if color else PatternFill(fill_type=None)


def column_count(layout: dict[str, Any]) -> int:
    return int(layout["columns"]["count"])


def style_columns(layout: dict[str, Any], key: str) -> set[int]:
    return {int(value) for value in layout["styles"].get(key, [])}


def sample_rows(layout: dict[str, Any] | None) -> dict[str, int]:
    if layout:
        return {name: int(row) for name, row in layout["sample_rows"].items()}
    return {"detail": 3, "subtotal": 6, "column_summary": 20, "total": 21, "grand": 22, "status": 23}


def column_format(layout: dict[str, Any], col_idx: int, *, text_default: bool = False) -> str:
    formats = layout["formats"]
    if col_idx in style_columns(layout, "amount_columns"):
        return formats["money"]
    if col_idx == int(layout["styles"]["proof_no_column"]):
        return formats["integer"]
    if text_default or col_idx in style_columns(layout, "text_columns"):
        return formats["text"]
    return formats["general"]


def border_for(
    col_idx: int,
    *,
    column_total: int = 13,
    top: str | None = "thin",
    bottom: str | None = "thin",
    inner: str | None = "thin",
) -> Border:
    return Border(
        left=Side(style="medium" if col_idx == 1 else None),
        right=Side(style="medium" if col_idx == column_total else inner),
        top=Side(style=top),
        bottom=Side(style=bottom),
    )


def set_cell_style(
    cell: Any,
    *,
    layout: dict[str, Any],
    font_name: str | None = None,
    bold: bool = False,
    font_color: str | None = None,
    fill_color: str | None = None,
    horizontal: str = "center",
    vertical: str = "center",
    wrap_text: bool = False,
    number_format: str = "General",
    border: Border | None = None,
) -> None:
    fonts = layout["fonts"]
    cell.font = font(font_name or fonts["latin"], bold=bold, size=int(fonts["size"]), color=font_color)
    cell.fill = fill(fill_color)
    cell.alignment = Alignment(horizontal=horizontal, vertical=vertical, wrap_text=wrap_text)
    cell.number_format = number_format
    cell.border = border or border_for(cell.column, column_total=column_count(layout))


def style_instruction_row(ws: Any, layout: dict[str, Any]) -> None:
    ws.row_dimensions[1].height = float(layout["rows"]["instruction_height"])
    fonts = layout["fonts"]
    colors = layout["colors"]
    latin_columns = style_columns(layout, "latin_instruction_columns")
    for col_idx, value in enumerate(layout["instruction_row"]["values"], start=1):
        cell = ws.cell(1, col_idx)
        cell.value = value
        set_cell_style(
            cell,
            layout=layout,
            font_name=fonts["latin"] if col_idx in latin_columns else fonts["cjk"],
            fill_color=colors["instruction_fill"],
            wrap_text=True,
            number_format=column_format(layout, col_idx),
            border=border_for(col_idx, column_total=column_count(layout), top="medium", bottom="thin"),
        )


def style_header_row(ws: Any, layout: dict[str, Any]) -> None:
    ws.row_dimensions[2].height = float(layout["rows"]["header_height"])
    colors = layout["colors"]
    date_header_column = int(layout["styles"]["date_header_column"])
    for col_idx, value in enumerate(layout["header_row"]["values"], start=1):
        cell = ws.cell(2, col_idx)
        cell.value = value
        set_cell_style(
            cell,
            layout=layout,
            bold=True,
            font_color=colors["date_header_font"] if col_idx == date_header_column else None,
            wrap_text=True,
            number_format=column_format(layout, col_idx, text_default=True),
            border=Border(
                left=Side(style="medium" if col_idx == 1 else None),
                right=Side(style="medium"),
                top=Side(style="medium"),
                bottom=Side(style="medium"),
            ),
        )


def style_detail_row(ws: Any, row: int, layout: dict[str, Any]) -> None:
    ws.row_dimensions[row].height = float(layout["rows"]["detail_height"])
    fonts = layout["fonts"]
    cjk_columns = style_columns(layout, "cjk_detail_columns")
    left_columns = style_columns(layout, "left_aligned_detail_columns")
    wrapped_columns = style_columns(layout, "wrapped_detail_columns")
    proof_no_column = int(layout["styles"]["proof_no_column"])
    for col_idx in range(1, column_count(layout) + 1):
        set_cell_style(
            ws.cell(row, col_idx),
            layout=layout,
            font_name=fonts["cjk"] if col_idx in cjk_columns else fonts["latin"],
            bold=col_idx == proof_no_column,
            horizontal="left" if col_idx in left_columns else "center",
            wrap_text=col_idx in wrapped_columns,
            number_format=column_format(layout, col_idx),
        )


def style_subtotal_row(ws: Any, row: int, layout: dict[str, Any]) -> None:
    ws.row_dimensions[row].height = float(layout["rows"]["subtotal_height"])
    fonts = layout["fonts"]
    colors = layout["colors"]
    label_column = int(layout["styles"]["subtotal_label_column"])
    formula_column = int(layout["styles"]["subtotal_formula_column"])
    bold_columns = style_columns(layout, "subtotal_bold_columns")
    for col_idx in range(1, column_count(layout) + 1):
        number_format = layout["formats"]["money"] if col_idx == formula_column else layout["formats"]["general"]
        set_cell_style(
            ws.cell(row, col_idx),
            layout=layout,
            font_name=fonts["cjk"] if col_idx == label_column else fonts["latin"],
            bold=col_idx in bold_columns,
            fill_color=colors["subtotal_fill"],
            number_format=number_format,
            border=border_for(col_idx, column_total=column_count(layout), top=None, bottom="thin"),
        )


def style_summary_row(ws: Any, row: int, kind: str, layout: dict[str, Any]) -> None:
    ws.row_dimensions[row].height = float(layout["rows"]["summary_height"])
    formula_column = int(layout["styles"]["summary_formula_column"])
    amount_columns = style_columns(layout, "amount_columns")
    for col_idx in range(1, column_count(layout) + 1):
        number_format = layout["formats"]["general"]
        if (kind == "column_summary" and col_idx in amount_columns) or (kind in {"total", "grand"} and col_idx == formula_column):
            number_format = layout["formats"]["money"]
        set_cell_style(
            ws.cell(row, col_idx),
            layout=layout,
            bold=col_idx >= 5,
            number_format=number_format,
            border=Border(),
        )


def create_base_workbook(layout: dict[str, Any]) -> Any:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = layout["sheet"]["name"]
    ws.sheet_view.showGridLines = bool(layout["sheet"].get("show_grid_lines", True))
    for col_letter, width in layout["columns"]["widths"].items():
        ws.column_dimensions[col_letter].width = width
    rows = sample_rows(layout)
    style_instruction_row(ws, layout)
    style_header_row(ws, layout)
    style_detail_row(ws, rows["detail"], layout)
    style_subtotal_row(ws, rows["subtotal"], layout)
    style_summary_row(ws, rows["column_summary"], "column_summary", layout)
    style_summary_row(ws, rows["total"], "total", layout)
    style_summary_row(ws, rows["grand"], "grand", layout)
    style_summary_row(ws, rows["status"], "status", layout)
    labels = layout["labels"]
    ws.cell(rows["subtotal"], int(layout["styles"]["subtotal_label_column"])).value = labels["project_subtotal"]
    ws.cell(rows["subtotal"], int(layout["styles"]["subtotal_formula_column"])).value = 0
    ws.cell(rows["total"], int(layout["styles"]["summary_label_column"])).value = labels["total"]
    ws.cell(rows["total"], int(layout["styles"]["summary_formula_column"])).value = 0
    ws.cell(rows["grand"], int(layout["styles"]["summary_label_column"])).value = labels["grand_total"]
    ws.cell(rows["grand"], int(layout["styles"]["summary_formula_column"])).value = 0
    ws.cell(rows["status"], int(layout["styles"]["summary_label_column"])).value = labels["status"]
    ws.cell(rows["status"], int(layout["styles"]["summary_formula_column"])).value = labels["initial_status"]
    return wb


def capture_styles(ws: Any, layout: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    styles = {}
    total_columns = column_count(layout) if layout else 13
    for name, row in sample_rows(layout).items():
        styles[name] = []
        for col in range(1, total_columns + 1):
            cell = ws.cell(row, col)
            styles[name].append({
                "style": copy.copy(cell._style),
                "number_format": cell.number_format,
                "alignment": copy.copy(cell.alignment),
                "border": copy.copy(cell.border),
                "fill": copy.copy(cell.fill),
                "font": copy.copy(cell.font),
            })
    return styles


def apply_style(ws: Any, row: int, style_row: list[Any]) -> None:
    for col, style in enumerate(style_row, start=1):
        cell = ws.cell(row, col)
        cell._style = copy.copy(style["style"])
        cell.number_format = style["number_format"]
        cell.alignment = copy.copy(style["alignment"])
        cell.border = copy.copy(style["border"])
        cell.fill = copy.copy(style["fill"])
        cell.font = copy.copy(style["font"])


def write_workbook(
    output: Path,
    rows: list[dict[str, Any]],
    template: Path | None = None,
    layout: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not template and layout is None:
        layout = load_layout(bundled_layout_path())
    wb = openpyxl.load_workbook(template) if template else create_base_workbook(layout)
    ws = wb.active
    styles = capture_styles(ws, layout)
    if ws.max_row >= 3:
        ws.delete_rows(3, ws.max_row - 2)

    project_groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        key = f"{row['client']}|{row['client_charge_code']}"
        project_groups.setdefault(key, []).append(row)

    output_row = 3
    project_blocks = []
    subtotal_rows = []
    for key, group_rows in project_groups.items():
        group_rows.sort(key=lambda r: (ROW_ORDER.get(r["row_order_type"], 99), r["expense_date"], r["proof_no"]))
        first = output_row
        for row_data in group_rows:
            apply_style(ws, output_row, styles["detail"])
            values = [
                row_data["date"],
                row_data["requester"],
                row_data["client"],
                row_data["client_charge_code"],
                row_data["expenses_nature"],
                row_data["note"],
            ]
            for col_idx, value in enumerate(values, start=1):
                ws.cell(output_row, col_idx).value = value
            for col_name, col_letter in AMOUNT_COLUMNS.items():
                ws[f"{col_letter}{output_row}"] = Decimal(row_data["amount"]) if row_data["amount_column"] == col_name else None
            ws.cell(output_row, 13).value = row_data["proof_no"]
            row_data["excel_row"] = output_row
            output_row += 1
        last = output_row - 1
        subtotal = output_row
        apply_style(ws, subtotal, styles["subtotal"])
        subtotal_label = layout["labels"]["project_subtotal"] if layout else C["summary"]
        ws[f"D{subtotal}"] = subtotal_label
        ws[f"F{subtotal}"] = f"=SUM(G{first}:L{last})"
        subtotal_rows.append(subtotal)
        client, code = key.split("|", 1)
        project_blocks.append({
            "project_key": key,
            "client": client,
            "client_charge_code": code,
            "first_detail_row": first,
            "last_detail_row": last,
            "subtotal_row": subtotal,
            "subtotal_formula": f"SUM(G{first}:L{last})",
        })
        output_row += 1

    last_project_subtotal_row = output_row - 1
    column_summary_row = output_row
    apply_style(ws, column_summary_row, styles["column_summary"])
    for col_letter in AMOUNT_COLUMNS.values():
        ws[f"{col_letter}{column_summary_row}"] = f"=SUM({col_letter}3:{col_letter}{last_project_subtotal_row})"

    total_row = output_row + 1
    apply_style(ws, total_row, styles["total"])
    ws[f"E{total_row}"] = layout["labels"]["total"] if layout else "Total: (RMB)"
    ws[f"F{total_row}"] = f"=SUM(G{column_summary_row}:L{column_summary_row})"

    grand_total_row = output_row + 2
    apply_style(ws, grand_total_row, styles["grand"])
    ws[f"E{grand_total_row}"] = layout["labels"]["grand_total"] if layout else "Grand Total: (RMB)"
    refs = ",".join(f"F{r}" for r in subtotal_rows) or "0"
    ws[f"F{grand_total_row}"] = f"=SUM({refs})"

    status_row = output_row + 3
    apply_style(ws, status_row, styles["status"])
    ws[f"E{status_row}"] = layout["labels"]["status"] if layout else "Status"
    ws[f"F{status_row}"] = f"=F{total_row}=F{grand_total_row}"

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    summary_rows = {
        "column_summary_row": column_summary_row,
        "total_row": total_row,
        "grand_total_row": grand_total_row,
        "status_row": status_row,
    }
    return project_blocks, summary_rows


def build_markdown(payload: dict[str, Any], workbook: Path) -> str:
    lines = [
        "# Final Expense Rows",
        "",
        f"Generated at: {payload['generated_at']}",
        f"Requester: {payload['requester']}",
        f"Workbook: {workbook}",
        "",
        "## Rows",
        "",
        "| Excel Row | Date | Client | Code | Nature | Note | Column | Amount | No. |",
        "| ---: | --- | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for row in payload["rows"]:
        lines.append(
            f"| {row.get('excel_row','')} | {row['date']} | {row['client']} | {row['client_charge_code']} | "
            f"{row['expenses_nature']} | {row['note']} | {row['amount_column']} | {row['amount']} | {row['proof_no']} |"
        )
    lines += ["", "## Project Blocks", "", "| Project | Rows | Subtotal Row | Formula |", "| --- | --- | ---: | --- |"]
    for block in payload["project_blocks"]:
        lines.append(
            f"| {block['project_key']} | {block['first_detail_row']}:{block['last_detail_row']} | "
            f"{block['subtotal_row']} | {block['subtotal_formula']} |"
        )
    lines += [
        "",
        "## Meal Daily Cap Checks",
        "",
        "| Policy | Date | Total | Cap | Over By | Status | Severity | Needs Confirmation |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for check in payload.get("meal_daily_cap_checks", []):
        lines.append(
            f"| {check.get('policy_name', '')} | {check['date']} | {check['total']} | {check['cap']} | {check['over_by']} | "
            f"{check['status']} | {check.get('severity', 'ok')} | {check['requires_user_confirmation']} |"
        )
    lines += [
        "",
        "## Hotel Cap Checks",
        "",
        "| Item | City | Nights | Total | Cap Total | Over By | Status | Severity | Needs Confirmation |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for check in payload.get("hotel_cap_checks", []):
        item = check.get("items", [{}])[0]
        lines.append(
            f"| {item.get('user_no', '')} | {check.get('city', '')} | {check.get('nights', '')} | "
            f"{check['total']} | {check.get('cap_total', '')} | {check['over_by']} | "
            f"{check['status']} | {check.get('severity', 'ok')} | {check['requires_user_confirmation']} |"
        )
    return "\n".join(lines) + "\n"


def aggregate_check_status(checks: list[dict[str, Any]]) -> str:
    if any(check.get("requires_user_confirmation") for check in checks):
        return "needs_confirmation"
    if any(check.get("severity") == "advisory" or check.get("advisory") for check in checks):
        return "advisory"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write reimbursement workbook from stage-2 allocation JSON.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json.")
    parser.add_argument(
        "--template",
        help="Optional reimbursement template .xlsx, or 'bundled' for assets/reimbursement-template.xlsx. If omitted, the workbook is generated directly by script.",
    )
    parser.add_argument(
        "--layout",
        help="Optional generated-workbook layout TOML. Defaults to assets/reimbursement-workbook-layout.toml when no template is supplied.",
    )
    parser.add_argument("--output", required=True, help="Output .xlsx path.")
    parser.add_argument("--requester", required=True, help="Requester name.")
    parser.add_argument("--process-dir", default="process", help="Folder for final-expense-rows outputs.")
    parser.add_argument("--allow-unconfirmed", action="store_true", help="Allow writing with open questions/unconfirmed units.")
    args = parser.parse_args(argv)

    allocation_path = Path(args.allocation)
    template_path = resolve_template_arg(args.template)
    if template_path and not template_path.exists():
        print(
            f"ERROR: Template workbook not found: {template_path}. "
            "Pass an existing --template path or omit --template to generate the workbook directly.",
            file=sys.stderr,
        )
        return 2
    layout_path = resolve_layout_arg(args.layout)
    layout = None
    if not template_path or args.layout:
        try:
            layout = load_layout(layout_path)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            print(f"ERROR: Workbook layout could not be loaded from {layout_path}: {exc}", file=sys.stderr)
            return 2

    allocation = load_json(allocation_path)
    errors = require_ready(allocation, args.allow_unconfirmed)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    units = included_units(allocation)
    proof_groups = assign_proof_numbers(units)
    rows = make_rows(units, args.requester)
    rows.sort(key=lambda r: (r["client"], r["client_charge_code"], ROW_ORDER.get(r["row_order_type"], 99), r["expense_date"], r["proof_no"]))
    meal_checks = meal_daily_cap_checks(rows)
    hotel_checks = hotel_cap_checks(rows)
    project_blocks, summary_rows = write_workbook(Path(args.output), rows, template_path, layout)
    meal_check_status = aggregate_check_status(meal_checks)
    hotel_check_status = aggregate_check_status(hotel_checks)

    payload = {
        "schema_version": "final_expense_rows.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "requester": args.requester,
        "source_allocation_file": str(allocation_path),
        "workbook_source": "template" if template_path else "generated",
        "template_workbook": str(template_path) if template_path else "",
        "layout_file": str(layout_path) if layout else "",
        "workbook": str(Path(args.output)),
        "proof_groups": [{k: v for k, v in group.items() if k != "units"} for group in proof_groups],
        "rows": rows,
        "project_blocks": project_blocks,
        "summary_rows": summary_rows,
        "meal_daily_cap_checks": meal_checks,
        "hotel_cap_checks": hotel_checks,
        "checks": [
            {
                "name": "meal_daily_caps",
                "caps": {
                    "business_trip_meal": money(BUSINESS_TRIP_MEAL_DAILY_CAP),
                    "local_overtime_meal": money(LOCAL_OVERTIME_MEAL_DAILY_CAP),
                },
                "status": meal_check_status,
                "days_checked": len(meal_checks),
                "days_with_advisory": sum(1 for check in meal_checks if check.get("severity") == "advisory"),
                "days_requiring_confirmation": sum(1 for check in meal_checks if check["requires_user_confirmation"]),
            },
            {
                "name": "hotel_caps",
                "caps": {
                    "first_tier_city_per_night": money(FIRST_TIER_HOTEL_CAP),
                    "other_city_per_night": money(OTHER_CITY_HOTEL_CAP),
                },
                "status": hotel_check_status,
                "items_checked": len(hotel_checks),
                "items_with_advisory": sum(1 for check in hotel_checks if check.get("severity") == "advisory"),
                "items_requiring_confirmation": sum(1 for check in hotel_checks if check["requires_user_confirmation"]),
            }
        ],
    }
    process_dir = Path(args.process_dir)
    write_json(process_dir / "final-expense-rows.json", payload)
    (process_dir / "final-expense-rows.md").write_text(build_markdown(payload, Path(args.output)), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Wrote {process_dir / 'final-expense-rows.json'}")
    print(f"Wrote {process_dir / 'final-expense-rows.md'}")
    print_meal_cap_check(meal_checks)
    print_hotel_cap_check(hotel_checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
