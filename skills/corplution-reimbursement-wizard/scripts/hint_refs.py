#!/usr/bin/env python3
"""Stable content-derived identity codes for applicant expense records.

R1/R2 display labels are generation-local and may shift whenever project
contexts or invoice matching are regenerated. A hint_ref identifies the
concrete applicant-provided record independently of that display position.

The ref is derived from the record's semantic content after inherited project
context fields have been applied. Positional/generated fields such as hint_id
and _source_index are excluded. An occurrence counter distinguishes exact
duplicates without making unrelated insertions renumber existing records.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalized(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) != "hint_id" and not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def semantic_material(hint: dict[str, Any]) -> str:
    return json.dumps(_normalized(hint), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assign_hint_refs(hints: list[dict[str, Any]]) -> None:
    """Stamp each hint with an 8-char stable ref; idempotent for one ordering."""
    occurrences: dict[str, int] = {}
    refs: dict[str, str] = {}
    for hint in hints:
        material = semantic_material(hint)
        occurrences[material] = occurrences.get(material, 0) + 1
        digest_input = f"{material}|occurrence={occurrences[material]}"
        identity = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        ref = identity[:8]
        if ref in refs and refs[ref] != identity:
            raise ValueError(
                f"applicant expense-record reference collision at {ref}; "
                "the hint ledger cannot be generated safely"
            )
        refs[ref] = identity
        hint["_hint_identity_sha256"] = identity
        hint["_hint_ref"] = ref
