#!/usr/bin/env python3
"""One-command workflow status: where are we, what is stale, what is next.

Run this whenever the user asks about progress, when resuming after an
interruption, or when unsure what to do next. Relay the output to the user.

Usage:
  python scripts/check_workflow_status.py [--process-dir process] [--output-root output]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import integrity


def configure_stdio() -> None:
    """Status is often run from Windows terminals that cannot encode all markers."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_file_path(folder: Path, filename: Any) -> Path | None:
    name = str(filename or "")
    if not name or Path(name).name != name:
        return None
    return folder / name


def validate_package_manifest(
    manifest: dict[str, Any],
    workbook: Path,
    expected_workbook_sha: str,
    final_rows_fingerprint: str,
) -> tuple[bool, str]:
    ok, reason = integrity.check(manifest)
    if not ok:
        return False, f"manifest integrity failed: {reason}"
    if Path(str(manifest.get("package_root", ""))).resolve() != workbook.parent.resolve():
        return False, "manifest package_root does not match the workbook folder"
    if str(manifest.get("workbook", "")) != workbook.name:
        return False, "manifest workbook name does not match the packaged workbook"
    if str(manifest.get("workbook_sha256", "")) != expected_workbook_sha:
        return False, "manifest workbook hash does not match current final rows"
    if str(manifest.get("final_rows_fingerprint", "")) != final_rows_fingerprint:
        return False, "manifest belongs to an older or different final-rows generation"
    issues = manifest.get("issues", [])
    if not isinstance(issues, list):
        return False, "manifest issues field is malformed"
    if issues:
        return False, f"package has {len(issues)} unresolved issue(s)"

    for key, folder_name, count_key in [
        ("invoice_files", "发票", "invoice_count"),
        ("support_files", "支持文档", "support_count"),
    ]:
        files = manifest.get(key, [])
        if not isinstance(files, list):
            return False, f"manifest {key} field is malformed"
        try:
            count = int(manifest.get(count_key, -1))
        except (TypeError, ValueError):
            return False, f"manifest {count_key} is invalid"
        if count != len(files):
            return False, f"manifest {count_key} does not match its file list"
        folder = workbook.parent / folder_name
        if not folder.is_dir():
            return False, f"package folder is missing: {folder_name}"
        for item in files:
            if not isinstance(item, dict):
                return False, f"manifest {key} contains a malformed item"
            path = manifest_file_path(folder, item.get("filename"))
            if path is None or not path.is_file():
                return False, f"package file is missing: {folder_name}/{item.get('filename', '?')}"
            expected_file_sha = str(item.get("sha256", ""))
            if not expected_file_sha:
                return False, f"package file has no recorded hash: {folder_name}/{path.name}"
            if sha256_file(path) != expected_file_sha:
                return False, f"package file hash mismatch: {folder_name}/{path.name}"
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Report reimbursement workflow status.")
    parser.add_argument("--process-dir", default="process")
    parser.add_argument("--output-root", default="output")
    args = parser.parse_args(argv)
    pdir = Path(args.process_dir)

    lines: list[str] = []
    next_action = ""
    integrity_blocked = False

    # Stage 1
    extraction = load(pdir / "invoice-extraction.json")
    extraction_fp = ""
    if not extraction:
        lines.append("Stage 1 提取: ✗ 未运行 (no invoice-extraction.json)")
        next_action = "run extract_invoices.py on ALL provided files"
    else:
        ok, reason = integrity.check(extraction)
        extraction_fp = str((extraction.get("integrity") or {}).get("fingerprint", "")) if ok else ""
        docs = extraction.get("documents", [])
        excluded = [d for d in docs if d.get("excluded_by_user")]
        pending = [d for d in docs if not d.get("excluded_by_user")
                   and (d.get("needs_review") or d.get("document_role") == "unknown")]
        unresolved_inputs = [
            item for item in extraction.get("unresolved_input_files", [])
            if item.get("status", "open") == "open"
        ]
        state = "✓" if ok else "BLOCKED"
        stamp_note = "" if ok else f" [INTEGRITY FAILED: {reason}]"
        lines.append(f"Stage 1 提取: {state} {len(docs)} 份文档，排除 {len(excluded)}，待识别/复核 {len(pending)}{stamp_note}")
        if unresolved_inputs:
            lines.append(f"  - BLOCKED: {len(unresolved_inputs)} 个不支持的输入文件尚未记录用户处理决定")
            for item in unresolved_inputs[:10]:
                lines.append(f"    * {item.get('filename', '?')}")
        for d in pending[:10]:
            lines.append(f"  - 待识别: {Path(str(d.get('source_file', '?'))).name}")
        if not ok:
            integrity_blocked = True
            next_action = "re-run extract_invoices.py; make changes only through apply_extraction_corrections.py"
        elif unresolved_inputs:
            next_action = "ask the user to exclude or convert every unsupported input, apply input_resolutions, then re-run extract_invoices.py"
        elif pending and not next_action:
            next_action = "resolve unidentified documents (vision/OCR/ask user + apply_extraction_corrections.py)"

    # Stage 2
    allocation = load(pdir / "expense-allocation.json")
    alloc_fp = ""
    if not allocation:
        lines.append("Stage 2 归集: ✗ 未运行")
        if not next_action:
            next_action = "run allocate_expenses.py with project context"
    else:
        ok, reason = integrity.check(allocation)
        alloc_fp = (allocation.get("integrity") or {}).get("fingerprint", "") if ok else ""
        units = allocation.get("allocation_units", [])
        confirmed = [u for u in units if u.get("status") in {"confirmed", "fixed"}]
        closed = [u for u in units if u.get("status") in {"dropped", "excluded", "non_reimbursable"}]
        open_qs = [q for q in allocation.get("questions", []) if q.get("status", "open") == "open"]
        generation_mismatch = bool(extraction_fp) and (
            str(allocation.get("source_extraction_fingerprint", "")) != extraction_fp
        )
        stage2_state = "BLOCKED" if not ok or generation_mismatch else ("✓" if not open_qs else "…")
        stamp_note = "" if ok else f" [INTEGRITY FAILED: {reason}]"
        if generation_mismatch:
            stamp_note += " [STALE: extraction changed after allocation]"
        lines.append(f"Stage 2 归集: {stage2_state} 单元 {len(confirmed)}/{len(units)} 已确认"
                     f"（另排除 {len(closed)}），阻断问题 {len(open_qs)} 个{stamp_note}")
        if not ok:
            integrity_blocked = True
        if not ok and not next_action:
            next_action = "re-run allocate_expenses.py; make changes only through the allocation answers updater"
        elif generation_mismatch and not next_action:
            next_action = "re-run allocate_expenses.py because extraction changed; regenerate the answers template before applying answers"
        elif open_qs and not next_action:
            next_action = f"relay the {len(open_qs)} blocking question(s) to the user verbatim (re-run allocate_expenses.py to reprint them)"

    # Stage 3
    rows = load(pdir / "final-expense-rows.json")
    rows_ready = False
    rows_fingerprint = ""
    if not rows:
        lines.append("Stage 3 报销表: ✗ 未运行（餐费/酒店上限检查尚未发生）")
        if not next_action:
            next_action = "run write_reimbursement_template.py"
    else:
        rows_ok, rows_reason = integrity.check(rows)
        rows_fp = str(rows.get("source_allocation_fingerprint", ""))
        rows_fingerprint = str((rows.get("integrity") or {}).get("fingerprint", ""))
        workbook_sha = str(rows.get("workbook_sha256", ""))
        blocking = int(rows.get("blocking_policy_checks", 0) or 0)
        stale = bool(alloc_fp) and rows_fp != alloc_fp
        if not rows_ok:
            state = f"✗ 完整性失败（{rows_reason}）"
        elif not workbook_sha:
            state = "✗ 缺少工作簿哈希，不能验证或打包"
        elif stale:
            state = "✗ 已过期（allocation 在其生成后被修改）"
        elif blocking:
            state = "… 有阻断的政策检查"
        else:
            state = "✓"
            rows_ready = True
        lines.append(f"Stage 3 报销表: {state}，行数 {len(rows.get('rows', []))}，未决餐费/酒店检查 {blocking} 个")
        if not rows_ok and not next_action:
            next_action = "re-run write_reimbursement_template.py (final rows integrity failed)"
        if not rows_ok:
            integrity_blocked = True
        elif not workbook_sha and not next_action:
            next_action = "re-run write_reimbursement_template.py (workbook hash is missing)"
        elif stale and not next_action:
            next_action = "re-run write_reimbursement_template.py (workbook is stale)"
        elif blocking and not next_action:
            next_action = "relay the stage 3 review summary to the user; resolve via updater, then re-run stage 3"

    # Stage 4: require a current, stamped manifest and every listed package file.
    root = Path(args.output_root)
    candidates = sorted(p for p in root.glob("**/*.xlsx")) if root.exists() else []
    expected_sha = str((rows or {}).get("workbook_sha256", ""))
    verified = None
    incomplete: tuple[Path, str] | None = None
    orphan: Path | None = None
    if rows_ready and expected_sha:
        for c in candidates:
            try:
                if sha256_file(c) != expected_sha:
                    continue
            except OSError:
                continue
            mf = load(c.parent / "package-manifest.json")
            if mf:
                ok, reason = validate_package_manifest(mf, c, expected_sha, rows_fingerprint)
                if ok:
                    verified = c
                    break
                incomplete = (c, reason)
            else:
                orphan = c
    if verified:
        lines.append(f"Stage 4 打包: ✓ {verified.parent}（manifest、final rows 与全部包内文件已验证）")
    elif incomplete:
        candidate, reason = incomplete
        lines.append(f"Stage 4 打包: ✗ {candidate.parent} 不可提交：{reason}")
        if not next_action:
            next_action = "resolve package issues or rebuild the package with package_reimbursement_files.py"
    elif orphan:
        lines.append(f"Stage 4 打包: ? {orphan} 哈希匹配但无有效 package manifest —— 只是被复制的工作簿，不是完整打包")
        if not next_action:
            next_action = "run package_reimbursement_files.py to produce a real package (workbook alone is not a deliverable)"
    elif candidates:
        lines.append(f"Stage 4 打包: ? 发现 {len(candidates)} 个 .xlsx 但均非本批 stage 3 产物（哈希不符/无法验证）")
        if not next_action:
            next_action = "re-run package_reimbursement_files.py with the latest stage 3 workbook"
    elif rows and not rows_ready:
        lines.append("Stage 4 打包: ✗ Stage 3 产物不可验证，不能判断打包状态")
    else:
        lines.append("Stage 4 打包: ✗ 未运行")
        if not next_action:
            next_action = "run package_reimbursement_files.py"

    print("WORKFLOW STATUS (relay to the user):")
    for line in lines:
        print(line)
    print(f"NEXT: {next_action or 'workflow complete — deliver the package summary to the user'}")
    return 2 if integrity_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
