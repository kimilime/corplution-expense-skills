# Corplution报销向导

作者：Terence Wang

Corplution 专用 Codex skill，用于报销工作流：发票提取、项目分摊、报销工作簿生成，以及最终文件打包。

## Skill

该 skill 位于：

```text
skills/corplution-reimbursement-wizard/
```

调用方式：

```text
$corplution-reimbursement-wizard
```

如需在 Codex 中使用仓库版本，请将 `skills/corplution-reimbursement-wizard/` 复制或同步到你的 Codex skills 目录，通常为：

```text
C:\Users\<you>\.codex\skills\corplution-reimbursement-wizard
```

仓库更新后，请再次同步该文件夹，避免 Codex 继续使用旧的已安装版本。

## Workflow

1. 将发票和行程报告证据提取至 `process/invoice-extraction.md/json`。
2. 根据顾问提供的背景信息和确认循环，将费用分摊至 Corplution 项目。
3. 写入报销 Excel 工作簿，包括项目区块、小计、Total、Grand Total 和 Status 公式。
4. 打包已填写的工作簿、重命名后的发票，以及支持性文件，用于提交报销。

报销工作簿由脚本直接生成，并使用 `skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml` 定义静态布局，例如 sheet 名称、字体、行高、列宽、说明文字、表头和示例行样式。旧版模板仍随包保留在 `skills/corplution-reimbursement-wizard/assets/reimbursement-template.xlsx`，并可通过 `--template bundled` 作为备用方案使用。

## Applicant Experience

Agent 不应要求申请人打开 `process/*.md` 或手动编写 JSON。它应当：

1. 接收发票、行程报告、截图，以及自然语言形式的项目说明。
2. 当事实信息足够时，在内部将项目说明转换为临时 `project-context.json`。
3. 识别完成后，在聊天中粘贴或概述提取审核清单，包括项目编号、来源文件名、角色、类型、发票号码、销售方/服务提供方、日期、金额、费用类别和审核状态。
4. 在提出项目/分摊问题前，先在聊天中粘贴或概述分摊审核清单，包括项目编号、来源文件名、日期、金额、费用类别和建议项目。
5. 将发票开具日期仅作为证据使用，但纯 `other` 类型费用可临时使用发票开具日期，并给出非阻塞提示。当日期不可靠时，应询问实际发生/记录日期：例如机票/火车票上打印的出行日期、酒店入住日期、滴滴/高德行程报告时间，或手机话费的月末日期。
6. 当多项费用需要回答同类问题时，应按费用类型合并提问。例如，将餐费问题合并为一个清单，让申请人按项目编号和日期回答，而不是逐张发票询问。
7. 按费用类型自动匹配，而不是使用一条通用的城市规则：酒店使用入住/城市证据，餐费用明确备注或唯一的非上海城市，出租车使用行程/接驳逻辑，机票/火车票使用目的地/日期，`other` 不应根据开票方城市预匹配。
8. 接受类似“第9项金额不对”的更正，并能根据项目编号追溯到对应来源文件名后再更新。
9. 对于 `CORP-2026-ADMIN` 行，手机话费的 Client 使用 `通讯费`。其他行政费用在已知具体事项时使用具体事项名称；否则使用 `项目、调研以外的其他费用`，并在聊天中粘贴非阻塞提示，询问申请人是否希望使用更具体的事项名称，例如 年会、半年会、客户会、行业协会会议。
10. 写入工作簿后，粘贴或概述餐费每日限额检查。出差餐费上限为 RMB 150/天，本地加班餐费上限为 RMB 60/天；超限但有用餐人员明细的日期仅作为提示，超限且缺少用餐人员明细的日期需要确认，或提供 `reimbursable_amount` 更正。
11. 粘贴或概述酒店限额检查。北京/上海/广州/深圳酒店上限为 RMB 800/晚，其他城市为 RMB 600/晚；超限但有合住房间/共同入住人明细的酒店仅作为提示，缺少晚数或超限且没有合住房间明细的酒店需要确认，或提供 `reimbursable_amount` 调整。
12. 最后输出打包摘要：工作簿、发票数量、支持性文件数量，以及未解决问题。

## Dependencies and OCR

使用以下命令安装 Python 依赖：

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --install
```

对于可选择文本的电子 PDF，通常只需要 Python 依赖即可。对于扫描版 PDF 或图片发票，OCR 还需要系统工具：

* Tesseract OCR，可通过 `PATH` 中的 `tesseract` 调用
* Poppler，可通过 `PATH` 中的 `pdftoppm` 调用，用于在 OCR 前渲染纯扫描 PDF

使用以下命令检查 OCR 就绪情况：

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --strict-ocr
```

如果缺少 OCR 工具，提取器应将纯扫描/图片输入标记为 `manual_review`，并要求确认，而不是编造发票字段。

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/extract_invoices.py --output process <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/allocate_expenses.py \
  --extraction process/invoice-extraction.json \
  --context project-context.json \
  --output process

python skills/corplution-reimbursement-wizard/scripts/apply_allocation_answers.py \
  --allocation process/expense-allocation.json \
  --answers process/allocation-answers.json

python skills/corplution-reimbursement-wizard/scripts/trace_expense_item.py \
  --allocation process/expense-allocation.json \
  --extraction process/invoice-extraction.json \
  --item 9

python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --process-dir process

# Optional: override generated-workbook layout
python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --layout skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml \
  --process-dir process

python skills/corplution-reimbursement-wizard/scripts/package_reimbursement_files.py \
  --final-rows process/final-expense-rows.json \
  --extraction process/invoice-extraction.json \
  --workbook <filled.xlsx> \
  --output-root output
```

## Notes

本仓库适用于 Corplution 内部报销规则。由于报销材料可能包含个人、客户和发票信息，通常应保持私有。

---

# Corplution Reimbursement Wizard

Author: Terence Wang

Corplution-specific Codex skill for reimbursement workflows: invoice extraction, project allocation, reimbursement workbook generation, and final file packaging.

## Skill

The skill lives at:

```text
skills/corplution-reimbursement-wizard/
```

Invoke it as:

```text
$corplution-reimbursement-wizard
```

To use the repository version inside Codex, copy or sync `skills/corplution-reimbursement-wizard/` into your Codex skills directory, usually:

```text
C:\Users\<you>\.codex\skills\corplution-reimbursement-wizard
```

After updating the repo, sync that folder again so Codex does not keep using an older installed copy.

## Workflow

1. Extract invoice and trip-report evidence into `process/invoice-extraction.md/json`.
2. Allocate expenses to Corplution projects using consultant-provided context and confirmation loops.
3. Write the reimbursement Excel workbook with project blocks, subtotals, Total, Grand Total, and Status formulas.
4. Package the filled workbook, renamed invoices, and support documents for submission.

The reimbursement workbook is generated directly by script using `skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml` for static layout such as sheet name, fonts, row heights, column widths, instruction text, headers, and sample-row styles. The legacy template is still bundled at `skills/corplution-reimbursement-wizard/assets/reimbursement-template.xlsx` and can be used as a fallback with `--template bundled`.

## Applicant Experience

The agent should not ask the applicant to open `process/*.md` or write JSON manually. It should:

1. Accept invoices, trip reports, screenshots, and natural-language project notes.
2. Convert project notes into temporary `project-context.json` internally when enough facts are present.
3. Paste or summarize the extraction review list in chat after recognition, including item number, source filename, role, type, invoice number, seller/provider, date, amount, category, and review status.
4. Paste or summarize the allocation review list in chat before asking project/allocation questions, including item number, source filename, date, amount, category, and suggested project.
5. Treat invoice issue dates as evidence only, except pure `other` expenses may temporarily use invoice issue date with a non-blocking advisory. Ask for the actual occurrence/record date when the date is not reliable: printed flight/rail date, hotel stay dates, Didi/Gaode trip-report time, or mobile month-end.
6. Ask grouped questions by expense type when many items need the same kind of answer. For example, combine meal questions into one list and let the applicant answer by item numbers and dates instead of asking every invoice separately.
7. Auto-match by expense type rather than by one generic city rule: hotels use stay/city evidence, meals use explicit notes or unique non-Shanghai city, taxi rides use journey/transfer logic, flight/rail use destination/date, and `other` is not pre-matched by issuer city.
8. Accept corrections such as "第9项金额不对" and trace the item back to its source filename before updating.
9. For `CORP-2026-ADMIN` rows, use `通讯费` as the Client for mobile expenses. For other admin expenses, use the specific matter name when known; otherwise use `项目、调研以外的其他费用` and paste a non-blocking chat prompt asking whether the applicant wants a more specific matter such as 年会、半年会、客户会、行业协会会议.
10. After writing the workbook, paste or summarize the meal daily cap check. Business-trip meals are capped at RMB 150/day and local overtime meals at RMB 60/day; over-cap days with attendee details are advisory, while over-cap days without attendee details require confirmation or a `reimbursable_amount` correction.
11. Paste or summarize the hotel cap check. Beijing/Shanghai/Guangzhou/Shenzhen hotels are capped at RMB 800/night and other cities at RMB 600/night; over-cap hotels with shared-room/co-occupant details are advisory, while missing nights or over-cap hotels without shared-room details require confirmation or a `reimbursable_amount` adjustment.
12. End with a package summary: workbook, invoice count, support-document count, and unresolved issues.

## Dependencies and OCR

Install Python dependencies with:

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --install
```

For selectable electronic PDFs, Python dependencies are usually enough. For scanned PDFs or image invoices, OCR also needs system tools:

- Tesseract OCR, available on `PATH` as `tesseract`
- Poppler, available on `PATH` as `pdftoppm`, for rendering scan-only PDFs before OCR

Check OCR readiness with:

```bash
python skills/corplution-reimbursement-wizard/scripts/check_dependencies.py --strict-ocr
```

If OCR tools are missing, the extractor should mark scan-only/image inputs as `manual_review` and ask for confirmation instead of inventing invoice fields.

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/extract_invoices.py --output process <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/allocate_expenses.py \
  --extraction process/invoice-extraction.json \
  --context project-context.json \
  --output process

python skills/corplution-reimbursement-wizard/scripts/apply_allocation_answers.py \
  --allocation process/expense-allocation.json \
  --answers process/allocation-answers.json

python skills/corplution-reimbursement-wizard/scripts/trace_expense_item.py \
  --allocation process/expense-allocation.json \
  --extraction process/invoice-extraction.json \
  --item 9

python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --process-dir process

# Optional: override generated-workbook layout
python skills/corplution-reimbursement-wizard/scripts/write_reimbursement_template.py \
  --allocation process/expense-allocation.json \
  --output <filled.xlsx> \
  --requester <name> \
  --layout skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml \
  --process-dir process

python skills/corplution-reimbursement-wizard/scripts/package_reimbursement_files.py \
  --final-rows process/final-expense-rows.json \
  --extraction process/invoice-extraction.json \
  --workbook <filled.xlsx> \
  --output-root output
```

## Notes

This repository is intended for Corplution internal reimbursement rules. It should usually be kept private because reimbursement materials may contain personal, client, and invoice information.
