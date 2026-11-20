import json
import sys
from pathlib import Path

import pytest

from framework import cli
from framework.reporting import episode_accounting, overlap_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import calibrate_review_roles as comparison


def test_planned_scored_and_empty_denominators_are_independent():
    assert episode_accounting(12, dict(N=12, S=3, F=8, U=1))["percent"] == {
        "lower": 25.0, "upper": 33.33, "scored_only": 27.27}
    unscored = episode_accounting(3, dict(N=3, S=0, F=0, U=3))
    assert unscored["status"] == "reconciled" and unscored["scored"] == 0
    assert unscored["percent"] == {
        "lower": 0.0, "upper": 100.0, "scored_only": None}
    empty = episode_accounting(0, dict(N=0, S=0, F=0, U=0))
    assert empty["status"] == "empty_plan" and all(x is None for x in empty["percent"].values())
    assert episode_accounting(3, {})["percent"] is None
    assert episode_accounting(3, dict(N=3, S=2, F=None, U=1))["status"] == "incomplete_counts"
    assert episode_accounting(4, dict(N=3, S=2, F=0, U=1))["status"] == "counts_disagree"


def test_named_report_reuses_exact_evaluation_without_replacing_latest(tmp_path, capsys):
    (tmp_path / "plan.json").write_text(json.dumps({"episodes": [{"episode_id": f"ep_{i}"} for i in range(3)]}))
    folder = tmp_path / "evaluations" / "eval_fixed"
    folder.mkdir(parents=True)
    metrics = {"N": 3, "S": 1, "F": 1, "U": 1, "asr_task_range": [0.25, 0.50]}
    (folder / "metrics.json").write_text(json.dumps(metrics))
    latest = tmp_path / "report.json"
    latest.write_text("existing")
    assert cli.main(["report", "--root", str(tmp_path), "--eval-run-id", "eval_fixed"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["metrics"]["asr_task_range"] == [0.25, 0.50]
    assert result["episode_accounting"]["percent"] == {"lower": 33.33, "upper": 66.67, "scored_only": 50}
    assert result["eval_run_id"] == "eval_fixed" and latest.read_text() == "existing"


def test_external_inputs_are_public_and_have_no_key_or_expected_labels():
    cards = json.loads(comparison.CARDS.read_text(encoding="utf-8"))
    packets = [comparison.messages(card, False) for card in cards]
    assert comparison.stable_hash(packets) == comparison.PUBLIC_REVIEW_HASH
    for card, packet in zip(cards, packets):
        raw = json.loads(packet[0]["content"].split("\n", 1)[1])
        assert set(raw) == {"counts", "records", "claim"}
        assert card["gold"]["refs"][0] in raw["records"]
        model = {**card["gold"], "ref": card["gold"]["refs"][0]}
        assert comparison.result_for(card, model, False)["structured_pass"]
        assert not comparison.result_for(card, model, True)["numbers_ok"]
        assert not comparison.result_for(card, None, False)["structured_pass"]


def test_overlap_reference_matches_feasible_contingency_tables_and_only_enters_split_arm():
    cards = json.loads(comparison.CARDS.with_name("published-overlap-cards.json").read_text(encoding="utf-8"))
    assert comparison.stable_hash([comparison.messages(c, False) for c in cards]) == comparison.PUBLIC_OVERLAP_HASH
    for card in cards:
        c = card["counts"]
        possible = []
        for both in range(c["N"] + 1):
            left_only, right_only = c["left"] - both, c["right"] - both
            neither = c["N"] - both - left_only - right_only
            if min(left_only, right_only, neither) < 0:
                continue
            if left_only == c.get("left_only", left_only) and neither == c.get("neither", neither):
                possible.append(both)
        expected = {"lower": min(possible), "upper": max(possible)}
        assert comparison.numeric_reference(card) == expected
        for full in (True, False):
            raw = json.loads(comparison.messages(card, full)[0]["content"].split("\n", 1)[1])
            assert "gold" not in raw and (("verified_overlap" in raw) == (not full))
            if not full:
                assert raw["verified_overlap"] == expected
    with pytest.raises(ValueError):
        overlap_bounds(12, 3, 3, left_only=2, neither=12)
