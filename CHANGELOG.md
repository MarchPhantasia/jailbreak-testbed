# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号对应**规格版本**，不对应代码版本。

## [Unreleased]

### 已知问题（版本管理侧记录）

- `src/jailbreak_testbed/`（7 个文件）与 `src/framework/`（17 个文件）在 budget / events / state / replay 上概念重复；前者仅被 3 项早期测试使用（`test_events.py`、`test_replay.py`、`test_state.py`）。待决定：删除，或改为对 `framework` 的 re-export。
- `.gitignore` 中的 `../.venv/` 是无效模式（git 忽略含 `..` 的规则），已移除；虚拟环境应放在仓库内并由 `.venv/` 覆盖。

## [code-v0.1.0] — 2026-11-11

### 新增（实现）

- `src/framework/`：契约、事件与账本、预算守卫、接入层（Mock / OpenAI 兼容）、三种状态后端（原文 / 摘要 / F-O-X）、前沿与策略、装配与候选、在线与最终评价、执行引擎、控制器、恢复、数据集适配、实验编排、CLI。
- `fixtures/synthetic_library/`：3 条合成任务（含拒绝样本），用于 M0 闭环。
- `configs/`：experiment / endpoints / datasets / stages 四份样例。
- `tests/`：59 项，覆盖手册 §18.1 的必要检查（预算不可绕过、拒答不退款、切换不退款、恢复不调模型、节点隔离、视图正确、出处存在、未知评分不为零、缺项不计覆盖、结果判定顺序）。

### 变更

- `requires-python` 保持 `>=3.10`；新增运行时依赖 `PyYAML` 与入口 `jbt = framework.cli:main`。
- 移除 `configs/example.yaml`（旧的规格期配置，已被 `configs/experiment.yaml` 取代）。

### 已知缺口

- 归因三层（节点/事实/前缀）只有骨架；LLM 控制器与真实 endpoint 未联调；双裁判与人工切片未实现。


## [spec-v1.1] — 2026-11-10

### 变更（相对 v1.0）

- 生命周期补上装配步骤，并把「在线停止判定」与「离线成功判定」拆成两个裁判。
- 新增四类事件：`state_write`、`cache_hit`、`view_snapshot`、`assembled`。
- 搜索拆成 `Frontier` / `Scorer` / `Controller` 三个接口；打分策略从写死的规则变成可插拔实现。
- 新增 `BudgetGuard`（所有模型调用持票）与 `SessionMode`（回退是否真的生效）。
- 统计从「重复 N 次 + Δ>2σ」改为配对差值 + 置信区间 + 预定义效应量；缓存命中不得进入方差。
- 新增 M2.5「忠实基线」硬门槛。

## [spec-v1.0] — 2026-11-10

### 新增

- 目标函数：同 `target_calls` 预算下的 ASR；其余指标降为护栏。
- 数据契约 C1–C4、接口契约 I1–I8。
- 五个实验配置 B0–B4、四个假设 H1–H4。
- 里程碑 M0–M5、风险登记 R1–R8。
- ADR-0001 至 ADR-0005。
