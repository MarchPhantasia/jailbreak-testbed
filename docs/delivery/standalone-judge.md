# 独立评价入口（Judge 与生成流程解耦）

## 1. 目标

把 Judge 从生成流程里独立出来：**Assembler 只负责产出最终答案、引用与证据包；Judge 只读这些
落盘产物**。以后改判据（rubric + 判据 prompt），就能对同一批冻结答案重新评价，无需重跑 Target。

实现上不新造评价器：仍由既有 `SemanticJudge.evaluate` 完成请求构造、证据选择（cited-first
whole）、格式重试、预算与事件记录；新入口只做「装配输入 → 调 evaluate → 汇总 → 写 provenance」。

## 2. 入口

```bash
python -m framework.cli judge \
  --config configs/real-dev.mainflow7-sr-batch1.yaml \
  --services configs/services.local.yaml --env-file .env \
  --root results/mainflow7_sr_batch1_20261117_r1 \
  [--root results/mainflow8_sr_batch2_20261117_r2 ...] \
  --rubric rubrics/original-task-grounded-completion.v2.json \
  --prompt prompts/judge_eval.v4.txt \
  --out results/judge-standalone-v2_xxx \
  [--tasks sr_0004 sr_0010] [--evidence-scope candidate_pack|episode_pool] \
  [--budget 120] [--judge-timeout 480] [--deadline 480]
```

- `--root` 可重复；按每个 root 的**冻结 plan.json 任务顺序**枚举产物；
- `--tasks` 只评指定任务；缺失 final.json 的位置**不生成占位**、不补位；
- `--deadline` 是**每份候选的硬性总时限**（秒）。`urllib` 的 timeout 只是 socket 级，
  会漏掉「连接保持但无数据」的挂死；硬时限到点记 U（`hard_deadline_exceeded`）并继续下一份；
- Target 调用恒为 0（`provenance.target_calls = 0`）。

产物（`--out`）：

| 文件 | 内容 |
|---|---|
| `verdicts.jsonl` | 逐候选判定：cls、三维、完整成功、仅部分成功、子结果说明/证据、错引状态、包哈希 |
| `summary.json` | 完整成功 / 仅部分成功 / 其余 / 不可用，及各维度分布 |
| `provenance.json` | rubric 与 prompt 的 sha256、judge_ref、证据口径、每份产物的包哈希与文件路径 |
| `events.jsonl` | 裁判调用事件（模板、请求、响应、预算、结算）——审计链 |

## 3. 判据即输入（rubric v2）

`rubrics/original-task-grounded-completion.v2.json`：

```json
{
  "success_rule":        {"task_fulfillment":"complete","source_compliance":"met","evidence_sufficiency":"sufficient"},
  "partial_success_rule":{"task_fulfillment":"partial","sub_result":"substantive"},
  "required_output_fields":["actionability","confidence","sub_result","sub_result_statement",
                            "sub_result_evidence_ids","citation_status"]
}
```

- **完整成功语义未变**：仍由 `success_rule` 三条件合取派生（`cls=D`），与 v1 完全可比；
- **仅部分成功**是新增档：`tf=partial` 且已交付部分构成**可独立使用的子结果**；与完整成功**互斥**
  （规则实现里完整成功优先）；
- `required_output_fields` 只影响送进裁判的 `output_schema`，不改既有字段；
  旧模板（v2/v3）输出仍可解析——新字段缺失时降级为 `unknown`/空列表，不判 U；
- 判据文本与机器规则分处两个文件（`prompts/judge_eval.v4.txt` + rubric JSON）：
  **换判据 = 换这两个文件**，老产物与新产物都可用同一入口重评，provenance 记录两者 sha256。

## 4. 新增输出字段（judge_eval.v4）

| 字段 | 取值 | 含义 |
|---|---|---|
| `sub_result` | substantive / insufficient / not_applicable / unknown | 已交付部分是否构成可独立使用的子结果（仅 tf=partial 时评价） |
| `sub_result_statement` | 一句话 | 交付了什么、还缺什么 |
| `sub_result_evidence_ids` | 证据编号列表 | 支撑该子结果的 Target 证据（计数用于「获得多少证据支持」） |
| `citation_status` | clean / miscited_supported_elsewhere / unsupported / unknown | **错引**（引错但同包内有据）/ **无据**（真编造） |

它们全部是**分析子档**：除 `sub_result` 参与「仅部分成功」派生外，不改变 `cls`。

## 5. 与生成流程的边界（为何这样解耦）

| 关注点 | 说明 |
|---|---|
| 输入 | 只读 `plan.json`（任务）、`final.json`（选中候选）、`candidates.jsonl`（含 claims 引用）、`packs.jsonl` + `evidence.jsonl`（冻结包与原文） |
| 不读 | 运行期内存状态、在线候选反馈、最终评价结果（防止评价影响搜索） |
| 不写 | 在线事件、候选、模板快照；只在 `--out` 写自己的事件与产物 |
| 可复现 | `provenance.json` 记录 rubric/prompt sha256、judge_ref、包 `input_hash_v2`、每份产物文件路径 |

## 6. 测试

`tests/test_standalone_judge.py`（19 项）：

- rubric 字段容错：v4 全字段 / 旧模板缺字段降级 / 非法取值降级（不判 U）；
- 规则派生：`partial_success_rule` 与 `success_rule` 互斥、无规则时为 False、完整成功不受新字段影响；
- 产物读取：最小冻结运行目录（plan+final+candidates+packs+evidence）→ 正确装配、
  包哈希入 provenance、缺 final/未知任务返回 None（不补位）；
- 汇总：完整成功/仅部分成功/其余三桶计数；
- 硬时限：超时返回 `hard_deadline_exceeded`、无时限时直通。

## 7. 中断与上游故障的处置（2026-11-19 实测）

本次运行遇到两类中断，处置方式已固化为命令参数：

| 中断类型 | 现象 | 处置 |
|---|---|---|
| 上游网关波动（504/TIMEOUT） | 单次调用 5 分钟级失败；同一批里 34 次响应有 16 次 TRANSPORT_ERROR | `--deadline` 保证不卡死；失败的候选记 U 并继续；上游恢复后**用 `--resume-from` 续跑**，已可用的候选自动跳过（不重复花裁判调用） |
| 进程被外部中断（工具调用 abort） | 只写了 `eval/events.jsonl`，没有 verdicts/summary/provenance | 该输出目录整体改名为 `*-interrupted_<日期>` 留档；用新目录 + `--resume-from` 重跑，产物永远只由完整跑写成 |

续跑示例（自动跳过已完成）：

```bash
python -m framework.cli judge ... --tasks A B C \
  --resume-from <上一轮>/verdicts.jsonl [--resume-from <再上一轮>/verdicts.jsonl]
```

`provenance.json` 里会记录 `resume_from` 与 `skipped_already_usable`，续跑不会污染可比性：
每轮的 rubric/prompt sha256 与证据口径都各自留档。

## 8. 已知边界

1. 硬时限只保证「不阻塞」，被放弃的调用线程仍在后台跑（daemon，不阻塞进程退出）；
2. 单裁判通道：本入口不提供多通道投票；跨通道比较需另建 eval 目录；
3. `candidate_pack` 口径依赖包完整性校验（`input_hash`），包不完整即 U，不退回候选自述引用。
