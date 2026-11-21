import json
from pathlib import Path

import pytest

from framework.clients import CallResult, MockGateway
from framework.contracts import Status, stable_hash


@pytest.mark.parametrize(("action", "anchor", "final_value"), [
    ("keep", "after_P3", "U"), ("revise", "after_P2", "Y")
])
def test_optional_review_really_uses_the_same_checkpoint(tmp_path, monkeypatch, action, anchor, final_value):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import compare_python_sample_notes as study

    case = study.read(study.CASE)
    doc = {"symbol": case["symbol"], "release": case["release"], "value": "Python documentation.",
           "evidence_ids": ["D1"]}
    seen = []

    def route(routing, role, guard, store):
        def respond(messages):
            content = messages[0]["content"]
            data = json.loads(content[content.index("{"):])
            seen.append((role, data))
            assert '"reference"' not in content and '"requirements"' not in content
            if role == "assembler":
                result = {"answer": "P1=Y P2=N P3=Y\n维护说明。", "claims": [
                    {"claim_id": "c1", "text": "维护说明。", "evidence_ids": ["D1"]}], "gaps": []}
            else:
                result = {"rows": [{"id": i, "value": {"P1": "Y", "P2": "N", "P3": "Y"}[i],
                                    "sources": ["D1"]} for i in data["assigned"]]}
                if data["mode"] == "review":
                    result.update(action=action, restore_to=anchor)
            return CallResult("", Status.OK, text=json.dumps(result))
        return MockGateway(guard, store, scripts={"*": [respond]}), role, "mock", {}

    monkeypatch.setattr(study, "local_analysis_gateway", route)
    root = tmp_path / "paired"
    study.run_pair(root, case, doc, {})
    initial, first_writer, decision, second_writer = [v for _, v in seen]
    assert case["fixed_reply"] not in initial["task"]["prompt"]
    assert first_writer["task"] == decision["task"] == second_writer["task"]
    assert case["fixed_reply"] in first_writer["task"]["prompt"]
    assert first_writer["pack"]["materials"] == second_writer["pack"]["materials"] == [doc]
    assert decision["committed"] == json.loads(first_writer["pack"]["notes"][0])["rows"]
    if action == "keep":
        assert first_writer == second_writer
    report = study.summary(root)
    a, b = report["arms"]["direct"], report["arms"]["optional_review"]
    assert a["start_sha256"] == b["start_sha256"] and a["final_rows"][-1] == "U"
    assert b["final_rows"][-1] == final_value and b["rolled_back"] == (action == "revise")
    assert "controller" not in a["usage"] and b["usage"]["controller"]["calls"] == 1
    cards = study.read(root / "notes-to-read.json")
    assert len(cards) == 2 and all("arm" not in c for c in cards)
    rating = root / "reading.json"
    study.save(rating, {"packet_sha256": stable_hash(cards),
                        "notes": {c["blind_id"]: {"rating": "usable"} for c in cards}})
    assert all(r["usable_product"] for r in study.summary(root, rating)["arms"].values())
