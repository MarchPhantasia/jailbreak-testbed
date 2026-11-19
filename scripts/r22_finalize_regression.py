#!/usr/bin/env python
"""R22 固定样例回归：网页「结束」与批处理收尾一致性（三场景）。

场景（全部零 Target / 零独立 Judge；Assembler 真实调用共 3 次）：
  S1 材料未变：结束前候选=本轮 A/B 交付的 cand_217ebd143ff7（A 臂）+ 原包 19 材料。
     finalize → 保留原候选、0 新增装配；连续两次结束 → 终止记录仍 1 条。
  S2 online 模式材料已变 + 额度可用：快照1=原包前 10 材料（真实装配 C1），
     快照2=全部 19 材料（回归构造，标明）。finalize → 新装配 C2 追溯新包；
     旧候选 C1 仍可打开；final.json 与网页显示一致。
  S3 online 模式材料已变 + 装配额度耗尽（stage assemble=1，被 C1 用掉）：
     finalize → 按批处理回退规则走原文回退，记录最终答案/实际包/回退原因。

快照构造说明：原 R22 运行是单次装配冻结（无两阶段序列），故快照从**已归档的
原包材料**按前缀切分（前 10 / 全 19），在独立回归目录使用，标 regression_constructed。
原冻结包与历史候选不动。

用法：
  set -a && source .env && set +a
  python scripts/r22_finalize_regression.py
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402
from framework.interactive import InteractiveSession  # noqa: E402

MANIFEST = "data/samples/r22-regression.json"
S1_ROOT = "results/r22-finalize-s1-unchanged"
S2_ROOT = "results/r22-finalize-s2-online-changed"
S3_ROOT = "results/r22-finalize-s3-no-budget"
SNAPSHOT_SPLIT = 10  # 快照1 = 原包前 10 材料


def load_r22():
    m = json.load(open(MANIFEST, encoding="utf-8"))
    ident = m["identity"]
    src_ep = os.path.join(ident["source_run_root"], "episodes", ident["episode_id"])
    pack_row = None
    for line in open(os.path.join(src_ep, "packs.jsonl"), encoding="utf-8"):
        row = json.loads(line)
        if row["pack_id"] == ident["pack_id"]:
            pack_row = row
    assert pack_row is not None and pack_row["input_hash_v2"].startswith(ident["input_hash_v2"][:12])
    plan = runner.load_plan_from_root(ident["source_run_root"])
    entry = next(e for e in plan["episodes"] if e["episode_id"] == ident["episode_id"])
    from framework.contracts import Task
    return m, Task(**entry["task"]), pack_row, src_ep


def assembler_calls(sess) -> int:
    return sum(1 for ev in sess.store.of_type("request_sent") if ev.role == "assembler")


def inject(session: InteractiveSession, materials, pack_row=None):
    """把（归档）材料注入 root 状态；pack_row 给定时同时注入既有冻结包。"""
    st = session.engine.states[session.engine.current_node_id or "root"]
    st.materials = copy.deepcopy(materials)
    if pack_row is not None:
        session.engine.packs.append(copy.deepcopy(pack_row))
        with open(os.path.join(session.engine._evidence_dir, "packs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(pack_row, ensure_ascii=False, default=str) + "\n")


def scenario_1(cfg, task, pack_row, src_ep) -> dict:
    """材料未变：保留原候选、0 新增装配、双击结束一条终止。"""
    if os.path.exists(os.path.join(S1_ROOT, "plan.json")):
        raise SystemExit(f"{S1_ROOT} 已存在")
    sess = InteractiveSession(cfg, task, S1_ROOT)
    inject(sess, pack_row["materials"], pack_row=pack_row)
    # 结束前候选：本轮 A/B 交付的 A 臂（磁盘读回）
    cand = runner._load_final_candidate(
        "results/r22-ab-20261120/episodes/ep_49f441c969", "cand_217ebd143ff7")
    assert cand is not None, "A/B A 臂候选读回失败"
    sess.engine.candidates.append(cand)
    sess.engine.final_candidate = cand

    before = assembler_calls(sess)
    out = sess.finalize("s1_user_finish")
    after = assembler_calls(sess)
    term = [e for e in sess.store.of_type("terminate")]
    assert sess.engine.final_candidate.candidate_id == cand.candidate_id, "S1：候选被换掉"
    assert after - before == 0, f"S1：不应新增装配调用，实际 {after - before}"
    assert len(term) == 1, f"S1：终止记录应 1 条，实际 {len(term)}"
    # 连续第二次点击结束
    sess.finalize("s1_second_click")
    term2 = [e for e in sess.store.of_type("terminate")]
    assert len(term2) == 1, f"S1：二次结束后终止记录应仍 1 条，实际 {len(term2)}"
    fin = json.load(open(os.path.join(S1_ROOT, "episodes", sess.eid, "final.json"), encoding="utf-8"))
    assert fin["final_candidate_id"] == cand.candidate_id
    return {
        "scenario": "S1 材料未变",
        "结束前候选与包": f"{cand.candidate_id} / {pack_row['pack_id']}（{pack_row['input_hash'][:12]}…）",
        "结束时材料是否变化": "否（指纹=原包 input_hash）",
        "最终候选与包": f"{fin['final_candidate_id']} / {pack_row['pack_id']}（不变）",
        "实际新增调用数": {"assembler": after - before, "target": 0, "judge": 0},
        "终止记录": len(term2),
        "root": S1_ROOT, "episode": sess.eid,
    }


def _changed_session(cfg, task, root, *, online=True, assemble_budget=None):
    """S2/S3 公共构造：online 会话 + 快照1 真实装配 C1。返回 (sess, P1, C1)。"""
    if os.path.exists(os.path.join(root, "plan.json")):
        raise SystemExit(f"{root} 已存在")
    cfg = copy.deepcopy(cfg)
    if online:
        cfg["method"]["stopping"]["policy"] = "online"
    cfg["method"]["assembly"]["k"] = 1
    if assemble_budget is not None:
        cfg["experiment"]["budget"]["stage_calls"]["assemble"] = assemble_budget
    # ctx 必须从改后的 cfg 解析：stage_calls 才会进 BudgetLimits（否则额度耗尽场景失效）
    ctx = runner.build_run_context(cfg)
    sess = InteractiveSession(cfg, task, root, ctx=ctx)
    return sess, cfg


def scenario_2(cfg, task, pack_row, src_ep) -> dict:
    """online + 材料已变 + 额度可用：重装配追溯新包，旧候选仍在。"""
    sess, _ = _changed_session(cfg, task, S2_ROOT)
    snap1 = pack_row["materials"][:SNAPSHOT_SPLIT]
    snap2 = pack_row["materials"]
    inject(sess, snap1)
    # 快照1 真实装配 → C1（1 次装配调用）
    sess.assemble_now("s2_snapshot1_assembly")
    c1 = sess.engine.final_candidate
    p1_hash = sess.engine.candidate_pack_hashes()[0]
    assert c1 is not None and p1_hash
    calls_c1 = assembler_calls(sess)

    # 材料变化：注入快照2（全部 19 材料；回归构造）
    inject(sess, snap2)
    changed = sess.engine.candidate_materials_changed(sess.engine.current_node_id)
    assert changed, "S2：材料应已变化"

    out = sess.finalize("s2_user_finish")
    c2 = sess.engine.final_candidate
    calls_c2 = assembler_calls(sess)
    p2_hash = sess.engine.candidate_pack_hashes()[0]
    assert c2.candidate_id != c1.candidate_id, "S2：应产生新候选"
    assert p2_hash != p1_hash, "S2：最终候选应追溯更新后的包"
    # 旧候选仍可打开（落盘后磁盘读回）
    fin_dir = os.path.join(S2_ROOT, "episodes", sess.eid)
    c1_back = runner._load_final_candidate(fin_dir, c1.candidate_id)
    assert c1_back is not None, "S2：旧候选应仍可读"
    fin = json.load(open(os.path.join(fin_dir, "final.json"), encoding="utf-8"))
    assert fin["final_candidate_id"] == c2.candidate_id
    assert fin["final_candidate_pack_ids"] == c2.evidence_pack_ids
    return {
        "scenario": "S2 online·材料已变·额度可用",
        "结束前候选与包": f"{c1.candidate_id} / 快照1包（{p1_hash[:12]}…，前 {SNAPSHOT_SPLIT} 材料，回归构造）",
        "结束时材料是否变化": "是（快照2=全部 19 材料）",
        "最终候选与包": f"{c2.candidate_id} / 新包 {c2.evidence_pack_ids[0]}（{p2_hash[:12]}…）",
        "实际新增调用数": {"assembler": calls_c2, "target": 0, "judge": 0,
                          "说明": f"快照1装配 {calls_c1} + finalize 重装配 {calls_c2 - calls_c1}"},
        "终止记录": len([e for e in sess.store.of_type("terminate")]),
        "root": S2_ROOT, "episode": sess.eid,
        "旧候选可打开": c1_back.candidate_id,
    }


def scenario_3(cfg, task, pack_row, src_ep) -> dict:
    """online + 材料已变 + 装配额度耗尽：批处理回退规则（原文回退）。"""
    sess, _ = _changed_session(cfg, task, S3_ROOT, assemble_budget=1)
    snap1 = pack_row["materials"][:SNAPSHOT_SPLIT]
    snap2 = pack_row["materials"]
    inject(sess, snap1)
    sess.assemble_now("s3_snapshot1_assembly")  # 用掉唯一装配额度
    c1 = sess.engine.final_candidate
    p1_hash = sess.engine.candidate_pack_hashes()[0]
    calls_c1 = assembler_calls(sess)
    assert calls_c1 == 1

    inject(sess, snap2)
    assert sess.engine.candidate_materials_changed(sess.engine.current_node_id)

    before = assembler_calls(sess)
    sess.finalize("s3_user_finish")
    after = assembler_calls(sess)
    c3 = sess.engine.final_candidate
    p3_hash = sess.engine.candidate_pack_hashes()[0]
    ac = c3.assembly_config or {}
    # 批处理回退：不新增模型装配调用；fallback_render=True；原因入记录
    assert after - before == 0, f"S3：额度耗尽不应新增装配调用，实际 {after - before}"
    assert ac.get("fallback_render") is True, "S3：应走原文回退"
    assert ac.get("assembler") == "material_template"
    fin = json.load(open(os.path.join(S3_ROOT, "episodes", sess.eid, "final.json"), encoding="utf-8"))
    assert fin["final_candidate_id"] == c3.candidate_id
    rejects = [e.payload for e in sess.store.of_type("action_rejected")
               if (e.payload or {}).get("reason") == "no_assembly_budget"]
    return {
        "scenario": "S3 online·材料已变·额度耗尽",
        "结束前候选与包": f"{c1.candidate_id} / 快照1包（{p1_hash[:12]}…）",
        "结束时材料是否变化": "是（快照2=全部 19 材料）",
        "最终候选与包": f"{c3.candidate_id} / 回退包 {c3.evidence_pack_ids[0]}（{p3_hash[:12]}…，"
                       f"快照2材料冻结）",
        "实际新增调用数": {"assembler": after - before, "target": 0, "judge": 0,
                          "说明": "快照1装配 1（预算内）；finalize 走原文回退 0 调用"},
        "回退原因": rejects[0]["detail"] if rejects else "?",
        "终止记录": len([e for e in sess.store.of_type("terminate")]),
        "root": S3_ROOT, "episode": sess.eid,
        "最终答案形态": f"material_template 原文回退（{len(c3.answer)} 字符，{len(c3.claims)} 主张）",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    ap.add_argument("--services", default="configs/services.local.yaml")
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()

    from framework.cli import _load_config
    cfg = _load_config(argparse.Namespace(config=args.config, services=args.services,
                                          env_file=args.env_file))
    m, task, pack_row, src_ep = load_r22()
    print(f"R22 样例：{task.task_id} | 包 {pack_row['pack_id']} | {len(pack_row['materials'])} 材料")

    # evidence.jsonl 复制到各回归目录（网页可读，不改正文）
    results = []
    for fn, root in ((scenario_1, S1_ROOT), (scenario_2, S2_ROOT), (scenario_3, S3_ROOT)):
        r = fn(cfg, task, pack_row, src_ep)
        ep_dir = os.path.join(root, "episodes", r["episode"])
        import shutil
        shutil.copyfile(os.path.join(src_ep, "evidence.jsonl"),
                        os.path.join(ep_dir, "evidence.jsonl"))
        results.append(r)
        print(f"[{r['scenario']}] 最终 {r['最终候选与包'][:60]}… 新增装配 {r['实际新增调用数']['assembler']}")

    out = {
        "manifest": MANIFEST,
        "snapshot_note": f"快照从原包材料前缀切分（{SNAPSHOT_SPLIT}/{len(pack_row['materials'])}），"
                         "独立回归目录使用，regression_constructed；原冻结包与历史候选未动",
        "scenarios": results,
    }
    json.dump(out, open("results/r22-finalize-regression.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n三行表：")
    for r in results:
        print(f"  {r['scenario']}：{r['结束前候选与包'][:44]}… → 材料{'变' if '是' in r['结束时材料是否变化'] else '未变'} → "
              f"{r['最终候选与包'][:52]}… 新增装配 {r['实际新增调用数']['assembler']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
