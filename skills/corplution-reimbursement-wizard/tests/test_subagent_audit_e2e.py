"""End-to-end guardrails for the unified subagent audit (Otako Mirror Warden /
Kaede Gate Challenger).

These re-cover behaviors that used to live in the retired proposal-era pilot
suite but are NOT exercised by test_subagent_audit.py (which locks the schema,
384 KiB cap, and protocol entry): a current blocking audit suppresses Stage 3
(writer) and Stage 4 (packaging), an accepted block survives deletion of its
canonical sidecar via the immutable archive, an audit goes stale when the role
instruction generation changes, and the read-only task packet scrubs absolute
paths out of free text.
"""
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
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_workflow_status  # noqa: E402
import integrity  # noqa: E402
import subagent_protocol  # noqa: E402

PYTHON = sys.executable


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class AuditFixture:
    """A canonical, stamped process dir that builds a valid audit task."""

    def __init__(self, root: Path, *, unit_status: str = "confirmed", open_question: bool = False) -> None:
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
            role, self.allocation_path, self.extraction_path, self.process
        )

    def completed_result(self, task: dict, *, outcome: str = "pass", findings: list | None = None) -> dict:
        candidate = subagent_protocol.result_template(task)
        candidate["coverage"] = [
            {"check_id": item["check_id"], "status": "completed", "notes": "checked"}
            for item in candidate["coverage"]
        ]
        candidate["summary"] = "Independent audit over every required check."
        candidate["outcome"] = outcome
        candidate["findings"] = findings or []
        return candidate

    def accept_block(self, role: str, code: str, message: str) -> tuple[dict, dict[str, Path]]:
        task, _paths = self.task(role)
        finding = {
            "finding_id": "F-001",
            "severity": "blocking",
            "code": code,
            "message": message,
            "unit_refs": ["1@deadbeef"],
            "evidence_refs": ["DOC-001"],
            "recommended_action": "Resolve through Composer and Updater.",
        }
        candidate = self.completed_result(task, outcome="block", findings=[finding])
        # Raw subagent results must live OUTSIDE process/.
        result_path = self.root / f"{role}-result.json"
        write_json(result_path, candidate)
        return subagent_protocol.accept_result(
            role, self.allocation_path, self.extraction_path, self.process, result_path
        )

    def run_writer(self, workbook: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                PYTHON, str(SCRIPTS / "write_reimbursement_template.py"),
                "--allocation", str(self.allocation_path),
                "--output", str(workbook),
                "--requester", "Test Requester",
                "--process-dir", str(self.process),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def run_packager(self, workbook: Path) -> subprocess.CompletedProcess:
        workbook.write_bytes(b"stale workbook")
        final_rows = {
            "schema_version": "final_expense_rows.v1",
            "source_allocation_fingerprint": self.allocation()["integrity"]["fingerprint"],
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
        final_rows_path = self.process / "final-expense-rows.json"
        write_json(final_rows_path, final_rows)
        return subprocess.run(
            [
                PYTHON, str(SCRIPTS / "package_reimbursement_files.py"),
                "--final-rows", str(final_rows_path),
                "--extraction", str(self.extraction_path),
                "--workbook", str(workbook),
                "--output-root", str(self.output),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )


class AuditBlockSuppressesGeneration(unittest.TestCase):
    def test_current_block_suppresses_stage3_and_stage4(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = AuditFixture(Path(tmp))
            fx.accept_block("mirror_warden", "MEAL_FORM_CITY_CONFLICT",
                            "Formal city evidence conflicts with the selected amount column.")

            state = subagent_protocol.audit_state(
                "mirror_warden", fx.process, fx.allocation(),
                fx.allocation_path, fx.extraction_path,
            )
            self.assertTrue(state["current"])
            self.assertEqual("block", state["outcome"])
            self.assertGreaterEqual(state["blocking_count"], 1)

            # Status engine routes the user to resolve the block before Stage 3.
            wf = check_workflow_status.inspect_workflow(fx.process, fx.output)
            self.assertEqual("needs_user", wf["next"]["kind"])
            self.assertEqual("workbook", wf["next"]["stage"])
            self.assertTrue(wf["subagents"]["mirror_warden"]["current"])

            # Stage 3: the writer refuses to generate a workbook.
            workbook = Path(tmp) / "blocked.xlsx"
            writer = fx.run_writer(workbook)
            self.assertEqual(2, writer.returncode, writer.stderr)
            self.assertIn("audit blocker", writer.stderr)
            self.assertFalse(workbook.exists())

            # Stage 4: packaging refuses even a hand-placed workbook.
            packager = fx.run_packager(workbook)
            self.assertEqual(2, packager.returncode, packager.stderr)
            self.assertIn("blocking findings", packager.stderr)


class AuditBlockSurvivesArchive(unittest.TestCase):
    def test_block_survives_deleted_canonical_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = AuditFixture(Path(tmp))
            _accepted, paths = fx.accept_block(
                "mirror_warden", "DURABLE_BLOCK",
                "This accepted blocker must survive sidecar deletion.",
            )
            # The immutable archive copy exists; delete the mutable canonical sidecar.
            self.assertTrue(paths["audit_archive"].is_file())
            paths["audit_result"].unlink()

            state = subagent_protocol.audit_state(
                "mirror_warden", fx.process, fx.allocation(),
                fx.allocation_path, fx.extraction_path,
            )
            self.assertTrue(state["current"])
            self.assertTrue(state["recovered_from_archive"])
            self.assertEqual("block", state["outcome"])

            workbook = Path(tmp) / "archive-blocked.xlsx"
            writer = fx.run_writer(workbook)
            self.assertEqual(2, writer.returncode, writer.stderr)
            self.assertIn("audit blocker", writer.stderr)
            self.assertFalse(workbook.exists())


class AuditStaleOnRuleChange(unittest.TestCase):
    def test_audit_is_stale_when_role_instruction_generation_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = AuditFixture(Path(tmp))
            task, _paths = fx.task("mirror_warden")
            candidate = fx.completed_result(task)
            result_path = Path(tmp) / "mirror-current.json"
            write_json(result_path, candidate)
            subagent_protocol.accept_result(
                "mirror_warden", fx.allocation_path, fx.extraction_path, fx.process, result_path
            )

            # Baseline: the accepted audit is current.
            fresh = subagent_protocol.audit_state(
                "mirror_warden", fx.process, fx.allocation(),
                fx.allocation_path, fx.extraction_path,
            )
            self.assertTrue(fresh["current"])

            # Roll the role instruction generation: the task fingerprint changes,
            # so the previously accepted result no longer binds to the current task.
            changed_reference = Path(tmp) / "changed-mirror-warden.md"
            changed_reference.write_text(
                Path(subagent_protocol.ROLE_SPECS["mirror_warden"]["reference"]).read_text(
                    encoding="utf-8-sig"
                ) + "\nChanged audit rule generation.\n",
                encoding="utf-8",
            )
            changed_spec = dict(subagent_protocol.ROLE_SPECS["mirror_warden"])
            changed_spec["reference"] = changed_reference
            with mock.patch.dict(subagent_protocol.ROLE_SPECS, {"mirror_warden": changed_spec}):
                stale = subagent_protocol.audit_state(
                    "mirror_warden", fx.process, fx.allocation(),
                    fx.allocation_path, fx.extraction_path,
                )
            self.assertFalse(stale["current"])
            self.assertEqual("stale", stale["status"])


class TaskPacketScrubsPaths(unittest.TestCase):
    def test_task_packet_scrubs_absolute_paths_embedded_in_free_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A ready allocation (the audit runs pre-Stage-3 on a confirmed claim);
            # the embedded paths ride in the carried `issues` free text.
            fx = AuditFixture(Path(tmp))
            allocation = fx.allocation()
            allocation["allocation_units"][0]["issues"] = [{
                "field": "support",
                "problem": (
                    r"Missing D:\private-client\approval.png; mirror \\secret-server\finance\invoice.pdf; "
                    "converted from /home/private-client/invoice.pdf"
                ),
            }]
            integrity.stamp(allocation, "test")
            write_json(fx.allocation_path, allocation)

            task, _paths = fx.task("mirror_warden")
            encoded = json.dumps(task, ensure_ascii=False)
            self.assertNotIn("private-client", encoded)
            self.assertNotIn("secret-server", encoded)
            self.assertNotIn("/home/", encoded)
            self.assertIn("[local-path]", encoded)


if __name__ == "__main__":
    unittest.main()
