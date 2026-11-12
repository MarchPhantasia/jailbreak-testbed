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

## 5. 已知限制

- 语义裁判仍是通用模型 + 冻结模板，未做与人工的一致性校准；`truth_status` 保持 `not_assessed`。
- 无额度回退用的是模板装配（`fallback_render=true`），只搬运原文，不做跨材料推理。
- 行级覆盖检查只是结构检查，不能证明语义支持；引用存在 ≠ 主张被证明。
- `assembly` 的 `k>1`、分支搜索、在线语义停止、语义抽取/摘要不在本轮范围。
- 同一 endpoint 上 Controller 与 Assembler 共用权重时噪声相关，报告需注明同源性。
- 预检只发一次最小生成；它证明"能生成"，不证明"参数语义已生效"。
