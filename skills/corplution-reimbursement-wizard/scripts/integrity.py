#!/usr/bin/env python3
"""Tamper detection for process/expense-allocation.json.

Why this exists: the only legitimate way to modify the allocation file is
through apply_allocation_answers.py, which preserves change history, closes
questions, and runs accounting checks. Agents sometimes bypass it with ad hoc
patch scripts or hand edits, silently corrupting the audit trail. Every
legitimate writer stamps a content fingerprint; every downstream reader
verifies it and refuses to proceed on a mismatch, so the bypass route is a
dead end rather than a shortcut.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

INTEGRITY_KEY = "integrity"


def _canonical_digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != INTEGRITY_KEY}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp(payload: dict[str, Any], stamped_by: str) -> None:
    """Attach/refresh the fingerprint. Call immediately before writing."""
    payload[INTEGRITY_KEY] = {
        "fingerprint": _canonical_digest(payload),
        "stamped_by": stamped_by,
        "stamped_at": datetime.now().replace(microsecond=0).isoformat(),
    }


def check(payload: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means missing or mismatched fingerprint."""
    info = payload.get(INTEGRITY_KEY)
    if not isinstance(info, dict) or "fingerprint" not in info:
        return False, "missing integrity stamp"
    if info["fingerprint"] != _canonical_digest(payload):
        return False, f"content does not match the fingerprint stamped by {info.get('stamped_by', '?')} at {info.get('stamped_at', '?')}"
    return True, "ok"


def _recovery_message(path: Path, reason: str, kind: str) -> str:
    bak = path.with_suffix(path.suffix + ".bak")
    if kind == "final_rows":
        return (
            f"INTEGRITY CHECK FAILED for {path}: {reason}.\n"
            "Final expense rows were modified or forged outside stage 3. There is nothing to "
            "patch here — this file is a pure derivative. Recover by re-running:\n"
            "  python scripts/write_reimbursement_template.py --allocation process/expense-allocation.json ...\n"
            "which regenerates rows, workbook, hash, and stamp together."
        )
    if kind == "extraction":
        return (
            f"INTEGRITY CHECK FAILED for {path}: {reason}.\n"
            "This file was hand-edited or regenerated outside the sanctioned flow. Hand edits are\n"
            "wiped on extractor re-runs anyway — do NOT keep patching. Recover by ONE of:\n"
            f"  1. Restore the last valid backup: cp {bak} {path}, or\n"
            "  2. Re-run scripts/extract_invoices.py (saved corrections replay automatically).\n"
            "Then make your change ONLY via:\n"
            "  python scripts/apply_extraction_corrections.py --extraction <file> --corrections <your.json>\n"
            "Corrections persist in process/extraction-corrections.json and survive re-runs."
        )
    return (
        f"INTEGRITY CHECK FAILED for {path}: {reason}.\n"
        "This file was modified outside the sanctioned flow (ad hoc patch script, manual edit,\n"
        "or regenerated with the wrong tool). Do NOT keep patching it. Recover by ONE of:\n"
        "  1. Restore a verified fingerprinted generation from process/allocation-generations/ "
        "only when its lineage is known, or\n"
        "  2. Re-run scripts/allocate_expenses.py to regenerate allocation from extraction + context.\n"
        "Then apply changes ONLY via:\n"
        "  python scripts/compose_answers.py --allocation <file> --decisions <allocation_decisions.v1.json>\n"
        "  python scripts/apply_allocation_answers.py --allocation <file> --answers process/allocation-answers.json\n"
        "This is the only route that preserves change history and runs the accounting checks."
    )


def require_valid(payload: dict[str, Any], path: Path, kind: str = "allocation") -> None:
    """Hard gate for downstream writers. Exits with code 4 on failure."""
    ok, reason = check(payload)
    if not ok:
        print(_recovery_message(path, reason, kind), file=sys.stderr)
        raise SystemExit(4)


def warn_if_invalid(payload: dict[str, Any], path: Path) -> None:
    """Soft gate for read-only diagnostic tools."""
    ok, reason = check(payload)
    if not ok:
        print(f"WARNING: {path} failed integrity check ({reason}). "
              "Results below may reflect tampered data.", file=sys.stderr)
