# Close Message Contract

Use this reference only after Stage 4 exits with code `0`, the promoted package manifest is integrity-valid, and `issues` is empty. The package script prints one `CLOSE MESSAGE TO SHOW IN CHAT (relay verbatim)` block sourced from the stamped manifest. Relay that block verbatim as the workflow's final user-facing message; do not replace it with a generic “已完成”.

If Stage 4 exits nonzero or the manifest contains issues, do not emit a success Close Message. Show the blocking package issues and follow `CHIEF NEXT` instead.

## Required Content

The successful Close Message contains:

1. A short completion sentence.
2. A summary table with package path, workbook filename, packaged invoice count, packaged support-file count, total reimbursement amount, omitted allocation-unit count, and user expense-records explicitly marked not reimbursed.
3. Every row whose reimbursable amount differs from its invoice amount, showing original amount, final amount, and the recorded decision reason.
4. Every remaining non-blocking meal/hotel policy advisory.
5. Every excluded source file, inactive/zero-reimbursement allocation unit, and `not_reimbursed` applicant expense record, each with its recorded reason.
6. One project summary row per distinct `Client + Client Charge Code`, with hotel, transport, meal, other, and grand totals.
7. The closing sentence `如有疑问或需要修改，请继续对话。`

Omit an empty detail section instead of inventing examples. Keep zero counts in the top summary table so the user can distinguish “none” from “not checked”.

## Source Of Truth

Use only the current package manifest's integrity-stamped `close_summary`; never reconstruct the final answer from conversation memory or approximate workbook totals. Its data comes from the current generation of:

- `final-expense-rows.json`: final rows, invoice/reimbursable amounts, policy checks, and applicant-record reconciliation;
- `expense-allocation.json`: inactive units and recorded Composer/Updater decision reasons;
- `invoice-extraction.json`: excluded evidence and exclusion reasons;
- the package manifest: the files actually copied into `发票/` and `支持文档/`.

Do not call an amount reduction “超标调整” unless the recorded decision says so. Describe it neutrally as an amount adjustment and preserve the recorded reason. Do not invent a reason when the workflow failed to persist one; show `未记录具体原因` so the audit gap remains visible.

`not_reimbursed` applicant records originate in the reverse reconciliation queue for notes that had no unique evidence match. Report them under `用户记录无票/无唯一凭证不报` and preserve the applicant's exact resolution text.

## Manifest Shape

The additive `close_summary` object uses schema `reimbursement_close_summary.v1` and contains:

- packaged/excluded/omitted counts and `grand_total`;
- `amount_adjustments`;
- `policy_advisories`;
- `excluded_evidence`;
- `omitted_units`;
- `not_reimbursed_records`;
- `projects`, including per-category counts and totals.

Because `close_summary` is inside the stamped package manifest, manual edits invalidate package integrity. Regenerate Stage 4 instead of editing the summary.

## Example Shape

The exact rows vary with the evidence. A typical successful message resembles:

```markdown
报销资料已整理、校验并完成打包。以下是本次工作的最终摘要。

| 项目 | 内容 |
| :--- | :--- |
| **包路径** | `output/报销申请表-申请人-20260721` |
| **Excel** | `报销申请表-申请人-20260721.xlsx` |
| **发票** | 53 张（另排除 3 张） |
| **支持文件** | 5 份 |
| **报销总额** | ¥12,345.67 |
| **未纳入费用项** | 2 项 |
| **用户记录未报销** | 5 条 |

### 已处理的金额与政策事项

- **2026-07-02｜餐费｜北京盒马** ¥118.70 → ¥114.30：当日餐费按标准调整

### 未纳入本次报销

- **排除文件** duplicate.pdf / ¥88.00：重复发票
- **用户记录无票/无唯一凭证不报** 6.23 水果 ¥25.00：未提供发票，本次不报销

### 4 个项目汇总

| Client Charge Code | 酒店 | 交通 | 餐费 | 其他 | 合计 |
| :--- | ---: | :--- | ---: | :--- | ---: |
| CORP-2026-0035 千味央厨 | 酒店 ¥4,875.00（3项） | 高铁 ¥3,149.50（4项） + 打车 ¥1,136.80（12项） | 餐费 ¥1,250.00（25项） | — | ¥10,411.30 |

如有疑问或需要修改，请继续对话。
```
