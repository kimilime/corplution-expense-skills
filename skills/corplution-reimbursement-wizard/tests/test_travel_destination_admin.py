"""Regression: Admin must never be matched as a travel destination.

A return leg (e.g. 北京南 -> 上海) must not be flagged against an Admin context
whose city happens to appear in the destination (e.g. 通讯费 / CORP-2026-ADMIN in
上海). Admin is not a project you travel to; policy says Admin is not a fallback.
Stage 2 already filters Admin out via allocate_expenses.non_admin_contexts; the
Stage 3 preflight must do the same in travel_destination_context.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import write_reimbursement_template as W  # noqa: E402

DATES = {"date_start": "2026-07-01", "date_end": "2026-07-31"}
RETURN_LEG = {
    "unit_id": "UNIT-003",
    "source_category": "travel",
    "route": "北京南-上海",
    "expense_date": "2026-07-15",
}
ADMIN_CTX = {
    "context_id": "CTX-ADMIN",
    "city": "上海",
    "client_name": "通讯费",
    "client_charge_code": W.ADMIN_CODE,
    **DATES,
}
CLIENT_CTX_SH = {
    "context_id": "CTX-CLIENT",
    "city": "上海",
    "client_name": "客户X",
    "client_charge_code": "CORP-2026-CLIENTX",
    **DATES,
}


class TravelDestinationAdminExclusion(unittest.TestCase):
    def test_admin_context_is_not_a_destination_match(self):
        # Before the fix this returned ADMIN_CTX; Admin must be excluded outright.
        self.assertIsNone(W.travel_destination_context(RETURN_LEG, [ADMIN_CTX]))

    def test_non_admin_destination_still_matches(self):
        # The fix must not over-suppress: a real project in the destination city
        # is still returned.
        self.assertEqual(
            W.travel_destination_context(RETURN_LEG, [CLIENT_CTX_SH]),
            CLIENT_CTX_SH,
        )

    def test_preflight_does_not_flag_admin_only_destination(self):
        # Unit is assigned to a real client whose context is NOT in the
        # destination city, so the only 上海 context in range is Admin. The
        # preflight must not emit the "travel route destination points to" block.
        unit = {
            **RETURN_LEG,
            "status": "confirmed",
            "client_name": "客户X",
            "client_charge_code": "CORP-2026-CLIENTX",
            "final_template_column": "travel",
            "amount": "553.00",
        }
        client_ctx_bj = {**CLIENT_CTX_SH, "city": "北京"}
        allocation = {
            "project_contexts": [ADMIN_CTX, client_ctx_bj],
            "allocation_units": [unit],
            "questions": [],
        }
        errors = W.require_ready(allocation, allow_unconfirmed=False)
        self.assertFalse(
            any("travel route destination points to" in e for e in errors),
            f"Admin wrongly flagged as travel destination: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
