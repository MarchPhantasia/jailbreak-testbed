"""CLI 冒烟（手册 §17）：命令是否调用模型、产物是否落到约定目录。"""

from __future__ import annotations

import json
import os

import yaml

from framework import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "synthetic_library", "tasks.jsonl")


def write_cfg(tmp_path):
    cfg = {
        "experiment": {
            "id": "cli_exp",
            "seed": 1,
            "session_mode": "stateless",
            "results_dir": str(tmp_path / "results"),
            "max_steps": 6,
            "budget": {"target_calls": 4, "non_target_calls": 40},
        },
        "datasets": {
            "adapter": "synthetic_library",
            "path": FIXTURE,
            "split": "dev",
            "dev_ratio": 1,
            "split_seed": 1,
            "manifests_dir": str(tmp_path / "manifests"),
        },
        "method": {
            "name": "independent@v0.1",
            "controller": {"type": "element"},
            "state": {"backend": "fox", "max_chars": 300},
            "assembly": {"k": 1, "selector": "coverage"},
            "search": {"policy": "linear", "scorer": "rule"},
            "judge": {"rubric_id": "inline", "version": "v1"},
        },
        "target": {"type": "mock"},
    }
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_cli_pipeline(tmp_path, capsys):
    cfg_path = write_cfg(tmp_path)
    assert cli.main(["prepare-data", "--config", cfg_path]) == 0
    assert cli.main(["check-data", "--config", cfg_path]) == 0
    assert cli.main(["check-services", "--config", cfg_path]) == 0
    assert cli.main(["run", "--config", cfg_path]) == 0
    assert cli.main(["evaluate", "--config", cfg_path]) == 0
    root = tmp_path / "results" / "cli_exp"
    eps = sorted(os.listdir(root / "episodes"))
    assert len(eps) == 3
    assert cli.main(["restore", "--episode-dir", str(root / "episodes" / eps[0])]) == 0
    assert cli.main(["audit", "--episode-dir", str(root / "episodes" / eps[0])]) == 0
    assert cli.main(["analyze", "--episode-dir", str(root / "episodes" / eps[0])]) == 0
    metrics_files = list((root / "evaluations").glob("*/metrics.json"))
    assert metrics_files
    m = json.loads(metrics_files[0].read_text(encoding="utf-8"))
    assert m["N"] == 3
