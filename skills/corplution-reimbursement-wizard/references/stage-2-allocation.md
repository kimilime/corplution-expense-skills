# Stage 2 Project Allocation Workflow

Use this reference after stage 1 has produced `process/invoice-extraction.json`. Stage 2 maps extracted invoice evidence to projects, clients, charge codes, cities, and final reimbursement-template columns through LLM-assisted matching and user confirmation.

Do not fill the Excel reimbursement template in this stage. Produce allocation process files for the next stage.

## Contents

- [Inputs](#inputs)
- [User Project Context](#user-project-context)
- [Project Context Model](#project-context-model)
- [Allocation Units](#allocation-units)
- [Matching Method](#matching-method)
- [Expense-Type Rules](#expense-type-rules)
- [Final Note Format](#final-note-format)
- [Interaction Loop](#interaction-loop)
- [Applying User Answers](#applying-user-answers)
- [JSON Output](#json-output)
- [Completion Criteria](#completion-criteria)

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
  - optional structured `meal_hints` / `expense_hints` parsed by the agent from the user's natural-language notes
- For meals when known:
  - actual meal date
  - client/project
  - attendees or dining counterparties
  - substantive purpose: business-trip meal (including a Shanghai meal before/during travel) or explicit local overtime meal
  - formal restaurant/invoice city, which separately determines the workbook column and `Expense Nature`
  - any "with X", "和X一起", "同事X", or similar companion/counterparty note, even if the meal is under the daily cap
- Substitute invoice notes:
  - which invoice/item is a substitute invoice
  - what it substitutes for
  - whether partner approval screenshot is available

Do not require a rigid form. Parse the user's natural language into structured context and show a concise draft for confirmation when the context is ambiguous.

Ask missing information in the current chat, not by asking the user to open process files. The Markdown and JSON outputs are internal audit/process artifacts for the agent; the user-facing loop must happen in conversation.

## Project Context Model

Write the agent-created `project-context.json` in exactly the `project_context.v1` root shape below. The user supplies business facts in natural language; the agent writes this JSON. Do not use aliases such as root `projects`, `charge_code`, or free-form `notes`.

```json
{
  "schema_version": "project_context.v1",
  "project_contexts": [
    {
      "context_id": "CTX-001",
      "date_start": "<YYYY-MM-DD>",
      "date_end": "<YYYY-MM-DD>",
      "city": "<project city>",
      "client_name": "<client name>",
      "client_charge_code": "<Client Charge Code>",
      "project_description": "",
      "user_notes": "",
      "travel_buffer_days": 1,
      "status": "draft",
      "meal_hints": [],
      "expense_hints": []
    }
  ]
}
```

Use `assets/project-context-template.json` as the source structure. Create one context object for each distinct travel/project date window. Repeating the same Client and Code across several date windows is valid; final workbook blocks still merge by `client_name + client_charge_code`. Allocation rejects malformed, empty, placeholder-filled, or alias-based contexts before creating any expense units.

Project identity is composite:

```text
client_name + city + date_start/date_end + client_charge_code + project_description
```

Never use `client_charge_code` alone as the project key. Multiple distinct projects may share `CORP-2026-BD` while belonging to different clients.

When the user provides concrete meal or expense notes, translate them into structured hints inside the relevant project context whenever possible:

```json
{
  "meal_hints": [
    {
      "date": "YYYY-MM-DD",
      "amount": "117.00",
      "merchant": "德克士",
      "merchant_aliases": ["Dicos"],
      "attendees": "姚",
      "meal_context": "business_trip"
    }
  ],
  "expense_hints": [
    {
      "source_category": "meal",
      "date": "YYYY-MM-DD",
      "amount": "117.00",
      "attendees": "姚"
    }
  ]
}
```

The allocation script scores these hints against extracted units using amount, date, merchant text, and optional city. Treat these fields as evidence, not strict filters:

- Amount can be exact or approximate. A small difference may come from delivery fees, platform discounts, rounding, or a user memory error.
- Merchant can be brand/store text supplied by the applicant even when the invoice seller is a franchisee, platform merchant, or individual business.
- Date is the actual meal/expense date from the applicant's note. For ordinary meal invoices, invoice issue date is only weak evidence and must not replace the actual meal date.
- Auto-apply a hint only when one extracted unit is the unique high-confidence match. If several units have similar evidence, show the candidates in the chat and ask the user to confirm by item number.

Use hints to preserve attendee details even when a meal does not exceed the cap.

Do not let a meal hint override the formal amount column or `Expense Nature`. For meal invoices, `final_template_column` and nature follow the invoice/restaurant city: Shanghai formal city -> `meal`/local; non-Shanghai formal city -> `travel`/business trip. These are workbook-form fields only and never select the meal cap. A Shanghai invoice can belong to an out-of-town project, use note `出差餐费`, remain in the `meal` column with local nature, and still use the RMB 150/day business-trip policy. `final_template_column` is computed and re-normalized on every apply — it cannot be set through the answers file; to change a column, correct `city` or `source_category` instead.

## Allocation Units

Create allocation units from stage-1 data:

- For ordinary invoices, hotel invoices, railway e-ticket invoices, meal invoices, mobile invoices, and other single-document invoices: one allocation unit per invoice.
- For Didi/Gaode trip reports: one allocation unit per `supporting_items[]` ride.
- For Didi/Gaode summary invoices linked to trip reports: use them as supporting evidence, but do not create duplicate allocation units from the summary invoice.
- For unmatched Didi/Gaode summary invoices without trip reports: create one allocation unit, mark it `needs_user_confirmation: true`, and ask for trip details or whether to use summary-level allocation.

Each allocation unit should carry:

- source document ID and optional item ID
- source file path and user-facing source filename
- invoice number if available
- source category from stage 1
- amount
- formal invoice issue date when available
- reliable expense date or date range, plus `date_source` and `date_required`
- city, origin, destination, route, seller, and note evidence
- railway leg fields when applicable, plus `journey_chain_id`, ordered position, whole-chain route, confidence, assignment rule, and whole-chain confirmation status after transfer detection
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

- Candidate filter: for hotel, meal, taxi, Didi/Gaode, railway, and flight auto-matching, exclude `CORP-2026-ADMIN` contexts before city/date scoring. Admin is not a Shanghai project.
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
- Meal amount column and `Expense Nature` are form-over-substance: decide `meal`/local versus `travel`/business trip by the invoice/restaurant city, not by the assigned project. Shanghai meal invoices go to `meal`/local even when allocated to a business-trip project; non-Shanghai meal invoices go to `travel`/business trip.
- Taxi/Didi/Gaode: allocate to the project journey the ride supports. Ordinary non-local city rides may match by city/date. Shanghai/base-city rides must not be auto-assigned to a Shanghai local project merely because city and date match; require explicit client/project evidence in the ride endpoint, route note, user note, or context keyword. Airport/station transfers belong to the upcoming destination project when the next project starts within the travel buffer. Transfers from one project city to a station/airport for the next city belong to the project being traveled to.
- Taxi/Didi/Gaode amount column and `Expense Nature` are also form-over-substance: decide `taxi`/local versus `travel`/business trip by ride city, not by the assigned project. A Shanghai ride to an airport or railway station can belong to an out-of-town project while staying in the `taxi` column.
- Railway: before per-ticket matching, group connected ticket segments into a journey chain when travel dates/times are ordered and each intermediate destination station/city matches the next origin station/city. Allocate the chain as one journey; do not treat a transfer station as a separate project destination.
- Standalone flight/rail: match by route destination and travel date, allowing a reasonable +/- 1 day project buffer.
- For taxi/ride transfers, do not require the ride city to equal the project city when the ride is clearly to/from an airport or railway station. A Shanghai ride to Hongqiao station/airport can belong to the out-of-town destination project if it supports that journey.
- Never assign unmatched taxi/travel/hotel/meal to `CORP-2026-ADMIN`, Client `通讯费`, or the mobile amount column. If transfer logic does not identify a project, ask the user.
- Other and unknown: do not pre-match by invoice city. Ask the user for accounting note and project/admin matter because issuer city can be misleading for SaaS, online meeting, association, platform, or generic service expenses. For pure `other`, temporarily use the invoice issue date as Date and advise the user to confirm/correct it; for `unknown`, ask for the actual date until reclassified.

Record deterministic pre-allocation in `auto_project_match` with values such as `hotel_stay_dates`, `hotel_unique_city`, `unique_non_shanghai_city`, `taxi_explicit_project_evidence`, `taxi_transfer_to_next_project`, `taxi_transfer_matches_travel_unit`, `taxi_city_date`, `rail_transfer_chain_destination`, `rail_transfer_chain_return`, `travel_destination_date`, or `travel_route_date`, and explain the basis in `match_reason`.

## Expense-Type Rules

### Taxi, Didi, And Gaode

Use ride-level rows when Didi/Gaode trip reports exist.

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
- If there is a matched flight/rail item within +/- 1 day of an airport/station taxi, inherit that travel item's project when unique.
- If no travel document is available, but the ride is an airport/station transfer and exactly one non-local/out-of-city project context is active or within the travel buffer, assign the taxi to that project even if the ride city is Shanghai and the project city is elsewhere.
- If there is no travel document and two projects can both explain the ride, mark medium confidence and ask in the batch taxi question.

For Shanghai/local projects such as KEEWAY:

- Do not treat a Shanghai project as a default bucket for Shanghai taxi or travel items.
- Auto-assign to the local project only when the ride endpoint, source note, user note, `client_name`, alias, or `local_match_keywords` explicitly names that project/client, such as `家 -> KEEWAY集团` or `KEEWAY -> 虹桥机场`.
- Do not auto-assign `家/公司 -> 虹桥站/机场` to the local project unless the local client is explicitly named. These rides usually support an out-of-town project; inherit the adjacent flight/rail project when unique, otherwise ask.
- A train/flight returning to Shanghai is not automatically a Shanghai local project cost. It usually belongs to the out-of-town project just completed unless the user explicitly says it is for the local project.

If a ride city/date does not match any provided trip/project, ask the user. The answer may be a local overtime taxi, a missing project context, personal/non-reimbursable, or another business reason.

Final template column is form-over-substance by ride city, not by assigned project:

- Shanghai ride city -> `taxi`, even when the ride is assigned to an out-of-town project as an airport/station transfer.
- Non-Shanghai ride city -> `travel`.

Final note:

- Normal taxi: `打车（<confirmed origin place type>-<confirmed destination place type>）`
- Overtime taxi: `打车（<confirmed origin place type>-<confirmed destination place type>）（加班）`

Determine `origin_place_type` and `destination_place_type` before finalizing the note. If either type is uncertain, ask the user. Never write the literal placeholders `出发地类型` or `目的地类型` into `final_note` or the workbook.

### Railway And Flight Travel

Match by route, destination city, and travel date. Travel is usually high-confidence when the destination city and travel date align with a project city/date range, allowing a reasonable +/- 1 day buffer.

Before applying that rule ticket by ticket, build railway journey chains:

- Same-day rail tickets, or tightly connected cross-date tickets with usable times, form a chain when the earlier destination station matches the later origin station. Treat directional station variants in the same city, such as `周口东` and `周口`, as connectable when the remaining evidence is consistent.
- `上海虹桥 -> 周口东` plus `周口东 -> 郑州东` is one journey to Zhengzhou. Both tickets belong to the Zhengzhou project even when Zhoukou also has a project context; Zhoukou is only a transfer unless there is evidence of actual work, lodging, or a deliberate long stop there.
- Local/home -> project: assign every segment to the terminal/upcoming project.
- Project A -> project B: assign every segment to project B, the project being traveled to.
- Project -> local/home: assign every segment to the project just completed.
- Preserve each ticket as a separate allocation unit, proof, amount, and final Note such as `高铁（上海虹桥-周口东）`; share only `journey_chain_id` and project assignment.
- Show a resolved chain once as an advisory review item. Do not ask about each intermediate destination. Ask one blocking whole-chain question only if station continuity/time order is ambiguous, the whole chain has multiple plausible projects, or the applicant says an intermediate city was an actual stop.
- When the applicant corrects a chain assignment, include every displayed item number in one Composer decision. The updater clears the whole-chain gate only when all active legs share the same Client, Code, and project context; a partial or split update remains blocking.

When travel connects two project cities, assign it to the project being traveled to. Return travel without a following project usually belongs to the project just completed. If route direction is unclear, ask in the travel batch question. Do not assign a train/flight to the origin project merely because the departure station city matches it.

Final template column: `travel`.

Final note:

- High-speed rail or train: `高铁（出发地-目的地）`
- Flight: `飞机（出发地-目的地）`
- High-speed rail/train refund or cancellation fee: `高铁退票费（出发地-目的地）`
- Flight refund or cancellation fee: `飞机退票费（出发地-目的地）`

Use city or station/airport names from the route. Do not include train number, flight number, or seat in the final note unless the user asks; keep those details in evidence fields if needed. If the invoice text, line item, or remarks include `退票`, `退票费`, `退款`, `refund`, or cancellation wording, use the refund-fee note template.

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

Also ask for shared-room/co-occupant details when a hotel may exceed the per-night standard. Stage 3 applies hotel caps after final rows are built (per-night amounts and the first-tier city list are defined in `assets/policy.toml`; currently first-tier cities are RMB 800 per night, other cities RMB 600).

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
- whether its substantive purpose is business-trip meal (including a Shanghai pre-departure/station/airport meal) or explicit local overtime meal; never infer this from invoice city
- whether it is a substitute invoice
- whether actual reimbursable amount differs from invoice amount, when the user says a meal should be partially reimbursed to meet the daily standard

Final template column:

- Shanghai formal invoice/restaurant city -> `meal`
- Non-Shanghai formal invoice/restaurant city -> `travel`

This is form-over-substance. A Shanghai meal invoice allocated to a non-Shanghai business-trip project still uses `meal`; a non-Shanghai meal invoice uses `travel`.

Final note:

- Business-trip meal, regardless of whether formal city puts it in `meal` or `travel`: `出差餐费`
- Shanghai meal specifically tied to station/airport travel context: `出差餐费（高铁站/机场）`
- Explicit local overtime meal: `加班餐费`

These notes also select the downstream cap policy: either `出差餐费` form -> `business_trip_meal` at RMB 150/day, or explicit `加班餐费` -> `local_overtime_meal` at RMB 60/day. There is no generic local/Shanghai meal RMB 60 policy. Same-date business-trip meal rows are one 150 pool even when Shanghai rows appear in `meal` and non-Shanghai rows appear in `travel`.

Carry attendee details in a separate `attendees` field for downstream use. Include attendees in the final note only if the user explicitly wants that style.

If the user already provided attendee/counterparty details in the project notes, preserve them in `attendees` even when the daily cap is not exceeded. The cap check is not the only trigger for recording attendees.

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

`CORP-2026-ADMIN` is not a fallback bucket. Taxi, Didi/Gaode, railway, flight, hotel, and meal expenses must not be assigned to `CORP-2026-ADMIN` or Client `通讯费` unless the source category is genuinely mobile/telecom. If a transport item cannot be matched, keep it in the blocking question queue.

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
| High-speed rail refund/cancellation fee | `高铁退票费（出发地-目的地）` |
| Flight | `飞机（出发地-目的地）` |
| Flight refund/cancellation fee | `飞机退票费（出发地-目的地）` |
| Taxi / Didi / Gaode | `打车（<confirmed origin place type>-<confirmed destination place type>）` |
| Overtime taxi | `打车（<confirmed origin place type>-<confirmed destination place type>）（加班）` |
| Out-of-town meal | `出差餐费` |
| Shanghai station/airport meal | `出差餐费（高铁站/机场）` |
| Overtime meal | `加班餐费` |
| Hotel | `出差酒店（X晚，入住日-离店日）` |
| Mobile | `X月通讯费` |
| Other | User-provided note |

Do not use the verbose evidence note from stage 1 as the final reimbursement note. Stage 1 notes are evidence; stage 2 final notes are accounting-facing template text.

## Place Type Classification For Taxi Notes

Classify taxi/Didi/Gaode origin and destination into place types:

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
8. Translate the user's answers into canonical `allocation_decisions.v1`, run `scripts/compose_answers.py`, then apply the published answers with `scripts/apply_allocation_answers.py` to regenerate `expense-allocation.md/json`.
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

After the user provides the corrected value, encode it in `allocation_decisions.v1`, run Composer, and apply the published answers. Include `manual_correction: true` and a short `correction_note` when the correction changes recognized evidence such as amount, date, seller, invoice number, category, route, origin, or destination.

Example correction decision:

```json
{
  "schema_version": "allocation_decisions.v1",
  "decisions": [
    {
      "units": "9",
      "set": {
        "amount": "123.45",
        "expense_date": "2026-06-09",
        "source_category": "meal",
        "final_note": "加班餐费",
        "manual_correction": true,
        "correction_note": "User checked the source invoice and corrected the recognized amount/date."
      }
    }
  ],
  "question_updates": [],
  "project_contexts": [],
  "confirm_units": [],
  "drop_units": [],
  "exclude_units": []
}
```

## Applying User Answers

After the user answers in natural language, write a UTF-8 decisions file using `assets/allocation-decisions-template.json` as the exact root structure. Do not ask the user to create it.

Canonical decisions shape:

```json
{
  "schema_version": "allocation_decisions.v1",
  "decisions": [
    {
      "units": "1,3,5-7",
      "set": {
        "status": "confirmed",
        "client_name": "<real client name>",
        "client_charge_code": "<real charge code>",
        "expense_date": "<YYYY-MM-DD>",
        "final_note": "<final reimbursement note>"
      }
    }
  ],
  "question_updates": [],
  "project_contexts": [],
  "confirm_units": [],
  "drop_units": [],
  "exclude_units": []
}
```

`decisions[].units` accepts one displayed item number, comma-separated numbers, or ranges. `decisions[].set` accepts canonical updater fields plus Composer aliases such as `client`, `code`, `note`, `date`, `nights`, `checkin`, `checkout`, `attendee`, `origin_type`, and `destination_type`.

The root action arrays are also supported:

- `confirm_units`: list of user-facing item numbers or internal unit IDs to mark confirmed.
- `drop_units`: list of user-facing item numbers or internal unit IDs to drop.
- `exclude_units`: list of user-facing item numbers or internal unit IDs to exclude.
- `question_updates`: explicit question status/answer updates.
- `project_contexts`: controlled context additions/updates.

Run Composer, then apply only its published output:

```bash
python scripts/chief_orchestrator.py run compose --decisions process/batch-decisions.json
python scripts/chief_orchestrator.py run apply
```

Composer resolves the current displayed `user_no` values, binds the current allocation fingerprint, validates text and fields, and calls the updater in dry-run mode before atomically publishing `process/allocation-answers.json`. The updater remains the sole writer of `expense-allocation.json`; it refreshes derived hotel/taxi/rail/flight Notes, closes questions, retains a backup, and appends change history.

If Composer or its updater dry-run fails, correct the same UTF-8 decisions file and rerun Composer. Never generate `fill_answers.py`, write a batch helper, fill a diagnostic answers template, create a patch/fix script, edit a process JSON, or modify a bundled script. `build_allocation_answers_template.py` is developer-only diagnostic output and its schema is intentionally rejected by the updater.

For grouped questions, translate each natural-language batch into one or more `decisions` entries. If the user provides different dates for each item, create separate decision entries so each `expense_date` is correct.

Prefer displayed numeric item selectors when translating user answers. Internal `unit_id` values remain supported only in control action arrays and process files; do not show them to the user in chat.

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
      "train_no": "",
      "origin_station": "",
      "destination_station": "",
      "rail_departure_time": "",
      "rail_departure_datetime": "",
      "journey_chain_id": "",
      "journey_chain_route": "",
      "journey_chain_position": "",
      "journey_chain_length": "",
      "journey_chain_unit_ids": [],
      "journey_chain_confidence": "",
      "journey_chain_assignment_rule": "",
      "journey_chain_match_reason": "",
      "journey_chain_project_context_id": "",
      "journey_chain_needs_confirmation": false,
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
