from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_TESTS = ROOT / "skills" / "corplution-reimbursement-wizard" / "tests"


class BundledGenerationGuardSuiteTests(unittest.TestCase):
    def test_bundled_generation_guard_suite(self) -> None:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", str(SKILL_TESTS), "-v"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, result.returncode, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
