"""Timezone-aware timestamps for persisted workflow metadata."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now(*, timespec: str = "seconds") -> str:
    return utc_now().isoformat(timespec=timespec)
