from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import write_reimbursement_template as writer  # noqa: E402
import apply_allocation_answers as updater  # noqa: E402


def meal_row(
    number: int,
    amount: str,
    *,
    note: str,
    amount_column: str,
    nature: str,
    meal_context: str = "",
    date: str = "20260518",
) -> dict:
    return {
        "user_no": number,
        "proof_no": number,
        "source_unit_id": f"UNIT-{number:03d}",
        "source_filename": f"meal-{number}.pdf",
        "source_category": "meal",
        "note": note,
        "meal_context": meal_context,
        "amount_column": amount_column,
        "expenses_nature": nature,
        "date": date,
        "expense_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "amount": amount,
        "invoice_amount": amount,
        "reimbursable_amount": amount,
        "attendees": "",
    }


class MealPolicyTests(unittest.TestCase):
    def test_shanghai_meal_column_trip_meal_uses_150_policy(self) -> None:
        row = meal_row(
            13,
            "47.00",
            note="出差餐费（上海出发前）",
            amount_column="meal",
            nature="本地",
        )
        policy = writer.meal_cap_policy(row)
        self.assertEqual("business_trip_meal", policy["policy"])
        self.assertEqual("150.00", writer.money(policy["cap"]))

    def test_same_day_trip_meals_aggregate_across_meal_and_travel_columns(self) -> None:
        rows = [
            meal_row(13, "60.00", note="出差餐费（上海出发前）", amount_column="meal", nature="本地"),
            meal_row(14, "100.00", note="出差餐费（郑州）", amount_column="travel", nature="出差"),
        ]
        writer.annotate_meal_policies(rows)
        checks = writer.meal_daily_cap_checks(rows)
        self.assertEqual(1, len(checks))
        self.assertEqual("business_trip_meal", checks[0]["policy"])
        self.assertEqual("160.00", checks[0]["total"])
        self.assertEqual("150.00", checks[0]["cap"])
        self.assertEqual("10.00", checks[0]["over_by"])
        self.assertTrue(checks[0]["requires_user_confirmation"])
        self.assertTrue(checks[0]["cross_column_aggregation"])
        self.assertEqual({"meal", "travel"}, {item["amount_column"] for item in checks[0]["items"]})

    def test_only_explicit_overtime_meal_uses_60_policy(self) -> None:
        row = meal_row(
            21,
            "61.00",
            note="加班餐费",
            amount_column="meal",
            nature="本地",
        )
        writer.annotate_meal_policies([row])
        checks = writer.meal_daily_cap_checks([row])
        self.assertEqual("local_overtime_meal", row["meal_cap_policy"])
        self.assertEqual("60.00", row["meal_daily_cap"])
        self.assertEqual("1.00", checks[0]["over_by"])

    def test_amount_column_and_city_cannot_supply_missing_policy(self) -> None:
        value = {
            "source_category": "meal",
            "city": "Shanghai",
            "amount_column": "travel",
            "expenses_nature": "出差",
            "final_note": "meal",
            "meal_context": "",
        }
        policy, error = writer.classify_meal_policy(value)
        self.assertIsNone(policy)
        self.assertIn("do not infer it from Shanghai", error)

    def test_conflicting_note_and_context_are_rejected(self) -> None:
        policy, error = writer.classify_meal_policy({
            "source_category": "meal",
            "final_note": "出差餐费",
            "meal_context": "overtime",
        })
        self.assertIsNone(policy)
        self.assertIn("signals conflict", error)

    def test_updater_does_not_accept_city_as_meal_policy_signal(self) -> None:
        unit = {
            "unit_id": "UNIT-001",
            "source_category": "meal",
            "city": "上海",
            "meal_context": "",
            "final_note": "",
        }
        error = updater.guard_meal_reclass_signal(
            unit,
            {"source_category": "meal", "city": "上海"},
            was_meal=False,
        )
        self.assertIn("City is not a policy signal", error)

    def test_updater_accepts_explicit_meal_policy_signal(self) -> None:
        unit = {
            "unit_id": "UNIT-001",
            "source_category": "meal",
            "city": "上海",
            "meal_context": "business_trip",
            "final_note": "",
        }
        error = updater.guard_meal_reclass_signal(
            unit,
            {"source_category": "meal", "meal_context": "business_trip"},
            was_meal=False,
        )
        self.assertIsNone(error)

    def test_chat_output_states_policy_invariant(self) -> None:
        rows = [meal_row(13, "47.00", note="出差餐费", amount_column="meal", nature="本地")]
        writer.annotate_meal_policies(rows)
        checks = writer.meal_daily_cap_checks(rows)
        output = io.StringIO()
        with redirect_stdout(output):
            writer.print_meal_cap_check(checks)
        text = output.getvalue()
        self.assertIn("There is no generic 本地餐=60 rule", text)
        self.assertIn("policy business_trip_meal cap 150.00", text)
        self.assertIn("column meal | nature 本地", text)


if __name__ == "__main__":
    unittest.main()
