from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import integrity  # noqa: E402
import hint_refs  # noqa: E402
import allocate_expenses  # noqa: E402
import allocation_generations  # noqa: E402


def stamp(payload: dict) -> dict:
    integrity.stamp(payload, "generation-guard-test")
    return payload


def unit(number: int, ref: str, *, status: str = "needs_confirmation", sha: str = "a" * 64) -> dict:
    return {
        "unit_id": f"UNIT-{number:03d}",
        "user_no": str(number),
        "unit_ref": ref,
        "unit_identity_sha256": hashlib.sha256(ref.encode("ascii")).hexdigest(),
        "source_sha256": sha,
        "source_filename": f"item-{number}.pdf",
        "source_document_id": f"DOC-{number:03d}",
        "source_item_id": "ITEM-1",
        "source_category": "meal",
        "amount": "88.00",
        "invoice_amount": "88.00",
        "reimbursable_amount": "88.00",
        "expense_date": "2026-07-02",
        "status": status,
        "issues": [],
        "client_name": "山西信托" if status == "confirmed" else "",
        "client_charge_code": "SX001" if status == "confirmed" else "",
        "final_note": "已确认" if status == "confirmed" else "",
    }


def allocation(units: list[dict], *, contexts: list[dict] | None = None, policy_sha: str = "p" * 64,
               hint_record: dict | None = None) -> dict:
    questions = []
    records = []
    if hint_record:
        records = [hint_record]
        questions = [{
            "question_id": hint_record["question_id"],
            "question_type": "expense_hint_reconciliation",
            "hint_ids": [hint_record["hint_id"]],
            "unit_ids": [],
            "user_nos": [],
            "required_answer_tokens": [f"{hint_record['display_ref']}@{hint_record['hint_ref']}"],
            "question": "record question",
            "status": "open",
            "blocking": True,
            "requires_explicit_answer": True,
        }]
    return stamp({
        "schema_version": "expense_allocation.v1",
        "allocation_engine_revision": "expense-allocation-engine.v2",
        "source_extraction_file": "process/invoice-extraction.json",
        "allocation_units": units,
        "project_contexts": contexts or [],
        "questions": questions,
        "expense_hint_reconciliation": records,
        "change_log": [],
        "source_policy_sha256": policy_sha,
    })


class GenerationGuardTests(unittest.TestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

    def write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_set_requires_full_unit_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            alloc_path = td / "allocation.json"
            self.write_json(alloc_path, allocation([unit(1, "cafebabe")]))

            bare = self.run_script(
                "compose_answers.py", "--allocation", str(alloc_path),
                "--set", "1: final_note=wrong", "--output", str(td / "bare.json"),
            )
            self.assertEqual(bare.returncode, 2)
            self.assertIn("N@ref", bare.stderr)
            self.assertFalse((td / "bare.json").exists())

            correct = self.run_script(
                "compose_answers.py", "--allocation", str(alloc_path),
                "--set", "1@cafebabe: final_note=correct", "--output", str(td / "correct.json"),
            )
            self.assertEqual(correct.returncode, 0, correct.stderr)
            answers = json.loads((td / "correct.json").read_text(encoding="utf-8"))
            self.assertEqual(answers["unit_updates"][0]["unit_id"], "UNIT-001")

    def test_hint_resolution_requires_full_record_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record = {
                "hint_id": "CTX:expense_hints:1",
                "hint_ref": "1234abcd",
                "hint_identity_sha256": hashlib.sha256(b"hint-1").hexdigest(),
                "question_id": "Q-HINT-001",
                "display_ref": "R1",
                "display_token": "R1@1234abcd",
                "source_category": "meal",
                "summary": "郑州餐费 RMB 88.00",
                "resolution_status": "open",
                "match_status": "unmatched",
                "candidate_units": [],
            }
            alloc_path = td / "allocation.json"
            alloc = allocation([unit(1, "cafebabe")], hint_record=record)
            self.write_json(alloc_path, alloc)
            generation = alloc["integrity"]["fingerprint"][:8]

            def decision(record_ref: str | None, *, hint_id: str | None = None, units=None) -> dict:
                entry = {
                    "question_id": "Q-HINT-001",
                    "action": "not_reimbursed" if units is None else "matched_existing",
                    "note": "test",
                }
                if record_ref is not None:
                    entry["record_ref"] = record_ref
                if hint_id is not None:
                    entry["hint_id"] = hint_id
                if units is not None:
                    entry["units"] = units
                return {
                    "schema_version": "allocation_decisions.v1",
                    "for_allocation_fingerprint": generation,
                    "decisions": [],
                    "expense_hint_resolutions": [entry],
                }

            for name, payload in [
                ("bare-r", decision("R1")),
                ("wrong-r-ref", decision("R1@deadbeef")),
                ("hint-only", decision(None, hint_id="CTX:expense_hints:1")),
            ]:
                path = td / f"{name}.json"
                self.write_json(path, payload)
                result = self.run_script(
                    "compose_answers.py", "--allocation", str(alloc_path),
                    "--decisions", str(path), "--output", str(td / f"{name}.answers.json"),
                )
                self.assertEqual(result.returncode, 2, f"{name}: {result.stdout}\n{result.stderr}")

            bare_unit_path = td / "bare-unit.json"
            self.write_json(bare_unit_path, decision("R1@1234abcd", units="1"))
            bare_unit = self.run_script(
                "compose_answers.py", "--allocation", str(alloc_path),
                "--decisions", str(bare_unit_path), "--output", str(td / "bare-unit.answers.json"),
            )
            self.assertEqual(bare_unit.returncode, 2)
            self.assertIn("N@ref", bare_unit.stderr)

            valid_path = td / "valid.json"
            self.write_json(valid_path, decision("R1@1234abcd"))
            valid = self.run_script(
                "compose_answers.py", "--allocation", str(alloc_path),
                "--decisions", str(valid_path), "--output", str(td / "valid.answers.json"),
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            answers = json.loads((td / "valid.answers.json").read_text(encoding="utf-8"))
            self.assertEqual(answers["expense_hint_resolutions"][0]["hint_id"], "CTX:expense_hints:1")

    def test_allocation_question_prints_generation_safe_r_token(self) -> None:
        contexts = [{
            "context_id": "CTX-SX",
            "client_name": "山西信托",
            "client_charge_code": "SX001",
            "city": "太原",
            "expense_hints": [{
                "source_category": "meal",
                "date": "2026-07-02",
                "amount": "88.00",
                "description": "郑州餐费",
            }],
        }]
        reconciliation = allocate_expenses.apply_expense_hints([], contexts)
        self.assertEqual(len(reconciliation), 1)
        self.assertRegex(reconciliation[0]["hint_ref"], r"^[0-9a-f]{8}$")
        questions: list[dict] = []
        allocate_expenses.add_expense_hint_reconciliation_questions(questions, reconciliation)
        token = reconciliation[0]["display_token"]
        self.assertEqual(token, f"R1@{reconciliation[0]['hint_ref']}")
        self.assertIn(token, questions[0]["required_answer_tokens"])
        self.assertIn(token, questions[0]["question"])

    def test_hint_ref_is_stable_under_unrelated_insertion(self) -> None:
        old = [{
            "source_category": "travel", "amount": "412.00", "date": "2026-07-01",
            "description": "太原高铁", "project_context_id": "CTX-SX",
        }]
        new = [
            {"source_category": "meal", "amount": "88.00", "date": "2026-07-02", "description": "郑州餐费"},
            dict(old[0]),
        ]
        hint_refs.assign_hint_refs(old)
        hint_refs.assign_hint_refs(new)
        self.assertEqual(old[0]["_hint_ref"], new[1]["_hint_ref"])
        changed = [dict(old[0], amount="413.00")]
        changed[0].pop("_hint_ref", None)
        hint_refs.assign_hint_refs(changed)
        self.assertNotEqual(old[0]["_hint_ref"], changed[0]["_hint_ref"])

    def test_rebase_accepts_same_basis_and_relocates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托", "client_charge_code": "SX001"}]
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts)
            new = allocation([
                unit(1, "cafebabe", sha="b" * 64),
                {**unit(2, "deadbeef", sha="a" * 64), "source_filename": "item-1.pdf"},
            ], contexts=contexts)
            old_path, new_path, out_path = td / "old.json", td / "new.json", td / "rebased.json"
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            result = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(out_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rebased = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(rebased["decisions"][0]["units"], "2@deadbeef")

    def test_rebase_rejects_changed_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=[{"context_id": "CTX-SX", "client_name": "山西信托"}])
            new = allocation([unit(1, "deadbeef")], contexts=[{"context_id": "CTX-HN", "client_name": "河南信托"}])
            old_path, new_path, out_path = td / "old.json", td / "new.json", td / "rebased.json"
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            result = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(out_path),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("project contexts changed", result.stderr)
            self.assertFalse(out_path.exists())

    def test_rebase_rejects_changed_or_unbound_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts, policy_sha="a" * 64)
            new = allocation([unit(1, "deadbeef")], contexts=contexts, policy_sha="b" * 64)
            old_path, new_path = td / "old.json", td / "new.json"
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            changed = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(td / "changed.json"),
            )
            self.assertEqual(changed.returncode, 2)
            self.assertIn("policy changed", changed.stderr)

            missing = allocation([unit(1, "deadbeef")], contexts=contexts)
            missing.pop("source_policy_sha256")
            integrity.stamp(missing, "generation-guard-test")
            missing_path = td / "missing.json"
            self.write_json(missing_path, missing)
            unbound = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(missing_path), "--new", str(new_path),
                "--output", str(td / "unbound.json"),
            )
            self.assertEqual(unbound.returncode, 2)
            self.assertIn("lacks source_policy_sha256", unbound.stderr)

    def test_rebase_carries_explicit_user_fields_and_hint_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托", "client_charge_code": "SX001"}]
            old_unit = unit(1, "deadbeef", status="confirmed")
            explicit = {
                "client_name": "山西信托",
                "client_charge_code": "SX001",
                "source_category": "meal",
                "is_substitute_invoice": True,
                "approval_required": "partner_approval_screenshot",
                "approval_file": "approval.png",
                "origin_place_type": "公司",
                "destination_place_type": "火车站",
                "shared_room": True,
                "room_shared_with": "同事甲",
                "final_note": "出差餐费（抵）",
                "status": "confirmed",
            }
            old_unit.update(explicit)
            hint_identity = hashlib.sha256(b"same-hint").hexdigest()
            old_hint = {
                "hint_id": "OLD:expense_hints:1",
                "hint_ref": "1234abcd",
                "hint_identity_sha256": hint_identity,
                "question_id": "Q-HINT-OLD",
                "display_ref": "R1",
                "display_token": "R1@1234abcd",
                "resolution_status": "resolved",
                "resolution_action": "matched_existing",
                "resolution_answer": "申请人已确认",
                "matched_unit_ids": ["UNIT-001"],
            }
            new_hint = {
                "hint_id": "NEW:expense_hints:2",
                "hint_ref": "1234abcd",
                "hint_identity_sha256": hint_identity,
                "question_id": "Q-HINT-NEW",
                "display_ref": "R2",
                "display_token": "R2@1234abcd",
                "resolution_status": "open",
                "match_status": "unmatched",
            }
            old = allocation([old_unit], contexts=contexts, hint_record=old_hint)
            old["change_log"] = [{
                "script": "apply_allocation_answers.py",
                "changes": [{"unit_id": "UNIT-001", "after": explicit}],
            }]
            integrity.stamp(old, "generation-guard-test")
            new_unit = unit(2, "deadbeef", sha="a" * 64)
            new_unit["source_category"] = "other"
            new = allocation([new_unit], contexts=contexts, hint_record=new_hint)
            old_path, new_path, out_path = td / "old.json", td / "new.json", td / "rebased.json"
            new["previous_allocation_file"] = str(old_path.resolve())
            new["previous_allocation_fingerprint"] = old["integrity"]["fingerprint"]
            integrity.stamp(new, "generation-guard-test")
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            result = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(out_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rebased = json.loads(out_path.read_text(encoding="utf-8"))
            fields = rebased["decisions"][0]["set"]
            for field, expected in explicit.items():
                self.assertEqual(expected, fields[field])
            hint = rebased["expense_hint_resolutions"][0]
            self.assertEqual("R2@1234abcd", hint["record_ref"])
            self.assertEqual(["2@deadbeef"], hint["units"])
            composed = self.run_script(
                "compose_answers.py", "--allocation", str(new_path), "--decisions", str(out_path),
                "--output", str(td / "answers.json"),
            )
            self.assertEqual(0, composed.returncode, composed.stderr)
            rebased["rebase_metadata"]["source_allocation_fingerprint"] = "0" * 64
            tampered_path = td / "tampered-rebase.json"
            self.write_json(tampered_path, rebased)
            tampered = self.run_script(
                "compose_answers.py", "--allocation", str(new_path), "--decisions", str(tampered_path),
                "--output", str(td / "tampered-answers.json"),
            )
            self.assertEqual(2, tampered.returncode)
            self.assertIn("official integrity stamp", tampered.stderr)

    def test_rebase_rejects_changed_engine_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts)
            new = allocation([unit(1, "deadbeef")], contexts=contexts)
            old["allocation_engine_revision"] = "expense-allocation-engine.v1"
            integrity.stamp(old, "generation-guard-test")
            old_path, new_path = td / "old.json", td / "new.json"
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            result = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(td / "out.json"),
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("engine revision changed", result.stderr)

    def test_composer_refuses_to_consume_fresh_generation_before_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts)
            old["change_log"] = [{"script": "apply_allocation_answers.py", "changes": []}]
            integrity.stamp(old, "generation-guard-test")
            old_path = td / "old.json"
            self.write_json(old_path, old)
            new = allocation([unit(1, "deadbeef")], contexts=contexts)
            new["previous_allocation_file"] = str(old_path.resolve())
            new["previous_allocation_fingerprint"] = old["integrity"]["fingerprint"]
            integrity.stamp(new, "generation-guard-test")
            new_path = td / "new.json"
            self.write_json(new_path, new)
            decisions_path = td / "ordinary-decisions.json"
            self.write_json(decisions_path, {
                "schema_version": "allocation_decisions.v1",
                "for_allocation_fingerprint": new["integrity"]["fingerprint"][:8],
                "decisions": [{"units": "1@deadbeef", "set": {"status": "confirmed"}}],
            })
            result = self.run_script(
                "compose_answers.py", "--allocation", str(new_path),
                "--decisions", str(decisions_path), "--output", str(td / "answers.json"),
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("blocked until Chief runs rebase", result.stderr)
            self.assertFalse((td / "answers.json").exists())

    def test_zero_carry_rebase_still_records_lineage_clearance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            old = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts)
            old["change_log"] = [{"script": "apply_allocation_answers.py", "changes": []}]
            integrity.stamp(old, "generation-guard-test")
            old_path = td / "old.json"
            self.write_json(old_path, old)
            new = allocation([unit(1, "cafebabe", sha="b" * 64)], contexts=contexts)
            new["previous_allocation_file"] = str(old_path.resolve())
            new["previous_allocation_fingerprint"] = old["integrity"]["fingerprint"]
            integrity.stamp(new, "generation-guard-test")
            new_path, rebase_path, answers_path = td / "new.json", td / "rebase.json", td / "answers.json"
            self.write_json(new_path, new)
            rebased = self.run_script(
                "rebase_allocation_decisions.py", "--new", str(new_path), "--output", str(rebase_path),
            )
            self.assertEqual(0, rebased.returncode, rebased.stderr)
            rebase_data = json.loads(rebase_path.read_text(encoding="utf-8"))
            self.assertEqual([], rebase_data["decisions"])
            composed = self.run_script(
                "compose_answers.py", "--allocation", str(new_path), "--decisions", str(rebase_path),
                "--output", str(answers_path),
            )
            self.assertEqual(0, composed.returncode, composed.stderr)
            answers = json.loads(answers_path.read_text(encoding="utf-8"))
            self.assertEqual(old["integrity"]["fingerprint"], answers["lineage_rebase"]["source_allocation_fingerprint"])

    def test_rebase_rejects_duplicate_short_refs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            first = unit(1, "deadbeef", status="confirmed")
            second = unit(2, "deadbeef", status="confirmed", sha="b" * 64)
            second["unit_identity_sha256"] = hashlib.sha256(b"different-unit").hexdigest()
            old = allocation([first, second], contexts=contexts)
            new = allocation([unit(1, "deadbeef")], contexts=contexts)
            old_path, new_path = td / "old.json", td / "new.json"
            self.write_json(old_path, old)
            self.write_json(new_path, new)
            result = self.run_script(
                "rebase_allocation_decisions.py", "--old", str(old_path), "--new", str(new_path),
                "--output", str(td / "out.json"),
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("duplicate short evidence ref", result.stderr)

    def test_lineage_skips_repeated_fresh_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            allocation_path = Path(td) / "expense-allocation.json"
            contexts = [{"context_id": "CTX-SX", "client_name": "山西信托"}]
            decided = allocation([unit(1, "deadbeef", status="confirmed")], contexts=contexts)
            decided["change_log"] = [{"script": "apply_allocation_answers.py", "changes": []}]
            integrity.stamp(decided, "generation-guard-test")
            self.write_json(allocation_path, decided)
            archived_decided = allocation_generations.archive_current_generation(allocation_path)

            first_rerun = allocation([unit(1, "deadbeef")], contexts=contexts)
            allocation_generations.record_previous_generation(first_rerun, archived_decided)
            integrity.stamp(first_rerun, "generation-guard-test")
            self.write_json(allocation_path, first_rerun)
            archived_rerun = allocation_generations.archive_current_generation(allocation_path)

            second_rerun = allocation([unit(1, "deadbeef")], contexts=contexts)
            allocation_generations.record_previous_generation(second_rerun, archived_rerun)
            integrity.stamp(second_rerun, "generation-guard-test")
            self.write_json(allocation_path, second_rerun)
            source_path, source, reason = allocation_generations.discover_rebase_source(
                allocation_path, second_rerun
            )
            self.assertEqual("ok", reason)
            self.assertEqual(archived_decided[0], source_path)
            self.assertEqual(decided["integrity"]["fingerprint"], source["integrity"]["fingerprint"])

    def test_official_updater_preserves_every_fingerprinted_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            allocation_path = td / "expense-allocation.json"
            current = allocation([unit(1, "deadbeef")])
            self.write_json(allocation_path, current)
            original_fingerprint = current["integrity"]["fingerprint"]

            def apply_update(source: dict, note: str) -> dict:
                answers_path = td / "answers.json"
                self.write_json(answers_path, {
                    "schema_version": "allocation_answers.v1",
                    "source_allocation_fingerprint": source["integrity"]["fingerprint"],
                    "unit_updates": [{
                        "unit_id": "UNIT-001",
                        "status": "confirmed",
                        "client_name": "山西信托",
                        "client_charge_code": "SX001",
                        "meal_context": "business_trip",
                        "final_note": note,
                    }],
                    "expense_hint_resolutions": [],
                    "question_updates": [],
                    "project_contexts": [],
                })
                result = self.run_script(
                    "apply_allocation_answers.py", "--allocation", str(allocation_path),
                    "--answers", str(answers_path),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                return json.loads(allocation_path.read_text(encoding="utf-8"))

            first = apply_update(current, "出差餐费")
            first_fingerprint = first["integrity"]["fingerprint"]
            first_archive = Path(first["previous_allocation_file"])
            self.assertTrue(first_archive.is_file())
            self.assertEqual(original_fingerprint, first["previous_allocation_fingerprint"])
            original_archive_bytes = first_archive.read_bytes()

            second = apply_update(first, "出差餐费（已确认）")
            self.assertEqual(first_fingerprint, second["previous_allocation_fingerprint"])
            self.assertTrue(Path(second["previous_allocation_file"]).is_file())
            self.assertEqual(original_archive_bytes, first_archive.read_bytes())
            self.assertEqual(2, len(list((td / "allocation-generations").glob("*.json"))))


if __name__ == "__main__":
    unittest.main()
