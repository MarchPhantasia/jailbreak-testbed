# ADR-0007：按组件路由模型与模型特定参数档案

- 状态：已采纳（E01-R2）
- 日期：2026-11-12
- 相关：`src/framework/routing.py`、`src/framework/prompts.py`、`docs/review/execution1-r2-fixes.md`

## 背景

R2 之前，本地角色只有一个隐式网关：`attacker` 同时充当 Controller 与 Assembler，Judge_eval 固定为规则实现，
`clients.py` 只放行 `temperature/top_p/max_tokens/stop/seed` 五个字段。后果有三个：

1. 不同角色无法配置到不同服务或不同模型，换模型只能改代码；
2. 推理引擎的扩展参数（`top_k`、`chat_template_kwargs`、`thinking`、`reasoning_effort`）被静默丢弃，
   记录里看不到"配了但没生效"；
3. 两个模型的原生思考档位（Qwen 的 `low/medium/xhigh`、DeepSeek 的 `low/high/max`）语义不同，
   只按名字相近映射会制造不可比的实验条件。

## 决策

1. **配置层**：`services`（服务 ID → 客户端配置）+ `components`（角色 → enabled/service_ref/stage_id）。
   组件到服务、服务到模型是纯配置关系，代码里不按角色名写死。旧 `target/attacker/evaluation.service`
   继续兼容，归一化为 target、controller+assembler、judge_eval；两套入口同时出现直接报错。
2. **参数档案**：每个服务声明 `parameter_profile`（`openai_chat` / `qwen38_vllm` / `deepseek_thinking_proxy`）。
   档案先校验再序列化：未知字段、保留键覆盖、同名冲突、非原生 effort、thinking 下的无效采样参数
   都在发出请求前报错，不再静默过滤。
3. **`extra_body` 是应用容器**：展开合并到 HTTP 顶层，不把整个对象当作供应商字段发送。
   Qwen 的 `reasoning_effort` 同时写入顶层与 `chat_template_kwargs`（值必须一致）；
   DeepSeek 使用顶层 `thinking` 与 `reasoning_effort`，不发送 `temperature/top_p`。
4. **冻结与审计**：`plan.json` 记录 `model_manifest`（角色、阶段、服务、模型、净参数、能力状态）、
   `stages`（净发送参数）、`prompt_manifest`（模板 ID/版本/SHA-256/快照路径）与 `evaluation_spec`。
   预检结果另存 `preflight.json` / `model_manifest_observed.json`，`transport_accepted` 与
   `backend_confirmed` 分开记录。
5. **凭证**：只以环境变量名引用；`auth_required=true` 且变量缺失时在构造客户端时失败。
   凭证值不进入计划、事件、异常正文或缓存键。

## 后果

- 角色可独立配置模型与预算身份；同一 endpoint 上的 Controller/Assembler 仍各自取票、各自记账。
- 参数不受支持的配置会**在启动前**失败（含 preflight 之前），不会产生"看起来跑通了"的假结果。
- 能力状态默认 `unknown`：HTTP 200 只证明请求被接受，不证明 effort、采样或输出上限生效。
- 旧合成配置保持原行为（`openai_chat` + 五字段），历史验收不受影响。
- 本轮不实现禁用的 extractor/summarizer/judge_online 语义调用；`raw_history` 下启用它们会报错。
