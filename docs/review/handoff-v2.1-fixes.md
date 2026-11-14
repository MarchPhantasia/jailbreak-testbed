# 审查包 handoff-v2.1 修复记录（W01 / W02 / W03）

- 审查基线：`1bdc47bb39cd771f642a4b4991c0e5fc684605a6`
- 实际开发起点：同一提交；工作区无未提交改动，本轮在其上修改
- 状态：**offline_ready**（离线验收全绿；未做真实 InferKnock 调用，未测模型能力，不报告 ASR）
- 本轮不改变研究主终点：最终选中答案是否实质完成冻结的原始有害请求，仍由冻结判据与 Judge_eval 决定。
  装配策略与上下文预算的修正只让"能不能被正确解释"成立，不代表越狱成功率提升。

## 0. 总览

| # | 问题 | 改了什么 | 关键文件 |
|---|---|---|---|
| W01 | 离线裁判的完整请求没有窗口检查 | 冻结 `context_window` 送到实际网关；证据预算先扣固定输入开销；整份渲染请求复检；修复重试复检；超窗不发、不扣费、不无效重试 | `evaluation.py`、`runner._build_eval_judge` |
| W02 | 缺失冻结证据后仍可计分 | `candidate_pack` 完整性状态贯通 `scope → evidence_plan → verdict → records`；不可核验即 `unscored:evidence_input_incomplete`；不再用候选自述引用恢复来源 | `runner.candidate_evidence_scope`、`runner.evaluate_experiment`、`judge.decide_outcome` |
| W03 | 字面覆盖被当成装配准入 | `method.assembly.lexical_policy = diagnostic_only \| hard_gate`；默认只诊断；硬门槛显式保留并写入候选与计划 | `assembly.py`、`runner.build_run_context`、`runner._freeze_routing` |

## 1. W01 完整裁判请求的上下文预算

改动：

1. `runner._build_eval_judge()` 把冻结 `evaluation_spec.service.context_window` 传给
   `OpenAICompatGateway(context_window=...)`。此前只传给 `SemanticJudge`，实际发送网关是 `None`。
   窗口未知（`None`）保持未知语义，不填默认 32k。
2. `SemanticJudge.prepare_request()` 分三步：
   - 先渲染"无证据"的完整固定输入（模板、任务、答案、claims、rubric、schema、`evidence_plan` 包装），
     得到 `fixed_input_tokens_estimate`；
   - 证据 token 预算 = `context_window − max_output_tokens − reserve(512) − fixed 开销`，仍保持
     "被引用证据优先且完整、其余整条装/整条省、不切割"；
   - 对渲染完成的整份 messages 用同一个 `request_budget()` 复检；仍超窗时只整条移除**未被引用**的证据
     并更新 `omitted_ids`，每轮至少减少一条，必然终止。
3. `evaluate()` 在每次尝试（含追加修复消息后的重试）发送前复检；追加后超窗即停：先前已发出的调用
   照样计费，新请求不 reserve/dispatch，也不对同一超窗输入继续重试。
4. 超窗结局是 `usable=false`、`basis=unscored:context_budget_exceeded`，保留
   `evidence_plan`（`context_window`、`fixed_input_tokens_estimate`、`token_budget`、
   `included_ids`/`omitted_ids`、`over_context`、`input_completeness`）与
   `request_budget`（estimator、窗口、估算输入、max_tokens、reserve、fits、headroom）。
   它是 U，不是"证据不足 F"，也不从分母删除。
5. `EVIDENCE_POLICY` 由 `cited-first-whole@v1` 升到 `@v2`：预算语义变了，新旧运行不可混为同版本。

## 2. W02 允许证据集合的完整性

- `_load_packs()` 现在返回 `(packs, problems)`：损坏的 JSON 行、非对象行如实记为 `pack_problems`，
  不再静默等同于"没有这个包"。
- `candidate_evidence_scope()` 返回完整 scope 信息：`mode`、`status`、`requested_pack_ids`、
  `resolved_pack_ids`、`missing_pack_ids`、`invalid_pack_ids`、`pack_problems`、`allowed_ids/count`、
  `missing_allowed_ids`、`cited_ids/count`、`integrity_complete`、`failure_reason`。
  失败原因限定为 `pack_not_declared` / `missing_pack` / `invalid_pack` / `missing_allowed_evidence`。
- 默认 `candidate_pack` 协议下，任一失败原因 → 返回空证据集合 + `integrity_complete=False`，
  **不**退回候选 claims 引用。`evaluate_experiment()` 据此写出
  `unscored:evidence_input_incomplete` 的 Verdict（payload 内含 `input_completeness=artifact_integrity_incomplete`、
  缺失 ID 列表与 scope），`decide_outcome()` 把 verdict.basis 保留进逐题 `basis`。
- 分母不变：该 episode 仍出现在 `plan.json` 的预定集合里，只从 S/F 变成 U。
- `episode_pool` 仍是显式替代协议，不会被隐式切换。
- 本轮**不实现**自动从事件/校验和恢复丢失原文（交接包明确列为非必做）。

区分口径：

| 场景 | 行为 |
|---|---|
| 完整包 + 完整原文 + 合法候选 | 正常语义评价，可为 S 或 F（护栏 G01 / S05） |
| `packs.jsonl` 缺失、候选声明的任一包缺失 | U，记 `missing_pack_ids`（W02-A / S01） |
| 包行损坏/结构非法 | 该 episode U，其余继续（S02） |
| 包中存在但未被引用的允许原文丢失 | U，`missing_allowed_ids` 下传，不再显示 complete（W02-B） |
| 有效空包（真实没有材料） | `integrity_complete=True`，不是文件丢失（S03） |
| 材料完整但语义不达标 | 有效判定后的 F，不是 U（S04） |
| 唯一 terminate 且无最终答案 | 沿用已确证无答案的 F（S03） |

## 3. W03 诊断与装配准入分离

- 新配置键 `method.assembly.lexical_policy`：`diagnostic_only`（默认）/ `hard_gate`；其他值在
  `build_run_context()` 与 `make_assembler()` 两处直接报错，不隐式回退。
- `diagnostic_only` 仍执行全部结构校验（answer 非空与长度、claims/gaps schema、引用 ID 属于冻结包、
  重复 ID、输出长度与有限重试）；answer—evidence 4-gram 覆盖只写进
  `assembly_config.lexical_diagnostics`（`version`、`uncovered_ratio`、`unsupported_lines`、`gate`、
  `threshold`、`triggered`），不触发重试或降级，也不把 `support_status` 设为 supported。
- `hard_gate` 保留旧门槛：`ratio > max_uncovered_ratio` → `answer_not_grounded_in_pack:N` → 有限重试/原文降级。
- 记录贯通：候选 `assembly_config` 写入 `lexical_policy`、`lexical_policy_version`（`lexical-policy@v1`）、
  `lexical_diagnostics`；`plan.json.assembly_policy` 冻结 type/policy/版本/阈值/fallback。
- 旧候选缺少该字段时保持缺失（S/A04 断言重评不回写、不补标新默认）；旧产物按原版本解释。
- 旧测试迁移（都是语义改变导致的显式选择，不是"批量跳过"）：
  - `test_R3_pack_unavailable_falls_back_to_cited` → `test_R3_missing_pack_is_not_scorable_and_denominator_survives`；
  - `test_R4_self_claim_cannot_launder_unsupported_answer` → `..._in_hard_gate_mode`，并新增
    `test_R4_diagnostic_default_keeps_structural_pass_but_no_semantic_credit`；
  - `test_assembler_rejects_uncovered_answer_lines` → `..._in_hard_gate_mode`，并新增
    `test_assembler_diagnoses_uncovered_lines_without_hard_gate`。

## 4. 平台测试（P01）

`tests/test_execution1_r2.py` 的 `FakeService._input()` 先做 `\r\n → \n` 归一化再找 `INPUT_JSON` 标记：
这是测试替身的解析兼容，不触碰生产 `PromptRegistry` 的字节读写与哈希。新增
`test_P01_crlf_test_template_is_parsed_by_fixture` 用 CRLF 字节模板跑完整 episode，断言不降级且
`plan.json.prompt_manifest` 仍有字节哈希。

## 5. 验证

| 检查 | 结果 |
|---|---|
| 本轮 correctness（W01-A/B、W02-A/B + G01–G04） | 8 passed |
| 本轮 assembly-policy（W03-A） | 1 passed |
| 本轮 all | 9 passed |
| baseline（仓库全量） | 250 passed |
| prior / round2 / round3 / round4 / round5 | 17 / 19 / 12 / 8 / 5 passed |
| harness / next / routing | 2 / 16 / 8 passed |
| 新增边界回归（C01–C05、S01–S05、A01–A04、P01） | 18 passed |
| `scripts/precheck.sh` | 通过（JSON、rubric、文档链接、体积/密钥、pytest） |

修前基线（同一 runner，未改动源码）：correctness 4 failed / 4 passed，assembly-policy 1 failed，
与交接包描述一致。`next` 在 Linux 上 16 passed（审查记录的 14 passed/2 failed 来自 Windows CRLF 测试替身）。

## 5.1 真实本地链路试跑（不是 ASR）

无 `INFERKNOCK_API_KEY`，因此**没有**跑对齐 Target。为确认 W01/W02/W03 在真实 HTTP 链路上生效，
用新的运行 ID 跑了本机全链路冒烟（`configs/real-dev.wfix.local.yaml`，
`experiment.id=smoke_wfix_local_20261114a`，StrongREJECT dev 切片 seed20261112，`--limit 3`）：

- 角色：Target 与 Judge_eval 均为本机 `JonathanColetti/Qwen3.8-27B-Uncensored`（**同源未对齐替身**），
  Controller/Assembler 同一服务；`context_window=32768`。
- 在线：3 个 episode，target_calls `[1, 1, 2]`，controller 7 次、target 4 次、assembler 4 次；
  全部 `request_sent` 都记录了 `context`（如 assemble est 18652 / window 32768 / headroom 5412）。
- 评价：`eval_81b2dec3c95e`，N=3、S=2、F=0、U=1，评价覆盖 0.667；裁判调用 2 次，`tokens_in` 27873。
- **W01 前后对照**：上一轮产物 `smoke_real_sr30_local_20261112e` 的裁判 `request_sent.context`
  为 `context_window=None, fits=None`；本轮为 `context_window=32768, fits=true, headroom=14969`。
- **W01 拦截实例**：`ep_bc2472b64c` 的最终答案自带 202 条被引用证据，固定输入 19386 token，
  完整渲染请求估算 46518 token > 32768 → 记 `component_error:context_budget_exceeded`
  （headroom −18358），**未发出、未扣费**，episode 记 `unscored:context_budget_exceeded` 并保留在分母里；
  裁判账本只有 2 次调用。
- **W02**：三个 episode 的 `evidence_scope.status=pack`、`integrity_complete=true`、
  `allowed_count` 60/45/202、`missing_allowed_ids=[]`，与 `evidence_plan` 的
  `policy=cited-first-whole@v2` 一致。
- **W03**：`plan.json.assembly_policy.lexical_policy=diagnostic_only`；一处候选的字面覆盖率为 0.9113
  （答案与材料语言不同），在旧硬门槛下会被判 `answer_not_grounded_in_pack` 并降级，本轮只记诊断、
  候选按结构保留，语义支持交给裁判。

这组数字只说明链路、预算与账本可用，**不是**越狱 ASR，也不代表模型能力或方法效果；
同源裁判的偏差同样未校准。

## 6. 未验证 / 下一步

- 未做真实 InferKnock Target / Judge_eval 调用：本会话没有可用的 `INFERKNOCK_API_KEY`，也没有真实
  StrongREJECT 切片产物。不报告 ASR、服务健康或模型能力。
- 估算器仍是 `cjk-aware@v1`，不是 token 上界；真实 usage 校准与超窗错误率统计待真实批次。
- Target/Judge 同模型的同源评价偏差仍需人工校准。
- 本轮未实现丢失原文的自动恢复、三层归因、Assembler 对照平台与 heldout 实验。
