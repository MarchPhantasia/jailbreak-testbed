# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号对应**规格版本**，不对应代码版本。

## [Unreleased]

（M0 之前的规格修订都进这里）

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
