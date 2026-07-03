# Corplution Reimbursement Wizard

Author: Terence Wang

Corplution-specific Codex skill for reimbursement workflows: invoice extraction, project allocation, reimbursement workbook generation, and final file packaging.

## Skill

The skill lives at:

```text
skills/corplution-reimbursement-wizard/
```

Invoke it as:

```text
$corplution-reimbursement-wizard
```

To use the repository version inside Codex, copy or sync `skills/corplution-reimbursement-wizard/` into your Codex skills directory, usually:

```text
C:\Users\<you>\.codex\skills\corplution-reimbursement-wizard
```

After updating the repo, sync that folder again so Codex does not keep using an older installed copy.

## Workflow

1. Extract invoice and trip-report evidence into `process/invoice-extraction.md/json`.
2. Allocate expenses to Corplution projects using consultant-provided context and confirmation loops.
3. Write the reimbursement Excel workbook with project blocks, subtotals, Total, Grand Total, and Status formulas.
4. Package the filled workbook, renamed invoices, and support documents for submission.

The reimbursement workbook is generated directly by script using `skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml` for static layout such as sheet name, fonts, row heights, column widths, instruction text, headers, and sample-row styles. The legacy template is still bundled at `skills/corplution-reimbursement-wizard/assets/reimbursement-template.xlsx` and can be used as a fallback with `--template bundled`.

## Applicant Experience

The agent should not ask the applicant to open `process/*.md` or write JSON manually. It should:

1. Accept invoices, trip reports, screenshots, and natural-language project notes.
2. Convert project notes into temporary `project-context.json` internally when enough facts are present.
3. Paste or summarize the extraction review list in chat after recognition, including item number, source filename, role, type, invoice number, seller/provider, date, amount, category, and review status.
4. Paste or summarize the allocation review list in chat before asking project/allocation questions, including item number, source filename, date, amount, category, and suggested project.
5. Ask grouped questions by item number when something is uncertain.
6. Accept corrections such as "第9项金额不对" and trace the item back to its source filename before updating.
7. After writing the workbook, paste or summarize the meal daily cap check. Business-trip meals are capped at RMB 150/day and local overtime meals at RMB 60/day; if a day exceeds the relevant cap without attendee details, ask whether the date, attendees, or `reimbursable_amount` should be corrected.
8. End with a package summary: workbook, invoice count, support-document count, and unresolved issues.

## Dependencies and OCR

Install Python dependencies with:

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --install
```

For selectable electronic PDFs, Python dependencies are usually enough. For scanned PDFs or image invoices, OCR also needs system tools:

- Tesseract OCR, available on `PATH` as `tesseract`
- Poppler, available on `PATH` as `pdftoppm`, for rendering scan-only PDFs before OCR

Check OCR readiness with:

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --strict-ocr
```

If OCR tools are missing, the extractor should mark scan-only/image inputs as `manual_review` and ask for confirmation instead of inventing invoice fields.

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/extract_invoices.py --output process <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/allocate_expenses.py \
  --extraction process/invoice-extraction.json \
  --context project-context.json \
  --output process

python skills/corplution-reimbursement-wizard/scripts/apply_allocation_answers.py \
  --allocation process/expense-allocation.json \
  --answers process/allocation-answers.json

python skills/corplution-reimbursement-wizard/scripts/trace_expense_item.py \
  --allocation process/expense-allocation.json \
  --extraction process/invoice-extraction.json \
  --item 9

python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --process-dir process

# Optional: override generated-workbook layout
python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --layout skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml \
  --process-dir process

python skills/corplution-reimbursement-wizard/scripts/package_reimbursement_files.py \
  --final-rows process/final-expense-rows.json \
  --extraction process/invoice-extraction.json \
  --workbook <filled.xlsx> \
  --output-root output
```

## Notes

This repository is intended for Corplution internal reimbursement rules. It should usually be kept private because reimbursement materials may contain personal, client, and invoice information.
