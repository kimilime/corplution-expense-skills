# Stage 4 Final Package Workflow

Use this reference after stage 3 has produced the final reimbursement workbook and `process/final-expense-rows.json`.

Stage 4 only organizes files. Do not recalculate amounts, change allocation decisions, or rewrite the workbook except to copy the final workbook into the package root.

## Inputs

Required:

- final reimbursement workbook from stage 3
- `process/final-expense-rows.json`
- `process/expense-allocation.json`
- `process/invoice-extraction.json`
- original invoice PDFs/images
- original support documents, such as Didi/Gaode trip reports and substitute approval screenshots

Optional:

- user-provided approval screenshot files
- user-provided replacement file names or corrected support files

## Package Structure

Create one package root folder:

```text
报销申请表-{Requester}-{YYYYMMDD}/
  报销申请表-{Requester}-{YYYYMMDD}.xlsx
  发票/
  支持文档/
```

Use the form completion date for `{YYYYMMDD}` unless the user asks for another date. The workbook lives directly in the package root, not inside either folder.

## Invoice Folder

Copy original invoice files into:

```text
发票/
```

Rename invoice files as:

```text
{No}-{类型}-{金额}{-专票 if applicable}.{ext}
```

Examples:

```text
001-高铁-446.00.pdf
002-酒店-499.00-专票.pdf
003-滴滴-382.60.pdf
006-餐费-86.50.pdf
007-通讯费-83.40.pdf
```

Rules:

- `{No}` is the final proof number from stage 3. Use zero-padding for filename sorting, such as `001`, while the Excel `No.` cell may remain numeric.
- `{类型}` should be the finance-facing type: `飞机`, `高铁`, `酒店`, `打车`, `滴滴`, `高德`, `餐费`, `通讯费`, or `其他`.
- `{金额}` is the invoice/proof-group amount rounded to two decimals.
- Add `-专票` only for VAT special invoices.
- Keep the original file extension.
- Copy, do not move, source files.

For Didi/Gaode:

- If several ride rows share one summary invoice, copy the invoice once using the shared proof number and total invoice amount.
- Do not create one invoice file per ride.

## Supporting Documents Folder

Copy supporting documents into:

```text
支持文档/
```

Rename support files as:

```text
{No}-{类型}.{ext}
```

Examples:

```text
003-行程单.pdf
004-高德行程单.pdf
006-替票审批.png
```

Support document types:

- `行程单` for Didi/Gaode trip reports.
- `替票审批` for partner approval screenshots.
- User-provided type for unusual support files.

Rules:

- Use the same `{No}` as the related invoice/proof group.
- If one support document supports several rows under the same proof number, copy it once.
- If a substitute invoice has no approval screenshot, keep the issue visible in the package manifest and ask the user before final delivery when possible.

## Workbook File

Copy the filled workbook into the package root as:

```text
报销申请表-{Requester}-{YYYYMMDD}.xlsx
```

Do not put the workbook in `发票/` or `支持文档/`.

## Package Manifest

Write `package-manifest.md` and `package-manifest.json` in the package root.

The manifest should list:

- workbook filename
- every invoice file copied
- every support document copied
- unresolved issues that must be shown directly to the user before submission
- proof number
- source document ID
- source item IDs when relevant
- invoice number when available
- amount
- special-invoice flag
- missing support or approval issues

JSON shape:

```json
{
  "schema_version": "reimbursement_package.v1",
  "generated_at": "YYYY-MM-DDTHH:mm:ss",
  "requester": "",
  "package_date": "YYYYMMDD",
  "workbook": "报销申请表-Requester-YYYYMMDD.xlsx",
  "invoice_files": [
    {
      "proof_no": 1,
      "filename": "001-高铁-446.00.pdf",
      "source_file": "",
      "invoice_no": "",
      "type": "高铁",
      "amount": "446.00",
      "is_special_invoice": false
    }
  ],
  "support_files": [
    {
      "proof_no": 3,
      "filename": "003-行程单.pdf",
      "source_file": "",
      "type": "行程单"
    }
  ],
  "issues": []
}
```

## Validation

Before final delivery:

- Package root exists.
- Workbook exists in package root and is named `报销申请表-{Requester}-{YYYYMMDD}.xlsx`.
- `发票/` and `支持文档/` folders exist.
- Every proof group that needs an invoice has exactly one invoice file unless the user explicitly confirms otherwise.
- Didi/Gaode trip-report support files exist for split ride rows.
- Substitute invoices have `替票审批` support files or a visible missing-approval issue.
- File names use final proof numbers and match `final-expense-rows.json`.
- Original source files were copied, not moved.
- A concise final package summary has been shown in chat: package folder, workbook filename, invoice/support-document counts, and unresolved issues.
