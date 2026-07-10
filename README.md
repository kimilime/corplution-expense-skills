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
5. 用户回答后，先运行模板生成器，填充本次 allocation 专属的 canonical `unit_updates` 模板，再用 updater dry-run/apply；不要凭空编造 `answers[].allocations` 等 JSON 结构。
6. 将发票开具日期仅作为证据使用，但纯 `other` 类型费用可临时使用发票开具日期，并给出非阻塞提示。当日期不可靠时，应询问实际发生/记录日期：例如机票/火车票上打印的出行日期、酒店入住日期、滴滴/高德行程报告时间，或手机话费的月末日期。
7. 当多项费用需要回答同类问题时，应按费用类型合并提问。例如，将餐费问题合并为一个清单，让申请人按项目编号和日期回答，而不是逐张发票询问。
8. 按费用类型自动匹配，而不是使用一条通用的城市规则：酒店使用入住/城市证据；餐费优先使用自然语言记录，例如 `6.1 德克士 61.8`，并按金额、日期、商户文本综合评分，而不是要求开票方、开票日期或金额完全一致；餐费金额列和 `Expense Nature` 执行形式重于实质，上海发票/餐厅城市进 `meal`/本地，非上海进 `travel`/出差，即使归属到出差项目也不改变；缺少明确记录时才使用唯一的非上海城市预归集；出租车使用行程/接驳逻辑，打车金额列和 `Expense Nature` 同样按发生城市而不是归属项目决定，上海打车进 `taxi`/本地，非上海打车进 `travel`/出差，并且不得把 `出发地类型` 或 `目的地类型` 这些模板占位词写进最终 Note；机票/火车票使用目的地/日期；`other` 不应根据开票方城市预匹配；`CORP-2026-ADMIN` 不能当作上海项目或兜底项目参与自动匹配。
9. 接受类似“第9项金额不对”的更正，并能根据项目编号追溯到对应来源文件名后再更新。
10. 对于 `CORP-2026-ADMIN` 行，手机话费的 Client 使用 `通讯费`。其他行政费用在已知具体事项时使用具体事项名称；否则使用 `项目、调研以外的其他费用`，并在聊天中粘贴非阻塞提示，询问申请人是否希望使用更具体的事项名称，例如 年会、半年会、客户会、行业协会会议。
11. 写入工作簿后，粘贴或概述餐费每日限额检查。出差餐费上限为 RMB 150/天，本地加班餐费上限为 RMB 60/天；超限但有用餐人员明细的日期仅作为提示，超限且缺少用餐人员明细的日期需要确认，或提供 `reimbursable_amount` 更正。
12. 粘贴或概述酒店限额检查。北京/上海/广州/深圳酒店上限为 RMB 800/晚，其他城市为 RMB 600/晚；超限但有合住房间/共同入住人明细的酒店仅作为提示，缺少晚数或超限且没有合住房间明细的酒店需要确认，或提供 `reimbursable_amount` 调整。
13. 最后仅在包内文件和 manifest 校验通过且没有未解决问题时，输出“可提交”的打包摘要。若打包脚本返回 code `3`，该文件夹只是待补件的 review package，必须先在聊天中解决缺失发票、行程单或替票审批问题并重新打包。

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

如果 Windows/Git Bash 终端输出乱码或截断，不要另写临时提取脚本绕过流程；直接读取脚本已写出的 UTF-8 Markdown 过程文件，例如 `process/invoice-extraction.md`、`process/expense-allocation.md` 或 `process/final-expense-rows.md`。批量修正 allocation 时，应先生成并填写 `allocation-answers.template.json`，再将结果保存为 `allocation-answers.json` 并运行 `apply_allocation_answers.py`；不要直接 patch `expense-allocation.json`。

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/extract_invoices.py --output process <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/allocate_expenses.py \
  --extraction process/invoice-extraction.json \
  --context project-context.json \
  --output process

python skills/corplution-reimbursement-wizard/scripts/build_allocation_answers_template.py \
  --allocation process/expense-allocation.json \
  --output process/allocation-answers.template.json

python skills/corplution-reimbursement-wizard/scripts/apply_allocation_answers.py \
  --allocation process/expense-allocation.json \
  --answers process/allocation-answers.json \
  --dry-run

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

如果写表脚本以退出码 `3` 结束，它仍然已经写出 workbook 和 `process/final-expense-rows.*`，但 Stage 3 政策检查需要申请人确认后才能最终提交。请把 `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` 摘要直接贴回对话，并处理列出的餐费/酒店标准问题。

如果写表脚本以退出码 `2` 结束，说明 `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT` 发现 allocation 还没有结构化准备好，workbook 不会写出。请先处理开放问题、未确认费用、非法分类/金额列、缺少日期/客户/编号/金额、Admin/通讯费冲突、原始高铁/飞机票据 Note、或缺少打车地点类型等问题。

如果打包脚本以退出码 `3` 结束，说明已经生成了用于补件核对的 package 和 manifest，但存在缺失发票、支持文件或替票审批等阻断问题。不要提交或宣称流程完成；先处理 manifest 的 issues，再重新运行打包。

## 完整性与重跑安全

- 不支持的输入文件（例如 OFD、EML、ZIP）会连同 SHA-256 持久化到提取结果中；在用户明确排除并说明理由，或提供可读取的替代文件前，不能进入归集、写表或打包。
- 归集记录提取批次指纹；如果重新提取了材料，必须重新归集、重新生成 answers 模板并重新写表，不能修补旧文件继续使用。
- 同一编号下重名的行程单或审批件会保留为 `-2`、`-3` 等文件名，不会互相覆盖。重跑打包会以完整的新文件夹替换旧包，因此旧证据不会残留在新提交包中。
- `check_workflow_status.py` 中任何完整性失败都是 `BLOCKED`，应按输出的恢复步骤重新生成受影响阶段，而不是手改过程 JSON。

## Integrity and Rerun Safety

- Unsupported inputs such as OFD, EML, and ZIP are persisted with a SHA-256 in extraction output. Allocation, workbook generation, and packaging stop until the user records an exclusion reason or supplies a readable replacement.
- Allocation records its extraction generation. After re-extraction, rerun allocation, regenerate the answers template, and rewrite the workbook; do not patch an older process file forward.
- Same-proof support files receive `-2`, `-3`, and later suffixes rather than overwriting one another. Packaging replaces the old package only after a complete fresh staging build, so stale evidence cannot survive a rerun.
- Any integrity failure in `check_workflow_status.py` is `BLOCKED`. Follow its recovery instruction and regenerate the affected stage instead of editing process JSON by hand.

## 批量 Answers Helper 的编码安全

- 批量 helper 只允许从本次生成的 `allocation-answers.template.json` 填充既有 `unit_updates`，再生成 `allocation-answers.json`；绝不能直接修改任何 `process/*.json`。
- helper 必须显式以 `utf-8-sig` 读取模板、以 UTF-8 写出 JSON。不要把中文值经由 PowerShell inline Python、`-Command` 或控制台管道传入；应使用 UTF-8 的 Python/JSON 文件，或在受限 inline 值中使用 Unicode escape。
- 先运行官方 updater 的 `--dry-run`，再 apply。updater、Stage 3 和保存后的 Excel 均会拒绝 `?`、替换字符及常见乱码标记；失败后修 helper 输入并重新生成 answers，不要补丁式修改 allocation 或 workbook。

## Batch Answers Helper Encoding Safety

- A batch helper may only fill existing `unit_updates` in the current `allocation-answers.template.json` and then generate `allocation-answers.json`; it must never edit any `process/*.json` directly.
- Read the template as `utf-8-sig` and write JSON as UTF-8. Do not route Chinese text through PowerShell inline Python, `-Command`, or a console pipeline; use UTF-8 Python/JSON files or Unicode escapes for constrained inline values.
- Run the official updater with `--dry-run` before apply. The updater, Stage 3, and the saved workbook reject `?`, replacement characters, and common mojibake markers; repair the helper input and regenerate answers instead of patching allocation or workbook files.

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
5. After the user answers, run the template generator, fill the current allocation's canonical `unit_updates` template, and validate/apply it with the updater; do not invent JSON shapes such as `answers[].allocations`.
6. Treat invoice issue dates as evidence only, except pure `other` expenses may temporarily use invoice issue date with a non-blocking advisory. Ask for the actual occurrence/record date when the date is not reliable: printed flight/rail date, hotel stay dates, Didi/Gaode trip-report time, or mobile month-end.
7. Ask grouped questions by expense type when many items need the same kind of answer. For example, combine meal questions into one list and let the applicant answer by item numbers and dates instead of asking every invoice separately.
8. Auto-match by expense type rather than by one generic city rule: hotels use stay/city evidence; meals first use natural-language notes such as `6.1 德克士 61.8` and score combined amount/date/merchant evidence instead of requiring seller, issue date, or amount to match exactly; meal amount columns and `Expense Nature` follow form over substance, so Shanghai invoice/restaurant city goes to `meal`/local and non-Shanghai goes to `travel`/business trip even when the meal is allocated to a business-trip project; meals without explicit notes may use unique non-Shanghai city pre-allocation; taxi rides use journey/transfer logic, but taxi amount columns and `Expense Nature` also follow form over substance by ride city rather than assigned project, so Shanghai rides go to `taxi`/local and non-Shanghai rides go to `travel`/business trip; taxi notes must not write literal placeholders such as `出发地类型` or `目的地类型`; flight/rail use destination/date; `other` is not pre-matched by issuer city; `CORP-2026-ADMIN` is not a Shanghai project or fallback project for auto-matching.
9. Never use `CORP-2026-ADMIN`, Client `通讯费`, or the mobile column as a fallback for unmatched taxi/travel/meal/hotel expenses. Unmatched transport should stay in the question queue unless transfer/travel logic assigns it.
10. Accept corrections such as "第9项金额不对" and trace the item back to its source filename before updating.
11. Preserve meal attendees from user notes, even when the meal is under the cap.
12. For `CORP-2026-ADMIN` rows, use `通讯费` as the Client for mobile expenses. For other admin expenses, use the specific matter name when known; otherwise use `项目、调研以外的其他费用` and paste a non-blocking chat prompt asking whether the applicant wants a more specific matter such as 年会、半年会、客户会、行业协会会议.
13. After writing the workbook, paste or summarize the meal daily cap check. Business-trip meals are capped at RMB 150/day and local overtime meals at RMB 60/day; over-cap days with attendee details are advisory, while over-cap days without attendee details require confirmation or a `reimbursable_amount` correction.
14. Paste or summarize the hotel cap check. Beijing/Shanghai/Guangzhou/Shenzhen hotels are capped at RMB 800/night and other cities at RMB 600/night; over-cap hotels with shared-room/co-occupant details are advisory, while missing nights or over-cap hotels without shared-room details require confirmation or a `reimbursable_amount` adjustment.
15. End with a submission-ready package summary only after the package files and manifest validate with no unresolved issues. If packaging exits with code `3`, the folder is a review package awaiting missing invoices, trip reports, or substitute approvals; resolve them in chat and repackage before submission.

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

If Windows/Git Bash terminal output is garbled or truncated, do not write temporary extraction scripts to bypass the workflow; read the UTF-8 Markdown process files already written by the scripts, such as `process/invoice-extraction.md`, `process/expense-allocation.md`, or `process/final-expense-rows.md`. For bulk allocation corrections, generate and fill `allocation-answers.template.json`, save the filled result as `allocation-answers.json`, and run `apply_allocation_answers.py` instead of directly patching `expense-allocation.json`.

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/extract_invoices.py --output process <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/allocate_expenses.py \
  --extraction process/invoice-extraction.json \
  --context project-context.json \
  --output process

python skills/corplution-reimbursement-wizard/scripts/build_allocation_answers_template.py \
  --allocation process/expense-allocation.json \
  --output process/allocation-answers.template.json

python skills/corplution-reimbursement-wizard/scripts/apply_allocation_answers.py \
  --allocation process/expense-allocation.json \
  --answers process/allocation-answers.json \
  --dry-run

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

If the workbook writer exits with code `3`, it has still written the workbook and `process/final-expense-rows.*`, but Stage 3 policy checks need applicant confirmation before final submission. Copy the `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` block into the conversation and resolve the listed meal/hotel cap items.

If the workbook writer exits with code `2`, the `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT` block found that allocation is not structurally ready, and no workbook was written. Resolve open questions, unconfirmed units, invalid categories/amount columns, missing date/client/code/amount fields, Admin/mobile conflicts, raw rail/flight notes, or missing taxi place types first.

If the packaging script exits with code `3`, it has written a review package and manifest but found blocking missing invoices, support files, or substitute approvals. Do not submit it or call the workflow complete; resolve the manifest issues and rerun packaging.

## Notes

This repository is intended for Corplution internal reimbursement rules. It should usually be kept private because reimbursement materials may contain personal, client, and invoice information.
