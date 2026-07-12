# Stage 3 Excel Output Workflow

Use this reference after stage 2 has produced `process/expense-allocation.json`. Stage 3 converts confirmed allocation units into rows in the reimbursement Excel workbook.

Before writing the workbook, `scripts/write_reimbursement_template.py` runs `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT`. This is the hard connection-point validation between allocation and the initial reimbursement table. If it exits with code `2`, no workbook was written; fix the listed allocation issues first, such as open questions, unconfirmed units, invalid categories/columns, missing dates/client/code/amount, admin/mobile conflicts, raw rail/flight evidence in final notes, or missing taxi place types.

## Inputs

Required:

- `process/expense-allocation.json`
- Requester name, supplied by the user if not already known

Optional:

- Generated-workbook layout TOML override. If omitted, use `assets/reimbursement-workbook-layout.toml`.
- Reimbursement template workbook override. If omitted, generate the workbook directly by script plus the bundled layout TOML. Use `--template bundled` only when the retained `assets/reimbursement-template.xlsx` fallback is explicitly needed.
- `process/invoice-extraction.json` for invoice/supporting-document links
- Approval screenshot paths for substitute invoices
- User confirmation for dropped or excluded items

## Blocking Checks Before Writing

Do not write the final workbook until:

- `Requester` is known.
- The extraction fingerprint and project-context SHA-256 recorded by Stage 2 still match their current inputs; otherwise rerun allocation and Composer first.
- Every included allocation unit is confirmed or fixed.
- Open questions that affect client, charge code, final template column, amount, date, note, or proof number are resolved.
- No included unit has `date_required: true`, and no included unit has a blank `expense_date`. Pure `other` rows may use a provisional invoice-date `expense_date` when `date_is_provisional: true`; this is advisory, not blocking.
- No non-mobile row uses Client `通讯费`, final column `mobile`, or a note containing `通讯费`.
- No taxi/travel row is assigned to `CORP-2026-ADMIN`. Admin is not a fallback for unmatched transport.
- Standalone flight/rail whose route destination uniquely matches a project context is assigned to that destination project, not the origin project.
- Every active railway journey chain has at least two continuous ticket segments, one shared project assignment, no open whole-chain question, and current length/member/route metadata. A dropped or corrected segment requires Stage 2 to rebuild the chain. Skip per-ticket destination-project enforcement for intermediate transfer segments; validate the chain as a whole instead.
- Substitute invoices either have approval screenshot paths or explicit missing-screenshot issues.
- Didi/Gaode summary invoices linked to trip reports are not duplicated as standalone expense rows.

Units with `status: dropped`, `status: excluded`, or user-confirmed non-reimbursable status must not be written to the workbook.

## Template Columns

Write these fields:

| Template Column | Source |
| --- | --- |
| `Date (YYYYMMDD)` | confirmed, reliable, or pure-`other` provisional `expense_date` formatted as `YYYYMMDD`; do not fall back to invoice issue date for non-`other` items |
| `Requester` | ask user if missing |
| `Client` | confirmed `client_name`; for `CORP-2026-ADMIN`, normalize mobile to `通讯费` and other missing/admin placeholders to `项目、调研以外的其他费用` |
| `Client Charge Code` | confirmed `client_charge_code` |
| `Expenses Nature` | formal local/trip rule below |
| `Note` | confirmed `final_note` from stage 2 |
| `hotel` | amount when final template column is `hotel` |
| `travel` | amount when final template column is `travel` |
| `taxi` | amount when final template column is `taxi` |
| `meal` | amount when final template column is `meal` |
| `mobile` | amount when final template column is `mobile` |
| `other` | amount when final template column is `other` |
| `No.` | overall proof number assigned in this stage |

Amounts must be numeric values. Each row must have exactly one populated amount column.

Use `reimbursable_amount` for the visible Excel amount when it is present; otherwise use `amount`. Preserve `invoice_amount` in `final-expense-rows.json`. If the reimbursable amount differs from the invoice amount, append `（发票金额XX/实际报销XX）` to the final note.

Treat project allocation, visible form classification, and meal policy as separate axes. `client_name`, `client_charge_code`, and the project block come from confirmed project allocation. `meal`/`taxi` versus `travel`, and matching `Expenses Nature`, come from formal city evidence. Meal cap policy comes only from substantive purpose in `final_note`/`meal_context`. Never derive one axis from another.

For meal rows, choose the visible amount column by formal invoice/restaurant city, not by project substance:

- Shanghai formal city -> `meal`.
- Non-Shanghai formal city -> `travel`.

A Shanghai meal invoice assigned to an out-of-town project can still use note `出差餐费`, but its amount must stay in the `meal` column and `Expense Nature` must stay local. Despite those local form fields, it remains `business_trip_meal` under the RMB 150/day policy.

For taxi/Didi/Gaode ride rows, choose the visible amount column by ride city, not by assigned project:

- Shanghai ride city -> `taxi`, even when the ride supports an out-of-town project transfer.
- Non-Shanghai ride city -> `travel`.

Date must come from Stage 2:

- flight/rail: printed travel date
- hotel: checkout date when check-in/check-out are known
- Didi/Gaode: ride datetime from the trip report
- mobile: last day of the billing month or invoice month
- meal, unknown, and taxi without trip report: user-confirmed occurrence/record date
- pure `other`: invoice issue date may be used provisionally with `date_source: other_invoice_issue_date_provisional` and a non-blocking advisory

If a non-`other` allocation only has `issue_date`, stop and ask the applicant for the actual date.

## Meal Daily Caps

Corplution has two meal daily standards:

- Business-trip meals: RMB 150 per day.
- Local overtime meals: RMB 60 per day.

Authoritative cap values live in `assets/policy.toml`; the numbers above are the current values for readability.

There is no generic "Shanghai meal", "local meal", or `amount_column=meal` RMB 60 policy. RMB 60 applies only when the item is explicitly an overtime meal. `Expense Nature: 本地` is a workbook presentation value and is not a cap-policy signal.

Keep these three axes distinct:

| Axis | Meaning | Selector | Example for a Shanghai pre-departure trip meal |
| --- | --- | --- | --- |
| `source_category` | substantive expense type / inclusion in meal checks | recognized expense type | `meal` |
| `amount_column` + `Expense Nature` | workbook form | formal invoice/restaurant city | `meal` + `本地` |
| `meal_cap_policy` | daily cap and aggregation pool | explicit `final_note` / `meal_context` | `business_trip_meal` + `150.00` |

After final rows are built and before final submission, calculate daily totals separately by meal policy.

Treat a row as a business-trip meal when:

- `source_category` is `meal`; and
- final note starts with `出差餐费`, or `meal_context` is `travel`, `business_trip`, or `station_airport`.

Treat a row as a local overtime meal when:

- `source_category` is `meal`; and
- final note starts with `加班餐费`, or `meal_context` is `overtime`.

If neither policy has an explicit signal, Stage 3 must block and ask for the meal purpose. If trip and overtime signals conflict, Stage 3 must also block. City, project, `amount_column`, `final_template_column`, and `Expense Nature` must never resolve a missing or conflicting policy.

For each date and meal policy:

- Sum the visible Excel amounts, meaning `reimbursable_amount` when present, otherwise `amount`.
- Aggregate across workbook columns. For example, a Shanghai `meal`-column trip meal and a Zhengzhou `travel`-column trip meal on the same date share one RMB 150 business-trip pool.
- If total is within the relevant cap, mark the date as OK.
- If total exceeds the relevant cap and at least one item has `attendees`, set `severity: advisory` and show a warning only: the day exceeds the standard but may be valid because multiple people are involved.
- If total exceeds the relevant cap and no item has `attendees`, ask the user directly whether the actual meal date is wrong, attendee details are missing, or one item should be partially reimbursed.
- When partial reimbursement is needed, suggest a `reimbursable_amount` adjustment that makes the day total exactly equal to the relevant cap. For example, if a local overtime meal is 70, suggest changing `reimbursable_amount` to 60; if two same-day trip meals are 90 + 90, suggest changing one item's `reimbursable_amount` to 60.
- Apply user-confirmed partial reimbursement with `scripts/apply_allocation_answers.py`, then regenerate the workbook. The final note should show the invoice/reimbursable difference automatically.

Relay the script's complete `MEAL DAILY CAP CHECK TO RELAY VERBATIM` block without independently reclassifying or recomputing it. The script writes `meal_cap_policy`, `meal_daily_cap`, and `meal_policy_basis` onto every meal row and includes the authoritative policy/day pools in `meal_daily_cap_checks`. Do not replace those fields with an inference from city or workbook column.

## Hotel Caps

Corplution's hotel standards are:

- Beijing, Shanghai, Guangzhou, Shenzhen: RMB 800 per night.
- Other cities: RMB 600 per night.

Authoritative cap values and the first-tier city list live in `assets/policy.toml`; the numbers above are the current values for readability.

After final rows are built and before final submission, calculate each hotel item's reimbursable amount per stay against the relevant cap.

Use these sources for hotel nights, in order:

- `hotel_nights`
- `check_in_date` and `check_out_date`
- `final_note` text such as `出差酒店（2晚，2026-06-10-2026-06-12）`

If nights cannot be determined, ask the user for check-in date, check-out date, and number of nights. Do not invent the night count.

For each hotel item:

- Determine city tier from `hotel_city_tier` when present; otherwise infer from `hotel_city` or `city`.
- Calculate cap total as per-night cap multiplied by nights.
- If reimbursable amount is within cap, mark the item as OK.
- If the amount exceeds cap and `shared_room`, `room_shared_with`, or `room_share_note` is present, set `severity: advisory` and show a warning only: the hotel exceeds the standard but may be valid because it was a shared standard room.
- If the amount exceeds cap and no shared-room information exists, ask whether the nights/city tier are wrong, there was a co-occupant/shared standard room, or the hotel should be partially reimbursed.
- When partial reimbursement is needed, suggest `reimbursable_amount = cap_total`.
- Apply user-confirmed partial reimbursement with `scripts/apply_allocation_answers.py`, then regenerate the workbook. The final note should show the invoice/reimbursable difference automatically.

Always copy or summarize the script's `HOTEL CAP CHECK TO SHOW IN CHAT` output in the conversation. Do not leave this only in `final-expense-rows.json/md`.

If `scripts/write_reimbursement_template.py` exits with code `3`, treat it as a policy-confirmation stop, not as a missing-output failure. The workbook and `process/final-expense-rows.*` have been written, but the `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` block contains blocking meal/hotel cap checks that must be shown to the applicant and resolved before final submission.

## Workbook Format

When no template is supplied, generate the workbook from `assets/reimbursement-workbook-layout.toml` plus script-written rows and formulas. Keep static workbook layout in TOML and keep reimbursement business logic in Python.

The bundled layout TOML defines:

- Sheet name: `工作表1`.
- Columns A:M and the same first-row instructions/header labels as the retained template.
- Column widths: A 14.832, B 10.7109, C 30.7109, D/E 15.7109, F 21.7109, G 9.71094, H:M 13.
- Row 1 height 90, light blue fill, wrapped instruction text.
- Row 2 height 48.75, bold headers, medium borders.
- Detail rows with Microsoft YaHei for Chinese text fields, Arial for Latin/numeric fields, wrapped `Client`/`Note`, amount columns formatted as money, and `No.` formatted as integer.
- Subtotal rows with gray fill, bold `汇总` and subtotal formula.
- Summary rows with bold labels/formulas.

Do not write substitute approval fields into the visible Excel table, but preserve them in `final-expense-rows.json` rows:

- `is_substitute_invoice`
- `substitute_for`
- `approval_required`
- `approval_file`
- `approval_file_status`

Stage 4 needs these fields to copy the approval screenshot into the support-document folder.

Also preserve manual correction metadata in `final-expense-rows.json`, but do not write it into the visible Excel table unless the user explicitly asks:

- `manual_correction`
- `correction_note`
- `corrected_fields`

## Project Blocks

The workbook is organized by project blocks. A project is identified by:

```text
Client + Client Charge Code
```

If two projects share the same `Client Charge Code` but have different `Client` values, treat them as separate project blocks. This is common when several pending projects use `CORP-2026-BD`.

For `CORP-2026-ADMIN`, do not group all rows under `Admin`. Treat the Client value as the admin matter name. Mobile rows should group under `通讯费 / CORP-2026-ADMIN`; other admin rows should use the specific matter name when provided, or `项目、调研以外的其他费用 / CORP-2026-ADMIN` as the non-blocking default.

For each project block:

1. Write all included detail rows for that project.
2. Sort rows inside the project by the substantive expense order below, then by expense date:
   1. Flight and railway/high-speed rail
   2. Hotel
   3. Taxi / Didi
   4. Gaode
   5. Meal
   6. Mobile / telecom
   7. Other
3. Insert one project subtotal row immediately below the project's detail rows.
4. Put `汇总` in column D of the subtotal row.
5. Put a formula in column F of the subtotal row that sums all amount cells in columns G:L for that project's detail-row range.

Example for a project whose detail rows are rows 3:5:

```text
D6 = 汇总
F6 = SUM(G3:L5)
```

Do not put project subtotal formulas in columns G:L. Project subtotal rows use column F as the project total.

## Expense Nature

Use a formal invoice/location rule:

- If the formal invoice/location city is Shanghai, write `本地`.
- Otherwise write `出差`.

This is a form-over-substance field. Do not override it merely because the business purpose feels local or travel-related.

Recommended city source by expense type:

- Didi/Gaode ride item: ride city from the trip report.
- Taxi without ride detail: invoice/service city if available; otherwise ask.
- Railway/flight: route destination or travel city. If the route leaves Shanghai or goes to a non-Shanghai city, use `出差`.
- Hotel: hotel/seller city or confirmed stay city.
- Meal: restaurant/seller city or user-confirmed actual meal city.
- Mobile: invoice/service city if available; otherwise `本地` unless user says otherwise.
- Other: ask user when the city is unclear.

## Final Notes

Use `final_note` from stage 2. It should already follow these templates:

- `高铁（出发地-目的地）`
- `高铁退票费（出发地-目的地）`
- `飞机（出发地-目的地）`
- `飞机退票费（出发地-目的地）`
- `打车（<confirmed origin place type>-<confirmed destination place type>）`
- `打车（<confirmed origin place type>-<confirmed destination place type>）（加班）`
- `出差餐费`
- `出差餐费（高铁站/机场）`
- `加班餐费`
- `出差酒店（X晚，入住日-离店日）`
- `X月通讯费`
- user-provided note for `other`

If `is_substitute_invoice: true`, append `（抵）` after the normal final note.

If `reimbursable_amount` differs from `invoice_amount`, append `（发票金额XX/实际报销XX）` after the normal final note, preserving any `（抵）` marker.

Do not write literal placeholders such as `出发地类型` or `目的地类型` into the workbook. If a taxi/Didi/Gaode row still has those words, or if a confirmed ride row has missing `origin_place_type` or `destination_place_type`, stop and ask the applicant to confirm the place types.

Do not write hotel placeholders such as `X晚`, `入住日`, or `离店日` into the workbook. If `hotel_nights`, `check_in_date`, and `check_out_date` are present, regenerate the note as `出差酒店（X晚，入住日-离店日）` with actual values. If any of those fields are missing, stop before workbook generation and ask the applicant for the missing hotel stay details.

## Overall Proof Numbering

Assign `No.` as an overall proof number by substantive proof groups, not simply by output row order and not necessarily by the tax invoice number.

Number proof groups in this order:

1. Flight and railway/high-speed rail
2. Hotel
3. Taxi / Didi
4. Gaode
5. Meal
6. Mobile / telecom
7. Other

Within each group, sort by:

1. expense date
2. client/project context
3. source document order
4. source item order

Use sequential numbers starting from `1` unless the user asks for another format.

### Proof Group Rules

- Flight/rail: one proof number per ticket/invoice.
- Hotel: one proof number per hotel invoice.
- Taxi/Didi/Gaode: if a trip report is linked to a summary invoice, assign one proof number to the summary invoice and reuse that number for every ride row supported by that invoice.
- Gaode: split to one row per ride when a trip report exists, but reuse the same proof number for every ride supported by the same Gaode invoice.
- Meal: one proof number per meal invoice unless the user explicitly groups substitute invoices.
- Mobile: one proof number per mobile invoice.
- Other: one proof number per invoice or user-confirmed proof group.

If a trip report exists without a matching invoice, ask whether to drop it or wait for the invoice. If the user confirms it should not be reimbursed, mark it dropped and do not number it.

If an invoice exists without the required Didi/Gaode trip report, ask the user to provide the trip report or confirm summary-level handling before numbering.

## Row Ordering

The workbook rows should remain useful for finance review:

1. Group rows by confirmed client/project identity.
2. Within each project, sort by the substantive expense order: flight/rail, hotel, taxi/Didi, Gaode, meal, mobile, other.
3. Within each expense type, sort by date ascending.
4. Preserve the assigned `No.` even if proof-number order differs from row order.

The `No.` field is a proof index; it does not have to determine the row order.

## Summary Formulas

After all project blocks, write the workbook summary rows.

Let:

- `first_detail_row` = 3
- `last_project_subtotal_row` = the row containing the final project subtotal
- `column_summary_row` = the next row after the final project subtotal
- `total_row` = `column_summary_row + 1`
- `grand_total_row` = `column_summary_row + 2`
- `status_row` = `column_summary_row + 3`

### Per-Project Subtotal Rows

For each project subtotal row:

```text
D{subtotal_row} = 汇总
F{subtotal_row} = SUM(G{project_first_detail_row}:L{project_last_detail_row})
```

If a project has only one detail row, use the same formula pattern with a one-row range:

```text
F{subtotal_row} = SUM(G8:L8)
```

### Column Summary Row

In the row after all project blocks, write formulas in G:L:

```text
G{column_summary_row} = SUM(G3:G{last_project_subtotal_row})
H{column_summary_row} = SUM(H3:H{last_project_subtotal_row})
I{column_summary_row} = SUM(I3:I{last_project_subtotal_row})
J{column_summary_row} = SUM(J3:J{last_project_subtotal_row})
K{column_summary_row} = SUM(K3:K{last_project_subtotal_row})
L{column_summary_row} = SUM(L3:L{last_project_subtotal_row})
```

Because project subtotal values live in column F, summing G:L from row 3 through the final project subtotal row is safe: blank G:L cells on subtotal rows do not affect the result.

### Total Row

Use the row below the column summary row:

```text
E{total_row} = Total: (RMB)
F{total_row} = SUM(G{column_summary_row}:L{column_summary_row})
```

### Grand Total Row

Use the row below `Total: (RMB)`:

```text
E{grand_total_row} = Grand Total: (RMB)
F{grand_total_row} = SUM(F{subtotal_row_1},F{subtotal_row_2},...)
```

If the spreadsheet library makes comma-separated subtotal references awkward, use an equivalent sum over the full F range that only contains numeric values in project subtotal rows and final summary rows are excluded. Prefer explicit subtotal references when possible.

### Status Row

Use the row below `Grand Total: (RMB)`:

```text
E{status_row} = Status
F{status_row} = F{total_row}=F{grand_total_row}
```

The status cell must evaluate to `TRUE` when the column-summary total equals the sum of project subtotal totals, otherwise `FALSE`.

## Output Artifacts

Create or update:

- final reimbursement workbook `.xlsx`
- `process/final-expense-rows.md`
- `process/final-expense-rows.json`

`final-expense-rows.json` should include:

```json
{
  "schema_version": "final_expense_rows.v1",
  "generated_at": "YYYY-MM-DDTHH:mm:ss",
  "requester": "",
  "source_allocation_file": "process/expense-allocation.json",
  "workbook_source": "generated",
  "template_workbook": "",
  "layout_file": "assets/reimbursement-workbook-layout.toml",
  "proof_groups": [
    {
      "proof_no": 1,
      "proof_group_id": "PROOF-001",
      "proof_type": "taxi_didi",
      "source_document_ids": ["DOC-001"],
      "source_item_ids": ["DOC-002-ITEM-001", "DOC-002-ITEM-002"],
      "source_invoice_no": "",
      "amount_total": "0.00"
    }
  ],
  "rows": [
    {
      "date": "YYYYMMDD",
      "requester": "",
      "client": "",
      "client_charge_code": "",
      "expenses_nature": "本地",
      "note": "出差餐费（上海出发前）",
      "amount_column": "meal",
      "amount": "0.00",
      "invoice_amount": "0.00",
      "reimbursable_amount": "0.00",
      "proof_no": 1,
      "user_no": 1,
      "source_unit_id": "UNIT-001",
      "source_document_id": "DOC-001",
      "source_item_id": "",
      "source_category": "meal",
      "source_filename": "",
      "supporting_invoice_filename": "",
      "issue_date": "YYYY-MM-DD",
      "date_source": "user_confirmed",
      "date_is_provisional": false,
      "date_required": false,
      "seller_name": "",
      "attendees": "",
      "meal_context": "business_trip",
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
      "meal_cap_policy": "business_trip_meal",
      "meal_daily_cap": "150.00",
      "meal_policy_basis": [
        "final_note starts with 出差餐费",
        "meal_context=business_trip"
      ],
      "meal_policy_error": "",
      "hotel_city": "",
      "hotel_city_tier": "",
      "hotel_nights": "",
      "check_in_date": "",
      "check_out_date": "",
      "shared_room": false,
      "room_shared_with": "",
      "room_share_note": "",
      "is_substitute_invoice": false,
      "substitute_for": "",
      "approval_required": "",
      "approval_file": "",
      "approval_file_status": "",
      "manual_correction": false,
      "correction_note": "",
      "corrected_fields": []
    }
  ],
  "rail_journey_chains": [],
  "project_blocks": [
    {
      "project_key": "Client|Client Charge Code",
      "client": "",
      "client_charge_code": "",
      "first_detail_row": 3,
      "last_detail_row": 5,
      "subtotal_row": 6,
      "subtotal_formula": "SUM(G3:L5)"
    }
  ],
  "summary_rows": {
    "column_summary_row": 20,
    "total_row": 21,
    "grand_total_row": 22,
    "status_row": 23
  },
  "meal_policy_rule": {
    "population_selector": "source_category=meal",
    "cap_selector": "final_note/meal_context substantive purpose",
    "not_cap_selectors": [
      "city",
      "amount_column",
      "final_template_column",
      "expenses_nature"
    ],
    "cross_column_aggregation": "Same-date rows with the same meal_cap_policy are summed together even when one is in meal and another is in travel.",
    "critical_invariant": "A Shanghai meal-column/local-nature row with Note=出差餐费 remains business_trip_meal at RMB 150/day; only explicit 加班餐费 uses RMB 60/day."
  },
  "meal_daily_cap_checks": [
    {
      "policy": "business_trip_meal",
      "policy_name": "出差餐费",
      "date": "YYYYMMDD",
      "cap": "150.00",
      "aggregation_key": "meal_cap_policy + expense_date",
      "cross_column_aggregation": true,
      "policy_basis": [
        "final_note starts with 出差餐费",
        "meal_context=business_trip"
      ],
      "policy_non_triggers": [
        "city",
        "amount_column",
        "final_template_column",
        "expenses_nature"
      ],
      "total": "0.00",
      "over_by": "0.00",
      "status": "未超标",
      "severity": "ok",
      "advisory": false,
      "has_attendees": false,
      "requires_user_confirmation": false,
      "items": [],
      "suggested_adjustments": []
    }
  ],
  "hotel_cap_checks": [
    {
      "policy": "hotel_cap",
      "policy_name": "酒店（北上广深）",
      "city_tier": "first_tier",
      "city": "上海",
      "date": "YYYYMMDD",
      "check_in_date": "YYYY-MM-DD",
      "check_out_date": "YYYY-MM-DD",
      "nights": 1,
      "cap_per_night": "800.00",
      "cap_total": "800.00",
      "total": "0.00",
      "over_by": "0.00",
      "status": "未超标",
      "severity": "ok",
      "advisory": false,
      "has_shared_room": false,
      "requires_user_confirmation": false,
      "items": [],
      "suggested_adjustments": []
    }
  ],
  "checks": [
    {
      "name": "meal_daily_caps",
      "caps": {
        "business_trip_meal": "150.00",
        "local_overtime_meal": "60.00"
      },
      "status": "ok",
      "days_checked": 0,
      "days_with_advisory": 0,
      "days_requiring_confirmation": 0
    },
    {
      "name": "hotel_caps",
      "caps": {
        "first_tier_city_per_night": "800.00",
        "other_city_per_night": "600.00"
      },
      "status": "ok",
      "items_checked": 0,
      "items_with_advisory": 0,
      "items_requiring_confirmation": 0
    }
  ]
}
```

## Validation

Before delivering the workbook:

- Every included allocation unit appears in `final-expense-rows.json`.
- Dropped/excluded units do not appear in workbook rows.
- Every workbook row has a confirmed, reliable, or pure-`other` provisional Date. No non-`other` row uses invoice issue date merely because the actual occurrence date was missing.
- Each workbook row has exactly one amount column populated.
- Row totals equal the sum of included allocation units' reimbursable amounts.
- Proof group totals equal the sum of rows using that proof number.
- Every project block has a subtotal row with `D = 汇总` and `F = SUM(G:L for that project's detail rows)`.
- G:L column summary formulas sum from row 3 through the final project subtotal row.
- `Total: (RMB)` equals the sum of G:L on the column summary row.
- `Grand Total: (RMB)` equals the sum of every project subtotal in column F.
- `Status` evaluates to `TRUE` only when Total equals Grand Total.
- Didi/Gaode rides are split into ride rows and share the linked invoice proof number.
- No Didi/Gaode summary invoice is also written as a duplicate row.
- No taxi/travel/meal/hotel row falls back to Client `通讯费`, mobile column, or `CORP-2026-ADMIN`.
- Meal amount columns follow the formal invoice/restaurant city rule: Shanghai -> `meal`; non-Shanghai -> `travel`.
- Taxi/Didi/Gaode amount columns follow the ride city rule: Shanghai -> `taxi`; non-Shanghai -> `travel`.
- Taxi/Didi/Gaode final notes do not contain literal `出发地类型` or `目的地类型` placeholders.
- Hotel final notes do not contain literal `X晚`, `入住日`, or `离店日` placeholders.
- Standalone flight/rail between two project cities belongs to the destination/project being traveled to.
- Connected railway tickets retain separate rows/proofs/Notes but share one project assignment. Adjacent stations must still connect, and a transfer station must not split the chain into another project.
- `Expense Nature` follows the formal Shanghai/non-Shanghai rule.
- All substitute invoice notes include `（抵）`.
- Meal daily cap checks are present and have been shown in chat. Any over-cap day with attendees is advisory only; any over-cap day without attendees must be resolved or explicitly acknowledged before final submission.
- Hotel cap checks are present and have been shown in chat. Any over-cap hotel with shared-room/co-occupant details is advisory only; missing nights/city tier or any over-cap hotel without shared-room details must be resolved or explicitly acknowledged before final submission.
- Substitute invoice approval fields are preserved in `final-expense-rows.json` for Stage 4 packaging.
- Manual correction metadata is preserved in `final-expense-rows.json`.
- Missing approval screenshots remain visible in checks/issues.
