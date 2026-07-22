"""Named text-normalization contracts for scalar and extracted evidence text."""

from __future__ import annotations

import re
from typing import Any


def strip_scalar(value: Any) -> str:
    """Trim a scalar without rewriting its internal whitespace."""
    return "" if value is None else str(value).strip()


def normalize_text(value: Any) -> str:
    """Collapse whitespace in machine-extracted or generated display text."""
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
