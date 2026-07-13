from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "corplution-reimbursement-wizard"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import allocate_expenses  # noqa: E402
import integrity  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class ProjectContextSchemaTests(unittest.TestCase):
    def valid_context(self) -> dict:
        return {
            "schema_version": "project_context.v1",
            "project_contexts": [{
                "context_id": "CTX-001",
                "date_start": "2026-06-01",
                "date_end": "2026-06-03",
                "city": "Taiyuan",
                "client_name": "Test Client",
                "client_charge_code": "CORP-TEST",
                "project_description": "On-site project",
                "meal_hints": [],
                "expense_hints": [],
            }],
        }

    def test_canonical_context_is_accepted(self) -> None:
        self.assertEqual([], allocate_expenses.context_schema_errors(self.valid_context()))

    def test_guessed_projects_charge_code_notes_shape_is_rejected(self) -> None:
        guessed = {
            "projects": [{
                "client_name": "Test Client",
                "charge_code": "CORP-TEST",
                "city": "Taiyuan",
                "notes": ["June project"],
            }],
            "special_items": [],
        }
        errors = allocate_expenses.context_schema_errors(guessed)
        joined = " ".join(errors)
        self.assertIn("projects", joined)
        self.assertIn("project_contexts", joined)

    def test_invalid_context_stops_allocation_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            extraction = {
                "schema_version": "invoice_extraction.v1",
                "documents": [],
                "unresolved_input_files": [],
            }
            integrity.stamp(extraction, "test")
            extraction_path = root / "process" / "invoice-extraction.json"
            write_json(extraction_path, extraction)
            context_path = root / "project-context.json"
            write_json(context_path, {"projects": []})
            output = root / "process"

            result = run_script(
                "allocate_expenses.py",
                "--extraction", str(extraction_path),
                "--context", str(context_path),
                "--output", str(output),
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("CANONICAL PROJECT CONTEXT TEMPLATE", result.stderr)
            self.assertIn("project_contexts", result.stderr)
            self.assertFalse((output / "expense-allocation.json").exists())


class ComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.allocation_path = self.root / "process" / "expense-allocation.json"
        allocation = {
            "schema_version": "expense_allocation.v1",
            "source_extraction_file": str(self.root / "process" / "invoice-extraction.json"),
            "allocation_units": [{
                "unit_id": "UNIT-001",
                "user_no": 1,
                "unit_ref": "00000001",
                "unit_identity_sha256": hashlib.sha256(b"unit-1").hexdigest(),
                "source_sha256": "a" * 64,
                "status": "draft",
                "source_category": "other",
                "final_template_column": "other",
                "client_name": "Test Client",
                "client_charge_code": "CORP-TEST",
                "expense_date": "2026-06-01",
                "final_note": "Test expense",
            }],
            "project_contexts": [],
            "allocation_engine_revision": "expense-allocation-engine.v2",
            "source_policy_sha256": "p" * 64,
            "questions": [{
                "question_id": "Q-001",
                "unit_ids": ["UNIT-001"],
                "status": "open",
                "question": "Confirm item",
            }],
        }
        integrity.stamp(allocation, "test")
        write_json(self.allocation_path, allocation)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def compose(self, decisions: dict) -> tuple[subprocess.CompletedProcess[str], Path]:
        if decisions.get("schema_version") == "allocation_decisions.v1" and not decisions.get(
            "for_allocation_fingerprint"
        ):
            allocation = json.loads(self.allocation_path.read_text(encoding="utf-8"))
            decisions = dict(decisions)
            decisions["for_allocation_fingerprint"] = allocation["integrity"]["fingerprint"][:8]
        decisions_path = self.root / "batch-decisions.json"
        output_path = self.root / "process" / "allocation-answers.json"
        write_json(decisions_path, decisions)
        result = run_script(
            "compose_answers.py",
            "--allocation", str(self.allocation_path),
            "--decisions", str(decisions_path),
            "--output", str(output_path),
        )
        return result, output_path

    def test_composer_resolves_real_user_no_field(self) -> None:
        result, output = self.compose({
            "schema_version": "allocation_decisions.v1",
            "decisions": [{"units": "1@00000001", "set": {"status": "confirmed"}}],
            "question_updates": [],
            "project_contexts": [],
            "confirm_units": [],
            "drop_units": [],
            "exclude_units": [],
        })
        self.assertEqual(0, result.returncode, result.stderr)
        answers = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("UNIT-001", answers["unit_updates"][0]["unit_id"])
        self.assertEqual("confirmed", answers["unit_updates"][0]["status"])
        self.assertFalse((self.root / "fill_answers.py").exists())

    def test_compact_set_accepts_utf8_chinese_and_unquoted_value_spaces(self) -> None:
        output_path = self.root / "process" / "allocation-answers.json"
        result = run_script(
            "compose_answers.py",
            "--allocation", str(self.allocation_path),
            "--set", "1@00000001: note=客户会议交通 补充说明 status=confirmed",
            "--output", str(output_path),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        answers = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual("客户会议交通 补充说明", answers["unit_updates"][0]["final_note"])
        self.assertEqual("confirmed", answers["unit_updates"][0]["status"])

    def test_composer_handles_large_allocator_user_no_batch(self) -> None:
        allocation = json.loads(self.allocation_path.read_text(encoding="utf-8"))
        prototype = allocation["allocation_units"][0]
        allocation["allocation_units"] = [
            {
                **prototype,
                "unit_id": f"UNIT-{number:03d}",
                "user_no": number,
                "unit_ref": f"{number:08x}",
                "unit_identity_sha256": hashlib.sha256(f"unit-{number}".encode()).hexdigest(),
            }
            for number in range(1, 72)
        ]
        allocation["questions"] = []
        integrity.stamp(allocation, "test")
        write_json(self.allocation_path, allocation)
        result, output = self.compose({
            "schema_version": "allocation_decisions.v1",
            "decisions": [{
                "units": ",".join(f"{number}@{number:08x}" for number in range(1, 72)),
                "set": {"status": "dropped"},
            }],
            "question_updates": [],
            "project_contexts": [],
            "confirm_units": [],
            "drop_units": [],
            "exclude_units": [],
        })
        self.assertEqual(0, result.returncode, result.stderr)
        answers = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(71, len(answers["unit_updates"]))
        self.assertEqual("UNIT-071", answers["unit_updates"][-1]["unit_id"])

    def test_composer_supports_every_non_unit_updater_action(self) -> None:
        result, output = self.compose({
            "schema_version": "allocation_decisions.v1",
            "decisions": [],
            "question_updates": [{"question_id": "Q-001", "status": "answered", "answer": "Confirmed"}],
            "project_contexts": [{
                "context_id": "CTX-001",
                "date_start": "2026-06-01",
                "date_end": "2026-06-03",
                "city": "Taiyuan",
                "client_name": "Test Client",
                "client_charge_code": "CORP-TEST",
                "project_description": "Updated",
            }],
            "confirm_units": [],
            "drop_units": ["1@00000001"],
            "exclude_units": [],
        })
        self.assertEqual(0, result.returncode, result.stderr)
        answers = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("dropped", answers["unit_updates"][0]["status"])
        self.assertEqual("Q-001", answers["question_updates"][0]["question_id"])
        self.assertEqual("CTX-001", answers["project_contexts"][0]["context_id"])

    def test_composer_structures_pending_invoice_without_closing_gate(self) -> None:
        allocation = json.loads(self.allocation_path.read_text(encoding="utf-8"))
        allocation["expense_hint_reconciliation"] = [{
            "hint_id": "CTX-001:expense_hints:1",
            "hint_ref": "1234abcd",
            "hint_identity_sha256": hashlib.sha256(b"hint-1").hexdigest(),
            "question_id": "Q-HINT-001",
            "display_ref": "R1",
            "display_token": "R1@1234abcd",
            "summary": "2026-06-01 未开票费用 RMB 88.00",
            "match_status": "unmatched",
            "resolution_status": "open",
            "resolution_action": "",
            "resolution_answer": "",
        }]
        allocation["questions"] = [{
            "question_id": "Q-HINT-001",
            "question_type": "expense_hint_reconciliation",
            "status": "open",
            "requires_explicit_answer": True,
            "required_answer_tokens": ["R1@1234abcd"],
        }]
        integrity.stamp(allocation, "test")
        write_json(self.allocation_path, allocation)

        result, output = self.compose({
            "schema_version": "allocation_decisions.v1",
            "decisions": [],
            "expense_hint_resolutions": [{
                "question_id": "Q-HINT-001",
                "record_ref": "R1@1234abcd",
                "action": "pending_invoice",
                "note": "商户承诺稍后补开",
            }],
            "question_updates": [],
            "project_contexts": [],
            "confirm_units": [],
            "drop_units": [],
            "exclude_units": [],
        })

        self.assertEqual(0, result.returncode, result.stderr)
        answers = json.loads(output.read_text(encoding="utf-8"))
        resolution = answers["expense_hint_resolutions"][0]
        self.assertEqual("CTX-001:expense_hints:1", resolution["hint_id"])
        self.assertEqual("pending_invoice", resolution["action"])
        self.assertEqual([], resolution["unit_ids"])

    def test_old_guessed_decisions_shape_is_rejected_without_output(self) -> None:
        result, output = self.compose({
            "decisions": [{"units": "1", "set": {"status": "confirmed"}}],
        })
        self.assertEqual(2, result.returncode)
        self.assertFalse(output.exists())
        self.assertIn("allocation_decisions.v1", result.stderr)
        self.assertIn("Do not generate/fill", result.stderr)

    def test_diagnostic_template_is_structurally_unapplicable(self) -> None:
        diagnostic = self.root / "process" / "allocation-answers.diagnostic.json"
        built = run_script(
            "build_allocation_answers_template.py",
            "--allocation", str(self.allocation_path),
            "--output", str(diagnostic),
        )
        self.assertEqual(0, built.returncode, built.stderr)
        applied = run_script(
            "apply_allocation_answers.py",
            "--allocation", str(self.allocation_path),
            "--answers", str(diagnostic),
            "--dry-run",
        )
        self.assertEqual(2, applied.returncode)
        self.assertIn("diagnostic templates are intentionally not accepted", applied.stderr)


if __name__ == "__main__":
    unittest.main()
