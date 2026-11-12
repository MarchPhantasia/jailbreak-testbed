#!/usr/bin/env python3
"""导出一次运行的逐题结果与汇总（E05）。

用法：
  python scripts/report_run.py --root results/<experiment_id> [--csv results/<id>/per_episode.csv]

只读取已有产物，不调用任何模型、不改写在线的 events 与候选。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="results/<experiment_id> 目录")
    parser.add_argument("--csv", default="", help="额外写出逐题 CSV 的路径")
    args = parser.parse_args()
    report = runner.report_run(args.root)
    if args.csv:
        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["episode_id", "task_id", "outcome", "basis", "verdict_id", "class", "judge_ref", "fallback_render"],
            )
            writer.writeheader()
            for row in report["per_episode"]:
                writer.writerow({k: row.get(k) for k in writer.fieldnames})
        report["csv"] = str(path)
    print(json.dumps({k: report[k] for k in ("experiment_id", "episodes", "eval_run_id", "metrics", "calls_by_role", "calls_by_stage")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
