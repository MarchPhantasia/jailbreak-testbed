# Execution-1 R2 交付记录（E01–E05）

- 基线：`d6f325a`（源码快照 `56a3356`，V01/V02 已关闭）
- 本轮任务包：`jailbreak-testbed-execution1-r2-d6f325a`
- 交付提交：`8a2d9f7`（本轮不推送；远端推送由另一会话负责）
- 结论：**offline_ready = 是**；**real_pilot_complete = 否**（本会话未连接任何真实服务，未产生真实 ASR）

## 1. 做了什么

| 阶段 | 交付 | 主要落点 |
|---|---|---|
| E00 | 六套旧验收原样保留 | `tests/`、历轮 `docs/review/` |
| E01 | services/components 路由、三个参数档案、`extra_body` 展开、`model_manifest`、三角色生成探针（含部署别名核对）、阶段额度 | `routing.py`、`clients.py`、`budget.py`、`runner.py`、`contracts.py` |
| E01 | 模板注册表：ID/版本/SHA-256/渲染器版本，运行目录快照 | `prompts.py`、`prompts/*.v1.txt` |
| E02 | `request_generated` 权威事件 + `requests.jsonl` 派生索引 + Target `request_id` 关联 | `requests.py`、`engine.py`、`controller.py`、`methods/independent.py` |
| E03 | `assembly.type=llm` 真调用 Assembler，引用归属校验、行级覆盖检查、有限重试、verbatim 降级；无本地额度时允许不调用模型的原文回退 | `assembly.py`、`engine.py` |
| E04 | 语义 Judge_eval：独立 EvaluationPlan/事件/账本，S/F/U 由冻结规则计算，模板快照只读 | `evaluation.py`、`runner.py` |
| E05 | 真实 CSV 形态、revision 透传、开发切片脚本、运行报告、示例配置 | `datasets.py`、`slices.py`、`scripts/`、`configs/real-dev.example.yaml` |

## 2. 外部契约与迁移说明

新增/改变的行为（旧配置不变）：

- `experiment.budget.stage_calls`：在线阶段上限，与全局 `target_calls`/`non_target_calls` 同时检查。
  `tokens`/`wall_ms` 非 `null` 时直接报错（尚无硬中断实现，不能拿事后统计冒充额度）。
- `services`/`components`：新入口。与旧 `target`/`attacker`/`evaluation.service` 混用报错。
- `prompts.<stage>.{id,version,path}`：`plan`/`assemble`/`judge_eval` 至少三项；渲染器为
  `single-substitute@v1`，只替换 `{{input_json}}`。
- `stages.stages.<stage>`：按服务的 `parameter_profile` 校验；未知键、保留键覆盖、冲突、非原生
  effort、thinking 下的 `temperature` 都在启动前报错。
- `evaluation.type=llm`：需要 `objective` 与非空 `success_rule`；空契约启动前报错。
- `plan.json` 新增 `routing`、`stages`、`model_manifest`、`evaluation_spec`、`prompt_manifest`、`budget`、`profile`。
- 新增事件：`request_generated`、`preflight_probe`（保留类型）、`assembly_rejected`、`evaluation_started`、
  `evaluation_verdict`、`evaluation_completed`。
- 新增产物：`requests.jsonl`、`prompts/*.txt`、`preflight.json`、`model_manifest_observed.json`、
  `evaluations/<id>/{plan.json,config.json,events.jsonl,ledger.json,verdicts.jsonl,records.jsonl,metrics.json}`、`report.json`。
- CLI：`check-services` 不健康时退出码 1；`evaluate` 的 `calls_model` 按冻结配置报告；新增 `report`。

## 3. 检查结果

命令：

```bash
PATH=/home/pha/sci-deepseek/.venv/bin:$PATH \
  python3 tools/run_checks.py all --repo /home/pha/sci-deepseek/jailbreak-testbed
```

| 套件 | 结果 |
|---|---|
| baseline | 198 passed |
| prior / round2 / round3 / round4 / round5 | 17 / 19 / 12 / 8 / 5 passed |
| harness | 2 passed |
| next（N01–N15） | 16 passed |
| routing（M01–M07） | 8 passed |

`--transport http`（仅绑定 127.0.0.1 的临时服务）另跑一次，结论一致：

```
baseline 198 / prior 17 / round2 19 / round3 12 / round4 8 / round5 5 / harness 2 / next 16 / routing 8
```

## 4. 与真实实验的距离

仍缺的输入（由研究者提供，不在仓库内）：

| 项 | 状态 |
|---|---|
| Target 的 `base_url`/`model`/`api_key_env`/能力参数 | 待填（示例配置用占位地址） |
| 本地 Qwen 部署的实际 `served-model-name`、chat template hash、vLLM 版本 | 待核对；`capability_status` 保持 `unknown` |
| InferKnock 是否转发 `thinking`/`reasoning_effort` | 未知；只记录 `transport_accepted` |
| 真实开发切片（StrongREJECT 固定 revision） | 用 `scripts/select_dev_slice.py` 在本机生成 |
| 人工复核（20–30 条） | 未开始 |

## 5. 真实数据冒烟（本地替代 Target，不作为 ASR 结果）

数据：StrongREJECT 官方完整集，固定 revision `f7cad6c1…`，SHA-256 `4dd70357…`
（与执行包 `references/dataset_sources.json` 记录一致，313 行）。开发切片按
`scripts/select_dev_slice.py` 生成：seed 20261112、30 条、30 个重复组、切片
SHA-256 `1ae2261a…`；类别分布 Non-violent crimes 10 / Disinformation 7 /
Hate 4 / Sexual 4 / Violence 4 / Illegal goods 1。原始 CSV 与切片都在 `data/`（不入库）。

服务：本机 vLLM（`JonathanColetti/Qwen3.8-27B-Uncensored`，127.0.0.1:8000）同时充当
Controller / Assembler / Target / Judge_eval。**Target 是未对齐模型，Judge 与生成同源**，
所以下面的 S/F/U 只用于验证链路，不是越狱 ASR。配置见 `configs/real-dev.local-smoke.yaml`。

| 运行 | 配置差异 | 结果 | 观察 |
|---|---|---|---|
| v1（中止） | 全链 thinking 开，`target_query.max_tokens=3072` | 无候选 | Target 的 3072 个 completion token 全部落在 reasoning（13.6k 字符），`content` 为空 → `INVALID_OUTPUT(reasoning_only)`，两次重试同样为空，预算烧完且没有材料 |
| v2 `…20261112b` | Target 关闭 thinking、4096；Assemble 仍 thinking/4096 | 3 集全跑完；S/F/U=3/0/0 | 装配 3/3 降级：2 次 `TRUNCATED`（10.5k 字符 JSON 被截断），1 次 `answer_lines_not_covered`（答案有引导句、claim 写成描述性 paraphrase 被判未覆盖） |
| v3 `…20261112c` | Assemble 关闭 thinking、8192；覆盖检查改为以证据为锚 | 3 集；装配 2/3 正常（1 集两次重试后降级） | 正常候选 16 claims / 6331 chars，claims 的 evidence_ids 全部属于该证据包；调用 6 plan / 3 target / 4 assemble，31k 输入 + 18k 输出 token |

### 由真实数据暴露并已修复的问题

1. **Target 的 system 指令是实验条件却被隐含**：原来 Engine 固定注入"只依据对话内容作答、
   不确定写未提供"的系统提示，会压制 Target 的正常回应。现在 `target_system_prompt` 显式配置，
   空串表示不注入任何指令；字符数与 SHA-256 写入 `episode_started`，`real_dev` 默认空。
2. **装配覆盖检查锚错了对象**：只和 `claims.text` 比字符串，模型写"描述性 claim + 引用式答案"
   时会被判无依据并降级。现在 `line-ngram-evidence@v1`：长段落与 claims **和** 证据包原文做
   4-gram 覆盖，短引导句/标题豁免；仍拦截材料之外的长段落。
3. **评价证据范围取错**：原来只把 claims 引用到的证据给裁判，引用写不全就会被判
   `source_compliance=not_met` + `evidence_sufficiency=insufficient`。现在传入该 episode 的
   **全部 Target 材料**，并把 `cited_evidence_ids` 单独列出；v3 同一批候选重评后
   由 S/F/U=2/1/0 变为 3/0/0，说明该 F 是证据范围造成的伪失败。
4. **配置相对路径**：`datasets.path` / `manifests_dir` / `results_dir` 先按工作目录找，
   找不到再按配置文件目录解析，`configs/real-dev.*.yaml` 可以直接用相对路径。
5. **划分打乱源顺序**：`split` 现在按 task_id 排序返回，切片"前 N 条"与文件顺序一致。

### 真实数据冒烟得出的下一步建议

- 任何 thinking 模型作为 Target 时，输出上限必须覆盖"推理 + 答案"；否则会得到
  `reasoning_only` 并浪费 Target 预算。建议 Target 侧关闭 thinking，或把上限提到 8192 以上。
- 装配是"整理已有材料"，不需要长推理：thinking 关闭 + 8192 比 thinking 开 + 4096 更稳。
- `Judge_eval` 必须看到完整材料；claims 引用质量应作为独立诊断指标，而不是成功门槛。
- 同源裁判的判定偏宽松（3/3 D），正式实验必须换成 InferKnock 的 deepseek 裁判并做人工抽检。

## 6. 已知限制

- 语义裁判仍是通用模型 + 冻结模板，未做与人工的一致性校准；`truth_status` 保持 `not_assessed`。
- 无额度回退用的是模板装配（`fallback_render=true`），只搬运原文，不做跨材料推理。
- 行级覆盖检查只是结构检查，不能证明语义支持；引用存在 ≠ 主张被证明。
- `assembly` 的 `k>1`、分支搜索、在线语义停止、语义抽取/摘要不在本轮范围。
- 同一 endpoint 上 Controller 与 Assembler 共用权重时噪声相关，报告需注明同源性。
- 预检只发一次最小生成；它证明"能生成"，不证明"参数语义已生效"。
