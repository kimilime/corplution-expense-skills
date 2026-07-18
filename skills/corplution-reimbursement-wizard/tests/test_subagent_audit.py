"""Guards for the two-role subagent audit protocol (Otako Mirror Warden /
Kaede Gate Challenger). Locks the unified audit surface after the proposal-era
Otako/Kaede roles were retired."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import subagent_protocol as sp  # noqa: E402


class RoleSurface(unittest.TestCase):
    def test_exactly_the_two_new_roles(self):
        self.assertEqual(set(sp.ROLE_SPECS), {"mirror_warden", "gate_challenger"})

    def test_display_names_and_references_exist(self):
        self.assertEqual(sp.ROLE_SPECS["mirror_warden"]["display_name"], "Otako - Mirror Warden")
        self.assertEqual(sp.ROLE_SPECS["gate_challenger"]["display_name"], "Kaede - Gate Challenger")
        for role in sp.ROLE_SPECS.values():
            self.assertTrue(Path(role["reference"]).is_file(), role["reference"])

    def test_distinct_contract_versions(self):
        a = sp.ROLE_SPECS["mirror_warden"]["contract_version"]
        b = sp.ROLE_SPECS["gate_challenger"]["contract_version"]
        self.assertNotEqual(a, b)

    def test_retired_symbols_are_gone(self):
        for name in (
            "review_state", "analysis_state", "promote_proposals",
            "validate_promoted_proposal", "_validate_review", "_validate_analysis",
        ):
            self.assertFalse(hasattr(sp, name), f"{name} should be retired")
        for name in ("audit_state", "accept_result", "prepare_task", "validate_result"):
            self.assertTrue(hasattr(sp, name), f"{name} should exist")


class UnifiedAuditShape(unittest.TestCase):
    def test_both_roles_produce_outcome_findings_schema(self):
        for role in ("mirror_warden", "gate_challenger"):
            coverage = sp.ROLE_SPECS[role]["coverage"]
            schema = sp.response_json_schema(role, coverage)
            props = schema["properties"]
            self.assertEqual(props["schema_version"]["const"], sp.AUDIT_SCHEMA)
            self.assertIn("outcome", props)
            self.assertIn("findings", props)
            self.assertNotIn("proposals", props)
            self.assertNotIn("user_questions", props)
            self.assertEqual(
                props["audit_contract_version"]["const"],
                sp.ROLE_SPECS[role]["contract_version"],
            )

    def test_response_contract_is_audit_shaped(self):
        contract = sp.response_contract("mirror_warden", sp.ROLE_SPECS["mirror_warden"]["coverage"])
        self.assertIn("outcome", contract)
        self.assertIn("finding", contract)
        self.assertNotIn("proposal", contract)


class CliSurface(unittest.TestCase):
    def test_prepare_accept_take_new_roles_and_promote_gone(self):
        parser = sp.build_parser()
        # prepare with a new role parses; promote is no longer a subcommand
        ns = parser.parse_args(["prepare", "--role", "mirror_warden",
                                "--allocation", "a.json", "--extraction", "e.json"])
        self.assertEqual(ns.role, "mirror_warden")
        with self.assertRaises(SystemExit):
            parser.parse_args(["promote", "--all", "--reviewed-by", "coordinator",
                               "--allocation", "a.json", "--extraction", "e.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["prepare", "--role", "allocation_analyst",
                               "--allocation", "a.json", "--extraction", "e.json"])

    def test_audit_state_requires_role(self):
        import inspect
        params = list(inspect.signature(sp.audit_state).parameters)
        self.assertEqual(params[0], "role")


class ValidateResultRoundTrip(unittest.TestCase):
    """The accept/validate path for the unified audit result, no disk fixtures."""

    role = "gate_challenger"

    def _task_and_alloc(self):
        import integrity
        coverage = sp.ROLE_SPECS[self.role]["coverage"]
        alloc = {"allocation_units": [{"unit_id": "UNIT-001", "user_no": "1", "unit_ref": "abc123"}]}
        task = {
            "schema_version": sp.TASK_SCHEMA, "task_id": "t123", "role_id": self.role,
            "codename": sp.ROLE_SPECS[self.role]["codename"],
            "display_name": sp.ROLE_SPECS[self.role]["display_name"],
            "role_title": sp.ROLE_SPECS[self.role]["role_title"],
            "contract_version": sp.ROLE_SPECS[self.role]["contract_version"],
            "source_generation": {"source_allocation_fingerprint": "f" * 64,
                                  "source_extraction_fingerprint": "e" * 64},
            "required_coverage": list(coverage),
            "evidence_index": [{"document_id": "DOC-1", "source_file": "/x/a.pdf"}],
            "expense_hint_reconciliation": [],
        }
        integrity.stamp(task, "subagent_protocol.py")
        return task, alloc, coverage

    def _result(self, task, coverage, outcome, findings):
        return {
            "schema_version": sp.AUDIT_SCHEMA,
            "audit_contract_version": sp.ROLE_SPECS[self.role]["contract_version"],
            "task_id": "t123",
            "source_task_fingerprint": task["integrity"]["fingerprint"],
            "source_allocation_fingerprint": "f" * 64,
            "source_extraction_fingerprint": "e" * 64,
            "agent_id": self.role,
            "agent_display_name": sp.ROLE_SPECS[self.role]["display_name"],
            "coverage": [{"check_id": c, "status": "completed", "notes": ""} for c in coverage],
            "summary": "ok",
            "outcome": outcome,
            "findings": findings,
        }

    def test_pass_and_block_validate(self):
        task, alloc, cov = self._task_and_alloc()
        sp.validate_result(self._result(task, cov, "pass", []), task, alloc)
        blocking = [{"finding_id": "F-1", "severity": "blocking", "code": "missing_required_approval",
                     "message": "over-cap hotel lacks approval", "unit_refs": ["1@abc123"],
                     "evidence_refs": ["DOC-1"], "recommended_action": "attach approval"}]
        sp.validate_result(self._result(task, cov, "block", blocking), task, alloc)

    def test_wrong_contract_version_rejected(self):
        task, alloc, cov = self._task_and_alloc()
        bad = self._result(task, cov, "pass", [])
        bad["audit_contract_version"] = sp.ROLE_SPECS["mirror_warden"]["contract_version"]
        with self.assertRaises(sp.ProtocolError):
            sp.validate_result(bad, task, alloc)

    def test_outcome_findings_mismatch_rejected(self):
        task, alloc, cov = self._task_and_alloc()
        mismatch = self._result(task, cov, "pass", [
            {"finding_id": "F", "severity": "blocking", "code": "c", "message": "m",
             "unit_refs": ["1@abc123"], "evidence_refs": [], "recommended_action": "a"}])
        with self.assertRaises(sp.ProtocolError):
            sp.validate_result(mismatch, task, alloc)


class HandoffCapDegrade(unittest.TestCase):
    """Over-cap packets fail open to the deterministic preflight instead of hard-erroring
    with misleading 'use an attachment / split into scoped packets' advice."""

    def test_cap_raised_to_384_kib(self):
        self.assertEqual(sp.MAX_HANDOFF_PACKET_BYTES, 384 * 1024)

    def test_handoff_too_large_is_a_protocol_error_carrying_size(self):
        self.assertTrue(issubclass(sp.HandoffTooLarge, sp.ProtocolError))
        exc = sp.HandoffTooLarge("Otako - Mirror Warden", 500_000)
        self.assertEqual(exc.packet_bytes, 500_000)
        self.assertEqual(exc.display_name, "Otako - Mirror Warden")
        text = str(exc)
        self.assertIn("Fail open", text)
        self.assertIn("preflight", text)
        # The retired inducements must not reappear in the message.
        self.assertNotIn("attachment", text.lower())
        self.assertNotIn("scoped packet", text.lower())

    def test_enforce_cap_returns_size_under_cap_and_raises_over(self):
        small = {"display_name": "X", "payload": "a" * 100}
        self.assertGreater(sp._enforce_handoff_cap(small), 0)
        big = {"display_name": "Otako - Mirror Warden", "payload": "x" * (400 * 1024)}
        with self.assertRaises(sp.HandoffTooLarge):
            sp._enforce_handoff_cap(big)

    def test_prepare_task_degrades_without_writing_handoff(self):
        import tempfile
        oversize = {"display_name": "Otako - Mirror Warden", "payload": "x" * (400 * 1024)}
        with tempfile.TemporaryDirectory() as tmp:
            process_dir = Path(tmp)
            orig_canonical = sp._require_canonical_process_dir
            orig_build = sp._build_task
            sp._require_canonical_process_dir = lambda *a, **k: None
            sp._build_task = lambda *a, **k: (oversize, {})
            try:
                with self.assertRaises(sp.HandoffTooLarge):
                    sp.prepare_task("mirror_warden", Path("a.json"), Path("e.json"), process_dir)
            finally:
                sp._require_canonical_process_dir = orig_canonical
                sp._build_task = orig_build
            # No handoff task/template may be left behind for a coordinator to accept.
            self.assertEqual(list(process_dir.iterdir()), [])

    def test_prepare_task_writes_handoff_under_cap(self):
        import tempfile
        small_task = {
            "task_id": "t-happy",
            "role_id": "mirror_warden",
            "display_name": "Otako - Mirror Warden",
            "contract_version": "cv-1",
            "required_coverage": ["c1"],
            "source_generation": {
                "source_allocation_fingerprint": "f" * 64,
                "source_extraction_fingerprint": "e" * 64,
            },
            "integrity": {"fingerprint": "a" * 64},
            "payload": "small",
        }
        with tempfile.TemporaryDirectory() as tmp:
            process_dir = Path(tmp) / "process"
            process_dir.mkdir()
            orig_canonical = sp._require_canonical_process_dir
            orig_build = sp._build_task
            sp._require_canonical_process_dir = lambda *a, **k: None
            sp._build_task = lambda *a, **k: (small_task, {})
            try:
                _task, paths = sp.prepare_task(
                    "mirror_warden", Path("a.json"), Path("e.json"), process_dir
                )
            finally:
                sp._require_canonical_process_dir = orig_canonical
                sp._build_task = orig_build
            # Under cap the real handoff task + result template are written.
            self.assertTrue(paths["task"].is_file())
            self.assertTrue(paths["template"].is_file())

    def test_accept_refuses_a_degraded_tasks_result(self):
        oversize = {"display_name": "Otako - Mirror Warden", "payload": "x" * (400 * 1024)}
        orig_canonical = sp._require_canonical_process_dir
        orig_build = sp._build_task
        sp._require_canonical_process_dir = lambda *a, **k: None
        sp._build_task = lambda *a, **k: (oversize, {})
        try:
            with self.assertRaises(sp.HandoffTooLarge):
                sp.accept_result(
                    "mirror_warden", Path("a.json"), Path("e.json"),
                    Path("proc"), Path("result.json"),
                )
        finally:
            sp._require_canonical_process_dir = orig_canonical
            sp._build_task = orig_build


class MainDegradeRouting(unittest.TestCase):
    """prepare degrades cleanly (exit 0); accept refuses (exit 2)."""

    def _run(self, argv):
        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sp.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_prepare_over_cap_exits_zero_with_degrade_notice(self):
        orig = sp.prepare_task
        sp.prepare_task = lambda *a, **k: (_ for _ in ()).throw(
            sp.HandoffTooLarge("Otako - Mirror Warden", 500_000)
        )
        try:
            code, out, _err = self._run(
                ["prepare", "--role", "mirror_warden", "--allocation", "a.json", "--extraction", "e.json"]
            )
        finally:
            sp.prepare_task = orig
        self.assertEqual(code, 0)
        self.assertIn("DEGRADE", out)
        self.assertIn("preflight", out)

    def test_accept_over_cap_is_refused(self):
        orig = sp.accept_result
        sp.accept_result = lambda *a, **k: (_ for _ in ()).throw(
            sp.HandoffTooLarge("Otako - Mirror Warden", 500_000)
        )
        try:
            code, _out, err = self._run(
                ["accept", "--role", "mirror_warden", "--allocation", "a.json",
                 "--extraction", "e.json", "--result", "r.json"]
            )
        finally:
            sp.accept_result = orig
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)


class EventMealStandardPacket(unittest.TestCase):
    """The auditors read Stage-2 units, so the one-off event meal standards must
    reach them through the project-context packet, not writer-computed row fields."""

    def test_project_context_packet_carries_meal_standards(self):
        self.assertIn("meal_standards", sp.PROJECT_CONTEXT_PACKET_FIELDS)

    def test_compact_snapshot_includes_declared_standards(self):
        allocation = {
            "allocation_units": [{"unit_id": "UNIT-001", "user_no": "1", "unit_ref": "abc123"}],
            "project_contexts": [{
                "context_id": "CTX-003",
                "date_start": "2026-07-17",
                "date_end": "2026-07-18",
                "city": "上海",
                "client_name": "年会",
                "client_charge_code": "CORP-2026-ADMIN",
                "meal_standards": [
                    {"date": "2026-07-17", "daily_cap": "60.00", "label": "年会自理餐标"},
                    {"date": "2026-07-18", "daily_cap": "150.00", "label": "年会自理餐标"},
                ],
            }],
        }
        for role in ("gate_challenger", "mirror_warden"):
            snapshot = sp._compact_task_snapshot(role, allocation, {"documents": []})
            ctx = snapshot["project_contexts"][0]
            self.assertIn("meal_standards", ctx, role)
            dates = {entry.get("date") for entry in ctx["meal_standards"]}
            self.assertEqual(dates, {"2026-07-17", "2026-07-18"}, role)


if __name__ == "__main__":
    unittest.main()
