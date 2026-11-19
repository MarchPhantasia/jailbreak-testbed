"""Candidate identity across independent evaluation, resume and the review page."""
import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml

from framework import standalone, webapp
from framework.cli import main
from test_execution1_r2 import FakeService, routed_cfg
from test_standalone_judge import _write_run_root


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture
def judge_run(tmp_path, monkeypatch):
    root = _write_run_root(tmp_path / "results" / "archive")
    cfg = routed_cfg(tmp_path)
    fake = FakeService()
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-test")
    monkeypatch.setattr("urllib.request.urlopen", fake)
    rubric = tmp_path / "rubric.json"
    write_json(rubric, standalone.DEFAULT_RUBRIC)
    kwargs = dict(roots=[str(root)], rubric_path=str(rubric),
                  prompt_path=str(Path(__file__).resolve().parents[1] / "prompts/judge_eval.v2.txt"),
                  out_dir=str(tmp_path / "results" / "custom-evaluation"))
    return cfg, kwargs, fake


def test_regrade_preserves_first_run_and_resumes_by_input(judge_run):
    cfg, kwargs, fake = judge_run
    first = standalone.run(cfg, **kwargs)
    assert first["rows"][0]["judge_evidence_ids"] == ["ev_1"]
    root = Path(first["out_dir"])
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    kwargs["resume_from"] = [str(root)]
    skipped = standalone.run(cfg, **kwargs)
    assert not skipped["calls_model"] and skipped["summary"]["skipped"] == 1
    assert len(fake.bodies) == 1

    rubric = Path(kwargs["rubric_path"])
    write_json(rubric, {**read_json(rubric), "objective": "检查登记条件"})
    second = standalone.run(cfg, **kwargs)
    assert second["eval_run_id"] != first["eval_run_id"]
    assert Path(second["out_dir"]) == root / "runs" / second["eval_run_id"]
    for name in ("summary.json", "provenance.json", "verdicts.jsonl"):
        assert read_json(Path(second["out_dir"]) / name)["eval_run_id"] == second["eval_run_id"]
    assert all(p.read_bytes() == original for p, original in snapshot.items())
    assert standalone.run(cfg, **kwargs)["summary"]["skipped"] == 1

    candidate = Path(kwargs["roots"][0]) / "episodes/ep_1/candidates.jsonl"
    write_json(candidate, {**read_json(candidate), "answer": "更正登记条件"})
    assert standalone.run(cfg, **kwargs)["calls_model"]
    cfg["services"]["general"]["model"] = "another-judge"
    assert standalone.run(cfg, **kwargs)["calls_model"]
    assert len(fake.bodies) == 4


def test_duplicate_task_reads_its_own_episode(tmp_path):
    root = _write_run_root(tmp_path / "archive")
    plan = read_json(root / "plan.json")
    plan["episodes"].append({**plan["episodes"][0], "episode_id": "ep_2"})
    write_json(root / "plan.json", plan)
    ep = root / "episodes/ep_2"
    shutil.copytree(root / "episodes/ep_1", ep)
    write_json(ep / "candidates.jsonl", {**read_json(ep / "candidates.jsonl"), "answer": "第二次登记"})
    arts = standalone.discover_artifacts([str(root)])
    assert [a.episode_id for a in arts] == ["ep_1", "ep_2"]
    assert arts[1].candidate.answer == "第二次登记"
    assert arts[0].resume_key() != arts[1].resume_key()


def test_pool_scope_with_truncated_extra_pack_line(judge_run):
    cfg, kwargs, fake = judge_run
    pack = Path(kwargs["roots"][0]) / "episodes/ep_1/packs.jsonl"
    with pack.open("a", encoding="utf-8") as f:
        f.write("{")
    result = standalone.run(cfg, evidence_scope="episode_pool", **kwargs)
    assert result["rows"][0]["usable"]
    assert result["rows"][0]["judge_evidence_ids"] == ["ev_1"]
    assert [b["model"] for b in fake.bodies] == ["unit-judge"]


def test_web_binds_human_and_judge_to_reviewed_candidate(judge_run, tmp_path, monkeypatch):
    cfg, kwargs, _ = judge_run
    first = standalone.run(cfg, **kwargs)
    second = standalone.run(cfg, **kwargs)
    monkeypatch.chdir(tmp_path)
    root = Path(kwargs["roots"][0])
    monkeypatch.setitem(webapp.BATCH2ROOT, "b1", str(root))
    write_json(Path("results/human-blind-review-20261119/internal/identity_map.json"),
               [{"task_id": "t1", "episode_id": "ep_1", "candidate_id": "cand_1", "batch": "b1", "盲审编号": "R01"}])
    returns = Path("results/human-review-returns")
    write_json(returns / "partial_success_labels_v2.json", {"labels": {}})
    with (returns / "20261119_评审表_returned.csv").open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["盲审编号", "tf", "src", "ev"], ["R01", "complete", "met", "sufficient"]])
    path = root / "episodes/ep_1/candidates.jsonl"
    sibling = {**read_json(path), "candidate_id": "cand_2"}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sibling) + "\n")
    detail = webapp.candidate_detail(str(root), "ep_1", "cand_1")
    assert detail["verdicts"]["human"]["full_success"]
    receipts = detail["claims"][0]["evidence"][0]["judge_inputs"]
    assert {r["evaluation"] for r in receipts} == {first["eval_run_id"], second["eval_run_id"]}
    assert all(r["included"] is True for r in receipts)
    other = webapp.candidate_detail(str(root), "ep_1", "cand_2")
    assert "human" not in other["verdicts"] and not other["verdicts"]["model_v4"]
    assert other["claims"][0]["evidence"][0]["judge_seen"] is None


def test_preflight_plan_is_not_a_receipt():
    plan = {"included_ids": ["ev_1"]}
    assert standalone.recorded_evidence_ids({"evidence_plan": plan, "dispatched": False}) is None
    assert standalone.recorded_evidence_ids({"attempts": [
        {"status": "TIMEOUT", "call_id": "c", "evidence_plan": plan},
    ]}) is None


def test_cli_reports_actual_directory_and_zero_call_resume(judge_run, tmp_path, capsys):
    cfg, kwargs, fake = judge_run
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    args = ["judge", "--config", str(config), "--root", kwargs["roots"][0],
            "--rubric", kwargs["rubric_path"], "--prompt", kwargs["prompt_path"],
            "--out", kwargs["out_dir"]]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    second = next((Path(kwargs["out_dir"]) / "runs").iterdir())
    output = capsys.readouterr().out
    assert json.dumps(str(second))[1:-1] in output and second.name in output
    assert main(args + ["--resume-from", kwargs["out_dir"]]) == 0
    assert '"calls_model": false' in capsys.readouterr().out
    assert len(fake.bodies) == 2
