from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import integrity  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def expense_unit(
    number: int,
    ref: str,
    seed: str,
    *,
    status: str,
    filename: str,
) -> dict:
    return {
        "unit_id": f"UNIT-{number:03d}",
        "user_no": str(number),
        "unit_ref": ref,
        "unit_identity_sha256": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(filename.encode("utf-8")).hexdigest(),
        "source_filename": filename,
        "source_document_id": f"DOC-{number:03d}",
        "source_category": "meal",
        "amount": "88.00",
        "expense_date": "2026-07-02",
        "status": status,
    }


def allocation(units: list[dict], *, decided: bool) -> dict:
    payload = {
        "schema_version": "expense_allocation.v1",
        "allocation_engine_revision": "expense-allocation-engine.v2",
        "source_policy_sha256": "p" * 64,
        "source_extraction_file": "process/invoice-extraction.json",
        "project_contexts": [],
        "allocation_units": units,
        "expense_hint_reconciliation": [],
        "questions": [],
        "change_log": ([{"script": "apply_allocation_answers.py", "changes": []}] if decided else []),
    }
    integrity.stamp(payload, "test")
    return payload


class RemovedEvidenceRebaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_path = self.root / "old-allocation.json"
        self.new_path = self.root / "new-allocation.json"
        self.rebase_path = self.root / "rebase-decisions.json"
        self.resolutions_path = self.root / "rebase-removal-resolutions.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

    def write_lineage(self, old_status: str = "confirmed") -> tuple[dict, dict]:
        old = allocation([
            expense_unit(1, "oldref01", "old-evidence", status=old_status, filename="旧餐费发票.pdf")
        ], decided=True)
        write_json(self.old_path, old)
        new = allocation([
            expense_unit(7, "newref07", "new-evidence", status="draft", filename="新增餐费发票.pdf")
        ], decided=False)
        new["previous_allocation_file"] = str(self.old_path.resolve())
        new["previous_allocation_fingerprint"] = old["integrity"]["fingerprint"]
        integrity.stamp(new, "test")
        write_json(self.new_path, new)
        return old, new

    def initial_rebase(self) -> dict:
        result = self.run_script(
            "rebase_allocation_decisions.py",
            "--old", str(self.old_path),
            "--new", str(self.new_path),
            "--output", str(self.rebase_path),
            "--resolution-template", str(self.resolutions_path),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(self.rebase_path.read_text(encoding="utf-8"))

    def resolve(self, action: str, *, replacements: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        template = json.loads(self.resolutions_path.read_text(encoding="utf-8"))
        template["resolutions"][0].update({
            "action": action,
            "replacement_units": replacements or [],
            "note": "申请人已确认该旧证据的处理方式。",
        })
        write_json(self.resolutions_path, template)
        return self.run_script(
            "rebase_allocation_decisions.py",
            "--old", str(self.old_path),
            "--new", str(self.new_path),
            "--output", str(self.rebase_path),
            "--resolution-template", str(self.resolutions_path),
            "--resolutions", str(self.resolutions_path),
        )

    def test_confirmed_removed_item_blocks_then_is_audited_end_to_end(self) -> None:
        old, _new = self.write_lineage()
        initial = self.initial_rebase()
        entry = initial["removed_evidence"][0]
        self.assertEqual("open", entry["resolution_status"])
        self.assertEqual("旧餐费发票.pdf", entry["source_filename"])
        self.assertTrue(self.resolutions_path.is_file())

        refused = self.run_script(
            "compose_answers.py",
            "--allocation", str(self.new_path),
            "--decisions", str(self.rebase_path),
            "--output", str(self.root / "refused-answers.json"),
        )
        self.assertEqual(2, refused.returncode)
        self.assertIn("is open", refused.stderr)

        resolved = self.resolve("intentional_removal")
        self.assertEqual(0, resolved.returncode, resolved.stderr)
        packet = json.loads(self.rebase_path.read_text(encoding="utf-8"))
        self.assertEqual("resolved", packet["removed_evidence"][0]["resolution_status"])

        answers_path = self.root / "allocation-answers.json"
        composed = self.run_script(
            "compose_answers.py",
            "--allocation", str(self.new_path),
            "--decisions", str(self.rebase_path),
            "--output", str(answers_path),
        )
        self.assertEqual(0, composed.returncode, composed.stderr)
        tampered_answers = json.loads(answers_path.read_text(encoding="utf-8"))
        tampered_answers["lineage_rebase"]["removed_evidence"] = []
        tampered_path = self.root / "tampered-answers.json"
        write_json(tampered_path, tampered_answers)
        rejected_tamper = self.run_script(
            "apply_allocation_answers.py",
            "--allocation", str(self.new_path),
            "--answers", str(tampered_path),
            "--dry-run",
        )
        self.assertEqual(2, rejected_tamper.returncode)
        self.assertIn("does not exactly reconcile", rejected_tamper.stderr)
        applied_path = self.root / "applied-allocation.json"
        applied = self.run_script(
            "apply_allocation_answers.py",
            "--allocation", str(self.new_path),
            "--answers", str(answers_path),
            "--output", str(applied_path),
        )
        self.assertEqual(0, applied.returncode, applied.stderr)
        payload = json.loads(applied_path.read_text(encoding="utf-8"))
        self.assertEqual(
            old["allocation_units"][0]["unit_identity_sha256"],
            payload["removed_evidence_reconciliation"][0]["unit_identity_sha256"],
        )

    def test_replacement_requires_an_exact_current_token(self) -> None:
        self.write_lineage()
        self.initial_rebase()
        bad = self.resolve("replacement_provided", replacements=["7@badref00"])
        self.assertEqual(2, bad.returncode)
        self.assertIn("exact current N@ref", bad.stderr)

        template = json.loads(self.resolutions_path.read_text(encoding="utf-8"))
        template["resolutions"][0]["replacement_units"] = ["7@newref07"]
        write_json(self.resolutions_path, template)
        good = self.run_script(
            "rebase_allocation_decisions.py",
            "--old", str(self.old_path),
            "--new", str(self.new_path),
            "--output", str(self.rebase_path),
            "--resolutions", str(self.resolutions_path),
        )
        self.assertEqual(0, good.returncode, good.stderr)
        entry = json.loads(self.rebase_path.read_text(encoding="utf-8"))["removed_evidence"][0]
        self.assertEqual(["7@newref07"], entry["replacement_unit_refs"])
        self.assertEqual("resolved", entry["resolution_status"])

    def test_restore_required_remains_blocking(self) -> None:
        self.write_lineage()
        self.initial_rebase()
        result = self.resolve("restore_required")
        self.assertEqual(0, result.returncode, result.stderr)
        entry = json.loads(self.rebase_path.read_text(encoding="utf-8"))["removed_evidence"][0]
        self.assertEqual("pending_restore", entry["resolution_status"])
        refused = self.run_script(
            "compose_answers.py",
            "--allocation", str(self.new_path),
            "--decisions", str(self.rebase_path),
            "--output", str(self.root / "answers.json"),
        )
        self.assertEqual(2, refused.returncode)
        self.assertIn("pending_restore", refused.stderr)

    def test_already_dropped_removed_item_auto_resolves(self) -> None:
        self.write_lineage(old_status="dropped")
        packet = self.initial_rebase()
        entry = packet["removed_evidence"][0]
        self.assertFalse(entry["requires_confirmation"])
        self.assertEqual("prior_closed_item_removed", entry["resolution_action"])
        self.assertEqual(0, packet["rebase_metadata"]["removed_evidence_open_count"])

    def test_all_current_units_can_disappear_without_hiding_the_removal(self) -> None:
        old = allocation([
            expense_unit(1, "oldref01", "old-evidence", status="confirmed", filename="唯一发票.pdf")
        ], decided=True)
        write_json(self.old_path, old)
        new = allocation([], decided=False)
        new["previous_allocation_file"] = str(self.old_path.resolve())
        new["previous_allocation_fingerprint"] = old["integrity"]["fingerprint"]
        integrity.stamp(new, "test")
        write_json(self.new_path, new)

        packet = self.initial_rebase()
        self.assertEqual(1, packet["rebase_metadata"]["removed_evidence_open_count"])
        self.assertEqual("唯一发票.pdf", packet["removed_evidence"][0]["source_filename"])


if __name__ == "__main__":
    unittest.main()
