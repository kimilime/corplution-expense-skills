# Opening Message

Use this Chinese opening message when the user invokes the skill without already providing all source files and project context. Adapt lightly to the actual request, but keep it short and action-oriented.

```text
可以，我来按 Corplution 的报销流程帮你处理。

你可以先把这些材料直接发给我：

1. 发票文件：PDF、图片、扫描件都可以。
2. 行程单/凭证：滴滴、高德行程单，以及付款凭证、审批截图等。
3. 项目说明：自然语言就行，不用做成表格。信息越明确，匹配越准、追问越少——最好能说清每个项目分别哪天吃了什么、多少钱、哪几天住哪；只给“几号到几号在哪个项目、哪个城市”也可以，我会按发票开票地和报销标准帮你匹配到天。
4. 特殊说明（有就说，没有也没关系）：某天的特殊餐标（如年会自理餐标 7.17=60、7.18=150）、客户办公地点、替票、加班、和谁一起用餐、合住、某笔不报销等。

过程中随时可以补充材料或文字。我会先识别整理发票和行程，把不确定的地方直接在对话里问你确认，最后输出填好的报销 Excel 和整理好的文件包。
```

If the user already provided some files, acknowledge them briefly and ask only for the missing high-level context. Do not require a rigid form.
