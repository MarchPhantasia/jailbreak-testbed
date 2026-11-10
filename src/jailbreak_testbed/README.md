# jailbreak_testbed（参考模型，非运行路径）

## 定位

这是一个**独立的 M0 参考模型**：在一个进程内用最少的代码实现"事件 + 账本 + 回放 + 状态"，
用于交叉核对 `src/framework` 的对应语义。它**不被** `framework`、`runner` 与 CLI 引用，
也不参与任何实验产物。

## 与 framework 的关系

| 能力 | framework（运行路径） | 本包（参考模型） |
|---|---|---|
| 事件 | `events.EventStore`，带 node/view/attempt/phase | `EventLog`，只保留 seq/type/cost |
| 预算 | `BudgetGuard`（reserve→dispatch→settle，拒答不退） | `Budget`/`Ledger`（同样规则，接口更窄） |
| 状态 | `state` 三后端 + 快照恢复 | `Fact`/`FailedItem` 等纯数据对象与校验 |
| 回放 | `runner.restore_episode`（只读事件计数） | `replay()`，含 **recompute 差分与 rollback 语义** |

冲突时以 `docs/manual/v3.5.md` 与 `src/framework` 为准。

## 已知差异与后续

- 本包有 `recompute`（新抽取器重算）的差分报告，framework 目前只有派生目录占位；
  计划把该差分迁入 framework 的 analysis 层，迁移完成后再删除本包。
- 本包的测试是 `unittest` 风格（`tests/test_events.py`、`tests/test_replay.py`、`tests/test_state.py`），
  与 framework 的 pytest 套件并行运行。

## 决策记录

- 2026-11-11：保留并显式标注为参考模型（而不是删除或 re-export）。
  理由：其 recompute/rollback 语义是 framework 尚未覆盖的能力，先保留可复核的实现比丢失更安全；
  代价是仓库里存在两套同类实现，故在此明确边界与迁移计划。
