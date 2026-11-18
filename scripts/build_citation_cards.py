#!/usr/bin/env python
"""引用审计抽样与中文审计卡生成（第一阶段交付的第二部分）。

预注册抽样规则（评审前记录，评审时隐藏分组）：
- 总量目标 25 项（区间 20-30）；
- 分层：5 个"疑例案例"各抽 ≤3 条主张；5 个"对照案例"各抽 ≤2 条主张；
- 案例内按 seed 洗牌取前 N；
- 审计卡不显示疑例/对照标签，盲编号 C01.. 乱序；
- 每卡：中文主张 + 被引证据（中文并排，含"不在证据清单内"的引用）+ Q1/Q2/Q3；
- Q3 检索范围 = 对应评审卡的证据清单（原判定所见集合）；完整包差异已存档，
  需要检索被裁剪部分时标"需原文复核"。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re


AUDIT_TMPL = """# 引用审计卡 {cid}

> 声明级审计条目（非 23 卡总体抽样；错误比例不能外推为全部候选的错误率）。
> Q3 的检索范围 = 评审卡 {rid} 第三部分的证据清单；如需检索该清单之外的部分，勾"需原文复核"。

## 主张（中文译文）

{claim_cn}

## 被引证据（中文译文，与主张并排）

{cited}

## 我的回答

- **Q1** 被引材料能否定位、是否非空、能否核验？ → **____**
- **Q2** 这段材料是否支持主张中的对象、数值、单位、条件和含义？ → **____**
- **Q3** 若 Q2 为不支持：该案例当时可用的其他材料里是否另有依据？如有，记下证据编号。 → **____**
- 备注 / 需原文复核：**____**
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", default="results/citation-audit-citaudit_706cdf096475/packet.json")
    ap.add_argument("--blind-dir", required=True, help="build_blind_review 输出目录")
    ap.add_argument("--claims-cn", required=True, help="抽样主张的中文译文 JSON")
    ap.add_argument("--seed", type=int, default=20261120)
    ap.add_argument("--max-suspect", type=int, default=3)
    ap.add_argument("--max-control", type=int, default=2)
    args = ap.parse_args()

    pkt = json.load(open(args.packet, encoding="utf-8"))
    claims_cn = json.load(open(args.claims_cn, encoding="utf-8"))  # {claim_id: 译文}
    ident = json.load(open(os.path.join(args.blind_dir, "internal", "identity_map.json"), encoding="utf-8"))
    # ref（task@batch）→ 盲审编号 + 题内映射
    rid_by_ref = {r["ref"]: r["盲审编号"] for r in ident}
    evmap_by_ref = {r["ref"]: r["evidence_local_to_ev"] for r in ident}  # local→ev

    rng = random.Random(args.seed)
    # 案例分组（内部）：batch_label+task_id → items
    cases = {}
    for it in pkt["items"]:
        key = (it["_internal"]["batch_label"], it["_internal"]["task_id"])
        cases.setdefault(key, []).append(it)
    # 疑例/对照集合（来自 packet 生成时的内部口径，此处仅用于配额）
    SUS = {("批次A", "sr_0002"), ("批次B1", "sr_0000"), ("批次B1", "sr_0001"),
           ("批次B2", "sr_0008"), ("批次B2", "sr_0011")}

    sampled = []
    rule_log = []
    for key, items in sorted(cases.items()):
        quota = args.max_suspect if key in SUS else args.max_control
        pool = items[:]
        rng.shuffle(pool)
        take = pool[:quota]
        rule_log.append({"case": f"{key[0]}:{key[1]}", "stratum": "suspect" if key in SUS else "control",
                         "claims_total": len(items), "sampled": len(take)})
        sampled.extend(take)
    rng.shuffle(sampled)

    os.makedirs(os.path.join(args.blind_dir, "citation"), exist_ok=True)
    evmap_rev = {}  # ev → local（按 ref）
    for r in ident:
        evmap_rev[r["ref"]] = {v: k for k, v in r["evidence_local_to_ev"].items()}

    cards = []
    for i, it in enumerate(sampled):
        cid = f"C{i+1:02d}"
        ref = f"{it['_internal']['task_id']}@" + {"批次A": "b1", "批次B1": "b2r1", "批次B2": "b2r2"}[it["_internal"]["batch_label"]]
        rid = rid_by_ref[ref]
        ev2local = evmap_rev[ref]
        cited_ids = [ce["evidence_id"] for ce in it["cited_evidence"]]
        # 主张译文（占位替换 ev_ 引用为题内编号）
        claim_text = it["claim"]
        claim_cn = claims_cn.get(it["_internal"]["claim_id"]) or claim_text
        for eid in cited_ids:
            if eid in ev2local:
                claim_cn = claim_cn.replace(eid, ev2local[eid])
        # 被引证据渲染
        lines = []
        for ce in it["cited_evidence"]:
            loc = ev2local.get(ce["evidence_id"])
            cn = claims_cn.get(f"ev:{ce['evidence_id']}") or ce["text"]
            lines.append(f"**{loc or ce['evidence_id']}**：\n\n{cn}\n")
        for mid in it.get("missing_cited_ids") or []:
            lines.append(f"**{mid}**：〔该引用编号不在本案例可用证据清单内〕\n")
        with open(os.path.join(args.blind_dir, "citation", f"{cid}.md"), "w", encoding="utf-8") as f:
            f.write(AUDIT_TMPL.format(cid=cid, rid=rid, claim_cn=claim_cn, cited="\n".join(lines)))
        cards.append({"cid": cid, "rid": rid, "claim_id": it["_internal"]["claim_id"]})

    json.dump({"seed": args.seed, "rule": {
        "总量": "疑例案例 ≤3 条 + 对照案例 ≤2 条（分层配额，案例内 seed 洗牌取前 N）",
        "目标区间": "20-30 项", "盲化": "审计卡隐藏疑例/对照标签与批次信息",
        "Q3范围": "对应评审卡的证据清单（原判定所见）；完整包差异另档",
    }, "allocation": rule_log, "cards": cards},
        open(os.path.join(args.blind_dir, "internal", "citation_sampling.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)
    print(json.dumps({"audit_cards": len(cards), "by_case": {r['case']: r['sampled'] for r in rule_log}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
