#!/usr/bin/env python3
"""Supervised facade for the Corplution reimbursement workflow.

The chief does not extract, allocate, mutate process JSON, write workbooks, or
package evidence itself. It fills canonical paths, dispatches the existing
scripts, preserves their exit codes, records privacy-minimized journal events,
and asks the shared status engine what should happen next.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import check_workflow_status
import workflow_journal

SCRIPT_DIR = Path(__file__).resolve().parent

RUN_METADATA = {
    "dependencies": ("environment", "check_dependencies.py"),
    "extract": ("stage1", "extract_invoices.py"),
    "correct-extraction": ("stage1-correction", "apply_extraction_corrections.py"),
    "allocate": ("stage2", "allocate_expenses.py"),
    "compose": ("stage2-control", "compose_answers.py"),
    "apply": ("stage2-update", "apply_allocation_answers.py"),
    "trace": ("stage2-query", "trace_expense_item.py"),
    "rebase": ("stage2-rebase", "rebase_allocation_decisions.py"),
    "prepare-agent": ("subagent-pilot", "subagent_protocol.py"),
    "accept-agent": ("subagent-pilot", "subagent_protocol.py"),
    "promote-proposals": ("subagent-pilot", "subagent_protocol.py"),
    "write": ("stage3", "write_reimbursement_template.py"),
    "package": ("stage4", "package_reimbursement_files.py"),
}


class OrchestratorError(ValueError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def add_run_parsers(run_parser: argparse.ArgumentParser) -> None:
    stages = run_parser.add_subparsers(dest="run_stage", required=True)

    dependencies = stages.add_parser("dependencies", help="Check or install bundled dependencies.")
    dependencies.add_argument("--install", action="store_true")
    dependencies.add_argument("--strict-ocr", action="store_true")

    extract = stages.add_parser("extract", help="Run Stage 1 on every supplied evidence input.")
    extract.add_argument("inputs", nargs="+")

    correct = stages.add_parser("correct-extraction", help="Apply sanctioned extraction corrections/resolutions.")
    correct.add_argument("--corrections", required=True)
    correct.add_argument("--dry-run", action="store_true")

    allocate = stages.add_parser("allocate", help="Run Stage 2 with project context.")
    allocate.add_argument("--context", required=True)

    compose = stages.add_parser("compose", help="Compose and dry-run allocation answers.")
    compose.add_argument("--set", action="append", default=[], dest="specs")
    compose_source = compose.add_mutually_exclusive_group()
    compose_source.add_argument("--decisions")
    compose_source.add_argument("--proposal")

    apply_answers = stages.add_parser("apply", help="Apply current allocation answers through the updater.")
    apply_answers.add_argument("--answers", help="Defaults to process/allocation-answers.json.")
    apply_answers.add_argument("--dry-run", action="store_true")

    rebase = stages.add_parser("rebase", help="Carry decisions across an allocation regeneration by evidence ref.")
    rebase.add_argument("--old", help="Optional explicit source; otherwise discover the latest decided lineage generation.")

    prepare_agent = stages.add_parser(
        "prepare-agent",
        help="Prepare a path-free immutable task packet for an optional read-only subagent.",
    )
    prepare_agent.add_argument(
        "--role",
        choices=["allocation_analyst", "independent_reviewer"],
        required=True,
    )

    accept_agent = stages.add_parser(
        "accept-agent",
        help="Validate and stamp a returned subagent JSON result.",
    )
    accept_agent.add_argument(
        "--role",
        choices=["allocation_analyst", "independent_reviewer"],
        required=True,
    )
    accept_agent.add_argument("--result", required=True, help="UTF-8 JSON returned by the fresh subagent.")

    promote = stages.add_parser(
        "promote-proposals",
        help="Promote explicitly reviewed Otako proposal IDs into canonical decisions.",
    )
    selection = promote.add_mutually_exclusive_group(required=True)
    selection.add_argument("--select", action="append", default=[])
    selection.add_argument("--all", action="store_true")
    promote.add_argument("--reviewed-by", choices=["coordinator", "applicant"], required=True)
    promote.add_argument("--note", default="")
    promote.add_argument("--output")

    trace = stages.add_parser("trace", help="Trace a user-facing expense item to source evidence.")
    trace.add_argument("--item", required=True)
    trace.add_argument("--json", action="store_true")

    write = stages.add_parser("write", help="Run Stage 3 workbook generation and preflight.")
    write.add_argument("--requester", required=True)
    write.add_argument("--output", required=True)
    write.add_argument("--template")
    write.add_argument("--layout")

    package = stages.add_parser("package", help="Run Stage 4 with the current final rows and workbook.")
    package.add_argument("--workbook", help="Defaults to the workbook recorded by final-expense-rows.json.")
    package.add_argument("--date")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Navigate and supervise the reimbursement workflow.")
    parser.add_argument("--process-dir", default="process")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--journal", help="Defaults to <process-dir>/workflow-journal.jsonl.")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="Show all four stages from the shared status engine.")
    status.add_argument("--json", action="store_true")

    next_parser = commands.add_parser("next", help="Show exactly one next action or required user input.")
    next_parser.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="Dispatch one canonical workflow operation.")
    add_run_parsers(run)
    return parser


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def canonical_paths(args: argparse.Namespace) -> dict[str, Path]:
    pdir = Path(args.process_dir)
    return {
        "process": pdir,
        "extraction": pdir / "invoice-extraction.json",
        "allocation": pdir / "expense-allocation.json",
        "answers": pdir / "allocation-answers.json",
        "final_rows": pdir / "final-expense-rows.json",
    }


def is_within(path: Path, folder: Path) -> bool:
    resolved_path = path.resolve()
    resolved_folder = folder.resolve()
    return resolved_path == resolved_folder or resolved_folder in resolved_path.parents


def validate_extract_roots(args: argparse.Namespace, paths: dict[str, Path]) -> None:
    internal_paths = [
        paths["process"],
        Path(args.output_root),
        Path(args.journal) if args.journal else paths["process"] / "workflow-journal.jsonl",
    ]
    for raw_input in args.inputs:
        input_path = Path(raw_input).expanduser()
        if not input_path.is_dir():
            continue
        overlaps = [internal for internal in internal_paths if is_within(internal, input_path)]
        if overlaps:
            raise OrchestratorError(
                f"extract input directory {input_path} contains workflow-generated process/output/log paths. "
                "Pass the upload/evidence folder or explicit source files instead of the task root."
            )


def build_child_command(args: argparse.Namespace) -> tuple[str, str, list[str]]:
    stage = args.run_stage
    journal_stage, script_name = RUN_METADATA[stage]
    paths = canonical_paths(args)
    child: list[str] = [sys.executable, "-X", "utf8", str(SCRIPT_DIR / script_name)]

    if stage == "dependencies":
        if args.install:
            child.append("--install")
        if args.strict_ocr:
            child.append("--strict-ocr")
    elif stage == "extract":
        validate_extract_roots(args, paths)
        child.extend(["--output", str(paths["process"]), *args.inputs])
    elif stage == "correct-extraction":
        child.extend(["--extraction", str(paths["extraction"]), "--corrections", args.corrections])
        if args.dry_run:
            child.append("--dry-run")
    elif stage == "allocate":
        child.extend([
            "--extraction", str(paths["extraction"]),
            "--context", args.context,
            "--output", str(paths["process"]),
        ])
    elif stage == "compose":
        if not args.specs and not args.decisions and not args.proposal:
            raise OrchestratorError("compose requires at least one --set, --decisions, or --proposal input")
        child.extend([
            "--allocation", str(paths["allocation"]),
            "--output", str(paths["answers"]),
        ])
        for spec in args.specs:
            child.extend(["--set", spec])
        if args.decisions:
            child.extend(["--decisions", args.decisions])
        if args.proposal:
            child.extend(["--proposal", args.proposal])
    elif stage == "apply":
        child.extend([
            "--allocation", str(paths["allocation"]),
            "--answers", args.answers or str(paths["answers"]),
        ])
        if args.dry_run:
            child.append("--dry-run")
    elif stage == "rebase":
        if args.old:
            child.extend(["--old", args.old])
        child.extend([
            "--new", str(paths["allocation"]),
            "--output", str(paths["process"] / "rebase-decisions.json"),
        ])
    elif stage == "prepare-agent":
        display_name = (
            "Otako - Allocation Analyst"
            if args.role == "allocation_analyst"
            else "Kaede - Independent Reviewer"
        )
        journal_stage = f"subagent-{args.role}"
        script_name = f"{display_name} via subagent_protocol.py"
        child.extend([
            "prepare",
            "--role", args.role,
            "--allocation", str(paths["allocation"]),
            "--extraction", str(paths["extraction"]),
            "--process-dir", str(paths["process"]),
        ])
    elif stage == "accept-agent":
        display_name = (
            "Otako - Allocation Analyst"
            if args.role == "allocation_analyst"
            else "Kaede - Independent Reviewer"
        )
        journal_stage = f"subagent-{args.role}"
        script_name = f"{display_name} via subagent_protocol.py"
        child.extend([
            "accept",
            "--role", args.role,
            "--allocation", str(paths["allocation"]),
            "--extraction", str(paths["extraction"]),
            "--process-dir", str(paths["process"]),
            "--result", args.result,
        ])
    elif stage == "promote-proposals":
        journal_stage = "subagent-allocation_analyst"
        script_name = "Otako - Allocation Analyst proposal promotion via subagent_protocol.py"
        child.extend([
            "promote",
            "--allocation", str(paths["allocation"]),
            "--extraction", str(paths["extraction"]),
            "--process-dir", str(paths["process"]),
            "--reviewed-by", args.reviewed_by,
        ])
        for group in args.select:
            child.extend(["--select", group])
        if args.all:
            child.append("--all")
        if args.note:
            child.extend(["--note", args.note])
        if args.output:
            child.extend(["--output", args.output])
    elif stage == "trace":
        child.extend([
            "--allocation", str(paths["allocation"]),
            "--extraction", str(paths["extraction"]),
            "--item", args.item,
        ])
        if args.json:
            child.append("--json")
    elif stage == "write":
        child.extend([
            "--allocation", str(paths["allocation"]),
            "--output", args.output,
            "--requester", args.requester,
            "--process-dir", str(paths["process"]),
        ])
        if args.template:
            child.extend(["--template", args.template])
        if args.layout:
            child.extend(["--layout", args.layout])
    elif stage == "package":
        workbook = args.workbook
        if not workbook:
            final_rows = load_json(paths["final_rows"])
            workbook = str((final_rows or {}).get("workbook", ""))
        if not workbook:
            raise OrchestratorError(
                "package cannot determine the workbook; rerun Stage 3 or pass --workbook explicitly"
            )
        child.extend([
            "--final-rows", str(paths["final_rows"]),
            "--extraction", str(paths["extraction"]),
            "--workbook", workbook,
            "--output-root", args.output_root,
        ])
        if args.date:
            child.extend(["--date", args.date])
    else:  # pragma: no cover - argparse and RUN_METADATA keep this unreachable.
        raise OrchestratorError(f"unsupported run stage: {stage}")

    return journal_stage, script_name, child


def journal_path(args: argparse.Namespace) -> Path:
    return Path(args.journal) if args.journal else Path(args.process_dir) / "workflow-journal.jsonl"


def print_journal_warning(warning: str | None) -> None:
    if warning:
        print(f"WARNING: {warning}. The workflow command will continue.", file=sys.stderr)


def safe_snapshot(process_dir: str | Path, output_root: str | Path) -> dict[str, Any]:
    try:
        return workflow_journal.snapshot_artifacts(process_dir, output_root)
    except Exception as exc:
        print(f"WARNING: workflow artifact snapshot failed: {exc}. Dispatch will continue.", file=sys.stderr)
        return {}


def normalize_child_exit_code(returncode: int) -> int:
    """Preserve ordinary codes; translate POSIX signals to shell-style 128+signal."""
    return 128 + abs(returncode) if returncode < 0 else returncode


def run_child(
    *,
    stage: str,
    script_name: str,
    command: list[str],
    process_dir: str | Path,
    output_root: str | Path,
    journal: str | Path,
) -> int:
    run_id = str(uuid4())
    before = safe_snapshot(process_dir, output_root)
    print_journal_warning(workflow_journal.record_event(
        journal,
        process_dir=process_dir,
        run_id=run_id,
        stage=stage,
        script=script_name,
        event="started",
        input_artifacts=before,
    ))

    print(f"CHIEF dispatch: {script_name}", flush=True)
    started = time.monotonic()
    try:
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        result = subprocess.run(command, env=child_env)
        exit_code = normalize_child_exit_code(int(result.returncode))
    except KeyboardInterrupt:
        exit_code = 130
    except OSError as exc:
        print(f"ERROR: could not launch {script_name}: {exc}", file=sys.stderr)
        exit_code = 2
    duration_ms = int((time.monotonic() - started) * 1000)
    after = safe_snapshot(process_dir, output_root)
    print_journal_warning(workflow_journal.record_event(
        journal,
        process_dir=process_dir,
        run_id=run_id,
        stage=stage,
        script=script_name,
        event="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        duration_ms=duration_ms,
        input_artifacts=before,
        output_artifacts=after,
    ))
    return exit_code


def chief_argv(
    operation: str,
    parameters: dict[str, Any],
    *,
    process_dir: str,
    output_root: str,
    journal: str | None,
) -> list[str] | None:
    base = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--process-dir", process_dir,
        "--output-root", output_root,
    ]
    if journal:
        base.extend(["--journal", journal])
    base.extend(["run", operation])
    if operation == "allocate":
        context = str(parameters.get("context", ""))
        return [*base, "--context", context] if context else None
    if operation == "write":
        requester = str(parameters.get("requester", ""))
        output = str(parameters.get("output", ""))
        if not requester or not output:
            return None
        command = [*base, "--requester", requester, "--output", output]
        template = str(parameters.get("template", ""))
        layout = str(parameters.get("layout", ""))
        if template:
            command.extend(["--template", template])
        if layout:
            command.extend(["--layout", layout])
        return command
    if operation == "apply":
        answers = str(parameters.get("answers", ""))
        return [*base, "--answers", answers] if answers else base
    if operation == "compose":
        decisions = str(parameters.get("decisions", ""))
        proposal = str(parameters.get("proposal", ""))
        if decisions:
            return [*base, "--decisions", decisions]
        if proposal:
            return [*base, "--proposal", proposal]
        return None
    if operation == "rebase":
        old = str(parameters.get("old", ""))
        return [*base, "--old", old] if old else base
    if operation == "prepare-agent":
        role = str(parameters.get("role", ""))
        return [*base, "--role", role] if role else None
    if operation == "accept-agent":
        role = str(parameters.get("role", ""))
        result = str(parameters.get("result", ""))
        return [*base, "--role", role, "--result", result] if role and result else None
    if operation == "package":
        return base
    return None


def enrich_next(state: dict[str, Any], journal: str | None = None) -> dict[str, Any]:
    result = dict(state.get("next") or {})
    result["parameters"] = dict(result.get("parameters") or {})
    result["missing"] = list(result.get("missing") or [])
    if result.get("kind") == "command" and result.get("operation"):
        result["argv"] = chief_argv(
            str(result["operation"]),
            result["parameters"],
            process_dir=str(state.get("process_dir", "process")),
            output_root=str(state.get("output_root", "output")),
            journal=journal,
        )
        if result["argv"] is None:
            result["kind"] = "needs_user"
            result["missing"].append("parameters required to construct the next command")
    else:
        result["argv"] = None
    result["delegations"] = []
    for recommendation in (state.get("subagents") or {}).get("recommended", []):
        role = str(recommendation.get("role", ""))
        command = chief_argv(
            "prepare-agent",
            {"role": role},
            process_dir=str(state.get("process_dir", "process")),
            output_root=str(state.get("output_root", "output")),
            journal=journal,
        )
        if command:
            result["delegations"].append({
                "role": role,
                "display_name": str(recommendation.get("display_name", role)),
                "reason": str(recommendation.get("reason", "optional independent pass")),
                "argv": command,
            })
    return result


def format_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def render_next(step: dict[str, Any]) -> str:
    lines = [
        "CHIEF NEXT (preferred orchestrated route):",
        f"Kind: {step.get('kind', 'blocked')}",
        f"Stage: {step.get('stage', 'unknown')}",
        f"Action: {step.get('summary', '')}",
    ]
    missing = step.get("missing") or []
    if missing:
        lines.append("Missing:")
        lines.extend(f"- {item}" for item in missing)
    argv = step.get("argv")
    if argv:
        lines.append("Command:")
        lines.append(format_command(argv))
    delegations = step.get("delegations") or []
    if delegations:
        lines.append("Optional subagent pilot (does not replace the NEXT action):")
        for delegation in delegations:
            lines.append(
                f"- {delegation.get('display_name')}: {delegation.get('reason')}"
            )
            lines.append(f"  Prepare: {format_command(delegation.get('argv') or [])}")
    return "\n".join(lines)


def inspect(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    state = check_workflow_status.inspect_workflow(args.process_dir, args.output_root)
    return state, enrich_next(state, args.journal)


def invoked_as_bundled_script() -> bool:
    try:
        return Path(sys.argv[0]).resolve() == Path(__file__).resolve()
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    if not invoked_as_bundled_script():
        direct = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        print(
            "ERROR: chief_orchestrator.py was imported through a wrapper/launcher. "
            "Do not create run_chief.py, modify sys.path, copy the script, or call chief_orchestrator.main().",
            file=sys.stderr,
        )
        print(f"NEXT: execute the bundled Chief directly:\n{format_command(direct)}", file=sys.stderr)
        raise SystemExit(2)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        state, step = inspect(args)
        state = dict(state)
        state["next"] = step
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(check_workflow_status.render_status(state))
            print("")
            print(render_next(step))
        return 2 if state.get("integrity_blocked") else 0

    if args.command == "next":
        state, step = inspect(args)
        if args.json:
            print(json.dumps(step, ensure_ascii=False, indent=2))
        else:
            print(render_next(step))
        return 2 if step.get("kind") == "blocked" else 0

    try:
        stage, script_name, child = build_child_command(args)
    except OrchestratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        rejected_stage, rejected_script = RUN_METADATA.get(
            getattr(args, "run_stage", ""),
            ("orchestrator", "chief_orchestrator.py"),
        )
        before = safe_snapshot(args.process_dir, args.output_root)
        print_journal_warning(workflow_journal.record_event(
            journal_path(args),
            process_dir=args.process_dir,
            run_id=str(uuid4()),
            stage=rejected_stage,
            script=rejected_script,
            event="blocked",
            exit_code=2,
            duration_ms=0,
            input_artifacts=before,
            output_artifacts=before,
        ))
        state, step = inspect(args)
        print(render_next(step))
        return 2

    exit_code = run_child(
        stage=stage,
        script_name=script_name,
        command=child,
        process_dir=args.process_dir,
        output_root=args.output_root,
        journal=journal_path(args),
    )
    try:
        _state, step = inspect(args)
        print("")
        print(render_next(step))
    except Exception as exc:  # Navigation must not replace the child exit code.
        print(f"WARNING: post-run workflow inspection failed: {exc}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
