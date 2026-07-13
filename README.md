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

本实验分支默认通过 `chief_orchestrator.py` 进入流程。Chief 只负责状态导航、规范参数调度和最小化运行日志；提取、归集、受控更新、写表和打包仍由原脚本完成。

## 双子 Agent 试验

该分支增加两个可选的只读角色：`Otako - Allocation Analyst` 在 Stage 2 初步归集后独立检查项目、行程链、接驳和用户费用记录；`Kaede - Independent Reviewer` 在 Stage 3 前从头审查材料完整性、项目归属、形式重于实质、Notes 和政策检查前提。子 Agent 不接收本地路径或写入权限，只接收由 `subagent_protocol.py` 生成的不可变数据快照，并以绑定完整 allocation/extraction 指纹的 JSON 返回。

```bash
# Otako：先生成任务，将任务和 result-template 的完整 JSON 内容交给新子 Agent。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run prepare-agent --role allocation_analyst
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run accept-agent --role allocation_analyst --result <otako-result.json>

# 原始建议是 unreviewed，Composer 会拒绝。先显式选择 proposal ID。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run promote-proposals --select P-001,P-003 --reviewed-by coordinator

# 再把 promote 命令打印的 .reviewed.json 文件交给 Composer。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run compose --proposal <reviewed-otako-proposals.json>

# Kaede：在 Stage 2 完全确认后、Stage 3 前运行。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run prepare-agent --role independent_reviewer
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run accept-agent --role independent_reviewer --result <kaede-result.json>
```

Otako 建议不会自动修改 allocation；未经过 `promote-proposals` 显式选择和盖章的建议也不能进入 Composer。Kaede 只能在 Stage 2 完全就绪后运行；当前且验章通过的 `block` 会阻断写表和打包。每份受理结果先进入 `process/subagent-review-generations/` 的不可变世代归档，因此删除或损坏便捷 sidecar 不能清除已受理阻断；`pass/advisory/unavailable` 会写入 final rows。只有当前任务确实不存在任何有效受理结果时，流程才 fail-open 到现有确定性预检，而不会伪造一个 `pass`。完整性印章只能证明结果验收后未被修改，不能证明模型身份或独立性。

报销工作簿由脚本直接生成，并使用 `skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml` 定义静态布局，例如 sheet 名称、字体、行高、列宽、说明文字、表头和示例行样式。旧版模板仍随包保留在 `skills/corplution-reimbursement-wizard/assets/reimbursement-template.xlsx`，并可通过 `--template bundled` 作为备用方案使用。

## Applicant Experience

Agent 不应要求申请人打开 `process/*.md` 或手动编写 JSON。它应当：

1. 接收发票、行程报告、截图，以及自然语言形式的项目说明。
2. 当事实信息足够时，在内部按 `assets/project-context-template.json` 的 `project_context.v1` 唯一结构生成 `project-context.json`；每个独立的项目/出差日期窗口写一个 context，即便 Client 和 Code 相同也不能省略日期窗口。不得猜测 `projects`、`charge_code`、`notes` 等别名，也不得让申请人写 JSON。
3. 识别完成后，在聊天中粘贴或概述提取审核清单，包括项目编号、来源文件名、角色、类型、发票号码、销售方/服务提供方、日期、金额、费用类别和审核状态。
4. 在提出项目/分摊问题前，先在聊天中粘贴或概述分摊审核清单，包括项目编号、来源文件名、日期、金额、费用类别和建议项目。
5. 用户回答后，将自然语言决议写入 `allocation_decisions.v1`，再由 Composer 转换为当前 allocation 专属的 canonical `unit_updates` 和 `expense_hint_resolutions`，自动 dry-run 后交给 updater 应用。Composer 支持 unit/question/context/hint-resolution 更新和 confirm/drop/exclude；失败时修正同一份 decisions 文件重跑，不得改用模板填充、`fill_answers.py` 或 patch/fix 脚本。每个费用项和用户费用记录均有短引用与完整证据 SHA-256；新增/删除发票后，Chief 会从不可覆盖的指纹历史链中找到最近的已决代，先 rebase 不变证据的用户决定，再只追问变化项。常用字段别名见 `references/stage-2-allocation.md` 的 Composer 速查表。
6. 将发票开具日期仅作为证据使用，但纯 `other` 类型费用可临时使用发票开具日期，并给出非阻塞提示。当日期不可靠时，应询问实际发生/记录日期：例如机票/火车票上打印的出行日期、酒店入住日期、滴滴/高德行程报告时间，或手机话费的月末日期。
7. 当多项费用需要回答同类问题时，应按费用类型合并提问。例如，将餐费问题合并为一个清单，让申请人按项目编号和日期回答，而不是逐张发票询问。
8. 按费用类型自动匹配，而不是使用一条通用的城市规则：酒店使用入住/城市证据；餐费优先使用自然语言记录，例如 `6.1 德克士 61.8`，并按金额、日期、商户文本综合评分，而不是要求开票方、开票日期或金额完全一致；餐费金额列和 `Expense Nature` 执行形式重于实质，上海发票/餐厅城市进 `meal`/本地，非上海进 `travel`/出差，即使归属到出差项目也不改变；但餐费政策是独立维度，`出差餐费`（包括上海出发前或差旅途中的餐费）统一进入 RMB 150/天的池，只有明确的 `加班餐费` 才进入 RMB 60/天的池，绝不能从城市、金额列或 `Expense Nature` 推断；缺少明确记录时才使用唯一的非上海城市预归集；出租车使用行程/接驳逻辑，打车金额列和 `Expense Nature` 同样按发生城市而不是归属项目决定，上海打车进 `taxi`/本地，非上海打车进 `travel`/出差，并且不得把 `出发地类型` 或 `目的地类型` 这些模板占位词写进最终 Note；机票/火车票使用目的地/日期；`other` 不应根据开票方城市预匹配；`CORP-2026-ADMIN` 不能当作上海项目或兜底项目参与自动匹配。
   铁路换乘补充：同日或时间紧密、车站首尾相接的多张火车票先组成一条连续行程链，再整体判断项目。中间站默认只是换乘节点；去程全部归最终前往项目，项目间转移全部归后一个项目，返程全部归刚结束的项目。每张票仍保留独立金额、编号和 Note，只共享项目归属；只有链路不连续、多项目同样合理或用户说明中间城市有实际停留时才整链询问。
   用户记录完整性补充：申请人提供的每条具体费用记录只能放入 `meal_hints` 或 `expense_hints` 之一；旧上下文若在两个数组重复写了同一条，脚本会按项目、类型、日期、金额和已有商户证据去重。每条独立记录都会反向核对是否找到发票/费用项；未找到或候选不唯一时必须在聊天中明确询问“对应哪项、是否由汇总发票合并覆盖、是否会补票，或是否不报销”，不得静默忽略。删除原匹配项会重新打开这个完整性问题。
9. 接受类似“第9项金额不对”的更正，并能根据项目编号追溯到对应来源文件名后再更新。
10. 对于 `CORP-2026-ADMIN` 行，手机话费的 Client 使用 `通讯费`。其他行政费用在已知具体事项时使用具体事项名称；否则使用 `项目、调研以外的其他费用`，并在聊天中粘贴非阻塞提示，询问申请人是否希望使用更具体的事项名称，例如 年会、半年会、客户会、行业协会会议。
11. 写入工作簿后，原样转述脚本生成的餐费每日限额检查，不要按 Excel 列重新计算。出差餐费上限为 RMB 150/天，本地加班餐费上限为 RMB 60/天；同日同政策的餐费跨 `meal`/`travel` 列合并计算。超限但有用餐人员明细的日期仅作为提示，超限且缺少用餐人员明细的日期需要确认，或提供 `reimbursable_amount` 更正。
12. 粘贴或概述酒店限额检查。北京/上海/广州/深圳酒店上限为 RMB 800/晚，其他城市为 RMB 600/晚；超限但有合住房间/共同入住人明细的酒店仅作为提示，缺少晚数或超限且没有合住房间明细的酒店需要确认，或提供 `reimbursable_amount` 调整。
13. 最后仅在包内文件和 manifest 校验通过且没有未解决问题时，输出“可提交”的打包摘要。若打包脚本返回 code `3`，该文件夹只是待补件的 review package，必须先在聊天中解决缺失发票、行程单或替票审批问题并重新打包。

## Dependencies and OCR

所有命令中的 `skills/corplution-reimbursement-wizard` 都代表本仓库的 Skill 根目录。Agent 在其他工作目录运行时，应直接调用已安装 Skill 中 `chief_orchestrator.py` 的绝对路径；不得创建 `run_chief.py`、修改 `sys.path`、导入 Chief 或复制脚本。Chief 会拒绝这类包装入口并打印正确命令。

使用以下命令安装 Python 依赖：

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run dependencies --install
```

对于可选择文本的电子 PDF，通常只需要 Python 依赖即可。对于扫描版 PDF 或图片发票，OCR 还需要系统工具：

* Tesseract OCR，可通过 `PATH` 中的 `tesseract` 调用
* Poppler，可通过 `PATH` 中的 `pdftoppm` 调用，用于在 OCR 前渲染纯扫描 PDF

使用以下命令检查 OCR 就绪情况：

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run dependencies --strict-ocr
```

如果缺少 OCR 工具，提取器应将纯扫描/图片输入标记为 `manual_review`，并要求确认，而不是编造发票字段。

Chief 会以 Python UTF-8 模式和 `PYTHONIOENCODING=utf-8` 启动所有子脚本，直接入口也会自行配置 UTF-8，正常流程无需反复手工添加编码环境变量。如果 Windows/Git Bash 终端仍显示乱码或截断，不要另写临时提取脚本绕过流程；直接读取脚本已写出的 UTF-8 Markdown 过程文件，例如 `process/invoice-extraction.md`、`process/expense-allocation.md` 或 `process/final-expense-rows.md`。批量确认 allocation 时，优先使用 `compose_answers.py` 从简洁决议生成 `allocation-answers.json`，它会自动 dry-run updater；不要直接 patch `expense-allocation.json`。

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py status
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py next
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py next --json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run extract <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run correct-extraction --corrections process/extraction-corrections-input.json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run allocate --context project-context.json

# 仅在 Chief NEXT 检测到可迁移历史决定时运行；通常直接照 NEXT 命令执行。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run rebase

# --set 仅用于短 ASCII 值；必须复制当前清单中的完整 N@ref，范围写法不被接受。
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run compose \
  --set "3@a1b2c3d4,5@e5f6a7b8: status=confirmed"

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run compose --decisions process/batch-decisions.json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run apply

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run trace --item 9

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run write --output <filled.xlsx> --requester <name>

# Optional: override generated-workbook layout
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run write \
  --output <filled.xlsx> --requester <name> \
  --layout skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run package
```

原有脚本仅供开发者调试；普通流程必须直接调用 Skill 内的 Chief，且只有经 Chief 调度的运行会写入工作流日志。失败命令不能通过包装器、临时 helper、patch/fix 脚本或修改 Skill 自身来绕过。
提取时应传上传/证据目录或明确的来源文件；不要传同时包含 `process`、`output` 或 Journal 的任务根目录，Chief 会拒绝这种重叠，避免生成物被误判为新证据。

如果写表脚本以退出码 `3` 结束，它仍然已经写出 workbook 和 `process/final-expense-rows.*`，但 Stage 3 政策检查需要申请人确认后才能最终提交。请把 `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` 摘要直接贴回对话，并处理列出的餐费/酒店标准问题。

如果写表脚本以退出码 `2` 结束，说明 `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT` 发现 allocation 还没有结构化准备好，workbook 不会写出。请先处理开放问题、未确认费用、非法分类/金额列、缺少日期/客户/编号/金额、Admin/通讯费冲突、原始高铁/飞机票据 Note、或缺少打车地点类型等问题。

如果打包脚本以退出码 `3` 结束，说明已经生成了用于补件核对的 package 和 manifest，但存在缺失发票、支持文件或替票审批等阻断问题。不要提交或宣称流程完成；先处理 manifest 的 issues，再重新运行打包。

## 完整性与重跑安全

- 不支持的输入文件（例如 OFD、EML、ZIP）会连同 SHA-256 持久化到提取结果中；在用户明确排除并说明理由，或提供可读取的替代文件前，不能进入归集、写表或打包。
- 完全相同的文件和重复发票号必须在 Stage 1 确认保留/排除；不能先进入 Stage 2 再靠 drop 费用项代替源材料决定。
- 归集同时记录提取批次指纹和 `project-context.json` 的 SHA-256；任一输入变化、缺失或失效后，都必须重新归集、重新编译 answers 并重新写表，不能修补或复用旧文件继续使用。
- `expense_hint_reconciliation` 逐条记录用户费用说明。已有凭证匹配、由指定汇总发票覆盖、确认不报销会关闭记录；“稍后补票”只保存为 `pending_invoice`，在凭证补齐或改为不报销前仍会阻断 Stage 3 和打包。
- 同一编号下重名的行程单或审批件会保留为 `-2`、`-3` 等文件名，不会互相覆盖。重跑打包会以完整的新文件夹替换旧包，因此旧证据不会残留在新提交包中。
- Windows 重打包遇到短暂文件锁会自动重试；若仍失败，应关闭旧包里的 Excel 和资源管理器预览后通过 Chief 重跑，直接调用底层打包脚本并不能绕过锁。
- Chief `status/next` 与 `check_workflow_status.py` 共用同一状态机；任何完整性失败都是 `BLOCKED`，应按输出的恢复步骤重新生成受影响阶段，而不是手改过程 JSON。

## 工作流日志

- Chief 调度的每次运行会向 `process/workflow-journal.jsonl` 追加 `started` 与 `completed/failed` 事件。
- 日志仅记录阶段、脚本名、时间、退出码、耗时、产物指纹和数量，不记录原始发票文本、用户答案、客户信息、完整命令参数或来源路径。
- 日志仅用于观察和恢复上下文，不参与完整性判定，也不会进入最终报销文件包；日志写入失败不会改变底层脚本退出码。

## 故障恢复

常见的 OFD/不支持输入、`input_resolutions` 格式、完全重复文件、Composer 字段、旧指纹、编码、酒店城市、Stage 3 退出码和 Windows 文件锁处理，见 [`references/troubleshooting.md`](skills/corplution-reimbursement-wizard/references/troubleshooting.md)。如果错误 correction/overlay 已被持久化且正规修复连续失败，按其中的“干净重建事实映射”保留带来源描述的用户确认事实；旧编号全部作废。恢复时先运行 Chief `status`，再执行唯一的 `CHIEF NEXT`；不要手改过程 JSON 或另写 helper/patch 脚本。

## Integrity and Rerun Safety

- Unsupported inputs such as OFD, EML, and ZIP are persisted with a SHA-256 in extraction output. Allocation, workbook generation, and packaging stop until the user records an exclusion reason or supplies a readable replacement.
- Exact-file and repeated-invoice-number duplicates require an explicit Stage 1 keep/exclude decision; dropping a Stage 2 unit is not a substitute.
- Allocation records extraction/context/policy generations, an allocation-engine revision, compact `N@ref`/`R@ref` handles, and full evidence identities. Every official allocation write archives the prior stamped generation under `process/allocation-generations/` and records a lineage pointer. After evidence additions/removals, follow `CHIEF NEXT` through rebase and Composer; changed contexts, policy, engine revision, or evidence identities force fresh review rather than replay.
- `expense_hint_reconciliation` records the outcome for every applicant expense note. Existing-evidence matches, identified combined-invoice coverage, and `not_reimbursed` close a record. `pending_invoice` records progress but still blocks Stage 3 and packaging until evidence arrives or the applicant drops the note from this claim.
- Same-proof support files receive `-2`, `-3`, and later suffixes rather than overwriting one another. Packaging replaces the old package only after a complete fresh staging build, so stale evidence cannot survive a rerun.
- Package promotion retries transient Windows locks. If retries are exhausted, close the old packaged workbook and Explorer previews, then rerun through Chief; direct package invocation cannot bypass the lock.
- Chief `status/next` and `check_workflow_status.py` use the same state engine. Any integrity failure is `BLOCKED`; regenerate the affected stage instead of editing process JSON by hand.

## 批量 Answers 的编码安全

- 始终使用 `compose_answers.py`，不要为任何批次临时写 Python helper。它读取真实的 `user_no`、使用当前 allocation 指纹、校验字段并自动 dry-run updater，且只有验证通过才发布 `allocation-answers.json`。
- `--set` 只适用于不含空格或 shell 敏感字符的短 ASCII 值。中文、引号、文件路径、长备注或不确定的控制台编码都使用基于 `assets/allocation-decisions-template.json` 的 UTF-8 `allocation_decisions.v1` 文件。
- Composer 已覆盖 updater 的全部正常动作，没有 helper 例外。Composer 失败时修正同一份 decisions 文件重跑；不得生成/填充 answers template、创建 `fill_answers.py`、直接修改过程 JSON 或篡改 Skill 脚本。

## Batch Answers Encoding Safety

- Always use `compose_answers.py`; never write a per-batch Python helper. It resolves the live `user_no` values, binds the current allocation fingerprint, validates fields, dry-runs the updater, and publishes `allocation-answers.json` only after validation passes.
- Use `--set` only for short ASCII values without whitespace or shell-sensitive characters. For Chinese, spaces, quotes, file paths, long notes, or uncertain terminal encoding, write UTF-8 `allocation_decisions.v1` from `assets/allocation-decisions-template.json`.
- Composer covers all normal updater actions, so there is no helper exception. On failure, correct the same decisions file and rerun Composer; never fill an answers template, create `fill_answers.py`, edit process JSON, or modify a bundled script.

## Troubleshooting and Recovery

See [`references/troubleshooting.md`](skills/corplution-reimbursement-wizard/references/troubleshooting.md) for unsupported/OFD inputs, canonical `input_resolutions`, exact duplicates, Composer fields, stale fingerprints, encoding, hotel city, Stage 3 exit codes, Windows package locks, and clean-rebuild fact mapping. If a wrong durable correction/overlay survives repeated sanctioned repair, preserve explicit user facts with source descriptors and rebuild in a clean sibling batch; every old item number expires. Start recovery with Chief `status`, then follow the single `CHIEF NEXT` action; never patch process JSON or create a helper to bypass a failed stage.

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

This experimental branch uses `chief_orchestrator.py` as the default entry point. Chief handles state navigation, canonical dispatch, and privacy-minimized run logging only; the existing scripts still perform extraction, allocation, controlled updates, workbook generation, and packaging.

## Two-Subagent Pilot

This branch adds two optional read-only roles. `Otako - Allocation Analyst` independently checks Stage 2 project matching, journey chains, transfers, and applicant expense records. `Kaede - Independent Reviewer` audits evidence completeness, project allocation, form-over-substance classification, Notes, and policy prerequisites immediately before Stage 3. Neither receives local paths or write capability; `subagent_protocol.py` supplies an immutable data snapshot and accepts only JSON bound to the full allocation/extraction fingerprints.

```bash
# Otako: give a fresh subagent the complete task and result-template JSON values.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run prepare-agent --role allocation_analyst
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run accept-agent --role allocation_analyst --result <otako-result.json>

# Raw proposals are unreviewed and rejected by Composer. Select proposal IDs explicitly.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run promote-proposals --select P-001,P-003 --reviewed-by coordinator

# Give Composer the stamped .reviewed.json file printed by promotion.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run compose --proposal <reviewed-otako-proposals.json>

# Kaede: run after Stage 2 is fully confirmed and before Stage 3.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run prepare-agent --role independent_reviewer
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run accept-agent --role independent_reviewer --result <kaede-result.json>
```

Otako proposals never mutate allocation automatically, and an unreviewed proposal cannot enter Composer until `promote-proposals` explicitly selects and stamps it. Kaede can run only after Stage 2 is fully ready. A current validated `block` prevents workbook generation and packaging, and every accepted result is first retained under the immutable `process/subagent-review-generations/` archive, so deleting or corrupting the convenience sidecar cannot clear a blocker. `pass/advisory/unavailable` is recorded in final rows. Only when no valid accepted result exists for the current task does the pilot fail open to the deterministic preflight; it never fabricates `pass`. The integrity stamp proves post-acceptance immutability, not reviewer identity or independence.

The reimbursement workbook is generated directly by script using `skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml` for static layout such as sheet name, fonts, row heights, column widths, instruction text, headers, and sample-row styles. The legacy template is still bundled at `skills/corplution-reimbursement-wizard/assets/reimbursement-template.xlsx` and can be used as a fallback with `--template bundled`.

## Applicant Experience

The agent should not ask the applicant to open `process/*.md` or write JSON manually. It should:

1. Accept invoices, trip reports, screenshots, and natural-language project notes.
2. Convert project notes internally into the single `project_context.v1` structure from `assets/project-context-template.json`. Use one context per distinct project/travel date window, even when Client and Code repeat. Do not guess aliases such as `projects`, `charge_code`, or `notes`, and never ask the applicant to write JSON.
3. Paste or summarize the extraction review list in chat after recognition, including item number, source filename, role, type, invoice number, seller/provider, date, amount, category, and review status.
4. Paste or summarize the allocation review list in chat before asking project/allocation questions, including item number, source filename, date, amount, category, and suggested project.
5. After the user answers, write `allocation_decisions.v1` and use Composer to convert it into the current allocation's canonical `unit_updates` and `expense_hint_resolutions`, dry-run them automatically, and apply them through the updater. Composer supports unit/question/context/hint-resolution updates plus confirm/drop/exclude. Units and applicant records have compact refs plus full evidence SHA-256 identities. After invoices are added/removed, Chief finds the nearest decided generation in the immutable fingerprint lineage, rebases unchanged user decisions, and asks only about changed/new evidence. If Composer fails, correct the same decisions file and rerun it; never switch to template filling, `fill_answers.py`, or patch/fix scripts.
6. Treat invoice issue dates as evidence only, except pure `other` expenses may temporarily use invoice issue date with a non-blocking advisory. Ask for the actual occurrence/record date when the date is not reliable: printed flight/rail date, hotel stay dates, Didi/Gaode trip-report time, or mobile month-end.
7. Ask grouped questions by expense type when many items need the same kind of answer. For example, combine meal questions into one list and let the applicant answer by item numbers and dates instead of asking every invoice separately.
8. Auto-match by expense type rather than by one generic city rule: hotels use stay/city evidence; meals first use natural-language notes such as `6.1 德克士 61.8` and score combined amount/date/merchant evidence instead of requiring seller, issue date, or amount to match exactly; meal amount columns and `Expense Nature` follow form over substance, so Shanghai invoice/restaurant city goes to `meal`/local and non-Shanghai goes to `travel`/business trip even when the meal is allocated to a business-trip project; meal policy is a separate axis, so `出差餐费` (including a Shanghai meal before or during travel) joins the RMB 150/day pool and only explicit `加班餐费` joins the RMB 60/day pool, never infer the cap from city, amount column, or `Expense Nature`; meals without explicit notes may use unique non-Shanghai city pre-allocation; taxi rides use journey/transfer logic, but taxi amount columns and `Expense Nature` also follow form over substance by ride city rather than assigned project, so Shanghai rides go to `taxi`/local and non-Shanghai rides go to `travel`/business trip; taxi notes must not write literal placeholders such as `出发地类型` or `目的地类型`; flight/rail use destination/date; `other` is not pre-matched by issuer city; `CORP-2026-ADMIN` is not a Shanghai project or fallback project for auto-matching.
   Railway-transfer addendum: first join same-day or tightly timed, station-connected tickets into one continuous journey chain, then allocate the whole chain. Intermediate stations are transfer nodes by default; outbound chains belong to the terminal/upcoming project, project-to-project chains to the latter project, and return chains to the project just completed. Each ticket keeps its own amount, proof number, and Note while sharing the project assignment. Ask one whole-chain question only when continuity is broken, multiple projects are equally plausible, or the applicant reports an actual stop in the intermediate city.
   User-record completeness addendum: put each concrete applicant expense record in either `meal_hints` or `expense_hints`, never both. Legacy cross-array copies are deduplicated by project/category/date/amount and any supplied merchant evidence. Every distinct record is reconciled back to an extracted item; a missing or ambiguous match creates an explicit chat question. Encode each answer as `matched_existing`, `covered_by_invoice`, `not_reimbursed`, or `pending_invoice`. Pending evidence remains blocking; a note the applicant confirms is erroneous or outside this claim can be closed as not reimbursed. Dropping previously linked evidence reopens the gate.
9. Never use `CORP-2026-ADMIN`, Client `通讯费`, or the mobile column as a fallback for unmatched taxi/travel/meal/hotel expenses. Unmatched transport should stay in the question queue unless transfer/travel logic assigns it.
10. Accept corrections such as "第9项金额不对" and trace the item back to its source filename before updating.
11. Preserve meal attendees from user notes, even when the meal is under the cap.
12. For `CORP-2026-ADMIN` rows, use `通讯费` as the Client for mobile expenses. For other admin expenses, use the specific matter name when known; otherwise use `项目、调研以外的其他费用` and paste a non-blocking chat prompt asking whether the applicant wants a more specific matter such as 年会、半年会、客户会、行业协会会议.
13. After writing the workbook, relay the script-generated meal daily cap check verbatim instead of recomputing it by workbook column. Business-trip meals are capped at RMB 150/day and local overtime meals at RMB 60/day; same-date rows under the same policy aggregate across the `meal` and `travel` columns. Over-cap days with attendee details are advisory, while over-cap days without attendee details require confirmation or a `reimbursable_amount` correction.
14. Paste or summarize the hotel cap check. Beijing/Shanghai/Guangzhou/Shenzhen hotels are capped at RMB 800/night and other cities at RMB 600/night; over-cap hotels with shared-room/co-occupant details are advisory, while missing nights or over-cap hotels without shared-room details require confirmation or a `reimbursable_amount` adjustment.
15. End with a submission-ready package summary only after the package files and manifest validate with no unresolved issues. If packaging exits with code `3`, the folder is a review package awaiting missing invoices, trip reports, or substitute approvals; resolve them in chat and repackage before submission.

## Dependencies and OCR

Every command path beginning with `skills/corplution-reimbursement-wizard` means the repository Skill root. When an agent runs elsewhere, it must invoke the installed Skill's `chief_orchestrator.py` by its exact absolute path. Do not create `run_chief.py`, modify `sys.path`, import Chief, or copy the script; Chief rejects wrapper launchers and prints the correct direct command.

Install Python dependencies with:

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run dependencies --install
```

For selectable electronic PDFs, Python dependencies are usually enough. For scanned PDFs or image invoices, OCR also needs system tools:

- Tesseract OCR, available on `PATH` as `tesseract`
- Poppler, available on `PATH` as `pdftoppm`, for rendering scan-only PDFs before OCR

Check OCR readiness with:

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run dependencies --strict-ocr
```

If OCR tools are missing, the extractor should mark scan-only/image inputs as `manual_review` and ask for confirmation instead of inventing invoice fields.

Chief starts every child in Python UTF-8 mode with `PYTHONIOENCODING=utf-8`, and direct entry scripts configure UTF-8 themselves, so normal runs should not need repeated environment prefixes. If Windows/Git Bash terminal output is still garbled or truncated, do not write temporary extraction scripts to bypass the workflow; read the UTF-8 Markdown process files already written by the scripts, such as `process/invoice-extraction.md`, `process/expense-allocation.md`, or `process/final-expense-rows.md`. For bulk allocation confirmations, use `compose_answers.py` to turn compact decisions into `allocation-answers.json`; it automatically dry-runs the updater. Do not directly patch `expense-allocation.json`.

## Scripts

```bash
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py status
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py next
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py next --json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run extract <files-or-folder>

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run correct-extraction --corrections process/extraction-corrections-input.json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run allocate --context project-context.json

# Run only when CHIEF NEXT detects transferable lineage decisions.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run rebase

# --set is for short ASCII values only; copy each full current N@ref token. Ranges are rejected.
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run compose \
  --set "3@a1b2c3d4,5@e5f6a7b8: status=confirmed"

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run compose --decisions process/batch-decisions.json

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run apply

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run trace --item 9

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py \
  run write --output <filled.xlsx> --requester <name>

# Optional: override generated-workbook layout
python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run write \
  --output <filled.xlsx> --requester <name> \
  --layout skills/corplution-reimbursement-wizard/assets/reimbursement-workbook-layout.toml

python skills/corplution-reimbursement-wizard/scripts/chief_orchestrator.py run package
```

The original scripts remain directly callable for developer debugging only. Normal workflow runs must invoke the bundled Chief directly, and only Chief-dispatched runs are journaled. Do not bypass a failed command with a wrapper, temporary helper, patch/fix script, or bundled-script modification.
For extraction, pass the upload/evidence folder or explicit source files, not a task root that also contains `process`, `output`, or the journal. Chief rejects that overlap so generated artifacts cannot be mistaken for new evidence.

If the workbook writer exits with code `3`, it has still written the workbook and `process/final-expense-rows.*`, but Stage 3 policy checks need applicant confirmation before final submission. Copy the `STAGE 3 REVIEW SUMMARY TO SHOW IN CHAT` block into the conversation and resolve the listed meal/hotel cap items.

If the workbook writer exits with code `2`, the `STAGE 3 PREFLIGHT CHECK TO SHOW IN CHAT` block found that allocation is not structurally ready, and no workbook was written. Resolve open questions, unconfirmed units, invalid categories/amount columns, missing date/client/code/amount fields, Admin/mobile conflicts, raw rail/flight notes, or missing taxi place types first.

If the packaging script exits with code `3`, it has written a review package and manifest but found blocking missing invoices, support files, or substitute approvals. Do not submit it or call the workflow complete; resolve the manifest issues and rerun packaging.

## Workflow Journal

- Each Chief-dispatched run appends `started` and `completed/failed` events to `process/workflow-journal.jsonl`.
- Events contain stage, script name, timestamp, exit code, duration, artifact fingerprints, and counts only. They omit raw invoice text, applicant answers, client details, full command arguments, and source paths.
- The journal is observational, does not participate in integrity decisions, never enters the final reimbursement package, and cannot replace a child script's exit code if logging fails.

## Notes

This repository is intended for Corplution internal reimbursement rules. It should usually be kept private because reimbursement materials may contain personal, client, and invoice information.
