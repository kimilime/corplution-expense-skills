# Stage 1 Output Specification

Use this reference when generating or revising `process/invoice-extraction.md` and `process/invoice-extraction.json`.

## Inputs

- PDF invoices with selectable text.
- Scan-only PDFs.
- Image invoices such as PNG, JPG, JPEG, HEIC, TIFF.
- Support documents, especially Didi trip reports.
- Optional user hints such as requester, reimbursement period, client, project, or known trip context.

## Output Folder

Create a `process/` folder next to the final workbook or in the user-requested output location.

- `process/invoice-extraction.md`: reviewable process file.
- `process/invoice-extraction.json`: structured handoff for later stages.
- `process/evidence/`: optional rendered pages or OCR crops.

## User-Facing Review List

After extraction, show the applicant a concise review list in chat. Do not ask the user to open `invoice-extraction.md/json`.

The list must identify each source file with:

- simple item number, such as `第1张`; do not expose `DOC-001` as the primary user handle
- original source filename
- document role and type, such as invoice, railway e-ticket, Didi trip report, or unknown
- invoice number when available
- seller/service provider when available
- date, amount, first-pass category, and review status
- a short reason when the item needs review

If the user says an item is wrong, use the simple item number and source filename to trace the file and correct the downstream allocation fields.

## Manual Correction And Traceability

Recognition errors are expected. Preserve enough source information for every extracted document so later stages can answer "which file is item N from?" without ambiguity.

When the user reports that an item is wrong:

1. Trace the user-facing item number back to `source_file`, invoice number, seller/service provider, amount, date, and any supporting trip item.
2. Inspect the source file or rendered evidence when needed.
3. Ask the user for the corrected value in natural language.
4. Apply corrected downstream fields through `process/allocation-answers.json` and `scripts/apply_allocation_answers.py`.
5. Keep correction notes/change logs instead of silently overwriting evidence.

Do not require the user to mention internal IDs such as `DOC-001` or `UNIT-001`.

## Canonical Document Fields

Each source document must have:

- `document_id`
- `source_file`
- `sha256`
- `page_count`
- `document_role`: `invoice`, `supporting_schedule`, `supporting_document`, or `unknown`
- `document_subtype`
- `extraction_method`: `text_layer`, `ocr`, `hybrid`, or `manual_review`
- `ocr_required`
- `confidence`
- `needs_review`
- `issues`

Invoice fields:

- `invoice_no`
- `invoice_type`: `ordinary`, `special`, `railway_e_ticket`, or `unknown`
- `issue_date`
- `buyer_name`
- `buyer_tax_id`
- `seller_name`
- `seller_tax_id`
- `line_item_name`
- `amount_without_tax`
- `tax_amount`
- `total_amount`
- `currency`
- `raw_remarks`

Classification fields:

- `expense_category`: `hotel`, `travel`, `taxi`, `meal`, `mobile`, `other`, or `unknown`
- `expense_date`: reliable occurrence date only, not the invoice issue date by default
- `expense_date_source`: source of `expense_date`, such as `railway_travel_date`, `trip_report_period_start`, or blank when no reliable occurrence date was extracted
- `expense_note`
- `reason`

Keep `invoice.issue_date` as the formal invoice date. Do not copy it into `classification.expense_date` for ordinary invoices, meal invoices, taxi summary invoices, hotel invoices without stay dates, or `other`/`unknown` invoices. Stage 2 will ask the applicant for the actual date when it is not reliable.

Didi trip reports additionally need:

- `report_date`
- `traveler_phone`
- `period_start`
- `period_end`
- `reported_total_amount`
- `item_count`
- `supporting_items`

Each Didi trip item should include:

- `item_id`
- `ride_datetime`
- `city`
- `vehicle_type`
- `origin`
- `destination`
- `distance_km`
- `amount`
- `expense_category`
- `expense_note`

## OCR Routing

Use these routes:

- `text_layer`: PDF text and tables are selectable and complete.
- `ocr`: OCR is used as the primary source.
- `hybrid`: text extraction exists but OCR/visual checks are needed for missing or garbled fields.
- `manual_review`: OCR is required but unavailable or the result is too uncertain.

When OCR is required, record engine availability, confidence if available, and unresolved fields in `issues`.

## Markdown Format

```markdown
# Invoice Extraction Process

Generated at: YYYY-MM-DD HH:mm
Input files: N
Documents needing review: N

## Batch Summary

| Metric | Value |
| --- | ---: |
| Invoice documents | 0 |
| Supporting schedules | 0 |
| Unknown documents | 0 |
| Total invoice amount | 0.00 |

## Document Index

| ID | File | Role | Subtype | Category | Amount | Date | Needs Review |
| --- | --- | --- | --- | --- | ---: | --- | --- |

## Extracted Documents

### DOC-001 - original-file-name.pdf

- Role:
- Subtype:
- Extraction method:
- OCR required:
- Invoice no:
- Invoice type:
- Issue date:
- Buyer:
- Seller:
- Amount:
- Category:
- Expense note:
- Raw remarks:
- Confidence:
- Issues:
- Evidence:

#### Supporting Items

| Item ID | Date/Time | City | Origin | Destination | Amount | Note |
| --- | --- | --- | --- | --- | ---: | --- |

## Document Links

| Source | Target | Relation | Check |
| --- | --- | --- | --- |

## Review Queue

| ID | Field | Problem | Suggested Action |
| --- | --- | --- | --- |
```

## JSON Schema Shape

```json
{
  "schema_version": "invoice_extraction.v1",
  "generated_at": "YYYY-MM-DDTHH:mm:ss",
  "batch": {
    "input_count": 0,
    "invoice_count": 0,
    "supporting_schedule_count": 0,
    "unknown_count": 0,
    "review_count": 0,
    "total_invoice_amount": "0.00"
  },
  "documents": [],
  "document_links": [],
  "review_queue": []
}
```

## Link Rules

Create `document_links` for:

- `invoice_total_matches_didi_trip_report`
- `invoice_supports_schedule`
- `duplicate_source_file`
- `possible_duplicate_invoice_no`

For Didi, link summary invoice and trip report when total amounts match. Generate later reimbursement rows from the trip items, not from the summary invoice, when a matching trip report exists.

## Completion Criteria

Stage 1 is complete only when:

- Every input file appears in the document index.
- Every invoice-like file has extracted fields or review issues.
- Every Didi trip report has parsed trip items and a reported total when available.
- Didi summary invoices are linked to matching trip reports when possible.
- All amounts and dates are normalized.
- Markdown and JSON outputs agree.
- The applicant-facing extraction review list has been printed or summarized in chat.
