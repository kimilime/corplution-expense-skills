#!/usr/bin/env python3
"""Build the final reimbursement submission package."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


C = {
    "package_prefix": "\u62a5\u9500\u7533\u8bf7\u8868",
    "invoice_folder": "\u53d1\u7968",
    "support_folder": "\u652f\u6301\u6587\u6863",
    "special": "\u4e13\u7968",
    "trip_report": "\u884c\u7a0b\u5355",
    "gaode_trip_report": "\u9ad8\u5fb7\u884c\u7a0b\u5355",
    "substitute_approval": "\u66ff\u7968\u5ba1\u6279",
}


TYPE_NAMES = {
    "flight": "\u98de\u673a",
    "rail": "\u9ad8\u94c1",
    "railway": "\u9ad8\u94c1",
    "railway_e_ticket": "\u9ad8\u94c1",
    "hotel": "\u9152\u5e97",
    "taxi_didi": "\u6ef4\u6ef4",
    "didi": "\u6ef4\u6ef4",
    "taxi": "\u6253\u8f66",
    "gaode": "\u9ad8\u5fb7",
    "meal": "\u9910\u8d39",
    "mobile": "\u901a\u8baf\u8d39",
    "other": "\u5176\u4ed6",
    "travel": "\u9ad8\u94c1",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", "", value)
    return value.strip(". ") or "file"


def proof_no_name(value: Any) -> str:
    try:
        return f"{int(value):03d}"
    except Exception:
        return safe_name(str(value))


def doc_map(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {doc["document_id"]: doc for doc in extraction.get("documents", [])}


def is_invoice_doc(doc: dict[str, Any]) -> bool:
    return doc.get("document_role") == "invoice"


def is_special_invoice(doc: dict[str, Any]) -> bool:
    invoice = doc.get("invoice") or {}
    return doc.get("document_subtype") == "vat_special_invoice" or invoice.get("invoice_type") == "special"


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def invoice_filename(group: dict[str, Any], doc: dict[str, Any]) -> str:
    no = proof_no_name(group.get("proof_no"))
    type_name = TYPE_NAMES.get(group.get("proof_type", ""), TYPE_NAMES["other"])
    amount = group.get("amount_total", "0.00")
    special = f"-{C['special']}" if is_special_invoice(doc) else ""
    ext = Path(doc.get("source_file", "")).suffix or ".pdf"
    return safe_name(f"{no}-{type_name}-{amount}{special}") + ext


def support_filename(proof_no: Any, support_type: str, source: Path) -> str:
    ext = source.suffix or ".pdf"
    return safe_name(f"{proof_no_name(proof_no)}-{support_type}") + ext


def package_paths(output_root: Path, requester: str, package_date: str) -> tuple[Path, Path, Path]:
    root = output_root / safe_name(f"{C['package_prefix']}-{requester}-{package_date}")
    invoice_dir = root / C["invoice_folder"]
    support_dir = root / C["support_folder"]
    invoice_dir.mkdir(parents=True, exist_ok=True)
    support_dir.mkdir(parents=True, exist_ok=True)
    return root, invoice_dir, support_dir


def rows_by_proof(final_rows: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for row in final_rows.get("rows", []):
        try:
            no = int(row.get("proof_no"))
        except Exception:
            continue
        out.setdefault(no, []).append(row)
    return out


def build_package(
    final_rows_path: Path,
    extraction_path: Path,
    workbook_path: Path,
    output_root: Path,
    package_date: str | None,
) -> dict[str, Any]:
    final_rows = load_json(final_rows_path)
    extraction = load_json(extraction_path)
    docs = doc_map(extraction)
    requester = safe_name(final_rows.get("requester", "Requester"))
    package_date = package_date or datetime.now().strftime("%Y%m%d")
    root, invoice_dir, support_dir = package_paths(output_root, requester, package_date)

    workbook_name = safe_name(f"{C['package_prefix']}-{requester}-{package_date}") + ".xlsx"
    workbook_target = root / workbook_name
    copy_file(workbook_path, workbook_target)

    manifest: dict[str, Any] = {
        "schema_version": "reimbursement_package.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "requester": requester,
        "package_date": package_date,
        "package_root": str(root),
        "workbook": workbook_name,
        "invoice_files": [],
        "support_files": [],
        "issues": [],
    }

    rows_lookup = rows_by_proof(final_rows)
    copied_support_files: set[tuple[Any, str, str]] = set()
    for group in final_rows.get("proof_groups", []):
        proof_no = group.get("proof_no")
        invoice_doc = None
        for doc_id in group.get("source_document_ids", []):
            doc = docs.get(doc_id)
            if doc and is_invoice_doc(doc):
                invoice_doc = doc
                break
        if invoice_doc:
            source = Path(invoice_doc.get("source_file", ""))
            if source.exists():
                filename = invoice_filename(group, invoice_doc)
                target = invoice_dir / filename
                copy_file(source, target)
                manifest["invoice_files"].append({
                    "proof_no": proof_no,
                    "filename": filename,
                    "source_file": str(source),
                    "invoice_no": (invoice_doc.get("invoice") or {}).get("invoice_no", ""),
                    "type": TYPE_NAMES.get(group.get("proof_type", ""), TYPE_NAMES["other"]),
                    "amount": group.get("amount_total", "0.00"),
                    "is_special_invoice": is_special_invoice(invoice_doc),
                })
            else:
                manifest["issues"].append({
                    "proof_no": proof_no,
                    "problem": f"Invoice source file not found: {source}",
                })
        else:
            manifest["issues"].append({
                "proof_no": proof_no,
                "problem": "No invoice document found for proof group.",
            })

        support_type = C["gaode_trip_report"] if group.get("proof_type") == "gaode" else C["trip_report"]
        for doc_id in group.get("support_document_ids", []):
            support_doc = docs.get(doc_id)
            if not support_doc:
                continue
            source = Path(support_doc.get("source_file", ""))
            if not source.exists():
                manifest["issues"].append({
                    "proof_no": proof_no,
                    "problem": f"Support source file not found: {source}",
                })
                continue
            filename = support_filename(proof_no, support_type, source)
            support_key = (proof_no, support_type, str(source.resolve()))
            if support_key in copied_support_files:
                continue
            copied_support_files.add(support_key)
            target = support_dir / filename
            copy_file(source, target)
            manifest["support_files"].append({
                "proof_no": proof_no,
                "filename": filename,
                "source_file": str(source),
                "type": support_type,
            })

        for row in rows_lookup.get(int(proof_no), []):
            approval_file = row.get("approval_file") or ""
            if row.get("is_substitute_invoice") and approval_file:
                source = Path(approval_file)
                if source.exists():
                    filename = support_filename(proof_no, C["substitute_approval"], source)
                    support_key = (proof_no, C["substitute_approval"], str(source.resolve()))
                    if support_key in copied_support_files:
                        continue
                    copied_support_files.add(support_key)
                    target = support_dir / filename
                    copy_file(source, target)
                    manifest["support_files"].append({
                        "proof_no": proof_no,
                        "filename": filename,
                        "source_file": str(source),
                        "type": C["substitute_approval"],
                    })
                else:
                    manifest["issues"].append({
                        "proof_no": proof_no,
                        "problem": f"Substitute approval file not found: {source}",
                    })
            elif row.get("is_substitute_invoice"):
                manifest["issues"].append({
                    "proof_no": proof_no,
                    "problem": "Substitute invoice missing approval screenshot.",
                })

    write_json(root / "package-manifest.json", manifest)
    (root / "package-manifest.md").write_text(build_markdown(manifest), encoding="utf-8")
    return manifest


def build_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Reimbursement Package Manifest",
        "",
        f"Generated at: {manifest['generated_at']}",
        f"Requester: {manifest['requester']}",
        f"Package date: {manifest['package_date']}",
        f"Workbook: {manifest['workbook']}",
        "",
        "## Invoice Files",
        "",
        "| No. | File | Type | Amount | Special | Source |",
        "| ---: | --- | --- | ---: | --- | --- |",
    ]
    for item in manifest["invoice_files"]:
        lines.append(
            f"| {item['proof_no']} | {item['filename']} | {item['type']} | {item['amount']} | "
            f"{item['is_special_invoice']} | {item['source_file']} |"
        )
    lines += ["", "## Support Files", "", "| No. | File | Type | Source |", "| ---: | --- | --- | --- |"]
    for item in manifest["support_files"]:
        lines.append(f"| {item['proof_no']} | {item['filename']} | {item['type']} | {item['source_file']} |")
    lines += ["", "## Issues", "", "| No. | Problem |", "| ---: | --- |"]
    for item in manifest["issues"]:
        lines.append(f"| {item.get('proof_no','')} | {item.get('problem','')} |")
    return "\n".join(lines) + "\n"


def print_final_summary(manifest: dict[str, Any]) -> None:
    print("")
    print("FINAL PACKAGE SUMMARY TO SHOW IN CHAT:")
    print(f"Package folder: {manifest['package_root']}")
    print(f"Workbook: {manifest['workbook']}")
    print(f"Invoice files: {len(manifest['invoice_files'])}")
    print(f"Support files: {len(manifest['support_files'])}")
    print(f"Issues: {len(manifest['issues'])}")
    if manifest["issues"]:
        print("Issues to resolve before submission:")
        for item in manifest["issues"]:
            proof_no = item.get("proof_no", "")
            prefix = f"No. {proof_no}: " if proof_no else ""
            print(f"- {prefix}{item.get('problem', '')}")
    else:
        print("No package issues detected.")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Package reimbursement workbook, invoices, and support files.")
    parser.add_argument("--final-rows", required=True, help="Path to process/final-expense-rows.json.")
    parser.add_argument("--extraction", required=True, help="Path to process/invoice-extraction.json.")
    parser.add_argument("--workbook", required=True, help="Final workbook path.")
    parser.add_argument("--output-root", default="output", help="Folder where package root will be created.")
    parser.add_argument("--date", help="Package date YYYYMMDD; defaults to today.")
    args = parser.parse_args(argv)

    manifest = build_package(
        final_rows_path=Path(args.final_rows),
        extraction_path=Path(args.extraction),
        workbook_path=Path(args.workbook),
        output_root=Path(args.output_root),
        package_date=args.date,
    )
    print(f"Wrote package: {manifest['package_root']}")
    print(f"Issues: {len(manifest['issues'])}")
    print_final_summary(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
