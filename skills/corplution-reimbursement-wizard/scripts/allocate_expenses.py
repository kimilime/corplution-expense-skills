#!/usr/bin/env python3
"""Create stage-2 allocation units and question queues from extracted invoices."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Policy numbers and year-coded charge codes come from assets/policy.toml;
# edit that file (not this one) when company policy changes.
from policy_config import load_policy
import integrity

_POLICY = load_policy()


C = {
    "local": "\u672c\u5730",
    "trip": "\u51fa\u5dee",
    "company": "\u516c\u53f8",
    "airport": "\u673a\u573a",
    "railway_station": "\u706b\u8f66\u7ad9",
    "hotel": "\u9152\u5e97",
    "client": "\u5ba2\u6237",
    "home": "\u5bb6",
    "restaurant": "\u9910\u5385",
    "gaode": "\u9ad8\u5fb7",
    "didi": "\u6ef4\u6ef4",
    "admin": "Admin",
    "admin_code": _POLICY.admin_code,
    "admin_fallback_client": "\u9879\u76ee\u3001\u8c03\u7814\u4ee5\u5916\u7684\u5176\u4ed6\u8d39\u7528",
    "travel_meal": "\u51fa\u5dee\u9910\u8d39",
    "station_meal": "\u51fa\u5dee\u9910\u8d39\uff08\u9ad8\u94c1\u7ad9/\u673a\u573a\uff09",
    "overtime_meal": "\u52a0\u73ed\u9910\u8d39",
    "taxi": "\u6253\u8f66",
    "flight": "\u98de\u673a",
    "rail": "\u9ad8\u94c1",
    "flight_refund": "\u98de\u673a\u9000\u7968\u8d39",
    "rail_refund": "\u9ad8\u94c1\u9000\u7968\u8d39",
    "hotel_note": "\u51fa\u5dee\u9152\u5e97",
    "mobile": "\u901a\u8baf\u8d39",
    "substitute": "\uff08\u62b5\uff09",
}


EXPENSE_ORDER = {
    "flight": 1,
    "rail": 1,
    "railway_e_ticket": 1,
    "hotel": 2,
    "taxi": 3,
    "didi": 3,
    "gaode": 4,
    "meal": 5,
    "mobile": 6,
    "other": 7,
    "travel": 8,
    "unknown": 99,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def is_admin_code(value: Any) -> bool:
    return clean(value).upper() == C["admin_code"]


def non_admin_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ctx for ctx in contexts if not is_admin_code(ctx.get("client_charge_code"))]


def is_mobile_admin_unit(unit: dict[str, Any]) -> bool:
    return unit.get("source_category") == "mobile" or unit.get("final_template_column") == "mobile"


def mobile_accounting_errors(unit: dict[str, Any]) -> list[str]:
    source_category = clean(unit.get("source_category"))
    final_column = clean(unit.get("final_template_column"))
    client = clean(unit.get("client_name"))
    note = clean(unit.get("final_note") or unit.get("expense_note") or unit.get("source_note"))
    errors: list[str] = []
    if source_category != "mobile":
        if final_column == "mobile":
            errors.append("non-mobile expense cannot use the mobile amount column")
        if client == C["mobile"]:
            errors.append("non-mobile expense cannot use Client = 通讯费")
        if C["mobile"] in note:
            errors.append("non-mobile expense cannot use a 通讯费 note")
    project_expense_categories = {"hotel", "meal", "taxi", "travel"}
    if source_category in project_expense_categories or final_column in project_expense_categories:
        if is_admin_code(unit.get("client_charge_code")):
            errors.append(f"project expenses cannot be assigned to {_POLICY.admin_code}")
    return errors


def note_placeholder_errors(unit: dict[str, Any]) -> list[str]:
    note = clean(unit.get("final_note"))
    errors: list[str] = []
    if "\u51fa\u53d1\u5730\u7c7b\u578b" in note or "\u76ee\u7684\u5730\u7c7b\u578b" in note:
        errors.append("taxi note still contains place-type placeholders")
    if clean(unit.get("source_category")) in {"taxi", "travel"} and clean(unit.get("origin")):
        if not clean(unit.get("origin_place_type")) or not clean(unit.get("destination_place_type")):
            errors.append("taxi note requires confirmed origin and destination place types")
    return errors


def normalize_admin_client(unit: dict[str, Any]) -> None:
    if not is_admin_code(unit.get("client_charge_code")):
        return
    client = clean(unit.get("client_name"))
    placeholder = client.lower() in {"", "admin", C["admin_code"].lower()}
    if is_mobile_admin_unit(unit):
        if placeholder or client == C["admin_fallback_client"]:
            unit["client_name"] = C["mobile"]
        unit["admin_client_review_needed"] = False
        return
    if placeholder:
        unit["client_name"] = C["admin_fallback_client"]
        unit["admin_client_review_needed"] = True
    elif client == C["admin_fallback_client"]:
        unit["admin_client_review_needed"] = True
    elif client == C["mobile"] and not is_mobile_admin_unit(unit):
        unit["client_name"] = C["admin_fallback_client"]
        unit["admin_client_review_needed"] = True
    else:
        unit["admin_client_review_needed"] = False


def money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(str(value).replace(',', '')):.2f}"
    except InvalidOperation:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return f"{Decimal(match.group(0)):.2f}" if match else ""


def parse_date(value: str) -> date | None:
    if not value:
        return None
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if not match:
        match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def date_key(value: str) -> str:
    d = parse_date(value)
    return d.isoformat() if d else ""


def first_date_in_text(value: Any) -> str:
    return date_key(clean(value))


def billing_month_end(billing_period: str, issue_date: str = "") -> tuple[str, str]:
    period = clean(billing_period)
    match = re.search(r"(\d{4})(\d{2})", period)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        if 1 <= month <= 12:
            day = calendar.monthrange(year, month)[1]
            return date(year, month, day).isoformat(), "mobile_billing_period_month_end"
    issued = parse_date(issue_date)
    if issued:
        day = calendar.monthrange(issued.year, issued.month)[1]
        return date(issued.year, issued.month, day).isoformat(), "mobile_issue_month_end"
    return "", "needs_user_date"


def hotel_stay_dates(classification: dict[str, Any], invoice: dict[str, Any], source_note: str) -> tuple[str, str]:
    check_in = ""
    check_out = ""
    for key in ["check_in_date", "hotel_check_in_date", "stay_start", "date_start"]:
        check_in = first_date_in_text(classification.get(key) or invoice.get(key))
        if check_in:
            break
    for key in ["check_out_date", "hotel_check_out_date", "stay_end", "date_end"]:
        check_out = first_date_in_text(classification.get(key) or invoice.get(key))
        if check_out:
            break
    if check_in and check_out:
        return check_in, check_out
    dates = re.findall(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", source_note or "")
    normalized = [date_key(item) for item in dates]
    normalized = [item for item in normalized if item]
    if len(normalized) >= 2:
        return normalized[0], normalized[1]
    return check_in, check_out


def reliable_invoice_expense_date(
    *,
    source_category: str,
    subtype: str,
    classification: dict[str, Any],
    invoice: dict[str, Any],
    source_note: str,
    billing_period: str,
    check_in_date: str,
    check_out_date: str,
) -> tuple[str, str, bool, str]:
    issue_date = date_key(invoice.get("issue_date", ""))
    class_date = date_key(classification.get("expense_date", ""))
    date_source = clean(classification.get("expense_date_source") or classification.get("date_source"))
    note_date = first_date_in_text(source_note)

    if source_category == "mobile":
        value, source = billing_month_end(billing_period, issue_date)
        return value, source, not bool(value), "通讯费按账期或开票月份的最后一天填 Date。"

    if source_category == "hotel":
        if check_in_date and check_out_date:
            return check_out_date, "hotel_check_out_date", False, "酒店发票有入住/离店日期，Date 使用离店日期。"
        return "", "needs_user_date", True, "酒店没有可靠入住/离店日期，不能用开票日期代替。"

    if subtype == "railway_e_ticket":
        if date_source in {"railway_travel_date", "travel_date"} and class_date:
            return class_date, date_source, False, "高铁/铁路电子客票使用票面乘车日期。"
        if note_date:
            return note_date, "railway_note_travel_date", False, "高铁/铁路电子客票从票面备注识别到乘车日期。"
        if class_date and class_date != issue_date:
            return class_date, "railway_classification_date", False, "高铁/铁路电子客票识别日期不同于开票日期，按乘车日期处理。"
        return "", "needs_user_date", True, "高铁/铁路电子客票未可靠识别乘车日期，不能用开票日期代替。"

    if source_category == "travel":
        if date_source in {"flight_date", "travel_date", "railway_travel_date"} and class_date:
            return class_date, date_source, False, "机票/交通票据使用票面出行日期。"
        if note_date:
            return note_date, "travel_note_date", False, "机票/交通票据备注中识别到出行日期。"
        return "", "needs_user_date", True, "机票/交通票据没有可靠出行日期，不能用开票日期代替。"

    if source_category == "other":
        if issue_date:
            return issue_date, "other_invoice_issue_date_provisional", False, "其他费用暂用开票日期作为 Date；请提示用户复核，不作为阻塞项。"
        return "", "needs_user_date", True, "其他费用没有可用开票日期，需要用户确认记账日期。"

    return "", "needs_user_date", True, "普通发票开票日期不能直接作为报销发生日期。"


PROJECT_CONTEXT_SCHEMA_VERSION = "project_context.v1"
PROJECT_CONTEXT_REQUIRED_FIELDS = {
    "date_start",
    "date_end",
    "city",
    "client_name",
    "client_charge_code",
}
PROJECT_CONTEXT_OPTIONAL_FIELDS = {
    "context_id",
    "project_description",
    "user_notes",
    "project_scope",
    "travel_buffer_days",
    "status",
    "meal_hints",
    "expense_hints",
}
PROJECT_CONTEXT_FIELDS = PROJECT_CONTEXT_REQUIRED_FIELDS | PROJECT_CONTEXT_OPTIONAL_FIELDS
PROJECT_CONTEXT_ROOT_FIELDS = {"schema_version", "project_contexts"}


def project_context_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "project-context-template.json"


def context_schema_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root must be an object with schema_version and project_contexts; root arrays are not accepted"]

    errors: list[str] = []
    unknown_root = sorted(set(payload) - PROJECT_CONTEXT_ROOT_FIELDS)
    if unknown_root:
        errors.append(
            "unsupported root field(s): " + ", ".join(unknown_root)
            + "; use only schema_version and project_contexts"
        )
    if payload.get("schema_version") != PROJECT_CONTEXT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PROJECT_CONTEXT_SCHEMA_VERSION!r}")

    contexts = payload.get("project_contexts")
    if not isinstance(contexts, list) or not contexts:
        alias_note = " Found 'projects'; rename it to 'project_contexts'." if "projects" in payload else ""
        errors.append("project_contexts must be a non-empty array." + alias_note)
        return errors

    seen_context_ids: set[str] = set()
    for index, context in enumerate(contexts, start=1):
        label = f"project_contexts[{index - 1}]"
        if not isinstance(context, dict):
            errors.append(f"{label} must be an object")
            continue
        unknown_fields = sorted(set(context) - PROJECT_CONTEXT_FIELDS)
        if unknown_fields:
            errors.append(
                f"{label} unsupported field(s): {', '.join(unknown_fields)}; "
                f"allowed fields: {', '.join(sorted(PROJECT_CONTEXT_FIELDS))}"
            )
        missing = sorted(
            field for field in PROJECT_CONTEXT_REQUIRED_FIELDS
            if not clean(context.get(field))
        )
        if missing:
            errors.append(f"{label} missing required canonical field(s): {', '.join(missing)}")
        context_id = clean(context.get("context_id"))
        if context_id:
            if context_id in seen_context_ids:
                errors.append(f"{label}.context_id duplicates {context_id!r}")
            seen_context_ids.add(context_id)
        for field in PROJECT_CONTEXT_REQUIRED_FIELDS | {"project_description", "context_id"}:
            value = clean(context.get(field))
            if value.startswith("<") and value.endswith(">"):
                errors.append(f"{label}.{field} still contains template placeholder {value!r}")
        start_text = clean(context.get("date_start"))
        end_text = clean(context.get("date_end"))
        start = parse_date(start_text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_text) else None
        end = parse_date(end_text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_text) else None
        if start_text and start is None:
            errors.append(f"{label}.date_start must be YYYY-MM-DD")
        if end_text and end is None:
            errors.append(f"{label}.date_end must be YYYY-MM-DD")
        if start and end and end < start:
            errors.append(f"{label}.date_end cannot be earlier than date_start")
        try:
            buffer_days = int(context.get("travel_buffer_days", 1))
            if buffer_days < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label}.travel_buffer_days must be a non-negative integer")
        for hints_field in ("meal_hints", "expense_hints"):
            hints = context.get(hints_field, [])
            if not isinstance(hints, list) or any(not isinstance(item, dict) for item in hints):
                errors.append(f"{label}.{hints_field} must be an array of objects")
    return errors


def load_context(path: Path | None) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    if not path:
        return [], "", [{
            "question_id": "Q-CONTEXT-001",
            "unit_ids": [],
            "question": "请提供本次报销周期内的项目上下文：日期范围、城市、客户名称、项目编号、项目描述。",
            "why_it_matters": "没有项目上下文时，只能生成费用清单，不能可靠匹配 Client 和 Client Charge Code。",
            "status": "open",
        }]
    if path.suffix.lower() != ".json":
        raise ValueError(
            "project context must be canonical UTF-8 JSON; convert natural-language notes internally "
            "instead of passing a text file to allocation"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"project context JSON cannot be read: {exc}") from exc
    errors = context_schema_errors(payload)
    if errors:
        raise ValueError("invalid project context schema:\n- " + "\n- ".join(errors))

    normalized = []
    for idx, ctx in enumerate(payload["project_contexts"], start=1):
        item = dict(ctx)
        item.setdefault("context_id", f"CTX-{idx:03d}")
        item.setdefault("project_description", "")
        item.setdefault("user_notes", "")
        item.setdefault("travel_buffer_days", 1)
        item.setdefault("status", "draft")
        item.setdefault("meal_hints", [])
        item.setdefault("expense_hints", [])
        normalized.append(item)
    return normalized, "", []


def doc_by_id(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {doc["document_id"]: doc for doc in extraction.get("documents", [])}


def ride_schedule_links(extraction: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    invoice_to_schedule: dict[str, str] = {}
    schedule_to_invoice: dict[str, str] = {}
    for link in extraction.get("document_links", []):
        if link.get("relation") in {"invoice_total_matches_didi_trip_report", "invoice_total_matches_gaode_trip_report"}:
            invoice_to_schedule[link.get("source_document_id", "")] = link.get("target_document_id", "")
            schedule_to_invoice[link.get("target_document_id", "")] = link.get("source_document_id", "")
    return invoice_to_schedule, schedule_to_invoice


def source_file(doc: dict[str, Any] | None) -> str:
    return (doc or {}).get("source_file", "")


def source_filename(doc: dict[str, Any] | None) -> str:
    value = source_file(doc)
    return Path(value).name if value else ""


def infer_city_from_text(text: str) -> str:
    for city in [
        "\u4e0a\u6d77", "\u5317\u4eac", "\u592a\u539f", "\u90d1\u5dde", "\u5e7f\u5dde", "\u6df1\u5733",
        "\u676d\u5dde", "\u5357\u4eac", "\u82cf\u5dde", "\u6b66\u6c49", "\u6210\u90fd", "\u91cd\u5e86",
        "\u897f\u5b89", "\u9752\u5c9b", "\u6d4e\u5357", "\u957f\u6c99", "\u5408\u80a5",
    ]:
        if city in text:
            return city
    return ""


def final_column(source_category: str, city: str = "") -> str:
    if source_category == "hotel":
        return "hotel"
    if source_category == "mobile":
        return "mobile"
    if source_category == "meal":
        return "travel" if city and "\u4e0a\u6d77" not in city else "meal"
    if source_category == "taxi":
        if not city or "\u4e0a\u6d77" in city:
            return "taxi"
        return "travel"
    if source_category == "travel":
        return "travel"
    if source_category in {"other", "unknown"}:
        return "other"
    return source_category or "other"


def formal_meal_column(unit: dict[str, Any]) -> str:
    if clean(unit.get("source_category")) != "meal":
        return clean(unit.get("final_template_column"))
    city = clean(unit.get("city"))
    if city and "\u4e0a\u6d77" in city:
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


def classify_place_type(place: str, contexts: list[dict[str, Any]]) -> tuple[str, str, bool]:
    text = clean(place)
    if not text:
        return "", "low", True
    if any(k in text for k in ["\u6c5f\u5b81\u8def", "\u53cb\u529b\u56fd\u9645\u5927\u53a6"]):
        return C["company"], "high", False
    if any(k in text for k in ["\u673a\u573a", "\u822a\u7ad9\u697c", "T1", "T2", "T3", "3F", "\u51fa\u53d1", "\u5230\u8fbe"]):
        return C["airport"], "high", False
    if any(k in text for k in ["\u706b\u8f66\u7ad9", "\u9ad8\u94c1\u7ad9", "\u8f66\u7ad9", "\u8679\u6865\u7ad9"]):
        return C["railway_station"], "high", False
    if any(k in text for k in ["\u9152\u5e97", "\u5bbe\u9986", "\u4e9a\u6735", "\u5168\u5b63", "\u559c\u6765\u767b", "\u6c49\u5ead"]):
        return C["hotel"], "high", False
    for ctx in contexts:
        client = clean(ctx.get("client_name"))
        if client and client in text:
            return C["client"], "high", False
    if any(k in text for k in ["\u5927\u53a6", "\u4e2d\u5fc3", "\u56ed", "\u94f6\u884c", "\u4fe1\u6258", "\u4fdd\u9669", "\u8bc1\u5238", "\u516c\u53f8"]):
        return C["client"], "medium", True
    return "", "low", True


def route_from_note(note: str) -> str:
    match = re.search(r"([^,，]+?)\s*->\s*([^,，]+)", note or "")
    return f"{clean(match.group(1))}-{clean(match.group(2))}" if match else ""


def is_refund_fee(unit: dict[str, Any]) -> bool:
    text = clean(" ".join([
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("raw_remarks", ""),
        unit.get("line_item_name", ""),
        unit.get("seller_name", ""),
    ]))
    return any(keyword in text for keyword in ["退票费", "退票", "退款", "refund", "Refund", "cancellation"])


def normal_note(unit: dict[str, Any]) -> str:
    category = unit.get("source_category", "")
    subtype = unit.get("document_subtype", "")
    source = clean(unit.get("source_note"))
    if subtype == "railway_e_ticket" or "G" in source[:12]:
        route = route_from_note(source) or clean(unit.get("route"))
        note_type = C["rail_refund"] if is_refund_fee(unit) else C["rail"]
        return f"{note_type}\uff08{route}\uff09" if route else note_type
    if category == "hotel":
        nights = unit.get("hotel_nights") or "X"
        checkin = unit.get("check_in_date") or "\u5165\u4f4f\u65e5"
        checkout = unit.get("check_out_date") or "\u79bb\u5e97\u65e5"
        return f"{C['hotel_note']}\uff08{nights}\u665a\uff0c{checkin}-{checkout}\uff09"
    if category == "mobile":
        period = clean(unit.get("billing_period"))
        if period and len(period) >= 6 and period[:4].isdigit() and period[4:6].isdigit():
            return f"{int(period[4:6])}\u6708{C['mobile']}"
        return f"X\u6708{C['mobile']}"
    if category == "meal":
        if unit.get("meal_context") == "overtime":
            return C["overtime_meal"]
        if unit.get("meal_context") == "station_airport":
            return C["station_meal"]
        return C["travel_meal"]
    if category in {"taxi", "travel"} and unit.get("origin"):
        origin_type = clean(unit.get("origin_place_type"))
        dest_type = clean(unit.get("destination_place_type"))
        if not origin_type or not dest_type:
            return source
        suffix = "\uff08\u52a0\u73ed\uff09" if unit.get("business_reason") == "overtime" else ""
        return f"{C['taxi']}\uff08{origin_type}-{dest_type}\uff09{suffix}"
    return source


def print_document_reconciliation(extraction: dict[str, Any], units: list[dict[str, Any]]) -> None:
    """Every extraction document must land in exactly one bucket. If any is
    unaccounted for, that is a pipeline bug — say so loudly."""
    docs = extraction.get("documents", [])
    _, schedule_to_invoice = ride_schedule_links(extraction)
    linked_invoices = set(schedule_to_invoice.values())
    unit_doc_ids = {u.get("source_doc_id") for u in units if u.get("source_doc_id")}
    excluded = [d for d in docs if d.get("excluded_by_user")]
    supporting = [d for d in docs if not d.get("excluded_by_user")
                  and d.get("document_role") == "supporting_document"]
    unknown_units = [u for u in units if u.get("source_category") == "unknown"]
    accounted = set()
    for d in docs:
        did = d["document_id"]
        if d.get("excluded_by_user") or did in unit_doc_ids or did in linked_invoices            or d.get("document_role") in {"supporting_document", "supporting_schedule"}:
            accounted.add(did)
    unaccounted = [d for d in docs if d["document_id"] not in accounted]
    print("")
    print("DOCUMENT RECONCILIATION TO SHOW IN CHAT:")
    print(f"- Extraction documents: {len(docs)} = expense units/linked {len(docs) - len(excluded) - len(supporting) - len(unaccounted)}"
          f" + supporting documents {len(supporting)} + excluded by user {len(excluded)}")
    for d in excluded:
        print(f"  * excluded: {Path(str(d.get('source_file',''))).name} ({d.get('exclusion_reason','')})")
    if unknown_units:
        print(f"- Unidentified documents held as BLOCKING questions: {len(unknown_units)} (resolve via chat + apply_extraction_corrections.py)")
    if unaccounted:
        print(f"ERROR: {len(unaccounted)} document(s) unaccounted for — this is a bug, do not proceed:")
        for d in unaccounted:
            print(f"  * {d['document_id']}: {Path(str(d.get('source_file',''))).name} (role={d.get('document_role')})")


def create_units(extraction: dict[str, Any], contexts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs = doc_by_id(extraction)
    invoice_to_schedule, schedule_to_invoice = ride_schedule_links(extraction)
    units: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    unit_idx = 1

    for doc in extraction.get("documents", []):
        doc_id = doc["document_id"]
        if doc.get("excluded_by_user"):
            continue
        role = doc.get("document_role")
        subtype = doc.get("document_subtype")
        classification = doc.get("classification") or {}
        invoice = doc.get("invoice") or {}

        if role == "invoice" and doc_id in invoice_to_schedule:
            continue

        if subtype in {"didi_trip_report", "gaode_trip_report"}:
            provider = C["gaode"] if subtype == "gaode_trip_report" else C["didi"]
            linked_invoice_id = schedule_to_invoice.get(doc_id, "")
            linked_invoice_doc = docs.get(linked_invoice_id, {})
            if not linked_invoice_id:
                questions.append({
                    "question_id": f"Q-LINK-{doc_id}",
                    "unit_ids": [],
                    "question": f"{doc_id} 是{provider}行程单，但没有匹配到发票。请补发票，或确认这份行程单不报销/删除。",
                    "why_it_matters": "拆行程需要对应发票作为财务凭证。",
                    "status": "open",
                })
            for item in doc.get("supporting_items", []):
                city = clean(item.get("city"))
                source_category = item.get("expense_category") or "taxi"
                col = final_column(source_category, city)
                origin_type, origin_conf, origin_need = classify_place_type(item.get("origin", ""), contexts)
                dest_type, dest_conf, dest_need = classify_place_type(item.get("destination", ""), contexts)
                unit = {
                    "unit_id": f"UNIT-{unit_idx:03d}",
                    "user_no": unit_idx,
                    "source_document_id": doc_id,
                    "source_file": source_file(doc),
                    "source_filename": source_filename(doc),
                    "source_item_id": item.get("item_id"),
                    "supporting_invoice_document_id": linked_invoice_id,
                    "supporting_invoice_file": source_file(linked_invoice_doc),
                    "supporting_invoice_filename": source_filename(linked_invoice_doc),
                    "supporting_schedule_document_id": doc_id,
                    "supporting_schedule_file": source_file(doc),
                    "supporting_schedule_filename": source_filename(doc),
                    "invoice_no": (docs.get(linked_invoice_id, {}).get("invoice") or {}).get("invoice_no", ""),
                    "amount": money(item.get("amount")),
                    "invoice_amount": money(item.get("amount")),
                    "reimbursable_amount": "",
                    "issue_date": (docs.get(linked_invoice_id, {}).get("invoice") or {}).get("issue_date", ""),
                    "expense_date": date_key(item.get("ride_datetime", "")),
                    "date_source": "trip_report_ride_datetime" if date_key(item.get("ride_datetime", "")) else "needs_user_date",
                    "date_is_provisional": False,
                    "date_required": not bool(date_key(item.get("ride_datetime", ""))),
                    "date_question_reason": "滴滴/高德行程单行程时间是可靠发生日期；未识别到行程时间时需要用户补充。",
                    "source_category": source_category,
                    "final_template_column": col,
                    "city": city,
                    "route": "",
                    "origin": item.get("origin", ""),
                    "destination": item.get("destination", ""),
                    "origin_place_type": origin_type,
                    "destination_place_type": dest_type,
                    "place_type_confidence": "low" if origin_need or dest_need else "high",
                    "place_type_needs_confirmation": bool(origin_need or dest_need),
                    "seller_name": provider,
                    "project_context_id": "",
                    "client_name": "",
                    "client_charge_code": "",
                    "expenses_nature": C["local"] if "\u4e0a\u6d77" in city else C["trip"],
                    "source_note": item.get("expense_note", ""),
                    "expense_note": item.get("expense_note", ""),
                    "final_note": "",
                    "attendees": "",
                    "is_substitute_invoice": False,
                    "substitute_for": "",
                    "approval_required": "",
                    "approval_file": "",
                    "approval_file_status": "",
                    "confidence": "low",
                    "match_reason": "",
                    "status": "needs_confirmation" if origin_need or dest_need else "draft",
                    "issues": [],
                }
                normalize_meal_column(unit)
                normalize_taxi_column(unit)
                unit["final_note"] = normal_note(unit)
                units.append(unit)
                unit_idx += 1
            continue

        if role == "invoice":
            source_category = classification.get("expense_category", "unknown")
            text_for_city = " ".join([
                invoice.get("seller_name", ""),
                invoice.get("raw_remarks", ""),
                classification.get("expense_note", ""),
            ])
            city = infer_city_from_text(text_for_city)
            col = final_column(source_category, city)
            source_note = classification.get("expense_note", "")
            billing_period = ""
            match = re.search(r"(\d{6})", invoice.get("raw_remarks", "") + " " + source_note)
            if source_category == "mobile" and match:
                billing_period = match.group(1)
            check_in_date, check_out_date = hotel_stay_dates(classification, invoice, source_note)
            expense_date, date_source, date_required, date_question_reason = reliable_invoice_expense_date(
                source_category=source_category,
                subtype=subtype,
                classification=classification,
                invoice=invoice,
                source_note=source_note,
                billing_period=billing_period,
                check_in_date=check_in_date,
                check_out_date=check_out_date,
            )
            unit = {
                "unit_id": f"UNIT-{unit_idx:03d}",
                "user_no": unit_idx,
                "source_document_id": doc_id,
                "source_file": source_file(doc),
                "source_filename": source_filename(doc),
                "source_item_id": None,
                "supporting_invoice_document_id": doc_id,
                "supporting_invoice_file": source_file(doc),
                "supporting_invoice_filename": source_filename(doc),
                "supporting_schedule_document_id": "",
                "supporting_schedule_file": "",
                "supporting_schedule_filename": "",
                "invoice_no": invoice.get("invoice_no", ""),
                "amount": money(invoice.get("total_amount")),
                "invoice_amount": money(invoice.get("total_amount")),
                "reimbursable_amount": "",
                "issue_date": invoice.get("issue_date", ""),
                "expense_date": expense_date,
                "date_source": date_source,
                "date_is_provisional": date_source == "other_invoice_issue_date_provisional",
                "date_required": date_required,
                "date_question_reason": date_question_reason,
                "source_category": source_category,
                "document_subtype": subtype,
                "final_template_column": col,
                "city": city,
                "route": route_from_note(source_note),
                "origin": "",
                "destination": "",
                "origin_place_type": "",
                "destination_place_type": "",
                "place_type_confidence": "",
                "place_type_needs_confirmation": False,
                "seller_name": invoice.get("seller_name", ""),
                "line_item_name": invoice.get("line_item_name", ""),
                "raw_remarks": invoice.get("raw_remarks", ""),
                "project_context_id": "",
                "client_name": "",
                "client_charge_code": "",
                "expenses_nature": C["local"] if "\u4e0a\u6d77" in city else C["trip"],
                "source_note": source_note,
                "expense_note": source_note,
                "final_note": "",
                "billing_period": billing_period,
                "attendees": "",
                "hotel_city": city if source_category == "hotel" else "",
                "hotel_city_tier": "",
                "hotel_nights": "",
                "check_in_date": check_in_date,
                "check_out_date": check_out_date,
                "shared_room": False,
                "room_shared_with": "",
                "room_share_note": "",
                "is_substitute_invoice": False,
                "substitute_for": "",
                "approval_required": "",
                "approval_file": "",
                "approval_file_status": "",
                "confidence": "low",
                "match_reason": "",
                "status": "draft",
                "issues": [],
            }
            normalize_meal_column(unit)
            normalize_taxi_column(unit)
            unit["final_note"] = normal_note(unit)
            units.append(unit)
            unit_idx += 1
            continue

        if role == "supporting_document":
            # Legitimate evidence with no expense row of its own (approval
            # screenshots, payment receipts); packaging picks it up later.
            continue

        # Catch-all: every document the pipeline does not recognize becomes a
        # BLOCKING question unit instead of silently vanishing. A file is
        # evidence until the user explicitly drops it — it may be an invoice
        # that needs OCR/vision, a partner approval screenshot, or a payment
        # receipt (paper slip / Alipay / WeChat screenshot).
        filename = Path(str(doc.get("source_file", ""))).name
        amount = clean((doc.get("invoice") or {}).get("total_amount"))
        unit = {
            "unit_id": f"UNIT-{unit_idx:03d}",
            "unit_no": unit_idx,
            "source_doc_id": doc_id,
            "source_filename": filename,
            "source_category": "unknown",
            "amount": amount,
            "amount_column": "",
            "status": "open",
            "confidence": "low",
            "match_reason": "document_role could not be determined automatically",
            "issues": ["unidentified_document"],
            "final_note": "",
        }
        units.append(unit)
        questions.append({
            "question_id": f"Q-UNKNOWN-{doc_id}",
            "unit_ids": [unit["unit_id"]],
            "question": (
                f"第{unit_idx}项（{filename}）无法自动识别，请确认它是什么："
                "① 发票（图片/扫描件，需要人工或视觉识别，请提供发票号码、开票日期、销售方、金额）；"
                "② 合伙人审批截图等审批凭证；"
                "③ 付款凭证（小票、支付宝/微信支付截图等支持文档）；"
                "④ 其他，请说明；"
                "或明确回复不报销/排除（需说明原因）。"
                "确认后通过 apply_extraction_corrections.py 写回，再重跑 allocation。"
            ),
            "why_it_matters": "未识别的文件默认是报销证据；不确认或不排除就无法进入报销表，也无法通过 stage 3 preflight。",
            "status": "open",
        })
        unit_idx += 1

    return units, questions


def date_in_context(unit_date: str, ctx: dict[str, Any]) -> bool:
    d = parse_date(unit_date)
    start = parse_date(ctx.get("date_start", ""))
    end = parse_date(ctx.get("date_end", ""))
    if not d or not start or not end:
        return False
    buffer = int(ctx.get("travel_buffer_days") or 0)
    return start - timedelta(days=buffer) <= d <= end + timedelta(days=buffer)


def context_start(ctx: dict[str, Any]) -> date | None:
    return parse_date(ctx.get("date_start", ""))


def context_end(ctx: dict[str, Any]) -> date | None:
    return parse_date(ctx.get("date_end", ""))


def date_range_overlaps_context(start: date | None, end: date | None, ctx: dict[str, Any]) -> bool:
    ctx_start = context_start(ctx)
    ctx_end = context_end(ctx)
    if not start or not end or not ctx_start or not ctx_end:
        return False
    if end < start:
        start, end = end, start
    return start <= ctx_end and end >= ctx_start


def city_key(value: Any) -> str:
    return clean(value).lower().replace("\u5e02", "")


def is_shanghai_city(value: Any) -> bool:
    text = clean(value).lower()
    return "\u4e0a\u6d77" in text or "shanghai" in text


def contains_text(text: str, needle: str) -> bool:
    if not needle:
        return False
    return needle in text or needle.lower() in text.lower()


def unit_match_text(unit: dict[str, Any]) -> str:
    return clean(" ".join([
        clean(unit.get("city")),
        clean(unit.get("route")),
        clean(unit.get("origin")),
        clean(unit.get("destination")),
        clean(unit.get("source_note")),
        clean(unit.get("expense_note")),
        clean(unit.get("final_note")),
        clean(unit.get("raw_remarks")),
        clean(unit.get("seller_name")),
    ]))


def city_matches_unit(unit: dict[str, Any], ctx: dict[str, Any]) -> bool:
    ctx_city = clean(ctx.get("city"))
    if not ctx_city:
        return False
    return contains_text(unit_match_text(unit), ctx_city)


def list_context_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_terms = value
    else:
        raw_terms = re.split(r"[,，、;/；\s]+", clean(value))
    terms: list[str] = []
    stop_terms = {
        "admin", "corp", "bd", "project", "local",
        "上海", "上海市", "本地", "本地项目", "项目", "客户",
    }
    for raw in raw_terms:
        term = clean(raw)
        if len(term) >= 2 and term.lower() not in stop_terms and term not in stop_terms:
            terms.append(term)
    return terms


def is_local_context(ctx: dict[str, Any]) -> bool:
    marker = clean(ctx.get("project_scope") or ctx.get("scope") or ctx.get("project_type")).lower()
    if marker in {"local", "local_project", "本地", "本地项目"}:
        return True
    return is_shanghai_city(ctx.get("city"))


def context_match_terms(ctx: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for field in ["client_name", "project_name", "project_description", "context_id"]:
        terms.extend(list_context_terms(ctx.get(field)))
    for field in ["aliases", "match_keywords", "local_match_keywords"]:
        terms.extend(list_context_terms(ctx.get(field)))
    deduped: list[str] = []
    for term in terms:
        if term not in deduped:
            deduped.append(term)
    return deduped


def has_explicit_project_evidence(unit: dict[str, Any], ctx: dict[str, Any]) -> bool:
    text = unit_match_text(unit)
    return any(contains_text(text, term) for term in context_match_terms(ctx))


def local_context_allowed_for_auto_match(unit: dict[str, Any], ctx: dict[str, Any]) -> bool:
    if not is_local_context(ctx):
        return True
    return has_explicit_project_evidence(unit, ctx)


def explicit_project_context(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        ctx for ctx in contexts
        if date_in_context(unit.get("expense_date", ""), ctx)
        and has_explicit_project_evidence(unit, ctx)
    ]
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def city_contexts_for_unit(
    unit: dict[str, Any],
    contexts: list[dict[str, Any]],
    *,
    require_non_shanghai: bool,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for ctx in contexts:
        ctx_city = clean(ctx.get("city"))
        if not ctx_city or not city_matches_unit(unit, ctx):
            continue
        if require_non_shanghai and is_shanghai_city(ctx_city):
            continue
        same_city_contexts = [
            item for item in contexts
            if city_key(item.get("city")) == city_key(ctx_city)
        ]
        if len(same_city_contexts) != 1:
            continue
        candidates.append(ctx)
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def unique_non_shanghai_context_for_unit(
    unit: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return city_contexts_for_unit(unit, contexts, require_non_shanghai=True)


def score_context(unit: dict[str, Any], ctx: dict[str, Any]) -> int:
    category = clean(unit.get("source_category"))
    explicit = has_explicit_project_evidence(unit, ctx)
    if category in {"taxi", "travel"} and is_local_context(ctx) and not explicit:
        return 0
    score = 0
    if date_in_context(unit.get("expense_date", ""), ctx):
        score += 3
    city = clean(unit.get("city"))
    ctx_city = clean(ctx.get("city"))
    route = clean(unit.get("route") or unit.get("source_note"))
    if ctx_city and (ctx_city in city or ctx_city in route):
        score += 4
    client = clean(ctx.get("client_name"))
    if client and client in clean(unit.get("source_note")):
        score += 2
    if explicit:
        score += 6
    return score


def project_score_contexts(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    category = clean(unit.get("source_category"))
    candidates = contexts
    if category in {"hotel", "meal", "taxi", "travel"}:
        candidates = non_admin_contexts(contexts)
    return sorted(
        [(score_context(unit, ctx), ctx) for ctx in candidates],
        key=lambda item: (item[0], clean(item[1].get("context_id"))),
        reverse=True,
    )


def route_endpoints(unit: dict[str, Any]) -> tuple[str, str]:
    route = clean(unit.get("route"))
    if not route:
        route = route_from_note(clean(unit.get("source_note") or unit.get("expense_note")))
    if not route:
        return "", ""
    parts = re.split(r"\s*(?:->|-|—|~|至|到)\s*", route, maxsplit=1)
    if len(parts) == 2:
        return clean(parts[0]), clean(parts[1])
    return "", clean(route)


def context_city_in_text(ctx: dict[str, Any], text: str) -> bool:
    city = clean(ctx.get("city"))
    return bool(city and contains_text(text, city))


def apply_context_match(
    unit: dict[str, Any],
    ctx: dict[str, Any],
    *,
    confidence: str,
    status: str,
    auto_project_match: str,
    reason: str,
) -> None:
    unit.update({
        "project_context_id": ctx.get("context_id", ""),
        "client_name": ctx.get("client_name", ""),
        "client_charge_code": ctx.get("client_charge_code", ""),
        "confidence": confidence,
        "status": status,
        "auto_project_match": auto_project_match,
        "match_reason": reason,
    })
    normalize_taxi_column_for_context(unit, ctx, auto_project_match)
    normalize_admin_client(unit)


def normalize_taxi_column_for_context(unit: dict[str, Any], ctx: dict[str, Any], auto_project_match: str) -> None:
    if clean(unit.get("source_category")) not in {"taxi", "travel"} or not is_ride_unit(unit):
        return
    normalize_taxi_column(unit)


def match_hotel(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    contexts = non_admin_contexts(contexts)
    city = clean(unit.get("hotel_city") or unit.get("city"))
    if not city:
        return None
    check_in = parse_date(unit.get("check_in_date", ""))
    check_out = parse_date(unit.get("check_out_date", ""))
    if check_in and check_out:
        candidates = [
            ctx for ctx in contexts
            if context_city_in_text(ctx, city) and date_range_overlaps_context(check_in, check_out, ctx)
        ]
        if len({clean(ctx.get("context_id")) for ctx in candidates}) == 1:
            ctx = candidates[0]
            return {
                "ctx": ctx,
                "auto": "hotel_stay_dates",
                "reason": f"Hotel city and stay dates match project context {ctx.get('context_id', '')}.",
            }
    unique_city_ctx = city_contexts_for_unit(unit, contexts, require_non_shanghai=False)
    if unique_city_ctx:
        return {
            "ctx": unique_city_ctx,
            "auto": "hotel_unique_city",
            "reason": f"Hotel city has exactly one project context: {unique_city_ctx.get('city', '')}.",
        }
    return None


def match_meal(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    contexts = non_admin_contexts(contexts)
    unique_city_ctx = unique_non_shanghai_context_for_unit(unit, contexts)
    if unique_city_ctx:
        return {
            "ctx": unique_city_ctx,
            "auto": "unique_non_shanghai_city",
            "reason": f"Meal city has exactly one non-Shanghai project context: {unique_city_ctx.get('city', '')}.",
        }
    return None


def is_transfer_endpoint(unit: dict[str, Any]) -> bool:
    place_types = {
        clean(unit.get("origin_place_type")),
        clean(unit.get("destination_place_type")),
    }
    if C["airport"] in place_types or C["railway_station"] in place_types:
        return True
    text = clean(" ".join([
        unit.get("origin", ""),
        unit.get("destination", ""),
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
    ]))
    return any(keyword in text for keyword in ["\u673a\u573a", "\u706b\u8f66\u7ad9", "\u9ad8\u94c1\u7ad9", "\u8f66\u7ad9"])


def next_project_for_transfer(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    ride_date = parse_date(unit.get("expense_date", ""))
    if not ride_date or not is_transfer_endpoint(unit):
        return None
    candidates: list[tuple[date, dict[str, Any]]] = []
    for ctx in contexts:
        if is_local_context(ctx):
            continue
        start = context_start(ctx)
        if not start:
            continue
        if ride_date <= start <= ride_date + timedelta(days=1):
            candidates.append((start, ctx))
    candidates.sort(key=lambda item: (item[0], clean(item[1].get("context_id"))))
    context_ids = {clean(ctx.get("context_id")) for _, ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0][1]
    return None


def transfer_project_by_period(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    ride_date = parse_date(unit.get("expense_date", ""))
    if not ride_date or not is_transfer_endpoint(unit):
        return None
    unit_city = clean(unit.get("city"))
    candidates: list[dict[str, Any]] = []
    for ctx in contexts:
        ctx_city = clean(ctx.get("city"))
        if not ctx_city or (unit_city and city_key(ctx_city) == city_key(unit_city)):
            continue
        if date_in_context(unit.get("expense_date", ""), ctx):
            candidates.append(ctx)
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def city_date_context(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        ctx for ctx in contexts
        if city_matches_unit(unit, ctx) and date_in_context(unit.get("expense_date", ""), ctx)
        and local_context_allowed_for_auto_match(unit, ctx)
    ]
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def match_taxi(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    contexts = non_admin_contexts(contexts)
    explicit_ctx = explicit_project_context(unit, contexts)
    if explicit_ctx:
        return {
            "ctx": explicit_ctx,
            "auto": "taxi_explicit_project_evidence",
            "reason": f"Taxi endpoints or notes explicitly mention project context {explicit_ctx.get('context_id', '')}.",
        }
    next_ctx = next_project_for_transfer(unit, contexts)
    if next_ctx:
        return {
            "ctx": next_ctx,
            "auto": "taxi_transfer_to_next_project",
            "reason": f"Taxi appears to be an airport/station transfer to next project context {next_ctx.get('context_id', '')}.",
        }
    period_ctx = transfer_project_by_period(unit, contexts)
    if period_ctx:
        return {
            "ctx": period_ctx,
            "auto": "taxi_transfer_unique_active_project",
            "reason": f"Taxi appears to be an airport/station transfer during a unique active out-of-city project context {period_ctx.get('context_id', '')}.",
        }
    if is_transfer_endpoint(unit):
        return None
    ctx = city_date_context(unit, contexts)
    if ctx:
        return {
            "ctx": ctx,
            "auto": "taxi_city_date",
            "reason": f"Taxi city and date match project context {ctx.get('context_id', '')}.",
        }
    return None


def match_travel(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    contexts = non_admin_contexts(contexts)
    origin, destination = route_endpoints(unit)
    dated_contexts = [ctx for ctx in contexts if date_in_context(unit.get("expense_date", ""), ctx)]
    if destination:
        dest_matches = [
            ctx for ctx in dated_contexts
            if context_city_in_text(ctx, destination)
            and local_context_allowed_for_auto_match(unit, ctx)
        ]
        if len({clean(ctx.get("context_id")) for ctx in dest_matches}) == 1:
            ctx = dest_matches[0]
            return {
                "ctx": ctx,
                "auto": "travel_destination_date",
                "reason": f"Travel destination and date match project context {ctx.get('context_id', '')}.",
            }
    route_text = clean(f"{origin} {destination} {unit.get('route', '')} {unit.get('source_note', '')}")
    route_matches = [
        ctx for ctx in dated_contexts
        if context_city_in_text(ctx, route_text)
        and local_context_allowed_for_auto_match(unit, ctx)
    ]
    if len({clean(ctx.get("context_id")) for ctx in route_matches}) == 1:
        ctx = route_matches[0]
        return {
            "ctx": ctx,
            "auto": "travel_route_date",
            "reason": f"Travel route and date match project context {ctx.get('context_id', '')}.",
        }
    return None


def typed_project_match(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    category = clean(unit.get("source_category"))
    subtype = clean(unit.get("document_subtype"))
    if category == "hotel":
        return match_hotel(unit, contexts)
    if category == "meal":
        return match_meal(unit, contexts)
    if category == "taxi":
        return match_taxi(unit, contexts)
    if category == "travel" or subtype == "railway_e_ticket":
        return match_travel(unit, contexts)
    return None


def taxi_transfer_matches_travel_unit(
    taxi_unit: dict[str, Any],
    units: list[dict[str, Any]],
    contexts_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    ride_date = parse_date(taxi_unit.get("expense_date", ""))
    if not ride_date or not is_transfer_endpoint(taxi_unit):
        return None
    candidates: list[dict[str, Any]] = []
    for travel_unit in units:
        if travel_unit is taxi_unit:
            continue
        if clean(travel_unit.get("source_category")) != "travel" and clean(travel_unit.get("document_subtype")) != "railway_e_ticket":
            continue
        ctx_id = clean(travel_unit.get("project_context_id"))
        if not ctx_id or ctx_id not in contexts_by_id:
            continue
        if is_admin_code(contexts_by_id[ctx_id].get("client_charge_code")):
            continue
        travel_date = parse_date(travel_unit.get("expense_date", ""))
        if not travel_date:
            continue
        if timedelta(days=-1) <= travel_date - ride_date <= timedelta(days=1):
            candidates.append(contexts_by_id[ctx_id])
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def assigned_local_transfer_without_explicit_evidence(
    unit: dict[str, Any],
    contexts_by_id: dict[str, dict[str, Any]],
) -> bool:
    if clean(unit.get("source_category")) != "taxi" or not is_transfer_endpoint(unit):
        return False
    ctx_id = clean(unit.get("project_context_id"))
    ctx = contexts_by_id.get(ctx_id)
    if not ctx or not is_local_context(ctx):
        return False
    return not has_explicit_project_evidence(unit, ctx)


def list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def list_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [clean(item) for item in value]
        return "、".join(part for part in parts if part)
    return clean(value)


def decimal_amount(value: Any) -> Decimal | None:
    amount = money(value)
    if not amount:
        return None
    try:
        return Decimal(amount)
    except InvalidOperation:
        return None


def expense_hints_from_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for ctx in contexts:
        for field, default_category in [("meal_hints", "meal"), ("expense_hints", "")]:
            for index, raw_hint in enumerate(list_value(ctx.get(field)), start=1):
                if not isinstance(raw_hint, dict):
                    continue
                hint = dict(raw_hint)
                hint.setdefault("hint_id", f"{clean(ctx.get('context_id')) or 'CTX'}:{field}:{index}")
                if default_category and not hint.get("source_category") and not hint.get("category"):
                    hint["source_category"] = default_category
                hint.setdefault("project_context_id", ctx.get("context_id", ""))
                hint.setdefault("client_name", ctx.get("client_name", ""))
                hint.setdefault("client_charge_code", ctx.get("client_charge_code", ""))
                hint.setdefault("city", ctx.get("city", ""))
                hints.append(hint)
    return hints


def hint_hard_mismatch(hint: dict[str, Any], unit: dict[str, Any]) -> bool:
    category = clean(hint.get("source_category") or hint.get("category"))
    if category and category != clean(unit.get("source_category")):
        return True
    hint_no = clean(hint.get("unit_no") or hint.get("user_no"))
    if hint_no and hint_no != clean(unit.get("user_no")):
        return True
    invoice_no = clean(hint.get("invoice_no"))
    if invoice_no and invoice_no != clean(unit.get("invoice_no")):
        return True
    return False


def merchant_terms(hint: dict[str, Any]) -> list[str]:
    raw_terms: list[Any] = []
    for field in ["seller_contains", "seller", "merchant", "restaurant", "vendor"]:
        raw_terms.extend(list_value(hint.get(field)))
    raw_terms.extend(list_value(hint.get("merchant_aliases")))
    out: list[str] = []
    for term in raw_terms:
        text = clean(term)
        if text and text not in out:
            out.append(text)
    return out


def compact_merchant_text(value: Any) -> str:
    text = clean(value).lower()
    text = re.sub(r"[（）()\[\]【】,，.。·\-\s]", "", text)
    for word in [
        "有限责任公司",
        "股份有限公司",
        "有限公司",
        "分公司",
        "个体工商户",
        "餐饮管理",
        "餐饮服务",
        "餐饮",
        "管理",
        "服务",
        "店",
    ]:
        text = text.replace(word, "")
    return text


def unit_hint_text(unit: dict[str, Any]) -> str:
    return clean(" ".join([
        unit.get("seller_name", ""),
        unit.get("line_item_name", ""),
        unit.get("raw_remarks", ""),
        unit.get("source_note", ""),
        unit.get("expense_note", ""),
        unit.get("source_filename", ""),
        unit.get("supporting_invoice_filename", ""),
    ]))


def amount_hint_score(hint: dict[str, Any], unit: dict[str, Any]) -> tuple[int, str]:
    hint_amount = decimal_amount(hint.get("amount") or hint.get("recorded_amount"))
    if hint_amount is None:
        return 0, ""
    unit_amount = decimal_amount(unit.get("amount"))
    if unit_amount is None:
        return 0, ""
    delta = abs(unit_amount - hint_amount)
    base = max(abs(hint_amount), Decimal("1.00"))
    pct = delta / base
    abs_tolerance = decimal_amount(hint.get("amount_tolerance_abs")) or Decimal("8.00")
    pct_tolerance = decimal_amount(hint.get("amount_tolerance_pct")) or Decimal("0.12")
    if delta == 0:
        return 5, f"amount exact {unit_amount}"
    if delta <= Decimal("1.00"):
        return 4, f"amount within 1 yuan: hint {hint_amount}, invoice {unit_amount}"
    if delta <= Decimal("3.00") or pct <= Decimal("0.05"):
        return 3, f"amount close: hint {hint_amount}, invoice {unit_amount}"
    if delta <= abs_tolerance or pct <= pct_tolerance:
        return 2, f"amount approximate: hint {hint_amount}, invoice {unit_amount}"
    return -2, f"amount differs: hint {hint_amount}, invoice {unit_amount}"


def date_hint_value(hint: dict[str, Any]) -> str:
    return date_key(clean(hint.get("expense_date") or hint.get("date") or hint.get("meal_date") or ""))


def date_hint_score(hint: dict[str, Any], unit: dict[str, Any]) -> tuple[int, str]:
    hint_date = parse_date(date_hint_value(hint))
    if not hint_date:
        return 0, ""
    reliable_date = parse_date(clean(unit.get("expense_date")))
    if reliable_date:
        diff = abs((reliable_date - hint_date).days)
        if diff == 0:
            return 4, f"reliable date matches {hint_date.isoformat()}"
        if diff <= 1:
            return 2, f"reliable date within 1 day of hint {hint_date.isoformat()}"
    issue_date = parse_date(clean(unit.get("issue_date")))
    if issue_date:
        diff = abs((issue_date - hint_date).days)
        if diff <= 1:
            return 2, f"invoice issue date is within 1 day of hint {hint_date.isoformat()}"
        if diff <= 3:
            return 1, f"invoice issue date is near hint {hint_date.isoformat()}"
    return 0, ""


def merchant_hint_score(hint: dict[str, Any], unit: dict[str, Any]) -> tuple[int, str]:
    terms = merchant_terms(hint)
    if not terms:
        return 0, ""
    haystack = unit_hint_text(unit)
    compact_haystack = compact_merchant_text(haystack)
    for term in terms:
        compact_term = compact_merchant_text(term)
        if not compact_term:
            continue
        if term in haystack or compact_term in compact_haystack:
            return 4, f"merchant text matches {term}"
    return 0, ""


def city_hint_score(hint: dict[str, Any], unit: dict[str, Any]) -> tuple[int, str]:
    city = clean(hint.get("city"))
    if not city:
        return 0, ""
    text = unit_hint_text(unit) + clean(unit.get("city"))
    if city in text:
        return 1, f"city matches {city}"
    return 0, ""


def hint_match_score(hint: dict[str, Any], unit: dict[str, Any]) -> tuple[int, list[str], set[str]]:
    if hint_hard_mismatch(hint, unit):
        return -999, [], set()
    if clean(hint.get("unit_no") or hint.get("user_no") or hint.get("invoice_no")):
        return 100, ["explicit unit or invoice identifier"], {"direct"}
    score = 0
    reasons: list[str] = []
    dimensions: set[str] = set()
    for dimension, scorer in [
        ("amount", amount_hint_score),
        ("date", date_hint_score),
        ("merchant", merchant_hint_score),
        ("city", city_hint_score),
    ]:
        points, reason = scorer(hint, unit)
        score += points
        if points > 0:
            dimensions.add(dimension)
            reasons.append(reason)
        elif points < 0 and reason:
            reasons.append(reason)
    return score, reasons, dimensions


def hint_summary(hint: dict[str, Any]) -> str:
    parts = []
    hint_date = date_hint_value(hint)
    if hint_date:
        parts.append(hint_date)
    merchants = merchant_terms(hint)
    if merchants:
        parts.append("/".join(merchants[:2]))
    amount = money(hint.get("amount") or hint.get("recorded_amount"))
    if amount:
        parts.append(f"RMB {amount}")
    attendees = list_text(hint.get("attendees"))
    if attendees:
        parts.append(f"with {attendees}")
    return " ".join(parts) or clean(hint.get("hint_id")) or "user expense hint"


def hint_auto_match_allowed(score: int, dimensions: set[str]) -> bool:
    if "direct" in dimensions:
        return True
    return score >= 7 and len(dimensions) >= 2


def add_hint_candidate(unit: dict[str, Any], hint: dict[str, Any], score: int, reasons: list[str]) -> None:
    candidates = unit.setdefault("hint_candidates", [])
    candidates.append({
        "hint_id": hint.get("hint_id", ""),
        "summary": hint_summary(hint),
        "score": score,
        "reasons": reasons[:4],
    })
    candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    del candidates[3:]


def apply_hint_to_unit(unit: dict[str, Any], hint: dict[str, Any], score: int, reasons: list[str]) -> None:
    if hint.get("attendees"):
        unit["attendees"] = list_text(hint.get("attendees"))
    if hint.get("meal_context"):
        unit["meal_context"] = clean(hint.get("meal_context"))
    hint_date = date_hint_value(hint)
    if hint_date:
        unit["expense_date"] = hint_date
        unit["date_source"] = "user_context_hint"
        unit["date_required"] = False
        unit["date_is_provisional"] = False
    if hint.get("project_context_id"):
        unit["project_context_id"] = clean(hint.get("project_context_id"))
    if hint.get("client_name"):
        unit["client_name"] = clean(hint.get("client_name"))
    if hint.get("client_charge_code"):
        unit["client_charge_code"] = clean(hint.get("client_charge_code"))
    if hint.get("final_template_column"):
        unit["final_template_column"] = clean(hint.get("final_template_column"))
    normalize_meal_column(unit)
    if hint.get("final_note"):
        unit["final_note"] = clean(hint.get("final_note"))
    elif clean(unit.get("source_category")) == "meal":
        unit["final_note"] = normal_note(unit)
    unit["hint_match_score"] = score
    unit["hint_match_summary"] = hint_summary(hint)
    unit["hint_match_reasons"] = reasons[:6]
    if unit.get("client_charge_code") and (unit.get("expense_date") or clean(unit.get("source_category")) != "meal"):
        unit["confidence"] = "high"
        unit["status"] = "confirmed"
        unit["auto_project_match"] = "user_context_expense_hint"
        unit["match_reason"] = "Matched by user-provided expense hint: " + "; ".join(reasons[:3])


def scored_hint_candidates(
    hint: dict[str, Any],
    units: list[dict[str, Any]],
    assigned_units: set[str],
) -> list[tuple[int, dict[str, Any], list[str], set[str]]]:
    candidates: list[tuple[int, dict[str, Any], list[str], set[str]]] = []
    for unit in units:
        if unit.get("unit_id") in assigned_units:
            continue
        score, reasons, dimensions = hint_match_score(hint, unit)
        if score >= 4:
            candidates.append((score, unit, reasons, dimensions))
    candidates.sort(key=lambda item: (item[0], clean(item[1].get("unit_id"))), reverse=True)
    return candidates


def apply_expense_hints(units: list[dict[str, Any]], contexts: list[dict[str, Any]]) -> None:
    hints = expense_hints_from_contexts(contexts)
    if not hints:
        return
    for unit in units:
        unit.pop("hint_candidates", None)
    assigned_units: set[str] = set()
    pending = list(hints)
    while pending:
        changed = False
        next_pending: list[dict[str, Any]] = []
        for hint in pending:
            candidates = scored_hint_candidates(hint, units, assigned_units)
            if not candidates:
                continue
            top_score = candidates[0][0]
            top = [item for item in candidates if item[0] == top_score]
            if len(top) == 1 and hint_auto_match_allowed(top_score, top[0][3]):
                _, unit, reasons, _ = top[0]
                apply_hint_to_unit(unit, hint, top_score, reasons)
                assigned_units.add(unit["unit_id"])
                changed = True
            else:
                next_pending.append(hint)
        if not changed:
            pending = next_pending
            break
        pending = next_pending
    for hint in pending:
        for score, unit, reasons, _ in scored_hint_candidates(hint, units, assigned_units)[:3]:
            add_hint_candidate(unit, hint, score, reasons)


def apply_matches(units: list[dict[str, Any]], contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contexts_by_id = {clean(ctx.get("context_id")): ctx for ctx in contexts if clean(ctx.get("context_id"))}
    apply_expense_hints(units, contexts)
    for unit in units:
        if unit.get("source_category") == "mobile":
            unit.update({
                "project_context_id": "CTX-ADMIN",
                "client_name": C["mobile"],
                "client_charge_code": C["admin_code"],
                "confidence": "fixed",
                "status": "confirmed",
                "match_reason": f"Mobile/telecom expenses are assigned to {_POLICY.admin_code} with Client = {_POLICY.mobile_client}.",
            })
            normalize_admin_client(unit)
            continue
        if unit.get("auto_project_match") == "user_context_expense_hint" and unit.get("client_charge_code"):
            normalize_admin_client(unit)
            continue
        if unit.get("source_category") in {"other", "unknown"}:
            continue
        typed_match = typed_project_match(unit, contexts)
        if typed_match:
            apply_context_match(
                unit,
                typed_match["ctx"],
                confidence="high",
                status="confirmed",
                auto_project_match=typed_match["auto"],
                reason=typed_match["reason"],
            )
            continue
        scored = project_score_contexts(unit, contexts)
        top_score = scored[0][0] if scored else 0
        top_is_unique = bool(scored) and sum(1 for score, _ in scored if score == top_score) == 1
        if top_is_unique and top_score >= 5:
            ctx = scored[0][1]
            matched_status = "needs_confirmation"
            if top_score >= 7:
                if unit.get("source_category") not in {"meal", "hotel", "taxi", "travel", "other", "unknown"}:
                    matched_status = "confirmed"
            unit.update({
                "project_context_id": ctx.get("context_id", ""),
                "client_name": ctx.get("client_name", ""),
                "client_charge_code": ctx.get("client_charge_code", ""),
                "confidence": "high" if top_score >= 7 else "medium",
                "status": matched_status,
                "match_reason": f"Matched by date/city/context score {top_score}.",
            })
        normalize_admin_client(unit)
    for unit in units:
        if clean(unit.get("source_category")) != "taxi":
            continue
        if unit.get("client_charge_code") and not assigned_local_transfer_without_explicit_evidence(unit, contexts_by_id):
            continue
        ctx = taxi_transfer_matches_travel_unit(unit, units, contexts_by_id)
        if not ctx:
            continue
        apply_context_match(
            unit,
            ctx,
            confidence="high",
            status="confirmed",
            auto_project_match="taxi_transfer_matches_travel_unit",
            reason=f"Taxi airport/station transfer is within one day of a travel item assigned to project context {ctx.get('context_id', '')}.",
        )
    return units


def _legacy_build_questions(units: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = list(existing)
    qidx = len(questions) + 1
    for unit in units:
        unit_id = unit["unit_id"]
        if not unit.get("client_charge_code") and unit.get("source_category") != "mobile":
            questions.append({
                "question_id": f"Q-{qidx:03d}",
                "unit_ids": [unit_id],
                "question": f"{unit_id} 暂未匹配到项目。请确认客户、项目编号、城市和日期范围，或确认不报销。",
                "why_it_matters": "Excel 需要 Client 和 Client Charge Code。",
                "status": "open",
            })
            qidx += 1
        if unit.get("source_category") == "meal":
            questions.append({
                "question_id": f"Q-{qidx:03d}",
                "unit_ids": [unit_id],
                "question": f"{unit_id} 是餐费。请确认实际就餐日期、归属项目、同行/招待对象，以及应写出差餐费、出差餐费（高铁站/机场）还是加班餐费。",
                "why_it_matters": "餐费开票日期不一定等于就餐日期，且本地/出差影响最终填表列。",
                "status": "open",
            })
            qidx += 1
        if unit.get("source_category") == "other":
            questions.append({
                "question_id": f"Q-{qidx:03d}",
                "unit_ids": [unit_id],
                "question": f"{unit_id} 是其他费用。请确认项目归属、最终列和 Note。",
                "why_it_matters": "其他费用无法用通用规则稳定归集。",
                "status": "open",
            })
            qidx += 1
        if unit.get("place_type_needs_confirmation"):
            questions.append({
                "question_id": f"Q-{qidx:03d}",
                "unit_ids": [unit_id],
                "question": f"{unit_id} 的打车地点类型不完整。请确认出发地/目的地应写公司、客户、酒店、机场、火车站、家、餐厅或其他。",
                "why_it_matters": "Taxi Note 必须写真实地点类型，例如打车（公司-火车站）或打车（机场-酒店），不能直接写占位字样。",
                "status": "open",
            })
            qidx += 1
    return questions


def normalize_existing_question(question: dict[str, Any]) -> dict[str, Any]:
    out = dict(question)
    question_id = clean(out.get("question_id"))
    if question_id.startswith("Q-CONTEXT"):
        out["question"] = (
            "请在当前对话里直接提供本次报销周期和项目上下文：周期起止日期、"
            "每个项目的日期/城市/客户名称/Client Charge Code/项目描述，"
            "以及你已知的餐费、替票或特殊费用说明。"
        )
        out["why_it_matters"] = "没有项目上下文时，只能列费用，不能可靠归集到 Client 和 Client Charge Code。"
    elif question_id.startswith("Q-LINK-"):
        out["question"] = (
            "有一份行程单没有匹配到对应发票。请补充发票，"
            "或确认这份行程单不用报销/需要 drop。"
        )
        out["why_it_matters"] = "滴滴/高德需要发票作为财务凭证；行程单用于拆分每笔行程。"
    return out


def unit_user_no(unit: dict[str, Any]) -> int | str:
    if unit.get("user_no"):
        return unit["user_no"]
    match = re.search(r"(\d+)$", clean(unit.get("unit_id")))
    return int(match.group(1)) if match else clean(unit.get("unit_id"))


def unit_label(unit: dict[str, Any]) -> str:
    return f"第{unit_user_no(unit)}项"


def display_date(unit: dict[str, Any]) -> str:
    value = clean(unit.get("expense_date"))
    if value and unit.get("date_is_provisional"):
        return f"{value}（暂用开票日）"
    if value:
        return value
    if unit.get("issue_date"):
        return f"开票{unit.get('issue_date')}（需补发生日期）"
    return ""


def unit_brief(unit: dict[str, Any]) -> str:
    parts = [unit_label(unit)]
    schedule_filename = clean(unit.get("supporting_schedule_filename"))
    invoice_filename = clean(unit.get("supporting_invoice_filename"))
    source_name = clean(unit.get("source_filename"))
    if schedule_filename and invoice_filename and schedule_filename != invoice_filename:
        parts.append(f"行程单 {schedule_filename}")
        parts.append(f"发票文件 {invoice_filename}")
    elif source_name:
        parts.append(f"来源文件 {source_name}")
    if unit.get("invoice_no"):
        parts.append(f"发票号 {unit.get('invoice_no')}")
    elif unit.get("source_item_id"):
        parts.append(f"行程项 {unit.get('source_item_id')}")
    seller = clean(unit.get("seller_name"))
    if seller:
        parts.append(f"开具方/服务方 {seller}")
    if unit.get("amount"):
        parts.append(f"金额 {unit.get('amount')}")
    date_value = display_date(unit)
    if date_value:
        parts.append(f"日期 {date_value}")
    category = clean(unit.get("source_category"))
    final_column = clean(unit.get("final_template_column"))
    if category or final_column:
        parts.append(f"分类 {category or '?'} -> {final_column or '?'}")
    evidence = clean(unit.get("source_note") or unit.get("expense_note"))
    if evidence:
        parts.append(f"备注 {evidence}")
    if unit.get("client_name") or unit.get("client_charge_code"):
        parts.append(f"初步归属 {unit.get('client_name') or '?'} / {unit.get('client_charge_code') or '?'}")
    else:
        parts.append("暂未匹配项目")
    if unit.get("match_reason"):
        parts.append(f"判断依据 {unit.get('match_reason')}")
    hint_candidates = unit.get("hint_candidates") or []
    if hint_candidates:
        top = hint_candidates[0]
        parts.append(
            f"可能对应用户记录 {top.get('summary', '')} "
            f"(score {top.get('score', '')}: {'; '.join(top.get('reasons', []))})"
        )
    return "；".join(part for part in parts if part)


def add_combined_question(
    questions: list[dict[str, Any]],
    unit: dict[str, Any],
    prompts: list[tuple[str, str]],
) -> None:
    if not prompts:
        return
    question_lines = []
    why_lines = []
    for question_text, why_it_matters in prompts:
        prefix = "- " if len(prompts) > 1 else ""
        question_lines.append(prefix + question_text)
        if why_it_matters and why_it_matters not in why_lines:
            why_lines.append(why_it_matters)
    questions.append({
        "question_id": f"Q-{len(questions) + 1:03d}",
        "unit_ids": [unit["unit_id"]],
        "user_no": unit_user_no(unit),
        "question": f"{unit_brief(unit)}\n" + "\n".join(question_lines),
        "why_it_matters": "；".join(why_lines),
        "status": "open",
    })


def add_advisory_question(
    questions: list[dict[str, Any]],
    unit: dict[str, Any],
    question_text: str,
    why_it_matters: str,
) -> None:
    if not question_text:
        return
    questions.append({
        "question_id": f"Q-ADV-{len(questions) + 1:03d}",
        "unit_ids": [unit["unit_id"]],
        "user_no": unit_user_no(unit),
        "question": f"{unit_brief(unit)}\n{question_text}",
        "why_it_matters": why_it_matters,
        "status": "advisory",
        "blocking": False,
    })


def question_unit(question: dict[str, Any], units_by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    unit_ids = question.get("unit_ids", [])
    if len(unit_ids) != 1:
        return None
    return units_by_id.get(unit_ids[0])


def batch_group_key(question: dict[str, Any], unit: dict[str, Any]) -> str:
    category = clean(unit.get("source_category")) or "unknown"
    if category == "meal":
        return "meal"
    if category == "hotel":
        return "hotel"
    if unit.get("place_type_needs_confirmation"):
        return "taxi_place"
    if category == "other":
        if unit.get("date_required") and not unit.get("expense_date"):
            return "other_date"
        return "other"
    if not unit.get("client_charge_code"):
        return f"{category}_project"
    if unit.get("date_required") and not unit.get("expense_date"):
        return f"{category}_date"
    if unit.get("status") == "needs_confirmation" or unit.get("confidence") == "medium":
        return f"{category}_confirm"
    return ""


def batch_item_line(unit: dict[str, Any]) -> str:
    source = clean(unit.get("source_filename")) or clean(unit.get("supporting_schedule_filename")) or "-"
    invoice = clean(unit.get("supporting_invoice_filename"))
    if invoice and invoice != source:
        source = f"{source} / 发票 {invoice}"
    parts = [f"{unit_user_no(unit)}"]
    parts.append(f"文件 {source}")
    if unit.get("invoice_no"):
        parts.append(f"发票号 {unit.get('invoice_no')}")
    seller = clean(unit.get("seller_name"))
    if seller:
        parts.append(f"开具方/服务方 {seller}")
    date_value = display_date(unit)
    if date_value:
        parts.append(f"日期 {date_value}")
    if unit.get("amount"):
        parts.append(f"金额 {unit.get('amount')}")
    category = clean(unit.get("source_category"))
    column = clean(unit.get("final_template_column"))
    if category or column:
        parts.append(f"分类 {category or '?'}->{column or '?'}")
    if unit.get("client_name") or unit.get("client_charge_code"):
        parts.append(f"初步归属 {unit.get('client_name') or '?'} / {unit.get('client_charge_code') or '?'}")
    note = clean(unit.get("source_note") or unit.get("expense_note"))
    if note:
        parts.append(f"备注 {note}")
    hint_candidates = unit.get("hint_candidates") or []
    if hint_candidates:
        top = hint_candidates[0]
        parts.append(
            f"用户记录候选 {top.get('summary', '')} "
            f"(score {top.get('score', '')}: {'; '.join(top.get('reasons', []))})"
        )
    return "- " + " | ".join(parts)


def batch_prompt_for_key(key: str) -> str:
    if key == "meal":
        return (
            "以下餐费请批量确认或修正实际就餐日期、归属项目、同行人/招待对象，以及 Note 类型。开票日期不能直接当就餐日期。"
            "可以按项目和日期一起回复，例如：1/3/5/7 属于山西信托，日期分别是 6/3、6/4、6/5、6/6；"
            "2/4/6 属于广联达，日期分别是 6/10、6/11、6/12。"
        )
    if key == "hotel":
        return "以下酒店费用请批量确认入住日期、离店日期、住宿晚数；如果是标间同住，也请说明同住人或同住情况。"
    if key == "taxi_place":
        return "以下打车/行程地点类型或行程日期不明确，请批量确认实际行程日期，以及出发地和目的地应写公司、客户、酒店、机场、火车站、家、餐厅或其他。"
    if key == "other":
        return "以下其他费用请批量确认项目归属、最终金额列，以及 Note 应该怎么写；日期已暂用开票日，如不对请按编号改成实际发生/记账日期。如果是 ADMIN，也请说明具体事项名称。"
    if key == "other_date":
        return "以下其他费用缺少可用日期，请批量确认实际发生/记账日期、项目归属、最终金额列，以及 Note 应该怎么写。如果是 ADMIN，也请说明具体事项名称。"
    if key.startswith("travel_"):
        return "以下机票/高铁/交通票据缺少可靠出行日期或项目归属，请批量确认出行日期、路线和客户/项目编号。开票日期不能直接作为出行日期。"
    if key.startswith("taxi_"):
        return "以下打车/行程缺少可靠行程日期或项目归属，请批量确认实际行程日期、出发地/目的地场景和归属项目。只有行程单时间才能直接作为打车发生日期。"
    if key.startswith("unknown_"):
        return "以下费用类型或日期不明确，请批量确认记在哪天、费用类型、项目归属和 Note。开票日期不能直接作为发生日期。"
    return "以下费用存在相同类型的不确定信息，请按项目、日期或处理方式批量回复；只要列出需要修改或确认的项目编号即可。"


def build_batch_question(
    key: str,
    grouped_questions: list[dict[str, Any]],
    units_by_id: dict[str, dict[str, Any]],
    question_id: str,
) -> dict[str, Any]:
    units = [question_unit(question, units_by_id) for question in grouped_questions]
    units = [unit for unit in units if unit]
    why_lines: list[str] = []
    for question in grouped_questions:
        why = clean(question.get("why_it_matters"))
        if why and why not in why_lines:
            why_lines.append(why)
    return {
        "question_id": question_id,
        "question_type": f"batch_{key}",
        "unit_ids": [unit["unit_id"] for unit in units],
        "user_nos": [unit_user_no(unit) for unit in units],
        "question": batch_prompt_for_key(key) + "\n" + "\n".join(batch_item_line(unit) for unit in units),
        "why_it_matters": "；".join(why_lines),
        "status": "open",
        "blocking": True,
    }


def group_open_questions(questions: list[dict[str, Any]], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units_by_id = {unit["unit_id"]: unit for unit in units}
    groups: dict[str, list[dict[str, Any]]] = {}
    slots: list[tuple[str, Any]] = []
    for question in questions:
        if question.get("status", "open") != "open":
            slots.append(("single", question))
            continue
        unit = question_unit(question, units_by_id)
        key = batch_group_key(question, unit) if unit else ""
        if not key:
            slots.append(("single", question))
            continue
        if key not in groups:
            groups[key] = []
            slots.append(("group", key))
        groups[key].append(question)

    out: list[dict[str, Any]] = []
    group_idx = 1
    for slot_type, value in slots:
        if slot_type == "single":
            out.append(value)
            continue
        grouped_questions = groups[value]
        if len(grouped_questions) == 1:
            out.append(grouped_questions[0])
            continue
        out.append(build_batch_question(value, grouped_questions, units_by_id, f"Q-GROUP-{group_idx:03d}"))
        group_idx += 1
    return out


def add_auto_matched_meal_review(questions: list[dict[str, Any]], units: list[dict[str, Any]]) -> None:
    auto_meals = [
        unit for unit in units
        if unit.get("source_category") == "meal"
        and unit.get("auto_project_match") == "unique_non_shanghai_city"
        and unit.get("status") == "confirmed"
        and unit.get("expense_date")
    ]
    if not auto_meals:
        return
    questions.append({
        "question_id": f"Q-ADV-MEAL-{len(questions) + 1:03d}",
        "question_type": "auto_matched_meal_review",
        "unit_ids": [unit["unit_id"] for unit in auto_meals],
        "user_nos": [unit_user_no(unit) for unit in auto_meals],
        "question": (
            "以下餐费已按“非上海城市唯一项目”自动归属。"
            "开票日期不作为就餐日期；如果有归属、日期、同行人或实报金额需要调整，请直接按编号批量回复。"
            "\n" + "\n".join(batch_item_line(unit) for unit in auto_meals)
        ),
        "why_it_matters": "非上海城市在本周期只有一个项目时，默认归属通常足够可靠；餐费日期和同行人仍允许用户批量修正。",
        "status": "advisory",
        "blocking": False,
    })


def add_provisional_other_date_review(questions: list[dict[str, Any]], units: list[dict[str, Any]]) -> None:
    provisional_units = [
        unit for unit in units
        if unit.get("source_category") == "other"
        and unit.get("date_is_provisional")
        and unit.get("expense_date")
    ]
    if not provisional_units:
        return
    questions.append({
        "question_id": f"Q-ADV-OTHER-DATE-{len(questions) + 1:03d}",
        "question_type": "provisional_other_date_review",
        "unit_ids": [unit["unit_id"] for unit in provisional_units],
        "user_nos": [unit_user_no(unit) for unit in provisional_units],
        "question": (
            "以下 other 费用的 Date 已暂用发票开票日期。这个不是阻塞项；如果实际发生/记账日期不同，请按编号批量更正。"
            "\n" + "\n".join(batch_item_line(unit) for unit in provisional_units)
        ),
        "why_it_matters": "纯 other 费用通常可以先用开票日期推进；这里是给申请人的非阻塞复核提示。",
        "status": "advisory",
        "blocking": False,
    })


def date_prompt_for_unit(unit: dict[str, Any]) -> tuple[str, str] | None:
    if not unit.get("date_required") or unit.get("expense_date") or is_mobile_admin_unit(unit):
        return None
    source_category = clean(unit.get("source_category")) or "unknown"
    reason = clean(unit.get("date_question_reason")) or "开票日期不能直接作为报销发生日期。"
    if source_category in {"meal", "hotel"}:
        return None
    if source_category == "taxi":
        return (
            "这笔打车/行程没有可靠行程日期。请确认实际行程日期；如果缺行程单，也请说明是否会补行程单，或是否按汇总发票处理。",
            reason,
        )
    if source_category == "travel":
        return (
            "这笔机票/高铁/交通票据没有可靠出行日期。请确认实际出行日期和路线；不能直接使用开票日期。",
            reason,
        )
    if source_category == "other":
        return (
            "这笔其他费用需要确认记在哪一天。请提供发生日期/记账日期；不能直接使用开票日期。",
            reason,
        )
    return (
        "这笔费用缺少可靠发生日期。请确认应记在哪一天；不能直接使用开票日期。",
        reason,
    )


def build_questions(units: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = [normalize_existing_question(q) for q in existing]
    for unit in units:
        source_category = unit.get("source_category")
        has_project = bool(unit.get("client_charge_code"))
        prompts: list[tuple[str, str]] = []
        accounting_errors = mobile_accounting_errors(unit)
        if accounting_errors:
            prompts.append(
                (
                    f"这项费用的会计归属存在明显冲突：非通讯费不能写“通讯费”、不能进 mobile 列；打车/交通费也不能归入 {_POLICY.admin_code}。请按实际项目重新确认 Client、Client Charge Code、金额列和 Note。",
                    "这是硬性防呆规则，用于防止未匹配交通费被错误丢进通讯费/Admin。",
                )
            )
        placeholder_errors = note_placeholder_errors(unit)
        if placeholder_errors:
            prompts.append(
                (
                    "这笔打车/行程的 Note 还缺少确认后的地点类型，不能把“出发地类型”或“目的地类型”这些占位字样写进最终表。请确认出发地和目的地分别应写公司、客户、酒店、机场、火车站、家、餐厅或其他。",
                    "Taxi Note 必须使用真实地点类型；占位词只用于说明模板，不能作为最终报销说明。",
                )
            )
        date_prompt = date_prompt_for_unit(unit)
        if date_prompt:
            prompts.append(date_prompt)

        if not has_project and source_category != "mobile":
            prompts.append(
                (
                    "这张/这笔费用暂未匹配到项目。请直接回复应归属的客户、项目编号、城市和日期范围；如果不用报销，也请说明 drop。",
                    "Excel 必须填写 Client 和 Client Charge Code；无法确认时不能直接进最终表。",
                )
            )
        elif unit.get("status") == "needs_confirmation" or unit.get("confidence") == "medium":
            admin_matter_only = (
                source_category == "other"
                and is_admin_code(unit.get("client_charge_code"))
                and unit.get("confidence") == "high"
            )
            if admin_matter_only:
                pass
            else:
                prompts.append(
                    (
                        (
                            "我初步判断它归属于上述项目。请确认这个归属是否正确；"
                            "如果不正确，请回复正确的客户、项目编号和原因。"
                        ),
                        "这是模型可以初步判断但需要你确认的项目归属，尤其适用于跨日期交通、重名 code 或证据不完整的情况。",
                    )
                )

        if source_category == "hotel":
            final_note = clean(unit.get("final_note"))
            has_nights = bool(
                unit.get("hotel_nights")
                or (unit.get("check_in_date") and unit.get("check_out_date"))
                or re.search(r"\d+\s*\u665a", final_note)
            )
            if not has_nights or not unit.get("expense_date"):
                prompts.append(
                    (
                        "这是酒店费用。请确认入住日期、离店日期、住宿晚数；如果是标间同住，也请说明同住人或同住情况。",
                        f"酒店报销标准按每晚计算：北上广深 {_POLICY.first_tier_hotel_cap:f}/晚，其他城市 {_POLICY.other_city_hotel_cap:f}/晚；缺少入住/离店日期和晚数时无法判断日期与是否超标。",
                    )
                )

        if source_category == "meal" and (
            unit.get("auto_project_match") != "unique_non_shanghai_city" or not unit.get("expense_date")
        ):
            prompts.append(
                (
                    (
                        "这是餐费。请确认实际就餐日期、归属项目/客户、同行人或招待对象，"
                        "以及 Note 应写出差餐费、出差餐费（高铁站/机场）还是加班餐费。"
                    ),
                    "餐费开票日期经常不等于就餐日期，并且本地/出差会影响最终填表列。",
                )
            )

        if source_category == "other" and not is_admin_code(unit.get("client_charge_code")):
            if unit.get("date_is_provisional"):
                other_question = "这是其他费用。请确认项目归属、最终金额列，以及 Note 应该怎么写；日期已暂用开票日，如不对请一起更正。"
                other_reason = "其他费用无法用通用规则稳定归集，需要用户给出会计口径；日期暂用开票日仅作为非阻塞复核项。"
            else:
                other_question = "这是其他费用。请确认发生日期/记账日期、项目归属、最终金额列，以及 Note 应该怎么写。"
                other_reason = "这笔其他费用没有可暂用的开票日期，需要用户给出日期和会计口径。"
            prompts.append(
                (
                    other_question,
                    other_reason,
                )
            )

        if unit.get("place_type_needs_confirmation"):
            origin = clean(unit.get("origin"))
            destination = clean(unit.get("destination"))
            prompts.append(
                (
                    (
                        f"这笔打车地点类型不完整：出发地「{origin or '?'}」、目的地「{destination or '?'}」。"
                        "请确认分别应写公司、客户、酒店、机场、火车站、家、餐厅或其他。"
                    ),
                    "Taxi Note 必须写真实地点类型，例如“打车（公司-火车站）”或“打车（机场-酒店）”，敏感或模糊地点不能硬猜，也不能直接写占位字样。",
                )
            )
        add_combined_question(questions, unit, prompts)
        if (
            is_admin_code(unit.get("client_charge_code"))
            and not is_mobile_admin_unit(unit)
            and clean(unit.get("client_name")) == C["admin_fallback_client"]
        ):
            add_advisory_question(
                questions,
                unit,
                (
                    f"这笔费用已经归到 {_POLICY.admin_code}，Client 暂写为“{_POLICY.admin_fallback_client}”。"
                    "如果其实是年会、半年会、客户会、行业协会会议等具体事项，请直接告诉我应改成什么；"
                    "如果不改，这个默认值也可以先进入最终表。"
                ),
                "Admin 的 Client 列用于说明事项，不能笼统写 Admin；但事项名称缺失不是阻塞项。",
            )
    questions = group_open_questions(questions, units)
    add_auto_matched_meal_review(questions, units)
    add_provisional_other_date_review(questions, units)
    return questions


def unit_review_line(unit: dict[str, Any]) -> str:
    source = clean(unit.get("source_filename")) or clean(unit.get("supporting_schedule_filename")) or "-"
    invoice = clean(unit.get("supporting_invoice_filename"))
    if invoice and invoice != source:
        source = f"{source} / 发票 {invoice}"
    seller = clean(unit.get("seller_name")) or "-"
    date_value = display_date(unit) or "-"
    amount = clean(unit.get("amount")) or "-"
    category = clean(unit.get("source_category")) or "-"
    column = clean(unit.get("final_template_column")) or "-"
    client = clean(unit.get("client_name")) or "待确认"
    code = clean(unit.get("client_charge_code")) or "待确认"
    status = "待确认" if unit.get("status") != "confirmed" or unit.get("confidence") in {"low", "medium"} else "已确认"
    if unit.get("place_type_needs_confirmation"):
        status = "待确认地点类型"
    if category == "meal":
        if unit.get("status") == "confirmed":
            status = "已自动归属，可复核" if unit.get("auto_project_match") else "已确认"
        else:
            status = "待确认"
    if category == "other" and not (is_admin_code(code) and unit.get("status") == "confirmed"):
        status = "待确认"
    return (
        f"{unit_label(unit)} | 文件 {source} | 开具方/服务方 {seller} | 日期 {date_value} | "
        f"金额 {amount} | 分类 {category}->{column} | 归属 {client}/{code} | 状态 {status}"
    )


def print_applicant_review_list(payload: dict[str, Any]) -> None:
    units = payload.get("allocation_units", [])
    if not units:
        return
    print("")
    print("APPLICANT REVIEW LIST TO SHOW IN CHAT:")
    print("Copy or summarize this list before asking questions, so the user can correct items by number and source filename.")
    for unit in units:
        print(unit_review_line(unit))


def add_trip_window_advisories(payload: dict[str, Any]) -> None:
    """A weak classifier often files trip-day meals/transport as `other`.
    The script cannot re-judge the category, but it can raise suspicion:
    any `other` expense dated inside a project's travel window gets an
    advisory so the user (the final judge) sees it."""
    contexts = [c for c in payload.get("project_contexts", [])
                if clean(c.get("client_charge_code")) != _POLICY.admin_code]
    for unit in payload.get("allocation_units", []):
        if unit.get("source_category") != "other":
            continue
        unit_date = clean(unit.get("expense_date"))
        if not unit_date:
            continue
        for ctx in contexts:
            if date_in_context(unit_date, ctx):
                add_advisory_question(
                    payload.setdefault("questions", []),
                    unit,
                    f"此项被分类为 other，但日期 {unit_date} 落在「{clean(ctx.get('client_name'))}」出差期间。"
                    "请确认它是否其实是出差餐费/交通/住宿（若是，需改回对应类别，否则不会参与餐费/酒店标准检查）。",
                    "出差期间的餐费若被归为 other，将绕过每日 150 元出差餐费上限检查。",
                )
                break


def print_open_questions(payload: dict[str, Any]) -> None:
    open_questions = [q for q in payload.get("questions", []) if q.get("status", "open") == "open"]
    if not open_questions:
        print("No open allocation questions. Stage 2 is ready for Excel output.")
        print("NEXT: if draft units remain, write allocation_decisions.v1 and run compose_answers.py; "
              "otherwise run write_reimbursement_template.py.")
    else:
        print("")
        print("=== 请将以下内容原样转发给用户（逐字复制，不要归纳或省略）===")
        print(f"这批费用有 {len(open_questions)} 个问题需要你确认：")
        for idx, question in enumerate(open_questions, start=1):
            print(f"{idx}. {question.get('question', '')}")
        print("请按编号回复即可，不用严格格式。")
        print("=== 转发块结束 ===")
        print("")
        print(f"⚠ NEXT (MANDATORY): {len(open_questions)} blocking question(s). Your very next chat "
              "message MUST contain the 转发块 above, verbatim, then STOP and wait for the user's "
              "answers. Do not end your turn without sending it, and do not proceed to any later "
              "stage first.")


def print_advisory_questions(payload: dict[str, Any]) -> None:
    advisory_questions = [q for q in payload.get("questions", []) if q.get("status") == "advisory"]
    if not advisory_questions:
        return
    print("")
    print("NON-BLOCKING PROMPTS TO SHOW IN CHAT:")
    print("These are optional refinements. They do not block Excel output if the default value is acceptable.")
    for idx, question in enumerate(advisory_questions, start=1):
        print(f"{idx}. {question.get('question', '')}")
        why = clean(question.get("why_it_matters"))
        if why:
            print(f"   Why: {why}")


def build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Expense Allocation Process",
        "",
        f"Generated at: {payload['generated_at']}",
        f"Source extraction file: {payload['source_extraction_file']}",
        f"Allocation units: {len(payload['allocation_units'])}",
        f"Questions remaining: {sum(1 for q in payload['questions'] if q.get('status') == 'open')}",
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
            f"| {unit.get('user_no') or unit_user_no(unit)} | {unit['unit_id']} | {unit.get('source_filename','')} | "
            f"{unit.get('source_document_id','')} {unit.get('source_item_id') or ''} | "
            f"{unit.get('expense_date','')} | {city_route} | {unit.get('invoice_amount') or unit.get('amount','')} | "
            f"{unit.get('reimbursable_amount') or unit.get('amount','')} | {unit.get('source_category','')} | "
            f"{unit.get('client_name','')} | {unit.get('client_charge_code','')} | {unit.get('final_template_column','')} | "
            f"{unit.get('confidence','')} | {unit.get('status','')} |"
        )
    lines += [
        "",
        "## Applicant Review List",
        "",
    ]
    for unit in payload["allocation_units"]:
        lines.append(f"- {unit_review_line(unit)}")
    lines += [
        "",
        "## Questions For User",
        "",
        "| Question ID | Unit(s) | Question | Why It Matters |",
        "| --- | --- | --- | --- |",
    ]
    for q in payload["questions"]:
        lines.append(f"| {q['question_id']} | {', '.join(q.get('unit_ids', []))} | {q.get('question','')} | {q.get('why_it_matters','')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Build stage-2 expense allocation scaffold.")
    parser.add_argument("--extraction", required=True, help="Path to process/invoice-extraction.json.")
    parser.add_argument("--context", help="Optional project context JSON or text file.")
    parser.add_argument("--output", "-o", default="process", help="Output folder.")
    args = parser.parse_args(argv)

    extraction_path = Path(args.extraction)
    extraction = load_json(extraction_path)
    integrity.require_valid(extraction, extraction_path, kind="extraction")
    unresolved_inputs = [
        item for item in extraction.get("unresolved_input_files", [])
        if item.get("status", "open") == "open"
    ]
    if unresolved_inputs:
        print("ERROR: allocation is blocked because supplied files remain unsupported and undecided:", file=sys.stderr)
        for item in unresolved_inputs:
            print(f"  - {item.get('filename', '?')} ({item.get('suffix') or 'no suffix'}; "
                  f"sha256 {item.get('sha256', '?')})", file=sys.stderr)
        print("NEXT: ask the user whether each file should be excluded or converted to a readable PDF/image, "
              "record that decision with apply_extraction_corrections.py (input_resolutions), then re-run "
              "extract_invoices.py before allocation.", file=sys.stderr)
        return 2
    context_path = Path(args.context) if args.context else None
    try:
        contexts, raw_context, context_questions = load_context(context_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        print("CANONICAL PROJECT CONTEXT TEMPLATE (copy the structure; replace every placeholder):", file=sys.stderr)
        print(project_context_template_path().read_text(encoding="utf-8"), file=sys.stderr)
        print(
            "NEXT: rewrite the same project-context.json in this exact schema, using one context object "
            "per distinct project/travel date window, then rerun allocation. Do not ask the applicant "
            "to write JSON and do not bypass this check with a launcher or patch script.",
            file=sys.stderr,
        )
        return 2
    print(f"PROJECT CONTEXT VALIDATED: {len(contexts)} canonical context window(s).")
    units, link_questions = create_units(extraction, contexts)
    print_document_reconciliation(extraction, units)
    apply_matches(units, contexts)
    questions = build_questions(units, context_questions + link_questions)

    payload = {
        "schema_version": "expense_allocation.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "source_extraction_file": str(extraction_path),
        "source_extraction_fingerprint": extraction["integrity"]["fingerprint"],
        "source_project_context_file": str(context_path.resolve()) if context_path else "",
        "source_project_context_sha256": (
            hashlib.sha256(context_path.read_bytes()).hexdigest() if context_path else ""
        ),
        "raw_project_context": raw_context,
        "project_contexts": contexts,
        "allocation_units": units,
        "questions": questions,
        "change_log": [],
    }
    output = Path(args.output)
    integrity.stamp(payload, "allocate_expenses.py")
    write_json(output / "expense-allocation.json", payload)
    (output / "expense-allocation.md").write_text(build_markdown(payload), encoding="utf-8")
    print(f"Wrote {output / 'expense-allocation.json'}")
    print(f"Wrote {output / 'expense-allocation.md'}")
    print_applicant_review_list(payload)
    add_trip_window_advisories(payload)
    print_open_questions(payload)
    print_advisory_questions(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
