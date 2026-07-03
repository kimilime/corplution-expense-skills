---
name: corplution-reimbursement-wizard
description: "Corplution reimbursement workflow for identifying, extracting, allocating, writing, and packaging invoice evidence from PDFs, images, trip reports, and consultant project context into auditable process files, the reimbursement Excel workbook, and a final submission package. Use when Codex needs Corplution-specific reimbursement rules: reading invoices and trip reports, matching expenses to clients and charge codes, producing the final expense table, and organizing invoice/supporting files."
---

# Corplution Reimbursement Wizard

Author: Terence Wang

## Overview

Use this skill for the Corplution reimbursement workflow:

- Stage 1: inventory source files, identify invoice and support-document types, extract canonical fields, classify the first-pass expense category, and write `process/invoice-extraction.md` plus `process/invoice-extraction.json`.
- Stage 2: read the consultant's natural-language project context, match extracted expense evidence to clients, charge codes, cities, and projects, ask targeted follow-up questions, and write `process/expense-allocation.md` plus `process/expense-allocation.json`.
- Stage 3: ask for requester, assign overall proof numbers, convert confirmed allocations into reimbursement rows, and write the final Excel workbook.
- Stage 4: package the finished workbook, renamed invoice files, and support documents into the final reimbursement submission folder.

Do not write the reimbursement Excel workbook until stage 2 has no blocking open questions. Preserve uncertain results in the review queue or allocation question queue.

## Intake Behavior

On first use in a conversation, if the user has not already provided enough invoice files and project context to start processing, read `references/opening-message.md` and send a short Chinese intake message. The message should invite invoices, trip reports, natural-language project notes, and optional special explanations, and should explain that unclear items will be confirmed through chat.

If the user already provided files or context, acknowledge what is present, ask only for missing essentials, and proceed with the relevant stage.

## Quick Start

1. In a new environment, check bundled script dependencies before running workflow scripts. The skill-local dependency file is `requirements.txt`.

```bash
python scripts/check_dependencies.py
```

If Python packages are missing, install them from the skill-local requirements file:

```bash
python scripts/check_dependencies.py --install
```

Missing OCR system tools such as Tesseract or Poppler do not block text-layer PDFs or Excel/package stages. If OCR is unavailable, stage 1 must mark scan-only inputs as `manual_review` instead of inventing fields.

If the user mainly provides scanned PDFs or images and expects OCR, run the stricter check and explain any missing system tool in chat:

```bash
python scripts/check_dependencies.py --strict-ocr
```

2. For invoice extraction, read `references/stage-1-output.md` before changing the schema, classification rules, or process-file format.
3. Run the bundled extractor when the user provides PDFs or images:

```bash
python scripts/extract_invoices.py --output process <input-file-or-folder> [...]
```

4. The extractor prints an extraction review list. Copy or summarize it directly in chat before moving to allocation, so the user can confirm recognized files by item number and source filename. Correct any `needs_review` items by inspecting the source document or rendered evidence; do not ask the user to open `process/invoice-extraction.md/json`.
5. For project allocation, read `references/stage-2-allocation.md`, then parse the user's natural-language project context and match it against `process/invoice-extraction.json`. Convert the user's natural-language project note into a temporary `project-context.json` yourself whenever enough information is present; do not ask the user to write JSON. Ask the user only for missing business facts such as date range, city, client name, or Client Charge Code.

```bash
python scripts/allocate_expenses.py --extraction process/invoice-extraction.json --context project-context.json --output process
```

The allocation script prints an applicant review list and a grouped question list. Copy or summarize both directly in the current conversation so the user can confirm or correct items by simple item number and source filename.

When the user answers allocation questions, summarize the answers into an answers JSON file and apply them with the bundled updater. Repeat until no blocking questions remain.

```bash
python scripts/apply_allocation_answers.py --allocation process/expense-allocation.json --answers process/allocation-answers.json
```

If the user says a recognized item is wrong, first trace that user-facing item number back to its source files, then ask for or apply the corrected fields.

```bash
python scripts/trace_expense_item.py --allocation process/expense-allocation.json --extraction process/invoice-extraction.json --item 9
```

6. For Excel output, read `references/stage-3-excel-output.md`, ask the user for requester if missing, and write rows from `process/expense-allocation.json`. By default, the workbook is generated directly by script using `assets/reimbursement-workbook-layout.toml` for static workbook layout and Python code for business logic, formulas, sorting, and project blocks. The legacy template remains bundled at `assets/reimbursement-template.xlsx`; pass `--template bundled` or a custom `.xlsx` path only when a template-based fallback is explicitly needed.

```bash
python scripts/write_reimbursement_template.py --allocation process/expense-allocation.json --output <filled.xlsx> --requester <name> --process-dir process
```

7. For final packaging, read `references/stage-4-package.md`, then copy and rename source files using the final proof numbers.

```bash
python scripts/package_reimbursement_files.py --final-rows process/final-expense-rows.json --extraction process/invoice-extraction.json --workbook <filled.xlsx> --output-root output
```

After packaging, copy or summarize the final package summary in chat: package folder, workbook name, invoice/support-document counts, and any unresolved package issues.

## Extraction Decision Tree

1. Prefer PDF text/table extraction for selectable electronic invoices and Didi trip reports.
2. Use rendered page images to visually verify route, amount, date, and table layout when extraction looks suspicious.
3. Use OCR only when the PDF has no usable text layer or the input is an image.
4. If no local OCR engine is available, mark `extraction_method: manual_review`, set `ocr_required: true`, and include the document in the review queue instead of inventing fields.
5. Use hybrid extraction when text exists but key fields are missing, garbled, or contradicted by the visual page.

## Classification Priorities

Classify document role before expense type:

1. Didi trip report or trip table -> `supporting_schedule/didi_trip_report`; parse one support item per ride.
2. Tax invoice markers such as invoice number, issue date, buyer/seller blocks, and total amount -> `invoice`.
3. Railway e-ticket invoice -> `invoice/railway_e_ticket`.
4. Non-invoice expense evidence -> `supporting_document`.
5. Anything unclear -> `unknown` with `needs_review: true`.

Important: identify Didi trip reports before scanning for hotel or airport keywords. A trip destination may contain a hotel name, but the document is still a trip report.

## First-Pass Categories

Use only conservative first-pass categories:

- `hotel`: lodging or hotel accommodation invoices.
- `travel`: railway, air, out-of-town Didi rides, or other travel expenses.
- `taxi`: Shanghai/local Didi rides or taxi invoices.
- `meal`: meal invoices before later trip-context overrides.
- `mobile`: telecom or mobile service invoices.
- `other`: valid invoices that do not fit the above.
- `unknown`: insufficient evidence; require review.

Didi tax invoices are summary invoices. Link them to Didi trip reports by total amount, but generate downstream rows from the trip report items when a matching trip report exists.

## Notes For Downstream Work

Stage 1 can build provisional `expense_note` from useful operational evidence:

- Railway: train number, route, travel date/time, seat/cabin.
- Didi: city, origin, destination.
- Hotel: seller or hotel name, city if inferable, quantity or nights.
- Meal: seller/restaurant and meal service.
- Mobile: phone number and billing period.

Stage 2 must normalize final notes using `references/stage-2-allocation.md`. Keep source remarks in `raw_remarks`; do not replace them with generated notes.

## Stage 2 Allocation Rules

Read `references/stage-2-allocation.md` before allocating expenses. Keep these core rules in mind:

- Treat project identity as `client_name + city + date_range + charge_code + user_description`; do not treat charge code alone as unique because many pending projects may share `CORP-2026-BD`.
- Use LLM judgment for first-pass matching, but ask the user about low-confidence or conflicting items.
- Match taxi and Didi ride items by city and date, allowing reasonable prior-day or next-day airport/station transfers.
- Match railway and flight travel by route, destination city, and travel date.
- Match hotels by city and stay date when available; otherwise use city plus nearby project period and ask when uncertain.
- Treat meals as confirmation-heavy because invoice issue date may not equal meal date; ask the user for meal date/project/attendees when needed.
- Allocate mobile expenses to `CORP-2026-ADMIN`.
- Ask about `other` expenses by default.
- Ask follow-up questions directly in the current conversation. Use `process/expense-allocation.md/json` as internal process files only; do not tell the user to inspect those files. Each question should name the relevant invoice or trip item, amount, seller/service provider, date, suggested project if any, and why confirmation is needed.
- Before asking follow-up questions, show a compact applicant review list in chat with item number, source filename, seller/provider, date, amount, category, suggested project, and status.
- Combine all uncertainties for the same item into one question block. For example, a meal with no matched project should ask for actual meal date, project/client, attendees, and note type together under the same item number.
- Use simple user-facing item numbers in conversation, such as item 1 or item 2, instead of internal IDs like `DOC-001` or `UNIT-001`. Keep internal IDs only in process JSON/Markdown for traceability.
- When a user challenges an item, run `scripts/trace_expense_item.py` and identify the source filename, invoice number, seller, amount, date, and trip details before applying corrections.
- Track substitute invoices separately, ask the user for the partner approval screenshot, append the substitute marker to the final note, and carry the substitute flag to the Excel stage.
- After receiving user answers, use `scripts/apply_allocation_answers.py` to update `expense-allocation.json` instead of manually rewriting allocation files. This preserves question status, substitute approval links, and change history.
- Generate final reimbursement notes with the required Chinese templates from the stage-2 reference, including confirmed taxi origin/destination place types; ask the user when either endpoint type is unclear.

## Stage 3 Excel Output Rules

Read `references/stage-3-excel-output.md` before writing the reimbursement workbook. Keep these core rules in mind:

- Ask the user for `Requester` if not already known.
- Write `Date` as `YYYYMMDD`.
- Use confirmed `client_name` and `client_charge_code` from stage 2.
- Set `Expense Nature` by formal invoice/location city: Shanghai means local; otherwise business trip.
- Use the confirmed stage-2 `final_note` for `Note`.
- Put each amount in exactly one template amount column: hotel, travel, taxi, meal, mobile, or other.
- Assign overall proof numbers by substantive proof order: flight/rail, hotel, taxi/Didi, Gaode, meal, mobile, other.
- Split Didi/Gaode trip reports into one row per ride, but reuse the same overall proof number for all rides supported by the same invoice.
- Write rows as project blocks; each block gets a subtotal row, then workbook-level column totals, Total, Grand Total, and Status formulas.

## Stage 4 Packaging Rules

Read `references/stage-4-package.md` before building the final file package. Keep these core rules in mind:

- Put the filled workbook in the package root as `reimbursement-application-{requester}-{date}.xlsx` using the Chinese filename defined in the reference.
- Create two folders: invoices and supporting documents.
- Rename invoice files as proof number, type, amount, and special-invoice marker when applicable.
- Rename support files as proof number and support type, such as trip report or substitute approval.
- Copy files; do not move or modify the original source files.
- End with a concise user-facing submission summary. If the package manifest has issues, list them directly in chat and ask for the missing file or decision.

## Validation Expectations

Before declaring the workflow complete:

- Every input file appears in the document index.
- Every invoice-like file has extracted fields or review issues explaining why not.
- Every Didi trip report has parsed trip items and a reported total when available.
- Didi summary invoices are linked to matching trip reports when totals match.
- Duplicate invoice numbers are flagged.
- Amounts and dates are normalized.
- Markdown and JSON outputs contain the same documents, amounts, categories, links, and review issues.
- Stage 1 extraction review list has been shown or summarized in chat when there are recognized files or items needing review.
- Stage 2 allocation has either a confirmed project/context assignment or a user-facing question for every allocation unit.
- Stage 3 output has a requester, no unconfirmed blocking items, one amount column per row, no duplicate Didi/Gaode summary rows, and totals reconcile to confirmed allocation units.
- Substitute invoice metadata and approval screenshot paths remain in `final-expense-rows.json` even though they are not written into the visible Excel rows.
- Stage 4 package contains the workbook at root plus separate invoice and support-document folders, with filenames matching final proof numbers.
