#!/usr/bin/env python3
"""Check and optionally install Python dependencies for this skill."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


PYTHON_IMPORTS = {
    "openpyxl": "openpyxl",
    "pdfplumber": "pdfplumber",
    "pypdf": "pypdf",
    "pdf2image": "pdf2image",
    "Pillow": "PIL",
    "pytesseract": "pytesseract",
}

if sys.version_info < (3, 11):
    PYTHON_IMPORTS["tomli"] = "tomli"


SYSTEM_TOOLS = {
    "tesseract": "Required only for OCR on image/scan-only invoices.",
    "pdftoppm": "Poppler tool used by pdf2image for OCR on scan-only PDFs.",
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def requirements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "requirements.txt"


def missing_python_packages() -> list[str]:
    missing = []
    for package, import_name in PYTHON_IMPORTS.items():
        if importlib.util.find_spec(import_name) is None:
            missing.append(package)
    return missing


def missing_system_tools() -> list[str]:
    return [tool for tool in SYSTEM_TOOLS if shutil.which(tool) is None]


def install_requirements(requirements: Path) -> int:
    command = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    print("Installing Python dependencies:")
    print(" ".join(command))
    rc = subprocess.call(command)
    if rc != 0:
        # Externally managed environments (PEP 668), e.g. sandboxed agent
        # containers, reject plain pip installs; retry with the override flag.
        retry = command + ["--break-system-packages"]
        print("Plain install failed; retrying for externally managed environments:")
        print(" ".join(retry))
        rc = subprocess.call(retry)
    return rc


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Check Corplution reimbursement skill dependencies.")
    parser.add_argument("--install", action="store_true", help="Install missing Python dependencies from requirements.txt.")
    parser.add_argument("--strict-ocr", action="store_true", help="Fail if OCR system tools are missing.")
    args = parser.parse_args(argv)

    requirements = requirements_path()
    if not requirements.exists():
        print(f"ERROR: requirements.txt not found: {requirements}", file=sys.stderr)
        return 2

    missing_packages = missing_python_packages()
    if missing_packages:
        print("Missing Python packages: " + ", ".join(missing_packages))
        if args.install:
            rc = install_requirements(requirements)
            if rc != 0:
                return rc
            missing_packages = missing_python_packages()
            if missing_packages:
                print("ERROR: Packages still missing after install: " + ", ".join(missing_packages), file=sys.stderr)
                return 1
        else:
            print(f"Run: {sys.executable} -m pip install -r {requirements}")
            return 1
    else:
        print("Python dependencies OK.")

    missing_tools = missing_system_tools()
    if missing_tools:
        for tool in missing_tools:
            print(f"WARNING: {tool} not found. {SYSTEM_TOOLS[tool]}")
        if args.strict_ocr:
            return 1
        print("OCR can still fall back to manual_review when system OCR tools are unavailable.")
    else:
        print("OCR system tools OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
