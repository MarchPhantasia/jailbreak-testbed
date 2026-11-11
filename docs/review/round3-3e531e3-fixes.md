# 第三轮审查（3e531e3）修复记录 T01–T08

- 修复提交：见 `git log --oneline` 中 `fix: 按第三轮审查 T01–T08 …`
- 验证：仓库 122 项通过；前两轮 36 项、第三轮 12 项验收全过（`tools/run_checks.py all --repo .`）
- 口径：只修工程与评价契约；请求策略 / 提示词管理（METHOD_STATUS）留待下一轮，不混入本轮证据。

| ID | 状态 | 修改 | 验证 | 契约变化 | 剩余限制 |
|---|---|---|---|---|---|
| T01 | 已修复 | 评价只解析一次 final；损坏/非对象/缺字段 → `unresolved`，不再回写；`_authoritative_terminate` 读唯一 terminate 并核对候选 ID | 截断 JSON、非对象、候选冲突三项回归 | 评价不再覆盖损坏源文件；冲突记 `terminate_candidate_conflict` | 权威事件只认单一 terminate；多事件视为不可用 |
| T02 | 已修复 | `LLMController` 新增 `last_failure`：解析失败 → `INVALID_OUTPUT`；编排层优先读 `last_failure`，拒答仍走 `component_refusal` | 非法 JSON 进入格式重试；REFUSED 走降级装配 | 控制器暴露 `last_status`（传输）与 `last_failure`（解析）两层 | 仅一次格式重试（按冻结配置） |
| T03 | 已修复 | 新增 `active_materials()`；缺项、评分、选择、装配共用；原文块按 key 解析，不再"无 key 即满足所有要素" | 原文单要素、归档不关闭当前缺项、评分同视图 | 归档 `summary_archive` 只供复核 | 原文块解析只支持 `key：value` 形态 |
| T04 | 已修复 | 精确任务身份 = 请求 + 上下文 + 冻结判据；`duplicate_group` 与任务身份分开；指标新增 `n_tasks`/`asr_task_*` | 区间=[0.5,0.5]、不同判据 3 个任务、U 扩宽区间 | `metrics` 新增任务级字段；episode 级字段保留 | 任务权重按等权；加权方案未实现 |
| T05 | 已修复 | `element_satisfied` 把 accept 绑定到要素 key 的值段，再做否定/过期/未确认检查 | 2 个反例 + 正向 + 同值不同要素 | 判分不再跨要素匹配 | 只支持受控文本格式，语义裁判仍未接入 |
| T06 | 已修复 | 装配前深拷贝并落盘 `packs.jsonl`（含 `input_hash`），候选关联写 `pack_links.jsonl`；新增 `pack_frozen` 事件 | 装配器进入时包已存在；冻结副本不被后续状态改写 | 新增事件 `pack_frozen`；`packs` 行不再带候选列表 | 装配异常时的输入审计已具备，但回放工具未提供 |
| T07 | 已修复 | 计划写 `split_seed` / `experiment_seed`；`prepare_data` 用 `plan.split_seed` | 两 seed 不同用例 | `split_manifest` 增加 `experiment_seed` | 清单一致性校验仍是单次读取 |
| T08 | 已修复 | `run_episode` 解析 `state.window_chars` 并校验：仅 raw 后端可用、必须正整数、未知键报错 | 窗口生效、非 raw 拒绝、未知键拒绝 | `method.state` 只接受 `backend/max_chars/window_chars` | 窗口按每条回复截断，非累计视图 |

## 行为变化（对既有实验的影响）

- 装配默认终止（`assembled`）：原先装配后会继续让控制器决策，现在装配即冻结答案；如需继续询问，后续加 `method.assembly.continue_after_assemble`。
- 主终点从 episode 级均值改为**任务级等权**；旧结果文件仍保留 episode 级字段，但 `asr_point` 语义已变，跨版本比较需注明。

## 未做的事

- METHOD_STATUS 列出的请求策略、提示词模板管理、阶段预算落地。
- 归因三层、双裁判、真实 endpoint 联调。
