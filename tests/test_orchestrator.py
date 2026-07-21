from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_workflow_status  # noqa: E402
import chief_orchestrator  # noqa: E402
import allocation_generations  # noqa: E402
import integrity  # noqa: E402
import workflow_journal  # noqa: E402


class WorkflowFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.process = root / "process"
        self.output = root / "output"
        self.process.mkdir(parents=True)

    def write_stamped(self, filename: str, payload: dict, stamped_by: str) -> dict:
        integrity.stamp(payload, stamped_by)
        (self.process / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def extraction(self, *, pending: bool = False) -> dict:
        documents = [{
            "document_id": "DOC-001",
            "source_file": str(self.root / "private-client-invoice.pdf"),
            "needs_review": pending,
            "document_role": "invoice" if not pending else "unknown",
        }]
        return self.write_stamped(
            "invoice-extraction.json",
            {
                "schema_version": "invoice_extraction.v1",
                "documents": documents,
                "unresolved_input_files": [],
            },
            "test",
        )

    def allocation(self, extraction: dict, *, status: str = "confirmed", open_question: bool = False) -> dict:
        questions = [{
            "question_id": "Q-001",
            "status": "open",
            "unit_ids": ["UNIT-001"],
            "question": "private question",
        }] if open_question else []
        return self.write_stamped(
            "expense-allocation.json",
            {
                "schema_version": "expense_allocation.v1",
                "allocation_engine_revision": allocation_generations.ALLOCATION_ENGINE_REVISION,
                "source_policy_sha256": "p" * 64,
                "source_extraction_fingerprint": extraction["integrity"]["fingerprint"],
                "allocation_units": [{
                    "unit_id": "UNIT-001",
                    "unit_no": 1,
                    "unit_ref": "deadbeef",
                    "unit_identity_sha256": hashlib.sha256(b"unit-1").hexdigest(),
                    "source_sha256": "a" * 64,
                    "status": status,
                }],
                "project_contexts": [],
                "expense_hint_reconciliation": [],
                "change_log": [],
                "questions": questions,
            },
            "test",
        )

    def final_rows(
        self,
        allocation: dict,
        *,
        preview: bool = False,
        blocking: int = 0,
        template: str = "",
        layout: str = "",
    ) -> tuple[dict, Path]:
        workbook = self.root / "reimbursement.xlsx"
        workbook.write_bytes(b"test workbook bytes")
        payload = {
            "schema_version": "final_expense_rows.v1",
            "requester": "Terence Wang",
            "source_allocation_fingerprint": allocation["integrity"]["fingerprint"],
            "workbook_source": "template" if template else "generated",
            "template_workbook": template,
            "layout_file": layout,
            "workbook": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "blocking_policy_checks": blocking,
            "generated_with_allow_unconfirmed": preview,
            "open_allocation_questions": 1 if preview else 0,
            "expense_hint_reconciliation": [],
            "unresolved_expense_hint_count": 0,
            "rows": [],
        }
        return self.write_stamped("final-expense-rows.json", payload, "test"), workbook


class WorkflowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = WorkflowFixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def inspect(self) -> dict:
        return check_workflow_status.inspect_workflow(self.fixture.process, self.fixture.output)

    def test_empty_workflow_needs_source_inputs_without_command(self) -> None:
        state = self.inspect()
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("extraction", state["next"]["stage"])
        self.assertIsNone(state["next"]["argv"])

    def test_ready_extraction_needs_context_then_yields_allocate_command(self) -> None:
        self.fixture.extraction()
        state = self.inspect()
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("allocation", state["next"]["stage"])

        context = self.fixture.root / "project-context.json"
        context.write_text(json.dumps({
            "schema_version": "project_context.v1",
            "project_contexts": [{
                "date_start": "2026-06-01",
                "date_end": "2026-06-30",
                "city": "Shanghai",
                "client_name": "Test Client",
                "client_charge_code": "CORP-TEST",
            }],
        }), encoding="utf-8")
        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("allocate", state["next"]["operation"])
        self.assertEqual(str(context), state["next"]["parameters"]["context"])

    def test_current_composed_answers_yield_apply_command(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction, status="draft", open_question=True)
        (self.fixture.process / "allocation-answers.json").write_text(
            json.dumps({
                "schema_version": "allocation_answers.v1",
                "source_allocation_fingerprint": allocation["integrity"]["fingerprint"],
                "unit_updates": [{"unit_id": "UNIT-001", "status": "confirmed"}],
            }),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("apply", state["next"]["operation"])
        enriched = chief_orchestrator.enrich_next(state)
        self.assertIn("apply", enriched["argv"])

    def test_stale_answers_do_not_get_reapplied(self) -> None:
        extraction = self.fixture.extraction()
        self.fixture.allocation(extraction, status="draft", open_question=True)
        (self.fixture.process / "allocation-answers.json").write_text(
            json.dumps({
                "schema_version": "allocation_answers.v1",
                "source_allocation_fingerprint": "old-fingerprint",
                "unit_updates": [{"unit_id": "UNIT-001", "status": "confirmed"}],
            }),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("compose", state["next"]["operation"])

    def test_chief_routes_fresh_generation_through_rebase_then_composer(self) -> None:
        extraction = self.fixture.extraction()
        old = self.fixture.allocation(extraction, status="confirmed")
        old["change_log"] = [{"script": "apply_allocation_answers.py", "changes": []}]
        integrity.stamp(old, "test")
        allocation_path = self.fixture.process / "expense-allocation.json"
        allocation_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        archived = allocation_generations.archive_current_generation(allocation_path)

        current = self.fixture.allocation(extraction, status="draft", open_question=True)
        allocation_generations.record_previous_generation(current, archived)
        integrity.stamp(current, "test")
        allocation_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("rebase", state["next"]["operation"])
        enriched = chief_orchestrator.enrich_next(state)
        self.assertIn("rebase", enriched["argv"])
        self.assertIn(str(archived[0]), enriched["argv"])
        self.assertEqual(current["integrity"]["fingerprint"][:8], enriched["generation"]["allocation_code"])
        lineage = chief_orchestrator.lineage_report(state)
        self.assertEqual(old["integrity"]["fingerprint"], lineage["selected_rebase_source"]["fingerprint"])
        self.assertIn("depth 1", lineage["selected_rebase_source"]["selection_reason"])

        rebase_path = self.fixture.process / "rebase-decisions.json"
        rebase_payload = {
            "schema_version": "allocation_decisions.v1",
            "for_allocation_fingerprint": current["integrity"]["fingerprint"][:8],
            "decisions": [{"units": "1@deadbeef", "set": {"status": "confirmed"}}],
            "expense_hint_resolutions": [],
            "removed_evidence": [],
            "rebase_metadata": {
                "source_allocation_fingerprint": old["integrity"]["fingerprint"],
                "target_allocation_fingerprint": current["integrity"]["fingerprint"],
                "removed_evidence_count": 0,
                "removed_evidence_open_count": 0,
                "removed_evidence_pending_restore_count": 0,
            },
        }
        integrity.stamp(rebase_payload, "rebase_allocation_decisions.py")
        rebase_path.write_text(json.dumps(rebase_payload), encoding="utf-8")
        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("compose", state["next"]["operation"])
        enriched = chief_orchestrator.enrich_next(state)
        self.assertIn("compose", enriched["argv"])
        self.assertIn(str(rebase_path), enriched["argv"])

        rebase_payload["rebase_metadata"]["source_allocation_fingerprint"] = "0" * 64
        integrity.stamp(rebase_payload, "rebase_allocation_decisions.py")
        rebase_path.write_text(json.dumps(rebase_payload), encoding="utf-8")
        state = self.inspect()
        self.assertEqual("rebase", state["next"]["operation"])
        self.assertEqual("stale", state["artifacts"]["rebase_decisions"]["status"])
        self.assertIn("declared source fingerprint", state["next"]["summary"])

    def test_chief_stops_on_removed_confirmed_evidence_and_prints_follow_up(self) -> None:
        extraction = self.fixture.extraction()
        old = self.fixture.allocation(extraction, status="confirmed")
        old["allocation_units"][0].update({
            "source_filename": "旧酒店发票.pdf",
            "amount": "1600.00",
            "expense_date": "2026-07-02",
            "source_category": "hotel",
        })
        old["change_log"] = [{"script": "apply_allocation_answers.py", "changes": []}]
        integrity.stamp(old, "test")
        allocation_path = self.fixture.process / "expense-allocation.json"
        allocation_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        archived = allocation_generations.archive_current_generation(allocation_path)

        current = self.fixture.allocation(extraction, status="draft", open_question=True)
        current["allocation_units"][0].update({
            "unit_ref": "cafebabe",
            "unit_identity_sha256": hashlib.sha256(b"new-unit").hexdigest(),
            "source_sha256": "b" * 64,
        })
        allocation_generations.record_previous_generation(current, archived)
        integrity.stamp(current, "test")
        allocation_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

        rebase_result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "rebase_allocation_decisions.py"),
                "--old", str(archived[0]),
                "--new", str(allocation_path),
                "--output", str(self.fixture.process / "rebase-decisions.json"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, rebase_result.returncode, rebase_result.stderr)

        state = self.inspect()
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("rebase", state["next"]["operation"])
        self.assertIn("旧酒店发票.pdf", state["next"]["missing"][0])
        self.assertEqual(
            1,
            state["artifacts"]["rebase_decisions"]["removed_evidence_open_count"],
        )
        enriched = chief_orchestrator.enrich_next(state)
        self.assertIn("--resolutions", enriched["follow_up_argv"])
        rendered = chief_orchestrator.render_next(enriched)
        self.assertIn("After recording the requested user answers", rendered)

    def test_broken_generation_lineage_is_blocked(self) -> None:
        extraction = self.fixture.extraction()
        current = self.fixture.allocation(extraction, status="draft", open_question=True)
        current["previous_allocation_file"] = str(self.fixture.process / "allocation-generations" / "missing.json")
        current["previous_allocation_fingerprint"] = "f" * 64
        integrity.stamp(current, "test")
        (self.fixture.process / "expense-allocation.json").write_text(
            json.dumps(current, ensure_ascii=False), encoding="utf-8"
        )
        state = self.inspect()
        self.assertEqual("blocked", state["next"]["kind"])
        self.assertTrue(state["integrity_blocked"])
        self.assertIn("lineage", state["next"]["missing"][0])

    def test_non_unit_answer_actions_are_not_skipped(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction, status="draft", open_question=True)
        (self.fixture.process / "allocation-answers.json").write_text(
            json.dumps({
                "schema_version": "allocation_answers.v1",
                "source_allocation_fingerprint": allocation["integrity"]["fingerprint"],
                "unit_updates": [],
                "question_updates": [{"question_id": "Q-001", "status": "resolved"}],
                "expense_hint_resolutions": [{
                    "hint_id": "H-001",
                    "action": "pending_invoice",
                }],
                "confirm_units": ["UNIT-001"],
            }),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("apply", state["next"]["operation"])
        self.assertEqual(3, state["stages"]["allocation"]["unapplied_answer_count"])

    def test_ready_rows_yield_package_command(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        self.fixture.final_rows(allocation)
        state = self.inspect()
        self.assertEqual("command", state["next"]["kind"])
        self.assertEqual("package", state["next"]["operation"])

    def test_changed_project_context_makes_allocation_stale(self) -> None:
        extraction = self.fixture.extraction()
        context = self.fixture.root / "project-context.json"
        context_payload = {
            "schema_version": "project_context.v1",
            "project_contexts": [{
                "date_start": "2026-06-01",
                "date_end": "2026-06-30",
                "city": "Shanghai",
                "client_name": "Test Client",
                "client_charge_code": "CORP-TEST",
            }],
        }
        context.write_text(json.dumps(context_payload), encoding="utf-8")
        allocation = self.fixture.allocation(extraction)
        allocation["source_project_context_file"] = str(context)
        allocation["source_project_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
        integrity.stamp(allocation, "test")
        (self.fixture.process / "expense-allocation.json").write_text(
            json.dumps(allocation, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertFalse(self.inspect()["stages"]["allocation"]["context_mismatch"])

        context_payload["project_contexts"][0]["project_description"] = "Changed after allocation"
        context.write_text(json.dumps(context_payload), encoding="utf-8")
        state = self.inspect()
        self.assertTrue(state["stages"]["allocation"]["context_mismatch"])
        self.assertEqual("allocate", state["next"]["operation"])

    def test_missing_or_preview_workbook_yields_safe_stage3_regeneration(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        _rows, workbook = self.fixture.final_rows(allocation)
        workbook.unlink()
        state = self.inspect()
        self.assertEqual("write", state["next"]["operation"])
        self.assertEqual("command", state["next"]["kind"])

        self.fixture.final_rows(allocation, preview=True)
        state = self.inspect()
        self.assertEqual("write", state["next"]["operation"])
        self.assertEqual("command", state["next"]["kind"])

    def test_stage3_regeneration_preserves_template_or_layout(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        template = self.fixture.root / "custom-template.xlsx"
        template.write_bytes(b"template")
        _rows, workbook = self.fixture.final_rows(allocation, template=str(template))
        workbook.unlink()
        state = self.inspect()
        self.assertEqual(str(template), state["next"]["parameters"]["template"])
        argv = chief_orchestrator.enrich_next(state)["argv"]
        self.assertEqual(str(template), argv[argv.index("--template") + 1])

        layout = self.fixture.root / "custom-layout.toml"
        layout.write_text("", encoding="utf-8")
        _rows, workbook = self.fixture.final_rows(allocation, layout=str(layout))
        workbook.unlink()
        state = self.inspect()
        self.assertEqual(str(layout), state["next"]["parameters"]["layout"])
        argv = chief_orchestrator.enrich_next(state)["argv"]
        self.assertEqual(str(layout), argv[argv.index("--layout") + 1])

    def test_malformed_process_json_is_blocked(self) -> None:
        (self.fixture.process / "invoice-extraction.json").write_text("{broken", encoding="utf-8")
        state = self.inspect()
        self.assertEqual("blocked", state["next"]["kind"])
        self.assertTrue(state["integrity_blocked"])

        (self.fixture.process / "invoice-extraction.json").write_text("[]", encoding="utf-8")
        state = self.inspect()
        self.assertEqual("blocked", state["next"]["kind"])
        self.assertTrue(state["integrity_blocked"])

    def test_later_integrity_failure_overrides_earlier_needs_user(self) -> None:
        (self.fixture.process / "expense-allocation.json").write_text("{broken", encoding="utf-8")
        state = self.inspect()
        self.assertTrue(state["integrity_blocked"])
        self.assertEqual("blocked", state["next"]["kind"])
        self.assertEqual("allocation", state["next"]["stage"])

    def test_tampered_final_rows_with_policy_checks_remains_blocked(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        rows, _workbook = self.fixture.final_rows(allocation, blocking=1)
        rows["requester"] = "tampered"
        (self.fixture.process / "final-expense-rows.json").write_text(
            json.dumps(rows, ensure_ascii=False),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertEqual("blocked", state["stages"]["workbook"]["status"])
        self.assertEqual("blocked", state["next"]["kind"])

    def test_verified_manifest_is_complete(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        rows, workbook = self.fixture.final_rows(allocation)
        package = self.fixture.output / "package"
        (package / "发票").mkdir(parents=True)
        (package / "支持文档").mkdir()
        packaged_workbook = package / "reimbursement.xlsx"
        shutil.copy2(workbook, packaged_workbook)
        manifest = {
            "schema_version": "reimbursement_package_manifest.v1",
            "package_root": str(package),
            "workbook": packaged_workbook.name,
            "workbook_sha256": rows["workbook_sha256"],
            "final_rows_fingerprint": rows["integrity"]["fingerprint"],
            "issues": [],
            "invoice_count": 0,
            "support_count": 0,
            "expense_hint_reconciliation_count": 0,
            "invoice_files": [],
            "support_files": [],
        }
        integrity.stamp(manifest, "test")
        (package / "package-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertTrue(state["complete"])
        self.assertEqual("complete", state["next"]["kind"])

    def test_hidden_staging_package_is_not_treated_as_deliverable(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        rows, workbook = self.fixture.final_rows(allocation)
        staging = self.fixture.output / ".package.staging-test"
        staging.mkdir(parents=True)
        staged_workbook = staging / "reimbursement.xlsx"
        shutil.copy2(workbook, staged_workbook)
        manifest = {
            "schema_version": "reimbursement_package_manifest.v1",
            "package_root": str(staging),
            "workbook": staged_workbook.name,
            "workbook_sha256": rows["workbook_sha256"],
            "final_rows_fingerprint": rows["integrity"]["fingerprint"],
            "issues": [],
            "invoice_count": 0,
            "support_count": 0,
            "expense_hint_reconciliation_count": 0,
            "invoice_files": [],
            "support_files": [],
        }
        integrity.stamp(manifest, "test")
        (staging / "package-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        state = self.inspect()
        self.assertFalse(state["complete"])
        self.assertEqual("package", state["next"]["operation"])

    def test_tampered_package_manifest_sets_integrity_block(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction)
        rows, workbook = self.fixture.final_rows(allocation)
        package = self.fixture.output / "package"
        (package / "发票").mkdir(parents=True)
        (package / "支持文档").mkdir()
        packaged_workbook = package / "reimbursement.xlsx"
        shutil.copy2(workbook, packaged_workbook)
        manifest = {
            "schema_version": "reimbursement_package_manifest.v1",
            "package_root": str(package),
            "workbook": packaged_workbook.name,
            "workbook_sha256": rows["workbook_sha256"],
            "final_rows_fingerprint": rows["integrity"]["fingerprint"],
            "issues": [],
            "invoice_count": 0,
            "support_count": 0,
            "expense_hint_reconciliation_count": 0,
            "invoice_files": [],
            "support_files": [],
        }
        integrity.stamp(manifest, "test")
        manifest["workbook"] = "tampered.xlsx"
        (package / "package-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        state = self.inspect()
        self.assertTrue(state["integrity_blocked"])
        self.assertEqual("blocked", state["next"]["kind"])
        self.assertEqual("package", state["next"]["stage"])

    def test_chief_status_and_next_share_identical_next_state(self) -> None:
        extraction = self.fixture.extraction()
        allocation = self.fixture.allocation(extraction, status="confirmed")
        chief = SCRIPTS / "chief_orchestrator.py"
        common = [
            sys.executable,
            str(chief),
            "--process-dir", str(self.fixture.process),
            "--output-root", str(self.fixture.output),
        ]
        status = subprocess.run(
            [*common, "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        next_result = subprocess.run(
            [*common, "next", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        lineage_result = subprocess.run(
            [*common, "lineage", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        self.assertEqual(json.loads(status.stdout)["next"], json.loads(next_result.stdout))
        lineage = json.loads(lineage_result.stdout)
        self.assertEqual(
            allocation["integrity"]["fingerprint"],
            lineage["allocation"]["fingerprint"],
        )

    def test_blocked_query_commands_report_state_with_success_exit_code(self) -> None:
        (self.fixture.process / "invoice-extraction.json").write_text("{broken", encoding="utf-8")
        chief = SCRIPTS / "chief_orchestrator.py"
        common = [
            sys.executable,
            str(chief),
            "--process-dir", str(self.fixture.process),
            "--output-root", str(self.fixture.output),
        ]

        for command in ("status", "next", "lineage"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [*common, command, "--json"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout)
                if command == "status":
                    self.assertEqual("blocked", payload["next"]["kind"])
                elif command == "next":
                    self.assertEqual("blocked", payload["kind"])


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.process = self.root / "process"
        self.output = self.root / "output"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_child_exit_code_is_preserved_and_arguments_are_not_logged(self) -> None:
        child = self.root / "child.py"
        child.write_text("import sys\nraise SystemExit(7)\n", encoding="utf-8")
        journal = self.process / "workflow-journal.jsonl"
        rc = chief_orchestrator.run_child(
            stage="test",
            script_name="child.py",
            command=[sys.executable, str(child), "PRIVATE-CLIENT-秘密"],
            process_dir=self.process,
            output_root=self.output,
            journal=journal,
        )
        self.assertEqual(7, rc)
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["started", "failed"], [entry["event"] for entry in entries])
        self.assertEqual(entries[0]["run_id"], entries[1]["run_id"])
        self.assertEqual(7, entries[1]["exit_code"])
        self.assertNotIn("PRIVATE-CLIENT", journal.read_text(encoding="utf-8"))
        self.assertNotIn("秘密", journal.read_text(encoding="utf-8"))

    def test_exit_code_normalization_preserves_codes_and_maps_signals(self) -> None:
        self.assertEqual(7, chief_orchestrator.normalize_child_exit_code(7))
        self.assertEqual(143, chief_orchestrator.normalize_child_exit_code(-15))

    def test_snapshot_contains_counts_and_hashes_but_not_source_names(self) -> None:
        fixture = WorkflowFixture(self.root)
        fixture.extraction()
        snapshot = workflow_journal.snapshot_artifacts(fixture.process, fixture.output)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(1, snapshot["extraction"]["document_count"])
        self.assertIn("file_sha256", snapshot["extraction"])
        self.assertNotIn("private-client-invoice", encoded)

    def test_journal_failure_does_not_replace_child_exit_code(self) -> None:
        child = self.root / "child.py"
        child.write_text("raise SystemExit(6)\n", encoding="utf-8")
        impossible_parent = self.root / "not-a-directory"
        impossible_parent.write_text("file", encoding="utf-8")
        rc = chief_orchestrator.run_child(
            stage="test",
            script_name="child.py",
            command=[sys.executable, str(child)],
            process_dir=self.process,
            output_root=self.output,
            journal=impossible_parent / "workflow-journal.jsonl",
        )
        self.assertEqual(6, rc)

    def test_pre_dispatch_rejection_is_journaled_as_blocked(self) -> None:
        chief = SCRIPTS / "chief_orchestrator.py"
        result = subprocess.run([
            sys.executable,
            str(chief),
            "--process-dir", str(self.process),
            "--output-root", str(self.output),
            "run", "compose",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, result.returncode)
        journal = self.process / "workflow-journal.jsonl"
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, len(entries))
        self.assertEqual("blocked", entries[0]["event"])
        self.assertEqual("compose_answers.py", entries[0]["script"])
        self.assertEqual(2, entries[0]["exit_code"])

    def test_extract_rejects_input_directory_containing_workflow_outputs(self) -> None:
        parser = chief_orchestrator.build_parser()
        args = parser.parse_args([
            "--process-dir", str(self.root / "process"),
            "--output-root", str(self.root / "output"),
            "run", "extract", str(self.root),
        ])
        with self.assertRaises(chief_orchestrator.OrchestratorError):
            chief_orchestrator.build_child_command(args)

    def test_import_wrapper_is_rejected_with_direct_command(self) -> None:
        wrapper = self.root / "run_chief.py"
        wrapper.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import chief_orchestrator\n"
            "chief_orchestrator.main()\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(wrapper), "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("run_chief.py", result.stderr)
        self.assertIn(str(SCRIPTS / "chief_orchestrator.py"), result.stderr)


if __name__ == "__main__":
    unittest.main()
