"""Explicit JSON read contracts shared by reimbursement scripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonReadError(ValueError):
    """A required JSON file could not be read as the requested shape."""


def read_json(path: Path) -> Any:
    """Read required JSON and raise one stable error type on read/parse failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JsonReadError(f"cannot read JSON {path}: {exc}") from exc


def read_json_object(path: Path) -> dict[str, Any]:
    """Read required JSON whose root must be an object."""
    value = read_json(path)
    if not isinstance(value, dict):
        raise JsonReadError(f"JSON root must be an object: {path}")
    return value


def read_optional_json_object(path: Path) -> dict[str, Any] | None:
    """Read observational JSON; missing/invalid/non-object input is unavailable."""
    try:
        if not path.is_file():
            return None
        return read_json_object(path)
    except (OSError, JsonReadError):
        return None
