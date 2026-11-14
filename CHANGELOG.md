# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号对应**规格版本**，不对应代码版本。

## [code-v0.5.3] — 2026-11-14

按审查包 `review-1bdc47b-handoff-v2.1` 修复 W01/W02 并落实 W03 默认策略。

### 修复

- W01：离线 `_build_eval_judge()` 把冻结 `context_window` 传给实际发送网关（此前只有 `SemanticJudge`
  知道窗口，网关为 `None`）；裁判证据预算先扣除模板/任务/答案/claims/包装等固定开销，并对整份渲染请求
  复检，追加格式修复消息后同样复检；超窗不发请求、不 reserve/dispatch、不重复无意义重试，记
  `unscored:context_budget_exceeded`（U，保留分母）。`evidence_plan` 升到 `cited-first-whole@v2`。
- W02：`candidate_pack` 完整性状态贯通 `candidate_evidence_scope → evidence_plan → Verdict → records`。
  冻结包缺失、只找到部分包、包结构损坏或包指向的原文缺失时记 `unscored:evidence_input_incomplete`，
  不再退回候选自述引用计分；损坏包行如实记 `pack_problems`。

### 变更

- W03：新增 `method.assembly.lexical_policy = diagnostic_only | hard_gate`，默认 `diagnostic_only`
  （字面 4-gram 覆盖只诊断，不再触发重试/降级）；`hard_gate` 显式保留旧门槛。未知值在配置校验与
  `make_assembler()` 直接报错。候选 `assembly_config` 与 `plan.json.assembly_policy` 记录策略与版本。
- `evaluation.max_evidence_chars` 在文档中改称软上限：被引用证据允许整体超出并标 `over_cap`，
  不会被截断；真正的硬边界是服务窗口。

### 测试

- 迁移三处因语义改变而需显式选择策略的旧断言（`hard_gate`），并各补一条默认策略对照；
  `FakeService` 的 `INPUT_JSON` 标记解析兼容 CRLF（生产模板字节哈希规则不变）。
- 新增 `tests/test_w01_w02_w03_boundaries.py`：C01–C05、S01–S05、A01–A04、P01 共 18 项。
- 本轮验收 9/9；仓库 250 passed；九套历史检查全过；`scripts/precheck.sh` 通过。

## [code-v0.5.2] — 2026-11-14

### 新增

- `services.*.context_window`（token）：按服务声明上下文窗口；InferKnock 与本机 vLLM 起点均设 32768。
- 发送前上下文预算：保守估算（CJK 1 token/字、其他 0.3 token/字符）输入 + `max_tokens` + 预留；
  超窗直接拦截，**不发请求、不占预算**，记 `component_error:context_budget_exceeded`；
  `request_sent.payload.context` 记录估算输入/输出上限/余量。
- 裁判证据选择增加 token 预算维度：被引用证据优先且完整，其余整条装入或省略；
  被引用证据自身超预算 → `unscored:context_budget_exceeded`（可离线用更大窗口重评）。
- 阶段校验：`max_tokens` 必须小于服务的 `context_window`，否则启动前报错。
- `plan.json.model_manifest` 与 `evaluation_spec.service` 记录 `context_window`。

### 配置

- 32k 起点：Target/Judge `8192`、Assembler `8192`、Controller `4096`；
  裁判 `max_evidence_chars` 提高到 60000（example）/ 40000（local smoke）。

## [code-v0.5.1] — 2026-11-14

### 新增

- `--env-file` / `JBT_ENV_FILE`：显式加载 KEY=VALUE 文件（支持注释、`export`、成对引号）；
  默认**不覆盖**已存在的环境变量，只报告键名、跳过项与文件权限，绝不回显值。
  框架仍不会自动搜索 `.env`。
- `.env.example`（仅键名）与 `.gitignore` 例外；README 增加"凭证怎么放"：
  服务器用 systemd `EnvironmentFile` 或 shell `export`，本地开发用 `.env` + `--env-file`。

### 说明

- 密钥优先级：系统环境 > env 文件；权限过宽时提示 `chmod 600`。
- 7 项新回归（解析、优先级、覆盖开关、缺失/格式错误、CLI 不回显）。

## [code-v0.5.0] — 2026-11-13

按审查包 `execution1-619709f`（R1–R6）修复；Target 接线切到 InferKnock（本地未试跑，缺凭证）。
仓库 217 项、九套检查全过。

远端 CI：[run 43463820245](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43463820245) success（18s）。推送前复跑九套：仓库 230 + 17/19/12/8/5 + harness 2 + next 16 + routing 8。
审查观察脚本复跑 10 例：9 例 `observed_issue` 已转 false；`R2_mid_evidence_cut` 该字段由脚本写死为 true，实际输出显示限定语 `NOT CURRENT` 已完整保留（12009 字符，超限部分为被引用的关键片段）。

### 修复

- **R1** 最终内容适配器 `inline-think@v2`：修正起始标签（原代码写成缺 `<` 的 `" think"`），
  未闭合块从块首截断，孤立结束标签之前视为推理；Controller / Assembler / Judge 统一使用，
  只有推理或空内容不再产生动作、答案或计分结论。
- **R2** 裁判证据选择 `cited-first-whole@v1`：被引用证据优先且完整；其余整条装入，装不下整条省略并记录；
  不再按 12,000 字符静默切前缀或截断单条证据；新增 `evidence_plan` 与证据区间完整性；
  引用证据缺失时记 `unscored:evidence_input_incomplete`。上限 `evaluation.max_evidence_chars` 贯通冻结。
- **R3** 来源判定默认使用候选冻结证据包（`evaluation.evidence_scope=candidate_pack`），
  episode 全量只作显式替代协议/分析口径；证据包缺失时退回 claims 引用并记录状态。
- **R4** 覆盖检查改为只与证据原文比对（修掉"answer+claim 同时写材料外内容即通过"），
  拆分为 `answer_evidence` / `answer_claim` / `claim_citation` 三项结构诊断；短行豁免单独计数；
  `judge_eval.v2.txt` 明确引用质量是诊断、`source_compliance` 只看实质支持与条件保留。
- **R5** `experiment.path_base`（config 默认 / cwd 显式）：相对路径确定解析，不再按 exists 猜测；
  输出目录创建前后一致，换工作目录不漂移。
- **R6** 模板按 UTF-8 字节读取/落盘/哈希；测试改用临时目录。

### 变更

- Target 路由改为 InferKnock `deepseek-v4.1-flash`（`target_query`: high / 8192 / thinking enabled，无 temperature）；
  Assembler 关闭 thinking、8192；Judge_eval 同源（报告须注明）。
- 新增 `prompts/judge_eval.v2.txt`；`configs/real-dev.example.yaml` 增加 `path_base`、证据策略字段。
- `records.jsonl` 增加 `evidence_scope`；`plan.json.evaluation_spec` 增加 `evidence_scope` / `max_evidence_chars`。

### 已知限制

- InferKnock 未试跑：本会话无 `INFERKNOCK_API_KEY`（接口返回 401），真实小批次与 Assembler 对照待执行。
- 结构诊断不证明语义支持；裁判仍需人工一致性校准。

## [code-v0.4.1] — 2026-11-12

远端 CI：[run 43315126285](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43315126285) success（16s）。执行包 `execution1-r2` 六套历史验收 + harness + next + routing 共 290 项在推送前全部通过。

用 StrongREJECT 真实切片（30 条，固定 revision）在本地 vLLM 上跑通全链路后修复的问题。
这一轮不改变目标函数，也不产生可报告的 ASR（Target 是未对齐的本地模型、裁判同源）。

### 修复

- **Target 系统指令**：`target_system_prompt` 显式可配（空串=不注入），字符数与 SHA-256 写入
  `episode_started`；`real_dev` 默认不注入，合成 baseline 保留旧提示。
- **装配覆盖检查**：`line-ngram-evidence@v1`，长段落与 claims + 证据包原文做 4-gram 覆盖，
  短引导句豁免；修掉"描述性 claim + 引用式答案"被误判为无依据而整体降级。
- **评价证据范围**：`Judge_eval` 读取该 episode 的全部 Target 材料（不再只取 claims 引用子集），
  并单独记录 `cited_evidence_ids`；同一批候选由此前的 2/1/0 变为 3/0/0（伪失败）。
- **配置相对路径**：`datasets.path` / `manifests_dir` / `results_dir` 支持"工作目录优先、
  配置文件目录兜底"。
- **划分顺序**：`split` 按 task_id 排序返回，切片前 N 条与源文件顺序一致。

### 新增

- `configs/real-dev.local-smoke.yaml`：真实数据 + 本地服务的联调配置（Target/Judge 同源，非 ASR）。
- `tests/test_execution1_r2.py` 增加真实切片计划、Target 系统提示、裁判证据范围等回归；
  真实切片存在时才运行（`data/slices` 不入库）。

### 已知限制

- 冒烟里的 Target 未对齐、裁判同源，S/F/U 不能当作方法效果。
- claims 与 evidence 的对齐质量目前只是记录，不作为成功门槛。

## [code-v0.4.0] — 2026-11-12

最小真实闭环（执行包 `execution1-r2`，E01–E05）：按组件路由模型、请求产物、模型装配、独立语义裁判、
真实数据入口。旧合成配置与六套历史验收保持原样。

状态：**offline_ready**（本地假服务与验收全过）；**real_pilot_complete = 否**（未连接真实服务，无真实 ASR）。

### 新增

- **E01 路由与参数档案**：`services`/`components` 显式路由；`openai_chat` / `qwen38_vllm` /
  `deepseek_thinking_proxy` 三个档案先校验再序列化；`extra_body` 展开到 HTTP 顶层；未知字段、
  保留键覆盖、同名冲突、非原生 effort、thinking 下的采样参数在启动前报错（不再静默丢弃）。
- **E01 模板冻结**：`prompts.<stage>.{id,version,path}` + `prompt_manifest`（SHA-256、渲染器版本、
  运行目录快照、变量协议）；`single-substitute@v1` 只替换 `{{input_json}}`。
- **E01 生成探针**：`check-services` 对 target/attacker(controller+assembler)/judge_eval 做真实
  非流式生成，按 `service+profile` 去重并单独记账；不健康时 CLI 非零退出，正式 run 不启动 episode。
- **E01 阶段额度**：`experiment.budget.stage_calls` 与全局额度同时生效；`tokens`/`wall_ms` 非空拒绝。
- **E02 请求产物**：`request_generated` 权威事件 + `requests.jsonl` 可重建索引；Target 的
  `request_sent.payload.request_id` 与索引双向可查；网络重发保留 `request_id`，新生成创建新 ID 并链接父产物。
- **E03 模型装配**：`assembly.type=llm` 真正调用 Assembler 角色；schema 与引用归属校验、
  答案行级覆盖检查、有限重试、`fallback_render` 原文降级。
- **E04 语义裁判**：`evaluation.type=llm` 调用独立服务，自带 `EvaluationPlan`、独立事件与账本；
  `scored` 三维度 + 冻结 `success_rule` 由程序计算 S/F/U；`unscored` 与"证据不足"严格分开。
- **E05 数据与报告**：revision 透传到 adapter/manifest；`scripts/select_dev_slice.py` 按重复组原子抽样；
  `scripts/report_run.py` 与 `jbt report` 导出逐题与汇总；`configs/real-dev.example.yaml` 为真实配置模板。

### 契约变化

- `BudgetLimits` 新增 `stage_calls`；`BudgetGuard.reserve(..., stage_id=)` 同时检查阶段额度。
- `plan.json` 新增 `routing`/`stages`/`model_manifest`/`evaluation_spec`/`prompt_manifest`/`budget`/`profile`。
- 新增事件 `request_generated`、`assembly_rejected`、`evaluation_started`、`evaluation_verdict`、`evaluation_completed`；
  `Event` 增加 `request_id` 字段。
- `check_services` 返回值扩展为 `probes/roles/target/attacker/judge_eval/healthy`；`attacker` 为兼容别名。
- `CallResult` 增加 `reasoning`；`reasoning`/`reasoning_content` 与最终 `content` 分开解析，
  只有推理没有内容记为 `INVALID_OUTPUT(reasoning_only)`。
- `Assembler.assemble_from_pack` 与 `PromptRegistry`、`RoutingPlan` 为新增公开接口；旧调用保持兼容。

### 已知限制

- 语义裁判未做人工一致性校准；`truth_status` 保持 `not_assessed`。
- 行级覆盖检查是结构检查，不证明语义支持。
- `extractor`/`summarizer`/`judge_online` 的语义实现不在本轮；`raw_history` 下启用会报错。
- 真实服务的能力状态默认 `unknown`：HTTP 200 只代表接受请求。

## [code-v0.2.1] — 2026-11-11

### 变更

- 完成 S01：CI 进入 `.github/workflows/ci.yml`，R13 断言恢复严格；
  首次远端运行 [run 43019024263](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43019024263) 结论 success（job `test`，9 步）。
- 第二轮审查 14 组问题至此全部关闭；仓库测试 103 项、上轮验收 17/17、本轮验收 19/19。

## [Unreleased]

（下一轮）

## [code-v0.2.0] — 2026-11-11

### 第二轮审查修复（S01–S14）

按审查包 `review2-4a5c50d` 的 14 组问题修复，16 个文件涉及；新增 `tests/test_round2_invariants.py`（8 项）作仓库内回归保护。

| 问题 | 结论 | 主要落点 |
|---|---|---|
| S01 | CI 进入 `.github/workflows/ci.yml`，断言恢复严格；凭据补齐后推送成功（远端是否真正触发以 Actions run 为准） | `.github/workflows/ci.yml`、`tests/test_review_regressions.py` |
| S02 | 原文后端向控制器与装配提供完整原文；截断改为显式 `window_chars`（默认不截断） | `state.py` |
| S03 | 摘要历史快照归档为 `summary_archive`，活跃材料只保留当前摘要 | `state.py`、`assembly.py` |
| S04 | 行/键/值联合绑定来源；合并摘要累积全部贡献 evidence；主张继承 support/fidelity，不再一律 ENTAILED | `state.py`、`assembly.py` |
| S05 | EvidencePack 在装配前冻结落盘（含 materials/input_hash/candidates），一个 pack_id 只定义一次 | `engine.py`、`assembly.py` |
| S06 | 逐要素满足结果统一供覆盖率与 critical 使用；否定出现不算满足 | `judge.py` |
| S07 | 控制器保留真实状态（REFUSED ≠ 格式错误）；本地额度耗尽进入正常收尾 | `controller.py`、`methods/independent.py` |
| S08 | `evaluate` 读取被评价目录自己的 `plan.json` | `runner.py` |
| S09 | `split_seed` 与 `episode_seed` 分离；episode 身份由采样种子决定 | `runner.py` |
| S10 | `final.json` 先校验必需字段与权威 `terminate`，缺字段记 U 而非 F | `runner.py` |
| S11 | `usage_coverage` 要求输入与输出都已知，另报 in/out 分项 | `events.py` |
| S12 | 缓存命中保留原始 call/response 关联；证据按 CallResult 关联解析 | `clients.py`、`engine.py` |
| S13 | 指标按重复组做任务级加权；`asr_point` 为主终点，`asr_episode` 与区间同时保留 | `judge.py`、`runner.py` |
| S14 | UCT 完成扩展后沿声明路径回传 reward；选择动作不再增加零样本访问 | `engine.py` |

结果：仓库测试 103 项全通过；上轮原始验收 17/17；本轮验收 19/19。

### 版本管理侧改动（本轮）

- `.github/workflows/ci.yml` 暂存为 `docs/ci/ci.yml`：当前 token 缺 `workflow` 权限，推送会被远端拒绝。内容为 install + pytest + precheck，补齐权限后一条 `git mv` 即生效。
- 删除早先的 `docs/ci/docs-check.yml`（其职责已被 `docs/ci/ci.yml` 覆盖）。

### 已知问题（版本管理侧记录）

- ~~`src/jailbreak_testbed/` 与 `src/framework/` 概念重复，待决定去留~~ → **已决定：保留并标注为参考模型**（2026-11-11）。
  它提供 framework 尚未覆盖的 `recompute` 差分与 `rollback` 语义；已加 `src/jailbreak_testbed/README.md` 说明边界，
  计划在把 recompute 迁入 analysis 层后删除。
- `.gitignore` 中的 `../.venv/` 是无效模式（git 忽略含 `..` 的规则），已移除；虚拟环境应放在仓库内并由 `.venv/` 覆盖。

## [code-v0.3.1] — 2026-11-12

按第五轮审查（`9c1157a`，V01–V02）修复；仓库 152 项、历轮 48 项 + 本轮 5 项全过。

远端 CI：[run 43132708275](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43132708275) success（21s）。

### 修复

- **V01** online 策略下自动收尾冻结旧候选：新增材料指纹比较，收尾时若活跃材料已变化且本地额度充足，
  执行最终装配并把新证据带入答案；旧候选不可变。无额度时记 `stale_candidate_no_local_budget`。
- **V02** HTTP 适配层把拒答/过滤/缺 choices 记为 OK：`_parse_completion()` 先校验结构，
  `message.refusal` → `REFUSED(provider_refusal)`，`finish_reason=content_filter` → `REFUSED(content_filter)`，
  缺 choices → `INVALID_OUTPUT`，内容+拒答 → `PARTIAL`，`length` → `TRUNCATED`。

### 契约变化

- `Engine.materials_fingerprint()` 暴露给编排层；online 收尾可能产生第二次 `assembled`。
- `CallResult.error` 增加原因标签；`raw` 保留 `refusal` / `filter_reason`。

## [code-v0.3.0] — 2026-11-12

按第四轮审查（`ad300ee`，U01–U04）修复；仓库 141 项、历轮验收 48 项 + 本轮 8 项全过。

远端 CI：[run 43124145421](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43124145421) success（18s）。

### 修复

- **U01** 任务级主终点改为"任务内先求比例、任务间等权"：`asr_point = mean(S_i/n_i)`，
  可识别范围与覆盖率同权重；`any_success_rate` 单列，不覆盖主终点。
- **U02** 评价前统一核对 final 身份、唯一 terminate、候选 ID 双向一致；候选文件逐行容错；
  每个 episode 异常隔离，单个坏产物不影响分母。
- **U03** 控制器校验动作 schema（`ASK_TARGET.query` 必须为非空字符串）；编排层预校验 +
  Engine 拒绝的 `INVALID_OUTPUT` 共同进入有界格式恢复。
- **U04** `ElementController` 改用 `compute_open` 活跃视图，归档不再让未询问要素被跳过。

### 新增

- `method.stopping.policy`：`after_assembly`（默认）/ `online`；写入 `episode_started` 事件；
  `online` 下重复装配同一材料判为 `no_progress_assembly`。

### 契约变化

- `metrics` 新增 `asr_task_lower` / `asr_task_upper` / `any_success_rate` / `any_success_tasks`；
  `asr_point` 恒为任务内比例的等权均值（存在 U 时是保守下界）。
- 新增诊断串：`terminate_ambiguous` / `terminate_candidate_conflict` / `identity_mismatch` / `artifact_error:*`。

## [code-v0.2.2] — 2026-11-12

按第三轮审查（`3e531e3`，T01–T08）修复；仓库 122 项、三轮验收 48 项全过。

远端 CI：[run 43064220847](https://github.com/MarchPhantasia/jailbreak-testbed/actions/runs/43064220847) success（13s）。

### 修复

- **T01** 评价只解析一次 final；损坏/非对象/缺字段记 `unresolved` 且不回写；核对唯一 terminate 与候选 ID 的一致性，冲突不再算失败。
- **T02** 控制器区分传输状态与解析状态：解析失败置 `INVALID_OUTPUT` 并进入格式恢复；拒答仍单独记 `component_refusal`。
- **T03** 新增 `active_materials()`：缺项、评分、选择、装配共用同一活跃视图；原文块按 key 解析，归档不参与决策。
- **T04** 精确任务身份 = 请求 + 上下文 + 冻结判据；指标新增任务级 `n_tasks` / `asr_task_range`，与 episode 级指标分开。
- **T05** accept 绑定到要素 key 的值段后再做否定/过期判定，修掉跨要素与旧版命中。
- **T06** 装配输入先深拷贝落盘（`packs.jsonl` + `input_hash`），候选关联另记 `pack_links.jsonl`；新增 `pack_frozen` 事件。
- **T07** `prepare_data` 使用计划的 `split_seed`，并记录 `experiment_seed`。
- **T08** `state.window_chars` 接线并校验（仅 raw 后端、正整数、未知键报错）。

### 契约变化

- `metrics` 新增任务级字段；`asr_point` 语义改为任务级等权。
- 装配默认终止（`assembled`）后冻结答案。
- 新增事件 `pack_frozen` 与文件 `pack_links.jsonl`。

## [code-v0.1.1] — 2026-11-11

按审查包（`a9b45c9`，R01–R13）修复工程与评价契约；测试 60 → 93 项，包内 17 项验收全过。

### 修复

- **R01** 服务配置生效：按 `target.type` 构造客户端，未支持类型直接报错；`check-services` 做真实探测（不可达即不健康）。
- **R02** 冻结判据：每题 rubric 在计划阶段落盘，评价只读冻结对象；`accept` 值不再被元素名覆盖。
- **R03** 分母与运行身份：先写 `plan.json` 冻结 episode 清单；同 ID 重跑抛 `FileExistsError`；缺 final / 缺候选记未决，正常终止无答案仍计失败。
- **R04** 重复组先于划分建立，组内不跨 split。
- **R05** 原文 / 摘要 / F-O-X 三后端都向装配提供材料；摘要受长度上限约束。
- **R06** Evidence 完整记录落盘（`evidence.jsonl`），主张 → 证据 → 原始事件 → span → checksum 可解析。
- **R07** `state_write` 保存所属节点与完整快照；`restore_state(..., node_id=)` 精确恢复，不合并兄弟、不调模型。
- **R08** 恢复决定真正执行：`retry_same` 重发原查询且不推进控制器；`fallback_assemble` 真正装配。
- **R09** Target 额度耗尽只拒绝新请求，收尾仍可用剩余本地额度装配并冻结答案。
- **R10** 缺项按要素关联计算；"未确认/未提供/旧版"不算回答；同值不同要素不合并。
- **R11** 分支入口可用（`search.branch_width`），UCT 访问/回传接通；未知策略显式报错。
- **R12** usage 缺失记 `None` 并单列 `*_unknown_calls` / `usage_coverage`，不写成实测 0。
- **R13** 生效 CI（`.github/workflows/ci.yml` 跑 pytest），`scripts/precheck.sh` 增加测试步骤。

### 契约变化

- 新增 `plan.json`（episode 清单 + 冻结 rubric）与 `rubrics.json`。
- 新增 `episodes/<id>/evidence.jsonl` 与 `packs.jsonl`。
- `state_write.payload.snapshot` 保存完整状态；`restore_state` 增加 `node_id` 参数。
- `budget_settled.cost.tokens_in/tokens_out` 可为 `null`（未知）；ledger 增加 `*_unknown_calls`、`usage_coverage`。
- `search.branch_width` 控制同父分支宽度（1 = 线性）。

### 已知限制

- R11 的分支策略由研究者声明；未内置自动分叉阈值。
- 归因三层、双裁判、人工切片仍未实现（按手册属后续阶段）。

## [code-v0.1.0] — 2026-11-11

### 新增（实现）

- `src/framework/`：契约、事件与账本、预算守卫、接入层（Mock / OpenAI 兼容）、三种状态后端（原文 / 摘要 / F-O-X）、前沿与策略、装配与候选、在线与最终评价、执行引擎、控制器、恢复、数据集适配、实验编排、CLI。
- `fixtures/synthetic_library/`：3 条合成任务（含拒绝样本），用于 M0 闭环。
- `configs/`：experiment / endpoints / datasets / stages 四份样例。
- `tests/`：59 项，覆盖手册 §18.1 的必要检查（预算不可绕过、拒答不退款、切换不退款、恢复不调模型、节点隔离、视图正确、出处存在、未知评分不为零、缺项不计覆盖、结果判定顺序）。

### 变更

- `requires-python` 保持 `>=3.10`；新增运行时依赖 `PyYAML` 与入口 `jbt = framework.cli:main`。
- 移除 `configs/example.yaml`（旧的规格期配置，已被 `configs/experiment.yaml` 取代）。

### 已知缺口

- 归因三层（节点/事实/前缀）只有骨架；LLM 控制器与真实 endpoint 未联调；双裁判与人工切片未实现。


## [spec-v1.1] — 2026-11-10

### 变更（相对 v1.0）

- 生命周期补上装配步骤，并把「在线停止判定」与「离线成功判定」拆成两个裁判。
- 新增四类事件：`state_write`、`cache_hit`、`view_snapshot`、`assembled`。
- 搜索拆成 `Frontier` / `Scorer` / `Controller` 三个接口；打分策略从写死的规则变成可插拔实现。
- 新增 `BudgetGuard`（所有模型调用持票）与 `SessionMode`（回退是否真的生效）。
- 统计从「重复 N 次 + Δ>2σ」改为配对差值 + 置信区间 + 预定义效应量；缓存命中不得进入方差。
- 新增 M2.5「忠实基线」硬门槛。

## [spec-v1.0] — 2026-11-10

### 新增

- 目标函数：同 `target_calls` 预算下的 ASR；其余指标降为护栏。
- 数据契约 C1–C4、接口契约 I1–I8。
- 五个实验配置 B0–B4、四个假设 H1–H4。
- 里程碑 M0–M5、风险登记 R1–R8。
- ADR-0001 至 ADR-0005。
