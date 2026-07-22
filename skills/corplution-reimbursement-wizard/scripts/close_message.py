"""Build, validate, and render the final reimbursement Close Message."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CATEGORY_NAMES = {
    "hotel": "酒店",
    "flight": "飞机",
    "rail": "高铁",
    "taxi": "打车",
    "travel": "交通",
    "meal": "餐费",
    "mobile": "通讯费",
    "other": "其他",
}

INACTIVE_UNIT_STATUSES = {"dropped", "excluded", "non_reimbursable"}


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(clean(value) or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money(value: Any) -> str:
    return f"{decimal_value(value):.2f}"


def display_money(value: Any) -> str:
    return f"¥{decimal_value(value):,.2f}"


def markdown_text(value: Any) -> str:
    return clean(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def row_category(row: dict[str, Any]) -> str:
    row_type = clean(row.get("row_order_type")).lower()
    source_category = clean(row.get("source_category")).lower()
    if row_type == "hotel" or source_category == "hotel":
        return "hotel"
    if row_type == "flight" or source_category == "flight":
        return "flight"
    if row_type in {"rail", "railway", "railway_e_ticket"} or source_category == "rail":
        return "rail"
    if row_type in {"taxi", "taxi_didi", "didi", "gaode"} or source_category == "taxi":
        return "taxi"
    if row_type == "meal" or source_category == "meal":
        return "meal"
    if row_type == "mobile" or source_category == "mobile":
        return "mobile"
    if row_type == "travel" or source_category == "travel":
        return "travel"
    return "other"


def latest_unit_change(
    allocation: dict[str, Any],
    unit_id: str,
    *,
    changed_field: str | None = None,
    final_status: str | None = None,
) -> dict[str, Any] | None:
    for entry in reversed(allocation.get("change_log", [])):
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        for change in reversed(changes if isinstance(changes, list) else []):
            if clean(change.get("unit_id")) != unit_id:
                continue
            before = change.get("before") if isinstance(change.get("before"), dict) else {}
            after = change.get("after") if isinstance(change.get("after"), dict) else {}
            if changed_field and before.get(changed_field) == after.get(changed_field):
                continue
            if changed_field and changed_field not in before and changed_field not in after:
                continue
            if final_status is not None and clean(after.get("status")) != final_status:
                continue
            return change
    return None


def decision_reason(change: dict[str, Any] | None, fallback: Any = "") -> str:
    if change:
        answer = clean(change.get("answer"))
        if answer:
            return answer
    return clean(fallback) or "未记录具体原因"


def project_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projects: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        client = clean(row.get("client")) or "未填写 Client"
        charge_code = clean(row.get("client_charge_code")) or "未填写 Code"
        key = f"{client}|{charge_code}"
        project = projects.setdefault(key, {
            "client": client,
            "client_charge_code": charge_code,
            "row_count": 0,
            "total": Decimal("0"),
            "category_counts": {name: 0 for name in CATEGORY_NAMES},
            "category_totals": {name: Decimal("0") for name in CATEGORY_NAMES},
        })
        category = row_category(row)
        amount = decimal_value(row.get("reimbursable_amount", row.get("amount")))
        project["row_count"] += 1
        project["total"] += amount
        project["category_counts"][category] += 1
        project["category_totals"][category] += amount

    return [
        {
            "client": project["client"],
            "client_charge_code": project["client_charge_code"],
            "row_count": project["row_count"],
            "total": money(project["total"]),
            "category_counts": project["category_counts"],
            "category_totals": {
                name: money(value) for name, value in project["category_totals"].items()
            },
        }
        for project in projects.values()
    ]


def build_summary(
    final_rows: dict[str, Any],
    allocation: dict[str, Any],
    extraction: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build the factual, generation-bound source for the final chat message."""
    rows = [row for row in final_rows.get("rows", []) if isinstance(row, dict)]
    units = [unit for unit in allocation.get("allocation_units", []) if isinstance(unit, dict)]
    units_by_id = {clean(unit.get("unit_id")): unit for unit in units}

    amount_adjustments = []
    for row in rows:
        invoice_amount = decimal_value(row.get("invoice_amount"))
        reimbursable_amount = decimal_value(row.get("reimbursable_amount", row.get("amount")))
        if invoice_amount == reimbursable_amount:
            continue
        unit_id = clean(row.get("source_unit_id"))
        unit = units_by_id.get(unit_id, {})
        change = latest_unit_change(allocation, unit_id, changed_field="reimbursable_amount")
        amount_adjustments.append({
            "unit_id": unit_id,
            "user_no": row.get("user_no", ""),
            "proof_no": row.get("proof_no", ""),
            "date": clean(row.get("expense_date") or row.get("date")),
            "category": CATEGORY_NAMES[row_category(row)],
            "description": clean(row.get("seller_name") or row.get("source_filename") or row.get("note")),
            "invoice_amount": money(invoice_amount),
            "reimbursable_amount": money(reimbursable_amount),
            "difference": money(reimbursable_amount - invoice_amount),
            "reason": decision_reason(change, unit.get("correction_note")),
        })

    omitted_units = []
    for unit in units:
        status = clean(unit.get("status"))
        zero_reimbursement = (
            status not in INACTIVE_UNIT_STATUSES
            and clean(unit.get("reimbursable_amount")) != ""
            and decimal_value(unit.get("reimbursable_amount")) == 0
        )
        if status not in INACTIVE_UNIT_STATUSES and not zero_reimbursement:
            continue
        unit_id = clean(unit.get("unit_id"))
        change = latest_unit_change(
            allocation,
            unit_id,
            changed_field="reimbursable_amount" if zero_reimbursement else None,
            final_status=None if zero_reimbursement else status,
        )
        omitted_units.append({
            "unit_id": unit_id,
            "user_no": unit.get("user_no", ""),
            "status": "zero_reimbursement" if zero_reimbursement else status,
            "date": clean(unit.get("expense_date")),
            "category": CATEGORY_NAMES[row_category({
                "row_order_type": unit.get("proof_type", ""),
                "source_category": unit.get("source_category", ""),
            })],
            "description": clean(
                unit.get("seller_name")
                or Path(clean(unit.get("source_filename") or unit.get("source_file"))).name
                or unit.get("final_note")
                or unit.get("expense_note")
            ),
            "amount": money(unit.get("invoice_amount", unit.get("amount"))),
            "reason": decision_reason(change, unit.get("correction_note")),
        })

    excluded_evidence = []
    for doc in extraction.get("documents", []):
        if not isinstance(doc, dict) or not doc.get("excluded_by_user"):
            continue
        invoice = doc.get("invoice") if isinstance(doc.get("invoice"), dict) else {}
        excluded_evidence.append({
            "document_id": clean(doc.get("document_id")),
            "kind": clean(doc.get("document_role")) or "document",
            "filename": Path(clean(doc.get("source_file"))).name,
            "amount": money(invoice.get("total_amount")) if clean(invoice.get("total_amount")) else "",
            "reason": clean(doc.get("exclusion_reason")) or "未记录具体原因",
        })
    for item in extraction.get("unresolved_input_files", []):
        if not isinstance(item, dict) or clean(item.get("status")) != "exclude":
            continue
        excluded_evidence.append({
            "document_id": "",
            "kind": "unsupported_input",
            "filename": clean(item.get("filename")) or Path(clean(item.get("source_file"))).name,
            "amount": "",
            "reason": clean(item.get("resolution")) or "未记录具体原因",
        })

    not_reimbursed_records = []
    for record in final_rows.get("expense_hint_reconciliation", []):
        if not isinstance(record, dict) or clean(record.get("resolution_action")) != "not_reimbursed":
            continue
        answer = clean(record.get("resolution_answer"))
        if answer == "not_reimbursed":
            answer = "用户确认不报销；未记录具体原因"
        not_reimbursed_records.append({
            "record_ref": clean(record.get("display_token") or record.get("display_ref") or record.get("hint_id")),
            "summary": clean(record.get("summary")) or clean(record.get("hint_id")),
            "category": clean(record.get("source_category")),
            "reason": answer or "用户确认不报销；未记录具体原因",
        })

    policy_advisories = []
    for check_type, checks in (
        ("餐费", final_rows.get("meal_daily_cap_checks", [])),
        ("酒店", final_rows.get("hotel_cap_checks", [])),
    ):
        for check in checks if isinstance(checks, list) else []:
            if not isinstance(check, dict) or clean(check.get("severity")) != "advisory":
                continue
            policy_advisories.append({
                "type": check_type,
                "date": clean(check.get("date") or check.get("check_in_date")),
                "policy_name": clean(check.get("policy_name")),
                "total": money(check.get("total", check.get("amount"))),
                "cap": money(check.get("cap", check.get("cap_total"))),
                "over_by": money(check.get("over_by")),
                "status": clean(check.get("status")),
            })

    projects = project_summaries(rows)
    invoice_exclusions = sum(1 for item in excluded_evidence if item["kind"] == "invoice")
    support_exclusions = sum(
        1 for item in excluded_evidence
        if item["kind"] in {"supporting_document", "supporting_schedule"}
    )
    return {
        "schema_version": "reimbursement_close_summary.v1",
        "packaged_invoice_count": len(manifest.get("invoice_files", [])),
        "packaged_support_count": len(manifest.get("support_files", [])),
        "excluded_evidence_count": len(excluded_evidence),
        "excluded_invoice_count": invoice_exclusions,
        "excluded_support_count": support_exclusions,
        "omitted_unit_count": len(omitted_units),
        "not_reimbursed_record_count": len(not_reimbursed_records),
        "amount_adjustment_count": len(amount_adjustments),
        "policy_advisory_count": len(policy_advisories),
        "project_count": len(projects),
        "grand_total": money(sum((
            decimal_value(row.get("reimbursable_amount", row.get("amount"))) for row in rows
        ), Decimal("0"))),
        "amount_adjustments": amount_adjustments,
        "policy_advisories": policy_advisories,
        "excluded_evidence": excluded_evidence,
        "omitted_units": omitted_units,
        "not_reimbursed_records": not_reimbursed_records,
        "projects": projects,
    }


def project_cell(project: dict[str, Any], categories: tuple[str, ...]) -> str:
    totals = project.get("category_totals", {})
    counts = project.get("category_counts", {})
    parts = []
    for category in categories:
        total = decimal_value(totals.get(category))
        if total == 0:
            continue
        label = CATEGORY_NAMES[category]
        count = int(counts.get(category, 0) or 0)
        parts.append(f"{label} {display_money(total)}（{count}项）")
    return " + ".join(parts) or "—"


def render(manifest: dict[str, Any]) -> str:
    summary = manifest.get("close_summary") if isinstance(manifest.get("close_summary"), dict) else {}
    lines = [
        "报销资料已整理、校验并完成打包。以下是本次工作的最终摘要。",
        "",
        "| 项目 | 内容 |",
        "| :--- | :--- |",
        f"| **包路径** | `{markdown_text(manifest.get('package_root'))}` |",
        f"| **Excel** | `{markdown_text(manifest.get('workbook'))}` |",
    ]
    invoice_detail = f"{summary.get('packaged_invoice_count', len(manifest.get('invoice_files', [])))} 张"
    if int(summary.get("excluded_invoice_count", 0) or 0):
        invoice_detail += f"（另排除 {summary['excluded_invoice_count']} 张）"
    support_detail = f"{summary.get('packaged_support_count', len(manifest.get('support_files', [])))} 份"
    if int(summary.get("excluded_support_count", 0) or 0):
        support_detail += f"（另排除 {summary['excluded_support_count']} 份）"
    lines.extend([
        f"| **发票** | {invoice_detail} |",
        f"| **支持文件** | {support_detail} |",
        f"| **报销总额** | {display_money(summary.get('grand_total'))} |",
        f"| **未纳入费用项** | {summary.get('omitted_unit_count', 0)} 项 |",
        f"| **用户记录未报销** | {summary.get('not_reimbursed_record_count', 0)} 条 |",
    ])

    adjustments = summary.get("amount_adjustments", [])
    advisories = summary.get("policy_advisories", [])
    if adjustments or advisories:
        lines.extend(["", "### 已处理的金额与政策事项", ""])
        for item in adjustments:
            subject = "｜".join(filter(None, [
                markdown_text(item.get("date")),
                markdown_text(item.get("category")),
                markdown_text(item.get("description")),
            ]))
            lines.append(
                f"- **{subject or markdown_text(item.get('unit_id'))}** "
                f"{display_money(item.get('invoice_amount'))} → {display_money(item.get('reimbursable_amount'))}："
                f"{markdown_text(item.get('reason'))}"
            )
        for item in advisories:
            subject = "｜".join(filter(None, [
                markdown_text(item.get("date")),
                markdown_text(item.get("type")),
                markdown_text(item.get("policy_name")),
            ]))
            lines.append(
                f"- **{subject or '政策提示'}** 合计 {display_money(item.get('total'))} / "
                f"标准 {display_money(item.get('cap'))} / 超出 {display_money(item.get('over_by'))}："
                f"{markdown_text(item.get('status'))}（advisory，不阻断）"
            )

    excluded = summary.get("excluded_evidence", [])
    omitted = summary.get("omitted_units", [])
    not_reimbursed = summary.get("not_reimbursed_records", [])
    if excluded or omitted or not_reimbursed:
        lines.extend(["", "### 未纳入本次报销", ""])
        for item in excluded:
            amount = f" / {display_money(item.get('amount'))}" if clean(item.get("amount")) else ""
            lines.append(
                f"- **排除文件** {markdown_text(item.get('filename'))}{amount}："
                f"{markdown_text(item.get('reason'))}"
            )
        for item in omitted:
            subject = "｜".join(filter(None, [
                markdown_text(item.get("date")),
                markdown_text(item.get("category")),
                markdown_text(item.get("description")),
            ]))
            lines.append(
                f"- **费用项未报** {subject or markdown_text(item.get('unit_id'))} / "
                f"{display_money(item.get('amount'))}：{markdown_text(item.get('reason'))}"
            )
        for item in not_reimbursed:
            lines.append(
                f"- **用户记录无票/无唯一凭证不报** {markdown_text(item.get('summary'))}："
                f"{markdown_text(item.get('reason'))}"
            )

    projects = summary.get("projects", [])
    lines.extend([
        "",
        f"### {len(projects)} 个项目汇总",
        "",
        "| Client Charge Code | 酒店 | 交通 | 餐费 | 其他 | 合计 |",
        "| :--- | ---: | :--- | ---: | :--- | ---: |",
    ])
    for project in projects:
        project_name = " ".join(filter(None, [
            markdown_text(project.get("client_charge_code")),
            markdown_text(project.get("client")),
        ]))
        lines.append(
            f"| {project_name} | {project_cell(project, ('hotel',))} | "
            f"{project_cell(project, ('flight', 'rail', 'taxi', 'travel'))} | "
            f"{project_cell(project, ('meal',))} | {project_cell(project, ('mobile', 'other'))} | "
            f"{display_money(project.get('total'))} |"
        )
    lines.extend(["", "如有疑问或需要修改，请继续对话。"])
    return "\n".join(lines)


def validate(summary: Any, *, invoice_count: Any, support_count: Any) -> tuple[bool, str]:
    if not isinstance(summary, dict):
        return False, "manifest close_summary is missing; rerun Stage 4 to generate the required Close Message"
    if summary.get("schema_version") != "reimbursement_close_summary.v1":
        return False, "manifest close_summary schema is invalid; rerun Stage 4"
    list_fields = (
        "amount_adjustments",
        "policy_advisories",
        "excluded_evidence",
        "omitted_units",
        "not_reimbursed_records",
        "projects",
    )
    for key in list_fields:
        if not isinstance(summary.get(key), list):
            return False, f"manifest close_summary.{key} is malformed"
    count_pairs = (
        ("amount_adjustment_count", "amount_adjustments"),
        ("policy_advisory_count", "policy_advisories"),
        ("excluded_evidence_count", "excluded_evidence"),
        ("omitted_unit_count", "omitted_units"),
        ("not_reimbursed_record_count", "not_reimbursed_records"),
        ("project_count", "projects"),
    )
    for count_key, list_key in count_pairs:
        try:
            count = int(summary.get(count_key, -1))
        except (TypeError, ValueError):
            return False, f"manifest close_summary.{count_key} is invalid"
        if count != len(summary[list_key]):
            return False, f"manifest close_summary.{count_key} does not match {list_key}"
    try:
        grand_total = Decimal(str(summary.get("grand_total", "")))
    except (InvalidOperation, ValueError):
        return False, "manifest close_summary.grand_total is invalid"
    if not grand_total.is_finite():
        return False, "manifest close_summary.grand_total must be finite"
    for key, expected in (
        ("packaged_invoice_count", invoice_count),
        ("packaged_support_count", support_count),
    ):
        try:
            actual_count = int(summary.get(key, -1))
            expected_count = int(expected)
        except (TypeError, ValueError):
            return False, f"manifest close_summary.{key} is invalid"
        if actual_count != expected_count:
            return False, f"manifest close_summary.{key} does not match the package manifest"
    return True, "ok"
