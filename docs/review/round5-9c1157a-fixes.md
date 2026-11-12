# 第五轮审查（9c1157a）修复记录 V01–V02

- 修复提交：见 `git log --oneline` 中 `fix: 按第五轮审查 V01–V02 …`
- 验证：仓库 152 项、历轮 17/19/12/8、第五轮 5 项全过（`tools/run_checks.py all --repo .`）
- 口径：仍以"同 Target 预算下的最终越狱 ASR"为主终点；本轮不引入请求策略与语义裁判。

| ID | 状态 | 修改 | 验证 | 契约变化 | 剩余限制 |
|---|---|---|---|---|---|
| V01 | 已修复 | 新增 `Engine.materials_fingerprint()`（与 pack `input_hash` 同序列化）；online 收尾时若活跃材料指纹变化且本地额度充足，执行最终装配并保留旧候选 | max_steps / target_budget 两种边界均把新证据带入答案；无变化不重复装配；旧候选不被改写 | 收尾可能产生第二次 `assembled`；无额度时记 `stale_candidate_no_local_budget` | 只在 online 策略下触发；after_assembly 仍是单次装配基线 |
| V02 | 已修复 | `OpenAICompatGateway._parse_completion()`：先校验结构，再映射 `message.refusal`、`finish_reason=content_filter`、缺 choices、混合内容、截断 | 5 种响应形态各自的枚举与原因标签；每次调用仅结算一次 | `CallResult.error` 新增 `provider_refusal` / `content_filter` / `mixed_refusal_with_content` / `empty_content` / `malformed_response:*`；`raw` 保留 `refusal`、`filter_reason` | 自然语言拒答的语义识别仍需语义裁判，不靠关键词 |

## 说明

- V02 未新增 `FILTERED` 枚举：按验收口径，供应商显式拒答与内容过滤都记 `REFUSED`，用 `error`/`raw` 区分原因；若后续需要独立状态，可在此基础上加。
- V01 的"新证据"判定用材料指纹而非事件序号：拒答或空回复不会触发无意义重装配。
