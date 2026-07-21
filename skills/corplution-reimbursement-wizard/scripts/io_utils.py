"""Low-level file and console helpers with one unambiguous contract."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def configure_utf8_stdio() -> None:
    """Best-effort UTF-8 configuration for direct script entry points."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
