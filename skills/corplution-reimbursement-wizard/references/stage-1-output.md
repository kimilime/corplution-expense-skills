# Stage 1 Output Specification

Use this reference when generating or revising `process/invoice-extraction.md` and `process/invoice-extraction.json`.

## Inputs

- PDF invoices with selectable text.
- Scan-only PDFs.
- Image invoices such as PNG, JPG, JPEG, HEIC, TIFF.
- Support documents, especially Didi/Gaode trip reports.
- Optional user hints such as requester, reimbursement period, client, project, or known trip context.

## Output Folder

Create a `process/` folder next to the final workbook or in the user-requested output location.

- `process/invoice-extraction.md`: reviewable process file.
- `process/invoice-extraction.json`: structured handoff for later stages.
- `process/extraction-corrections.json`: durable, integrity-stamped correction and unsupported-input resolution overlay.
- `process/evidence/`: optional rendered pages or OCR crops.

## User-Facing Review List

After extraction, show the applicant a concise review list in chat. Do not ask the user to open `invoice-extraction.md/json`.

The list must identify each source file with:

- simple item number, such as `第1张`; do not expose `DOC-001` as the primary user handle
- original source filename
- document role and type, such as invoice, railway e-ticket, Didi/Gaode trip report, or unknown
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
4. Apply an extraction correction through `scripts/apply_extraction_corrections.py`, then re-run allocation and use the allocation answers updater for any downstream decision.
5. Keep correction notes/change logs instead of silently overwriting evidence.

Do not require the user to mention internal IDs such as `DOC-001` or `UNIT-001`.

`DOC-xxx` is generation-local, not a durable evidence identity. Adding, removing, converting, or reordering an input can change later document numbers. When preserving an explicit user correction for a possible clean rebuild, expand the current item number into the original source filename plus available corroborating fields such as SHA-256, invoice number, seller, amount, date, or route. Never retain only `DOC-xxx` or the displayed item number as the remembered fact.

## Unsupported Input Files

Folder uploads can contain evidence formats the extractor cannot read, such as OFD, EML, or ZIP. These files are not document records, but they are still evidence and must be persisted in top-level `unresolved_input_files` with:

- `source_file`, `filename`, `suffix`, and `sha256`
- `status`: `open`, `exclude`, or `converted`
- `resolution`, `resolved_by`, and `resolved_at` once the user decides
- `replacement_file` when status is `converted`

`open` is a hard stop for Stage 2, Stage 3, and Stage 4. Ask the user whether the file should be excluded or converted to a readable PDF/image, then save the decision through `input_resolutions` in `apply_extraction_corrections.py`. Use SHA-256 alone only when it identifies one input. For byte-identical copies, combine the shared SHA-256 with the intended copy's exact `source_file`; every selector that still matches multiple files is rejected.

Use the canonical nested `match` object below. Do not flatten `sha256` or `source_file` onto the resolution entry itself.

```json
{
  "input_resolutions": [
    {
      "match": {"sha256": "<sha256 from unresolved_input_files>"},
      "action": "exclude",
      "reason": "用户确认该文件不属于本次报销",
      "corrected_by": "user"
    },
    {
      "match": {"source_file": "original-invoice.ofd"},
      "action": "converted",
      "replacement_file": "original-invoice.pdf",
      "reason": "用户提供了可读取的 PDF 转换件",
      "corrected_by": "user"
    }
  ]
}
```

Use `sha256` when it identifies one unsupported-input content record. Use `source_file` alone only when its basename is unique in the current batch. For identical copies, use both keys and copy the exact source path from the extraction record. `input_resolutions` is only for `unresolved_input_files`. To correct or exclude an indexed document, use a `corrections` entry instead.

## Canonical Document Fields

Each source document must have:

- `document_id`
- `source_file`
- `sha256`
- `page_count`
- `document_role`: `invoice`, `supporting_schedule`, `supporting_document`, or `unknown`
- `document_subtype`
- For a `supporting_document` (payment receipt, partner approval screenshot, or other user-kept evidence): also record, via `apply_extraction_corrections.py`, a free-text `support_type` (e.g. `付款小票` / `审批截图`) and `supports_document_id` (the `document_id` of the invoice it backs). Stage 4 packages it under that invoice's proof number. A `supporting_document` with no `supports_document_id` is a hard block at Stage 3 until the user names the invoice it supports or excludes it.
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

For `railway_e_ticket`, also write `classification.railway_leg`:

- `train_no`
- `travel_date`
- `departure_time`
- `departure_datetime`
- `origin_station`
- `destination_station`
- `route`
- `is_refund_fee`
- `refund_fee_amount`

These structured fields support Stage 2 transfer-chain detection. Keep the readable railway `expense_note` as evidence and allow old extractions without `railway_leg` to fall back to parsing that note.

For a railway refund invoice, the amount printed beside `退票费` is the amount being reimbursed. Write it to both `classification.railway_leg.refund_fee_amount` and `invoice.total_amount`. Railway PDF text layers may emit the visually adjacent row as `￥63.50` followed by `退票费:` on the next extracted line; support both label-before-amount and amount-before-label orders. A blank or unresolved label is not `0`: leave the amount blank, keep `needs_review=true`, and ask the applicant to verify it.

Ordinary/special VAT invoices may still be flight evidence. Preserve line items such as `国内航空`, `国际航空`, `航空运输`, or `航空旅客运输`, plus seller and remark evidence, so Stage 2 can recognize the flight without a dedicated document subtype. Airline name is evidence, not the final reimbursement Note.

Keep `invoice.issue_date` as the formal invoice date. Do not copy it into `classification.expense_date` for ordinary invoices, meal invoices, taxi summary invoices, hotel invoices without stay dates, or `other`/`unknown` invoices. Stage 2 will ask the applicant for the actual date when it is not reliable.

Didi/Gaode trip reports additionally need:

- `report_date`
- `traveler_phone`
- `period_start`
- `period_end`
- `reported_total_amount`
- `item_count`
- `supporting_items`

Each Didi/Gaode trip item should include:

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
    "indexed_input_count": 0,
    "unresolved_input_count": 0,
    "total_invoice_amount": "0.00"
  },
  "documents": [],
  "unresolved_input_files": [],
  "document_links": [],
  "review_queue": []
}
```

## Link Rules

Create `document_links` for:

- `invoice_total_matches_didi_trip_report`
- `invoice_total_matches_gaode_trip_report`
- `invoice_supports_schedule`
- `duplicate_source_file`
- `possible_duplicate_invoice_no`

Both exact SHA-256 duplicates and repeated invoice numbers are Stage 1 review decisions. Byte-identical files have no substantive difference, so propose the first indexed copy as the canonical source instead of asking the applicant to choose between identical contents. Exclude each later copy through a `corrections` entry whose `match` combines the shared `sha256` with that copy's exact `source_file`. A SHA-only exclusion cannot identify one physical copy and is rejected atomically; Chief also requires exactly one active document in every exact-duplicate group. Repeated invoice numbers with different SHA-256 values are not exact copies and still require applicant review. Do not defer either decision to Stage 2 or drop an allocation unit: that would close a reimbursement row without resolving the source-evidence ledger.

For Didi/Gaode, link summary invoice and trip report when total amounts match. Generate later reimbursement rows from the trip items, not from the summary invoice, when a matching trip report exists.

## Completion Criteria

Stage 1 is complete only when:

- Every input file appears either in the document index or in `unresolved_input_files`.
- No `unresolved_input_files` entry remains `open`; the user has recorded an exclusion reason or a readable replacement file.
- No exact-file or invoice-number duplicate remains `needs_review`; the duplicate copy has an explicit Stage 1 keep/exclude decision.
- Every invoice-like file has extracted fields or review issues.
- Every Didi/Gaode trip report has parsed trip items and a reported total when available.
- Didi/Gaode summary invoices are linked to matching trip reports when possible.
- All amounts and dates are normalized.
- Markdown and JSON outputs agree.
- The applicant-facing extraction review list has been printed or summarized in chat.
