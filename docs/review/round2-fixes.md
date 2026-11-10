# 第二轮审查修复记录（S01–S14）

审查包：`review2-4a5c50d`（审查版本 `4a5c50d`）。以下按问题 ID 记录最终行为、落点与剩余限制。

## 输入与证据契约（S02 / S03 / S04 / S05 / S12）

| ID | 最终行为 | 落点 | 剩余限制 |
|---|---|---|---|
| S02 | 原文后端把**完整原文**同时提供给控制器渲染与装配；截断改为显式 `window_chars`，默认 `None`＝不截断，一旦启用会在 state update 的 `notes` 里留痕 | `state.py::RawHistoryBackend` | 窗口若启用，属研究协议变量，需在报告中声明 |
| S03 | 每次更新的旧摘要转为 `summary_archive`（归档、可复核），装配只消费当前摘要；活跃材料字符数受 `max_chars` 约束 | `state.py::SummaryBackend`、`assembly.py::ARCHIVED_KINDS` | 归档仍占状态体积，未做归档淘汰 |
| S04 | 证据按「整行 → 键+值 → 值」三级绑定，同值不同要素不再串源；合并摘要的 `evidence_ids` 为全部贡献轮次并集；`Claim.support_status/fidelity` 继承材料，只有逐字命中才记 `ENTAILED` | `state.py::_resolve_evidence`、`assembly.py::render` | support 仍由规则判定，未接语义核验 |
| S05 | 装配前冻结 `EvidencePack` 并落盘（materials/gaps/view/input_hash/candidates），一个 `pack_id` 只定义一次；候选一律从该包渲染，可重放 | `engine.py::_assemble`、`assembly.py::assemble_from_pack` | 包内容随状态增长，未做去重压缩 |
| S12 | `CallResult` 携带 `response_event_id/source_call_id`；缓存命中记录原始关联并复用原事件；证据按该关联解析，不再取「最后一个 response」 | `clients.py`、`engine.py::_ask_target` | 缓存默认关闭，正式实验不受影响 |

## 评价与计划（S06 / S08 / S09 / S10 / S13）

| ID | 最终行为 | 落点 |
|---|---|---|
| S06 | `element_satisfied()` 统一供覆盖率与 `critical_ok`；否定词（不是/并非/未/not…）出现在取值前 8 字符内即判不满足 | `judge.py` |
| S08 | `evaluate_experiment(cfg, root=...)` 从 `root/plan.json` 读取冻结计划与 rubric，不再使用配置指向的另一份 | `runner.py::load_plan_from_root` |
| S09 | `split_seed` 只用于数据划分；`episode_seed` 取 `experiment.seed`，episode ID 由该种子生成 | `runner.py::build_plan` |
| S10 | `REQUIRED_FINAL_FIELDS` 校验 + 权威 `terminate` 事件确认；缺字段/非法 JSON/无 terminate 均记 U | `runner.py::evaluate_experiment` |
| S13 | 指标按 `duplicate_group_id` 做任务级加权：`asr_point` 为主终点；`asr_episode`、`asr_range`、`n_tasks`、`n_merged_duplicates` 同时报告 | `judge.py::compute_metrics` |

## 组件恢复与资源（S07 / S11）

| ID | 最终行为 | 落点 |
|---|---|---|
| S07 | 控制器记录 `last_status`；REFUSED 走 `component_refused`（记 `component_refusal` 事件），不再被改写成格式错误；`BudgetExhausted` 转为 `terminate("local_budget_exhausted")` | `controller.py`、`methods/independent.py` |
| S11 | `usage_coverage` 要求输入与输出均已知；另给 `usage_coverage_in/out` 与 `usage_unknown_calls` | `events.py::ledger` |

## 搜索与交付（S14 / S01）

| ID | 最终行为 | 落点 |
|---|---|---|
| S14 | 一次完成的扩展按路径向祖先回传 visit 与 reward；选择动作不再记零样本访问，均值不被稀释 | `engine.py::_ask_target`、`select_node` |
| S01 | **未完成，记录为阻塞项**：CI 定义保留在 `docs/ci/ci.yml`（内容已是 install + pytest + precheck）。推送凭据缺 `workflow` 权限，无法写入 `.github/workflows/`。仓库断言恢复为严格形态并标记 `xfail(strict=True)`，理由写明"授权后移回即自动转绿"——不让断言冒充完成 | `docs/ci/ci.yml`、`tests/test_review_regressions.py` |

## 验收

| 套件 | 结果 |
|---|---|
| 仓库测试 | 101 passed |
| 上轮原始验收（17） | 17 passed |
| 本轮验收（19） | 19 passed |

## 未做与后续

- S07 的降级路径仍由 `recovery` 决定；`fallback_assemble` 自身的额度约束只做记账，不额外重试。
- S04 的 support 仍是规则判定；语义蕴含与人工切片属后续工作。
- 归因三层（节点/事实/前缀）与 `recompute-state` 未在本轮推进，按 `DECISIONS.md` 的顺序留待干预协议明确后处理。
