# 网页「结束」与批处理收尾一致性（R22 固定样例回归）

- 日期：2026-11-20；基线 `47ec82d`
- 修复：`InteractiveSession.finalize()` 与批处理 `run_episode._finalize` 同一决策序列——
  新鲜度以**当前选中候选自己引用的包**为起点（`Engine.candidate_pack_hashes` /
  `Engine.candidate_materials_changed`，批处理同源共用）；预算回退共用模块级
  `no_assembly_budget_reason`；`engine.terminate` 幂等保证双击结束只留一条终止记录。
- 快照构造：原 R22 运行为单次装配冻结（无两阶段序列），快照从**原包归档材料**前缀切分
  （前 10 / 全 19），独立回归目录使用，标 regression_constructed；原冻结包与历史候选未动。
- 调用数（账本）：Assembler **3**（S2×2 + S3×1，均为真实本地 27B）、Target **0**、独立 Judge **0**。

## 三行表

| 场景 | 结束前候选与包 | 结束时材料是否变化 | 最终候选与包 | 实际新增调用数 |
|---|---|---|---|---|
| S1 材料未变 | cand_217ebd143ff7 / pack_6ed40af26273（f77bd3e358c3…） | 否（指纹=原包 input_hash） | cand_217ebd143ff7 / pack_6ed40af26273（不变） | assembler 0 / target 0 / judge 0；连续两次结束终止记录仍 1 条 |
| S2 online·材料已变·额度可用 | cand_64071434271c / 快照1包（2aadcf8f7b26…，前 10 材料，回归构造） | 是（快照2=全部 19 材料） | cand_91f8e7d228bc / 新包 pack_130ab81e86ae（f77bd3e358c3…） | assembler 2（快照1装配 1 + finalize 重装配 1）/ target 0 / judge 0 |
| S3 online·材料已变·额度耗尽 | cand_0659c4fddf45 / 快照1包（2aadcf8f7b26…） | 是（快照2=全部 19 材料） | cand_32c1ce1c961b / 回退包 pack_a853b4fc6559（f77bd3e358c3…，快照2材料冻结） | assembler 0（快照1装配 1 已在预算内；finalize 原文回退 0）/ target 0 / judge 0 |

## S3 回退明细（最终选了哪份答案、实际用了哪个包、因何回退）

- **最终答案**：`cand_32c1ce1c961b`，material_template 原文回退（61773 字符 / 19 主张，
  保留 Target 原文与缺项，`fallback_render=true`）；
- **实际用的包**：`pack_a853b4fc6559`（快照 2 材料=全部 19 条冻结，input_hash `f77bd3e3…`
  与原 R22 包内容一致）；
- **回退原因**：`assembly_stage_exhausted`（装配阶段额度 1 已被快照 1 装配用掉；
  `action_rejected` 事件 reason=no_assembly_budget 落盘）；
- 网页按实际结果显示（`/api/candidate` 返回 fallback_render 与 reason）。

## 网页核对（零模型调用）

- S1/S2/S3：网页显示的最终候选 ID 与磁盘 `final.json` 逐一对上；
- S2：最终 `cand_91f8e7d228bc` → 新包 `pack_130ab81e86ae`（f77bd3e3…，全量材料）；
  旧候选 `cand_64071434271c` 仍可打开 → 快照 1 包 `pack_486af0ec4982`（2aadcf8f…）；
- S3：回退候选的 fallback_render/reason 在网页可见。

## 复现命令

```bash
PY=/home/pha/sci-deepseek/.venv/bin/python; cd ~/sci-deepseek/jailbreak-testbed
set -a && source .env && set +a
$PY scripts/r22_finalize_regression.py   # 3 次真实装配，Target/Judge 0
$PY -m framework.webapp --port 8765      # 网页核对（零调用）
```

回归目录：`results/r22-finalize-s1-unchanged` / `-s2-online-changed` / `-s3-no-budget`；
汇总 `results/r22-finalize-regression.json`。测试：全套通过（现有测试，未新增）。
