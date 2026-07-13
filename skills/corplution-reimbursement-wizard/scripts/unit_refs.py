#!/usr/bin/env python3
"""Stable evidence-derived identity codes (unit_ref) for allocation units.

A unit_ref answers "which concrete expense is this?" independently of display
numbering. Display numbers (第N项) shift whenever invoices are added/removed
and allocation is regenerated; refs stay identical as long as the underlying
EVIDENCE is identical, because they are derived only from evidence-side facts
captured at unit creation: source document sha256, the item's position/id
inside that document, and the created amount/date/route/category.

Two consequences, both intended:
- Same evidence across generations -> same ref -> a past decision can be
  safely carried over (rebase) or referenced ("1@a1b2c3d4").
- Evidence changed (extraction correction, re-parse) -> ref changes -> old
  decisions about it are refused until re-verified by a human.

Refs are computed once by allocate and stored on the unit; they are never
recomputed from decided fields and never settable through the updater.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def compute_unit_identity_sha256(
    doc_sha256: str,
    per_doc_ordinal: int,
    unit: dict[str, Any],
) -> str:
    parts = [
        _clean(doc_sha256),
        str(per_doc_ordinal),
        _clean(unit.get("source_item_id")),
        _clean(unit.get("amount")),
        _clean(unit.get("expense_date")),
        _clean(unit.get("origin")),
        _clean(unit.get("destination")),
        _clean(unit.get("source_category")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_unit_ref(doc_sha256: str, per_doc_ordinal: int, unit: dict[str, Any]) -> str:
    return compute_unit_identity_sha256(doc_sha256, per_doc_ordinal, unit)[:8]


def assign_unit_refs(extraction: dict[str, Any], units: list[dict[str, Any]]) -> None:
    """Post-pass: stamp every unit with its evidence ref. Idempotent per run."""
    sha_by_doc = {
        doc.get("document_id"): _clean(doc.get("sha256"))
        for doc in extraction.get("documents", [])
    }
    ordinal_by_doc: dict[str, int] = {}
    refs: dict[str, str] = {}
    identities: set[str] = set()
    for unit in units:
        doc_id = _clean(unit.get("source_document_id"))
        doc_sha256 = sha_by_doc.get(doc_id, "")
        if not doc_sha256:
            raise ValueError(
                f"cannot assign stable identity to {unit.get('unit_id') or doc_id or '?'}: "
                "source document SHA-256 is missing"
            )
        ordinal_by_doc[doc_id] = ordinal_by_doc.get(doc_id, 0) + 1
        identity = compute_unit_identity_sha256(doc_sha256, ordinal_by_doc[doc_id], unit)
        ref = identity[:8]
        if identity in identities:
            raise ValueError(
                f"duplicate evidence identity for allocation unit {unit.get('unit_id') or '?'}; "
                "resolve duplicate source evidence in Stage 1"
            )
        if ref in refs and refs[ref] != identity:
            raise ValueError(
                f"allocation unit reference collision at {ref}; regeneration cannot continue safely"
            )
        identities.add(identity)
        refs[ref] = identity
        unit["source_sha256"] = doc_sha256
        unit["unit_identity_sha256"] = identity
        unit["unit_ref"] = ref
