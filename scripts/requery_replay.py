#!/usr/bin/env python
"""requery-policy@v1 历史重放：7 次显式拒答点上的决策对照（0 模型调用）。

在每个 target_refusal 事件处重建当时状态，运行 decide()，把指令的
(模板, 组件, 语境族) 与历史上实际发生的下一次提问对照。
"""
from __future__ import annotations
import glob, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from framework.requery_policy import (classify_context_family, decide,
                                      reconstruct_state)

ROOTS = ["results/mainflow7_sr_batch1_20261117_r1",
         "results/mainflow8_sr_batch2_20261117_r1"]


def main() -> int:
    rows = []
    for root in ROOTS:
        for ep_dir in sorted(glob.glob(f"{root}/episodes/ep_*")):
            evs = [json.loads(l) for l in open(f"{ep_dir}/events.jsonl", encoding="utf-8")]
            for i, e in enumerate(evs):
                if e.get("type") != "target_refusal":
                    continue
                task = f"{root.split('_')[1]}/{ep_dir.split('/')[-1][:10]}"
                st = reconstruct_state(evs, up_to_event=i + 1)
                d = decide(st)
                # 历史上实际的下一次 target 提问
                later = [x for x in evs[i + 1:] if x.get("type") == "request_sent"
                         and x.get("role") == "target"]
                if later:
                    hist_q = ((later[0].get("payload") or {}).get("messages")
                              or [{}])[-1].get("content", "")
                    hist = {"family": classify_context_family(hist_q),
                            "query_head": hist_q[:60]}
                else:
                    hist = {"family": "(无后续提问)", "query_head": ""}
                pr = st.pending_refusal or {}
                rows.append({
                    "task": task, "refusal_event": i,
                    "refusal_type": pr.get("type"), "component": pr.get("component"),
                    "policy": {"action": d.action, "template": d.template_id,
                               "component": d.component, "family": d.context_family,
                               "rule": d.rule},
                    "historical_next": hist,
                })
    out = "results/requery-replay-20261122.json"
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{'任务':22s} {'型':3s} {'组件':4s} | 策略指令 → (模板/组件/族) | 历史下一问(族)")
    agree = 0
    for r in rows:
        p, h = r["policy"], r["historical_next"]
        same = (p["family"] == h["family"]) or (p["action"] != "ask" and h["family"].startswith("(无"))
        agree += same
        print(f"{r['task']:22s} {r['refusal_type'] or '?':3s} {r['component'] or '?':4s} | "
              f"{p['action']:8s} {p['template'] or '-':22s} {p['component'] or '-':4s} "
              f"{p['family']:16s} | {h['family']:18s} {h['query_head'][:40]}")
    print(f"\n重放 {len(rows)} 个拒答点；策略-历史一致（同族或同弃） {agree} 例；"
          f"分歧 {len(rows) - agree} 例（设计性分歧见交付文档）")
    print(f"明细：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
