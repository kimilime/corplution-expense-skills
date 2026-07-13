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

Company policy numbers (meal/hotel caps, first-tier cities) and year-coded charge codes live in `assets/policy.toml`; when policy or fiscal year changes, edit that one file. Process JSONs are integrity-stamped: `invoice-extraction.json` may only be changed via `apply_extraction_corrections.py`, `expense-allocation.json` only via Composer-generated answers + `apply_allocation_answers.py`, and `final-expense-rows.json` only by re-running Stage 3. Any other edit makes the next script exit with code `4` and print recovery steps — follow them instead of patching further.

Every input file is reimbursement evidence until the user explicitly says to drop it. Never skip or ignore a file because it cannot be read automatically — an unidentified file may be an invoice that needs OCR or visual reading, a partner approval screenshot, or a payment proof (paper receipt, Alipay/WeChat screenshot). Unidentified files become blocking questions; ask the user what each one is, and record an explicit exclusion (with the user's reason) for anything dropped.

## Intake Behavior

On first use in a conversation, if the user has not already provided enough invoice files and project context to start processing, read `references/opening-message.md` and send a short Chinese intake message. The message should invite invoices, trip reports, natural-language project notes, and optional special explanations, and should explain that unclear items will be confirmed through chat.

If the user already provided files or context, acknowledge what is present, ask only for missing essentials, and proceed with the relevant stage.

## Quick Start

Resolve `SKILL_ROOT` as the directory containing this `SKILL.md`. Command examples below are relative to `SKILL_ROOT`. When the task working directory is elsewhere, invoke the exact absolute path `<SKILL_ROOT>/scripts/chief_orchestrator.py`. Never create `run_chief.py`, modify `sys.path`, import `chief_orchestrator`, copy it into the task, or call `chief_orchestrator.main()`; Chief rejects wrapper launchers and prints the correct direct command.

1. In a new environment, check bundled script dependencies before running workflow scripts. The skill-local dependency file is `requirements.txt`.

```bash
python scripts/chief_orchestrator.py run dependencies
```

If Python packages are missing, install them from the skill-local requirements file:

```bash
python scripts/chief_orchestrator.py run dependencies --install
```

Use the bundled `scripts/chief_orchestrator.py` as the default workflow entry. It fills canonical process paths, dispatches the existing scripts without duplicating their business logic, preserves the child exit code, and prints one authoritative `CHIEF NEXT` result after every run. Direct script calls remain supported for developer debugging only; they are not journaled and are not a workaround for a failed Chief command.

```bash
python scripts/chief_orchestrator.py status
python scripts/chief_orchestrator.py next
python scripts/chief_orchestrator.py next --json
```

At any point, and ALWAYS when the user asks about progress or status, run the Chief `status` command and relay its output. `next` returns exactly one of `command`, `needs_user`, `blocked`, or `complete`; it emits executable `argv` only when all required parameters are known. Never turn a `needs_user` result into a guessed command. The legacy `python scripts/check_workflow_status.py` command uses the same shared state engine and remains compatible.

If a workflow command fails or the recovery path is unclear, read `references/troubleshooting.md`, run Chief `status`, and follow the one current `CHIEF NEXT` action. Do not improvise a helper, patch a process JSON, or bypass a failed stage.

Runs dispatched by Chief append privacy-minimized events to `process/workflow-journal.jsonl`. The journal records stage/script, timestamps, exit code, duration, artifact hashes, and counts only; it must not contain raw invoice text, applicant answers, client details, full command arguments, or source paths. It is observational rather than an integrity authority, never enters the final package, and a journal-write failure must not replace the underlying script's exit code.

Missing OCR system tools such as Tesseract or Poppler do not block text-layer PDFs or Excel/package stages. If OCR is unavailable, stage 1 must mark scan-only inputs as `manual_review` instead of inventing fields.

If the user mainly provides scanned PDFs or images and expects OCR, run the stricter check and explain any missing system tool in chat:

```bash
python scripts/chief_orchestrator.py run dependencies --strict-ocr
```

2. For invoice extraction, read `references/stage-1-output.md` before changing the schema, classification rules, or process-file format.
3. Run the bundled extractor when the user provides PDFs or images. Pass the whole upload/evidence folder (or every provided file) in one call — never pre-filter by which files look readable. Do not pass a task root that contains `process`, `output`, or the workflow journal; Chief rejects that overlap so generated artifacts cannot be mistaken for newly supplied evidence.

```bash
python scripts/chief_orchestrator.py run extract <input-file-or-folder> [...]
```

4. The extractor prints an `INPUT RECONCILIATION` block followed by an extraction review list, and writes the same review to `process/invoice-extraction.md` in UTF-8. Copy or summarize it directly in chat before moving to allocation, so the user can confirm recognized files by item number and source filename. Unsupported files such as OFD/eml are persisted in `unresolved_input_files` with a SHA-256 and block later stages until the user explicitly excludes them (with a reason) or provides a readable replacement through `input_resolutions`. Exact-file and invoice-number duplicates also remain Stage 1 review items: keep one source and record the user's exclusion against the duplicate through `correct-extraction`; dropping a later allocation unit does not resolve the evidence ledger. Apply a prepared corrections/resolutions file with `python scripts/chief_orchestrator.py run correct-extraction --corrections <corrections.json>`. If terminal output is garbled or truncated, read the UTF-8 Markdown process file instead of writing a one-off extraction helper script. Read a file visually when possible and record what you saw; otherwise ask the user using the extractor's question template. Never hand-edit `invoice-extraction.json`; corrections persist in `process/extraction-corrections.json` and replay automatically on every re-run. Do not ask the user to open `process/invoice-extraction.md/json`.
5. For project allocation, read `references/stage-2-allocation.md`, then parse the user's natural-language project context and match it against `process/invoice-extraction.json`. Convert the user's notes yourself into the exact `project_context.v1` root structure in `assets/project-context-template.json`; do not guess keys such as `projects`, `charge_code`, or `notes`, and do not ask the user to write JSON. Create one context object per distinct project/travel date window, even when several windows share the same Client and Code. Ask the user only for missing business facts such as date range, city, client name, or Client Charge Code.

```bash
python scripts/chief_orchestrator.py run allocate --context project-context.json
```

Every review-list line starts with a `[N@ref]` token: N is the display number (valid only within this generation) and ref is the item's short evidence identity; the allocation also stores the full SHA-256 identity and rejects missing or duplicate identities. Decisions files must reference items by the full token and declare `for_allocation_fingerprint` (the 8-char generation code printed by allocate); Composer refuses stale generations and mismatched refs before touching any data. Every official allocation write archives the prior stamped generation under `process/allocation-generations/` and records a lineage pointer, so repeated allocate/apply runs cannot overwrite the last decided state. When invoices are added or removed later, follow `CHIEF NEXT`: if effective project contexts, policy, and `allocation_engine_revision` are unchanged, Chief first runs rebase, then Composer/updater. Composer and updater reject ordinary answers while a transferable lineage generation is pending; even a zero-carry rebase must pass once through Composer/updater to record lineage clearance. Rebase carries official user-set unit fields and explicit `R@ref` record resolutions whose full evidence identities are unchanged; changed/new evidence returns to the user. If any business basis changed, review the regenerated allocation from scratch. Never reuse an old decisions file by editing its fingerprint or manually select an older generation when Chief has discovered the lineage source.

Allocation validates the project-context schema before creating any units, refuses to run while unsupported inputs or any Stage 1 review-required/unknown document remains open, and records hashes for both extraction and project context. If either input changes later, regenerate allocation and recompose answers rather than reusing the old allocation or answers file. A schema failure means rewrite the same context file from the bundled template; never bypass it with a launcher or patch script.

Treat every concrete expense line supplied by the applicant as an expected-record item. Put a meal record in `meal_hints` or another record in `expense_hints`, never duplicate the same record in both arrays; Stage 2 nevertheless deduplicates legacy cross-array copies by context/category/date/amount and compatible merchant evidence. It writes `expense_hint_reconciliation` for every distinct record. Records without unique extracted matches are grouped by expense type in chat and labeled with generation-safe `R1@ref` tokens. Translate every full R@ref answer into `expense_hint_resolutions` using one canonical action: `matched_existing`, `covered_by_invoice`, `not_reimbursed`, or `pending_invoice`. The first three close that record when valid; `pending_invoice` records the applicant's intention but remains blocking until evidence is supplied or the applicant changes it to `not_reimbursed`. Never close these questions through free-text `question_updates`. Dropping a linked unit reopens the record.

The allocation script prints an applicant review list, then a ready-to-send 转发块 containing all blocking questions in Chinese. If the optional subagent pilot is available, run the Otako pass described below before asking the applicant; otherwise continue immediately. Relay the allocator's 转发块 VERBATIM — do not summarize or shorten it — and add only clearly separated Otako questions/advisories when useful, then wait for the user's answers. Never silently replace or close an official blocking question from an advisory proposal. If terminal output is garbled, read the Markdown process file instead of creating temporary print/extraction scripts.

When the user answers allocation questions, use the bundled Composer to turn those decisions into the current allocation's canonical `unit_updates` and `expense_hint_resolutions`; do not invent another schema such as `answers[].allocations`. Composer resolves the actual `user_no` values printed in chat, supports unit/question/context/hint-resolution updates plus confirm/drop/exclude actions, binds the live allocation fingerprint, dry-runs the updater, and publishes `process/allocation-answers.json` only after validation succeeds. Then run the updater and repeat until no blocking questions remain. After ANY extraction or allocation re-run, treat every old `DOC-xxx`, `UNIT-xxx`, displayed item number, and bare/old `R1@ref` reference as expired. Rebind explicit user-confirmed facts to the new review list using the source descriptors in `references/troubleshooting.md`; never replay number-only memory or an old decisions file.

### Optional Subagent Pilot

When the host supports fresh subagents, use this two-role pilot without changing the canonical workflow. Subagents are read-only reasoning passes: never give them filesystem paths or mutation tools, and never let them write process JSON.

After allocation and any required lineage rebase are current, but before relaying unresolved allocation questions, prepare Otako's task:

```bash
python scripts/chief_orchestrator.py run prepare-agent --role allocation_analyst
```

Read the generated task packet and result template yourself, then send their COMPLETE JSON contents to a fresh subagent with no prior conversation context. The packet embeds `references/otako-allocation-analyst.md`. Save the exact JSON response outside `process/`, then validate it:

```bash
python scripts/chief_orchestrator.py run accept-agent \
    --role allocation_analyst --result <utf8-result.json>
```

Otako's report and generated `.unreviewed.json` proposal packet are advisory and are intentionally rejected by Composer. Use its grouped questions, but do not treat proposals as applicant-confirmed facts. After the coordinator/applicant selects or confirms proposal IDs, promote only that explicit selection:

```bash
python scripts/chief_orchestrator.py run promote-proposals \
    --select P-001,P-003 --reviewed-by coordinator
```

Then compile the stamped `.reviewed.json` path printed by that command through the exact-fingerprint route:

```bash
python scripts/chief_orchestrator.py run compose --proposal <reviewed-proposal.json>
```

Immediately before Stage 3, optionally prepare Kaede's independent task from the confirmed allocation:

```bash
python scripts/chief_orchestrator.py run prepare-agent --role independent_reviewer
python scripts/chief_orchestrator.py run accept-agent \
    --role independent_reviewer --result <utf8-result.json>
```

The packet embeds `references/kaede-independent-reviewer.md`. Kaede can be prepared or accepted only after Stage 2 is fully ready. A current validated `block` result prevents Stage 3 and packaging until the findings are resolved through Composer/Updater and a fresh review is run. Every accepted review is written first to an immutable task-generation archive under `process/subagent-review-generations/`; deleting or corrupting the convenience sidecar cannot clear an accepted blocker. `pass`, `advisory`, and explicit `unavailable` results are recorded in final rows. Only the absence of any accepted current-task review (including missing/stale/invalid results with no valid current archive) fails open to the existing deterministic preflight; never synthesize a pass. The integrity stamp proves only that the accepted result was not subsequently edited, not that the model was truly independent.

If the host cannot start a fresh isolated subagent, skip the pilot and continue the existing single-agent workflow. Do not imitate independence by asking the same context-laden agent to rubber-stamp its own work.

### Batch Answers

For every confirmation or correction batch, use Composer. It resolves current item numbers, writes the live allocation fingerprint, validates field aliases and text safety, dry-runs the updater, and publishes `allocation-answers.json` only after that dry-run passes.

```bash
# Use --set only for short ASCII values, and always copy full N@ref tokens.
python scripts/chief_orchestrator.py run compose \
    --set "3@a1b2c3d4,5@e5f6a7b8: status=confirmed"

# Use the canonical UTF-8 decisions file for Chinese or complex values.
python scripts/chief_orchestrator.py run compose \
    --decisions process/batch-decisions.json
```

Create `process/batch-decisions.json` from `assets/allocation-decisions-template.json`. Keep `schema_version: allocation_decisions.v1` and only these root actions: `decisions`, `expense_hint_resolutions`, `question_updates`, `project_contexts`, `confirm_units`, `drop_units`, and `exclude_units`. Composer compiles all of them; there is no helper-script exception. Use the field quick reference in `references/stage-2-allocation.md` instead of guessing names.

If Composer exits nonzero, read its updater error, correct the same UTF-8 decisions file, and rerun Composer. Never switch to `build_allocation_answers_template.py`, generate `fill_answers.py`, create `patch_allocation.py`/`fix_*.py`, edit `expense-allocation.json`, or modify a bundled script. The template builder is developer-only diagnostic output and is structurally rejected by the updater.

```bash
python scripts/chief_orchestrator.py run apply
```

Composer already ran the official updater in `--dry-run` mode before publishing the answers file. Apply only the file Composer published.

### Decisions Encoding Rule

Write the decisions JSON directly as UTF-8 with the agent's file-editing tool. Do not inject Chinese through PowerShell inline Python, `-Command`, shell interpolation, or a console pipeline. Chief launches every child with Python UTF-8 mode and `PYTHONIOENCODING=utf-8`; direct entry scripts also configure UTF-8, so the agent should not need to prepend environment variables to normal commands. If a terminal merely displays a UTF-8 file incorrectly, read it through a UTF-8-capable file tool instead of rewriting correct data from the garbled display. Composer and the updater reject consecutive `??` markers, replacement characters, and common mojibake; Stage 3 repeats the check on final-visible fields and workbook cells. A single ASCII `?` in legitimate English free text remains allowed.

If the user says a recognized item is wrong, first trace that user-facing item number back to its source files, then ask for or apply the corrected fields.

```bash
python scripts/chief_orchestrator.py run trace --item 9
```

6. For Excel output, read `references/stage-3-excel-output.md`, ask the user for requester if missing, and write rows from `process/expense-allocation.json`. By default, the workbook is generated directly by script using `assets/reimbursement-workbook-layout.toml` for static workbook layout and Python code for business logic, formulas, sorting, and project blocks. The legacy template remains bundled at `assets/reimbursement-template.xlsx`; pass `--template bundled` or a custom `.xlsx` path only when a template-based fallback is explicitly needed.

```bash
python scripts/chief_orchestrator.py run write --output <filled.xlsx> --requester <name>
```

Stage 3 verifies that allocation still belongs to the current extraction generation and that no unsupported input remains unresolved. A mismatch requires a fresh Stage 2 run, not a manual repair.

7. For final packaging, read `references/stage-4-package.md`, then copy and rename source files using the final proof numbers.

```bash
python scripts/chief_orchestrator.py run package
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

- Railway: extract structured `train_no`, `origin_station`, `destination_station`, `travel_date`, `departure_time`, `departure_datetime`, `route`, and refund status under `classification.railway_leg`; also retain the readable evidence note.
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
- Reconcile user notes in both directions. Every distinct structured expense hint must point to an active expense unit, be covered by an identified invoice, or carry an explicit `not_reimbursed` decision. A `pending_invoice` decision is valid progress but remains blocking. Never let an unmatched note disappear merely because no unit was created for it.
- Keep three meal axes independent. `source_category: meal` means the item is substantively a meal and only selects it into meal-policy checks. Shanghai invoice/restaurant city -> `amount_column: meal` plus `Expense Nature: 本地`; non-Shanghai -> `amount_column: travel` plus `Expense Nature: 出差`. Those formal fields never select the 150/60 cap.
- Select the meal cap only from substantive purpose recorded in `final_note`/`meal_context`: `出差餐费` (including a Shanghai meal before/during travel) -> `business_trip_meal`, RMB 150/day; explicit `加班餐费`/`meal_context=overtime` -> `local_overtime_meal`, RMB 60/day. There is no generic "Shanghai/local meal = 60" rule. If purpose is unclear, ask; do not infer it from city, amount column, `Expense Nature`, or assigned project.
- Match taxi and Didi/Gaode ride items by the project journey they support: city/date for ordinary rides, airport/station transfer to the upcoming destination project, and project-to-project station/airport transfers to the project being traveled to.
- Treat Shanghai/local projects conservatively. A local project such as KEEWAY must not receive Shanghai taxi/travel items merely because city and date match. Auto-assign a Shanghai local project only when the ride endpoint, route note, user note, or explicit project keyword names that local client/project; otherwise station/airport transfers inherit the adjacent out-of-town travel project or remain a blocking question.
- For taxi/Didi/Gaode amount columns, use form over substance by ride city: Shanghai rides stay in `taxi` even when allocated to an out-of-town project; non-Shanghai rides go to `travel`.
- Before matching individual railway tickets, group same-day or tightly connected ticket segments into a railway journey chain when dates/times are ordered and one segment's destination station/city matches the next segment's origin station/city. Treat intermediate stops as transfer nodes, not independent project destinations.
- Allocate the whole railway chain together: local/home -> project uses the terminal/upcoming project; project A -> project B uses project B; project -> local/home uses the project just completed. Keep every ticket as a separate expense row, proof number, amount, and segment-specific Note while sharing one project assignment and `journey_chain_id`.
- Show automatically inferred railway chains as one non-blocking review line in chat. Ask one whole-chain question only when the chain does not point to a unique project, the station/time sequence is ambiguous, or the user says an intermediate city was an actual stop/project rather than a transfer.
- Match standalone railway tickets and flight travel by route destination and travel date with a reasonable +/- 1 day project buffer.
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
- After receiving user answers, write canonical `allocation_decisions.v1`, use `scripts/compose_answers.py` (normally through Chief) to generate a fingerprint-bound `allocation-answers.json`, then use `scripts/apply_allocation_answers.py` to update `expense-allocation.json`. This preserves question status, substitute approval links, and change history.
- Never create temporary helper or patch scripts for allocation edits. Convert every batch through Composer and the updater even when the decision batch is long or a prior compose attempt failed.
- Generate final reimbursement notes with the required Chinese templates from the stage-2 reference, including confirmed taxi origin/destination place types. Never write literal placeholders such as `出发地类型` or `目的地类型` into `final_note`; ask the user when either endpoint type is unclear.
- Mark rail/flight cancellation or refund evidence in the final note as `高铁退票费（出发地-目的地）` or `飞机退票费（出发地-目的地）` instead of the ordinary travel note.

## Stage 3 Excel Output Rules

Read `references/stage-3-excel-output.md` before writing the reimbursement workbook. Keep these core rules in mind:

- Ask the user for `Requester` if not already known.
- Write `Date` as `YYYYMMDD`.
- Use only confirmed, reliable, or explicitly provisional `other` `expense_date`; if `date_required` is true or `expense_date` is blank, ask in chat before writing the workbook.
- Use confirmed `client_name` and `client_charge_code` from stage 2.
- Set `Expense Nature` by the formal amount-column evidence, not by assigned project: meal uses invoice/restaurant city; taxi/Didi/Gaode ride rows use ride city. Shanghai formal city means local; non-Shanghai formal city means business trip. For meals this is only workbook presentation and never determines the daily cap policy.
- Use the confirmed stage-2 `final_note` for `Note`.
- Put each amount in exactly one template amount column: hotel, travel, taxi, meal, mobile, or other.
- For meal rows, recompute the visible amount column by formal invoice/restaurant city before writing: Shanghai -> `meal`; non-Shanghai -> `travel`.
- For taxi/Didi/Gaode ride rows, recompute the visible amount column by ride city before writing: Shanghai -> `taxi`; non-Shanghai -> `travel`. Do not change this merely because the ride is allocated to an out-of-town project.
- For meal expenses with daily standards, trust the script-generated per-row `meal_cap_policy`/`meal_daily_cap` and `meal_daily_cap_checks`; never recalculate policy from city or workbook column. Sum same-date rows by `meal_cap_policy` across both `meal` and `travel` columns. Business-trip meals are RMB 150/day; only explicit local overtime meals are RMB 60/day. Relay the generated `MEAL DAILY CAP CHECK` block verbatim. If a date exceeds the relevant cap without attendee details, ask whether the meal date is wrong, attendees are missing, or one item should use a lower `reimbursable_amount`; if attendee details exist, treat the over-cap result as advisory only. If reimbursable amount differs from invoice amount, the final note must state `发票金额XX/实际报销XX`.
- For hotel expenses, apply the per-night cap after rows are built: Beijing/Shanghai/Guangzhou/Shenzhen are RMB 800/night, other cities are RMB 600/night. Show `hotel_cap_checks` in chat. If nights or city tier are missing, ask for check-in/check-out/nights/city. If a hotel exceeds the relevant cap with shared-room/co-occupant details, treat it as advisory only; otherwise ask whether one item should use a lower `reimbursable_amount`.
- On hotel corrections, setting either `city` or `hotel_city` updates the other; conflicting values are rejected. Do not ask the applicant for the same city twice or retry with a second field name.
- Hotel final notes must not keep placeholders such as `X晚`, `入住日`, or `离店日`. If hotel nights/check-in/check-out are known, the scripts regenerate `出差酒店（X晚，入住日-离店日）` with actual values; if those fields are missing, Stage 3 preflight blocks workbook generation.
- Always show or summarize `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT`. If the writer exits with code `2`, no workbook was written because allocation is not structurally ready: open questions, invalid categories/columns, missing dates/client/code/amount, admin/mobile conflicts, raw ticket notes, or missing taxi place types must be fixed first.
- If a current accepted Kaede review contains blocking findings, Stage 3 exits with code `2` before workbook creation. The writer requires `--process-dir` to be the same canonical directory that contains `--allocation`, so a second process folder cannot hide the gate. Resolve the cited `N@ref` items through Composer/Updater and run a fresh review. Keep these findings separate from deterministic meal/hotel `blocking_policy_checks`.
- If any stage script exits with code `4`, a process JSON failed its integrity check (modified outside the sanctioned flow); follow the printed recovery steps and do not patch further.
- If `write_reimbursement_template.py` exits with code `3`, the workbook and final row files were written, but the `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` block contains blocking meal/hotel policy checks that must be shown to the applicant and resolved before final submission.
- Assign overall proof numbers by substantive proof order: flight/rail, hotel, taxi/Didi, Gaode, meal, mobile, other.
- Stage 3 must keep every active railway journey chain on one project and reject a stale/broken chain whose adjacent stations no longer connect. Per-ticket destination validation must not override the chain assignment at an intermediate transfer station.
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
- On Windows, package promotion retries transient file locks. If it still fails, close the packaged workbook and any Explorer preview/window inside the prior package folder, then rerun Stage 4 through Chief; calling the package script directly is not a workaround.
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
- Every distinct applicant expense record in `meal_hints`/`expense_hints` appears in `expense_hint_reconciliation` and is either uniquely matched to active evidence, covered by an identified invoice, or explicitly marked `not_reimbursed`; no `pending_invoice` remains at finalization, and the same record duplicated across both hint arrays is counted once.
- Allocation, final rows, workbook, and package manifest belong to the same current extraction/allocation generations; a stale generation is regenerated rather than patched.
- Stage 3 output has a requester, no unconfirmed blocking items, one amount column per row, no duplicate Didi/Gaode summary rows, meal and hotel cap checks shown in chat, and totals reconcile to confirmed allocation units.
- Optional subagent tasks contain path-free snapshots bound to full allocation/extraction fingerprints. Otako proposals were reviewed rather than auto-applied; any current Kaede blocker was resolved before Stage 3, and the consumed review fingerprint is recorded in final rows.
- Substitute invoice metadata and approval screenshot paths remain in `final-expense-rows.json` even though they are not written into the visible Excel rows.
- Stage 4 package has a stamped manifest bound to the current final-rows fingerprint and workbook hash; every listed invoice/support file exists and matches its manifest hash, and the manifest has no unresolved issues.
