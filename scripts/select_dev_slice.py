#!/usr/bin/env python3
"""冻结开发切片（E05）。

用法：
  python scripts/select_dev_slice.py --source /path/strongreject_dataset.csv \
      --revision f7cad6c17e624e21d8df2278e918ae1dddb4cb56 --seed 20261112 --n 30 \
      --out data/slices --url https://github.com/alexandrasouly/strongreject/...

只读取本地固定版本文件，不下载、不改写原始请求；输出切片 CSV 与可复核清单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework.slices import build_dev_slice  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="固定版本的源 CSV")
    parser.add_argument("--revision", required=True, help="来源固定 revision（commit/hash），不接受 pinned")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--n", type=int, required=True, help="目标条数（按重复组原子抽样）")
    parser.add_argument("--out", default="data/slices")
    parser.add_argument("--url", default="", help="来源目录 URL（随清单保存）")
    parser.add_argument("--context-field", default="", help="多上下文数据集（如 HarmBench）的 ContextString 列名")
    args = parser.parse_args()
    if args.revision.strip().lower() in ("", "pinned", "main", "latest"):
        parser.error("--revision 必须是固定 commit/hash，不能用 pinned/main/latest")
    manifest = build_dev_slice(
        args.source,
        out_dir=args.out,
        revision=args.revision,
        seed=args.seed,
        n=args.n,
        source_url=args.url,
        context_field=args.context_field,
    )
    print(json.dumps({k: manifest[k] for k in ("slice_path", "slice_sha256", "selected", "groups_selected", "manifest_path")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
