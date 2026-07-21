"""Small value-contract helpers shared by reimbursement stages.

These helpers deliberately distinguish an absent value from a valid falsy
value such as numeric zero. Financial fallbacks must never use Python
truthiness because ``0`` is valid evidence.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def first_nonblank(*values: Any) -> Any:
    for value in values:
        if not is_blank(value):
            return value
    return None


def parse_finite_decimal(value: Any, *, field: str = "amount") -> Decimal:
    if is_blank(value):
        raise ValueError(f"{field} is required")
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite numeric value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite numeric value")
    return parsed


def format_money(value: Any, *, field: str = "amount") -> str:
    return f"{parse_finite_decimal(value, field=field):.2f}"


def parse_integer(value: Any, *, field: str, minimum: int | None = None) -> int:
    if is_blank(value) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return parsed


def display_value(value: Any, *, missing: str = "") -> str:
    return missing if is_blank(value) else str(value)


def format_optional_amount(value: Any) -> str:
    """Normalize a known scalar amount, with legacy embedded-number fallback."""
    if is_blank(value):
        return ""
    text = str(value).replace(",", "").strip()
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return ""
        parsed = Decimal(match.group(0))
    return f"{parsed:.2f}" if parsed.is_finite() else ""


def extract_evidence_amount(value: Any) -> str:
    """Extract the first decimal candidate from noisy OCR/evidence text."""
    if is_blank(value):
        return ""
    text = str(value).replace(",", "").replace("￥", "").replace("¥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return ""
    parsed = Decimal(match.group(0))
    return f"{parsed:.2f}" if parsed.is_finite() else ""
