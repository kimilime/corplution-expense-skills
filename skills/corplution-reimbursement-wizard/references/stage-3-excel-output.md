# Stage 3 Excel Output Workflow

Use this reference after stage 2 has produced `process/expense-allocation.json`. Stage 3 converts confirmed allocation units into rows in the reimbursement Excel workbook.

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
- Every included allocation unit is confirmed or fixed.
- Open questions that affect client, charge code, final template column, amount, date, note, or proof number are resolved.
- Substitute invoices either have approval screenshot paths or explicit missing-screenshot issues.
- Didi/Gaode summary invoices linked to trip reports are not duplicated as standalone expense rows.

Units with `status: dropped`, `status: excluded`, or user-confirmed non-reimbursable status must not be written to the workbook.

## Template Columns

Write these fields:

| Template Column | Source |
| --- | --- |
| `Date (YYYYMMDD)` | `expense_date` formatted as `YYYYMMDD` |
| `Requester` | ask user if missing |
| `Client` | confirmed `client_name` |
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
- `飞机（出发地-目的地）`
- `打车（出发地类型-目的地类型）`
- `打车（出发地类型-目的地类型）（加班）`
- `出差餐费`
- `出差餐费（高铁站/机场）`
- `加班餐费`
- `出差酒店（X晚，入住日-离店日）`
- `X月通讯费`
- user-provided note for `other`

If `is_substitute_invoice: true`, append `（抵）` after the normal final note.

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
- Taxi/Didi: if a Didi trip report is linked to a Didi summary invoice, assign one proof number to the summary invoice and reuse that number for every ride row supported by that invoice.
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
      "note": "",
      "amount_column": "travel",
      "amount": "0.00",
      "proof_no": 1,
      "source_unit_id": "UNIT-001",
      "source_document_id": "DOC-001",
      "source_item_id": "",
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
  "checks": []
}
```

## Validation

Before delivering the workbook:

- Every included allocation unit appears in `final-expense-rows.json`.
- Dropped/excluded units do not appear in workbook rows.
- Each workbook row has exactly one amount column populated.
- Row totals equal the sum of included allocation units.
- Proof group totals equal the sum of rows using that proof number.
- Every project block has a subtotal row with `D = 汇总` and `F = SUM(G:L for that project's detail rows)`.
- G:L column summary formulas sum from row 3 through the final project subtotal row.
- `Total: (RMB)` equals the sum of G:L on the column summary row.
- `Grand Total: (RMB)` equals the sum of every project subtotal in column F.
- `Status` evaluates to `TRUE` only when Total equals Grand Total.
- Didi/Gaode rides are split into ride rows and share the linked invoice proof number.
- No Didi/Gaode summary invoice is also written as a duplicate row.
- `Expense Nature` follows the formal Shanghai/non-Shanghai rule.
- All substitute invoice notes include `（抵）`.
- Substitute invoice approval fields are preserved in `final-expense-rows.json` for Stage 4 packaging.
- Manual correction metadata is preserved in `final-expense-rows.json`.
- Missing approval screenshots remain visible in checks/issues.
