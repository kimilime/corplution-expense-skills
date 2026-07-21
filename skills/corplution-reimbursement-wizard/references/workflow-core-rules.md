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

- Railway: extract structured `train_no`, `origin_station`, `destination_station`, `travel_date`, `departure_time`, `departure_datetime`, `route`, refund status, and `refund_fee_amount` under `classification.railway_leg`; also retain the readable evidence note. On a railway refund invoice, the printed refund fee is the reimbursable `invoice.total_amount`; never reinterpret it as the original fare or turn a blank label into zero.
- Didi/Gaode: city, origin, destination.
- Hotel: seller or hotel name, city if inferable, quantity or nights.
- Meal: seller/restaurant and meal service.
- Mobile: phone number and billing period.

Stage 2 must normalize final notes using `references/stage-2-allocation.md`. Keep source remarks in `raw_remarks`; do not replace them with generated notes.

## Stage 2 Allocation Rules

Read `references/stage-2-allocation.md` before allocating expenses. Keep these core rules in mind:

> **Fiscal-year codes:** `<ADMIN_CODE>` and `<BD_CODE>` are placeholders for the current fiscal-year charge codes, defined in `assets/special-code-definitions.json` and rolled each fiscal year with `python scripts/special_codes.py set-year <year>`. Substitute the current codes; never write a literal `<...>` placeholder or hardcode a year — Stage 3 rejects any charge code containing `<`/`>`.

- Treat project identity as `client_name + city + date_range + charge_code + user_description`; do not treat charge code alone as unique because many pending projects may share `<BD_CODE>`.
- Use LLM judgment for first-pass matching, but ask the user about low-confidence or conflicting items.
- Treat `invoice.issue_date` as evidence, not a default occurrence date. Reliable occurrence dates are: printed flight/rail travel date, printed hotel check-in/check-out dates, Didi/Gaode ride datetime from a trip report, and mobile month-end from the billing period or invoice month. For pure `other` expenses, you may temporarily use `invoice.issue_date` as `expense_date`, but mark it provisional and show a non-blocking advisory for user review.
- Exclude `<ADMIN_CODE>` contexts from hotel/meal/taxi/travel automatic city/date scoring. Admin is not a Shanghai project and must not win fallback matching.
- Match hotels first by hotel city plus stay dates; when stay dates or project dates are missing, use city uniqueness only for project pre-allocation, and still ask for missing nights/check-in/check-out needed for hotel caps.
- Match meals by explicit user-provided meal notes when available. Parse notes such as `6.1 德克士 61.8` into meal hints, then match by combined amount/date/merchant evidence instead of any single strict field. Otherwise treat invoice dates as unreliable and auto-assign only when a non-Shanghai invoice city has exactly one project in the period. Show inferred meals as advisory so the user can batch-correct dates/attendees/amounts.
- Reconcile user notes in both directions. Every distinct structured expense hint must point to an active expense unit, be covered by an identified invoice, or carry an explicit `not_reimbursed` decision. A `pending_invoice` decision is valid progress but remains blocking. Never let an unmatched note disappear merely because no unit was created for it.
- Keep three meal axes independent. `source_category: meal` means the item is substantively a meal and only selects it into meal-policy checks. Shanghai invoice/restaurant city -> `amount_column: meal` plus `Expense Nature: 本地`; non-Shanghai -> `amount_column: travel` plus `Expense Nature: 出差`. Those formal fields never select the 150/60 cap.
- Treat `高铁上点餐`, `餐车用餐`, and similar wording as `meal`, not a high-speed-rail ticket. Put it in `meal_hints`; the word `高铁` describes where the meal occurred. Classify a record as rail travel only with actual ticket evidence such as `铁路电子客票`, `12306`, or a train number plus route.
- Select the meal cap only from substantive purpose recorded in `final_note`/`meal_context`: `出差餐费` (including a Shanghai meal before/during travel) -> `business_trip_meal`, RMB 150/day; explicit `加班餐费`/`meal_context=overtime` -> `local_overtime_meal`, RMB 60/day. There is no generic "Shanghai/local meal = 60" rule. If purpose is unclear, ask; do not infer it from city, amount column, `Expense Nature`, or assigned project.
- Honor one-off, event-specific meal standards through project context, never `policy.toml`. When the applicant declares a special per-day standard for a specific event — e.g. a 年会/半年会 where 7.17's 自理餐标 is RMB 60 and 7.18's is RMB 150, or a client with its own on-site standard — record it on that event's context as `meal_standards`, a list of `{date, daily_cap, label}` (`daily_cap` is the RMB per-day amount). A meal is capped by a declared standard only when its `project_context_id` and `expense_date` both match an entry, so a same-date expense in another context is untouched. The declared cap replaces the generic 150/60 for that day and is recorded with its provenance in the daily cap check. `policy.toml` stays the permanent single source of truth for standing caps; one-off event standards live only in the (hash-pinned) project context, declared during Stage 2. A declared standard at or below the generic cap passes on the applicant's word; one above it is applied but flagged advisory (never blocked) so an approver can see it exceeds standard policy. Per-meal splits (e.g. 中餐60/晚餐90) are expressed as that day's `daily_cap` total (150), noted in `label`.
- Match taxi and Didi/Gaode ride items by the project journey they support: city/date for ordinary rides, airport/station transfer to the upcoming destination project, and project-to-project station/airport transfers to the project being traveled to.
- Treat Shanghai/local projects conservatively. A local project such as KEEWAY must not receive Shanghai taxi/travel items merely because city and date match. Auto-assign a Shanghai local project only when the ride endpoint, route note, user note, or explicit project keyword names that local client/project; otherwise station/airport transfers inherit the adjacent out-of-town travel project or remain a blocking question.
- For taxi/Didi/Gaode amount columns, use form over substance by ride city: Shanghai rides stay in `taxi` even when allocated to an out-of-town project; non-Shanghai rides go to `travel`.
- Before matching individual railway tickets, group same-day or tightly connected ticket segments into a railway journey chain when dates/times are ordered and one segment's destination station/city matches the next segment's origin station/city. Treat intermediate stops as transfer nodes, not independent project destinations.
- Allocate the whole railway chain together: local/home -> project uses the terminal/upcoming project; project A -> project B uses project B; project -> local/home uses the project just completed. Keep every ticket as a separate expense row, proof number, amount, and segment-specific Note while sharing one project assignment and `journey_chain_id`.
- Show automatically inferred railway chains as one non-blocking review line in chat. Ask one whole-chain question only when the chain does not point to a unique project, the station/time sequence is ambiguous, or the user says an intermediate city was an actual stop/project rather than a transfer.
- Match standalone railway tickets and flight travel by route destination and travel date with a reasonable +/- 1 day project buffer.
- When travel connects two project cities, assign it to the destination/project being traveled to, not the origin project. Never override this merely because the origin station city matches a previous project.
- Do not pre-match `other` or `unknown` by invoice city. Ask the user; invoice issuer city can be misleading for SaaS, online meetings, associations, and other services.
- Allocate mobile expenses to `<ADMIN_CODE>` with `client_name = 通讯费`, not `Admin`; fill Date as that month's last day.
- Never use `<ADMIN_CODE>`, `通讯费`, or the mobile amount column as a fallback for unmatched taxi/travel/meal/hotel expenses. Unmatched transport remains a blocking question unless a transfer/travel rule matches it to a project.
- For other `<ADMIN_CODE>` expenses, use a specific matter name as `client_name` when known, such as `年会`, `半年会`, `客户会`, or `行业协会会议`; if missing, use `项目、调研以外的其他费用` and show a non-blocking chat prompt so the applicant can refine it.
- Ask about `other` and `unknown` expenses by default. For `other`, project/note/accounting treatment may still be blocking, but the date can temporarily use the invoice date with an advisory. For `unknown`, ask for the actual date unless the user reclassifies it as pure `other`.
- Ask follow-up questions directly in the current conversation. Use `process/expense-allocation.md/json` as internal process files only; do not tell the user to inspect those files. Group repetitive uncertainties by expense type, such as one meal batch question listing all meal item numbers, files, invoice numbers, dates, amounts, and suggested projects.
- If the user gives meal details in natural language, including "with X", "和X一起", "同事X", or dining counterparties, capture them into `attendees` even when the daily meal cap is not exceeded. Do not rely on the cap check as the only attendee collection point.
- Before asking follow-up questions, show a compact applicant review list in chat with item number, source filename, seller/provider, date, amount, category, suggested project, and status.
- Combine all uncertainties for the same item into one question block, then batch same-type items into one grouped question whenever practical. For example, ask meal details once for items 1/3/5/7 instead of repeating the same question four times.
- Use simple user-facing item numbers in conversation, such as item 1 or item 2, instead of internal IDs like `DOC-001` or `UNIT-001`. Keep internal IDs only in process JSON/Markdown for traceability.
- When a user challenges an item, run `scripts/trace_expense_item.py` and identify the source filename, invoice number, seller, amount, date, and trip details before applying corrections.
- Flag an item as a substitute invoice only when the user explicitly declares it one ("替票"/"抵用发票"); never infer substitute status from the mere presence of an approval screenshot or the word "审批". A partner approval screenshot the user has not tied to a declared substitute stays a plain supporting document — do not append the substitute marker to any expense on that basis. For genuine substitutes, track them separately, ask the user for the partner approval screenshot, append the substitute marker to the final note, and carry the substitute flag to the Excel stage.
- After receiving user answers, write canonical `allocation_decisions.v1`, use `scripts/compose_answers.py` (normally through Chief) to generate a fingerprint-bound `allocation-answers.json`, then use `scripts/apply_allocation_answers.py` to update `expense-allocation.json`. This preserves question status, substitute approval links, and change history.
- Never create temporary helper or patch scripts for allocation edits. Convert every batch through Composer and the updater even when the decision batch is long or a prior compose attempt failed.
- Generate final reimbursement notes with the required Chinese templates from the stage-2 reference, including confirmed taxi origin/destination place types. Never write literal placeholders such as `出发地类型` or `目的地类型` into `final_note`; ask the user when either endpoint type is unclear.
- Custom place↔type relationships the applicant knows but the model cannot (e.g. 友力国际大厦=公司, 某某公寓=家, a client's industrial park=客户) persist across months in `assets/place-definitions.json`. No private facts are hard-coded — the office lives only in this JSON. Allocation reads it first, so a remembered place resolves at high confidence with no question. Public places (机场/火车站/酒店, e.g. 虹桥T2→机场) are general knowledge, classified by keyword heuristics without any memory, and are never stored or written back. When the applicant confirms a private taxi endpoint's type and it is applied via Composer/Updater (`origin_type`/`destination_type`), the confirmed mapping is written back automatically for future runs — model auto-guesses are never memorized. The file is user/agent-editable directly or via `python scripts/place_config.py add|list|remove`. Loading fails open to an empty memory (unknown places are asked as usual) and never blocks allocation.
- Mark rail/flight cancellation or refund evidence in the final note as `高铁退票费（出发地-目的地）` or `飞机退票费（出发地-目的地）` instead of the ordinary travel note.
- Allocate railway refund-fee tickets by the same route/date/destination-project rules as ordinary railway tickets. Connected refund tickets may form their own transfer chain, but must not be mixed into a chain of tickets that were actually travelled.

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
- If a current accepted subagent audit (Otako Mirror Warden or Kaede Gate Challenger) contains blocking findings, Stage 3 exits with code `2` before workbook creation. The writer requires `--process-dir` to be the same canonical directory that contains `--allocation`, so a second process folder cannot hide the gate. Resolve the cited `N@ref` items through Composer/Updater and run a fresh audit. Keep these findings separate from deterministic meal/hotel `blocking_policy_checks`.
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
- Rename support files as proof number and support type, such as trip report, payment receipt (付款小票), approval screenshot (审批截图), or substitute approval. Every supporting document the user keeps is packaged under the proof number of the invoice it backs; a supporting document that names no invoice (`supports_document_id`) hard-blocks Stage 3 until the user associates it or excludes it — it is never silently dropped.
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
- Preferred subagent tasks contain role-scoped, path-free snapshots bound to full allocation/extraction fingerprints and capped at 384 KiB (an inline-prompt budget, not a transport limit — the packet is pasted whole into a fresh Agent/Task-tool prompt). On Claude Code, paste the packet JSON into a fresh Agent/Task-tool subagent's prompt; never ask a subagent to locate workspace files. Each task carries the exact result template plus a JSON Schema and enum constraints for structured-output hosts. When the host offered a fresh Agent and the user did not opt out, the Mirror Warden and Gate Challenger audits ran immediately before Stage 3; any current blocking finding from either was resolved before Stage 3, and the consumed audit fingerprints are recorded in final rows. An unavailable host remains fail-open and never fabricates a pass. If a packet still exceeds the cap (only at extreme unit counts, ~430+), `prepare-agent` degrades: it writes no handoff and prints DEGRADE, and the audit falls open to the deterministic Stage-3 preflight. Never react to an over-cap packet by hand-assembling a packet, splitting it manually, or manually accepting a result — `accept-agent` refuses a degraded task's result; proceed straight to Chief write, where the preflight is authoritative.
- Substitute invoice metadata and approval screenshot paths remain in `final-expense-rows.json` even though they are not written into the visible Excel rows.
- Stage 4 package has a stamped manifest bound to the current final-rows fingerprint and workbook hash; every listed invoice/support file exists and matches its manifest hash, and the manifest has no unresolved issues.
