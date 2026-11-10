# 03 运行时、预算与会话

## 一次 episode 的完整时序

```
 1  rubric 加载与版本校验
 2  初始化：AgentState(空) / HistoryView(v0) / Budget / SessionMode
 3  plan        Frontier + Scorer 决定下一动作 ───────[plan]
 4  预算预留     BudgetGuard.reserve("target") ─────── 不足 → terminate(budget)
 5  send        TargetClient.send(prompt, view) ──────[prompt_sent][view_snapshot]
 6  观察        response / defense_blocked ───────────[response] / [defense_blocked]
 7  状态更新    StateBackend.update → state_write ────[state_write]
 8  在线打分    Scorer.score → f_v（可选，按策略）─────[decide]
 9  decide      continue / retry / jump / stop ───────[decide]
10  回退（如）  View 版本 +1 ─────────────────────────[rollback]
11  装配        Assembler.assemble(traj, goal, k) ────[assembled]
12  在线停止判定 Judge_online(assembled) → 是否 stop ─[judge(phase=online)][terminate]
13  离线评价    Judge_eval(assembled) → Verdict ──────[judge(phase=offline)]（不计在线预算）
```

## 两个裁判必须分开

| | `Judge_online` | `Judge_eval` |
|---|---|---|
| 作用 | 决定"现在能不能停" | 决定"这次算不算成功" |
| 时机 | `terminate` 之前 | 可离线、可重跑 |
| 预算 | 计入在线预算 | 计入分析成本，不计入方法预算 |
| 可否换裁判 | 不能（否则停止行为会变） | 可以，且鼓励换 |

若两者共用同一个裁判，"在线停止"就会被优化目标污染：搜索会学会"在这个裁判眼里早点停"。

## 预算规则

- **两类成本分账**：`runtime`（episode 内，计入预算）与 `analysis`（重判、归因、反事实，事后，不计入方法预算但必须报）。
- **两种计数**：`logical`（逻辑请求）与 `served`（真实打到服务）。缓存只增加 logical。
- **持票调用**：规划、状态抽取、装配、判定、目标，全部经 `BudgetGuard`；未持票即实现错误，由离线审计脚本核对。
- **报告格式**：四元组 + 每样本中位数；装配与抽取的 token 单列，不许并入"其他"。

## 缓存语义

| 概念 | 含义 |
|---|---|
| 逻辑请求 | 方法发出的请求数 |
| 服务调用 | 真正到达模型/服务的次数 |
| 缓存命中 | 逻辑请求命中已有响应，不产生服务调用 |

规则：**方差估计只能用 `served=true` 的独立采样**；缓存命中不得进入方差或成功率的分母解释。

## 会话模式（决定"回退"是否真的生效）

| 模式 | 含义 | 影响 |
|---|---|---|
| `stateless` | 每次请求独立，历史由客户端拼装 | 回退完全有效 |
| `server_side` | 服务端自行保存会话历史 | 本地回退只是视图操作，服务端可能仍记得 |

`server_side` 模式下必须记录服务端 handle 与"服务端已见哪些 turn"；否则回退实验的解释无效，跨模式比较也不成立。

## 允许的预算检查点

只有两处需要"发送前预留"：目标调用（第 4 步）与在线判定（第 12 步）。其余步骤的消耗按实际结算即可——但**全部必须持票**，区别只是预留还是后结算。
