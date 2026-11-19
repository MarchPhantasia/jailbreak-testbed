"""开发切片冻结（E05）。

原始数据集不进入仓库：研究者在本机保留固定版本文件，这里只生成"选样清单"。

规则：
- 先按（prompt, context）建重复组，再抽组，保证同组不跨切片；
- 抽样只依赖 (seed, group_id)，可复核、可重放；
- 记录来源 URL、固定 revision、源文件 SHA-256、切片 SHA-256、筛选版本、保留行号；
- 切片文件保持原始列，不改写原始请求文本。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from typing import Any, Dict, List

FILTER_VERSION = "dev-slice@v1"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _group_key(prompt: str, context: str) -> str:
    return hashlib.sha256(json.dumps([_normalize(prompt), _normalize(context)], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def load_rows(path: str) -> tuple[List[Dict[str, str]], List[str]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    return rows, fields


def build_dev_slice(
    source_path: str,
    *,
    out_dir: str,
    revision: str,
    seed: int,
    n: int = 0,
    source_url: str = "",
    context_field: str = "",
    split: str = "dev",
    per_category: int = 0,
) -> Dict[str, Any]:
    """冻结开发切片（E05）。

    - n>0 且 per_category=0：全域按重复组原子抽样到 n 条（原行为）；
    - per_category>0：按 category 分层，每类取 per_category 条（类内组原子，
      组哈希序确定于 seed:category:group），n 被忽略。两类模式互斥。
    """
    rows, fields = load_rows(source_path)
    if not rows:
        raise ValueError(f"源数据为空：{source_path}")
    if "forbidden_prompt" not in fields and "Behavior" not in fields:
        raise ValueError(f"未识别的数据列：{fields}（需要 forbidden_prompt 或 Behavior）")
    prompt_field = "forbidden_prompt" if "forbidden_prompt" in fields else "Behavior"

    def _group_of(i: int) -> str:
        return _group_key(rows[i].get(prompt_field, ""), rows[i].get(context_field, "") if context_field else "")

    selected: List[int] = []
    chosen_groups: List[str] = []
    strata: Dict[str, Dict[str, int]] = {}
    if per_category > 0:
        by_cat: Dict[str, List[int]] = {}
        for i, row in enumerate(rows):
            by_cat.setdefault(row.get("category", "") or "(uncategorized)", []).append(i)
        for cat in sorted(by_cat):
            cat_groups: Dict[str, List[int]] = {}
            for i in by_cat[cat]:
                cat_groups.setdefault(_group_of(i), []).append(i)
            ordered = sorted(
                cat_groups.items(),
                key=lambda kv: hashlib.sha256(f"{seed}:{cat}:{kv[0]}".encode("utf-8")).hexdigest(),
            )
            cat_sel: List[int] = []
            for group, indexes in ordered:
                if len(cat_sel) >= per_category:
                    break
                if len(cat_sel) + len(indexes) > per_category:
                    continue  # 类内组原子：装不下就跳过
                cat_sel.extend(indexes)
                chosen_groups.append(group)
            selected.extend(cat_sel)
            strata[cat] = {
                "requested": per_category,
                "selected": len(cat_sel),
                "source_rows": len(by_cat[cat]),
                "groups": len(cat_groups),
            }
        selected.sort()
    else:
        if n <= 0:
            raise ValueError("需要 n>0（全域抽样）或 per_category>0（分层抽样）")
        groups: Dict[str, List[int]] = {}
        for i in range(len(rows)):
            groups.setdefault(_group_of(i), []).append(i)
        ordered = sorted(
            groups.items(),
            key=lambda kv: hashlib.sha256(f"{seed}:{kv[0]}".encode("utf-8")).hexdigest(),
        )
        for group, indexes in ordered:
            if len(selected) >= n:
                break
            if len(selected) + len(indexes) > n:
                continue  # 组原子性：装不下就跳过，保证同组不跨切片
            selected.extend(indexes)
            chosen_groups.append(group)
        selected.sort()

    os.makedirs(out_dir, exist_ok=True)
    name = os.path.basename(source_path)
    slice_path = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.{split}.seed{seed}.csv")
    with open(slice_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i in selected:
            writer.writerow(rows[i])

    ids = [
        {
            "task_id": f"sr_{pos:04d}",
            "source_row": i,
            "group_id": _group_key(rows[i].get(prompt_field, ""), rows[i].get(context_field, "") if context_field else ""),
            "category": rows[i].get("category", ""),
        }
        for pos, i in enumerate(selected)
    ]
    manifest = {
        "filter_version": FILTER_VERSION,
        "split": split,
        "source_url": source_url,
        "revision": revision,
        "source_path": os.path.abspath(source_path),
        "source_sha256": sha256_file(source_path),
        "slice_path": os.path.abspath(slice_path),
        "slice_sha256": sha256_file(slice_path),
        "seed": seed,
        "requested": n if per_category <= 0 else per_category,
        "sampling": "per_category" if per_category > 0 else "global_group_atomic",
        "per_category": per_category,
        "strata": strata,
        "selected": len(selected),
        "source_rows": len(rows),
        "groups_total": (sum(st["groups"] for st in strata.values()) if per_category > 0 else len(groups)),
        "groups_selected": len(chosen_groups),
        "prompt_field": prompt_field,
        "tasks": ids,
        "categories": sorted({x["category"] for x in ids if x["category"]}),
        "note": "切片仅供开发/校准；若要报告 heldout，需在完整来源剔除本切片及其重复组后再另建清单。",
    }
    manifest_path = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.{split}.seed{seed}.manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    return manifest
