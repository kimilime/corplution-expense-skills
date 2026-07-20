from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import write_reimbursement_template as wr  # noqa: E402
import check_workflow_status as cw  # noqa: E402
import allocate_expenses  # noqa: E402
import integrity  # noqa: E402


def meal_row(*, ctx, date, amount, attendees="", note="", meal_context=""):
    return {
        "source_category": "meal",
        "project_context_id": ctx,
        "date": wr.date_yyyymmdd(date),
        "expense_date": date,
        "amount": amount,
        "reimbursable_amount": amount,
        "invoice_amount": amount,
        "attendees": attendees,
        "note": note,
        "final_note": note,
        "meal_context": meal_context,
        "user_no": "1",
        "proof_no": "1",
    }


NIANHUI = [{
    "context_id": "CTX-003",
    "meal_standards": [
        {"date": "2026-07-17", "daily_cap": "60.00", "label": "年会自理餐标"},
        {"date": "2026-07-18", "daily_cap": "150.00", "label": "年会自理餐标"},
    ],
}]


def annotate_and_check(rows, contexts):
    wr.annotate_event_meal_standards(rows, contexts)
    wr.annotate_meal_policies(rows)
    return wr.meal_daily_cap_checks(rows)


def check_for_date(checks, yyyymmdd):
    return next(c for c in checks if c["date"] == yyyymmdd)


class EventMealStandardTests(unittest.TestCase):
    def test_declared_cap_applies_and_is_within_limit(self):
        rows = [meal_row(ctx="CTX-003", date="2026-07-17", amount="60.00")]
        checks = annotate_and_check(rows, NIANHUI)
        self.assertEqual(rows[0]["meal_cap_policy"], "event_declared:CTX-003")
        self.assertEqual(rows[0]["meal_daily_cap"], "60.00")
        c = check_for_date(checks, "20260717")
        self.assertEqual(c["cap"], "60.00")
        self.assertEqual(c["total"], "60.00")
        self.assertEqual(c["over_by"], "0.00")
        self.assertEqual(c["severity"], "ok")
        self.assertTrue(c["event_declared"])
        self.assertFalse(c["requires_user_confirmation"])

    def test_over_declared_cap_without_attendees_blocks(self):
        rows = [meal_row(ctx="CTX-003", date="2026-07-17", amount="70.00")]
        checks = annotate_and_check(rows, NIANHUI)
        c = check_for_date(checks, "20260717")
        self.assertEqual(c["cap"], "60.00")
        self.assertEqual(c["over_by"], "10.00")
        self.assertEqual(c["severity"], "blocking")
        self.assertTrue(c["requires_user_confirmation"])
        self.assertTrue(c["suggested_adjustments"])

    def test_over_declared_cap_with_attendees_is_advisory(self):
        rows = [meal_row(ctx="CTX-003", date="2026-07-17", amount="70.00", attendees="张三, 李四")]
        checks = annotate_and_check(rows, NIANHUI)
        c = check_for_date(checks, "20260717")
        self.assertEqual(c["severity"], "advisory")
        self.assertFalse(c["requires_user_confirmation"])

    def test_per_date_standard_lunch_plus_dinner(self):
        # 7.18 declared 150; lunch 60 + dinner 90 = 150 -> within cap.
        rows = [
            meal_row(ctx="CTX-003", date="2026-07-18", amount="60.00"),
            meal_row(ctx="CTX-003", date="2026-07-18", amount="90.00"),
        ]
        checks = annotate_and_check(rows, NIANHUI)
        c = check_for_date(checks, "20260718")
        self.assertEqual(c["cap"], "150.00")
        self.assertEqual(c["total"], "150.00")
        self.assertEqual(c["over_by"], "0.00")
        self.assertEqual(c["severity"], "ok")

    def test_scoping_other_context_same_date_untouched(self):
        # Same date but a different context with no meal_standards keeps the
        # generic business_trip cap and is a distinct policy pool.
        rows = [
            meal_row(ctx="CTX-003", date="2026-07-17", amount="60.00"),
            meal_row(ctx="CTX-001", date="2026-07-17", amount="120.00", note="出差餐费"),
        ]
        checks = annotate_and_check(rows, NIANHUI)
        self.assertNotIn("event_meal_cap", rows[1])
        self.assertEqual(rows[1]["meal_cap_policy"], "business_trip_meal")
        self.assertEqual(rows[1]["meal_daily_cap"], "150.00")
        policies = {c["policy"] for c in checks}
        self.assertIn("event_declared:CTX-003", policies)
        self.assertIn("business_trip_meal", policies)

    def test_direction_values(self):
        conservative = [{
            "context_id": "CTX-A",
            "meal_standards": [{"date": "2026-07-17", "daily_cap": "60.00", "label": "L"}],
        }]
        rows = [meal_row(ctx="CTX-A", date="2026-07-17", amount="50.00", note="出差餐费")]
        wr.annotate_event_meal_standards(rows, conservative)
        self.assertEqual(rows[0]["event_meal_direction"], "conservative")

        exceeds = [{
            "context_id": "CTX-A",
            "meal_standards": [{"date": "2026-07-17", "daily_cap": "200.00", "label": "L"}],
        }]
        rows = [meal_row(ctx="CTX-A", date="2026-07-17", amount="180.00", note="出差餐费")]
        wr.annotate_event_meal_standards(rows, exceeds)
        self.assertEqual(rows[0]["event_meal_direction"], "exceeds_generic")

        rows = [meal_row(ctx="CTX-003", date="2026-07-17", amount="60.00")]
        wr.annotate_event_meal_standards(rows, NIANHUI)
        self.assertEqual(rows[0]["event_meal_direction"], "no_generic_baseline")

    def test_provenance_recorded(self):
        rows = [meal_row(ctx="CTX-003", date="2026-07-17", amount="60.00")]
        checks = annotate_and_check(rows, NIANHUI)
        basis = " ".join(rows[0]["meal_policy_basis"])
        self.assertIn("CTX-003", basis)
        self.assertIn("60.00", basis)
        c = check_for_date(checks, "20260717")
        self.assertEqual(c["policy_name"], "年会自理餐标")
        self.assertTrue(any("CTX-003" in b for b in c["policy_basis"]))

    def test_writer_event_cap_over_without_attendees(self):
        # Event-declared meal-cap logic lives solely in write_reimbursement_template.
        # Assert the writer flags the over-cap-without-attendees case.
        rows_w = [meal_row(ctx="CTX-003", date="2026-07-17", amount="70.00")]
        checks = annotate_and_check(rows_w, NIANHUI)
        c = check_for_date(checks, "20260717")
        self.assertEqual(c["cap"], "60.00")
        self.assertEqual(c["over_by"], "10.00")
        self.assertTrue(c["requires_user_confirmation"])
        self.assertTrue(c["event_declared"])

    def test_status_engine_surfaces_event_meal_flag(self):
        # Option 2: the status engine never recomputes caps; it reads the writer's
        # final-expense-rows.meal_daily_cap_checks and surfaces event-declared /
        # confirmation-required items. Build a stamped Stage 3 artifact and assert
        # inspect_workflow relays the flag rather than re-deriving it.
        checks = annotate_and_check(
            [meal_row(ctx="CTX-003", date="2026-07-17", amount="70.00")], NIANHUI
        )
        rows_payload = {
            "schema_version": "final_expense_rows.v1",
            "rows": [],
            "meal_daily_cap_checks": checks,
            "blocking_policy_checks": 1,
        }
        integrity.stamp(rows_payload, "write_reimbursement_template.py")
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "process"
            pdir.mkdir()
            (pdir / "final-expense-rows.json").write_text(
                json.dumps(rows_payload, ensure_ascii=False), encoding="utf-8"
            )
            state = cw.inspect_workflow(pdir, Path(d) / "output")
        self.assertGreaterEqual(
            state["stages"]["workbook"]["event_meal_standard_flag_count"], 1
        )
        self.assertTrue(any("事件餐标" in line for line in state["lines"]))


class ContextSchemaTests(unittest.TestCase):
    def _payload(self, standards):
        return {
            "schema_version": "project_context.v1",
            "project_contexts": [{
                "date_start": "2026-07-17",
                "date_end": "2026-07-18",
                "city": "上海",
                "client_name": "年会",
                "client_charge_code": "CORP-2026-ADMIN",
                "meal_standards": standards,
            }],
        }

    def test_valid_meal_standards_accepted(self):
        errors = allocate_expenses.context_schema_errors(
            self._payload([{"date": "2026-07-17", "daily_cap": "60.00", "label": "年会自理餐标"}])
        )
        self.assertEqual(errors, [])

    def test_bad_date_rejected(self):
        errors = allocate_expenses.context_schema_errors(
            self._payload([{"date": "7-17", "daily_cap": "60.00"}])
        )
        self.assertTrue(any("meal_standards" in e and "date" in e for e in errors))

    def test_negative_cap_rejected(self):
        errors = allocate_expenses.context_schema_errors(
            self._payload([{"date": "2026-07-17", "daily_cap": "-5"}])
        )
        self.assertTrue(any("daily_cap" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
