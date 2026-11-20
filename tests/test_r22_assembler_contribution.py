import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import r22_assembler_contribution as assay


def test_only_the_assigned_source_excerpts_reach_each_call():
    sources = {**assay.SOURCES, assay.TARGET: {"origin": "Archived note", "text": "Archive-only marker."}}
    packets = {arm: assay.messages(sources, ids)[0]["content"] for arm, ids in assay.ARMS.items()}
    for arm, content in packets.items():
        records = json.loads(content.split("\n", 1)[1])["records"]
        assert [r["id"] for r in records] == assay.ARMS[arm]
        assert ("Archive-only marker." in content) == (arm in {"with_target", "with_both"})
        assert (sources[assay.FAQ]["text"] in content) == (arm in {"with_faq", "with_both"})
        assert all(assay.expected_date(year) not in content for year in assay.YEARS)
    assert len({content.split("\n", 1)[0] for content in packets.values()}) == 1


def test_truth_visible_support_and_unscored_remain_separate():
    item = {"policy": "limited", "policy_refs": [assay.TARGET], "archive_limit_years": 10}
    grade = assay.grade(item, assay.ARMS["with_target"])
    assert grade["archive_read_correct"] and grade["policy_outcome"] == "incorrect"
    assert grade["visible_direct_support"] == [assay.TARGET] and grade["policy_refs_match"]
    item.update(policy="unlimited", policy_refs=[])
    grade = assay.grade(item, assay.ARMS["with_target"])
    assert grade["policy_outcome"] == "correct" and grade["visible_direct_support"] == []
    assert not assay.grade(item, assay.ARMS["with_both"])["policy_refs_match"]
    assert assay.grade(None, assay.ARMS["with_target"])["archive_read_correct"] is None
    assert assay.grade({"policy": "unknown"}, [])["policy_outcome"] == "abstained"


def test_generated_calendar_query_must_work_on_other_years_and_return_one_value():
    rule = "SELECT date(printf('%04d-07-01', :year), 'weekday 3', '+21 days')"
    grade = assay.verify_sql(rule)
    assert grade["tested"] == grade["correct"] == 400 and grade["error"] is None
    assert grade["shown"] == {"2024": "2024-07-24", "2025": "2025-07-23", "2026": "2026-07-22"}
    hardcoded = "SELECT CASE :year WHEN 2024 THEN '2024-07-24' WHEN 2025 THEN '2025-07-23' WHEN 2026 THEN '2026-07-22' END"
    assert assay.verify_sql(hardcoded)["correct"] == 3
    assert assay.verify_sql(rule + " UNION ALL " + rule)["correct"] == 0
    assert assay.verify_sql("ATTACH DATABASE ':memory:' AS extra")["error"]
