# Stage 2 Project Allocation Workflow

Use this reference after stage 1 has produced `process/invoice-extraction.json`. Stage 2 maps extracted invoice evidence to projects, clients, charge codes, cities, and final reimbursement-template columns through LLM-assisted matching and user confirmation.

Do not fill the Excel reimbursement template in this stage. Produce allocation process files for the next stage.

## Inputs

Required:

- `process/invoice-extraction.json`
- User-provided natural-language project context for the reimbursement period

Optional:

- User corrections from prior allocation rounds
- Meal details, attendees, and business purpose
- Substitute-invoice notes
- Partner approval screenshot paths for substitute invoices

## User Project Context

Ask the user to provide, in natural language:

- Reimbursement period, such as "2026-06-01 to 2026-06-30".
- For each project or work event:
  - date range
  - city or cities
  - client name
  - client charge code
  - project or event description
  - optional specific fee notes
- For meals when known:
  - actual meal date
  - client/project
  - attendees or dining counterparties
  - whether it was local Shanghai meal or out-of-town trip meal
- Substitute invoice notes:
  - which invoice/item is a substitute invoice
  - what it substitutes for
  - whether partner approval screenshot is available

Do not require a rigid form. Parse the user's natural language into structured context and show a concise draft for confirmation when the context is ambiguous.

Ask missing information in the current chat, not by asking the user to open process files. The Markdown and JSON outputs are audit/process artifacts for Codex; the user-facing loop must happen in conversation.

## Project Context Model

Represent user context as `project_contexts`:

```json
{
  "context_id": "CTX-001",
  "date_start": "YYYY-MM-DD",
  "date_end": "YYYY-MM-DD",
  "city": "Taiyuan",
  "client_name": "",
  "client_charge_code": "CORP-2026-BD",
  "project_description": "",
  "user_notes": "",
  "travel_buffer_days": 1,
  "status": "draft"
}
```

Project identity is composite:

```text
client_name + city + date_start/date_end + client_charge_code + project_description
```

Never use `client_charge_code` alone as the project key. Multiple distinct projects may share `CORP-2026-BD` while belonging to different clients.

## Allocation Units

Create allocation units from stage-1 data:

- For ordinary invoices, hotel invoices, railway e-ticket invoices, meal invoices, mobile invoices, and other single-document invoices: one allocation unit per invoice.
- For Didi trip reports: one allocation unit per `supporting_items[]` ride.
- For Didi summary invoices linked to trip reports: use them as supporting evidence, but do not create duplicate allocation units from the summary invoice.
- For unmatched Didi summary invoices without trip reports: create one allocation unit, mark it `needs_user_confirmation: true`, and ask for trip details or whether to use summary-level allocation.

Each allocation unit should carry:

- source document ID and optional item ID
- source file path and user-facing source filename
- invoice number if available
- source category from stage 1
- amount
- formal invoice issue date when available
- reliable expense date or date range, plus `date_source` and `date_required`
- city, origin, destination, route, seller, and note evidence
- raw remarks
- linked support documents

Do not treat `invoice.issue_date` as the expense date. Reliable occurrence dates are limited to:

- flight or rail travel date printed on the ticket/invoice
- hotel check-in/check-out dates printed on the invoice; use checkout date as the workbook `Date` while preserving the stay range
- Didi/Gaode ride datetime from a trip report
- mobile/telecom month-end from the billing period, or the invoice month when no billing period is extractable

For pure `other` invoices, use `invoice.issue_date` as a provisional `expense_date` when it exists. Set `date_source: other_invoice_issue_date_provisional`, `date_is_provisional: true`, and show a non-blocking advisory so the applicant can correct the occurrence/record date if needed.

For ordinary meal, taxi summary, hotel-without-stay-date, and `unknown` invoices, leave `expense_date` blank, set `date_required: true`, and ask the applicant which date to record. If an `unknown` item is later reclassified as pure `other`, it can then use the provisional `other` date rule.

## Matching Method

For each allocation unit, score candidate project contexts using:

- Date fit: inside project date range, or within the allowed travel buffer for airport/station transfer.
- City fit: exact city match, destination city match, route endpoint match, or strong textual city evidence.
- Expense-type logic: taxi, travel, hotel, meal, mobile, other.
- User notes: direct mention of seller, restaurant, route, event, attendee, or special circumstance.
- Conflict checks: overlapping projects, same code with different clients, missing city, missing reliable expense date, missing actual meal date, or unsupported substitute invoice.

Use confidence labels:

- `high`: assign automatically, but still show in the allocation draft.
- `medium`: provide a suggested assignment and ask for confirmation in the chat.
- `low`: do not assign; ask the user.
- `fixed`: deterministic assignment such as mobile to admin.

Use type-specific pre-allocation rules. Do not apply one generic city/date rule to every invoice type:

- Hotel: prioritize hotel city plus stay dates. If stay dates or project dates are missing, pre-allocate only when the hotel city maps to exactly one project context; still ask for missing nights/check-in/check-out for cap checks.
- Meal: invoice date is unreliable. Use explicit user-provided meal details when available; otherwise pre-allocate only when a non-Shanghai meal city maps to exactly one project context. The project inference may be advisory, but the actual meal date remains blocking unless the user already provided it.
- Taxi/Didi: allocate to the project journey the ride supports. Ordinary city rides match by city/date. Airport/station transfers belong to the upcoming destination project when the next project starts within the travel buffer. Transfers from one project city to a station/airport for the next city belong to the project being traveled to.
- Flight/rail: match by route destination and travel date, allowing a reasonable +/- 1 day project buffer.
- Other and unknown: do not pre-match by invoice city. Ask the user for accounting note and project/admin matter because issuer city can be misleading for SaaS, online meeting, association, platform, or generic service expenses. For pure `other`, temporarily use the invoice issue date as Date and advise the user to confirm/correct it; for `unknown`, ask for the actual date until reclassified.

Record deterministic pre-allocation in `auto_project_match` with values such as `hotel_stay_dates`, `hotel_unique_city`, `unique_non_shanghai_city`, `taxi_transfer_to_next_project`, `taxi_city_date`, `travel_destination_date`, or `travel_route_date`, and explain the basis in `match_reason`.

## Expense-Type Rules

### Taxi And Didi

Use ride-level rows when Didi trip reports exist.

Match by:

- ride date/time
- city
- origin/destination
- project date range
- airport or railway station transfer pattern

Allow reasonable cross-date travel logic. Example: if the project starts on the 3rd in Shanxi, a taxi on the 2nd to an airport or railway station can belong to that project.

For airport/station transfer rides, prefer the project being traveled to:

- Shanghai taxi to airport/station on the day before an out-of-town project -> upcoming destination project.
- Taxi from a project city to airport/station when another city project starts within the next day -> the next/destination project.
- Arrival-side taxi in the destination city from airport/station to hotel/client -> the destination project.
- If there is no travel document and two projects can both explain the ride, mark medium confidence and ask in the batch taxi question.

If a ride city/date does not match any provided trip/project, ask the user. The answer may be a local overtime taxi, a missing project context, personal/non-reimbursable, or another business reason.

Final template column:

- Shanghai/local taxi -> `taxi`
- Out-of-town trip taxi -> `travel`

Final note:

- Normal taxi: `打车（出发地类型-目的地类型）`
- Overtime taxi: `打车（出发地类型-目的地类型）（加班）`

Determine `origin_place_type` and `destination_place_type` before finalizing the note. If either type is uncertain, ask the user.

### Railway And Flight Travel

Match by route, destination city, and travel date. Travel is usually high-confidence when the destination city and travel date align with a project city/date range, allowing a reasonable +/- 1 day buffer.

When travel connects two project cities, assign it to the project being traveled to. Return travel without a following project usually belongs to the project just completed. If route direction is unclear, ask in the travel batch question.

Final template column: `travel`.

Final note:

- High-speed rail or train: `高铁（出发地-目的地）`
- Flight: `飞机（出发地-目的地）`

Use city or station/airport names from the route. Do not include train number, flight number, or seat in the final note unless the user asks; keep those details in evidence fields if needed.

### Hotel

Match by hotel city and stay date when available. If the invoice has no stay date, the project context lacks dates, or the stay date cannot be reliably extracted, use hotel/seller city only when that city maps to exactly one project context.

Ask the user if:

- city matches multiple contexts
- stay dates are missing, unreliable, or conflict with the project period
- hotel city cannot be inferred
- there are overlapping trips

Final template column: `hotel`.

Final note:

- `出差酒店（X晚，入住日-离店日）`

If stay dates or nights are missing, infer project ownership from city uniqueness only when high confidence, but still ask the user for check-in date, check-out date, and number of nights. Project pre-allocation and hotel cap validation are separate: a hotel can be pre-allocated while still requiring stay-night confirmation.

Also ask for shared-room/co-occupant details when a hotel may exceed the per-night standard. Stage 3 applies hotel caps after final rows are built: Beijing/Shanghai/Guangzhou/Shenzhen are RMB 800 per night, other cities are RMB 600 per night.

Carry these fields when known:

- `hotel_city`
- `hotel_city_tier`: `first_tier` for Beijing/Shanghai/Guangzhou/Shenzhen, otherwise `other`
- `hotel_nights`
- `check_in_date`
- `check_out_date`
- `shared_room`
- `room_shared_with`
- `room_share_note`

If the user says only the cap amount should be reimbursed, keep `amount` / `invoice_amount` as the invoice amount and write the amount to claim in `reimbursable_amount`.

### Meal

Treat meals as confirmation-heavy because invoice issue date may not equal actual meal date.

If the user's project notes already say which meal/date/amount belongs to which project, use those notes directly. Otherwise, do not rely on invoice issue date for project matching or workbook Date. Use the invoice city for project pre-allocation only when it is a non-Shanghai city and that city maps to exactly one project in the reimbursement period.

For a large batch of meal invoices, do not ask one chat question per invoice. Use one grouped meal question that lists item number, source filename, invoice number, seller, invoice date, amount, and suggested project. The applicant can answer in batches, such as: "1/3/5/7 属于山西信托，日期分别是 6/3、6/4、6/5、6/6；2/4/6 属于广联达，日期分别是 6/10、6/11、6/12。"

If a meal was auto-assigned by the unique non-Shanghai city rule, make the project ownership review advisory rather than blocking. Do not use invoice date as the provisional meal date. If the actual meal date is missing, keep a blocking grouped meal question open so the applicant can provide dates, attendees, or reimbursable amount changes. Shanghai meals are not auto-confirmed merely because dates line up; they may be local project meals, overtime meals, client meetings, or other local events.

Create a meal review list unless the user already provided clear meal details. Ask for:

- actual meal date
- project/client
- attendees or dining counterparties
- whether it is local Shanghai meal or out-of-town trip meal
- whether it is a substitute invoice
- whether actual reimbursable amount differs from invoice amount, when the user says a meal should be partially reimbursed to meet the daily standard

Final template column:

- Shanghai/local meal -> `meal`
- Out-of-town business-trip meal -> `travel`

Final note:

- Out-of-town business-trip meal: `出差餐费`
- Shanghai meal that belongs in `meal` but is tied to station/airport travel context: `出差餐费（高铁站/机场）`
- Overtime meal: `加班餐费`

Carry attendee details in a separate `attendees` field for downstream use. Include attendees in the final note only if the user explicitly wants that style.

If the user says only part of a meal invoice should be reimbursed, keep `amount` / `invoice_amount` as the invoice amount and write the amount to claim in `reimbursable_amount`. Do not overwrite the recognized invoice amount just to meet the meal cap.

### Mobile

Assign mobile/telecom expenses to:

- `client_charge_code`: `CORP-2026-ADMIN`
- `client_name`: `通讯费`
- confidence: `fixed`

Final template column: `mobile`.

Final note:

- `X月通讯费`

Use the billing period, not the invoice issue date, when available. For example, billing period `202605` becomes `5月通讯费`, and `expense_date` should be `2026-05-31`. If no billing period is extractable, use the last day of the invoice month rather than the issue day itself.

### Admin Matter Client Names

Never use `Admin` as the final `client_name` for `CORP-2026-ADMIN` rows. The Client column should describe the matter:

- Mobile/telecom: use `通讯费` automatically and do not ask the user.
- Other admin expenses: use the specific matter when known, such as `年会`, `半年会`, `客户会`, or `行业协会会议`.
- If the matter is missing, set `client_name` to `项目、调研以外的其他费用` and show a non-blocking prompt in chat asking whether the applicant wants to replace it with a more specific matter name.

This prompt is advisory only. Do not block stage 3 Excel output merely because the admin matter name is still the default.

### Other

Ask the user by default. Do not invent a project assignment and do not pre-match by invoice issuer city. The issuer's city is often misleading for online meetings, SaaS, platform services, industry associations, and generic service providers.

For pure `other`, temporarily use the invoice issue date as the workbook Date when available. Mark it provisional and show an advisory review item in chat; do not block Stage 3 only because the applicant has not separately confirmed this date. If the applicant says the actual occurrence/record date differs, update `expense_date`, set `date_source: user_confirmed`, and clear `date_is_provisional`.

For `unknown`, do not use the invoice issue date until the item has been reclassified. Ask what it is and which date to record.

Final template column: `other` unless user context clearly says it belongs to another template column.

Final note: ask the user to provide the note.

## Final Note Format

The downstream Excel `Note` field must use these exact Chinese templates after allocation and user confirmation:

| Expense Type | Final Note Template |
| --- | --- |
| High-speed rail / train | `高铁（出发地-目的地）` |
| Flight | `飞机（出发地-目的地）` |
| Taxi / Didi | `打车（出发地类型-目的地类型）` |
| Overtime taxi | `打车（出发地类型-目的地类型）（加班）` |
| Out-of-town meal | `出差餐费` |
| Shanghai station/airport meal | `出差餐费（高铁站/机场）` |
| Overtime meal | `加班餐费` |
| Hotel | `出差酒店（X晚，入住日-离店日）` |
| Mobile | `X月通讯费` |
| Other | User-provided note |

Do not use the verbose evidence note from stage 1 as the final reimbursement note. Stage 1 notes are evidence; stage 2 final notes are accounting-facing template text.

## Place Type Classification For Taxi Notes

Classify taxi/Didi origin and destination into place types:

| Evidence | Place Type |
| --- | --- |
| `江宁路`, `友力国际大厦`, company office aliases, or known office address | `公司` |
| Airport names, terminals, arrivals, departures, or `机场` | `机场` |
| Railway stations, high-speed rail stations, train stations, or `火车站` | `火车站` |
| Hotel names or addresses that clearly look like lodging | `酒店` |
| Client office, bank/company office, industrial park, office tower tied to project context | `客户` |
| User confirms home or residential address | `家` |
| User confirms restaurant or dining venue | `餐厅` |
| Unclear place, residential-looking place, restaurant-looking place, or ambiguous POI | ask user |

Use LLM judgment for obvious place types, but do not guess sensitive or ambiguous locations. If a place might be the user's home, a restaurant, or another personal place, ask:

```text
这笔打车的出发地/目的地「XXX」我无法确定类型，应该写成 公司/客户/酒店/机场/火车站/家/餐厅/其他 哪一种？
```

Store:

- `origin_place_type`
- `destination_place_type`
- `place_type_confidence`
- `place_type_needs_confirmation`

Only generate the final taxi note after both endpoint types are known or confirmed.

## Substitute Invoices

When the user says an invoice is a substitute invoice, replacement invoice, or "替票":

- Set `is_substitute_invoice: true`.
- Ask the user to provide the partner approval screenshot file.
- Append `（抵）` to the final downstream `Note`; use full-width Chinese parentheses.
- Record what expense it substitutes for when provided.
- Set `approval_required: partner_approval_screenshot`.
- Record approval screenshot file path when provided.
- Set `approval_file_status: provided` when the file path is known and `approval_file_status: missing` when it is not.
- Add an issue if the screenshot is required but missing.

The final file-packaging stage must include the partner approval screenshot.

If the user has not provided the screenshot, add a question such as:

```text
你确认第X项是替票/抵用发票。我需要把 Note 标注为「...（抵）」，同时最终文件包需要合伙人审批截图。请提供审批截图文件，或说明暂时缺失。
```

Apply `（抵）` after generating the normal final note:

- `打车（公司-机场）` -> `打车（公司-机场）（抵）`
- `出差餐费` -> `出差餐费（抵）`
- `出差酒店（1晚，2026-06-15-2026-06-16）` -> `出差酒店（1晚，2026-06-15-2026-06-16）（抵）`

## Interaction Loop

Stage 2 is intentionally iterative:

1. Parse the user's project context into `project_contexts`. The user may provide this in natural language; the agent should structure it internally and ask only for missing business facts, not for JSON.
2. Show a concise context draft if dates, cities, clients, or codes are ambiguous.
3. Build allocation units from stage-1 JSON.
4. Assign high-confidence and fixed items.
5. Create suggested assignments for medium-confidence items.
6. Show an applicant review list in the current conversation before questions, so the user can identify each item by simple number and source filename.
7. Ask targeted questions in the current conversation for low-confidence, medium-confidence, meal, other, conflicting, or substitute-invoice items. Batch repetitive questions by expense type whenever several items need the same kind of answer.
8. Convert user answers into `allocation-answers.json`, apply them with `scripts/apply_allocation_answers.py`, and regenerate `expense-allocation.md/json`.
9. Repeat until every allocation unit is confirmed or explicitly left in the question queue.

Group questions by expense type first, then list user-facing item numbers inside each group. If one item has multiple uncertainties, ask them together in that type group. For example, a single meal group can ask for actual meal date, project/client, attendees, and note type for items 1/3/5/7. A single hotel group can ask for check-in/check-out/nights for several hotels. A single taxi group can ask for unclear origin/destination place types for several rides.

When presenting a grouped question, include enough information for batch replies:

- item number
- source filename, and supporting invoice filename when different
- invoice number when available
- seller or service provider
- reliable expense date, or invoice issue date clearly labeled as not enough
- amount
- category/final column
- suggested client/code/project
- short evidence note

Tell the user they can answer by grouped item numbers, such as: "1/3/5/7 属于山西信托，日期分别是...；2/4/6 属于广联达，日期分别是..." The agent should translate that natural-language answer into `unit_updates` with `unit_nos` arrays.

Use `status: advisory` with `blocking: false` for optional refinements that should be shown in chat but must not block Excel output, such as replacing the default admin Client `项目、调研以外的其他费用` with a more specific matter name, or reviewing pure `other` items whose Date temporarily uses the invoice issue date.

Every user-facing question must be answerable without opening any file. Include:

- simple item number, such as `第1项` or `第2项`; do not expose internal IDs such as `DOC-001` or `UNIT-001` in conversation
- source filename, and when applicable both trip-report filename and invoice filename
- invoice number when available
- seller or service provider
- amount
- expense/trip date when reliable; for pure `other`, label invoice-date-derived values as provisional; for `unknown` or non-`other` unreliable dates, ask for actual occurrence date
- category and final column when known
- suggested client/code/project when available
- the specific uncertainty: project ownership, actual meal date, attendees, taxi place types, missing trip report, substitute approval screenshot, or whether to drop

For items that the model can probably allocate but should confirm, phrase the question as a proposed conclusion, for example: "I initially match this to Client X / Code Y because date and city align; please confirm or correct."

## Tracing And Correcting Recognized Items

If the user says an item is wrong, such as "第9项金额不对" or "9号不是餐费", do not ask the user to locate `DOC-001` or `UNIT-001`. Run:

```bash
python scripts/trace_expense_item.py --allocation process/expense-allocation.json --extraction process/invoice-extraction.json --item 9
```

Then answer with the source filename(s), invoice number, seller/service provider, amount, date, and trip details so the user can confirm which file it is.

After the user provides the corrected value, convert it into `allocation-answers.json` and apply it with `scripts/apply_allocation_answers.py`. Include `manual_correction: true` and a short `correction_note` when the correction changes recognized evidence such as amount, date, seller, invoice number, category, route, origin, or destination.

Example correction patch:

```json
{
  "unit_updates": [
    {
      "unit_no": 9,
      "amount": "123.45",
      "expense_date": "2026-06-09",
      "source_category": "meal",
      "final_template_column": "meal",
      "final_note": "加班餐费",
      "manual_correction": true,
      "correction_note": "User checked the source invoice and corrected the recognized amount/date."
    }
  ]
}
```

## Applying User Answers

After the user answers in natural language, Codex should parse the answer into a small JSON patch and run:

```bash
python scripts/apply_allocation_answers.py --allocation process/expense-allocation.json --answers process/allocation-answers.json
```

Use this script instead of manually editing `expense-allocation.json`. It updates allocation units, closes answered questions, keeps a backup when overwriting, and appends a change-log entry.

Answers JSON shape:

```json
{
  "unit_updates": [
    {
      "unit_no": 1,
      "status": "confirmed",
      "client_name": "",
      "client_charge_code": "",
      "admin_client_review_needed": false,
      "project_context_id": "CTX-001",
      "expense_date": "YYYY-MM-DD",
      "date_source": "user_confirmed",
      "date_is_provisional": false,
      "date_required": false,
      "city": "",
      "final_template_column": "travel",
      "final_note": "",
      "reimbursable_amount": "",
      "hotel_nights": "",
      "check_in_date": "",
      "check_out_date": "",
      "shared_room": false,
      "room_shared_with": "",
      "origin_place_type": "",
      "destination_place_type": "",
      "attendees": "",
      "is_substitute_invoice": false,
      "substitute_for": "",
      "approval_file": "",
      "answer": "Short human-readable summary of the user's answer."
    }
  ],
  "question_updates": [
    {
      "question_id": "Q-001",
      "status": "answered",
      "answer": "User confirmed this item belongs to Client X / Code Y."
    }
  ],
  "project_contexts": []
}
```

Convenience keys are also supported:

- `confirm_units`: list of user-facing item numbers or internal unit IDs to mark confirmed.
- `drop_units`: list of user-facing item numbers or internal unit IDs to drop.
- `exclude_units`: list of user-facing item numbers or internal unit IDs to exclude.

For grouped questions, translate each natural-language batch into one or more `unit_updates` using `unit_nos`, for example `{"unit_nos": [1, 3, 5, 7], "client_name": "山西信托", "client_charge_code": "CORP-2026-BD", ...}`. If the user provides different dates for each item, create separate unit updates or separate per-item entries so each `expense_date` is correct.

Prefer `unit_no` or numeric lists when translating user answers. Internal `unit_id` / `unit_ids` remain supported for scripts and process files, but they should not be shown to the user in chat.

For substitute invoices, keep `approval_file` even though it will not appear in the visible Excel sheet. Stage 3 must carry it into `final-expense-rows.json`, and Stage 4 must use it to copy the approval screenshot into the support-document folder.

## Markdown Output

Write `process/expense-allocation.md`:

```markdown
# Expense Allocation Process

Generated at: YYYY-MM-DD HH:mm
Source extraction file: process/invoice-extraction.json
Allocation units: N
Confirmed units: N
Questions remaining: N

## Project Contexts

| Context ID | Date Range | City | Client | Code | Description |
| --- | --- | --- | --- | --- | --- |

## Allocation Draft

| User No | Unit ID | Source | Date | City/Route | Amount | Category | Suggested Project | Code | Final Column | Confidence | Status |
| ---: | --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- |

## Questions For User

| Question ID | Unit(s) | Question | Why It Matters |
| --- | --- | --- | --- |

## Confirmed Notes

| Unit ID | Note |
| --- | --- |

## Substitute Invoice Tracking

| Unit ID | Substitute | Approval Required | Approval File | Note |
| --- | --- | --- | --- | --- |
```

## JSON Output

Write `process/expense-allocation.json`:

```json
{
  "schema_version": "expense_allocation.v1",
  "generated_at": "YYYY-MM-DDTHH:mm:ss",
  "source_extraction_file": "process/invoice-extraction.json",
  "project_contexts": [],
  "allocation_units": [
    {
      "unit_id": "UNIT-001",
      "user_no": 1,
      "source_document_id": "DOC-001",
      "source_file": "",
      "source_filename": "",
      "source_item_id": null,
      "supporting_invoice_file": "",
      "supporting_invoice_filename": "",
      "supporting_schedule_file": "",
      "supporting_schedule_filename": "",
      "invoice_no": "",
      "amount": "0.00",
      "invoice_amount": "0.00",
      "reimbursable_amount": "",
      "issue_date": "YYYY-MM-DD",
      "expense_date": "YYYY-MM-DD",
      "date_source": "user_confirmed",
      "date_is_provisional": false,
      "date_required": false,
      "date_question_reason": "",
      "source_category": "meal",
      "final_template_column": "travel",
      "city": "",
      "hotel_city": "",
      "hotel_city_tier": "",
      "hotel_nights": "",
      "check_in_date": "",
      "check_out_date": "",
      "shared_room": false,
      "room_shared_with": "",
      "room_share_note": "",
      "route": "",
      "origin_place_type": "",
      "destination_place_type": "",
      "place_type_confidence": "",
      "place_type_needs_confirmation": false,
      "seller_name": "",
      "project_context_id": "CTX-001",
      "client_name": "",
      "client_charge_code": "",
      "admin_client_review_needed": false,
      "expenses_nature": "out_of_town",
      "expense_note": "",
      "final_note": "",
      "attendees": "",
      "is_substitute_invoice": false,
      "substitute_for": "",
      "approval_required": "",
      "approval_file": "",
      "approval_file_status": "",
      "confidence": "medium",
      "auto_project_match": "",
      "match_reason": "",
      "status": "needs_confirmation",
      "manual_correction": false,
      "correction_note": "",
      "corrected_fields": [],
      "issues": []
    }
  ],
  "questions": [
    {
      "question_id": "Q-GROUP-001",
      "question_type": "batch_meal",
      "unit_ids": ["UNIT-001", "UNIT-003", "UNIT-005"],
      "user_nos": [1, 3, 5],
      "question": "",
      "why_it_matters": "",
      "status": "open",
      "blocking": true
    }
  ],
  "change_log": []
}
```

## Completion Criteria

Stage 2 is complete when:

- The user-provided project context has been structured and confirmed enough to allocate expenses.
- Every allocation unit has a project assignment, fixed admin assignment, or open question.
- Every included allocation unit has a reliable `expense_date`, a pure-`other` provisional invoice-date `expense_date` with an advisory review item, or a blocking open question asking the applicant for the actual date to record.
- Meals have actual date/project/attendee details when needed.
- `CORP-2026-BD` collisions are separated by client/city/date context.
- Substitute invoices are marked and screenshot requirements are recorded.
- The Markdown and JSON allocation files agree.
