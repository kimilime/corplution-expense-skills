# Batch Allocation-Answers Helper Guide

Use this reference only when a large batch of confirmed natural-language answers makes manual editing of `allocation-answers.json` impractical.

## Boundary

A helper may generate `allocation-answers.json`. It must never modify any process file directly:

- Never write `invoice-extraction.json`, `expense-allocation.json`, `final-expense-rows.json`, or package manifests.
- Never create a replacement answers schema. Start from the current template and preserve its root object, `source_allocation_fingerprint`, and existing `unit_updates` references.
- The generated answers must still pass `apply_allocation_answers.py --dry-run` and then the normal updater.

## UTF-8-Safe Procedure

1. Generate the current template with `build_allocation_answers_template.py`.
2. Save the one-off helper as a UTF-8 Python file in the session working directory, not inside this skill's `scripts/` directory.
3. Read the template with `Path(...).read_text(encoding="utf-8-sig")`.
4. Fill only values inside existing `unit_updates` entries. Match them by the template's `unit_no`; do not reconstruct the list from scratch.
5. Keep Chinese source values in a UTF-8 JSON/text input file, or use Python Unicode escapes in a constrained inline command. Do not pass Chinese literals through PowerShell inline Python, `-Command`, or a console pipeline.
6. Write with `json.dumps(..., ensure_ascii=False, indent=2)` and `encoding="utf-8"`.
7. Run the official updater with `--dry-run`, then apply it without the flag.

Minimal pattern:

```python
# -*- coding: utf-8 -*-
import json
from pathlib import Path

template_path = Path("process/allocation-answers.template.json")
answers_path = Path("process/allocation-answers.json")
answers = json.loads(template_path.read_text(encoding="utf-8-sig"))

updates_by_no = {
    3: {"status": "confirmed", "client_name": "\u5ba2\u6237\u540d\u79f0", "final_note": "\u51fa\u5dee\u9910\u8d39"},
}
for update in answers["unit_updates"]:
    patch = updates_by_no.get(update["unit_no"])
    if patch:
        update.update(patch)

answers_path.write_text(
    json.dumps(answers, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
```

Then run:

```bash
python scripts/apply_allocation_answers.py --allocation process/expense-allocation.json --answers process/allocation-answers.json --dry-run
python scripts/apply_allocation_answers.py --allocation process/expense-allocation.json --answers process/allocation-answers.json
```

The updater rejects consecutive `??` markers, replacement characters, and common mojibake markers in editable answers. A single ASCII `?` is allowed in legitimate English free text. Stage 3 repeats the check for user-visible allocation fields and scans saved workbook cells. Treat an encoding failure exactly like a schema failure: fix the helper input, regenerate the answers file, and rerun the official path. Do not patch the allocation or workbook afterward.
