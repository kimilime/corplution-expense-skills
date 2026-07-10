---
name: corplution-reimbursement-wizard
description: "Corplution reimbursement workflow for identifying, extracting, allocating, writing, and packaging invoice evidence from PDFs, images, trip reports, and consultant project context into auditable process files, the reimbursement Excel workbook, and a final submission package. Use whenever the user asks for help with Corplution reimbursement or expense claims — including Chinese requests such as 报销、贴票、整理发票、填报销单、发票整理、滴滴/高德行程单、差旅费用报销 — or uploads invoices, trip reports, or travel receipts to be matched to clients and charge codes and turned into the reimbursement workbook and submission package."
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

Company policy numbers (meal/hotel caps, first-tier cities) and year-coded charge codes live in `assets/policy.toml`; when policy or fiscal year changes, edit that one file. Process JSONs are integrity-stamped: `invoice-extraction.json` may only be changed via `apply_extraction_corrections.py`, `expense-allocation.json` only via the answers template + `apply_allocation_answers.py`, and `final-expense-rows.json` only by re-running Stage 3. Any other edit makes the next script exit with code `4` and print recovery steps — follow them instead of patching further.

Every input file is reimbursement evidence until the user explicitly says to drop it. Never skip or ignore a file because it cannot be read automatically — an unidentified file may be an invoice that needs OCR or visual reading, a partner approval screenshot, or a payment proof (paper receipt, Alipay/WeChat screenshot). Unidentified files become blocking questions; ask the user what each one is, and record an explicit exclusion (with the user's reason) for anything dropped.

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

Every stage script prints a `NEXT:` line on completion telling you the required next action — always follow it. At any point, and ALWAYS when the user asks about progress or status, run `python scripts/check_workflow_status.py` and relay its output; it reports per-stage completion, unresolved items, and stale artifacts (e.g., a workbook generated before later allocation edits — packaging refuses stale workbooks and unresolved cap checks).

Missing OCR system tools such as Tesseract or Poppler do not block text-layer PDFs or Excel/package stages. If OCR is unavailable, stage 1 must mark scan-only inputs as `manual_review` instead of inventing fields.

If the user mainly provides scanned PDFs or images and expects OCR, run the stricter check and explain any missing system tool in chat:

```bash
python scripts/check_dependencies.py --strict-ocr
```

2. For invoice extraction, read `references/stage-1-output.md` before changing the schema, classification rules, or process-file format.
3. Run the bundled extractor when the user provides PDFs or images. Pass the whole upload folder (or every provided file) in one call — never pre-filter by which files look readable:

```bash
python scripts/extract_invoices.py --output process <input-file-or-folder> [...]
```

4. The extractor prints an `INPUT RECONCILIATION` block followed by an extraction review list, and writes the same review to `process/invoice-extraction.md` in UTF-8. Copy or summarize it directly in chat before moving to allocation, so the user can confirm recognized files by item number and source filename. Unsupported files such as OFD/eml are persisted in `unresolved_input_files` with a SHA-256 and block later stages until the user explicitly excludes them (with a reason) or provides a readable replacement through `input_resolutions` in `scripts/apply_extraction_corrections.py`. If terminal output is garbled or truncated, read the UTF-8 Markdown process file instead of writing a one-off extraction helper script. Correct any `needs_review` items through that same script — read the file visually if you can and record what you saw; otherwise ask the user what it is using the template the extractor prints. Never hand-edit `invoice-extraction.json`; corrections and input resolutions persist in `process/extraction-corrections.json` and replay automatically on every re-run. Do not ask the user to open `process/invoice-extraction.md/json`.
5. For project allocation, read `references/stage-2-allocation.md`, then parse the user's natural-language project context and match it against `process/invoice-extraction.json`. Convert the user's natural-language project note into a temporary `project-context.json` yourself whenever enough information is present; do not ask the user to write JSON. Ask the user only for missing business facts such as date range, city, client name, or Client Charge Code.

```bash
python scripts/allocate_expenses.py --extraction process/invoice-extraction.json --context project-context.json --output process
```

Allocation refuses to run while unsupported inputs remain open, and records the exact extraction fingerprint it used. If extraction is rerun later, regenerate allocation and its answers template rather than reusing the old allocation.

The allocation script prints an applicant review list, then a ready-to-send 转发块 containing all blocking questions in Chinese. Relay the 转发块 VERBATIM as your next chat message — do not summarize, shorten, or end your turn without sending it — then wait for the user's answers. If terminal output is garbled, read the Markdown process file instead of creating temporary print/extraction scripts.

Before translating the user's natural-language answers into JSON, generate a current-task answers template. Fill the generated canonical `unit_updates` entries; do not invent another schema such as `answers[].allocations`.

```bash
python scripts/build_allocation_answers_template.py --allocation process/expense-allocation.json --output process/allocation-answers.template.json
```

When the user answers allocation questions, fill the template into `process/allocation-answers.json`, validate it with the bundled updater, then apply it. Repeat until no blocking questions remain. Do not create ad hoc patch scripts to mutate `expense-allocation.json`; the updater refreshes notes, closes questions, preserves change history, and runs accounting checks. After ANY allocation re-run, regenerate the template before applying — the updater verifies the answers file was built against the current allocation generation and rejects stale ones, because unit ids may have shifted and replaying old answers would write data onto the wrong units.

Writing a helper script to GENERATE the answers file is allowed and even recommended for large batches (it is less error-prone than hand-writing JSON for dozens of units), as long as the result still goes through build-template + apply as usual. But helper scripts are one-off work products: save them in the session working directory, NEVER inside this skill's folder — a batch-specific script (with hardcoded project/city tables) left in `scripts/` will be mistaken for a bundled tool by future sessions and applied to the wrong batch. Direct mutation of process JSONs by helper scripts remains forbidden regardless of where the script lives.

```bash
python scripts/apply_allocation_answers.py --allocation process/expense-allocation.json --answers process/allocation-answers.json --dry-run
```

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

Stage 3 verifies that allocation still belongs to the current extraction generation and that no unsupported input remains unresolved. A mismatch requires a fresh Stage 2 run, not a manual repair.

7. For final packaging, read `references/stage-4-package.md`, then copy and rename source files using the final proof numbers.

```bash
python scripts/package_reimbursement_files.py --final-rows process/final-expense-rows.json --extraction process/invoice-extraction.json --workbook <filled.xlsx> --output-root output
```

After packaging, copy or summarize the final package summary in chat: package folder, workbook name, invoice/support-document counts, and any unresolved package issues.

If packaging exits with code `3`, it created a review package with blocking missing-file or approval issues. Do not call it complete or submit it; show the issues in chat, resolve them, then re-run Stage 4.

## Extraction Decision Tree

1. Prefer PDF text/table extraction for selectable electronic invoices and Didi/Gaode trip reports.
2. If the agent has image understanding, read rendered pages/images directly — the keyword classifier has low recall on photos, so an agent with vision is the better classifier for images; record findings via `apply_extraction_corrections.py`. Agents without vision rely on OCR text plus asking the user; never guess fields from filenames.
3. Use OCR only when the PDF has no usable text layer or the input is an image.
4. If no local OCR engine is available and the agent cannot read the file visually, mark `extraction_method: manual_review`, set `ocr_required: true`, and resolve by asking the user (the extractor prints a ready-to-send Chinese question template listing the affected files) — never invent fields, and never drop the file.
5. Use hybrid extraction when text exists but key fields are missing, garbled, or contradicted by the visual page.

## Classification Priorities

Classify document role before expense type:

1. Didi/Gaode trip report or trip table -> `supporting_schedule/didi_trip_report` or `supporting_schedule/gaode_trip_report`; parse one support item per ride.
2. Tax invoice markers such as invoice number, issue date, buyer/seller blocks, and total amount -> `invoice`.
3. Railway e-ticket invoice -> `invoice/railway_e_ticket`.
4. Non-invoice expense evidence -> `supporting_document`.
5. Anything unclear -> `unknown` with `needs_review: true`.

Important: identify Didi/Gaode trip reports before scanning for hotel or airport keywords. A trip destination may contain a hotel name, but the document is still a trip report.

## First-Pass Categories

Use only conservative first-pass categories:

- `hotel`: lodging or hotel accommodation invoices.
- `travel`: railway, air, out-of-town Didi/Gaode rides, or other travel expenses.
- `taxi`: Shanghai/local Didi/Gaode rides or taxi invoices.
- `meal`: meal invoices before later trip-context overrides.
- `mobile`: telecom or mobile service invoices.
- `other`: valid invoices that do not fit the above.
- `unknown`: insufficient evidence; require review.

Didi/Gaode tax invoices are summary invoices. Link them to trip reports by total amount, but generate downstream rows from the trip report items when a matching trip report exists.

## Notes For Downstream Work

Stage 1 can build provisional `expense_note` from useful operational evidence:

- Railway: train number, route, travel date/time, seat/cabin.
- Didi/Gaode: city, origin, destination.
- Hotel: seller or hotel name, city if inferable, quantity or nights.
- Meal: seller/restaurant and meal service.
- Mobile: phone number and billing period.

Stage 2 must normalize final notes using `references/stage-2-allocation.md`. Keep source remarks in `raw_remarks`; do not replace them with generated notes.

## Stage 2 Allocation Rules

Read `references/stage-2-allocation.md` before allocating expenses. Keep these core rules in mind:

- Treat project identity as `client_name + city + date_range + charge_code + user_description`; do not treat charge code alone as unique because many pending projects may share `CORP-2026-BD`.
- Use LLM judgment for first-pass matching, but ask the user about low-confidence or conflicting items.
- Treat `invoice.issue_date` as evidence, not a default occurrence date. Reliable occurrence dates are: printed flight/rail travel date, printed hotel check-in/check-out dates, Didi/Gaode ride datetime from a trip report, and mobile month-end from the billing period or invoice month. For pure `other` expenses, you may temporarily use `invoice.issue_date` as `expense_date`, but mark it provisional and show a non-blocking advisory for user review.
- Exclude `CORP-2026-ADMIN` contexts from hotel/meal/taxi/travel automatic city/date scoring. Admin is not a Shanghai project and must not win fallback matching.
- Match hotels first by hotel city plus stay dates; when stay dates or project dates are missing, use city uniqueness only for project pre-allocation, and still ask for missing nights/check-in/check-out needed for hotel caps.
- Match meals by explicit user-provided meal notes when available. Parse notes such as `6.1 德克士 61.8` into meal hints, then match by combined amount/date/merchant evidence instead of any single strict field. Otherwise treat invoice dates as unreliable and auto-assign only when a non-Shanghai invoice city has exactly one project in the period. Show inferred meals as advisory so the user can batch-correct dates/attendees/amounts.
- For meal amount columns and `Expense Nature`, apply form over substance: Shanghai invoice/restaurant city -> `meal`/local, non-Shanghai invoice/restaurant city -> `travel`/business trip, regardless of which project the meal is allocated to. This changes only the visible amount column: the unit keeps `source_category: meal` (the writer script assigns the column automatically, and the daily meal cap check only sees category=meal — the updater refuses meal->travel category changes unless marked as a manual extraction correction).
- Match taxi and Didi/Gaode ride items by the project journey they support: city/date for ordinary rides, airport/station transfer to the upcoming destination project, and project-to-project station/airport transfers to the project being traveled to.
- Treat Shanghai/local projects conservatively. A local project such as KEEWAY must not receive Shanghai taxi/travel items merely because city and date match. Auto-assign a Shanghai local project only when the ride endpoint, route note, user note, or explicit project keyword names that local client/project; otherwise station/airport transfers inherit the adjacent out-of-town travel project or remain a blocking question.
- For taxi/Didi/Gaode amount columns, use form over substance by ride city: Shanghai rides stay in `taxi` even when allocated to an out-of-town project; non-Shanghai rides go to `travel`.
- Match railway and flight travel by route destination and travel date with a reasonable +/- 1 day project buffer.
- When travel connects two project cities, assign it to the destination/project being traveled to, not the origin project. Never override this merely because the origin station city matches a previous project.
- Do not pre-match `other` or `unknown` by invoice city. Ask the user; invoice issuer city can be misleading for SaaS, online meetings, associations, and other services.
- Allocate mobile expenses to `CORP-2026-ADMIN` with `client_name = 通讯费`, not `Admin`; fill Date as that month's last day.
- Never use `CORP-2026-ADMIN`, `通讯费`, or the mobile amount column as a fallback for unmatched taxi/travel/meal/hotel expenses. Unmatched transport remains a blocking question unless a transfer/travel rule matches it to a project.
- For other `CORP-2026-ADMIN` expenses, use a specific matter name as `client_name` when known, such as `年会`, `半年会`, `客户会`, or `行业协会会议`; if missing, use `项目、调研以外的其他费用` and show a non-blocking chat prompt so the applicant can refine it.
- Ask about `other` and `unknown` expenses by default. For `other`, project/note/accounting treatment may still be blocking, but the date can temporarily use the invoice date with an advisory. For `unknown`, ask for the actual date unless the user reclassifies it as pure `other`.
- Ask follow-up questions directly in the current conversation. Use `process/expense-allocation.md/json` as internal process files only; do not tell the user to inspect those files. Group repetitive uncertainties by expense type, such as one meal batch question listing all meal item numbers, files, invoice numbers, dates, amounts, and suggested projects.
- If the user gives meal details in natural language, including "with X", "和X一起", "同事X", or dining counterparties, capture them into `attendees` even when the daily meal cap is not exceeded. Do not rely on the cap check as the only attendee collection point.
- Before asking follow-up questions, show a compact applicant review list in chat with item number, source filename, seller/provider, date, amount, category, suggested project, and status.
- Combine all uncertainties for the same item into one question block, then batch same-type items into one grouped question whenever practical. For example, ask meal details once for items 1/3/5/7 instead of repeating the same question four times.
- Use simple user-facing item numbers in conversation, such as item 1 or item 2, instead of internal IDs like `DOC-001` or `UNIT-001`. Keep internal IDs only in process JSON/Markdown for traceability.
- When a user challenges an item, run `scripts/trace_expense_item.py` and identify the source filename, invoice number, seller, amount, date, and trip details before applying corrections.
- Track substitute invoices separately, ask the user for the partner approval screenshot, append the substitute marker to the final note, and carry the substitute flag to the Excel stage.
- After receiving user answers, generate `allocation-answers.template.json` with `scripts/build_allocation_answers_template.py`, fill it into `allocation-answers.json`, and use `scripts/apply_allocation_answers.py` to update `expense-allocation.json` instead of manually rewriting allocation files. This preserves question status, substitute approval links, and change history.
- Never create temporary patch scripts for bulk allocation edits. Convert batch natural-language answers into the generated canonical `unit_updates` template and run the updater even when the JSON is long.
- Generate final reimbursement notes with the required Chinese templates from the stage-2 reference, including confirmed taxi origin/destination place types. Never write literal placeholders such as `出发地类型` or `目的地类型` into `final_note`; ask the user when either endpoint type is unclear.
- Mark rail/flight cancellation or refund evidence in the final note as `高铁退票费（出发地-目的地）` or `飞机退票费（出发地-目的地）` instead of the ordinary travel note.

## Stage 3 Excel Output Rules

Read `references/stage-3-excel-output.md` before writing the reimbursement workbook. Keep these core rules in mind:

- Ask the user for `Requester` if not already known.
- Write `Date` as `YYYYMMDD`.
- Use only confirmed, reliable, or explicitly provisional `other` `expense_date`; if `date_required` is true or `expense_date` is blank, ask in chat before writing the workbook.
- Use confirmed `client_name` and `client_charge_code` from stage 2.
- Set `Expense Nature` by the formal amount-column evidence, not by assigned project: meal uses invoice/restaurant city; taxi/Didi/Gaode ride rows use ride city. Shanghai formal city means local; non-Shanghai formal city means business trip.
- Use the confirmed stage-2 `final_note` for `Note`.
- Put each amount in exactly one template amount column: hotel, travel, taxi, meal, mobile, or other.
- For meal rows, recompute the visible amount column by formal invoice/restaurant city before writing: Shanghai -> `meal`; non-Shanghai -> `travel`.
- For taxi/Didi/Gaode ride rows, recompute the visible amount column by ride city before writing: Shanghai -> `taxi`; non-Shanghai -> `travel`. Do not change this merely because the ride is allocated to an out-of-town project.
- For meal expenses with daily standards, apply the cap after rows are built: business-trip meals are RMB 150/day, local overtime meals are RMB 60/day. Show `meal_daily_cap_checks` in chat. If a date exceeds the relevant cap without attendee details, ask whether the meal date is wrong, attendees are missing, or one item should use a lower `reimbursable_amount`; if attendee details exist, treat the over-cap result as advisory only. If reimbursable amount differs from invoice amount, the final note must state `发票金额XX/实际报销XX`.
- For hotel expenses, apply the per-night cap after rows are built: Beijing/Shanghai/Guangzhou/Shenzhen are RMB 800/night, other cities are RMB 600/night. Show `hotel_cap_checks` in chat. If nights or city tier are missing, ask for check-in/check-out/nights/city. If a hotel exceeds the relevant cap with shared-room/co-occupant details, treat it as advisory only; otherwise ask whether one item should use a lower `reimbursable_amount`.
- Hotel final notes must not keep placeholders such as `X晚`, `入住日`, or `离店日`. If hotel nights/check-in/check-out are known, the scripts regenerate `出差酒店（X晚，入住日-离店日）` with actual values; if those fields are missing, Stage 3 preflight blocks workbook generation.
- Always show or summarize `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT`. If the writer exits with code `2`, no workbook was written because allocation is not structurally ready: open questions, invalid categories/columns, missing dates/client/code/amount, admin/mobile conflicts, raw ticket notes, or missing taxi place types must be fixed first.
- If any stage script exits with code `4`, a process JSON failed its integrity check (modified outside the sanctioned flow); follow the printed recovery steps and do not patch further.
- If `write_reimbursement_template.py` exits with code `3`, the workbook and final row files were written, but the `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` block contains blocking meal/hotel policy checks that must be shown to the applicant and resolved before final submission.
- Assign overall proof numbers by substantive proof order: flight/rail, hotel, taxi/Didi, Gaode, meal, mobile, other.
- Split Didi/Gaode trip reports into one row per ride, but reuse the same overall proof number for all rides supported by the same invoice.
- Write rows as project blocks; each block gets a subtotal row, then workbook-level column totals, Total, Grand Total, and Status formulas.

## Stage 4 Packaging Rules

Read `references/stage-4-package.md` before building the final file package. Keep these core rules in mind:

- Put the filled workbook in the package root as `reimbursement-application-{requester}-{date}.xlsx` using the Chinese filename defined in the reference.
- Create two folders: invoices and supporting documents.
- Rename invoice files as proof number, type, amount, and special-invoice marker when applicable.
- Rename support files as proof number and support type, such as trip report or substitute approval.
- If multiple support files would have the same name, retain every file by adding deterministic `-2`, `-3` suffixes; never overwrite evidence.
- Copy files; do not move or modify the original source files.
- Build a fresh staging package and replace the prior package root only after all files and the stamped manifest are ready. A rerun must not retain files that are absent from the new manifest.
- End with a concise user-facing submission summary only when the package manifest has no issues. If it has issues, list them directly in chat as blocking items, ask for the missing file or decision, and re-run Stage 4 after resolution.

## Validation Expectations

Before declaring the workflow complete:

- Every input file appears either in the document index or the persisted unsupported-input list, and the extractor's `INPUT RECONCILIATION` plus allocation's `DOCUMENT RECONCILIATION` blocks were shown in chat with no unaccounted documents.
- No unsupported input remains `open`; every such file has a user-recorded exclusion reason or a readable replacement path before allocation.
- Every unidentified document was resolved through chat + `apply_extraction_corrections.py` (identified as invoice / approval screenshot / payment proof / other) or explicitly excluded with the user's reason — none left in limbo.
- Every invoice-like file has extracted fields or review issues explaining why not.
- Every Didi/Gaode trip report has parsed trip items and a reported total when available.
- Didi/Gaode summary invoices are linked to matching trip reports when totals match.
- Duplicate invoice numbers are flagged.
- Amounts and dates are normalized.
- Markdown and JSON outputs contain the same documents, amounts, categories, links, and review issues.
- Stage 1 extraction review list has been shown or summarized in chat when there are recognized files or items needing review.
- Stage 2 allocation has either a confirmed project/context assignment or a user-facing question for every allocation unit.
- Allocation, final rows, workbook, and package manifest belong to the same current extraction/allocation generations; a stale generation is regenerated rather than patched.
- Stage 3 output has a requester, no unconfirmed blocking items, one amount column per row, no duplicate Didi/Gaode summary rows, meal and hotel cap checks shown in chat, and totals reconcile to confirmed allocation units.
- Substitute invoice metadata and approval screenshot paths remain in `final-expense-rows.json` even though they are not written into the visible Excel rows.
- Stage 4 package has a stamped manifest bound to the current final-rows fingerprint and workbook hash; every listed invoice/support file exists and matches its manifest hash, and the manifest has no unresolved issues.
