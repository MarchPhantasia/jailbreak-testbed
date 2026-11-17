#!/usr/bin/env python
"""冻结 StrongREJECT held-out 测试集（一次性揭盲用）。

规则（与方法论咨询对齐）：
- 排除：所有历史切片触碰过的 source_row（dev/probe/repr/depth/mainflow1——
  这些题的结果曾用于设计迭代），以及与被排除行 prompt 完全相同的重复行；
- 规模：100 题（总体比例 ±9.8pp 量级的一次性估计）；
- 分配：按排除后剩余题数的类别占比比例分配（最大余数法）；
- 身份：hsr_XXXX 前缀（与开发切片 sr_XXXX 命名空间隔离），按选中行升序编号；
- 冻结即绑定：不用于任何设计迭代；最终配置冻结后一次性揭盲评价；
- U 处理与补评规则随清单冻结（budget-U 允许零 Target 补评，冻结输入不变）。

用法：python scripts/freeze_heldout.py [--n 100] [--seed 20261118]
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import time
from collections import Counter, defaultdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--out-dir", default="data/slices")
    args = ap.parse_args()

    src = "data/raw/strongreject_dataset.csv"
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    src_sha = hashlib.sha256(open(src, "rb").read()).hexdigest()

    # 排除：历史切片触碰的 source_row
    excluded_rows: set[int] = set()
    exclusion_sources: dict[str, list[int]] = {}
    for m in sorted(glob.glob("data/slices/strongreject*.manifest.json")):
        d = json.load(open(m, encoding="utf-8"))
        rs = set()
        for t in (d.get("tasks") or []):
            for key in ("source_row", "source_row_zero_based"):
                if key in t:
                    rs.add(int(t[key]))
                    break
        exclusion_sources[os.path.basename(m)] = sorted(rs)
        excluded_rows |= rs

    # 排除：与被排除行 prompt 完全相同的重复行（近重复组的最小处理）
    excluded_prompts = {rows[i]["forbidden_prompt"].strip().lower() for i in excluded_rows if i < len(rows)}
    dup_excluded: set[int] = set()
    for i, r in enumerate(rows):
        if i in excluded_rows:
            continue
        if r["forbidden_prompt"].strip().lower() in excluded_prompts:
            dup_excluded.add(i)

    pool = [i for i in range(len(rows)) if i not in excluded_rows and i not in dup_excluded]

    # 类别占比比例分配（最大余数法）
    cat_of = {i: rows[i]["category"] for i in pool}
    cat_counts = Counter(cat_of.values())
    base = {}
    remain = []
    for cat, cnt in cat_counts.items():
        exact = args.n * cnt / len(pool)
        base[cat] = int(exact)
        remain.append((exact - int(exact), cat))
    left = args.n - sum(base.values())
    for _, cat in sorted(remain, reverse=True)[:left]:
        base[cat] += 1

    # 每类内按 seed 洗牌后取前 N
    by_cat = defaultdict(list)
    for i in pool:
        by_cat[cat_of[i]].append(i)
    def mix(cat: str) -> str:
        return hashlib.sha256(f"{args.seed}:heldout:{cat}".encode()).hexdigest()
    selected: list[int] = []
    for cat in sorted(cat_counts):
        idx = sorted(by_cat[cat], key=lambda i: hashlib.sha256(f"{mix(cat)}:{i}".encode()).hexdigest())
        selected.extend(idx[: base[cat]])
    selected.sort()

    # 产物
    os.makedirs(args.out_dir, exist_ok=True)
    slice_path = os.path.join(args.out_dir, f"strongreject_dataset.heldout.seed{args.seed}.csv")
    with open(slice_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category", "source", "forbidden_prompt"])
        w.writeheader()
        for i in selected:
            w.writerow({k: rows[i][k] for k in ("category", "source", "forbidden_prompt")})
    slice_sha = hashlib.sha256(open(slice_path, "rb").read()).hexdigest()

    manifest = {
        "filter_version": "heldout-freeze@v1",
        "split": "heldout",
        "frozen_at_unix": time.time(),
        "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "source_path": os.path.abspath(src),
        "source_sha256": src_sha,
        "slice_path": os.path.abspath(slice_path),
        "slice_sha256": slice_sha,
        "seed": args.seed,
        "requested": args.n,
        "selected": len(selected),
        "sampling": "proportional_by_remaining_category",
        "allocation": base,
        "remaining_by_category": dict(cat_counts),
        "exclusion_rule": "历史切片触碰行 + 与其 prompt 完全相同的重复行",
        "exclusion_sources": exclusion_sources,
        "excluded_rows_count": len(excluded_rows),
        "dup_excluded_rows_count": len(dup_excluded),
        "tasks": [
            {
                "task_id": f"hsr_{pos:04d}",
                "source_row": i,
                "category": rows[i]["category"],
                "prompt_sha256": hashlib.sha256(rows[i]["forbidden_prompt"].encode()).hexdigest()[:16],
            }
            for pos, i in enumerate(selected)
        ],
        "binding_rules": {
            "用途": "最终配置一次性揭盲评价；不用于任何设计迭代或调优",
            "揭盲条件": "开发集上的配置冻结后，一次性运行+评价",
            "U处理": "unscored 保留原结果；budget 型 U 允许零 Target 补评（冻结输入不变、新评价 ID）",
            "判据": "揭盲时点的现行冻结判据；judge 模板版本在揭盲时记录",
            "主声明口径": "剩余 StrongREJECT 任务的总体一次运行成功比例（±≈9.8pp）；类别结果以计数为主",
        },
        "note": "冻结后本清单内容不可修改；任何对选择规则的改动需要新 seed 新清单并保留旧件。",
    }
    mp = os.path.join(args.out_dir, f"strongreject_dataset.heldout.seed{args.seed}.manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "slice": slice_path, "manifest": mp, "selected": len(selected),
        "allocation": base, "excluded": len(excluded_rows), "dup_excluded": len(dup_excluded),
        "pool": len(pool), "slice_sha256": slice_sha,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
