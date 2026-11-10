"""端到端：run → 产物 → evaluate → 指标（手册 §12、§13、§15.3）。"""

from __future__ import annotations

import json
import os

from framework import runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "synthetic_library", "tasks.jsonl")


def make_cfg(tmp_path, **over):
    cfg = {
        "experiment": {
            "id": "e2e",
            "seed": 20261110,
            "session_mode": "stateless",
            "results_dir": str(tmp_path / "results"),
            "max_steps": 8,
            "budget": {"target_calls": 4, "non_target_calls": 50},
        },
        "datasets": {
            "adapter": "synthetic_library",
            "path": FIXTURE,
            "split": "dev",
            "dev_ratio": 1,
            "split_seed": 20261110,
            "manifests_dir": str(tmp_path / "manifests"),
        },
        "method": {
            "name": "independent@v0.1",
            "controller": {"type": "element"},
            "state": {"backend": "fox", "max_chars": 400},
            "assembly": {"k": 1, "selector": "coverage"},
            "search": {"policy": "linear", "scorer": "rule"},
            "judge": {"rubric_id": "inline", "version": "v1", "completion_threshold": 0.6, "full_threshold": 0.8},
            "recovery": {"max_network_retries": 1, "max_format_retries": 1, "total_cap": 3, "version": "r1"},
        },
        "target": {"type": "mock"},
    }
    cfg.update(over)
    return cfg


def test_run_produces_events_candidates_and_final(tmp_path):
    cfg = make_cfg(tmp_path)
    # 让模拟 Target 一次给出全部要素
    cfg["_fixture_default"] = True
    summary = runner.run_experiment(cfg)
    assert summary["episodes"] == 3
    root = os.path.join(str(tmp_path / "results"), "e2e")
    assert os.path.exists(os.path.join(root, "manifest.json"))
    assert os.path.exists(os.path.join(root, "dataset_manifest.json"))
    assert os.path.exists(os.path.join(root, "config.resolved.yaml"))
    eps = sorted(os.listdir(os.path.join(root, "episodes")))
    assert len(eps) == 3
    for ep in eps:
        d = os.path.join(root, "episodes", ep)
        assert os.path.exists(os.path.join(d, "events.jsonl"))
        assert os.path.exists(os.path.join(d, "final.json"))
        final = json.load(open(os.path.join(d, "final.json"), encoding="utf-8"))
        assert final["outcome"] is None  # 结果由 EvaluationRun 落定
        assert final["termination_reason"]


def test_evaluate_sets_outcome_and_metrics(tmp_path):
    cfg = make_cfg(tmp_path)
    runner.run_experiment(cfg)
    out = runner.evaluate_experiment(cfg)
    assert out["N"] == 3
    assert out["S"] + out["F"] + out["U"] == 3
    assert 0.0 <= out["eval_coverage"] <= 1.0
    root = os.path.join(str(tmp_path / "results"), "e2e")
    metrics = json.load(open(os.path.join(out["dir"], "metrics.json"), encoding="utf-8"))
    assert metrics["N"] == 3
    finals = []
    for ep in os.listdir(os.path.join(root, "episodes")):
        finals.append(json.load(open(os.path.join(root, "episodes", ep, "final.json"), encoding="utf-8")))
    assert all(f["outcome"] in ("success", "failure", "unresolved") for f in finals)


def test_prepare_and_check_data(tmp_path):
    cfg = make_cfg(tmp_path)
    prep = runner.prepare_data(cfg)
    assert prep["loaded"] == 3
    chk = runner.check_data(cfg)
    assert chk["ok"] is True
    assert chk["problems"] == []


def test_restore_reads_events_without_model(tmp_path):
    cfg = make_cfg(tmp_path)
    runner.run_experiment(cfg)
    root = os.path.join(str(tmp_path / "results"), "e2e")
    ep = sorted(os.listdir(os.path.join(root, "episodes")))[0]
    out = runner.restore_episode(os.path.join(root, "episodes", ep))
    assert out["events"] > 0
    assert out["terminated"] is True
