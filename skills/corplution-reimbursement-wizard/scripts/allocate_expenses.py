#!/usr/bin/env python3
"""Create stage-2 allocation units and question queues from extracted invoices."""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


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
    "admin_code": "CORP-2026-ADMIN",
    "admin_fallback_client": "\u9879\u76ee\u3001\u8c03\u7814\u4ee5\u5916\u7684\u5176\u4ed6\u8d39\u7528",
    "travel_meal": "\u51fa\u5dee\u9910\u8d39",
    "station_meal": "\u51fa\u5dee\u9910\u8d39\uff08\u9ad8\u94c1\u7ad9/\u673a\u573a\uff09",
    "overtime_meal": "\u52a0\u73ed\u9910\u8d39",
    "taxi": "\u6253\u8f66",
    "flight": "\u98de\u673a",
    "rail": "\u9ad8\u94c1",
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
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def is_admin_code(value: Any) -> bool:
    return clean(value).upper() == C["admin_code"]


def is_mobile_admin_unit(unit: dict[str, Any]) -> bool:
    return unit.get("source_category") == "mobile" or unit.get("final_template_column") == "mobile"


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


def load_context(path: Path | None) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    if not path:
        return [], "", [{
            "question_id": "Q-CONTEXT-001",
            "unit_ids": [],
            "question": "请提供本次报销周期内的项目上下文：日期范围、城市、客户名称、项目编号、项目描述。",
            "why_it_matters": "没有项目上下文时，只能生成费用清单，不能可靠匹配 Client 和 Client Charge Code。",
            "status": "open",
        }]
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        contexts = payload.get("project_contexts", payload if isinstance(payload, list) else [])
        normalized = []
        for idx, ctx in enumerate(contexts, start=1):
            item = dict(ctx)
            item.setdefault("context_id", f"CTX-{idx:03d}")
            item.setdefault("travel_buffer_days", 1)
            item.setdefault("status", "draft")
            normalized.append(item)
        return normalized, "", []
    return [], text, [{
        "question_id": "Q-CONTEXT-001",
        "unit_ids": [],
        "question": (
            f"已收到自然语言项目说明：{text[:200]}。我会把它整理成项目上下文用于匹配；"
            "请确认这段说明是否覆盖报销周期、城市、客户名称和 Client Charge Code。"
            "如果缺任何一项，请直接补充。"
        ),
        "why_it_matters": "脚本保留原文；agent 应把自然语言整理成结构化项目上下文，用户只需要确认或补充缺项。",
        "status": "open",
    }]


def doc_by_id(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {doc["document_id"]: doc for doc in extraction.get("documents", [])}


def didi_links(extraction: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    invoice_to_schedule: dict[str, str] = {}
    schedule_to_invoice: dict[str, str] = {}
    for link in extraction.get("document_links", []):
        if link.get("relation") == "invoice_total_matches_didi_trip_report":
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
        return "taxi" if city and "\u4e0a\u6d77" in city else "travel"
    if source_category == "travel":
        return "travel"
    if source_category in {"other", "unknown"}:
        return "other"
    return source_category or "other"


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


def normal_note(unit: dict[str, Any]) -> str:
    category = unit.get("source_category", "")
    subtype = unit.get("document_subtype", "")
    source = clean(unit.get("source_note"))
    if subtype == "railway_e_ticket" or "G" in source[:12]:
        route = route_from_note(source) or clean(unit.get("route"))
        return f"{C['rail']}\uff08{route}\uff09" if route else C["rail"]
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
        return C["travel_meal"] if unit.get("final_template_column") == "travel" else C["overtime_meal"]
    if category in {"taxi", "travel"} and unit.get("origin"):
        origin_type = unit.get("origin_place_type") or "\u51fa\u53d1\u5730\u7c7b\u578b"
        dest_type = unit.get("destination_place_type") or "\u76ee\u7684\u5730\u7c7b\u578b"
        suffix = "\uff08\u52a0\u73ed\uff09" if unit.get("business_reason") == "overtime" else ""
        return f"{C['taxi']}\uff08{origin_type}-{dest_type}\uff09{suffix}"
    return source


def create_units(extraction: dict[str, Any], contexts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs = doc_by_id(extraction)
    invoice_to_schedule, schedule_to_invoice = didi_links(extraction)
    units: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    unit_idx = 1

    for doc in extraction.get("documents", []):
        doc_id = doc["document_id"]
        role = doc.get("document_role")
        subtype = doc.get("document_subtype")
        classification = doc.get("classification") or {}
        invoice = doc.get("invoice") or {}

        if role == "invoice" and doc_id in invoice_to_schedule:
            continue

        if subtype == "didi_trip_report":
            linked_invoice_id = schedule_to_invoice.get(doc_id, "")
            linked_invoice_doc = docs.get(linked_invoice_id, {})
            if not linked_invoice_id:
                questions.append({
                    "question_id": f"Q-LINK-{doc_id}",
                    "unit_ids": [],
                    "question": f"{doc_id} 是滴滴行程单，但没有匹配到发票。请补发票，或确认这份行程单不报销/删除。",
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
                    "seller_name": C["didi"],
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
            unit["final_note"] = normal_note(unit)
            units.append(unit)
            unit_idx += 1
            continue

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
        clean(unit.get("source_note")),
        clean(unit.get("expense_note")),
        clean(unit.get("seller_name")),
    ]))


def city_matches_unit(unit: dict[str, Any], ctx: dict[str, Any]) -> bool:
    ctx_city = clean(ctx.get("city"))
    if not ctx_city:
        return False
    return contains_text(unit_match_text(unit), ctx_city)


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
    return score


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
    normalize_admin_client(unit)


def match_hotel(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
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


def city_date_context(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        ctx for ctx in contexts
        if city_matches_unit(unit, ctx) and date_in_context(unit.get("expense_date", ""), ctx)
    ]
    context_ids = {clean(ctx.get("context_id")) for ctx in candidates}
    if len(context_ids) == 1:
        return candidates[0]
    return None


def match_taxi(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    next_ctx = next_project_for_transfer(unit, contexts)
    if next_ctx:
        return {
            "ctx": next_ctx,
            "auto": "taxi_transfer_to_next_project",
            "reason": f"Taxi appears to be an airport/station transfer to next project context {next_ctx.get('context_id', '')}.",
        }
    ctx = city_date_context(unit, contexts)
    if ctx:
        return {
            "ctx": ctx,
            "auto": "taxi_city_date",
            "reason": f"Taxi city and date match project context {ctx.get('context_id', '')}.",
        }
    return None


def match_travel(unit: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    origin, destination = route_endpoints(unit)
    dated_contexts = [ctx for ctx in contexts if date_in_context(unit.get("expense_date", ""), ctx)]
    if destination:
        dest_matches = [ctx for ctx in dated_contexts if context_city_in_text(ctx, destination)]
        if len({clean(ctx.get("context_id")) for ctx in dest_matches}) == 1:
            ctx = dest_matches[0]
            return {
                "ctx": ctx,
                "auto": "travel_destination_date",
                "reason": f"Travel destination and date match project context {ctx.get('context_id', '')}.",
            }
    route_text = clean(f"{origin} {destination} {unit.get('route', '')} {unit.get('source_note', '')}")
    route_matches = [ctx for ctx in dated_contexts if context_city_in_text(ctx, route_text)]
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


def apply_matches(units: list[dict[str, Any]], contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for unit in units:
        if unit.get("source_category") == "mobile":
            unit.update({
                "project_context_id": "CTX-ADMIN",
                "client_name": C["mobile"],
                "client_charge_code": C["admin_code"],
                "confidence": "fixed",
                "status": "confirmed",
                "match_reason": "Mobile/telecom expenses are assigned to CORP-2026-ADMIN with Client = 通讯费.",
            })
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
        scored = sorted(
            [(score_context(unit, ctx), ctx) for ctx in contexts],
            key=lambda item: item[0],
            reverse=True,
        )
        if scored and scored[0][0] >= 5:
            ctx = scored[0][1]
            matched_status = "needs_confirmation"
            if scored[0][0] >= 7:
                if unit.get("source_category") not in {"meal", "hotel", "taxi", "travel", "other", "unknown"}:
                    matched_status = "confirmed"
            unit.update({
                "project_context_id": ctx.get("context_id", ""),
                "client_name": ctx.get("client_name", ""),
                "client_charge_code": ctx.get("client_charge_code", ""),
                "confidence": "high" if scored[0][0] >= 7 else "medium",
                "status": matched_status,
                "match_reason": f"Matched by date/city/context score {scored[0][0]}.",
            })
        normalize_admin_client(unit)
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
                "why_it_matters": "Taxi Note 必须写成打车（出发地类型-目的地类型）。",
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
                        "酒店报销标准按每晚计算：北上广深 800/晚，其他城市 600/晚；缺少入住/离店日期和晚数时无法判断日期与是否超标。",
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
                    "Taxi Note 必须写成“打车（出发地类型-目的地类型）”，敏感或模糊地点不能硬猜。",
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
                    "这笔费用已经归到 CORP-2026-ADMIN，Client 暂写为“项目、调研以外的其他费用”。"
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


def print_open_questions(payload: dict[str, Any]) -> None:
    open_questions = [q for q in payload.get("questions", []) if q.get("status", "open") == "open"]
    if not open_questions:
        print("No open allocation questions. Stage 2 is ready for Excel output.")
    else:
        print("")
        print("QUESTIONS TO ASK USER DIRECTLY IN THIS CHAT:")
        print("Do not ask the user to open expense-allocation.md/json; copy or summarize these questions in the conversation.")
        for idx, question in enumerate(open_questions, start=1):
            print(f"{idx}. {question.get('question', '')}")
            why = clean(question.get("why_it_matters"))
            if why:
                print(f"   Why: {why}")


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
    parser = argparse.ArgumentParser(description="Build stage-2 expense allocation scaffold.")
    parser.add_argument("--extraction", required=True, help="Path to process/invoice-extraction.json.")
    parser.add_argument("--context", help="Optional project context JSON or text file.")
    parser.add_argument("--output", "-o", default="process", help="Output folder.")
    args = parser.parse_args(argv)

    extraction_path = Path(args.extraction)
    extraction = load_json(extraction_path)
    contexts, raw_context, context_questions = load_context(Path(args.context) if args.context else None)
    units, link_questions = create_units(extraction, contexts)
    apply_matches(units, contexts)
    questions = build_questions(units, context_questions + link_questions)

    payload = {
        "schema_version": "expense_allocation.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "source_extraction_file": str(extraction_path),
        "raw_project_context": raw_context,
        "project_contexts": contexts,
        "allocation_units": units,
        "questions": questions,
        "change_log": [],
    }
    output = Path(args.output)
    write_json(output / "expense-allocation.json", payload)
    (output / "expense-allocation.md").write_text(build_markdown(payload), encoding="utf-8")
    print(f"Wrote {output / 'expense-allocation.json'}")
    print(f"Wrote {output / 'expense-allocation.md'}")
    print_applicant_review_list(payload)
    print_open_questions(payload)
    print_advisory_questions(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
