# Corplution Reimbursement Wizard 使用指南

这份 Skill 使用Codex + ChatGPT 5.6 Sol开发，并采用Claude Fable 5审核，用标准流程与标准，结合Python脚本把报销材料整理成可提交的恺讯Excel报销申请表和文件包。正常使用时，你不需要理解脚本、JSON、OCR 配置或内部流程；只需提供材料，并在聊天中确认事实。

## 它能做什么

- 识别 PDF、扫描件和图片中的发票信息。
- 识别滴滴/高德行程单、付款凭证、替票审批截图等支持材料。
- 按飞机/高铁、酒店、打车、餐费、通讯费和其他费用整理材料。
- 根据项目、城市、日期和你的说明，将费用归集到客户与 Client Charge Code。
- 发现缺票、缺行程单、日期不明确、项目归属不清、餐费/酒店超标准等情况，并在对话中向你确认。
- 生成带项目小计、公式和校验的报销 Excel。
- 整理为一个报销文件包：Excel、发票、支持文档和核对清单。

## 安装

复制**整个** `skills/corplution-reimbursement-wizard` 文件夹，而不只是 `SKILL.md`。其中的脚本、规则、模板和参考资料都需要一起保留。

| 使用环境 | 安装位置 |
| --- | --- |
| Codex | `C:\Users\<你>\.codex\skills\corplution-reimbursement-wizard\` |
| WorkBuddy | `C:\Users\<你>\.workbuddy\skills\corplution-reimbursement-wizard\` |
| Claude Code（个人） | `~/.claude/skills/corplution-reimbursement-wizard/` |
| Claude Code（单一项目） | `<项目根目录>/.claude/skills/corplution-reimbursement-wizard/` |

WorkBuddy亦可通过“专家-技能-➕-上传技能”，并直接添加Zip压缩包的方式安装。

安装或更新后，重新打开一个 task/session 即可。

推荐的模型：DeepSeek V4 Pro/Flash（实惠）、Qwen-3.7、智谱清言GLM-5.1/5.2（效果更好但token消耗更大）。

首次运行时，Agent 会自行检查所需依赖。普通电子发票通常可以直接处理；扫描件或图片若需要额外 OCR 工具，Agent 会明确告诉你，而不会猜测内容。

## 怎么开始

在 Codex 或 WorkBuddy 中，可直接提及 Skill：

```text
$corplution-reimbursement-wizard
```

在 Claude Code 中，输入 `/corplution-reimbursement-wizard`，或直接描述 Corplution 报销任务。

然后自然地说明需求并上传材料，例如：

```text
请按 Corplution 报销流程处理我上传的发票和滴滴行程单。
本期是 6 月 1 日到 6 月 30 日；郑州项目是千味央厨，
Client Charge Code 是 CORP-2026-BD；北京项目是广联达。
6 月 8 日去机场的打车是去郑州项目，6 月 10 日餐费和张三一起。
```

不必填写表格，也不必自己整理成 JSON。没有显式调用入口时，直接说“帮我处理 Corplution 报销材料”即可。

## 应提供什么

尽量一次性提供同一报销周期内的材料：

1. **发票**：PDF、图片或扫描件。
2. **支持材料**：滴滴/高德行程单、付款凭证、审批截图等。
3. **项目说明**：项目日期、城市、客户名称、Client Charge Code，以及出差或接驳说明。
4. **补充事实**：实际发生日期、共同用餐人、加班、合住、部分报销、不报销、替票等。

不要预先筛选、分类或重命名文件。保留原始文件，直到整个报销包确认可提交。

## 过程中会发生什么

通常会有两到几轮对话：

1. **识别材料**：Agent 会列出识别结果，带原文件名、金额、日期和类别。你可以直接指出识别错误。
2. **归集项目**：Agent 会按类别集中问你不确定项，例如餐费属于哪个项目、打车是否为机场接驳、酒店住了几晚、某条记录是否还会补票。
3. **复核与补件**：如发现缺少行程单、审批截图或关键日期，Agent 会说明缺什么、为什么需要它。你可以补文件，或说明该项不报销。
4. **写表与检查**：Agent 会在写 Excel 前确认 Requester，并提示餐费/酒店标准、金额不一致或其他需要决定的事项。
5. **打包**：确认无阻断问题后，Agent 生成最终文件包。

如果中途新增或删除了材料，系统会重新核对受到影响的项目，并可能只追问变化部分。这是正常行为。

## 最终会得到什么

正常完成后，输出目录类似：

```text
报销申请表-{Requester}-{YYYYMMDD}/
  报销申请表-{Requester}-{YYYYMMDD}.xlsx
  发票/
    001-高铁-446.00.pdf
    002-酒店-499.00-专票.pdf
  支持文档/
    001-行程单.pdf
    002-合伙人审批.png
  package-manifest.md
  package-manifest.json
```

- Excel 按项目分块，包含小计、Total、Grand Total 和状态校验。
- 发票和支持文档会按编号、类型和金额重新命名，但原始文件不会被移动。
- `package-manifest` 记录包内文件和核对结果，便于追溯。

## 使用时请注意

- 看到“第 9 项不对”时，可以直接这样说；最好同时附上原文件名或错误点，Agent 会追溯后再改。
- 某条记录没有发票时，请明确告诉 Agent：由哪张发票覆盖、稍后补票，还是不报销。不要让它猜。
- 标明替票、加班、共同用餐人、合住人和部分报销金额，能显著减少追问。
- 若 Agent 说“review package”或“待确认”，该包还不能提交；先解决它列出的缺票、行程单、审批或政策确认问题。
- 发票、客户名、行程和审批截图可能含敏感信息，请只在获批准的私有空间中处理和保存。

需要修正时，可以直接用自然语言说明，例如：

```text
第 12 项金额和原发票不一致，请先查源文件再修改。
这张滴滴发票没有行程单，我晚点补；现在先保留为待确认。
6 月 3 日的餐费不报销，请关闭对应记录。
为什么这段高铁归到了北京项目？
```

你不需要手改过程文件。若流程卡住，只需让 Agent 查看当前状态并按它给出的下一步继续。
