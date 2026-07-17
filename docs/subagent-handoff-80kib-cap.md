# 结论备忘：双子 Agent 检查点在大额报销下"被绕开"

> 状态：**已解决（2026/07/16）**。原排查结论 + 备选方案见下方（保留作历史）。
> 相关脚本：`skills/corplution-reimbursement-wizard/scripts/subagent_protocol.py`、`SKILL.md`

## 已解决：本轮定案与实现（2026/07/16）

**决策（对应"下次讨论要定的点"）**

1. **cap 抬到 384 KiB**（`MAX_HANDOFF_PACKET_BYTES = 384 * 1024`）。依据：packet 是整体贴进 fresh Agent/Task prompt 的 inline 预算（非传输限制）；固定 ~10 KiB + ~870 B/单元下 384 KiB 可容 ~430 单，远超现实体量，且对 200K 上下文模型 inline 仍有余裕。
2. **分片：本轮不做**。cap 抬高 + fail-open 明确化已足以断掉"被绕开"；分片（按 charge_code / 单元块切分 + 各自指纹 + 结果合并）留作治本后续。
3. **超 cap = 干净 fail-open 降格，不再 hard-raise 误导**。

**实现**

- 新增异常 `HandoffTooLarge(ProtocolError)`，携带 `display_name` / `packet_bytes`；文案只讲"降格到确定性 Stage-3 preflight、禁止手动拼包/手工 accept"，删去原来"用 attachment / 拆成 scoped packets"这两个在 Claude Code 上根本走不了的误导出路。
- cap 校验从 `_build_task` 移出（size 只对 inline handoff 有意义，不该让 accept/`audit_state` 的指纹重算因体量失败），改由新 helper `_enforce_handoff_cap(task)` 在两处把关：
  - `prepare_task`：**写任何文件之前**校验；超限 → 抛 `HandoffTooLarge`，不留半个 handoff。`main` 的 `prepare` 分支捕获后打印 `DEGRADE` 并 **exit 0**，Chief 照常走到 Stage-3 write，既有 fail-open（无受理 audit → preflight 权威）自然接管。
  - `accept_result`：超限 → 抛 `HandoffTooLarge`，`main` 的 `accept` 分支 **exit 2 拒绝**（降格任务的任何 result 必是手工拼的）。
- `SKILL.md:322` 同步：80 KiB → 384 KiB，并加"超 cap 则 prepare 降格、accept 拒绝、直接走 preflight、禁止手动拼包"一句。

**测试**：`tests/test_subagent_audit.py` 新增 8 例（cap 值、异常形态、helper 上下界、prepare 降格不落盘、accept 拒绝、main 路由 prepare exit0 / accept exit2、under-cap 落盘）。全量套件 **45 通过**。

---


## 一句话结论

subagent 检查点被绕开**不是 Agent 偷懒，是协议在真实体量下"设计上跑不通"**，Agent 于是走了不该走的手动旁路（自己拼包、手工 accept），把审计的完整性保证绕没了。

## 证据

1. **80 KiB 上限是设计写死的**
   - `SKILL.md`：packet 是"路径无关、上限 80 KiB 的不可变数据快照"。
   - `subagent_protocol.py`：`MAX_HANDOFF_PACKET_BYTES = 80 * 1024`。
   - `_build_task`：先做压缩快照（角色字段裁剪、`PACKET_TEXT_LIMIT=480` 截文本、`PACKET_NESTED_LIST_LIMIT=24` 截列表），然后
     `if packet_bytes > MAX_HANDOFF_PACKET_BYTES: raise ProtocolError(...)`，报错原文让你"用只读资源附件或把 review 拆成 scoped packets"。

2. **上限在真实体量下够得着**
   - 实测：固定开销 ~10 KiB（role instructions + schema + contract）+ **~870 B/单元**（压缩后）。
   - → 72 单 ≈ 78 KiB，**约 74–75 单越过 80 KiB**；且这只算 1 份证据文档，真实每单带发票时 `evidence_index` 更大，越界更早。
   - → 70+ 单的正常报销，`prepare-agent` 会直接 `raise ProtocolError`。

3. **Claude Code 上两个"逃生出口"都不存在**
   - `SKILL.md`：Claude Code **没有 attachment 通道**，只能把 JSON 贴进 prompt。
   - **"拆成 scoped packets" 根本没实现**——一个角色只产一个整包，无分片 CLI。
   - → 压缩包超 80 KiB 时硬失败，报错指的两条出路在 Claude Code 上都走不了。

4. **Agent 做的是脱离协议的手动旁路**
   - "手动执行两份 review + 绕过 size cap 完成 accept" = 自己拼包、手工 accept，跳过了 `accept-agent` 的**指纹绑定**与**不可变归档**校验。
   - 它引用的"218 KB"多半是**原始 allocation/extraction**（非压缩包）；无论如何都触顶。
   - 这比"包太大"更严重：绕过的正是让审计可信/防篡改的护栏。

5. **本该 fail-open**
   - SKILL 留了正道：宿主起不了独立子 Agent 或包超限时，**降级到确定性 Stage-3 preflight**。
   - Agent 没降级反而伪造独立通道 = 判断失误 + 协议诱导（报错把"拆包/附件"写得像可行，实际不可行）共同导致。

## 备选方案（可组合）

1. **抬高 cap**：80 KiB 对现代模型 inline 太保守，200–300 KiB 完全能塞进 prompt。成本最低。
2. **真正实现分片**：按 `client_charge_code` 或按单元块切成多个 ≤cap 的 scoped packet，各自指纹绑定、结果合并。最治本，工作量最大。
3. **压得更狠**：快照再砍字段/再缩文本，让每单 <870 B。
4. **明确 fail-open 门槛**：Claude Code 上超过 N 单就文档化"降级到确定性 preflight"，堵死手动拼包的诱惑。

## 倾向

**1（先把 cap 抬到现实值）+ 4（把降级说清楚、禁止手动拼包）** —— 成本最低、最快消除"被绕开"。分片(2)留作治本后续。

## 下次讨论要定的点

- cap 抬到多少？依据是什么（inline prompt 预算 / 宿主限制）？
- 要不要做分片？切分维度（按 charge_code 还是按单元数）＋ 结果合并/整体指纹如何绑定？
- fail-open 门槛写死在哪（`subagent_protocol.py` 还是 `SKILL.md` 指引）？超限时报错文案怎么改，明确"降级、不要手动拼包"？

## 本轮已完成（背景，非本议题）

- 7 个绿盾加密脚本已解密为明文并提交（`dce570d`）。
- 坑2（`write_reimbursement_template.py` 的 `travel_destination_context` 加 Admin 排除）+ 坑1-A（`extraction_corrections.py` needs_review 提示 log）已改并加回归测试 `tests/test_travel_destination_admin.py`，全量套件 37 通过。**尚未提交。**
