# Troubleshooting And Recovery / 故障排查与恢复

Use this page only after a normal Chief-dispatched command fails or the next step is unclear. Run:

```bash
python scripts/chief_orchestrator.py status
python scripts/chief_orchestrator.py next
```

Follow the single current `CHIEF NEXT` action. Do not create launchers, helper/fix scripts, or edit integrity-stamped process JSON.

先运行 Chief `status` / `next`，再执行唯一的 `CHIEF NEXT`。不要创建启动器、临时 helper/fix 脚本，也不要手改带完整性印章的过程 JSON。

| Symptom / 现象 | Meaning and recovery / 原因与恢复 |
| --- | --- |
| OFD, EML, ZIP, or another unsupported file remains open / 不支持文件阻断 | Ask whether to exclude it or use a readable replacement. Write `input_resolutions` with nested `match`, then run `chief_orchestrator.py run correct-extraction --corrections <file>`. See the complete JSON example in `stage-1-output.md`. / 询问排除还是提供转换件，用嵌套 `match` 写入 `input_resolutions`。 |
| `input resolution needs a match key` / 提示缺少匹配键 | Use `"match": {"sha256": "..."}` or `"match": {"source_file": "..."}`. Do not put `sha256` or `source_file` at the entry root. / `sha256`、`source_file` 必须位于 `match` 对象内。 |
| Exact SHA-256 duplicate / 完全相同文件 | Byte-identical copies are substantively equivalent. Propose the first indexed copy as canonical; exclude each later copy with `match: {sha256: <shared>, source_file: <exact path>}`. SHA-only exclusion is rejected because it cannot identify one physical copy. Chief blocks zero or multiple active copies. / 默认保留首个索引件；排除副本时同时填写共享 SHA 与该副本精确路径。仅用 SHA 会被拒绝，且每组必须恰好保留一份。 |
| Same invoice number but different SHA-256 / 同发票号但内容不同 | This is not an exact copy. Show both filenames and key fields to the applicant and obtain an explicit keep/exclude decision. / 需向申请人展示两份材料并确认。 |
| Composer reports an unknown field / Composer 字段错误 | Use the Composer field quick-reference table in `stage-2-allocation.md`, correct the same UTF-8 decisions file, and rerun Composer. / 查速查表，修正同一份 decisions 文件后重跑。 |
| Composer reports a stale allocation generation (for_allocation_fingerprint mismatch) / decisions 代际过期 | Discard BOTH the old generated answers AND the old generation-bound decisions file. Run Chief `next`: when project contexts, policy, and allocation engine revision are unchanged, it discovers the fingerprinted generation lineage, rebases unchanged unit and applicant-record decisions, then routes the result through Composer/updater. If any basis changed, re-review from scratch. Never edit the fingerprint, reuse bare numbers, choose a `.bak`, or hand-pick an older generation. / 旧 answers 与旧 decisions 一并作废；运行 Chief `next`，由指纹历史链自动选择可迁移代并依次执行 rebase、Composer、updater；业务基础变化则从头复核，禁止改指纹、复用裸编号或手选旧备份。 |
| Allocation was regenerated twice before rebase / 归集重跑多次 | Do not restore or rename files. Each official write is preserved in `process/allocation-generations/`; Chief/rebase walks past fresh drafts and selects the nearest lineage generation containing official user decisions. / 不要恢复或改名文件；Chief 会沿不可覆盖的历史链跳过新草稿，找到最近的正式用户决定代。 |
| Composer says lineage rebase is pending / 提示必须先迁移代际决定 | Do not compose ordinary answers first. Run the exact Chief `next` rebase command, then follow its Composer and updater commands. This gate also records a valid zero-carry result, so it cannot be bypassed by writing one new answer first. / 不要先写普通答案；严格按 Chief `next` 依次运行 rebase、Composer、updater。即使没有可迁移项也会正式记录 clearance，禁止用先写一条新答案的方式绕过。 |
| Chief recommends rebase again immediately after rebase / rebase 后 Chief 仍重复推荐迁移 | Run Chief `lineage`. A current packet must route next to Composer; a stale/invalid packet now reports its source/target mismatch and must be regenerated with the exact current `CHIEF NEXT` command. Do not repeat blindly or hand-pick an archive. This state-routing issue is separate from a hint resolution that references a closed unit. / 运行 Chief `lineage`：当前迁移包下一步必须是 Composer；过期或无效包会显示源代/目标代不符原因，照当前 `CHIEF NEXT` 精确重生成，禁止盲目重复或手选归档。它与费用记录引用已关闭项是两类问题。 |
| Chief lists `M@ref` removed evidence / Chief 列出旧代际消失证据 | Show every listed filename, amount, and date/category in chat. Ask whether it was intentionally omitted, replaced by exact current `N@ref` item(s), or must be restored. Fill the generated `rebase-removal-resolutions.json` internally and rerun the exact Chief `rebase --resolutions` follow-up. `restore_required` remains blocked until Stages 1/2/rebase are regenerated. Never delete the entry or edit/stamp `rebase-decisions.json`. / 在聊天中逐项确认“确实移除、由当前哪项替代、还是需要恢复材料”；内部填写预生成模板后按 Chief 命令重跑。需要恢复时继续阻断，禁止删项或手改迁移包。 |
| Rebase hint resolution cites a unit also carried as dropped/excluded / 迁移的费用记录仍引用同批关闭项 | Rerun rebase with the current scripts. It prunes closed links from multi-unit matches; if no active link remains, it deliberately leaves that `R@ref` record open for confirmation. Composer stays strict. Never edit `rebase-decisions.json` or recalculate its integrity stamp by hand. / 使用当前脚本重跑 rebase：多项匹配会自动去掉已关闭关联；若全部关闭，则保留该费用记录为未决并重新确认。Composer 继续严格拒绝冲突，禁止手改 decisions 或自行重算印章。 |
| Allocation lineage archive is missing/invalid / 代际归档缺失或验章失败 | Stop: Chief, Composer, and updater intentionally block. Recover the exact referenced stamped file under `process/allocation-generations/`; if that is impossible, preserve explicit user facts by source descriptors and use the clean sibling-batch rebuild below. Never delete the pointer, forge a stamp, or continue with ordinary answers. / 立即停止；恢复指针所指的验章归档。无法恢复时按下文来源描述保留事实并建立干净的同级批次，禁止删指针、伪造印章或继续写普通答案。 |
| Composer rejects a same-generation entry (field name, JSON, encoding, or @ref of a corrected item) / 同代际条目被拒 | Fix that entry in the SAME decisions file and rerun Composer — this recovery applies only when the generation matches. An @ref mismatch on a corrected item means its evidence changed: re-verify it against the current review list before rebuilding the entry. / 同代际错误才允许改同一文件重跑；@ref 失配说明该项证据已变，须先重核。 |
| Subagent result is rejected as stale or has an unknown `N@ref` / 子 Agent 结果代际或引用失效 | Discard the result, rerun Chief `prepare-agent` against the CURRENT allocation, and give the fresh subagent the complete new task/template contents. Never replace only the fingerprint or refs. / 丢弃旧结果，从当前 allocation 重新生成完整任务；禁止只换指纹或编号。 |
| Otako produced proposals / Otako 已给出建议 | The `.unreviewed.json` packet is not an applicant decision and Composer rejects it. Review/select proposal IDs, confirm uncertain facts, run Chief `promote-proposals`, then give Composer the stamped `.reviewed.json` output. / 原始建议不能入账；先按 ID 复核选择并 promote，再将盖章后的 reviewed 文件交给 Composer。 |
| Kaede review blocks Stage 3 / Kaede 复核阻断写表 | Resolve each cited item through Composer/Updater. The allocation fingerprint then changes, so rerun Kaede before Stage 3. Accepted results are retained in an immutable task-generation archive; deleting or editing the sidecar cannot clear the blocker. / 按引用项正规修正，随后对新代际重新复核；受理结果另有不可变世代归档，删改 sidecar 不能解除阻断。 |
| No subagent runtime is available / 宿主不支持子 Agent | Preferred checkpoints apply only when the host exposes a genuinely fresh isolated Agent. If unavailable (or the user opts out), continue with the deterministic workflow. Missing/unavailable review is fail-open, never `pass`; workflow difficulty alone is not a reason to skip when the capability exists. / 有独立 Agent 能力时默认先跑；仅能力不存在或用户明确跳过时降级到确定性流程，不能伪造通过。 |
| Process JSON integrity failure, exit code 4 / 完整性校验失败 | Use the sanctioned updater for Stage 1/2, or regenerate Stage 3. Never repair the stamp or patch the JSON. / 用官方修正入口或重跑对应阶段，不修补印章。 |
| A wrong correction/overlay keeps returning after repeated repair / 错误修正被持续重放 | Stop adding corrective patches. Preserve explicit user-confirmed facts as the replay checklist defined below, start a clean sibling batch from the original evidence plus legitimate new files, and rebuild. Do not copy the contaminated `process/`, answers, workbook, or package into the clean batch. / 停止叠加修补；按下文事实清单保留用户确认内容，从原始证据建立干净批次。 |
| Terminal output is garbled or cannot print `¥` / 终端中文或货币符号乱码 | Read the UTF-8 Markdown artifact already written by the stage. Do not write a temporary extraction/printing script. / 直接读取 UTF-8 过程 Markdown，不另写临时脚本。 |
| Hotel cap still says city is missing / 酒店仍提示缺城市 | Set either `city` or `hotel_city` once through Composer; the updater mirrors the value. If both are supplied, they must be identical. / 通过 Composer 只填一个城市字段即可。 |
| Stage 3 exits with code 2 / 写表退出码 2 | Preflight failed and no deliverable workbook was written. Resolve the listed allocation fields/questions through Composer/updater, then rerun Stage 3. / 先解决结构化阻断项，再重跑写表。 |
| Stage 3 exits with code 3 / 写表退出码 3 | Workbook exists as a review output, but meal/hotel policy confirmations still block submission. Show the review summary and resolve it before packaging. / 工作簿仅供复核，处理政策确认后再打包。 |
| Package promotion raises `PermissionError` / Windows 打包文件锁 | Close the old packaged workbook and Explorer preview/windows inside the package folder, then rerun Stage 4 through Chief. The packager already retries transient locks. / 关闭 Excel 和资源管理器预览后经 Chief 重跑打包。 |

## Clean-Rebuild Fact Mapping / 干净重建事实映射

Use a clean rebuild only when a semantically wrong durable correction/overlay has made repeated sanctioned repair unreliable. Preserve the old task folder for audit, create a sibling task/batch, and feed only the original evidence plus legitimate newly supplied evidence into Stage 1. Do not copy old `process/*.json`, `extraction-corrections.json`, `allocation-answers.json`, `batch-decisions.json`, final rows, workbook, or package into the clean batch.

仅当错误的持久化 correction/overlay 导致正规修复反复失败时，才进行干净重建。保留旧任务目录供追溯，在旁边建立新批次；Stage 1 只接收原始材料和用户后来合法补充的材料，不复制旧过程文件、answers、工作簿或报销包。

### What Counts As A User-Confirmed Fact / 什么是用户确认事实

A replayable user-confirmed fact must satisfy all four conditions:

1. The applicant explicitly stated it, or explicitly confirmed a proposed conclusion shown in chat.
2. It records a business decision, such as project/client/code, actual date, attendee, place type, hotel stay, reimbursable amount, substitute status, evidence exclusion, or `not_reimbursed`/`pending_invoice` outcome.
3. It is expanded from the old item number into the evidence description required below.
4. The old item number is retained only as historical context, never as the replay target.

可重放事实必须同时满足：由申请人明确陈述或确认；内容是业务决定；已经从旧编号展开为下述来源描述；旧编号仅作为历史备注，绝不能作为重放目标。

Model inference, auto-matching, generated Notes, statuses, confidence values, proof numbers, formulas, fingerprints, and old `DOC-xxx` / `UNIT-xxx` / displayed item / `R1` identifiers are derived state. Recompute them in the clean batch instead of carrying them forward.

模型推断、自动匹配、生成的 Note、状态、置信度、凭证号、公式、指纹以及旧 `DOC/UNIT/显示编号/R` 都属于派生状态，必须在新批次重新计算。

### Minimum Replay Descriptor / 最低映射字段

Before abandoning the contaminated batch, summarize the confirmed facts in chat or agent working notes using the following descriptors. Expand a grouped answer into one record per logical expense.

Separate identity evidence from the decision being replayed. A field that the applicant corrected is part of the decision, not a matching discriminator: do not require the new batch to reproduce the old wrong OCR/category/date value before applying its correction. Match with the original filename and other unaffected evidence fields instead.

必须区分“用于找回材料的身份字段”和“用户要求改成什么的决定字段”。被用户纠正的字段本身属于决定，不能反过来要求新批次先识别成该值；应使用原始文件名及其他未受影响的字段定位。

| Logical record / 记录类型 | Required source description / 必需来源描述 | Examples of replayed decisions / 可重放决定 |
| --- | --- | --- |
| Invoice-based expense / 普通发票费用 | Original source filename; invoice number when present; seller/provider; invoice amount; add travel route/date or hotel stay when applicable | Client, code, expense date, category correction, hotel nights, reimbursable amount, substitute status |
| Didi/Gaode ride / 滴滴高德单笔行程 | Trip-report filename; ride date/time; amount; origin and destination; linked invoice filename/number when available | Project assignment, origin/destination place types, overtime/business purpose |
| Supporting document / 支持文件 | Original filename; document role; linked invoice filename/number or the business matter it supports | Trip-report link, substitute approval link, payment-proof role, exclusion reason |
| Applicant expense hint / 用户文字费用记录 | Original note text; project context; supplied date, amount, merchant, and attendees when present | Matched invoice, combined-invoice coverage, `not_reimbursed`, or `pending_invoice` |
| Project context / 项目上下文 | Client name; charge code; city; date range; project/event description | Project identity, travel window, Admin matter description |
| Exact duplicate-copy decision / 完全重复副本决定 | Canonical source filename and each duplicate source filename; shared SHA may be recorded only to prove content equality | Keep canonical copy; exclude specifically named duplicate copies |

Example replay checklist entries:

```text
Historical ref only: old item 9
Identity: 12306-ticket.pdf | invoice 2631... | seller 12306 | amount 196.00 | route Taiyuan-Zhengzhou
User-confirmed decision: client Qianwei | code CORP-... | expense date 2026-06-01 | note high-speed rail refund fee

Historical ref only: old item 32
Identity: Didi-trip-report.pdf | ride 2026-06-08 07:42 | amount 19.90 | Home -> Shanghai Hongqiao
User-confirmed decision: destination project Qianwei | origin type Home | destination type Railway station
```

### Rebinding Rule / 重绑定规则

After Stage 1 and Stage 2 regenerate the new review lists:

1. Ignore every old numeric/internal identifier.
2. Find the current candidate by the required source description.
3. Compare every populated corroborating field from the checklist. Any conflict means the fact is not safe to replay.
4. Replay automatically only when exactly one current logical record matches and no populated field conflicts.
5. If zero or multiple records match, show the candidates with their new item numbers and ask the applicant. Do not guess, choose by proximity, or apply the old number.
6. Write a fresh `allocation_decisions.v1` using only the new current item numbers, then run Composer and the updater normally.

重建后的规则是：旧编号全部作废；按来源描述查找；所有已有核对字段必须一致；只有唯一且无冲突的匹配才能复用。零匹配、多匹配或字段冲突都必须列出新编号询问用户。最终只用新编号生成新的 decisions，正常经过 Composer/updater。

When the error is not listed, do not infer a workaround from the last failed command. Run Chief `status`, preserve the child exit code/output, and follow its recovery message.

若问题未列出，不要根据上一条失败命令猜绕行方案。重新运行 Chief `status`，保留原始退出码和报错，并按照恢复提示继续。
