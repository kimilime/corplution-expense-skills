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
import allocate_expenses


def configure_stdio() -> None:
    """Status is often run from Windows terminals that cannot encode all markers."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_hidden_package_artifact(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts[:-1])


def validate_project_context(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "project context file does not exist"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"project context JSON cannot be read: {exc}"
    errors = allocate_expenses.context_schema_errors(payload)
    if errors:
        return False, "; ".join(errors)
    return True, "ok"


def recorded_project_context_path(allocation: dict[str, Any] | None, process_dir: Path) -> Path:
    recorded = str((allocation or {}).get("source_project_context_file", "")).strip()
    if not recorded:
        return process_dir.parent / "project-context.json"
    path = Path(recorded).expanduser()
    return path if path.is_absolute() else process_dir.parent / path


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
    expected_hint_count: int,
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
    try:
        manifest_hint_count = int(manifest.get("expense_hint_reconciliation_count", -1))
    except (TypeError, ValueError):
        return False, "manifest expense_hint_reconciliation_count is invalid"
    if manifest_hint_count != expected_hint_count:
        return False, "manifest applicant expense-record count does not match current final rows"
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


FINAL_UNIT_STATUSES = {"confirmed", "fixed", "dropped", "excluded", "non_reimbursable"}
ANSWER_ACTION_FIELDS = (
    "unit_updates",
    "expense_hint_resolutions",
    "question_updates",
    "project_contexts",
    "confirm_units",
    "drop_units",
    "exclude_units",
)


def next_step(
    kind: str,
    stage: str,
    summary: str,
    *,
    operation: str | None = None,
    parameters: dict[str, Any] | None = None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    """Build a machine-readable next action without embedding a shell command."""
    return {
        "kind": kind,
        "stage": stage,
        "summary": summary,
        "operation": operation,
        "parameters": parameters or {},
        "missing": missing or [],
        "argv": None,
    }


def inspect_workflow(
    process_dir: str | Path = "process",
    output_root: str | Path = "output",
) -> dict[str, Any]:
    """Inspect all four stages once and return the shared workflow state.

    This function is the single state source for both the legacy status CLI and
    chief_orchestrator.py. It is read-only and never repairs process artifacts.
    """
    pdir = Path(process_dir)
    root = Path(output_root)
    lines: list[str] = []
    pending_next: dict[str, Any] | None = None
    pending_priority = -1
    integrity_blocked = False
    stages: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}

    def set_next(candidate: dict[str, Any], *, priority: int = 0) -> None:
        nonlocal pending_next, pending_priority
        if pending_next is None or priority > pending_priority:
            pending_next = candidate
            pending_priority = priority

    # Stage 1
    extraction_path = pdir / "invoice-extraction.json"
    extraction = load(extraction_path)
    extraction_fp = ""
    if not extraction:
        if extraction_path.exists():
            lines.append("Stage 1 提取: BLOCKED invoice-extraction.json 无法解析")
            stages["extraction"] = {"number": 1, "status": "blocked", "reason": "malformed_json"}
            integrity_blocked = True
            set_next(next_step(
                "blocked",
                "extraction",
                "invoice-extraction.json cannot be parsed; re-run extraction on all original inputs.",
                missing=["all original invoice/support input paths"],
            ), priority=100)
        else:
            lines.append("Stage 1 提取: ✗ 未运行 (no invoice-extraction.json)")
            stages["extraction"] = {"number": 1, "status": "not_started"}
            set_next(next_step(
                "needs_user",
                "extraction",
                "Provide all invoice, trip-report, approval, and payment-evidence files or folders.",
                operation="extract",
                missing=["source invoice/support file paths"],
            ))
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
        state = "BLOCKED" if not ok else ("…" if unresolved_inputs or pending else "✓")
        stamp_note = "" if ok else f" [INTEGRITY FAILED: {reason}]"
        lines.append(f"Stage 1 提取: {state} {len(docs)} 份文档，排除 {len(excluded)}，待识别/复核 {len(pending)}{stamp_note}")
        if unresolved_inputs:
            lines.append(f"  - BLOCKED: {len(unresolved_inputs)} 个不支持的输入文件尚未记录用户处理决定")
            for item in unresolved_inputs[:10]:
                lines.append(f"    * {item.get('filename', '?')}")
        for d in pending[:10]:
            duplicate = any(
                "duplicate" in str(issue.get("problem", "")).lower()
                for issue in (d.get("issues") or [])
            )
            label = "重复凭证待 Stage 1 排除确认" if duplicate else "待识别"
            lines.append(f"  - {label}: {Path(str(d.get('source_file', '?'))).name}")
        stage1_status = "ready" if ok and not unresolved_inputs and not pending else (
            "blocked" if not ok else "needs_user"
        )
        stages["extraction"] = {
            "number": 1,
            "status": stage1_status,
            "document_count": len(docs),
            "excluded_count": len(excluded),
            "review_count": len(pending),
            "unresolved_input_count": len(unresolved_inputs),
            "integrity_valid": ok,
        }
        artifacts["extraction"] = {
            "path": str(extraction_path),
            "integrity_fingerprint": extraction_fp,
        }
        if not ok:
            integrity_blocked = True
            set_next(next_step(
                "blocked",
                "extraction",
                "Extraction integrity failed; re-run extraction and use only the sanctioned corrections flow.",
                missing=["all original invoice/support input paths"],
            ), priority=100)
        elif unresolved_inputs:
            set_next(next_step(
                "needs_user",
                "extraction",
                "Ask the applicant to exclude or replace every unsupported input, then apply the recorded resolutions.",
                operation="correct-extraction",
                missing=[f"resolution for {item.get('filename', '?')}" for item in unresolved_inputs],
            ))
        elif pending:
            duplicate_names = [
                Path(str(item.get("source_file", "?"))).name
                for item in pending
                if any(
                    "duplicate" in str(issue.get("problem", "")).lower()
                    for issue in (item.get("issues") or [])
                )
            ]
            set_next(next_step(
                "needs_user",
                "extraction",
                (
                    "Resolve every unidentified or review-required document through vision/OCR or applicant confirmation. "
                    "Duplicate files must be excluded in Stage 1 through apply_extraction_corrections.py; "
                    "do not drop their Stage 2 expense units instead."
                    if duplicate_names else
                    "Resolve every unidentified or review-required document through vision/OCR or applicant confirmation."
                ),
                operation="correct-extraction",
                missing=[Path(str(item.get("source_file", "?"))).name for item in pending],
            ))

    stage1_ready = stages.get("extraction", {}).get("status") == "ready"

    # Stage 2
    allocation_path = pdir / "expense-allocation.json"
    allocation = load(allocation_path)
    alloc_fp = ""
    answers_current = False
    current_answer_count = 0
    if not allocation:
        if allocation_path.exists():
            lines.append("Stage 2 归集: BLOCKED expense-allocation.json 无法解析")
            stages["allocation"] = {"number": 2, "status": "blocked", "reason": "malformed_json"}
            integrity_blocked = True
            set_next(next_step(
                "blocked",
                "allocation",
                "expense-allocation.json cannot be parsed; regenerate Stage 2 from the current extraction.",
                missing=["project context file"],
            ), priority=100)
        else:
            lines.append("Stage 2 归集: ✗ 未运行")
            stages["allocation"] = {"number": 2, "status": "not_started"}
            if stage1_ready:
                context_path = pdir.parent / "project-context.json"
                if context_path.is_file():
                    context_ok, context_reason = validate_project_context(context_path)
                    if context_ok:
                        set_next(next_step(
                            "command",
                            "allocation",
                            "Run Stage 2 with the current extraction and canonical project context.",
                            operation="allocate",
                            parameters={"context": str(context_path)},
                        ))
                    else:
                        lines.append(f"  - BLOCKED: project-context.json 结构无效：{context_reason}")
                        stages["allocation"]["status"] = "blocked"
                        stages["allocation"]["context_schema_valid"] = False
                        set_next(next_step(
                            "blocked",
                            "allocation",
                            "Rewrite project-context.json internally using assets/project-context-template.json; "
                            "do not ask the applicant to write JSON or run allocation with an invalid context.",
                            missing=["canonical project-context.json"],
                        ))
                else:
                    set_next(next_step(
                        "needs_user",
                        "allocation",
                        "Collect the consultant's project dates, cities, clients, and charge codes, "
                        "then create project-context.json.",
                        operation="allocate",
                        missing=["project context (date range, city, client, charge code)"],
                    ))
    else:
        ok, reason = integrity.check(allocation)
        alloc_fp = (allocation.get("integrity") or {}).get("fingerprint", "") if ok else ""
        units = allocation.get("allocation_units", [])
        confirmed = [u for u in units if u.get("status") in {"confirmed", "fixed"}]
        closed = [u for u in units if u.get("status") in {"dropped", "excluded", "non_reimbursable"}]
        unconfirmed = [u for u in units if u.get("status") not in FINAL_UNIT_STATUSES]
        open_qs = [q for q in allocation.get("questions", []) if q.get("status", "open") == "open"]
        contexts_have_hints = any(
            context.get(field)
            for context in allocation.get("project_contexts", [])
            for field in ("meal_hints", "expense_hints")
        )
        hint_ledger_missing = contexts_have_hints and "expense_hint_reconciliation" not in allocation
        unresolved_hints = [
            record for record in allocation.get("expense_hint_reconciliation", [])
            if record.get("resolution_status") not in {"not_required", "resolved"}
        ]
        answers_path = pdir / "allocation-answers.json"
        answers = load(answers_path)
        if answers:
            answers_schema_valid = answers.get("schema_version") == "allocation_answers.v1"
            answer_action_counts = {
                field: len(answers.get(field, []))
                if isinstance(answers.get(field, []), list) else 0
                for field in ANSWER_ACTION_FIELDS
            }
            current_answer_count = sum(answer_action_counts.values())
            answers_current = bool(
                alloc_fp
                and current_answer_count
                and answers_schema_valid
                and str(answers.get("source_allocation_fingerprint", "")) == str(alloc_fp)
            )
            artifacts["allocation_answers"] = {
                "path": str(answers_path),
                "source_allocation_fingerprint": str(answers.get("source_allocation_fingerprint", "")),
                "action_count": current_answer_count,
                "action_counts": answer_action_counts,
                "schema_valid": answers_schema_valid,
                "current": answers_current,
            }
        generation_mismatch = bool(extraction_fp) and (
            str(allocation.get("source_extraction_fingerprint", "")) != extraction_fp
        )
        context_path = recorded_project_context_path(allocation, pdir)
        expected_context_sha = str(allocation.get("source_project_context_sha256", "")).strip()
        context_ok, context_reason = validate_project_context(context_path)
        actual_context_sha = ""
        if context_ok:
            try:
                actual_context_sha = sha256_file(context_path)
            except OSError as exc:
                context_ok = False
                context_reason = f"project context cannot be hashed: {exc}"
        context_mismatch = bool(expected_context_sha) and (
            not context_ok or actual_context_sha != expected_context_sha
        )
        upstream_unavailable = not stage1_ready
        stage2_state = "BLOCKED" if not ok or generation_mismatch or context_mismatch or upstream_unavailable or hint_ledger_missing else (
            "✓" if not open_qs and not unconfirmed and not answers_current and not unresolved_hints else "…"
        )
        stamp_note = "" if ok else f" [INTEGRITY FAILED: {reason}]"
        if generation_mismatch:
            stamp_note += " [STALE: extraction changed after allocation]"
        if context_mismatch:
            stamp_note += " [STALE: project context changed, disappeared, or became invalid]"
        if upstream_unavailable and not generation_mismatch:
            stamp_note += " [BLOCKED: extraction is not ready]"
        if hint_ledger_missing:
            stamp_note += " [STALE: user expense-record reconciliation ledger missing]"
        answer_note = f"，待应用答案 {current_answer_count} 项" if answers_current else ""
        hint_count_text = "未知（台账缺失）" if hint_ledger_missing else f"{len(unresolved_hints)} 条"
        lines.append(f"Stage 2 归集: {stage2_state} 单元 {len(confirmed)}/{len(units)} 已确认"
                     f"（另排除 {len(closed)}），阻断问题 {len(open_qs)} 个，未对应用户记录 {hint_count_text}"
                     f"{answer_note}{stamp_note}")
        stage2_ready = (
            ok and not generation_mismatch and not context_mismatch and not upstream_unavailable
            and not hint_ledger_missing and not unresolved_hints
            and not open_qs and not unconfirmed and not answers_current
        )
        stages["allocation"] = {
            "number": 2,
            "status": "ready" if stage2_ready else (
                "blocked" if not ok or generation_mismatch or context_mismatch or upstream_unavailable else (
                    "command_ready" if answers_current else "needs_user"
                )
            ),
            "unit_count": len(units),
            "confirmed_count": len(confirmed),
            "closed_count": len(closed),
            "unconfirmed_count": len(unconfirmed),
            "open_question_count": len(open_qs),
            "unresolved_expense_hint_count": len(unresolved_hints),
            "expense_hint_ledger_missing": hint_ledger_missing,
            "unapplied_answer_count": current_answer_count if answers_current else 0,
            "integrity_valid": ok,
            "generation_mismatch": generation_mismatch,
            "context_mismatch": context_mismatch,
            "context_schema_valid": context_ok,
        }
        artifacts["allocation"] = {
            "path": str(allocation_path),
            "integrity_fingerprint": str(alloc_fp),
            "source_extraction_fingerprint": str(allocation.get("source_extraction_fingerprint", "")),
            "source_project_context_file": str(context_path),
            "source_project_context_sha256": expected_context_sha,
            "actual_project_context_sha256": actual_context_sha,
        }
        if not ok:
            integrity_blocked = True
        if not ok:
            set_next(next_step(
                "blocked",
                "allocation",
                "Allocation integrity failed; regenerate Stage 2 and use only composer/updater for later changes.",
                missing=["current project context file"],
            ), priority=100)
        elif generation_mismatch:
            if context_ok:
                set_next(next_step(
                    "command",
                    "allocation",
                    "Extraction changed; regenerate allocation and all answers from the current extraction.",
                    operation="allocate",
                    parameters={"context": str(context_path)},
                ))
            else:
                set_next(next_step(
                    "blocked",
                    "allocation",
                    "Extraction changed, but the project context is missing or invalid. Rewrite it internally "
                    "from the applicant's notes using assets/project-context-template.json.",
                    missing=[f"canonical project context: {context_reason}"],
                ))
        elif context_mismatch:
            if context_ok:
                set_next(next_step(
                    "command",
                    "allocation",
                    "Project context changed after allocation; regenerate Stage 2 and recompose all answers.",
                    operation="allocate",
                    parameters={"context": str(context_path)},
                ))
            else:
                set_next(next_step(
                    "blocked",
                    "allocation",
                    "The recorded project context disappeared or is invalid. Rewrite the canonical context "
                    "internally before rerunning allocation.",
                    missing=[f"canonical project context: {context_reason}"],
                ))
        elif hint_ledger_missing:
            if context_ok:
                set_next(next_step(
                    "command",
                    "allocation",
                    "User expense hints exist but the reverse reconciliation ledger is missing; regenerate Stage 2.",
                    operation="allocate",
                    parameters={"context": str(context_path)},
                ))
            else:
                set_next(next_step(
                    "blocked",
                    "allocation",
                    "The expense-record ledger is missing and project context is unavailable; restore the canonical context first.",
                    missing=[f"canonical project context: {context_reason}"],
                ))
        elif answers_current:
            set_next(next_step(
                "command",
                "allocation",
                f"Apply the {current_answer_count} current fingerprint-bound answer action(s) "
                "through the official updater.",
                operation="apply",
                parameters={"answers": str(answers_path)},
            ))
        elif open_qs:
            set_next(next_step(
                "needs_user",
                "allocation",
                f"Relay and answer the {len(open_qs)} blocking allocation question(s), "
                "then compose/apply the decisions.",
                operation="compose",
                missing=["applicant answers to open allocation questions"],
            ))
        elif unconfirmed:
            set_next(next_step(
                "needs_user",
                "allocation",
                f"Confirm or close the {len(unconfirmed)} remaining draft allocation unit(s), "
                "then compose/apply the decisions.",
                operation="compose",
                missing=["confirmation for remaining draft allocation units"],
            ))

    stage2_ready = stages.get("allocation", {}).get("status") == "ready"

    # Stage 3
    rows_path = pdir / "final-expense-rows.json"
    rows = load(rows_path)
    rows_ready = False
    rows_fingerprint = ""
    if not rows:
        if rows_path.exists():
            lines.append("Stage 3 报销表: BLOCKED final-expense-rows.json 无法解析")
            stages["workbook"] = {"number": 3, "status": "blocked", "reason": "malformed_json"}
            integrity_blocked = True
            set_next(next_step(
                "blocked",
                "workbook",
                "final-expense-rows.json cannot be parsed; regenerate Stage 3 from the confirmed allocation.",
                missing=["requester and workbook output path"],
            ), priority=100)
        else:
            lines.append("Stage 3 报销表: ✗ 未运行（餐费/酒店上限检查尚未发生）")
            stages["workbook"] = {"number": 3, "status": "not_started"}
            if stage2_ready:
                set_next(next_step(
                    "needs_user",
                    "workbook",
                    "Provide the requester and desired workbook path, then run Stage 3.",
                    operation="write",
                    missing=["requester", "workbook output path"],
                ))
    else:
        rows_ok, rows_reason = integrity.check(rows)
        rows_fp = str(rows.get("source_allocation_fingerprint", ""))
        rows_fingerprint = str((rows.get("integrity") or {}).get("fingerprint", "")) if rows_ok else ""
        workbook_sha = str(rows.get("workbook_sha256", ""))
        try:
            blocking = int(rows.get("blocking_policy_checks", 0) or 0)
        except (TypeError, ValueError):
            blocking = 1
        try:
            preview_open_questions = int(rows.get("open_allocation_questions", 0) or 0)
        except (TypeError, ValueError):
            preview_open_questions = 1
        hint_ledger_present = "expense_hint_reconciliation" in rows
        try:
            unresolved_hint_count = int(rows.get("unresolved_expense_hint_count", -1))
        except (TypeError, ValueError):
            unresolved_hint_count = -1
        preview = bool(rows.get("generated_with_allow_unconfirmed")) or preview_open_questions > 0
        stale = bool(alloc_fp) and rows_fp != alloc_fp
        upstream_unavailable = not stage2_ready
        recorded_workbook = str(rows.get("workbook", ""))
        template_workbook = str(rows.get("template_workbook", ""))
        layout_file = str(rows.get("layout_file", ""))
        workbook_source = str(rows.get("workbook_source", ""))
        workbook_path = Path(recorded_workbook).expanduser() if recorded_workbook else None
        workbook_exists = bool(workbook_path and workbook_path.is_file())
        actual_workbook_sha = ""
        if workbook_exists and workbook_path:
            try:
                actual_workbook_sha = sha256_file(workbook_path)
            except OSError:
                workbook_exists = False
        workbook_mismatch = bool(workbook_sha and actual_workbook_sha and workbook_sha != actual_workbook_sha)
        if not rows_ok:
            state = f"✗ 完整性失败（{rows_reason}）"
        elif upstream_unavailable:
            state = "✗ 上游 allocation 未就绪"
        elif not workbook_sha:
            state = "✗ 缺少工作簿哈希，不能验证或打包"
        elif not hint_ledger_present or unresolved_hint_count < 0:
            state = "✗ 缺少用户费用记录完整性台账，需重跑 Stage 3"
        elif stale:
            state = "✗ 已过期（allocation 在其生成后被修改）"
        elif preview:
            state = "✗ 预览件越过了开放关卡，不能交付"
        elif blocking:
            state = "… 有阻断的政策检查"
        elif unresolved_hint_count:
            state = f"… 有 {unresolved_hint_count} 条用户费用记录尚未对应凭证/明确排除"
        elif not workbook_exists:
            state = "✗ 记录中的工作簿不存在"
        elif workbook_mismatch:
            state = "✗ 工作簿内容与 final rows 哈希不符"
        else:
            state = "✓"
            rows_ready = True
        lines.append(f"Stage 3 报销表: {state}，行数 {len(rows.get('rows', []))}，未决餐费/酒店检查 {blocking} 个")
        if rows_ready:
            stage3_status = "ready"
        elif not rows_ok or upstream_unavailable or not workbook_sha or not hint_ledger_present or unresolved_hint_count < 0 or stale or preview:
            stage3_status = "blocked"
        elif blocking or unresolved_hint_count:
            stage3_status = "needs_user"
        else:
            stage3_status = "blocked"
        stages["workbook"] = {
            "number": 3,
            "status": stage3_status,
            "row_count": len(rows.get("rows", [])),
            "blocking_policy_check_count": blocking,
            "unresolved_expense_hint_count": max(unresolved_hint_count, 0),
            "expense_hint_ledger_present": hint_ledger_present,
            "preview": preview,
            "integrity_valid": rows_ok,
            "allocation_stale": stale,
            "workbook_exists": workbook_exists,
            "workbook_hash_matches": bool(actual_workbook_sha and actual_workbook_sha == workbook_sha),
        }
        artifacts["final_rows"] = {
            "path": str(rows_path),
            "integrity_fingerprint": rows_fingerprint,
            "source_allocation_fingerprint": rows_fp,
        }
        artifacts["workbook"] = {
            "path": recorded_workbook,
            "recorded_sha256": workbook_sha,
            "actual_sha256": actual_workbook_sha,
        }
        if not rows_ok:
            integrity_blocked = True
        if not rows_ok:
            set_next(next_step(
                "blocked",
                "workbook",
                "Final rows integrity failed; regenerate Stage 3 instead of repairing the derived file.",
                missing=["requester and workbook output path"],
            ), priority=100)
        elif upstream_unavailable:
            set_next(next_step(
                "blocked",
                "workbook",
                "The upstream allocation is not ready; resolve the earlier stage before regenerating Stage 3.",
            ))
        elif not workbook_sha or not hint_ledger_present or unresolved_hint_count < 0 or stale or preview or not workbook_exists or workbook_mismatch:
            requester = str(rows.get("requester", ""))
            write_parameters = {"requester": requester, "output": recorded_workbook}
            missing_write_source: list[str] = []
            if template_workbook:
                write_parameters["template"] = template_workbook
            elif layout_file:
                write_parameters["layout"] = layout_file
            elif workbook_source == "template":
                missing_write_source.append("original template workbook path")
            if requester and recorded_workbook and stage2_ready and not missing_write_source:
                set_next(next_step(
                    "command",
                    "workbook",
                    "Regenerate the workbook and final rows from the current confirmed allocation.",
                    operation="write",
                    parameters=write_parameters,
                ))
            else:
                set_next(next_step(
                    "needs_user",
                    "workbook",
                    "Regenerate Stage 3 after confirming its requester and workbook output path.",
                    operation="write",
                    missing=["requester", "workbook output path", *missing_write_source],
                ))
        elif blocking or unresolved_hint_count:
            if answers_current:
                set_next(next_step(
                    "command",
                    "workbook",
                    "Apply the current policy-check decisions through the updater, then rerun Stage 3.",
                    operation="apply",
                    parameters={"answers": str(pdir / "allocation-answers.json")},
                ))
            else:
                set_next(next_step(
                    "needs_user",
                    "workbook",
                    "Show the Stage 3 meal/hotel review summary, resolve every blocking check "
                    "through the updater, then rerun Stage 3.",
                    operation="compose",
                    missing=["applicant decision for blocking meal/hotel policy checks"],
                ))

    # Stage 4: require a current, stamped manifest and every listed package file.
    candidates = sorted(
        path for path in root.glob("**/*.xlsx")
        if not is_hidden_package_artifact(path, root)
    ) if root.exists() else []
    expected_sha = str((rows or {}).get("workbook_sha256", ""))
    expected_hint_count = len((rows or {}).get("expense_hint_reconciliation", []))
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
            manifest_path = c.parent / "package-manifest.json"
            mf = load(manifest_path)
            if manifest_path.exists() and mf is None:
                incomplete = (c, "manifest integrity failed: manifest JSON cannot be parsed")
            elif mf is not None:
                ok, reason = validate_package_manifest(
                    mf,
                    c,
                    expected_sha,
                    rows_fingerprint,
                    expected_hint_count,
                )
                if ok:
                    verified = c
                    break
                incomplete = (c, reason)
            else:
                orphan = c
    if verified:
        lines.append(f"Stage 4 打包: ✓ {verified.parent}（manifest、final rows 与全部包内文件已验证）")
        stages["package"] = {"number": 4, "status": "ready", "package_root": str(verified.parent)}
        manifest = load(verified.parent / "package-manifest.json") or {}
        artifacts["package_manifest"] = {
            "path": str(verified.parent / "package-manifest.json"),
            "integrity_fingerprint": str((manifest.get("integrity") or {}).get("fingerprint", "")),
        }
    elif incomplete:
        candidate, reason = incomplete
        lines.append(f"Stage 4 打包: ✗ {candidate.parent} 不可提交：{reason}")
        stages["package"] = {"number": 4, "status": "blocked", "reason": reason}
        if reason.startswith("manifest integrity failed:"):
            integrity_blocked = True
            set_next(next_step(
                "blocked",
                "package",
                "Package manifest integrity failed; rebuild Stage 4 from the current verified workbook.",
            ), priority=100)
        elif reason.startswith("package has "):
            set_next(next_step(
                "needs_user",
                "package",
                "Resolve every package manifest issue, then rebuild the package.",
                operation="package",
                missing=[reason],
            ))
        else:
            set_next(next_step(
                "command",
                "package",
                "Rebuild the package because its manifest or packaged files no longer validate.",
                operation="package",
            ))
    elif orphan:
        lines.append(f"Stage 4 打包: ? {orphan} 哈希匹配但无有效 package manifest —— 只是被复制的工作簿，不是完整打包")
        stages["package"] = {"number": 4, "status": "not_started", "reason": "orphan_workbook"}
        set_next(next_step(
            "command",
            "package",
            "Build a complete package; a copied workbook without a valid manifest is not deliverable.",
            operation="package",
        ))
    elif candidates:
        lines.append(f"Stage 4 打包: ? 发现 {len(candidates)} 个 .xlsx 但均非本批 stage 3 产物（哈希不符/无法验证）")
        stages["package"] = {"number": 4, "status": "not_started", "reason": "wrong_workbook_candidates"}
        set_next(next_step(
            "command",
            "package",
            "Package the exact workbook recorded by the latest final rows.",
            operation="package",
        ))
    elif rows and not rows_ready:
        lines.append("Stage 4 打包: ✗ Stage 3 产物不可验证，不能判断打包状态")
        stages["package"] = {"number": 4, "status": "blocked", "reason": "stage3_not_ready"}
    else:
        lines.append("Stage 4 打包: ✗ 未运行")
        stages["package"] = {"number": 4, "status": "not_started"}
        if rows_ready:
            set_next(next_step(
                "command",
                "package",
                "Build the final submission package from the current verified workbook.",
                operation="package",
            ))

    if pending_next is None:
        pending_next = next_step(
            "complete",
            "complete",
            "Workflow complete; deliver the verified package summary to the applicant.",
        )

    return {
        "schema_version": "reimbursement_workflow_state.v1",
        "process_dir": str(pdir),
        "output_root": str(root),
        "stages": stages,
        "artifacts": artifacts,
        "lines": lines,
        "next": pending_next,
        "integrity_blocked": integrity_blocked,
        "complete": pending_next.get("kind") == "complete",
    }


def render_status(state: dict[str, Any]) -> str:
    lines = ["WORKFLOW STATUS (relay to the user):", *state.get("lines", [])]
    lines.append(f"NEXT: {(state.get('next') or {}).get('summary', 'inspect workflow state')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Report reimbursement workflow status.")
    parser.add_argument("--process-dir", default="process")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--json", action="store_true", help="Print the shared machine-readable workflow state.")
    args = parser.parse_args(argv)

    state = inspect_workflow(args.process_dir, args.output_root)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(render_status(state))
    return 2 if state.get("integrity_blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
