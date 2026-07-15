#!/usr/bin/env python3
"""Build the final reimbursement submission package."""

from __future__ import annotations

import argparse
import hashlib
import json
import integrity
import subagent_protocol
import re
import shutil
import sys
import time
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
    "support_document": "\u652f\u6301\u6587\u6863",
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


class PackagePromotionError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
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
    independent_review = subagent_protocol.review_state(
        final_rows_path.parent,
        allocation,
        allocation_path,
        extraction_path,
    )
    if (
        independent_review.get("current")
        and independent_review.get("outcome") == "block"
        and int(independent_review.get("blocking_count", 0) or 0) > 0
    ):
        print(
            "ERROR: the current Kaede independent review contains blocking findings. "
            "NEXT: resolve them through Composer/Updater, obtain a fresh review, rerun Stage 3, then package.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if independent_review.get("current"):
        recorded_review = final_rows.get("independent_review", {})
        if not isinstance(recorded_review, dict):
            recorded_review = {}
        current_review_fp = str(independent_review.get("result_fingerprint", ""))
        recorded_review_fp = str(recorded_review.get("result_fingerprint", ""))
        if current_review_fp and current_review_fp != recorded_review_fp:
            print(
                "ERROR: the current independent review was accepted after this workbook was generated. "
                "NEXT: rerun Stage 3 so final rows consume the current review result, then package.",
                file=sys.stderr,
            )
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
    if "expense_hint_reconciliation" not in final_rows:
        print(
            "ERROR: final rows do not contain the user expense-record reconciliation ledger. "
            "They were generated by an older Stage 3 and cannot prove that every user note was matched "
            "or explicitly resolved. NEXT: rerun Stage 2 if needed, then rerun Stage 3 and package again.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    allocation_hint_records = allocation.get("expense_hint_reconciliation", [])
    final_hint_records = final_rows.get("expense_hint_reconciliation", [])
    if final_hint_records != allocation_hint_records:
        print(
            "ERROR: the final rows expense-record reconciliation ledger does not match the current allocation. "
            "NEXT: rerun Stage 3, then package again.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    unresolved_hint_records = [
        record for record in allocation_hint_records
        if record.get("resolution_status") not in {"not_required", "resolved"}
    ]
    recorded_unresolved = int(final_rows.get("unresolved_expense_hint_count", -1) or 0)
    if unresolved_hint_records or recorded_unresolved != len(unresolved_hint_records):
        print(
            f"ERROR: {len(unresolved_hint_records)} user expense record(s) still lack a deliverable resolution. "
            "A pending-invoice decision records progress but remains blocking; supply the evidence or mark the "
            "record not reimbursed. NEXT: relay the hint reconciliation "
            "questions, resolve them through Composer/updater, rerun Stage 3, then package.",
            file=sys.stderr,
        )
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
        "expense_hint_reconciliation_count": len(final_hint_records),
    }

    rows_lookup = rows_by_proof(final_rows)
    copied_support_files: set[tuple[Any, str, str]] = set()
    # Guards one physical file being packaged twice under two labels within the
    # same proof (e.g. a substitute approval that was also mounted generically).
    copied_support_sources: set[tuple[Any, str]] = set()
    used_support_filenames: set[str] = set()

    def add_support_file(proof_no: Any, source: Path, support_type: str) -> None:
        """Copy one existing support file into the package under its type label.

        Deduplicates by (proof, type) and by (proof, physical source) so the same
        evidence is never packaged twice, and never overwrites another file.
        """
        resolved = str(source.resolve())
        if (proof_no, resolved) in copied_support_sources:
            return
        support_key = (proof_no, support_type, resolved)
        if support_key in copied_support_files:
            return
        copied_support_files.add(support_key)
        copied_support_sources.add((proof_no, resolved))
        filename = reserve_filename(support_filename(proof_no, support_type, source), used_support_filenames)
        target = support_dir / filename
        copy_file(source, target)
        manifest["support_files"].append({
            "proof_no": proof_no,
            "filename": filename,
            "sha256": sha256_file(target),
            "source_file": str(source),
            "type": support_type,
        })

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
            add_support_file(proof_no, source, support_type)

        # Standalone supporting documents (payment receipts, non-substitute
        # approval screenshots, other user-kept evidence) the user tied to this
        # proof's invoice via supports_document_id. Each carries its own label.
        for support in group.get("support_documents", []):
            source = Path(support.get("source_file", ""))
            label = (support.get("support_type") or "").strip() or C["support_document"]
            if not source.exists():
                manifest["issues"].append({
                    "proof_no": proof_no,
                    "problem": f"Support source file not found: {source}",
                })
                continue
            add_support_file(proof_no, source, label)

        for row in rows_lookup.get(int(proof_no), []):
            approval_file = row.get("approval_file") or ""
            if row.get("is_substitute_invoice") and approval_file:
                source = Path(approval_file)
                if source.exists():
                    add_support_file(proof_no, source, C["substitute_approval"])
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
        f"Applicant expense records reconciled: {manifest.get('expense_hint_reconciliation_count', 0)}",
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


def replace_path_with_retry(
    source: Path,
    target: Path,
    *,
    attempts: int = 6,
    initial_delay: float = 0.10,
) -> None:
    delay = initial_delay
    last_error: PermissionError | None = None
    for attempt in range(1, attempts + 1):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(delay)
            delay *= 2
    raise PackagePromotionError(
        f"Windows could not rename {source} to {target} after {attempts} attempts because a file or "
        "folder is still open. Close the packaged Excel workbook and any Explorer preview/window "
        "inside the package folder, then rerun Stage 4 through Chief; direct invocation is not a workaround."
    ) from last_error


def remove_tree_with_retry(
    path: Path,
    *,
    attempts: int = 6,
    initial_delay: float = 0.10,
) -> None:
    delay = initial_delay
    last_error: PermissionError | None = None
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(delay)
            delay *= 2
    raise PackagePromotionError(
        f"Windows still locks cleanup directory {path}. Close workbook/Explorer previews and remove "
        "that hidden staging/previous directory later; atomic promotion keeps the final package state protected."
    ) from last_error


def promote_package(staging_root: Path, final_root: Path) -> None:
    """Atomically replace the previous package only after a complete new build exists."""
    final_root.parent.mkdir(parents=True, exist_ok=True)
    backup_root = final_root.parent / f".{final_root.name}.previous-{uuid4().hex}"
    if final_root.exists():
        if not final_root.is_dir():
            raise RuntimeError(f"Package destination exists but is not a directory: {final_root}")
        replace_path_with_retry(final_root, backup_root)
    try:
        replace_path_with_retry(staging_root, final_root)
    except Exception:
        if backup_root.exists() and not final_root.exists():
            replace_path_with_retry(backup_root, final_root)
        raise
    if backup_root.exists():
        try:
            remove_tree_with_retry(backup_root)
        except PackagePromotionError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)


def print_final_summary(manifest: dict[str, Any]) -> None:
    print("")
    print("FINAL PACKAGE SUMMARY TO SHOW IN CHAT:")
    print(f"Package folder: {manifest['package_root']}")
    print(f"Workbook: {manifest['workbook']}")
    print(f"Invoice files: {len(manifest['invoice_files'])}")
    print(f"Support files: {len(manifest['support_files'])}")
    print(f"Applicant expense records reconciled: {manifest.get('expense_hint_reconciliation_count', 0)}")
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
    except PackagePromotionError as exc:
        if staging_root.exists():
            try:
                remove_tree_with_retry(staging_root)
            except PackagePromotionError as cleanup_exc:
                print(f"WARNING: {cleanup_exc}", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
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
