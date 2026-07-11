#!/usr/bin/env python3
"""Build the final reimbursement submission package."""

from __future__ import annotations

import argparse
import hashlib
import json
import integrity
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def reserve_filename(filename: str, used_names: set[str]) -> str:
    """Return a deterministic unique package filename without overwriting evidence."""
    candidate = filename
    base = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while candidate.lower() in used_names:
        candidate = f"{base}-{index}{suffix}"
        index += 1
    used_names.add(candidate.lower())
    return candidate


def package_paths(root: Path) -> tuple[Path, Path, Path]:
    if root.exists():
        raise RuntimeError(f"Refusing to build into an existing package directory: {root}")
    invoice_dir = root / C["invoice_folder"]
    support_dir = root / C["support_folder"]
    invoice_dir.mkdir(parents=True)
    support_dir.mkdir(parents=True)
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
    package_root: Path,
    package_date: str | None,
) -> dict[str, Any]:
    final_rows = load_json(final_rows_path)
    integrity.require_valid(final_rows, final_rows_path, kind="final_rows")
    allocation_path = final_rows_path.parent / "expense-allocation.json"
    if not allocation_path.exists():
        print(f"ERROR: {allocation_path} not found next to final rows; cannot verify provenance. "
              "Run packaging with final rows inside the process directory.", file=sys.stderr)
        raise SystemExit(2)
    allocation = load_json(allocation_path)
    integrity.require_valid(allocation, allocation_path)
    expected_context_sha = str(allocation.get("source_project_context_sha256", "")).strip()
    recorded_context = str(allocation.get("source_project_context_file", "")).strip()
    if expected_context_sha:
        context_path = Path(recorded_context).expanduser() if recorded_context else Path()
        if recorded_context and not context_path.is_absolute():
            context_path = allocation_path.parent.parent / context_path
        if not recorded_context or not context_path.is_file():
            print(
                "ERROR: the project context used by allocation is missing. Restore/rewrite canonical "
                "project-context.json, rerun Stage 2 and Composer, then regenerate Stage 3.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        try:
            actual_context_sha = hashlib.sha256(context_path.read_bytes()).hexdigest()
        except OSError as exc:
            print(f"ERROR: project context cannot be read for provenance validation: {exc}", file=sys.stderr)
            raise SystemExit(2)
        if actual_context_sha != expected_context_sha:
            print(
                "ERROR: project context changed after allocation. Rerun Stage 2, recompose/apply answers, "
                "regenerate Stage 3, then package.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    current_fp = allocation.get("integrity", {}).get("fingerprint", "")
    rows_fp = str(final_rows.get("source_allocation_fingerprint", ""))
    if rows_fp != current_fp:
        print("ERROR: the workbook/final rows were generated from an OLDER allocation generation "
              f"({rows_fp[:8] or '<missing>'}... vs current {current_fp[:8]}...). The allocation was "
              "modified after stage 3 ran — the workbook is stale. NEXT: re-run "
              "write_reimbursement_template.py, then package again.", file=sys.stderr)
        raise SystemExit(2)
    if final_rows.get("generated_with_allow_unconfirmed"):
        print("ERROR: this workbook was generated with --allow-unconfirmed (a PREVIEW past open "
              "gates) and must not be packaged as the deliverable. NEXT: resolve remaining "
              "questions/confirmations, re-run stage 3 WITHOUT --allow-unconfirmed, then package.",
              file=sys.stderr)
        raise SystemExit(2)
    open_qs = int(final_rows.get("open_allocation_questions", 0) or 0)
    if open_qs:
        print(f"ERROR: allocation had {open_qs} open question(s) when this workbook was generated. "
              "NEXT: relay the questions to the user, resolve them, re-run stage 3, then package.",
              file=sys.stderr)
        raise SystemExit(2)
    expected_sha = str(final_rows.get("workbook_sha256", ""))
    if not expected_sha:
        print("ERROR: final rows carry no workbook_sha256 — they were generated by an older "
              "stage 3 or forged. Verification cannot be skipped. NEXT: re-run "
              "write_reimbursement_template.py, then package.", file=sys.stderr)
        raise SystemExit(2)
    if not workbook_path.exists():
        print(f"ERROR: --workbook {workbook_path} does not exist. NEXT: pass the workbook from the "
              "latest stage 3 run (path recorded in final-expense-rows.json).", file=sys.stderr)
        raise SystemExit(2)
    actual_sha = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        print(f"ERROR: --workbook {workbook_path} is NOT the workbook stage 3 generated for these "
              f"final rows (sha {actual_sha[:8]}... vs expected {expected_sha[:8]}...). You are "
              "packaging a stale or wrong Excel file. NEXT: pass the exact file from the latest "
              "stage 3 run (see final-expense-rows.json 'workbook' path), or re-run stage 3.",
              file=sys.stderr)
        raise SystemExit(2)
    blocking = int(final_rows.get("blocking_policy_checks", 0) or 0)
    if blocking:
        print(f"ERROR: stage 3 left {blocking} blocking policy check(s) (meal/hotel caps) unresolved. "
              "NEXT: relay the stage 3 review summary to the user, resolve via the answers updater, "
              "re-run stage 3, then package.", file=sys.stderr)
        raise SystemExit(2)
    extraction = load_json(extraction_path)
    integrity.require_valid(extraction, extraction_path, kind="extraction")
    extraction_fp = (extraction.get("integrity") or {}).get("fingerprint", "")
    allocation_extraction_fp = str(allocation.get("source_extraction_fingerprint", ""))
    if not allocation_extraction_fp or allocation_extraction_fp != extraction_fp:
        print("ERROR: allocation does not match the current extraction generation. Re-run "
              "allocate_expenses.py, recompose decisions, and reapply confirmed answers "
              "before writing Excel and packaging.", file=sys.stderr)
        raise SystemExit(2)
    unresolved_inputs = [
        item for item in extraction.get("unresolved_input_files", [])
        if item.get("status", "open") == "open"
    ]
    if unresolved_inputs:
        print("ERROR: packaging is blocked because unsupported input files have no recorded user decision: "
              + ", ".join(str(item.get("filename", "?")) for item in unresolved_inputs) + ". "
              "Resolve them through apply_extraction_corrections.py, then rerun the downstream stages.",
              file=sys.stderr)
        raise SystemExit(2)
    docs = doc_map(extraction)
    requester = safe_name(final_rows.get("requester", "Requester"))
    package_date = package_date or datetime.now().strftime("%Y%m%d")
    root, invoice_dir, support_dir = package_paths(package_root)

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
    used_support_filenames: set[str] = set()
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
                    "sha256": sha256_file(target),
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
            filename = reserve_filename(filename, used_support_filenames)
            target = support_dir / filename
            copy_file(source, target)
            manifest["support_files"].append({
                "proof_no": proof_no,
                "filename": filename,
                "sha256": sha256_file(target),
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
                    filename = reserve_filename(filename, used_support_filenames)
                    target = support_dir / filename
                    copy_file(source, target)
                    manifest["support_files"].append({
                        "proof_no": proof_no,
                        "filename": filename,
                        "sha256": sha256_file(target),
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

    return manifest


def build_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Reimbursement Package Manifest",
        "",
        f"Generated at: {manifest['generated_at']}",
        f"Requester: {manifest['requester']}",
        f"Package date: {manifest['package_date']}",
        f"Workbook: {manifest['workbook']}",
        f"Workbook SHA-256: {manifest.get('workbook_sha256', '')}",
        f"Final rows fingerprint: {manifest.get('final_rows_fingerprint', '')}",
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


def persist_manifest(manifest: dict[str, Any], final_rows: dict[str, Any], package_root: Path) -> Path:
    """Write a manifest into a staging root while recording its final location."""
    root = package_root
    workbook_path = root / manifest["workbook"]
    expected_sha = str(final_rows.get("workbook_sha256", ""))
    packaged_sha = sha256_file(workbook_path)
    if packaged_sha != expected_sha:
        raise RuntimeError(
            "Copied package workbook hash does not match final rows; remove the package and re-run Stage 3."
        )
    manifest["workbook_sha256"] = packaged_sha
    manifest["final_rows_fingerprint"] = (final_rows.get("integrity") or {}).get("fingerprint", "")
    manifest["invoice_count"] = len(manifest.get("invoice_files", []))
    manifest["support_count"] = len(manifest.get("support_files", []))
    integrity.stamp(manifest, "package_reimbursement_files.py")
    manifest_path = root / "package-manifest.json"
    write_json(manifest_path, manifest)
    (root / "package-manifest.md").write_text(build_markdown(manifest), encoding="utf-8")
    return manifest_path


def promote_package(staging_root: Path, final_root: Path) -> None:
    """Atomically replace the previous package only after a complete new build exists."""
    final_root.parent.mkdir(parents=True, exist_ok=True)
    backup_root = final_root.parent / f".{final_root.name}.previous-{uuid4().hex}"
    if final_root.exists():
        if not final_root.is_dir():
            raise RuntimeError(f"Package destination exists but is not a directory: {final_root}")
        final_root.replace(backup_root)
    try:
        staging_root.replace(final_root)
    except Exception:
        if backup_root.exists() and not final_root.exists():
            backup_root.replace(final_root)
        raise
    if backup_root.exists():
        shutil.rmtree(backup_root)


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

    final_rows_path = Path(args.final_rows)
    final_rows_for_path = load_json(final_rows_path)
    integrity.require_valid(final_rows_for_path, final_rows_path, kind="final_rows")
    requester = safe_name(final_rows_for_path.get("requester", "Requester"))
    package_date = args.date or datetime.now().strftime("%Y%m%d")
    output_root = Path(args.output_root)
    final_root = output_root / safe_name(f"{C['package_prefix']}-{requester}-{package_date}")
    staging_root = output_root / f".{final_root.name}.staging-{uuid4().hex}"

    try:
        manifest = build_package(
            final_rows_path=final_rows_path,
            extraction_path=Path(args.extraction),
            workbook_path=Path(args.workbook),
            package_root=staging_root,
            package_date=package_date,
        )
        final_rows = load_json(final_rows_path)
        manifest["package_root"] = str(final_root)
        persist_manifest(manifest, final_rows, staging_root)
        promote_package(staging_root, final_root)
        manifest_path = final_root / "package-manifest.json"
    except BaseException:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise

    print(f"Wrote package: {manifest['package_root']}")
    print(f"Wrote manifest: {manifest_path}")
    print(f"Issues: {len(manifest['issues'])}")
    print_final_summary(manifest)
    if manifest["issues"]:
        print(
            "PACKAGE CREATED WITH BLOCKING ISSUES — do not submit or call this workflow complete. "
            "Resolve the issues above, then re-run packaging.",
            file=sys.stderr,
        )
        return 3
    print("WORKFLOW COMPLETE — relay the package summary above to the user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
