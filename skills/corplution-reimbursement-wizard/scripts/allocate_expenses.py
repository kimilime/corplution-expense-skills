#!/usr/bin/env python3
"""Create stage-2 allocation units and question queues from extracted invoices."""

from __future__ import annotations

import argparse
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
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def date_key(value: str) -> str:
    d = parse_date(value)
    return d.isoformat() if d else ""


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
                    "expense_date": date_key(item.get("ride_datetime", "")),
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
                "expense_date": classification.get("expense_date") or invoice.get("issue_date", ""),
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
                "check_in_date": "",
                "check_out_date": "",
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
        scored = sorted(
            [(score_context(unit, ctx), ctx) for ctx in contexts],
            key=lambda item: item[0],
            reverse=True,
        )
        if scored and scored[0][0] >= 5:
            ctx = scored[0][1]
            matched_status = "needs_confirmation"
            if scored[0][0] >= 7:
                if unit.get("source_category") not in {"meal", "other"}:
                    matched_status = "confirmed"
                elif unit.get("source_category") == "other" and is_admin_code(ctx.get("client_charge_code")):
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
    if unit.get("expense_date"):
        parts.append(f"日期 {unit.get('expense_date')}")
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


def build_questions(units: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = [normalize_existing_question(q) for q in existing]
    for unit in units:
        source_category = unit.get("source_category")
        has_project = bool(unit.get("client_charge_code"))
        prompts: list[tuple[str, str]] = []

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
            if not has_nights:
                prompts.append(
                    (
                        "这是酒店费用。请确认入住日期、离店日期、住宿晚数；如果是标间同住，也请说明同住人或同住情况。",
                        "酒店报销标准按每晚计算：北上广深 800/晚，其他城市 600/晚；缺少晚数时无法判断是否超标。",
                    )
                )

        if source_category == "meal":
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
            prompts.append(
                (
                    "这是其他费用。请确认项目归属、最终金额列，以及 Note 应该怎么写。",
                    "其他费用无法用通用规则稳定归集，需要用户给出会计口径。",
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
    return questions


def unit_review_line(unit: dict[str, Any]) -> str:
    source = clean(unit.get("source_filename")) or clean(unit.get("supporting_schedule_filename")) or "-"
    invoice = clean(unit.get("supporting_invoice_filename"))
    if invoice and invoice != source:
        source = f"{source} / 发票 {invoice}"
    seller = clean(unit.get("seller_name")) or "-"
    date_value = clean(unit.get("expense_date")) or "-"
    amount = clean(unit.get("amount")) or "-"
    category = clean(unit.get("source_category")) or "-"
    column = clean(unit.get("final_template_column")) or "-"
    client = clean(unit.get("client_name")) or "待确认"
    code = clean(unit.get("client_charge_code")) or "待确认"
    status = "待确认" if unit.get("status") != "confirmed" or unit.get("confidence") in {"low", "medium"} else "已确认"
    if unit.get("place_type_needs_confirmation"):
        status = "待确认地点类型"
    if category in {"meal", "other"} and not (category == "other" and is_admin_code(code) and unit.get("status") == "confirmed"):
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
