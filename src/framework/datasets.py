"""数据集适配与清单（手册 §14）。

- 只读固定版本；保存原始文件 SHA-256 与来源定位；
- 规范化只用于比较，不改写原始文本；
- 精确重复按（prompt, context）合并并建立重复组；
- 划分按 hash 稳定分配，开发池不足时明确报错，不静默借用测试。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .contracts import DatasetManifest, Task, stable_hash


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


NORM_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return NORM_RE.sub(" ", (text or "").strip().lower())


@dataclass
class LoadResult:
    tasks: List[Task]
    excluded: List[Dict[str, str]] = field(default_factory=list)
    raw_sha256: str = ""
    source_url: str = ""
    revision: str = ""


class DatasetAdapter:
    name = "base"

    def load(self, path: str, **kwargs) -> LoadResult:  # pragma: no cover - 抽象
        raise NotImplementedError


class SyntheticLibraryAdapter(DatasetAdapter):
    """fixtures 用的合成任务（图书馆资料整理形态），字段最简。"""

    name = "synthetic_library"

    def load(self, path: str, **kwargs) -> LoadResult:
        tasks: List[Task] = []
        excluded: List[Dict[str, str]] = []
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if not d.get("prompt"):
                    excluded.append({"row": str(i), "reason": "missing_prompt"})
                    continue
                tasks.append(
                    Task(
                        task_id=d["task_id"],
                        prompt=d["prompt"],
                        primary_dataset="synthetic_library",
                        necessary_context=d.get("context", ""),
                        category=d.get("category", "synthetic"),
                        upstream_id=str(d.get("upstream_id", "")),
                        source_locator=f"fixtures:{os.path.basename(path)}:{i}",
                        dataset_revision="v1",
                        profile="dev",
                        eval_materials=d.get("eval_materials", {}),
                        runtime_hints={
                            "elements": d.get("elements", []),
                            "target_scripts": d.get("target_scripts", {}),
                        },
                        source_license="CC0-1.0",
                    )
                )
        return LoadResult(tasks, excluded, sha256_file(path), path, "v1")


class StrongRejectAdapter(DatasetAdapter):
    """StrongREJECT：优先 jsonl；兼容 csv（forbidden_prompt/category）。"""

    name = "strongreject"

    def load(self, path: str, **kwargs) -> LoadResult:
        tasks: List[Task] = []
        excluded: List[Dict[str, str]] = []
        revision = kwargs.get("revision", "pinned")
        if path.endswith(".jsonl"):
            with open(path, "r", encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        elif path.endswith(".json"):
            raw = json.load(open(path, "r", encoding="utf-8"))
            rows = raw if isinstance(raw, list) else raw.get("data", [])
        else:
            with open(path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        for i, r in enumerate(rows):
            prompt = r.get("forbidden_prompt") or r.get("prompt") or r.get("behavior") or ""
            if not prompt:
                excluded.append({"row": str(i), "reason": "missing_prompt"})
                continue
            tasks.append(
                Task(
                    task_id=f"sr_{i:04d}",
                    prompt=prompt,
                    primary_dataset="strongreject",
                    category=r.get("category", ""),
                    upstream_id=str(r.get("id", i)),
                    source_locator=f"{os.path.basename(path)}:{i}",
                    dataset_revision=revision,
                    eval_materials={"native_score": r.get("score")} if r.get("score") is not None else {},
                )
            )
        return LoadResult(tasks, excluded, sha256_file(path), path, revision)


class HarmBenchAdapter(DatasetAdapter):
    """HarmBench：csv，保留 Behavior/ContextString/SemanticCategory/BehaviorID。"""

    name = "harmbench"

    def load(self, path: str, **kwargs) -> LoadResult:
        tasks: List[Task] = []
        excluded: List[Dict[str, str]] = []
        revision = kwargs.get("revision", "pinned")
        with open(path, "r", encoding="utf-8", newline="") as f:
            for i, r in enumerate(csv.DictReader(f)):
                behavior = r.get("Behavior") or r.get("behavior") or ""
                if not behavior:
                    excluded.append({"row": str(i), "reason": "missing_behavior"})
                    continue
                tasks.append(
                    Task(
                        task_id=f"hb_{r.get('BehaviorID') or i}",
                        prompt=behavior,
                        primary_dataset="harmbench",
                        necessary_context=r.get("ContextString", "") or "",
                        category=r.get("SemanticCategory", "") or "",
                        upstream_id=str(r.get("BehaviorID", i)),
                        source_locator=f"{os.path.basename(path)}:{i}",
                        dataset_revision=revision,
                        eval_materials={"functional_category": r.get("FunctionalCategory", "")},
                    )
                )
        return LoadResult(tasks, excluded, sha256_file(path), path, revision)


ADAPTERS = {
    "synthetic_library": SyntheticLibraryAdapter(),
    "strongreject": StrongRejectAdapter(),
    "harmbench": HarmBenchAdapter(),
}


def get_adapter(name: str) -> DatasetAdapter:
    if name not in ADAPTERS:
        raise ValueError(f"未知数据集适配器：{name}")
    return ADAPTERS[name]


def assign_groups(tasks: List[Task]) -> Dict[str, List[str]]:
    """精确重复（prompt+context 相同）合并为同一重复组。"""
    buckets: Dict[str, List[str]] = {}
    for t in tasks:
        key = stable_hash([normalize(t.prompt), normalize(t.necessary_context)])[:12]
        t.duplicate_group_id = f"dup_{key}"
        buckets.setdefault(t.duplicate_group_id, []).append(t.task_id)
    return buckets


def split_tasks(tasks: List[Task], *, seed: int = 20261110, dev_ratio: int = 2) -> Tuple[List[Task], List[Task]]:
    """按重复组稳定划分：同组不跨集合。dev_ratio=2 表示每 2 组取 1 组进开发池。"""
    groups: Dict[str, List[Task]] = {}
    for t in tasks:
        groups.setdefault(t.duplicate_group_id or t.task_id, []).append(t)
    dev, held = [], []
    for gid in sorted(groups):
        h = int(hashlib.sha256(f"{seed}:{gid}".encode()).hexdigest()[:8], 16)
        target = dev if h % max(1, dev_ratio) == 0 else held
        for t in groups[gid]:
            t.split = "dev" if target is dev else "heldout"
            target.append(t)
    return dev, held


def build_manifest(result: LoadResult, tasks: List[Task], *, seed: int = 20261110, adapter_version: str = "1") -> DatasetManifest:
    groups = assign_groups(tasks)
    return DatasetManifest(
        source_url=result.source_url,
        revision=result.revision,
        raw_sha256=result.raw_sha256,
        adapter_version=adapter_version,
        filter_version="text_holdout_v1",
        split_seed=seed,
        tasks=[t.task_id for t in tasks],
        duplicate_groups=groups,
        included=len(tasks),
        excluded=result.excluded,
        source_license=tasks[0].source_license if tasks else "",
    )


def load_split(adapter_name: str, path: str, *, seed: int = 20261110, dev_ratio: int = 2, **kwargs) -> Tuple[LoadResult, List[Task], List[Task], DatasetManifest]:
    adapter = get_adapter(adapter_name)
    result = adapter.load(path, **kwargs)
    dev, held = split_tasks(result.tasks, seed=seed, dev_ratio=dev_ratio)
    manifest = build_manifest(result, result.tasks, seed=seed)
    return result, dev, held, manifest


def write_json(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_tasks_jsonl(tasks: Iterable[Task], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t.__dict__, ensure_ascii=False) + "\n")
