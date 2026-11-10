# Jailbreak Testbed

**Replayable, budget-accounted experiment framework for multi-turn LLM attack research.**

按《[独立研究框架完整开发方案 v3.5](docs/manual/v3.5.md)》实现的客户端侧框架。当前为 **v0.1.1**：M0 模拟闭环 + M2 证据与装配 + M2.5 分支入口，并已按审查包（R01–R13）修复工程与评价契约。全部测试在无 GPU、无网络的模拟服务上通过。

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

# 4) 恢复 / 审计 / 分析骨架（均不调用模型）
PYTHONPATH=src .venv/bin/python -m framework.cli restore  --episode-dir results/smoke_syn/episodes/<id>
PYTHONPATH=src .venv/bin/python -m framework.cli audit    --episode-dir results/smoke_syn/episodes/<id>
PYTHONPATH=src .venv/bin/python -m framework.cli analyze  --episode-dir results/smoke_syn/episodes/<id>

# 测试（93 项）
.venv/bin/python -m pytest

# 同一 experiment.id 重跑会被拒绝（防止分母混算）；换 id 或 results_dir 再跑
```

仓库内 `configs/experiment.yaml` 是冒烟配置：3 条合成任务、`target_calls: 4`、元素控制器。一次完整跑批的结果：

```
N=3  S=1  F=2  U=0  eval_coverage=1.0
class_dist = {D:1, B:1, A:1}
target_calls = [2, 3, 3]（逐 episode）
```

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
  runner.py      实验产物与评价运行
  cli.py         命令
configs/         实验、endpoints、datasets、stages 样例
fixtures/        合成任务（含模拟 Target 脚本）
spec/rubrics/    冻结判据示例
tests/           59 个测试
src/jailbreak_testbed/  M0 参考模型（不参与运行，见其 README）
docs/            手册（canonical）、早期设计笔记、ADR
```

## 审查修复记录

a9b45c9 的 13 项问题（R01–R13）逐项状态、修改、验证与剩余限制见
[docs/review/a9b45c9-fixes.md](docs/review/a9b45c9-fixes.md)。

## 与手册的对应

| 手册阶段 | 本版 |
|---|---|
| M0 模拟闭环 | ✅ 契约、Engine、事件、预算、阶段继承占位、模拟客户端、恢复 |
| M0.5 数据准备 | ✅ 适配器与清单；真实数据文件需研究者放入 `data/raw/` |
| M1 服务接入 | ✅ 代码路径；未与真实 endpoint 联调 |
| M2 证据与装配 | ✅ 原文定位、保真标记、单次装配、缺项 |
| M2.5 分支执行 | ✅ 节点/前沿/策略/隔离；策略可换 |
| M3 评价校准 | ⏳ 只有规则判据；语义抽检未做 |
| M4–M6 | ⏳ 未开始 |

## 已实现的关键约束（都有测试）

- 目标预算不可绕过；已发出请求（含拒答）不退预算；切换分支不退预算。
- 缓存命中记 `served=false`，不进入独立样本。
- 事实必须带 `evidence_ids` 且能在原文中定位；无出处内容只能进 `hypotheses`。
- 缺项行（`未提供：…`）不计入覆盖——否则"说明自己没提供"会被判为完成。
- 未知评分记为 `null`，不写 0、不当失败。
- `restore` 只重放 `state_write`，不调用模型。
- 每个 episode 只有一条 `terminate`；结果在 EvaluationRun 侧统一落定。

## 未完成（下一轮）

1. 节点级 / 事实级 / 前缀级干预与残留检查（§16）。
2. LLM 控制器与抽取器的提示模板、JSON 校验与降级路径联调。
3. 阶段预算（§8.1）与 `budget_settled` 的阶段分解报表。
4. 双裁判与人工切片工具（§15.2）。
5. 真实 StrongREJECT / HarmBench 清单落地与 heldout 冻结。

## License

MIT（见 `LICENSE`）。
