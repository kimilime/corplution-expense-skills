#!/usr/bin/env python3
"""Text-integrity checks for user-visible reimbursement data.

This is deliberately separate from cryptographic integrity. A valid JSON file
can still contain data damaged by a terminal encoding conversion, most commonly
literal question marks replacing Chinese text. The updater and Stage 3 call
these helpers before such text can become a process record or workbook cell.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


# A single ASCII '?' can be legitimate in an English free-text explanation.
# Repeated '?' is the common PowerShell/GBK loss pattern when several Chinese
# characters were injected through an inline command. The other markers are
# unambiguously suspect in confirmed business data.
QUESTION_MARK_RUN = re.compile(r"\?{2,}")
SUSPECT_MARKERS = ("\ufffd", "\u951f\u65a4\u62f7", "\u00c3", "\u00c2", "\u00e2\u20ac")


def find_suspect_text(value: Any, path: str = "$", limit: int = 20) -> list[str]:
    """Return bounded, path-qualified encoding-damage findings for a value."""
    findings: list[str] = []

    def visit(item: Any, item_path: str) -> None:
        if len(findings) >= limit:
            return
        if isinstance(item, str):
            if QUESTION_MARK_RUN.search(item):
                findings.append(f"{item_path} contains consecutive ASCII question marks: {item!r}")
                return
            for marker in SUSPECT_MARKERS:
                if marker in item:
                    findings.append(f"{item_path} contains suspect encoding marker {marker!r}: {item!r}")
                    return
            return
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{item_path}.{key}")
                if len(findings) >= limit:
                    return
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}[{index}]")
                if len(findings) >= limit:
                    return

    visit(value, path)
    return findings


def pick_fields(records: Iterable[dict[str, Any]], fields: set[str]) -> list[dict[str, Any]]:
    """Select only user-facing/editable fields, avoiding noisy raw OCR evidence."""
    return [
        {field: record[field] for field in fields if field in record}
        for record in records
        if isinstance(record, dict)
    ]
