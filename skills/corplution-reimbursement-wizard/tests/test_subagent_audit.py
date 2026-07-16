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


if __name__ == "__main__":
    unittest.main()
