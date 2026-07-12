#!/usr/bin/env python3
"""Apply corrections to process/invoice-extraction.json — the ONLY sanctioned
way to reclassify or fix an extracted document.

Typical uses:
- An agent with vision looked at an image the classifier marked unknown and
  identified it as an invoice (fill the invoice fields it read).
- The user says an unknown file is a partner approval screenshot or an
  Alipay/receipt payment proof -> document_role: supporting_document.
- The user says a file is not reimbursement evidence -> action: exclude
  with their reason.

Corrections are stored in process/extraction-corrections.json and replayed
automatically whenever extract_invoices.py re-runs, so they survive
re-extraction. Do not hand-edit invoice-extraction.json; hand edits are
wiped on re-run and fail the integrity check downstream.

Usage:
  python scripts/apply_extraction_corrections.py \
      --extraction process/invoice-extraction.json \
      --corrections my-corrections.json

Corrections file format:
{
  "corrections": [
    {
      "match": {"sha256": "..."},            // or document_id / source_file
      "action": "correct",                    // or "exclude"
      "set": {
        "document_role": "invoice",
        "invoice": {"invoice_number": "...", "issue_date": "2026-05-31",
                     "seller_name": "...", "total_amount": "88.00"},
        "classification": {"expense_category": "meal"}
      },
      "reason": "agent vision: VAT invoice photo",
      "corrected_by": "agent_vision"          // or user / user_transcription
    }
  ],
  "input_resolutions": [
    {
      "match": {"sha256": "..."},
      "action": "converted",                  // or "exclude"
      "replacement_file": "converted-invoice.pdf",
      "reason": "user supplied a readable PDF replacement",
      "corrected_by": "user"
    }
  ]
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import extraction_corrections as xc
import integrity


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Apply extraction corrections via the sanctioned overlay.")
    parser.add_argument("--extraction", required=True, help="Path to process/invoice-extraction.json")
    parser.add_argument("--corrections", required=True, help="Path to a corrections JSON file (see module docstring)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview without writing")
    args = parser.parse_args(argv)

    extraction_path = Path(args.extraction)
    process_dir = extraction_path.parent
    payload = load_json(extraction_path)
    integrity.require_valid(payload, extraction_path, kind="extraction")

    incoming = load_json(Path(args.corrections))
    entries = incoming.get("corrections", [])
    input_resolutions = incoming.get("input_resolutions", [])
    if not entries and not input_resolutions:
        print("ERROR: corrections file has neither 'corrections' nor 'input_resolutions' entries.", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    for idx, entry in enumerate(entries, start=1):
        for err in xc.validate_correction(entry):
            all_errors.append(f"correction #{idx}: {err}")
    for idx, entry in enumerate(input_resolutions, start=1):
        for err in xc.validate_input_resolution(entry):
            all_errors.append(f"input resolution #{idx}: {err}")
    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        print("", file=sys.stderr)
        print("See the format example in this script's docstring. Match by sha256 when possible;", file=sys.stderr)
        print("get it from the document entry in process/invoice-extraction.md or .json.", file=sys.stderr)
        return 2

    log = xc.apply_overlay(payload, {"corrections": entries})
    log.extend(xc.apply_input_resolutions(payload, {"input_resolutions": input_resolutions}))
    for line in log:
        print(line)
    hard_errors = [line for line in log if line.startswith("ERROR:")]
    if hard_errors:
        print("", file=sys.stderr)
        print(f"ABORTED: {len(hard_errors)} correction(s) could not be applied safely — nothing was "
              "written (extraction unchanged, overlay unchanged). Fix the match keys (use sha256 "
              "from process/invoice-extraction.md) and re-run.", file=sys.stderr)
        return 2

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    # Persist entries into the durable overlay so extractor re-runs replay them.
    overlay = xc.load_overlay(process_dir)
    if entries:
        overlay["corrections"].extend(entries)
    if input_resolutions:
        overlay["input_resolutions"].extend(input_resolutions)
    xc.save_overlay(process_dir, overlay)

    integrity.stamp(payload, "apply_extraction_corrections.py")
    extraction_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {extraction_path}")
    print(f"Overlay saved: {xc.overlay_path(process_dir)} (replayed automatically on extractor re-runs)")
    print("Next: re-run scripts/allocate_expenses.py, then RECOMPOSE decisions with compose_answers.py —")
    print("item bindings may have shifted; old answers files must not be replayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
