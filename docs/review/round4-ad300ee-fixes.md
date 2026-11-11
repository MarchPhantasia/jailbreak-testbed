# 第四轮审查（ad300ee）修复记录 U01–U04

- 修复提交：见 `git log --oneline` 中 `fix: 按第四轮审查 U01–U04 …`
- 验证：仓库 141 项、历轮验收 17/19/12、第四轮 8 项全过（`tools/run_checks.py all --repo .`）
- 口径：主终点保持"同 Target 预算下的最终越狱 ASR"；U 不被隐式当失败；本轮不引入请求策略或提示词管理。

| ID | 状态 | 修改 | 验证 | 契约变化 | 剩余限制 |
|---|---|---|---|---|---|
| U01 | 已修复 | `compute_metrics` 改为"任务内先求比例、任务间等权"：`asr_point = mean_i(S_i/n_i)`，`asr_task_range = [mean(S_i/n_i), mean((S_i+U_i)/n_i)]`，`task_eval_coverage = mean((n_i-U_i)/n_i)`；`any_success_rate` 单列为描述性指标 | S/F、S/U、全 U、重复数不等、coverage 五项 | `asr_point` 语义再校正；新增 `any_success_rate`/`asr_task_lower`/`asr_task_upper` | 任务权重固定等权；预声明权重方案未实现 |
| U02 | 已修复 | 评价前统一核对：final 身份（episode_id/task_id）、terminate 必须唯一、候选 ID 双向一致；候选文件逐行容错；每个 episode 整体 try/except 隔离 | 双 terminate、terminate 置空、身份错配、候选损坏、terminate 缺失 | 新增诊断串 `terminate_ambiguous` / `terminate_candidate_conflict` / `identity_mismatch` / `artifact_error:*` | 恢复式重建（从事件重建 final）未实现，当前一律记 U |
| U03 | 已修复 | `LLMController` 校验动作 schema：`ASK_TARGET.query` 必须非空字符串；编排层对空/非字符串查询预校验；Engine 拒绝的 `INVALID_OUTPUT` 也进入同一恢复策略 | 空串、null、缺字段三种；scripted 空查询；格式重试上限 | 恢复计数键区分 `controller` / `engine_reject` | 重采样（rephrase）策略未实现，只做有界重试 |
| U04 | 已修复 | `ElementController` 改用 `compute_open`（活跃材料视图），并把"已问过"与"已满足"分开 | 归档不跳过未询问要素 | 控制器不再直接扫描 `state.materials` | 其它自定义控制器需自行遵守同一视图 |

## 停止策略（DECISIONS.md）

新增 `method.stopping.policy`，取值 `after_assembly`（默认）与 `online`；未知值直接报错。策略写入 `episode_started.payload.stopping_policy`，便于事后区分实验口径。

| 策略 | 行为 |
|---|---|
| `after_assembly` | 首次成功装配并选中候选后结束（`assembled`）。作为单次装配基线 |
| `online` | 装配后不立即结束；由在线判据 / Controller STOP / 预算决定。材料与调用都没变的重复装配判为无进展并停止（`no_progress_assembly`） |

观察到的行为差异（与审查包一致）：在线裁判 `continue`、Target 仍有余额时，`online` 会继续取证；`after_assembly` 立即冻结。两者是策略变量，不构成 ASR 高低的证据。

## 未做的事

- 请求策略、提示词模板管理、阶段预算（NEXT_METHOD_CONTRACTS）。
- 归因三层、双裁判、真实 endpoint 联调与正式 ASR 实验。
