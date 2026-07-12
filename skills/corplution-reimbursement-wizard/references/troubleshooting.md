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
| Exact SHA-256 duplicate / 完全相同文件 | Byte-identical copies are substantively equivalent. Propose the first indexed copy as canonical and exclude later copies by unique `source_file` or current `document_id`. Do not use their shared SHA to exclude only one copy. / 默认保留首个索引件，后续副本按唯一文件名或当前文档编号排除。 |
| Same invoice number but different SHA-256 / 同发票号但内容不同 | This is not an exact copy. Show both filenames and key fields to the applicant and obtain an explicit keep/exclude decision. / 需向申请人展示两份材料并确认。 |
| Composer reports an unknown field / Composer 字段错误 | Use the Composer field quick-reference table in `stage-2-allocation.md`, correct the same UTF-8 decisions file, and rerun Composer. / 查速查表，修正同一份 decisions 文件后重跑。 |
| Composer reports stale/different allocation fingerprint / answers 指纹过期 | Allocation was regenerated or changed. Discard the old generated answers, rerun Composer against the current allocation, then apply its new output. / 针对当前 allocation 重新 compose，不能重放旧 answers。 |
| Process JSON integrity failure, exit code 4 / 完整性校验失败 | Use the sanctioned updater for Stage 1/2, or regenerate Stage 3. Never repair the stamp or patch the JSON. / 用官方修正入口或重跑对应阶段，不修补印章。 |
| Terminal output is garbled or cannot print `¥` / 终端中文或货币符号乱码 | Read the UTF-8 Markdown artifact already written by the stage. Do not write a temporary extraction/printing script. / 直接读取 UTF-8 过程 Markdown，不另写临时脚本。 |
| Hotel cap still says city is missing / 酒店仍提示缺城市 | Set either `city` or `hotel_city` once through Composer; the updater mirrors the value. If both are supplied, they must be identical. / 通过 Composer 只填一个城市字段即可。 |
| Stage 3 exits with code 2 / 写表退出码 2 | Preflight failed and no deliverable workbook was written. Resolve the listed allocation fields/questions through Composer/updater, then rerun Stage 3. / 先解决结构化阻断项，再重跑写表。 |
| Stage 3 exits with code 3 / 写表退出码 3 | Workbook exists as a review output, but meal/hotel policy confirmations still block submission. Show the review summary and resolve it before packaging. / 工作簿仅供复核，处理政策确认后再打包。 |
| Package promotion raises `PermissionError` / Windows 打包文件锁 | Close the old packaged workbook and Explorer preview/windows inside the package folder, then rerun Stage 4 through Chief. The packager already retries transient locks. / 关闭 Excel 和资源管理器预览后经 Chief 重跑打包。 |

When the error is not listed, do not infer a workaround from the last failed command. Run Chief `status`, preserve the child exit code/output, and follow its recovery message.

若问题未列出，不要根据上一条失败命令猜绕行方案。重新运行 Chief `status`，保留原始退出码和报错，并按照恢复提示继续。
