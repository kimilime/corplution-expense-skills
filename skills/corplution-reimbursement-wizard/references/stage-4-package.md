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
- If distinct support files would otherwise receive the same name, keep every file and add deterministic `-2`, `-3`, and later suffixes before the extension. Never overwrite one evidence file with another.
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
- current final-rows fingerprint and workbook SHA-256
- count of reconciled applicant expense records
- SHA-256 for every copied invoice and support document
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
  "workbook_sha256": "",
  "final_rows_fingerprint": "",
  "invoice_count": 1,
  "support_count": 1,
  "expense_hint_reconciliation_count": 3,
  "invoice_files": [
    {
      "proof_no": 1,
      "filename": "001-高铁-446.00.pdf",
      "sha256": "",
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
      "sha256": "",
      "source_file": "",
      "type": "行程单"
    }
  ],
  "issues": []
}
```

The manifest is integrity-stamped after all package files are copied. A package is deliverable only when its manifest validates against the current `final-expense-rows.json`, its workbook hash, and every listed invoice/support-file hash.

Build each package in a fresh sibling staging directory. Only after its workbook, evidence folders, and stamped manifest are complete may the script replace the previous package root for the same requester/date. This replacement is deliberate: a rerun must contain exactly the files named by its new manifest and must not retain stale invoices or support files from an earlier run.

Before copying files, require final rows to carry an `expense_hint_reconciliation` list identical to the current allocation and `unresolved_expense_hint_count: 0`. Missing legacy fields, open records, and `pending_invoice`/`pending_evidence` records require Stage 2/3 regeneration. Packaging must not infer that a note can be ignored merely because no invoice row exists. A record explicitly marked `not_reimbursed` is resolved and does not require an invoice in the package.

For the optional subagent pilot, packaging resolves the current accepted Kaede result from the canonical sidecar plus the immutable `process/subagent-review-generations/` archive. A current `block` prevents packaging even if the convenience sidecar was deleted or damaged. A newer current `pass`, `advisory`, or `unavailable` result whose fingerprint was not consumed by final rows makes the workbook stale and requires Stage 3 to run again. If no valid accepted result exists for the current task, packaging continues under the deterministic checks; absence is not recorded as a pass.

On Windows, renaming an existing package can be temporarily blocked by an open workbook, Explorer preview, antivirus scan, or indexer. Retry transient `PermissionError` locks with bounded exponential backoff while preserving the staging/backup rollback. If retries are exhausted, keep the old package intact, attempt to remove staging, warn if Windows also locks cleanup, and tell the agent to close the workbook and package-folder previews before rerunning Stage 4 through Chief. Status and journal discovery ignore hidden `.staging-*`/`.previous-*` folders so an interrupted package cannot masquerade as the deliverable. Direct script invocation does not bypass the lock and must not be presented as the fix.

If `package_reimbursement_files.py` exits with code `3`, it has written a review package and manifest, but `issues` is non-empty. Treat this as a blocking stop: show the issue list in chat, obtain the missing evidence or an explicit applicant decision, then re-run Stage 4. Do not describe that package as complete or submit it.

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
- A rerun has no stale invoice or support file outside the current manifest.
- The integrity-stamped manifest matches the current final-rows fingerprint, workbook hash, file counts, and every listed package file hash.
- The packaged final rows carry the same applicant expense-record reconciliation as the current allocation, with zero unresolved records.
- No current Kaede blocker exists, and any current accepted review fingerprint was consumed by Stage 3.
- The manifest has no unresolved issues before calling the workflow complete or submitting the package.
- A concise final package summary has been shown in chat: package folder, workbook filename, and invoice/support-document counts.
