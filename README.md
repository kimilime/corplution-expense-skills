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
5. Treat invoice issue dates as evidence only, except pure `other` expenses may temporarily use invoice issue date with a non-blocking advisory. Ask for the actual occurrence/record date when the date is not reliable: printed flight/rail date, hotel stay dates, Didi/Gaode trip-report time, or mobile month-end.
6. Ask grouped questions by expense type when many items need the same kind of answer. For example, combine meal questions into one list and let the applicant answer by item numbers and dates instead of asking every invoice separately.
7. Auto-match by expense type rather than by one generic city rule: hotels use stay/city evidence, meals use explicit notes or unique non-Shanghai city, taxi rides use journey/transfer logic, flight/rail use destination/date, and `other` is not pre-matched by issuer city.
8. Accept corrections such as "第9项金额不对" and trace the item back to its source filename before updating.
9. For `CORP-2026-ADMIN` rows, use `通讯费` as the Client for mobile expenses. For other admin expenses, use the specific matter name when known; otherwise use `项目、调研以外的其他费用` and paste a non-blocking chat prompt asking whether the applicant wants a more specific matter such as 年会、半年会、客户会、行业协会会议.
10. After writing the workbook, paste or summarize the meal daily cap check. Business-trip meals are capped at RMB 150/day and local overtime meals at RMB 60/day; over-cap days with attendee details are advisory, while over-cap days without attendee details require confirmation or a `reimbursable_amount` correction.
11. Paste or summarize the hotel cap check. Beijing/Shanghai/Guangzhou/Shenzhen hotels are capped at RMB 800/night and other cities at RMB 600/night; over-cap hotels with shared-room/co-occupant details are advisory, while missing nights or over-cap hotels without shared-room details require confirmation or a `reimbursable_amount` adjustment.
12. End with a package summary: workbook, invoice count, support-document count, and unresolved issues.

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
