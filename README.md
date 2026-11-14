# Jailbreak Testbed

**Replayable, budget-accounted experiment framework for multi-turn LLM attack research.**

按《[独立研究框架完整开发方案 v3.5](docs/manual/v3.5.md)》实现的客户端侧框架。当前为 **v0.4.0**：M0 模拟闭环 + M2 证据与装配 + M2.5 分支入口 + R2 最小真实闭环（按组件路由模型、请求产物、模型装配、独立语义裁判）。全部测试在无 GPU、无网络的模拟服务上通过；真实服务由研究者提供。

## 定位

**是**：客户端侧的实验基础设施——数据清单、事件与账本、状态表示、搜索与装配、评价与产物。部署（权重、引擎、GPU）在仓库范围之外。

**不是**：攻击工具集合（不含载荷与绕过模板）、不是某个项目的 fork（`docs/adr/0001`）、不是排行榜。

**目标函数**：固定 `target_calls` 预算下的 ASR；`support` / `dependence` / 成本四元组 / 双裁判 / 配对区间都是护栏，用来防止"提升是假的"。

规格以 [手册 v3.5](docs/manual/v3.5.md) 为准；`docs/spec/` 与 `docs/adr/` 是实现前的设计笔记与决策记录，冲突时以手册为准。

## 现在能做什么

| 能力 | 状态 | 证据 |
|---|---|---|
| 合成 episode 闭环（模拟 Target） | ✅ | `tests/test_runner_e2e.py`、`tests/test_cli.py` |
| 事件即主记录、账本由事件派生 | ✅ | `tests/test_events_restore.py` |
| 预算 reserve/dispatch/settle，拒答不退、切换不退 | ✅ | `tests/test_budget.py`（5 条） |
| 状态后端：原文 / 等长摘要 / F-O-X | ✅ | `framework/state.py` |
| 证据原文定位、事实必带出处 | ✅ | `tests/test_engine_isolation.py` |
| 装配与候选（k 候选、确定性选择） | ✅ | `framework/assembly.py` |
| 在线判定与最终评价分离 | ✅ | `framework/judge.py` |
| §15.3.2 结果判定顺序（success/failure/unresolved） | ✅ | `tests/test_judge_outcomes.py`（8 条） |
| 分支：节点、前沿、策略（linear/best-first/UCT） | ✅ 骨架 | `framework/search.py` |
| 组件恢复（有限重试、不编造） | ✅ | `framework/recovery.py`、`tests/test_recovery.py` |
| 数据适配（合成 / StrongREJECT / HarmBench）+ 清单 | ✅ 适配器 | `framework/datasets.py`、`tests/test_datasets.py` |
| 冻结计划与分母（plan.json、重跑拒绝） | ✅ R03 | `tests/test_review_regressions.py` |
| 冻结 rubric、评价不重建判据 | ✅ R02 | 同上 |
| 证据落盘与按节点恢复 | ✅ R06/R07 | 同上 |
| 配置接入与真实健康检查 | ✅ R01 | 同上 |
| 分支入口（branch_width）与 UCT 访问记录 | ✅ R11 | 同上 |
| 真实 HTTP 接入（OpenAI 兼容） | ✅ 代码路径，未联调 | `framework/clients.py` |
| 按组件路由服务/模型 + 参数档案 | ✅ E01 | `framework/routing.py`、`configs/real-dev.example.yaml` |
| 模板版本冻结与运行内快照 | ✅ E01 | `framework/prompts.py`、`prompts/` |
| 三角色生成探针（target/attacker/judge_eval） | ✅ E01 | `runner.check_services` |
| 阶段调用额度（与全局同时生效） | ✅ E01 | `framework/budget.py` |
| 请求产物：权威事件 + 派生索引 + Target 关联 | ✅ E02 | `framework/requests.py` |
| 证据约束的模型装配与声明降级 | ✅ E03 | `framework/assembly.py`（`LLMAssembler`） |
| 字面覆盖只诊断 / hard_gate 变体 | ✅ W03 | `method.assembly.lexical_policy` |
| 裁判完整请求预算与输入完整性 | ✅ W01/W02 | `runner._build_eval_judge`、`candidate_evidence_scope` |
| 独立语义 Judge_eval（S/F/U 由程序计算） | ✅ E04 | `framework/evaluation.py` |
| 真实 CSV 入口、开发切片与运行报告 | ✅ E05 | `framework/slices.py`、`scripts/` |
| 最终内容判定（推理块不冒充输出） | ✅ R1 | `framework/prompts.py`（`inline-think@v2`） |
| 裁判证据选择与完整性记录 | ✅ R2 | `framework/evaluation.py`（`cited-first-whole@v2`） |
| 候选证据包口径（vs episode 池） | ✅ R3 | `runner.candidate_evidence_scope` |
| 结构诊断拆分（不宣称语义支持） | ✅ R4 | `framework/assembly.py` |
| 确定性路径基准 | ✅ R5 | `experiment.path_base` |
| 上下文预算与发送前拦截 | ✅ | `framework/context.py`、`services.*.context_window` |
| 节点级/事实级/前缀级归因 | ⏳ 未实现 | 只有 `analyze` 骨架 |
| 多候选选择器、阶段预算细调、审计流程 | ⏳ 部分 | 见「未完成」 |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 1) 数据清单与划分（不调用模型）
PYTHONPATH=src .venv/bin/python -m framework.cli prepare-data --config configs/experiment.yaml
PYTHONPATH=src .venv/bin/python -m framework.cli check-data   --config configs/experiment.yaml

# 2) 跑 episode（默认模拟 Target，不联网）
PYTHONPATH=src .venv/bin/python -m framework.cli run      --config configs/experiment.yaml

# 3) 冻结答案后评价（不重跑 Target）
PYTHONPATH=src .venv/bin/python -m framework.cli evaluate --config configs/experiment.yaml

# 3b) 导出逐题结果与汇总（不调用模型）
PYTHONPATH=src .venv/bin/python -m framework.cli report --root results/<experiment_id> --csv results/<id>/per_episode.csv

# 4) 恢复 / 审计 / 分析骨架（均不调用模型）
PYTHONPATH=src .venv/bin/python -m framework.cli restore  --episode-dir results/smoke_syn/episodes/<id>
PYTHONPATH=src .venv/bin/python -m framework.cli audit    --episode-dir results/smoke_syn/episodes/<id>
PYTHONPATH=src .venv/bin/python -m framework.cli analyze  --episode-dir results/smoke_syn/episodes/<id>

# 真实服务前置：配置校验 + 三角色生成探针（不健康时退出码非零）
# 凭证：系统环境优先；也可以显式加载项目 .env（不覆盖已存在的变量，只报告键名）
export INFERKNOCK_API_KEY=...        # 或在 ~/.bashrc / systemd EnvironmentFile 里配置
PYTHONPATH=src .venv/bin/python -m framework.cli check-services --config configs/real-dev.example.yaml
PYTHONPATH=src .venv/bin/python -m framework.cli check-services --config configs/real-dev.example.yaml --env-file .env

# 测试（230 项）
.venv/bin/python -m pytest

# 同一 experiment.id 重跑会被拒绝（防止分母混算）；换 id 或 results_dir 再跑
```

仓库内 `configs/experiment.yaml` 是冒烟配置：3 条合成任务、`target_calls: 4`、元素控制器。一次完整跑批的结果：

```
N=3  S=1  F=2  U=0  eval_coverage=1.0
class_dist = {D:1, B:1, A:1}
target_calls = [2, 3, 3]（逐 episode）
```

## 上下文预算

服务声明 `context_window`（token，例如 InferKnock 与本机 vLLM 都用 32768）。框架不依赖具体
tokenizer，用偏保守的估算（CJK 1 token/字、其他 0.3 token/字符 + 每条消息 4）做**发送前**检查：

- 估算输入 + `max_tokens` + 预留 > 窗口 → **不发请求、不占预算**，记 `component_error:context_budget_exceeded`；
- 每个 `request_sent` 记录 `context`（估算输入、max_tokens、窗口、余量），便于事后核对；
- **离线裁判走同一条拦截路径**（W01）：`_build_eval_judge` 把冻结的 `evaluation_spec.service.context_window`
  交给实际发送网关，而不是只交给 `SemanticJudge`；窗口为 `None` 时保持未知语义，不伪造 32k。
- 裁判证据预算先扣除**固定输入开销**（模板、原始任务、最终答案、claims、rubric、schema、审计字段），
  再按整份渲染请求复检；追加格式修复消息后同样复检。仍放不下时只整条省略**未被引用**的证据并记录 id；
  必需内容自身超窗 → `unscored:context_budget_exceeded`（U，保留分母），且不对同一超窗输入重复重试。
- `evaluation.max_evidence_chars` 是**软上限**：被引用证据允许整体超出并标记 `over_cap`，
  不会被截断；真正的硬边界是服务窗口。两条约束都写进 `evidence_plan`。
- 校验阶段就拒绝 `max_tokens ≥ context_window` 的配置，不等到调用时才发现。

32k 窗口的起点配置：Target/Judge `max_tokens=8192`，Assembler `8192`，Controller `4096`；
裁判 `max_evidence_chars=60000`（ASCII 约 18k token，中文则由 token 预算先约束）。

## 凭证怎么放

框架只从 `os.environ` 读取密钥；**不会自动搜索 `.env`**，也不把值写进日志、事件或产物。

| 场景 | 推荐做法 |
|---|---|
| 服务器长期运行 / systemd | 写一个 600 权限的 `EnvironmentFile=/etc/jbt/keys.env`，由服务管理器注入；或登录后 `export` |
| 本地开发、临时联调 | 项目根目录 `.env`（已 gitignore，建议 `chmod 600`），用 `--env-file .env` 或 `export JBT_ENV_FILE=.env` 显式加载 |
| 一次性命令 | `set -a; . ./.env; set +a` 后再跑 `jbt` |

优先级：**已存在的环境变量 > env 文件**（只有显式 `override` 才反转，CLI 不提供该开关）。
加载结果只打印键名、跳过项与文件权限，不回显任何值；权限过宽时提示 `chmod 600`。

## 目录

```
src/framework/
  contracts.py   数据契约（Task/Episode/State/Evidence/Action/AnswerCandidate/Verdict）
  events.py      append-only 事件与账本派生
  budget.py      reserve → dispatch → settle
  clients.py     Mock 与 OpenAI 兼容客户端、内容哈希缓存
  state.py       原文 / 摘要 / F-O-X 三种状态后端、state_write 恢复
  search.py      前沿、策略（linear/best-first/UCT）、评分器
  assembly.py    证据包、装配、候选选择
  judge.py       在线判定、最终评价、结果判定顺序与指标
  engine.py      动作校验与执行、节点/视图/状态维护
  controller.py  scripted / element / llm 三种控制器
  recovery.py    有限重试与降级
  datasets.py    适配器、去重、划分、清单
  methods/independent.py  episode 编排
  runner.py      实验产物、预检、评价运行与报告
  routing.py     服务/组件路由、参数档案、model_manifest
  prompts.py     模板注册表、冻结快照、内联 think 适配
  requests.py    请求产物（权威事件 + 派生索引）
  evaluation.py  独立语义裁判与 S/F/U 映射
  slices.py      开发切片冻结（按重复组原子抽样）
  cli.py         命令
configs/         实验、endpoints、datasets、stages 样例 + real-dev 模板
prompts/         版本化模板（plan / assemble / judge_eval）
scripts/         切片与报告脚本、precheck
fixtures/        合成任务（含模拟 Target 脚本）、无害语义对照样例
spec/rubrics/    冻结判据示例
tests/           230 个测试
src/jailbreak_testbed/  M0 参考模型（不参与运行，见其 README）
docs/            手册（canonical）、早期设计笔记、ADR
```

## 审查修复记录

执行包 R2（E01–E05）：[docs/review/execution1-r2-fixes.md](docs/review/execution1-r2-fixes.md)
审查包 execution1-619709f（R1–R6）：[docs/review/execution1-619709f-fixes.md](docs/review/execution1-619709f-fixes.md)
第三轮（T01–T08）：[docs/review/round3-3e531e3-fixes.md](docs/review/round3-3e531e3-fixes.md)
第四轮（U01–U04）：[docs/review/round4-ad300ee-fixes.md](docs/review/round4-ad300ee-fixes.md)
第五轮（V01–V02）：[docs/review/round5-9c1157a-fixes.md](docs/review/round5-9c1157a-fixes.md)

a9b45c9 的 13 项问题（R01–R13）逐项状态、修改、验证与剩余限制见
[docs/review/a9b45c9-fixes.md](docs/review/a9b45c9-fixes.md)。

## 与手册的对应

| 手册阶段 | 本版 |
|---|---|
| M0 模拟闭环 | ✅ 契约、Engine、事件、预算、阶段继承占位、模拟客户端、恢复 |
| M0.5 数据准备 | ✅ 适配器与清单；真实数据文件需研究者放入 `data/raw/` |
| M1 服务接入 | ✅ 代码 + 路由/预检；未与真实 endpoint 联调 |
| M2 证据与装配 | ✅ 原文定位、保真标记、单次装配、缺项 |
| M2.5 分支执行 | ✅ 节点/前沿/策略/隔离；策略可换 |
| M3 评价校准 | ✅ 语义裁判接口；⏳ 与人工的一致性校准未做 |
| M4–M6 | ⏳ 未开始 |

## 已实现的关键约束（都有测试）

- 目标预算不可绕过；已发出请求（含拒答）不退预算；切换分支不退预算。
- 缓存命中记 `served=false`，不进入独立样本。
- 事实必须带 `evidence_ids` 且能在原文中定位；无出处内容只能进 `hypotheses`。
- 缺项行（`未提供：…`）不计入覆盖——否则"说明自己没提供"会被判为完成。
- 未知评分记为 `null`，不写 0、不当失败。
- `restore` 只重放 `state_write`，不调用模型。
- 每个 episode 只有一条 `terminate`；结果在 EvaluationRun 侧统一落定。
- `candidate_pack` 协议下，冻结包缺失/只找到部分包/包结构损坏/包指向的原文缺失 → 该 episode 记
  `unscored:evidence_input_incomplete`（U），**不**退回候选自己写的引用来认证来源，分母不缩水；
  完整但内容不足仍判 F，完整且合法仍可判 S。
- 装配的字面 4-gram 覆盖默认只作诊断（`lexical_policy=diagnostic_only`）：结构、引用归属仍校验，
  语义支持由冻结 Judge_eval 判定；`hard_gate` 显式保留旧的拒收/重试行为并写入候选配置与计划。

## 未完成（下一轮）

1. 真实服务联调 + 20–30 条开发批次与人工复核（`configs/real-dev.example.yaml`）。
2. 语义抽取/摘要与在线语义停止（`raw_history` 之外的后端）。
3. 节点级 / 事实级 / 前缀级干预与残留检查（§16）。
4. 多候选选择与分支搜索在固定 Target 预算下的对照。
5. 语义裁判与人工判定的一致性抽检；`truth_status` 审计。

## License

MIT（见 `LICENSE`）。
