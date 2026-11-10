# 审查包 a9b45c9 修复记录（R01–R13）

- 修复提交：`415aede`（实现）、`2f8b605`（参考模型标注）
- 验证命令：`PYTHONPATH=src .venv/bin/python -m pytest -q`（93 passed）；包内验收 `JBT_REPO=<repo> pytest acceptance`（17 passed）
- 口径：只修工程与评价契约；不改研究目标、不实现 CKA、不增加模型部署。

| ID | 状态 | 修改 | 验证 | 契约变化 | 剩余限制 |
|---|---|---|---|---|---|
| R01 | 已修复 | `methods/independent.py` 新增 `build_target_gateway` / `build_local_gateway`，按 `target.type` 构造；未支持类型 `ValueError`；`runner.check_services` 改真实探测（`clients.probe_openai`） | `test_R01_configured_endpoint_is_used`、`test_R01_unreachable_endpoint_is_not_healthy`、`test_R01_unsupported_type_is_rejected` | 事件 `request_sent.payload.model` 记录实际模型；preflight 记录 `probed` | 未与真实 endpoint 联调（由研究者提供后单独记录） |
| R02 | 已修复 | `runner.resolve_rubric` 在计划阶段冻结；`judge.Rubric.to_dict/from_frozen`；评价只读 `plan.json` 的 rubric，不再用元素名重建 accept | `test_R02_frozen_accept_values_are_preserved` 等 3 项 | 新增 `rubrics.json` 与 `episode_started.payload.rubric` | 判据仍是规则判分（无语义裁判） |
| R03 | 已修复 | 先写 `plan.json` 冻结清单；同 ID 重跑抛 `FileExistsError`；评价按清单对账；缺 final / 缺候选 → `unresolved` | 4 项回归（缺 final、重跑、缺候选、正常无答案） | 新增 `plan.json`；`final.json.outcome/basis` 由评价回写 | 中断后 resumse 未实现（当前策略是拒绝重跑） |
| R04 | 已修复 | `datasets.load_split` 先 `assign_groups` 再 `split_tasks` | `test_R04_duplicates_stay_in_one_split`、`test_R04_group_ids_are_stable_under_reordering` | 无 | 近义聚类与跨源合并规则未实现（按手册后续做） |
| R05 | 已修复 | `state` 三后端统一产出 `materials`；`assembly` 消费 materials；摘要受 `max_chars` 限制 | 参数化 3 后端 + 摘要长度用例 | `AgentState.materials` | 原文组材料截断到 2000 字符（防爆上下文） |
| R06 | 已修复 | 引擎按行落 `evidence.jsonl`（id/source_event/call/node/span/checksum）；装配落 `packs.jsonl` | `test_R06_evidence_resolves_to_original_span`、同文本不同事件可区分 | 新增两个文件 | 证据包与候选的跨进程加载仅覆盖 candidates.jsonl |
| R07 | 已修复 | `state_write.payload.snapshot` + `node`/`parent_state_seq`；`restore_state(..., node_id=)` 按快照恢复 | 3 项（等价、不合并兄弟、不调模型） | 恢复 API 增加 `node_id` | `recompute`（新抽取器重算）尚未接入 framework |
| R08 | 已修复 | 编排层执行 `retry_same`（重发原动作、不推进控制器）与 `fallback_assemble`（真正装配）；控制器失败不再压成 STOP | 2 项 | `StepResult.retryable`；恢复事件保留 attempt | `rephrase_retry` 未实现（策略未冻结） |
| R09 | 已修复 | 引擎额度耗尽只拒绝新请求并置 `target_exhausted`；收尾用剩余本地额度装配后唯一 terminate | `test_R09_target_cap_allows_final_assembly` | `BudgetExhausted` 不再等价于终止 | 本地额度也耗尽时按 `max_steps` 结束 |
| R10 | 已修复 | 要素关联：`element_evidence` 按 key + 值判定；`未确认/未提供/旧版` 不算回答；同值不同要素不合并 | 3 项 | 要素结构 `{key, accept, critical}`；字符串声明不生成 accept | 否定/版本的条件语义仍靠文本标记，非结构化解析 |
| R11 | 已修复 | 新增 `search.branch_width`；父节点在未达宽度前留在前沿；UCT 记录 visits/reward；未知策略报错 | `test_R11_branch_entry_produces_siblings` 等 3 项 | 新配置项 `search.branch_width` | 分支策略由研究者声明，未内置自动阈值 |
| R12 | 已修复 | `usage_field` 缺失返回 `None`；ledger 增加 `tokens_*_unknown_calls`、`usage_coverage` | `test_R12_missing_usage_is_not_zero`、`test_R12_reported_zero_stays_zero` | `budget_settled.cost.*` 可为 `null` | reasoning token 与 completion 的包含关系未单独建模 |
| R13 | 已修复 | 新增 `.github/workflows/ci.yml`（pytest + precheck）；`scripts/precheck.sh` 增加测试步骤 | `test_R13_active_ci_runs_tests`；本地 precheck 已跑 pytest | precheck 由 5 步变 6 步 | GitHub 上的真实执行需推送后确认 |

## 未纳入本轮（按交接说明保留）

双裁判、人工切片、归因三层（节点/事实/前缀）、真实 endpoint 联调、统计与语义校准。
