"""数据集适配、去重与划分（手册 §14、§18.1）。"""

from __future__ import annotations

import json
import os

from framework.datasets import assign_groups, build_manifest, load_split
from framework.contracts import Task  # noqa: F401

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "synthetic_library", "tasks.jsonl")


def test_fixture_loads_with_elements_and_scripts():
    result, dev, held, manifest = load_split("synthetic_library", FIXTURE, seed=7, dev_ratio=1)
    assert len(result.tasks) == 3
    t = result.tasks[0]
    assert t.public_view()["prompt"]
    assert t.runtime_hints["elements"] == ["库位", "登记", "开放时间"]
    assert t.runtime_hints["target_scripts"]
    assert t.eval_materials == {} or isinstance(t.eval_materials, dict)


def test_split_keeps_duplicate_groups_together():
    _, dev, held, manifest = load_split("synthetic_library", FIXTURE, seed=7, dev_ratio=2)
    dev_groups = {t.duplicate_group_id for t in dev}
    held_groups = {t.duplicate_group_id for t in held}
    assert dev_groups.isdisjoint(held_groups)
    assert len(dev) + len(held) == 3


def test_exact_duplicates_share_group(tmp_path):
    path = tmp_path / "dup.jsonl"
    rows = [
        {"task_id": "a", "prompt": "同一个请求", "context": "同一背景"},
        {"task_id": "b", "prompt": "同一个请求", "context": "同一背景"},
        {"task_id": "c", "prompt": "另一个请求", "context": ""},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    result, dev, held, manifest = load_split("synthetic_library", str(path), seed=7, dev_ratio=1)
    groups = assign_groups(result.tasks)
    assert len(groups) == 2
    dup_group = next(g for g, ids in groups.items() if len(ids) == 2)
    assert set(groups[dup_group]) == {"a", "b"}


def test_manifest_records_hash_and_counts():
    result, dev, held, manifest = load_split("synthetic_library", FIXTURE, seed=7, dev_ratio=1)
    assert len(manifest.raw_sha256) == 64
    assert manifest.included == 3
    assert set(manifest.tasks) == {"syn_001", "syn_002", "syn_003"}
    assert manifest.excluded == []
