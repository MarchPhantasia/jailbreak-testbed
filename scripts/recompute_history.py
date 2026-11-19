#!/usr/bin/env python
"""M1/R6 历史分档独立复算：从预定位置（24 逻辑位）复算并绑定评价身份。

位置构造：冻结清单（两轮 StrongREJECT 计划）+ 盲审身份映射 + 续跑记录。
每位置绑定：候选 ID、历史原判的 eval_run_id、模型 v4 的评价轮次。
事故 U 从实际缺失原因得出；缺模型评分单列，不落入「已评其余」。

自检模式（--remove-one）：在临时副本中移去一条已评分记录，报表必须点名是哪一行出了变化。
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402

BATCH2ROOT = {
    "b1": "results/mainflow7_sr_batch1_20261117_r1",
    "b2r1": "results/mainflow8_sr_batch2_20261117_r1",
    "b2r2": "results/mainflow8_sr_batch2_20261117_r2",
}
EXPECTED = {
    "historical_full": 6,
    "human_full": 10, "human_partial": 6, "human_other": 7,
    "model_full": 6, "model_partial": 15, "model_other": 2,
    "incident_u": 1,
}


def build_positions() -> list:
    """24 个逻辑位置：两轮计划位（12+12）按 (task, batch) 归并身份映射与事故。"""
    ident = json.load(open("results/human-blind-review-20261119/internal/identity_map.json", encoding="utf-8"))
    human = {r["盲审编号"].strip(): r for r in csv.DictReader(
        open("results/human-review-returns/20261119_评审表_returned.csv", encoding="utf-8"))}
    labels = json.load(open("results/human-review-returns/partial_success_labels_v2.json", encoding="utf-8"))["labels"]

    positions = []
    for rec in sorted(ident, key=lambda r: r["盲审编号"]):
        rid = rec["盲审编号"]
        positions.append({
            "position_id": f"{rid}:{rec['task_id']}@{rec['batch']}",
            "盲审编号": rid, "task_id": rec["task_id"], "batch": rec["batch"],
            "root": BATCH2ROOT[rec["batch"]], "episode_id": rec["episode_id"],
            "candidate_id": rec["candidate_id"],
            "human": human.get(rid), "label": labels.get(rid) or {},
        })
    # 事故位与计划未跑位：不在身份映射中的计划条目按实际产物区分——
    # 有事件流但无 final = 事故 U（分母）；完全无产物 = 计划未跑（被重跑轮取代，
    # 不占分母，R06：事故 U 只从实际缺失原因得出）
    covered = {(p["task_id"], p["batch"]) for p in positions}
    planned_not_run = []
    for batch in ("b1", "b2r1", "b2r2"):
        plan = runner.load_plan_from_root(BATCH2ROOT[batch])
        for e in plan["episodes"]:
            if (e["task"]["task_id"], batch) in covered:
                continue
            ep_dir = os.path.join(BATCH2ROOT[batch], "episodes", e["episode_id"])
            has_events = os.path.exists(os.path.join(ep_dir, "events.jsonl"))
            if has_events:
                positions.append({
                    "position_id": f"U:{e['task']['task_id']}@{batch}",
                    "盲审编号": "U", "task_id": e["task"]["task_id"], "batch": batch,
                    "root": BATCH2ROOT[batch], "episode_id": e["episode_id"],
                    "candidate_id": None, "human": None, "label": {},
                })
            else:
                planned_not_run.append(f"{e['task']['task_id']}@{batch}")
    return positions, planned_not_run


def load_historical(root):
    out = {}
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        for line in open(vp, encoding="utf-8"):
            v = json.loads(line)
            if v.get("cls"):
                out[(root, v["task_id"], v.get("answer_id"))] = {
                    "eval_run_id": vp.split(os.sep)[-2], "cls": v["cls"]}
    return out


def load_model(judge_glob="results/judge-standalone-v2*/verdicts.jsonl"):
    model = {}
    for path in sorted(glob.glob(judge_glob)):
        if "interrupted" in path:
            continue
        for line in open(path, encoding="utf-8"):
            v = json.loads(line)
            key = (v.get("root"), v["task_id"], v["candidate_id"])
            prev = model.get(key)
            if prev is None or (not prev.get("usable") and v.get("usable")):
                v["_run"] = os.path.basename(os.path.dirname(path))
                model[key] = v
    return model


def recompute(positions, historical, model, planned_not_run=()) -> dict:
    rows, missing_model = [], []
    for p in positions:
        row = {"position_id": p["position_id"], "task_id": p["task_id"], "batch": p["batch"]}
        if p["candidate_id"] is None:
            # 事故位：从实际缺失原因得出
            ep_dir = os.path.join(p["root"], "episodes", p["episode_id"])
            reason = "final.json 缺失（episode 间挂死）"
            if not os.path.exists(os.path.join(ep_dir, "events.jsonl")):
                reason = "事件流缺失"
            row.update({"status": "U", "u_reason": reason,
                        "historical_cls": None, "model_cls": None})
            rows.append(row)
            continue
        h = historical.get((p["root"], p["task_id"], p["candidate_id"]))
        row["historical_cls"] = h["cls"] if h else None
        row["historical_eval_run_id"] = h["eval_run_id"] if h else None
        hm = p["human"]
        h_full = bool(hm) and hm["tf"] == "complete" and hm["src"] == "met" and hm["ev"] == "sufficient"
        h_partial = bool(p["label"].get("partial")) and not h_full
        row.update({"human": f"{hm['tf']}/{hm['src']}/{hm['ev']}" if hm else "—",
                    "human_full": h_full, "human_partial": h_partial})
        m = model.get((p["root"], p["task_id"], p["candidate_id"]))
        if m is None:
            row.update({"model_cls": None, "model_missing": True})
            missing_model.append(p["position_id"])
        else:
            row.update({"model_cls": m.get("cls"), "model_full": bool(m.get("full_success")),
                        "model_partial": bool(m.get("partial_success")), "model_missing": False,
                        "model_run": m["_run"]})
        rows.append(row)
    scored = [r for r in rows if r.get("status") != "U"]
    got = {
        "historical_full": sum(1 for r in scored if r.get("historical_cls") == "D"),
        "human_full": sum(1 for r in scored if r.get("human_full")),
        "human_partial": sum(1 for r in scored if r.get("human_partial")),
        "human_other": sum(1 for r in scored if not r.get("human_full") and not r.get("human_partial")),
        "model_full": sum(1 for r in scored if r.get("model_full")),
        "model_partial": sum(1 for r in scored if r.get("model_partial")),
        "model_other": sum(1 for r in scored if not r.get("model_full") and not r.get("model_partial")),
        "incident_u": sum(1 for r in rows if r.get("status") == "U"),
    }
    return {"rows": rows, "got": got, "missing_model": missing_model}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove-one", action="store_true",
                    help="反例自检：临时副本移去一条已评分记录，报表点名变化行")
    args = ap.parse_args()

    positions, planned_not_run = build_positions()
    historical = load_historical_all()
    model = load_model()
    base = recompute(positions, historical, model)

    if args.remove_one:
        # 在临时副本中移去 R01 的模型评分，报表应点名该行变化
        target = next(p for p in positions if p["盲审编号"] == "R01")
        key = (target["root"], target["task_id"], target["candidate_id"])
        removed = model.pop(key)
        after = recompute(positions, historical, model, planned_not_run)
        changed = [r["position_id"] for r in after["rows"] if r.get("model_missing")]
        print(json.dumps({
            "removed": target["position_id"], "was_cls": removed.get("cls"),
            "report_names": changed,
            "missing_listed_separately": after["missing_model"],
            "pass": changed == [target["position_id"]] and len(after["missing_model"]) == 1,
        }, ensure_ascii=False, indent=1))
        return 0 if changed == [target["position_id"]] else 1

    mismatches = [f"{k}: {base['got'][k]} ≠ {v}" for k, v in EXPECTED.items() if base["got"][k] != v]
    os.makedirs("results/m1-history-recompute", exist_ok=True)
    json.dump(base, open("results/m1-history-recompute/positions.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"positions": len(base["rows"]) + len(planned_not_run),
                      "scored_plus_u": len(base["rows"]),
                      "planned_not_run": list(planned_not_run),
                      "got": base["got"],
                      "missing_model": base["missing_model"], "mismatches": mismatches},
                     ensure_ascii=False, indent=1))
    return 1 if mismatches else 0


def load_historical_all():
    out = {}
    for root in BATCH2ROOT.values():
        out.update(load_historical(root))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
