from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import special_codes  # noqa: E402


class LoadCodesTests(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(special_codes.load_codes(Path("nope-does-not-exist.json")), {})

    def test_malformed_file_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "special-code-definitions.json"
            bad.write_text("{ broken", encoding="utf-8")
            self.assertEqual(special_codes.load_codes(bad), {})

    def test_valid_file_returns_present_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "special-code-definitions.json"
            good.write_text(json.dumps({
                "schema_version": special_codes.SCHEMA_VERSION,
                "codes": {"admin": "CORP-2027-ADMIN", "shared_bd": "CORP-2027-BD"},
            }), encoding="utf-8")
            self.assertEqual(
                special_codes.load_codes(good),
                {"admin": "CORP-2027-ADMIN", "shared_bd": "CORP-2027-BD"},
            )

    def test_partial_file_returns_only_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "special-code-definitions.json"
            good.write_text(json.dumps({
                "schema_version": special_codes.SCHEMA_VERSION,
                "codes": {"admin": "CORP-2027-ADMIN"},
            }), encoding="utf-8")
            self.assertEqual(special_codes.load_codes(good), {"admin": "CORP-2027-ADMIN"})


class SetCodesTests(unittest.TestCase):
    def test_set_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "special-code-definitions.json"
            ok = special_codes.set_codes(admin="CORP-2030-ADMIN", shared_bd="CORP-2030-BD", path=path)
            self.assertTrue(ok)
            self.assertEqual(
                special_codes.load_codes(path),
                {"admin": "CORP-2030-ADMIN", "shared_bd": "CORP-2030-BD"},
            )

    def test_set_preserves_untouched_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "special-code-definitions.json"
            special_codes.set_codes(admin="CORP-2026-ADMIN", shared_bd="CORP-2026-BD", path=path)
            special_codes.set_codes(admin="CORP-2027-ADMIN", path=path)  # only admin
            self.assertEqual(
                special_codes.load_codes(path),
                {"admin": "CORP-2027-ADMIN", "shared_bd": "CORP-2026-BD"},
            )

    def test_bump_year_helper(self) -> None:
        self.assertEqual(special_codes._bump_year("CORP-2026-BD", "2027"), "CORP-2027-BD")
        self.assertEqual(special_codes._bump_year("CORP-2026-ADMIN", "2028"), "CORP-2028-ADMIN")


class PolicyIntegrationTests(unittest.TestCase):
    def test_definition_file_overrides_policy_toml(self) -> None:
        # The live seed file drives policy_config; confirm the wiring is in effect.
        import policy_config
        policy = policy_config.load_policy()
        overrides = special_codes.load_codes()  # live assets file
        if "admin" in overrides:
            self.assertEqual(policy.admin_code, overrides["admin"])
        if "shared_bd" in overrides:
            self.assertEqual(policy.shared_bd_code, overrides["shared_bd"])
        # Codes are always non-empty (fall back to policy.toml if the file is gone).
        self.assertTrue(policy.admin_code)
        self.assertTrue(policy.shared_bd_code)


if __name__ == "__main__":
    unittest.main()
