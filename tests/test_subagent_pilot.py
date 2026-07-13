from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_workflow_status  # noqa: E402
import chief_orchestrator  # noqa: E402
import integrity  # noqa: E402
import subagent_protocol  # noqa: E402


PYTHON = sys.executable


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class PilotFixture:
    def __init__(self, root: Path, *, unit_status: str = "draft", open_question: bool = True) -> None:
        self.root = root
        self.process = root / "process"
        self.output = root / "output"
        self.process.mkdir(parents=True)
        self.extraction_path = self.process / "invoice-extraction.json"
        self.allocation_path = self.process / "expense-allocation.json"

        extraction = {
            "schema_version": "invoice_extraction.v1",
            "documents": [{
                "document_id": "DOC-001",
                "source_file": str(root / "private" / "客户甲-餐费.pdf"),
                "source_sha256": "a" * 64,
                "document_role": "invoice",
                "document_subtype": "vat_e_invoice",
                "needs_review": False,
                "invoice": {
                    "invoice_number": "12345678",
                    "seller_name": "测试餐厅",
                    "issue_date": "2026-06-02",
                    "total_amount": "88.00",
                },
            }],
            "unresolved_input_files": [],
        }
        integrity.stamp(extraction, "test")
        write_json(self.extraction_path, extraction)

        questions = [{
            "question_id": "Q-001",
            "status": "open",
            "unit_ids": ["UNIT-001"],
            "question": "请确认餐费日期和项目",
        }] if open_question else []
        project_contexts = [{
            "context_id": "CTX-001",
            "date_start": "2026-06-01",
            "date_end": "2026-06-03",
            "city": "郑州",
            "client_name": "客户甲",
            "client_charge_code": "CORP-2026-BD",
            "project_description": "现场项目",
        }]
        context_path = root / "project-context.json"
        write_json(context_path, {
            "schema_version": "project_context.v1",
            "project_contexts": project_contexts,
        })
        policy_sha = hashlib.sha256(subagent_protocol.POLICY_PATH.read_bytes()).hexdigest()
        allocation = {
            "schema_version": "expense_allocation.v1",
            "allocation_engine_revision": "expense-allocation-engine.v2",
            "source_policy_sha256": policy_sha,
            "source_extraction_fingerprint": extraction["integrity"]["fingerprint"],
            "source_project_context_file": str(context_path),
            "source_project_context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
            "project_contexts": project_contexts,
            "allocation_units": [{
                "unit_id": "UNIT-001",
                "user_no": 1,
                "unit_ref": "deadbeef",
                "unit_identity_sha256": hashlib.sha256(b"unit-1").hexdigest(),
                "source_sha256": "a" * 64,
                "source_document_id": "DOC-001",
                "source_file": str(root / "private" / "客户甲-餐费.pdf"),
                "status": unit_status,
                "source_category": "meal",
                "final_template_column": "travel",
                "amount": "88.00",
                "invoice_amount": "88.00",
                "expense_date": "2026-06-01",
                "formal_city": "郑州",
                "client_name": "客户甲",
                "client_charge_code": "CORP-2026-BD",
                "project_context_id": "CTX-001",
                "final_note": "出差餐费",
            }],
            "expense_hint_reconciliation": [],
            "questions": questions,
            "change_log": [],
        }
        integrity.stamp(allocation, "test")
        write_json(self.allocation_path, allocation)

    def allocation(self) -> dict:
        return json.loads(self.allocation_path.read_text(encoding="utf-8"))

    def task(self, role: str) -> tuple[dict, dict[str, Path]]:
        return subagent_protocol.prepare_task(
            role,
            self.allocation_path,
            self.extraction_path,
            self.process,
        )

    @staticmethod
    def completed_template(task: dict) -> dict:
        candidate = subagent_protocol.result_template(task)
        candidate["coverage"] = [
            {"check_id": item["check_id"], "status": "completed", "notes": "checked"}
            for item in candidate["coverage"]
        ]
        candidate["summary"] = "Completed an independent pass over every required check."
        return candidate


class SubagentProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_packet_is_path_free_stable_and_does_not_mutate_process_json(self) -> None:
        fixture = PilotFixture(self.root)
        before = fixture.allocation_path.read_bytes()
        task, paths = fixture.task("allocation_analyst")
        first_fp = task["integrity"]["fingerprint"]
        task_again, _ = fixture.task("allocation_analyst")
        encoded = json.dumps(task, ensure_ascii=False)

        self.assertEqual(first_fp, task_again["integrity"]["fingerprint"])
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("source_project_context_file", encoded)
        self.assertNotIn("raw_text", encoded)
        self.assertEqual("客户甲-餐费.pdf", task["allocation_units"][0]["source_file"])
        self.assertTrue(paths["task"].is_file())
        self.assertTrue(paths["template"].is_file())
        self.assertEqual(before, fixture.allocation_path.read_bytes())

        context_path = self.root / "project-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["project_contexts"][0]["city"] = "太原"
        write_json(context_path, context)
        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "project context changed"):
            fixture.task("allocation_analyst")

    def test_task_packet_scrubs_absolute_paths_embedded_in_free_text(self) -> None:
        fixture = PilotFixture(self.root)
        allocation = fixture.allocation()
        allocation["allocation_units"][0]["issues"] = [{
            "field": "support",
            "problem": (
                r"Missing D:\private-client\approval.png; mirror \\secret-server\finance\invoice.pdf; "
                "converted from /home/private-client/invoice.pdf"
            ),
        }]
        integrity.stamp(allocation, "test")
        write_json(fixture.allocation_path, allocation)

        task, _paths = fixture.task("allocation_analyst")
        encoded = json.dumps(task, ensure_ascii=False)
        self.assertNotIn("private-client", encoded)
        self.assertNotIn("secret-server", encoded)
        self.assertNotIn("/home/", encoded)
        self.assertIn("[local-path]", encoded)

    def test_otako_result_requires_current_refs_and_generates_noncanonical_proposal_only(self) -> None:
        fixture = PilotFixture(self.root)
        task, _paths = fixture.task("allocation_analyst")
        candidate = fixture.completed_template(task)
        candidate["proposals"] = [{
            "proposal_id": "P-001",
            "unit_refs": ["1@deadbeef"],
            "set": {"status": "confirmed", "project_context_id": "CTX-001"},
            "confidence": "high",
            "reason": "City, date, amount, and project itinerary agree.",
            "evidence_refs": ["DOC-001"],
        }]
        result_path = self.root / "otako-result.json"
        write_json(result_path, candidate)
        before = fixture.allocation_path.read_bytes()

        untrusted_process_path = fixture.process / "raw-agent-result.json"
        write_json(untrusted_process_path, candidate)
        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "outside process"):
            subagent_protocol.accept_result(
                "allocation_analyst",
                fixture.allocation_path,
                fixture.extraction_path,
                fixture.process,
                untrusted_process_path,
            )

        accepted, paths = subagent_protocol.accept_result(
            "allocation_analyst",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )
        unreviewed = json.loads(paths["proposal"].read_text(encoding="utf-8"))
        self.assertEqual("allocation_analysis.v1", accepted["schema_version"])
        self.assertEqual("allocation_proposals.v1", unreviewed["schema_version"])
        self.assertEqual("unreviewed", unreviewed["review_status"])
        promoted, promoted_path = subagent_protocol.promote_proposals(
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            ["P-001"],
            select_all=False,
            reviewed_by="coordinator",
            review_note="Evidence reviewed before applicant confirmation.",
        )
        self.assertEqual(fixture.allocation()["integrity"]["fingerprint"], promoted["for_allocation_fingerprint"])
        self.assertEqual("1@deadbeef", promoted["decisions"][0]["units"])
        self.assertTrue(promoted_path.is_file())
        self.assertEqual(before, fixture.allocation_path.read_bytes())

        candidate["proposals"][0]["unit_refs"] = ["1@bad0cafe"]
        write_json(result_path, candidate)
        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "stale or unknown refs"):
            subagent_protocol.accept_result(
                "allocation_analyst",
                fixture.allocation_path,
                fixture.extraction_path,
                fixture.process,
                result_path,
            )

    def test_otako_rejects_nested_values_before_creating_proposal_artifacts(self) -> None:
        fixture = PilotFixture(self.root)
        task, paths = fixture.task("allocation_analyst")
        candidate = fixture.completed_template(task)
        candidate["proposals"] = [{
            "proposal_id": "P-NESTED",
            "unit_refs": ["1@deadbeef"],
            "set": {"client_name": {"nested": "not-a-string"}},
            "confidence": "high",
            "reason": "Deliberately malformed value for the regression test.",
            "evidence_refs": ["DOC-001"],
        }]
        result_path = self.root / "otako-nested.json"
        write_json(result_path, candidate)

        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "invalid updater values"):
            subagent_protocol.accept_result(
                "allocation_analyst",
                fixture.allocation_path,
                fixture.extraction_path,
                fixture.process,
                result_path,
            )
        self.assertFalse(paths["analyst_result"].exists())
        self.assertFalse(paths["proposal"].exists())

    def test_reviewer_block_is_current_and_suppresses_stage3(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        task, _paths = fixture.task("independent_reviewer")
        candidate = fixture.completed_template(task)
        candidate["outcome"] = "block"
        candidate["findings"] = [{
            "finding_id": "F-001",
            "severity": "blocking",
            "code": "MEAL_FORM_CITY_CONFLICT",
            "message": "The formal city evidence conflicts with the selected amount column.",
            "unit_refs": ["1@deadbeef"],
            "evidence_refs": ["DOC-001"],
            "recommended_action": "Correct the formal city or column through Composer/Updater.",
        }]
        result_path = self.root / "kaede-result.json"
        write_json(result_path, candidate)
        subagent_protocol.accept_result(
            "independent_reviewer",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )

        review = subagent_protocol.review_state(fixture.process, fixture.allocation())
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertTrue(review["current"])
        self.assertEqual("block", review["outcome"])
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("workbook", state["next"]["stage"])
        self.assertTrue(state["subagents"]["independent_reviewer"]["current"])

        workbook = self.root / "blocked.xlsx"
        write_result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "write_reimbursement_template.py"),
            "--allocation", str(fixture.allocation_path),
            "--output", str(workbook),
            "--requester", "Test Requester",
            "--process-dir", str(fixture.process),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, write_result.returncode)
        self.assertIn("Independent review blocker", write_result.stderr)
        self.assertFalse(workbook.exists())

        workbook.write_bytes(b"stale workbook")
        final_rows = {
            "schema_version": "final_expense_rows.v1",
            "source_allocation_fingerprint": fixture.allocation()["integrity"]["fingerprint"],
            "generated_with_allow_unconfirmed": False,
            "open_allocation_questions": 0,
            "expense_hint_reconciliation": [],
            "unresolved_expense_hint_count": 0,
            "workbook": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "blocking_policy_checks": 0,
            "rows": [],
            "proof_groups": [],
        }
        integrity.stamp(final_rows, "test")
        final_rows_path = fixture.process / "final-expense-rows.json"
        write_json(final_rows_path, final_rows)
        package_result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "package_reimbursement_files.py"),
            "--final-rows", str(final_rows_path),
            "--extraction", str(fixture.extraction_path),
            "--workbook", str(workbook),
            "--output-root", str(fixture.output),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, package_result.returncode)
        self.assertIn("current Kaede independent review", package_result.stderr)

    def test_reviewer_block_survives_deleted_canonical_sidecar(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        task, _paths = fixture.task("independent_reviewer")
        candidate = fixture.completed_template(task)
        candidate["outcome"] = "block"
        candidate["findings"] = [{
            "finding_id": "F-DURABLE",
            "severity": "blocking",
            "code": "DURABLE_BLOCK",
            "message": "This accepted blocker must survive sidecar deletion.",
            "unit_refs": ["1@deadbeef"],
            "evidence_refs": ["DOC-001"],
            "recommended_action": "Resolve through Composer and Updater.",
        }]
        result_path = self.root / "kaede-durable.json"
        write_json(result_path, candidate)
        _accepted, accepted_paths = subagent_protocol.accept_result(
            "independent_reviewer",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )
        self.assertTrue(accepted_paths["review_archive"].is_file())
        accepted_paths["review_result"].unlink()

        review = subagent_protocol.review_state(
            fixture.process,
            fixture.allocation(),
            fixture.allocation_path,
            fixture.extraction_path,
        )
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertTrue(review["current"])
        self.assertTrue(review["recovered_from_archive"])
        self.assertEqual("block", review["outcome"])
        self.assertEqual("workbook", state["next"]["stage"])

        workbook = self.root / "archive-blocked.xlsx"
        write_result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "write_reimbursement_template.py"),
            "--allocation", str(fixture.allocation_path),
            "--output", str(workbook),
            "--requester", "Test Requester",
            "--process-dir", str(fixture.process),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, write_result.returncode)
        self.assertIn("Independent review blocker", write_result.stderr)

        workbook.write_bytes(b"stale workbook")
        final_rows = {
            "schema_version": "final_expense_rows.v1",
            "source_allocation_fingerprint": fixture.allocation()["integrity"]["fingerprint"],
            "generated_with_allow_unconfirmed": False,
            "open_allocation_questions": 0,
            "expense_hint_reconciliation": [],
            "unresolved_expense_hint_count": 0,
            "workbook": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "blocking_policy_checks": 0,
            "rows": [],
            "proof_groups": [],
        }
        integrity.stamp(final_rows, "test")
        final_rows_path = fixture.process / "final-expense-rows.json"
        write_json(final_rows_path, final_rows)
        package_result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "package_reimbursement_files.py"),
            "--final-rows", str(final_rows_path),
            "--extraction", str(fixture.extraction_path),
            "--workbook", str(workbook),
            "--output-root", str(fixture.output),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, package_result.returncode)
        self.assertIn("current Kaede independent review", package_result.stderr)

    def test_review_is_stale_when_role_instruction_generation_changes(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        task, _paths = fixture.task("independent_reviewer")
        candidate = fixture.completed_template(task)
        result_path = self.root / "kaede-current.json"
        write_json(result_path, candidate)
        subagent_protocol.accept_result(
            "independent_reviewer",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )

        changed_reference = self.root / "changed-review-role.md"
        changed_reference.write_text(
            Path(subagent_protocol.ROLE_SPECS["independent_reviewer"]["reference"]).read_text(
                encoding="utf-8-sig"
            ) + "\nChanged review rule generation.\n",
            encoding="utf-8",
        )
        changed_spec = dict(subagent_protocol.ROLE_SPECS["independent_reviewer"])
        changed_spec["reference"] = changed_reference
        with mock.patch.dict(
            subagent_protocol.ROLE_SPECS,
            {"independent_reviewer": changed_spec},
        ):
            review = subagent_protocol.review_state(
                fixture.process,
                fixture.allocation(),
                fixture.allocation_path,
                fixture.extraction_path,
            )
        self.assertFalse(review["current"])
        self.assertEqual("stale", review["status"])

    def test_premature_review_cannot_override_stage2_next_action(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        task, _paths = fixture.task("independent_reviewer")
        candidate = fixture.completed_template(task)
        candidate["outcome"] = "block"
        candidate["findings"] = [{
            "finding_id": "F-OLD",
            "severity": "blocking",
            "code": "OLD_BLOCK",
            "message": "A prior-generation blocker.",
            "unit_refs": ["1@deadbeef"],
            "evidence_refs": ["DOC-001"],
            "recommended_action": "Resolve through the normal Stage 2 path.",
        }]
        result_path = self.root / "kaede-old-block.json"
        write_json(result_path, candidate)
        subagent_protocol.accept_result(
            "independent_reviewer",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )

        allocation = fixture.allocation()
        allocation["allocation_units"][0]["status"] = "draft"
        allocation["questions"] = [{
            "question_id": "Q-NEW",
            "status": "open",
            "unit_ids": ["UNIT-001"],
            "question": "Confirm the current project.",
        }]
        integrity.stamp(allocation, "test")
        write_json(fixture.allocation_path, allocation)

        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "review is premature"):
            fixture.task("independent_reviewer")
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertEqual("allocation", state["next"]["stage"])
        self.assertEqual("compose", state["next"]["operation"])

    def test_reviewer_rejects_current_unapplied_composer_actions(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        answers = {
            "schema_version": "allocation_answers.v1",
            "source_allocation_fingerprint": fixture.allocation()["integrity"]["fingerprint"],
            "unit_updates": [{
                "unit_id": "UNIT-001",
                "set": {"final_note": "Pending official update"},
            }],
            "expense_hint_resolutions": [],
            "question_updates": [],
            "project_contexts": [],
            "confirm_units": [],
            "drop_units": [],
            "exclude_units": [],
        }
        write_json(fixture.process / "allocation-answers.json", answers)

        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "remain unapplied"):
            fixture.task("independent_reviewer")
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertEqual("apply", state["next"]["operation"])

    def test_writer_rejects_process_directory_unrelated_to_allocation(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        other_process = self.root / "other-process"
        other_process.mkdir()
        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "cross-batch"):
            subagent_protocol.prepare_task(
                "allocation_analyst",
                fixture.allocation_path,
                fixture.extraction_path,
                other_process,
            )
        workbook = self.root / "wrong-process.xlsx"
        result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "write_reimbursement_template.py"),
            "--allocation", str(fixture.allocation_path),
            "--output", str(workbook),
            "--requester", "Test Requester",
            "--process-dir", str(other_process),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, result.returncode)
        self.assertIn("canonical directory containing --allocation", result.stderr)
        self.assertFalse(workbook.exists())

    def test_missing_review_is_explicit_fail_open_and_pass_review_keeps_write_route(self) -> None:
        fixture = PilotFixture(self.root, unit_status="confirmed", open_question=False)
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertEqual("needs_user", state["next"]["kind"])
        self.assertEqual("write", state["next"]["operation"])
        self.assertEqual("missing", state["subagents"]["independent_reviewer"]["status"])

        task, _paths = fixture.task("independent_reviewer")
        candidate = fixture.completed_template(task)
        result_path = self.root / "kaede-pass.json"
        write_json(result_path, candidate)
        subagent_protocol.accept_result(
            "independent_reviewer",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        self.assertEqual("write", state["next"]["operation"])
        self.assertEqual("pass", state["subagents"]["independent_reviewer"]["outcome"])

    def test_optional_delegation_never_replaces_chief_next_action(self) -> None:
        fixture = PilotFixture(self.root)
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        enriched = chief_orchestrator.enrich_next(state)
        self.assertEqual("needs_user", enriched["kind"])
        self.assertEqual("compose", enriched["operation"])
        self.assertEqual("allocation_analyst", enriched["delegations"][0]["role"])

        fixture = PilotFixture(self.root / "ready", unit_status="confirmed", open_question=False)
        state = check_workflow_status.inspect_workflow(fixture.process, fixture.output)
        enriched = chief_orchestrator.enrich_next(state)
        self.assertEqual("write", enriched["operation"])
        self.assertEqual("independent_reviewer", enriched["delegations"][0]["role"])

    def test_chief_dispatches_named_roles_through_protocol_only(self) -> None:
        fixture = PilotFixture(self.root)
        parser = chief_orchestrator.build_parser()
        args = parser.parse_args([
            "--process-dir", str(fixture.process),
            "--output-root", str(fixture.output),
            "run", "prepare-agent", "--role", "allocation_analyst",
        ])
        stage, script_name, command = chief_orchestrator.build_child_command(args)
        self.assertEqual("subagent-allocation_analyst", stage)
        self.assertIn("Otako - Allocation Analyst", script_name)
        self.assertIn("subagent_protocol.py", command[3])
        self.assertEqual("prepare", command[4])

        args = parser.parse_args([
            "--process-dir", str(fixture.process),
            "--output-root", str(fixture.output),
            "run", "promote-proposals", "--select", "P-001,P-003",
            "--reviewed-by", "coordinator",
        ])
        stage, script_name, command = chief_orchestrator.build_child_command(args)
        self.assertEqual("subagent-allocation_analyst", stage)
        self.assertIn("proposal promotion", script_name)
        self.assertEqual("promote", command[4])
        self.assertIn("P-001,P-003", command)

    def test_proposal_mode_requires_exact_full_generation_fingerprint(self) -> None:
        fixture = PilotFixture(self.root)
        allocation_fp = fixture.allocation()["integrity"]["fingerprint"]
        task, _paths = fixture.task("allocation_analyst")
        candidate = fixture.completed_template(task)
        candidate["proposals"] = [{
            "proposal_id": "P-001",
            "unit_refs": ["1@deadbeef"],
            "set": {"status": "confirmed"},
            "confidence": "high",
            "reason": "Current evidence supports confirmation.",
            "evidence_refs": ["DOC-001"],
        }]
        result_path = self.root / "otako-for-promotion.json"
        write_json(result_path, candidate)
        _accepted, paths = subagent_protocol.accept_result(
            "allocation_analyst",
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            result_path,
        )
        proposal, proposal_path = subagent_protocol.promote_proposals(
            fixture.allocation_path,
            fixture.extraction_path,
            fixture.process,
            ["P-001"],
            select_all=False,
            reviewed_by="coordinator",
            review_note="Reviewed against current evidence.",
        )
        proposal_path = self.root / "proposal.json"
        answers_path = self.process_answers_path(fixture)
        raw_result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "compose_answers.py"),
            "--allocation", str(fixture.allocation_path),
            "--proposal", str(paths["proposal"]),
            "--output", str(answers_path),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, raw_result.returncode)
        self.assertFalse(answers_path.exists())

        forged = json.loads(json.dumps(proposal, ensure_ascii=False))
        forged["decisions"][0]["set"]["client_name"] = "伪造客户"
        forged["proposal_review"]["selected_decisions_sha256"] = subagent_protocol.canonical_sha(
            forged["decisions"]
        )
        integrity.stamp(forged, "subagent_protocol.py")
        with self.assertRaisesRegex(subagent_protocol.ProtocolError, "do not exactly match"):
            subagent_protocol.validate_promoted_proposal(forged, fixture.allocation_path)

        short_proposal = dict(proposal)
        short_proposal["for_allocation_fingerprint"] = allocation_fp[:8]
        integrity.stamp(short_proposal, "subagent_protocol.py")
        write_json(proposal_path, short_proposal)
        result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "compose_answers.py"),
            "--allocation", str(fixture.allocation_path),
            "--proposal", str(proposal_path),
            "--output", str(answers_path),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, result.returncode)
        self.assertFalse(answers_path.exists())

        write_json(proposal_path, proposal)
        result = subprocess.run([
            PYTHON,
            str(SCRIPTS / "compose_answers.py"),
            "--allocation", str(fixture.allocation_path),
            "--proposal", str(proposal_path),
            "--output", str(answers_path),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(answers_path.is_file())

    @staticmethod
    def process_answers_path(fixture: PilotFixture) -> Path:
        return fixture.process / "allocation-answers.json"


if __name__ == "__main__":
    unittest.main()
