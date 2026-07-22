from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_allocation_answers as updater  # noqa: E402
import integrity  # noqa: E402
import rebase_allocation_decisions as rebase  # noqa: E402


def unit(unit_id: str, identity: str, user_no: int, ref: str, status: str) -> dict:
    return {
        "unit_id": unit_id,
        "unit_identity_sha256": identity,
        "user_no": user_no,
        "unit_ref": ref,
        "source_sha256": identity,
        "status": status,
    }


def hint(*, resolved: bool) -> dict:
    record = {
        "hint_id": "HINT-001",
        "hint_identity_sha256": "h" * 64,
        "hint_ref": "feedcafe",
        "display_ref": "R1",
        "display_token": "R1@feedcafe",
        "question_id": "Q-HINT-001",
        "resolution_status": "resolved" if resolved else "open",
    }
    if resolved:
        record.update({
            "resolution_action": "matched_existing",
            "resolution_answer": "Applicant confirmed the matching receipt.",
            "matched_unit_ids": ["OLD-001", "OLD-002"],
            "matched_user_nos": [4, 5],
            "match_status": "matched",
        })
    return record


class RebaseHintConsistencyTests(unittest.TestCase):
    def test_rebase_cli_writes_adjusted_hint_metadata_and_safe_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "old-allocation.json"
            new_path = root / "new-allocation.json"
            output_path = root / "rebase-decisions.json"
            old_active = unit("OLD-001", "a" * 64, 4, "oldref01", "confirmed")
            old_dropped = unit("OLD-002", "b" * 64, 5, "oldref02", "dropped")
            new_active = unit("NEW-011", "a" * 64, 11, "newref01", "draft")
            new_dropped = unit("NEW-012", "b" * 64, 12, "newref02", "draft")
            basis = {
                "project_contexts": [],
                "source_policy_sha256": "p" * 64,
                "allocation_engine_revision": "expense-allocation-engine.v2",
                "change_log": [],
            }
            old_allocation = {
                **basis,
                "allocation_units": [old_active, old_dropped],
                "expense_hint_reconciliation": [hint(resolved=True)],
            }
            new_allocation = {
                **basis,
                "allocation_units": [new_active, new_dropped],
                "expense_hint_reconciliation": [hint(resolved=False)],
            }
            integrity.stamp(old_allocation, "test")
            integrity.stamp(new_allocation, "test")
            old_path.write_text(json.dumps(old_allocation), encoding="utf-8")
            new_path.write_text(json.dumps(new_allocation), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                result = rebase.main([
                    "--old", str(old_path),
                    "--new", str(new_path),
                    "--output", str(output_path),
                ])

            decisions = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(0, result)
            self.assertEqual(1, decisions["rebase_metadata"]["adjusted_hint_count"])
            self.assertEqual(
                ["11@newref01"],
                decisions["expense_hint_resolutions"][0]["units"],
            )

    def test_rebase_prunes_dropped_unit_from_multi_unit_hint_match(self) -> None:
        old_active = unit("OLD-001", "a" * 64, 4, "oldref01", "confirmed")
        old_dropped = unit("OLD-002", "b" * 64, 5, "oldref02", "dropped")
        new_active = unit("NEW-011", "a" * 64, 11, "newref01", "draft")
        new_dropped = unit("NEW-012", "b" * 64, 12, "newref02", "draft")

        carried, changed, adjusted, fresh, orphaned = rebase.migrate_hint_resolutions(
            {"expense_hint_reconciliation": [hint(resolved=True)]},
            {"expense_hint_reconciliation": [hint(resolved=False)]},
            {"a" * 64: old_active, "b" * 64: old_dropped},
            {"a" * 64: new_active, "b" * 64: new_dropped},
        )

        self.assertEqual(["11@newref01"], carried[0]["units"])
        self.assertEqual([], changed)
        self.assertEqual(1, len(adjusted))
        self.assertEqual([], fresh)
        self.assertEqual([], orphaned)

    def test_rebase_leaves_hint_open_when_every_linked_unit_was_closed(self) -> None:
        old_one = unit("OLD-001", "a" * 64, 4, "oldref01", "dropped")
        old_two = unit("OLD-002", "b" * 64, 5, "oldref02", "excluded")
        new_one = unit("NEW-011", "a" * 64, 11, "newref01", "draft")
        new_two = unit("NEW-012", "b" * 64, 12, "newref02", "draft")

        carried, changed, adjusted, _fresh, _orphaned = rebase.migrate_hint_resolutions(
            {"expense_hint_reconciliation": [hint(resolved=True)]},
            {"expense_hint_reconciliation": [hint(resolved=False)]},
            {"a" * 64: old_one, "b" * 64: old_two},
            {"a" * 64: new_one, "b" * 64: new_two},
        )

        self.assertEqual([], carried)
        self.assertEqual(1, len(changed))
        self.assertEqual([], adjusted)

    def test_updater_prunes_partial_closed_links_in_source_generation(self) -> None:
        active = unit("UNIT-001", "a" * 64, 4, "ref00001", "confirmed")
        dropped = unit("UNIT-002", "b" * 64, 5, "ref00002", "dropped")
        record = hint(resolved=True)
        record["matched_unit_ids"] = ["UNIT-001", "UNIT-002"]
        payload = {
            "allocation_units": [active, dropped],
            "expense_hint_reconciliation": [record],
            "questions": [],
        }

        updater.refresh_expense_hint_reconciliation(payload, {"UNIT-002"})

        self.assertEqual(["UNIT-001"], record["matched_unit_ids"])
        self.assertEqual(["4"], record["matched_user_nos"])
        self.assertEqual("resolved", record["resolution_status"])
        self.assertEqual([], payload["questions"])


if __name__ == "__main__":
    unittest.main()
