#!/usr/bin/env python3
"""Trace a user-facing expense item number back to source files and extracted evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


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


def resolve_unit(allocation: dict[str, Any], item: str) -> dict[str, Any]:
    item = clean(item)
    for unit in allocation.get("allocation_units", []):
        if item == unit_no(unit) or item == clean(unit.get("unit_id")):
            return unit
    if item.isdigit():
        unit_id = f"UNIT-{int(item):03d}"
        for unit in allocation.get("allocation_units", []):
            if unit_id == clean(unit.get("unit_id")):
                return unit
    raise SystemExit(f"Item not found: {item}")


def docs_by_id(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {doc.get("document_id"): doc for doc in extraction.get("documents", [])}


def find_supporting_item(doc: dict[str, Any] | None, item_id: str) -> dict[str, Any]:
    if not doc or not item_id:
        return {}
    for item in doc.get("supporting_items", []):
        if item.get("item_id") == item_id:
            return item
    return {}


def doc_summary(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not doc:
        return {}
    invoice = doc.get("invoice") or {}
    classification = doc.get("classification") or {}
    schedule = doc.get("supporting_schedule") or {}
    source_file = doc.get("source_file", "")
    return {
        "document_id": doc.get("document_id", ""),
        "file": Path(source_file).name if source_file else "",
        "path": source_file,
        "role": doc.get("document_role", ""),
        "subtype": doc.get("document_subtype", ""),
        "invoice_no": invoice.get("invoice_no", ""),
        "seller_name": invoice.get("seller_name", ""),
        "issue_date": invoice.get("issue_date", ""),
        "amount": invoice.get("total_amount", ""),
        "category": classification.get("expense_category", ""),
        "expense_date": classification.get("expense_date", ""),
        "expense_note": classification.get("expense_note", ""),
        "reported_total_amount": schedule.get("reported_total_amount", ""),
        "period": f"{schedule.get('period_start', '')} - {schedule.get('period_end', '')}".strip(" -"),
    }


def trace_payload(allocation: dict[str, Any], extraction: dict[str, Any], item: str) -> dict[str, Any]:
    unit = resolve_unit(allocation, item)
    docs = docs_by_id(extraction)
    doc_ids = []
    for field in ["source_document_id", "supporting_invoice_document_id", "supporting_schedule_document_id"]:
        doc_id = unit.get(field)
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
    source_doc = docs.get(unit.get("source_document_id"))
    supporting_item = find_supporting_item(source_doc, unit.get("source_item_id", ""))
    related_questions = [
        q for q in allocation.get("questions", [])
        if unit.get("unit_id") in q.get("unit_ids", [])
    ]
    return {
        "item_no": unit_no(unit),
        "unit_id": unit.get("unit_id", ""),
        "current_unit": {
            "source_file": unit.get("source_file", ""),
            "source_filename": unit.get("source_filename", ""),
            "supporting_invoice_file": unit.get("supporting_invoice_file", ""),
            "supporting_invoice_filename": unit.get("supporting_invoice_filename", ""),
            "supporting_schedule_file": unit.get("supporting_schedule_file", ""),
            "supporting_schedule_filename": unit.get("supporting_schedule_filename", ""),
            "invoice_no": unit.get("invoice_no", ""),
            "seller_name": unit.get("seller_name", ""),
            "amount": unit.get("amount", ""),
            "expense_date": unit.get("expense_date", ""),
            "source_category": unit.get("source_category", ""),
            "final_template_column": unit.get("final_template_column", ""),
            "city": unit.get("city", ""),
            "origin": unit.get("origin", ""),
            "destination": unit.get("destination", ""),
            "source_note": unit.get("source_note", ""),
            "client_name": unit.get("client_name", ""),
            "client_charge_code": unit.get("client_charge_code", ""),
            "status": unit.get("status", ""),
        },
        "source_documents": [doc_summary(docs.get(doc_id)) for doc_id in doc_ids],
        "supporting_item": supporting_item,
        "related_questions": related_questions,
    }


def print_text(payload: dict[str, Any]) -> None:
    unit = payload["current_unit"]
    print(f"第{payload['item_no']}项")
    print(f"当前识别: 日期 {unit.get('expense_date') or '-'}, 金额 {unit.get('amount') or '-'}, 分类 {unit.get('source_category') or '-'} -> {unit.get('final_template_column') or '-'}")
    if unit.get("seller_name") or unit.get("invoice_no"):
        print(f"发票信息: 发票号 {unit.get('invoice_no') or '-'}, 开具方/服务方 {unit.get('seller_name') or '-'}")
    if unit.get("origin") or unit.get("destination"):
        print(f"行程信息: {unit.get('origin') or '-'} -> {unit.get('destination') or '-'}, 城市 {unit.get('city') or '-'}")
    if unit.get("source_note"):
        print(f"识别备注: {unit.get('source_note')}")
    print("")
    print("来源文件:")
    for doc in payload["source_documents"]:
        if not doc:
            continue
        role = doc.get("role") or "-"
        print(f"- {doc.get('file') or '-'} [{role}]")
        print(f"  路径: {doc.get('path') or '-'}")
        details = []
        for key, label in [
            ("invoice_no", "发票号"),
            ("seller_name", "开具方"),
            ("issue_date", "开票日期"),
            ("amount", "票面金额"),
            ("category", "分类"),
            ("expense_date", "费用日期"),
            ("reported_total_amount", "行程单合计"),
            ("period", "行程周期"),
        ]:
            if doc.get(key):
                details.append(f"{label}: {doc[key]}")
        if details:
            print("  " + "；".join(details))
    if payload.get("supporting_item"):
        print("")
        print("行程单明细:")
        print(json.dumps(payload["supporting_item"], ensure_ascii=False, indent=2))
    print("")
    print("如果识别有误，可以直接回复类似：")
    print(f"第{payload['item_no']}项金额应为 123.45，日期应为 2026-06-09，分类应为 meal，Note 写加班餐费。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trace an allocation item back to its source evidence.")
    parser.add_argument("--allocation", required=True, help="Path to process/expense-allocation.json.")
    parser.add_argument("--extraction", required=True, help="Path to process/invoice-extraction.json.")
    parser.add_argument("--item", required=True, help="User-facing item number, e.g. 9, or internal UNIT id.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args(argv)

    allocation = load_json(Path(args.allocation))
    extraction = load_json(Path(args.extraction))
    payload = trace_payload(allocation, extraction, args.item)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
