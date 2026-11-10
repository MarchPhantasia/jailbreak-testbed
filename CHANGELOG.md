# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号对应**规格版本**，不对应代码版本。

## [Unreleased]

### 第二轮审查修复（S01–S14）

按审查包 `review2-4a5c50d` 的 14 组问题修复，16 个文件涉及；新增 `tests/test_round2_invariants.py`（8 项）作仓库内回归保护。

| 问题 | 结论 | 主要落点 |
|---|---|---|
| S01 | **阻塞**：凭据缺 `workflow` 权限，CI 仍暂存 `docs/ci/ci.yml`；仓库断言恢复严格并标 `xfail(strict=True)`，授权后移回即自动转绿 | `docs/ci/ci.yml`、`tests/test_review_regressions.py` |
| S02 | 原文后端向控制器与装配提供完整原文；截断改为显式 `window_chars`（默认不截断） | `state.py` |
| S03 | 摘要历史快照归档为 `summary_archive`，活跃材料只保留当前摘要 | `state.py`、`assembly.py` |
| S04 | 行/键/值联合绑定来源；合并摘要累积全部贡献 evidence；主张继承 support/fidelity，不再一律 ENTAILED | `state.py`、`assembly.py` |
| S05 | EvidencePack 在装配前冻结落盘（含 materials/input_hash/candidates），一个 pack_id 只定义一次 | `engine.py`、`assembly.py` |
| S06 | 逐要素满足结果统一供覆盖率与 critical 使用；否定出现不算满足 | `judge.py` |
| S07 | 控制器保留真实状态（REFUSED ≠ 格式错误）；本地额度耗尽进入正常收尾 | `controller.py`、`methods/independent.py` |
| S08 | `evaluate` 读取被评价目录自己的 `plan.json` | `runner.py` |
| S09 | `split_seed` 与 `episode_seed` 分离；episode 身份由采样种子决定 | `runner.py` |
| S10 | `final.json` 先校验必需字段与权威 `terminate`，缺字段记 U 而非 F | `runner.py` |
| S11 | `usage_coverage` 要求输入与输出都已知，另报 in/out 分项 | `events.py` |
| S12 | 缓存命中保留原始 call/response 关联；证据按 CallResult 关联解析 | `clients.py`、`engine.py` |
| S13 | 指标按重复组做任务级加权；`asr_point` 为主终点，`asr_episode` 与区间同时保留 | `judge.py`、`runner.py` |
| S14 | UCT 完成扩展后沿声明路径回传 reward；选择动作不再增加零样本访问 | `engine.py` |

结果：仓库测试 102 项（含 1 项 S01 `xfail`）；上轮原始验收 17/17；本轮验收 18/19（S01 为凭据阻塞，测试本身断言有效目录）。

### 版本管理侧改动（本轮）

- `.github/workflows/ci.yml` 暂存为 `docs/ci/ci.yml`：当前 token 缺 `workflow` 权限，推送会被远端拒绝。内容为 install + pytest + precheck，补齐权限后一条 `git mv` 即生效。
- 删除早先的 `docs/ci/docs-check.yml`（其职责已被 `docs/ci/ci.yml` 覆盖）。

### 已知问题（版本管理侧记录）

- ~~`src/jailbreak_testbed/` 与 `src/framework/` 概念重复，待决定去留~~ → **已决定：保留并标注为参考模型**（2026-11-11）。
  它提供 framework 尚未覆盖的 `recompute` 差分与 `rollback` 语义；已加 `src/jailbreak_testbed/README.md` 说明边界，
  计划在把 recompute 迁入 analysis 层后删除。
- `.gitignore` 中的 `../.venv/` 是无效模式（git 忽略含 `..` 的规则），已移除；虚拟环境应放在仓库内并由 `.venv/` 覆盖。

## [code-v0.1.1] — 2026-11-11

按审查包（`a9b45c9`，R01–R13）修复工程与评价契约；测试 60 → 93 项，包内 17 项验收全过。

### 修复

- **R01** 服务配置生效：按 `target.type` 构造客户端，未支持类型直接报错；`check-services` 做真实探测（不可达即不健康）。
- **R02** 冻结判据：每题 rubric 在计划阶段落盘，评价只读冻结对象；`accept` 值不再被元素名覆盖。
- **R03** 分母与运行身份：先写 `plan.json` 冻结 episode 清单；同 ID 重跑抛 `FileExistsError`；缺 final / 缺候选记未决，正常终止无答案仍计失败。
- **R04** 重复组先于划分建立，组内不跨 split。
- **R05** 原文 / 摘要 / F-O-X 三后端都向装配提供材料；摘要受长度上限约束。
- **R06** Evidence 完整记录落盘（`evidence.jsonl`），主张 → 证据 → 原始事件 → span → checksum 可解析。
- **R07** `state_write` 保存所属节点与完整快照；`restore_state(..., node_id=)` 精确恢复，不合并兄弟、不调模型。
- **R08** 恢复决定真正执行：`retry_same` 重发原查询且不推进控制器；`fallback_assemble` 真正装配。
- **R09** Target 额度耗尽只拒绝新请求，收尾仍可用剩余本地额度装配并冻结答案。
- **R10** 缺项按要素关联计算；"未确认/未提供/旧版"不算回答；同值不同要素不合并。
- **R11** 分支入口可用（`search.branch_width`），UCT 访问/回传接通；未知策略显式报错。
- **R12** usage 缺失记 `None` 并单列 `*_unknown_calls` / `usage_coverage`，不写成实测 0。
- **R13** 生效 CI（`.github/workflows/ci.yml` 跑 pytest），`scripts/precheck.sh` 增加测试步骤。

### 契约变化

- 新增 `plan.json`（episode 清单 + 冻结 rubric）与 `rubrics.json`。
- 新增 `episodes/<id>/evidence.jsonl` 与 `packs.jsonl`。
- `state_write.payload.snapshot` 保存完整状态；`restore_state` 增加 `node_id` 参数。
- `budget_settled.cost.tokens_in/tokens_out` 可为 `null`（未知）；ledger 增加 `*_unknown_calls`、`usage_coverage`。
- `search.branch_width` 控制同父分支宽度（1 = 线性）。

### 已知限制

- R11 的分支策略由研究者声明；未内置自动分叉阈值。
- 归因三层、双裁判、人工切片仍未实现（按手册属后续阶段）。

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
