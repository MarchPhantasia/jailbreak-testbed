# 02 契约

契约一旦定下，后续所有实现服从它；改契约的代价随里程碑推进快速上升。

---

## 数据契约

### C1 Episode

| 字段 | 类型 | 说明 |
|---|---|---|
| `episode_id` | str | 全局唯一，形如 `ep_0007` |
| `method` | str | 方法名 + 版本，如 `baseline@v1` |
| `dataset_item` | obj | 请求、类别、rubric 版本 |
| `budget` | obj | 上限四元组：`target_calls / judge_calls / tokens / wall_ms` |
| `seed` | int | 采样种子，写入所有事件 |
| `session_mode` | enum | `stateless` / `server_side` |
| `config_hash` | str | 配置指纹，用于"同配置"判定 |

不变量：一个 episode 结束必须有且仅有一条 `terminate` 事件，携带结束原因。

### C2 Event（append-only）

| 字段 | 说明 |
|---|---|
| `seq` | episode 内单调递增 |
| `type` | 见下表 |
| `payload` | 类型相关 |
| `cost` | `target_calls / judge_calls / tokens_in / tokens_out / wall_ms` |
| `served` | 是否真的打到了外部服务（缓存命中为 false） |
| `budget_after` | 结算后剩余额度 |
| `tags` | `turn:n`、`attempt:m`、`view:v`、`component:*` |

事件类型（最小集）：

| 类型 | 何时写 | 为什么不能省 |
|---|---|---|
| `plan` | 产生下一步动作 | 复现决策序列 |
| `prompt_sent` | 发请求前 | 记录逻辑请求 |
| `view_snapshot` | 发请求前 | 记录**实际携带的历史原文**；回退实验的对照组 |
| `response` | 收到回复 | 原始观测 |
| `defense_blocked` | 被防御拦截 | 区分"目标拒绝"与"过滤器拦截" |
| `state_write` | 状态被更新 | 记录**抽取器实际写入了什么**及版本 |
| `assembled` | 装配产出 | 记录 k 份候选、选了哪份 |
| `judge` | 判定 | 在线与离线分别标记 `phase` |
| `decide` | 控制流决策 | 复现搜索过程 |
| `rollback` | 回退 | 抬高 View 版本，不删历史 |
| `cache_hit` | 命中缓存 | 方差与成本都必须按真实调用算 |
| `terminate` | 结束 | 结束原因 |

### C3 AgentState 与 HistoryView

```json
AgentState = {
  "facts":  [{"id","text","src_events":["e12"],"confidence","fidelity":"verbatim|paraphrase|inferred"}],
  "open":   [{"id","goal","why_open"}],
  "failed": [{"id","attempt","reason_class","src_events":["e19"]}],
  "extractor_version": "x1",
  "budget_left": {"target_calls":12,"depth":3}
}
HistoryView = {"view_id":"v3","visible_events":["e1","e3","e7"],"n_turns":3}
```

不变量：

1. `facts[].src_events` 非空——**没有出处的事实不允许进状态**。
2. `fidelity` 必填。出处只证明"引用了哪里"，证明不了"抽取是否正确"；`inferred` 比例超过 20% 触发告警。
3. `reason_class` 是枚举（`refusal|vague|offtopic|circular|premature`），不允许自由文本。
4. 状态存命题，不存原始对话；原文按事件 id 取。
5. `HistoryView` 与 `AgentState` 允许不同步——这正是回退实验要研究的对象。

### C4 Verdict

```json
Verdict = {
  "rubric_version":"sr.v1","phase":"online|offline",
  "class":"A|B|C|D","coverage":0.62,
  "claims":[{"id":"g1","covered":1,"support":"T|S|M","evidence_event":"e12"}],
  "judge_id":"…","confidence":0.8,
  "agreement":{"second_judge":"…","same_class":true}
}
```

不变量：判定落到**主张级**；`support` 只表示"有依据"，"是否被使用"由反事实重放单独给出 `dependence`。

---

## 接口契约

| 编号 | 接口 | 签名 | 语义约束 |
|---|---|---|---|
| I1 | `TargetClient` | `send(messages, view_id) -> Response \| Blocked` | 只能看到该 `view_id` 的历史；不得自行回退 |
| I1b | `Defense` | `wrap(TargetClient) -> TargetClient` | 拦截必须产生 `defense_blocked` |
| I2a | `Frontier` | `select(candidates) -> node` | UCT / best-first / BFS / 线性推进都在这里 |
| I2b | `Scorer` | `score(node, obs) -> float` | 混合评分、ΔF/Δc、信息增益都是它的实现 |
| I2c | `Controller` | `decide(obs, state, budget) -> {continue,retry,jump,stop}` | 只做控制流，不做打分 |
| I3 | `StateBackend` | `update(event) -> AgentState`；`render(state) -> str` | 必须可从事件流重建；`render` 有 token 上限 |
| I4 | `MemoryStore` | `query(keys, k) -> [Skill]`；`update(skill, outcome)` | 结构化检索键；有退役；不存原始提示全文 |
| I5 | `Judge` / `Rubric` | `judge(goal, answer, rubric, phase) -> Verdict` | rubric 对攻击者不可读；版本号进 Verdict |
| I6 | `Assembler` | `assemble(trajectory, goal, k) -> Answer` | 多段回答 → 最终答案的唯一入口 |
| I7 | `BudgetGuard` | `reserve(kind, est) -> ticket`；`settle(ticket, actual)` | 所有模型调用必须持票，未持票调用视为实现错误 |
| I8 | `SessionMode` | `stateless` / `server_side(handle)` | 决定"回退"能否让目标真正忘记 |

每个接口都必须定义三种失败行为：超时、解析失败、预算不足；行为本身也要落事件。
