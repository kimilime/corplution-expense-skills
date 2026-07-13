from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import allocate_expenses as allocator  # noqa: E402
import apply_allocation_answers as updater  # noqa: E402
import chief_orchestrator as chief  # noqa: E402
import extract_invoices as extractor  # noqa: E402
import integrity  # noqa: E402
import package_reimbursement_files as packager  # noqa: E402
import write_reimbursement_template as writer  # noqa: E402


def meal_unit() -> dict:
    return {
        "unit_id": "UNIT-001",
        "user_no": 1,
        "source_category": "meal",
        "document_subtype": "invoice",
        "source_filename": "meal.pdf",
        "seller_name": "郑州德克士加盟店",
        "city": "郑州",
        "amount": "61.80",
        "invoice_amount": "61.80",
        "issue_date": "2026-06-01",
        "expense_date": "",
        "final_template_column": "travel",
        "final_note": "出差餐费",
        "client_name": "",
        "client_charge_code": "",
        "status": "draft",
        "confidence": "low",
    }


def context_with_hint(**hint: object) -> dict:
    return {
        "context_id": "CTX-001",
        "date_start": "2026-06-01",
        "date_end": "2026-06-03",
        "city": "郑州",
        "client_name": "千味央厨",
        "client_charge_code": "CORP-QW",
        "meal_hints": [hint],
        "expense_hints": [],
    }


class ExpenseHintCompletenessTests(unittest.TestCase):
    def test_same_meal_record_in_both_hint_arrays_is_deduplicated(self) -> None:
        context = context_with_hint(date="2026-06-01", amount="61.80", merchant="德克士")
        context["expense_hints"] = [{
            "source_category": "meal",
            "date": "2026-06-01",
            "amount": "61.80",
        }]
        hints = allocator.expense_hints_from_contexts([context])

        self.assertEqual(1, len(hints))
        self.assertEqual(["meal_hints", "expense_hints"], hints[0]["_source_fields"])

    def test_same_date_amount_but_different_cross_array_merchants_are_not_deduplicated(self) -> None:
        context = context_with_hint(date="2026-06-01", amount="61.80", merchant="德克士")
        context["expense_hints"] = [{
            "source_category": "meal",
            "date": "2026-06-01",
            "amount": "61.80",
            "merchant": "汉堡王",
        }]
        hints = allocator.expense_hints_from_contexts([context])

        self.assertEqual(2, len(hints))

    def test_unique_hint_match_is_recorded_bidirectionally(self) -> None:
        unit = meal_unit()
        records = allocator.apply_expense_hints(
            [unit],
            [context_with_hint(date="2026-06-01", amount="61.80", merchant="德克士")],
        )

        self.assertEqual("matched", records[0]["match_status"])
        self.assertEqual("not_required", records[0]["resolution_status"])
        self.assertEqual(["UNIT-001"], records[0]["matched_unit_ids"])
        self.assertEqual([records[0]["hint_id"]], unit["matched_expense_hint_ids"])

    def test_unmatched_hint_creates_one_explicit_blocking_question(self) -> None:
        unit = meal_unit()
        records = allocator.apply_expense_hints(
            [unit],
            [context_with_hint(date="2026-06-02", amount="999.00", merchant="不存在的餐厅")],
        )
        questions = allocator.build_questions([unit], [], records)
        hint_questions = [
            question for question in questions
            if question.get("question_type") == "expense_hint_reconciliation"
        ]

        self.assertEqual("unmatched", records[0]["match_status"])
        self.assertEqual(1, len(hint_questions))
        self.assertTrue(hint_questions[0]["requires_explicit_answer"])
        self.assertIn("不能静默忽略", hint_questions[0]["question"])
        allocation = {
            "project_contexts": [context_with_hint(amount="999.00")],
            "allocation_units": [unit],
            "expense_hint_reconciliation": records,
            "questions": questions,
        }
        self.assertTrue(writer.expense_hint_reconciliation_errors(allocation))

    def test_hint_question_requires_structured_not_reimbursed_resolution(self) -> None:
        payload = {
            "expense_hint_reconciliation": [{
                "hint_id": "CTX-001:meal_hints:1",
                "question_id": "Q-HINT-001",
                "display_ref": "R1",
                "match_status": "unmatched",
                "resolution_status": "open",
                "resolution_answer": "",
            }],
            "questions": [{
                "question_id": "Q-HINT-001",
                "question_type": "expense_hint_reconciliation",
                "status": "open",
                "requires_explicit_answer": True,
            }],
        }
        with self.assertRaises(ValueError):
            updater.apply_question_updates(payload, [{"question_id": "Q-HINT-001", "status": "answered"}])

        with self.assertRaises(ValueError):
            updater.apply_question_updates(payload, [{
                "question_id": "Q-HINT-001",
                "status": "answered",
                "answer": "这条记录不报销，可以排除",
            }])
        updater.apply_expense_hint_resolutions(payload, [{
            "question_id": "Q-HINT-001",
            "record_ref": "R1",
            "hint_id": "CTX-001:meal_hints:1",
            "action": "not_reimbursed",
            "unit_ids": [],
            "note": "商户未开票，本次不报销",
        }])
        self.assertEqual("resolved", payload["expense_hint_reconciliation"][0]["resolution_status"])
        self.assertEqual("not_reimbursed", payload["expense_hint_reconciliation"][0]["resolution_action"])
        self.assertEqual("answered", payload["questions"][0]["status"])
        self.assertEqual([], writer.expense_hint_reconciliation_errors(payload))

    def test_unmatched_hints_are_grouped_but_every_record_token_is_required(self) -> None:
        context = context_with_hint(date="2026-06-02", amount="999.00", merchant="未找到一号")
        context["meal_hints"].append({
            "date": "2026-06-03",
            "amount": "888.00",
            "merchant": "未找到二号",
        })
        records = allocator.apply_expense_hints([meal_unit()], [context])
        questions = allocator.build_questions([meal_unit()], [], records)
        hint_questions = [
            question for question in questions
            if question.get("question_type") == "expense_hint_reconciliation"
        ]
        self.assertEqual(1, len(hint_questions))
        self.assertEqual(
            [records[0]["display_token"], records[1]["display_token"]],
            hint_questions[0]["required_answer_tokens"],
        )
        payload = {
            "expense_hint_reconciliation": records,
            "questions": hint_questions,
        }
        updater.apply_expense_hint_resolutions(payload, [{
            "question_id": hint_questions[0]["question_id"],
            "record_ref": "R1",
            "hint_id": records[0]["hint_id"],
            "action": "not_reimbursed",
            "unit_ids": [],
            "note": "记录有误，不报销",
        }, {
            "question_id": hint_questions[0]["question_id"],
            "record_ref": "R2",
            "hint_id": records[1]["hint_id"],
            "action": "pending_invoice",
            "unit_ids": [],
            "note": "商户稍后补开",
        }])
        self.assertEqual(["resolved", "pending_evidence"], [record["resolution_status"] for record in records])
        self.assertEqual("open", hint_questions[0]["status"])
        self.assertTrue(writer.expense_hint_reconciliation_errors({
            "project_contexts": [context],
            "allocation_units": [meal_unit()],
            "expense_hint_reconciliation": records,
        }))

        updater.apply_expense_hint_resolutions(payload, [{
            "question_id": hint_questions[0]["question_id"],
            "record_ref": "R2",
            "hint_id": records[1]["hint_id"],
            "action": "not_reimbursed",
            "unit_ids": [],
            "note": "最终确认不报销",
        }])
        self.assertEqual({"resolved"}, {record["resolution_status"] for record in records})
        self.assertEqual("answered", hint_questions[0]["status"])

    def test_matched_existing_resolution_links_active_unit(self) -> None:
        unit = meal_unit()
        payload = {
            "allocation_units": [unit],
            "expense_hint_reconciliation": [{
                "hint_id": "CTX-001:meal_hints:1",
                "question_id": "Q-HINT-001",
                "display_ref": "R1",
                "match_status": "ambiguous",
                "resolution_status": "open",
                "resolution_answer": "",
            }],
            "questions": [{
                "question_id": "Q-HINT-001",
                "question_type": "expense_hint_reconciliation",
                "status": "open",
            }],
        }
        updater.apply_expense_hint_resolutions(payload, [{
            "question_id": "Q-HINT-001",
            "record_ref": "R1",
            "hint_id": "CTX-001:meal_hints:1",
            "action": "matched_existing",
            "unit_ids": ["UNIT-001"],
            "note": "对应第1项",
        }])

        record = payload["expense_hint_reconciliation"][0]
        self.assertEqual("resolved", record["resolution_status"])
        self.assertEqual(["UNIT-001"], record["matched_unit_ids"])
        self.assertEqual(["1"], record["matched_user_nos"])

        unit["status"] = "dropped"
        updater.refresh_expense_hint_reconciliation(payload, {"UNIT-001"})
        self.assertEqual("open", record["resolution_status"])
        self.assertEqual("", record["resolution_action"])
        self.assertEqual("matched_unit_closed", record["match_status"])

    def test_dropping_matched_unit_reopens_hint_completeness_gate(self) -> None:
        unit = {**meal_unit(), "status": "dropped"}
        payload = {
            "allocation_units": [unit],
            "expense_hint_reconciliation": [{
                "hint_id": "CTX-001:meal_hints:1",
                "summary": "2026-06-01 德克士 RMB 61.80",
                "match_status": "matched",
                "resolution_status": "not_required",
                "matched_unit_ids": ["UNIT-001"],
                "matched_user_nos": [1],
                "question_id": "",
                "resolution_answer": "",
            }],
            "questions": [],
        }
        updater.refresh_expense_hint_reconciliation(payload, {"UNIT-001"})

        self.assertEqual("open", payload["expense_hint_reconciliation"][0]["resolution_status"])
        self.assertEqual(1, len(payload["questions"]))
        self.assertTrue(payload["questions"][0]["requires_explicit_answer"])


class CrossStageRegressionTests(unittest.TestCase):
    def test_packaging_refuses_legacy_final_rows_without_hint_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "process"
            process.mkdir()
            extraction = {
                "schema_version": "invoice_extraction.v1",
                "documents": [],
                "unresolved_input_files": [],
            }
            integrity.stamp(extraction, "test")
            extraction_path = process / "invoice-extraction.json"
            extraction_path.write_text(json.dumps(extraction), encoding="utf-8")
            allocation = {
                "schema_version": "expense_allocation.v1",
                "source_extraction_fingerprint": extraction["integrity"]["fingerprint"],
                "allocation_units": [],
                "questions": [],
            }
            integrity.stamp(allocation, "test")
            (process / "expense-allocation.json").write_text(json.dumps(allocation), encoding="utf-8")
            workbook = root / "reimbursement.xlsx"
            workbook.write_bytes(b"workbook")
            final_rows = {
                "schema_version": "final_expense_rows.v1",
                "requester": "Test",
                "source_allocation_fingerprint": allocation["integrity"]["fingerprint"],
                "generated_with_allow_unconfirmed": False,
                "open_allocation_questions": 0,
                "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
                "blocking_policy_checks": 0,
                "rows": [],
            }
            integrity.stamp(final_rows, "test")
            final_rows_path = process / "final-expense-rows.json"
            final_rows_path.write_text(json.dumps(final_rows), encoding="utf-8")

            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    packager.build_package(
                        final_rows_path,
                        extraction_path,
                        workbook,
                        root / "staging-package",
                        "20260712",
                    )

            self.assertEqual(2, raised.exception.code)
            self.assertFalse((root / "staging-package").exists())

    def test_hotel_city_update_synchronizes_cap_field_and_row(self) -> None:
        unit = {
            "unit_id": "UNIT-001",
            "source_category": "hotel",
            "city": "",
            "hotel_city": "",
            "amount": "800.00",
            "invoice_amount": "800.00",
            "expense_date": "2026-06-01",
            "final_template_column": "hotel",
            "final_note": "出差酒店（1晚，2026-06-01-2026-06-02）",
            "client_name": "Test Client",
            "client_charge_code": "CORP-TEST",
            "proof_no": 1,
            "user_no": 1,
        }
        updater.apply_unit_update(unit, {"unit_id": "UNIT-001", "city": "北京市"}, False)
        row = writer.make_rows([unit], "Requester")[0]

        self.assertEqual("北京市", unit["hotel_city"])
        self.assertEqual("北京市", row["hotel_city"])
        self.assertEqual("first_tier", writer.hotel_city_policy(row)["city_tier"])

    def test_exact_duplicate_is_marked_for_stage1_review(self) -> None:
        documents = [
            {
                "document_id": "DOC-001",
                "source_file": "first.pdf",
                "sha256": "same-hash",
                "document_role": "supporting_document",
                "issues": [],
                "needs_review": False,
            },
            {
                "document_id": "DOC-002",
                "source_file": "copy.pdf",
                "sha256": "same-hash",
                "document_role": "supporting_document",
                "issues": [],
                "needs_review": False,
            },
        ]
        links, _review = extractor.build_links_and_reviews(documents)

        self.assertTrue(documents[1]["needs_review"])
        self.assertTrue(any(link["relation"] == "duplicate_source_file" for link in links))
        self.assertIn("Stage 1", documents[1]["issues"][0]["suggested_action"])

    def test_allocation_refuses_pending_duplicate_instead_of_allowing_stage2_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "process"
            process.mkdir()
            extraction = {
                "schema_version": "invoice_extraction.v1",
                "documents": [{
                    "document_id": "DOC-002",
                    "source_file": "copy.pdf",
                    "document_role": "invoice",
                    "needs_review": True,
                    "issues": [{"problem": "File content exactly duplicates DOC-001."}],
                }],
                "unresolved_input_files": [],
            }
            integrity.stamp(extraction, "test")
            extraction_path = process / "invoice-extraction.json"
            extraction_path.write_text(json.dumps(extraction), encoding="utf-8")
            context_path = root / "project-context.json"
            context_path.write_text(json.dumps({
                "schema_version": "project_context.v1",
                "project_contexts": [{
                    "date_start": "2026-06-01",
                    "date_end": "2026-06-30",
                    "city": "上海",
                    "client_name": "Test Client",
                    "client_charge_code": "CORP-TEST",
                }],
            }, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(SCRIPTS / "allocate_expenses.py"),
                    "--extraction",
                    str(extraction_path),
                    "--context",
                    str(context_path),
                    "--output",
                    str(process),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("duplicate decision required at Stage 1", result.stderr)
            self.assertFalse((process / "expense-allocation.json").exists())

    def test_chief_forces_utf8_for_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = root / "encoding.json"
            child = root / "child.py"
            child.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps({"
                "'io': os.environ.get('PYTHONIOENCODING'), "
                "'utf8': os.environ.get('PYTHONUTF8'), "
                "'stdout': sys.stdout.encoding}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            rc = chief.run_child(
                stage="test",
                script_name="child.py",
                command=[sys.executable, str(child), str(result_path)],
                process_dir=root / "process",
                output_root=root / "output",
                journal=root / "process" / "journal.jsonl",
            )
            observed = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(0, rc)
            self.assertEqual("utf-8", observed["io"])
            self.assertEqual("1", observed["utf8"])
            self.assertEqual("utf-8", observed["stdout"].lower())

    def test_package_promotion_retries_transient_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "staging"
            target = root / "final"
            source.mkdir()
            (source / "file.txt").write_text("ready", encoding="utf-8")
            original_replace = Path.replace
            attempts = {"count": 0}

            def flaky_replace(path: Path, destination: Path) -> Path:
                if path == source and attempts["count"] < 2:
                    attempts["count"] += 1
                    raise PermissionError("temporary Windows lock")
                return original_replace(path, destination)

            with patch.object(Path, "replace", new=flaky_replace), patch.object(packager.time, "sleep"):
                packager.replace_path_with_retry(source, target, attempts=4, initial_delay=0)

            self.assertEqual(2, attempts["count"])
            self.assertTrue((target / "file.txt").is_file())


if __name__ == "__main__":
    unittest.main()
