from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import allocate_expenses as allocator  # noqa: E402
import apply_allocation_answers as updater  # noqa: E402
import check_workflow_status as workflow_status  # noqa: E402
import chief_orchestrator as chief  # noqa: E402
import close_message  # noqa: E402
import extract_invoices as extractor  # noqa: E402
import extraction_corrections as extraction_updates  # noqa: E402
from exit_codes import ExitCode  # noqa: E402
import integrity  # noqa: E402
import json_io  # noqa: E402
import package_reimbursement_files as packager  # noqa: E402
import time_utils  # noqa: E402
import value_utils  # noqa: E402
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
    def ready_stage3_fixture(self, root: Path) -> tuple[Path, Path]:
        process = root / "process"
        process.mkdir()
        extraction = {
            "schema_version": "invoice_extraction.v1",
            "documents": [],
            "unresolved_input_files": [],
        }
        integrity.stamp(extraction, "test")
        (process / "invoice-extraction.json").write_text(
            json.dumps(extraction),
            encoding="utf-8",
        )
        allocation = {
            "schema_version": "expense_allocation.v1",
            "source_extraction_fingerprint": extraction["integrity"]["fingerprint"],
            "source_policy_sha256": "",
            "source_project_context_sha256": "",
            "project_contexts": [],
            "expense_hint_reconciliation": [],
            "questions": [],
            "allocation_units": [{
                "unit_id": "UNIT-001",
                "user_no": 1,
                "source_document_id": "DOC-001",
                "source_category": "other",
                "status": "confirmed",
                "client_name": "测试客户",
                "client_charge_code": "CORP-TEST",
                "final_template_column": "other",
                "amount": "10.00",
                "invoice_amount": "10.00",
                "expense_date": "2026-07-01",
                "expenses_nature": "本地",
                "final_note": "测试费用",
                "source_filename": "test.pdf",
                "issues": [],
            }],
        }
        integrity.stamp(allocation, "test")
        allocation_path = process / "expense-allocation.json"
        allocation_path.write_text(
            json.dumps(allocation, ensure_ascii=False),
            encoding="utf-8",
        )
        return process, allocation_path

    def test_exit_code_contract_is_named_and_stable(self) -> None:
        self.assertEqual(
            [0, 1, 2, 3, 4, 130],
            [
                ExitCode.SUCCESS,
                ExitCode.OPERATIONAL_ERROR,
                ExitCode.COMMAND_ERROR,
                ExitCode.REVIEW_REQUIRED,
                ExitCode.INTEGRITY_ERROR,
                ExitCode.INTERRUPTED,
            ],
        )

    def test_skill_routes_detailed_rules_to_reference(self) -> None:
        skill_root = ROOT / "skills" / "corplution-reimbursement-wizard"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8-sig")
        rules_text = (skill_root / "references" / "workflow-core-rules.md").read_text(
            encoding="utf-8-sig"
        )

        self.assertLessEqual(len(skill_text.splitlines()), 220)
        self.assertIn("references/workflow-core-rules.md", skill_text)
        self.assertIn("## Extraction Decision Tree", rules_text)
        self.assertIn("## Validation Expectations", rules_text)

    def test_required_and_optional_json_readers_have_explicit_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(json_io.JsonReadError):
                json_io.read_json_object(path)
            self.assertIsNone(json_io.read_optional_json_object(path))

    def test_persisted_timestamps_are_timezone_aware(self) -> None:
        payload: dict = {}
        integrity.stamp(payload, "test")
        stamped_at = datetime.fromisoformat(payload["integrity"]["stamped_at"])
        self.assertIsNotNone(stamped_at.utcoffset())
        self.assertIsNotNone(datetime.fromisoformat(time_utils.iso_now()).utcoffset())

    def test_display_value_preserves_numeric_zero(self) -> None:
        self.assertEqual("0", value_utils.display_value(0, missing="?"))

    def test_invalid_journey_position_has_contextual_validation_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "journey_chain_position must be an integer"):
            updater.current_rail_chain_route([{
                "unit_id": "UNIT-001",
                "journey_chain_position": "not-a-number",
                "route": "上海-南京",
            }])

    def test_stage3_note_validation_is_pure(self) -> None:
        unit = {
            "status": "confirmed",
            "source_category": "travel",
            "document_subtype": "railway_e_ticket",
            "route": "上海-南京",
            "final_note": "G123 上海->南京 二等座",
        }
        original_note = unit["final_note"]

        errors = writer.stage3_note_errors(unit)

        self.assertEqual(original_note, unit["final_note"])
        self.assertTrue(any("finance template" in error for error in errors))

    def test_numeric_zero_invoice_amount_does_not_fall_back_to_amount(self) -> None:
        self.assertEqual("0.00", writer.invoice_amount({
            "invoice_amount": 0,
            "amount": "88.00",
        }))

    def test_stage3_rejects_non_numeric_amount_instead_of_writing_zero(self) -> None:
        unit = {
            "status": "confirmed",
            "source_category": "other",
            "final_template_column": "other",
            "amount": "not-a-number",
            "invoice_amount": "not-a-number",
            "expense_date": "2026-07-01",
            "client_name": "Test Client",
            "client_charge_code": "CORP-TEST",
            "final_note": "Test expense",
        }

        errors = writer.stage3_rule_errors(unit)
        self.assertTrue(any("amount must be a finite numeric value" in error for error in errors))
        self.assertTrue(any("invoice_amount must be a finite numeric value" in error for error in errors))

    def test_packaging_rejects_invalid_row_proof_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid proof_no"):
            packager.rows_by_proof({"rows": [{"proof_no": "not-a-number"}]})

        with self.assertRaisesRegex(ValueError, "invalid proof_no"):
            packager.proof_no_name("not-a-number")

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

    def test_close_summary_covers_adjustments_omissions_and_projects(self) -> None:
        final_rows = {
            "rows": [
                {
                    "source_unit_id": "UNIT-001",
                    "user_no": 1,
                    "proof_no": 1,
                    "expense_date": "2026-07-02",
                    "source_category": "meal",
                    "row_order_type": "meal",
                    "seller_name": "北京盒马",
                    "invoice_amount": "118.70",
                    "reimbursable_amount": "114.30",
                    "client": "千味央厨",
                    "client_charge_code": "CORP-2026-0035",
                },
                {
                    "source_unit_id": "UNIT-002",
                    "user_no": 2,
                    "proof_no": 2,
                    "expense_date": "2026-07-03",
                    "source_category": "travel",
                    "row_order_type": "rail",
                    "seller_name": "中国铁路",
                    "invoice_amount": "100.00",
                    "reimbursable_amount": "100.00",
                    "client": "千味央厨",
                    "client_charge_code": "CORP-2026-0035",
                },
                {
                    "source_unit_id": "UNIT-004",
                    "user_no": 4,
                    "proof_no": 3,
                    "expense_date": "2026-07-31",
                    "source_category": "mobile",
                    "row_order_type": "mobile",
                    "seller_name": "中国移动",
                    "invoice_amount": "130.99",
                    "reimbursable_amount": "130.99",
                    "client": "通讯费",
                    "client_charge_code": "CORP-2026-ADMIN",
                },
            ],
            "meal_daily_cap_checks": [{
                "date": "2026-06-24",
                "policy_name": "出差餐费",
                "total": "153.00",
                "cap": "150.00",
                "over_by": "3.00",
                "status": "超标但已有多人信息",
                "severity": "advisory",
            }],
            "hotel_cap_checks": [],
            "expense_hint_reconciliation": [{
                "display_token": "R1@fruit001",
                "hint_id": "HINT-001",
                "summary": "6.23 水果 ¥25.00",
                "source_category": "other",
                "resolution_action": "not_reimbursed",
                "resolution_answer": "未提供发票，本次不报销",
            }],
        }
        allocation = {
            "allocation_units": [
                {
                    "unit_id": "UNIT-001",
                    "status": "confirmed",
                    "correction_note": "",
                },
                {
                    "unit_id": "UNIT-002",
                    "status": "confirmed",
                },
                {
                    "unit_id": "UNIT-003",
                    "user_no": 3,
                    "status": "dropped",
                    "expense_date": "2026-07-01",
                    "source_category": "taxi",
                    "source_filename": "local-commute.pdf",
                    "invoice_amount": "20.00",
                    "seller_name": "本地通勤",
                },
                {
                    "unit_id": "UNIT-004",
                    "status": "confirmed",
                },
            ],
            "change_log": [{
                "changes": [
                    {
                        "unit_id": "UNIT-001",
                        "answer": "当日餐费按标准调整",
                        "before": {"reimbursable_amount": "118.70"},
                        "after": {"reimbursable_amount": "114.30"},
                    },
                    {
                        "unit_id": "UNIT-003",
                        "answer": "本地通勤不报销",
                        "before": {"status": "confirmed"},
                        "after": {"status": "dropped"},
                    },
                ],
            }],
        }
        extraction = {
            "documents": [
                {"document_id": "DOC-001", "document_role": "invoice", "source_file": "meal.pdf"},
                {
                    "document_id": "DOC-002",
                    "document_role": "invoice",
                    "source_file": "duplicate.pdf",
                    "excluded_by_user": True,
                    "exclusion_reason": "重复发票",
                    "invoice": {"total_amount": "88.00"},
                },
                {"document_id": "DOC-003", "document_role": "supporting_document", "source_file": "trip.pdf"},
            ],
            "unresolved_input_files": [],
        }
        manifest = {
            "package_root": "output/报销申请表-Test-20260721",
            "workbook": "报销申请表-Test-20260721.xlsx",
            "invoice_files": [{}, {}],
            "support_files": [{}],
            "issues": [],
        }

        summary = close_message.build_summary(final_rows, allocation, extraction, manifest)
        manifest["close_summary"] = summary

        self.assertEqual("reimbursement_close_summary.v1", summary["schema_version"])
        self.assertEqual("345.29", summary["grand_total"])
        self.assertEqual(2, summary["packaged_invoice_count"])
        self.assertEqual(1, summary["excluded_invoice_count"])
        self.assertEqual("当日餐费按标准调整", summary["amount_adjustments"][0]["reason"])
        self.assertEqual("本地通勤不报销", summary["omitted_units"][0]["reason"])
        self.assertEqual("未提供发票，本次不报销", summary["not_reimbursed_records"][0]["reason"])
        self.assertEqual(1, summary["policy_advisory_count"])
        self.assertEqual(2, summary["project_count"])
        self.assertEqual("214.30", summary["projects"][0]["total"])

        message = close_message.render(manifest)
        self.assertIn("**包路径**", message)
        self.assertIn("¥118.70 → ¥114.30", message)
        self.assertIn("重复发票", message)
        self.assertIn("用户记录无票/无唯一凭证不报", message)
        self.assertIn("CORP-2026-0035 千味央厨", message)
        self.assertIn("如有疑问或需要修改，请继续对话。", message)

    def test_close_message_prints_one_relay_marker(self) -> None:
        manifest = {
            "generated_at": "2026-07-21T12:00:00+08:00",
            "requester": "Test",
            "package_date": "20260721",
            "package_root": "output/package",
            "workbook": "book.xlsx",
            "workbook_sha256": "abc",
            "final_rows_fingerprint": "def",
            "expense_hint_reconciliation_count": 0,
            "invoice_files": [],
            "support_files": [],
            "issues": [],
            "close_summary": {
                "packaged_invoice_count": 0,
                "packaged_support_count": 0,
                "grand_total": "0.00",
                "omitted_unit_count": 0,
                "not_reimbursed_record_count": 0,
                "projects": [],
            },
        }
        output = StringIO()
        with redirect_stdout(output):
            packager.print_close_message(manifest)

        self.assertEqual(1, output.getvalue().count("CLOSE MESSAGE TO SHOW IN CHAT"))
        self.assertIn("### 0 个项目汇总", output.getvalue())
        self.assertIn("## Close Message", packager.build_markdown(manifest))

    def test_close_summary_is_required_for_verified_completion(self) -> None:
        valid, reason = close_message.validate(None, invoice_count=0, support_count=0)

        self.assertFalse(valid)
        self.assertIn("close_summary is missing", reason)

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

    def test_sha_only_duplicate_exclusion_is_atomic_and_composite_match_keeps_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "process"
            process.mkdir()
            first = root / "first.pdf"
            copy = root / "copy.pdf"
            payload = {
                "schema_version": "invoice_extraction.v1",
                "documents": [
                    {
                        "document_id": "DOC-001",
                        "source_file": str(first),
                        "sha256": "same-hash",
                        "document_role": "invoice",
                        "needs_review": False,
                    },
                    {
                        "document_id": "DOC-002",
                        "source_file": str(copy),
                        "sha256": "same-hash",
                        "document_role": "invoice",
                        "needs_review": True,
                    },
                ],
                "unresolved_input_files": [],
                "document_links": [],
            }
            integrity.stamp(payload, "test")
            extraction_path = process / "invoice-extraction.json"
            extraction_path.write_text(json.dumps(payload), encoding="utf-8")
            original = extraction_path.read_bytes()

            corrections_path = root / "corrections.json"
            corrections_path.write_text(json.dumps({
                "corrections": [{
                    "match": {"sha256": "same-hash"},
                    "action": "exclude",
                    "reason": "duplicate copy",
                    "corrected_by": "user",
                }],
            }), encoding="utf-8")
            command = [
                sys.executable,
                "-X",
                "utf8",
                str(SCRIPTS / "apply_extraction_corrections.py"),
                "--extraction",
                str(extraction_path),
                "--corrections",
                str(corrections_path),
            ]
            ambiguous = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")

            self.assertEqual(2, ambiguous.returncode)
            self.assertIn("matched 2 documents", ambiguous.stdout)
            self.assertEqual(original, extraction_path.read_bytes())
            self.assertFalse((process / "extraction-corrections.json").exists())

            corrections_path.write_text(json.dumps({
                "corrections": [{
                    "match": {"sha256": "same-hash", "source_file": str(copy)},
                    "action": "exclude",
                    "reason": "duplicate copy",
                    "corrected_by": "user",
                }],
            }), encoding="utf-8")
            precise = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")

            self.assertEqual(0, precise.returncode, precise.stdout + precise.stderr)
            updated = json.loads(extraction_path.read_text(encoding="utf-8"))
            self.assertFalse(updated["documents"][0].get("excluded_by_user", False))
            self.assertTrue(updated["documents"][1]["excluded_by_user"])

    def test_sha_only_duplicate_unsupported_resolution_is_rejected(self) -> None:
        payload = {
            "unresolved_input_files": [
                {"source_file": "first.ofd", "sha256": "same-hash", "status": "open"},
                {"source_file": "copy.ofd", "sha256": "same-hash", "status": "open"},
            ]
        }
        entry = {
            "match": {"sha256": "same-hash"},
            "action": "exclude",
            "reason": "duplicate copy",
            "corrected_by": "user",
        }

        log = extraction_updates.apply_input_resolutions(payload, {"input_resolutions": [entry]})

        self.assertTrue(any(line.startswith("ERROR:") for line in log))
        self.assertEqual(["open", "open"], [item["status"] for item in payload["unresolved_input_files"]])

    def test_status_blocks_legacy_duplicate_group_with_no_active_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "process"
            process.mkdir()
            extraction = {
                "schema_version": "invoice_extraction.v1",
                "documents": [
                    {
                        "document_id": "DOC-001",
                        "source_file": str(root / "first.pdf"),
                        "sha256": "same-hash",
                        "document_role": "invoice",
                        "needs_review": False,
                        "excluded_by_user": True,
                    },
                    {
                        "document_id": "DOC-002",
                        "source_file": str(root / "copy.pdf"),
                        "sha256": "same-hash",
                        "document_role": "invoice",
                        "needs_review": False,
                        "excluded_by_user": True,
                    },
                ],
                "unresolved_input_files": [],
                "document_links": [],
            }
            integrity.stamp(extraction, "test")
            (process / "invoice-extraction.json").write_text(json.dumps(extraction), encoding="utf-8")

            state = workflow_status.inspect_workflow(process, root / "output")

            self.assertEqual("needs_user", state["stages"]["extraction"]["status"])
            self.assertEqual(1, state["stages"]["extraction"]["duplicate_group_error_count"])
            self.assertEqual("extraction", state["next"]["stage"])
            self.assertIn("exactly one canonical active copy", state["next"]["summary"])

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

    def test_stage3_temporary_workbook_keeps_xlsx_suffix_and_parent(self) -> None:
        target = Path("output") / "reimbursement.xlsx"
        staged = writer.stage3_temporary_path(target, "abc123")

        self.assertEqual(target.parent, staged.parent)
        self.assertEqual(".xlsx", staged.suffix)
        self.assertIn("stage3-abc123", staged.name)

    def test_stage3_artifact_promotion_replaces_the_generation_as_one_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            targets = [
                root / "reimbursement.xlsx",
                root / "process" / "final-expense-rows.json",
                root / "process" / "final-expense-rows.md",
            ]
            staged = [writer.stage3_temporary_path(path, "success") for path in targets]
            for index, path in enumerate(targets):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"old-{index}", encoding="utf-8")
            for index, path in enumerate(staged):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"new-{index}", encoding="utf-8")

            writer.promote_stage3_artifacts(list(zip(staged, targets)))

            self.assertEqual(
                ["new-0", "new-1", "new-2"],
                [path.read_text(encoding="utf-8") for path in targets],
            )
            self.assertTrue(all(not path.exists() for path in staged))
            self.assertEqual([], list(root.rglob("*.stage3-previous-*")))

    def test_stage3_artifact_promotion_rolls_back_all_targets_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            targets = [
                root / "reimbursement.xlsx",
                root / "process" / "final-expense-rows.json",
                root / "process" / "final-expense-rows.md",
            ]
            staged = [writer.stage3_temporary_path(path, "failure") for path in targets]
            for index, path in enumerate(targets):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"old-{index}", encoding="utf-8")
            for index, path in enumerate(staged):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"new-{index}", encoding="utf-8")

            real_replace = writer.os.replace
            failed = {"value": False}

            def fail_second_promotion(source: str | Path, destination: str | Path) -> None:
                if (
                    Path(source) == staged[1]
                    and Path(destination) == targets[1]
                    and not failed["value"]
                ):
                    failed["value"] = True
                    raise PermissionError("simulated final-rows lock")
                real_replace(source, destination)

            with patch.object(writer.os, "replace", side_effect=fail_second_promotion):
                with self.assertRaises(writer.Stage3PromotionError) as raised:
                    writer.promote_stage3_artifacts(list(zip(staged, targets)))

            self.assertTrue(raised.exception.previous_artifacts_preserved)
            self.assertEqual(
                ["old-0", "old-1", "old-2"],
                [path.read_text(encoding="utf-8") for path in targets],
            )
            self.assertTrue(all(not path.exists() for path in staged))

    def test_stage3_result_marker_distinguishes_blocked_review_and_ok(self) -> None:
        cases = [
            ("blocked", ExitCode.COMMAND_ERROR, False, False),
            ("review_required", ExitCode.REVIEW_REQUIRED, True, False),
            ("ok", ExitCode.SUCCESS, True, True),
        ]
        for status, exit_code, artifacts_written, package_allowed in cases:
            with self.subTest(status=status):
                output = StringIO()
                with redirect_stdout(output):
                    writer.print_stage3_result(
                        status,
                        exit_code=exit_code,
                        allocation_fingerprint="a" * 64,
                        blocking_errors=1 if status == "blocked" else 0,
                        blocking_policy_checks=1 if status == "review_required" else 0,
                        artifacts_written=artifacts_written,
                        previous_artifacts_preserved=status == "blocked",
                        package_allowed=package_allowed,
                    )
                text = output.getvalue()
                self.assertIn(f"STAGE3_RESULT: {status}", text)
                self.assertIn(f"exit_code={int(exit_code)}", text)
                self.assertIn(
                    f"package_allowed={'true' if package_allowed else 'false'}",
                    text,
                )
                self.assertEqual(not package_allowed, "DO NOT RUN PACKAGE" in text)

    def test_stage3_post_write_scan_failure_preserves_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process, allocation_path = self.ready_stage3_fixture(root)
            workbook = root / "reimbursement.xlsx"
            final_rows = process / "final-expense-rows.json"
            final_markdown = process / "final-expense-rows.md"
            workbook.write_bytes(b"previous workbook")
            final_rows.write_text("previous final rows", encoding="utf-8")
            final_markdown.write_text("previous markdown", encoding="utf-8")

            output = StringIO()
            with patch.object(writer, "workbook_text_issues", return_value=["simulated scan failure"]):
                with redirect_stdout(output):
                    rc = writer.main([
                        "--allocation", str(allocation_path),
                        "--output", str(workbook),
                        "--requester", "Test",
                        "--process-dir", str(process),
                    ])

            self.assertEqual(ExitCode.COMMAND_ERROR, rc)
            self.assertEqual(b"previous workbook", workbook.read_bytes())
            self.assertEqual("previous final rows", final_rows.read_text(encoding="utf-8"))
            self.assertEqual("previous markdown", final_markdown.read_text(encoding="utf-8"))
            self.assertIn("STAGE3_RESULT: blocked", output.getvalue())
            self.assertIn("previous_artifacts_preserved=true", output.getvalue())
            self.assertEqual([], list(root.rglob("*.stage3-*")))

    def test_stage3_success_promotes_matching_workbook_and_final_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process, allocation_path = self.ready_stage3_fixture(root)
            workbook = root / "reimbursement.xlsx"
            output = StringIO()

            with redirect_stdout(output):
                rc = writer.main([
                    "--allocation", str(allocation_path),
                    "--output", str(workbook),
                    "--requester", "Test",
                    "--process-dir", str(process),
                ])

            final_rows = json.loads(
                (process / "final-expense-rows.json").read_text(encoding="utf-8")
            )
            allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
            self.assertEqual(ExitCode.SUCCESS, rc)
            self.assertTrue(workbook.is_file())
            self.assertEqual(
                hashlib.sha256(workbook.read_bytes()).hexdigest(),
                final_rows["workbook_sha256"],
            )
            self.assertEqual(
                allocation["integrity"]["fingerprint"],
                final_rows["source_allocation_fingerprint"],
            )
            self.assertIn("STAGE3_RESULT: ok", output.getvalue())
            self.assertIn("package_allowed=true", output.getvalue())
            self.assertEqual([], list(root.rglob("*.stage3-*")))


if __name__ == "__main__":
    unittest.main()
