# Opening Message

Use this Chinese opening message when the user invokes the skill without already providing all source files and project context. Adapt lightly to the actual request, but keep it short and action-oriented.

```text
可以，我来按 Corplution 的报销流程帮你处理。

你可以先把这些材料直接发给我：

1. 发票文件：PDF、图片、扫描件都可以。
2. 行程单/报销单：比如滴滴、高德等支持文件。
3. 项目说明：可以是自然语言，不用整理成表格。比如这个月几号到几号、在哪个城市、对应哪个客户、Client Charge Code 是什么。
4. 任何你觉得有帮助的补充：比如哪张是替票、哪笔是加班、餐费和谁一起、某张票不报销等。

过程中你可以随时补充材料或文字说明。我会先识别并整理发票和行程，再把不确定的地方直接在对话里问你确认，最后输出填好的报销 Excel 和整理好的文件包。
```

If the user already provided some files, acknowledge them briefly and ask only for the missing high-level context. Do not require a rigid form.
