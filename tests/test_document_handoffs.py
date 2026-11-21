import json
from argparse import Namespace
from pathlib import Path

import pytest

from framework.clients import CallResult, MockGateway
from framework.contracts import Status, stable_hash


def test_document_fork_uses_its_checkpoint_and_only_handoff_varies_between_arms(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_document_handoffs as study

    rows = [{"id": "P1", "value": "Y", "sources": ["D1"]},
            {"id": "P2", "value": "U", "sources": []}, {"id": "P3", "value": "U", "sources": []}]
    note = {"answer": "P1=Y P2=U P3=U\n资料说明。", "gaps": [],
            "claims": [{"claim_id": "c", "text": "资料说明。", "evidence_ids": ["D1"]}]}

    def mock_route(routing, role, guard, store):
        payload = {"rows": rows} if role == "controller" else note
        scripts = {"*": [CallResult("", Status.OK, text=json.dumps(payload))]}
        return MockGateway(guard, store, scripts=scripts), role, "mock", {}

    monkeypatch.setattr(study, "_load_config", lambda args: {})
    monkeypatch.setattr(study, "resolve_routing", lambda cfg: None)
    monkeypatch.setattr(study, "local_analysis_gateway", mock_route)
    out = tmp_path / "run"
    assert study.run(Namespace(out=str(out), protocol="v2")) == 0
    wheel = out / "wheel"
    root = study.read(wheel / "checkpoint.json")
    prev = study.read(wheel / "previous.json")
    cur = study.read(wheel / "current.json")
    assert cur["checkpoint_sha256"] == prev["checkpoint_sha256"] == stable_hash(root)
    assert prev["documents"][1]["revision"] != cur["documents"][1]["revision"]
    assert cur["reader"]["prior_sha256"] == stable_hash(root["reader"]["rows"])
    assert {c["statement"] for c in prev["reader"]["file_scope_checks"]} == {"P2", "P3"}
    costs = study.read(out / "summary.json")["actual_dispatches_including_version_fork"]
    assert costs == {"controller": 11, "assembler": 16}
    products = [study.read(wheel / f"current.{a}.json") for a in study.ARMS]
    assert len({a["document_sha256"] for a in products}) == 1
    assert len({a["handoff_sha256"] for a in products}) == 3
    assert all(a["labels"] == ["Y", "U", "U"] and not a["fallback"] for a in products)
    calls = [json.loads(s) for s in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    target = next(e for e in calls if e["call_id"] == cur["reader"]["call_id"] and e["type"] == "request_sent")
    body = target["payload"]["messages"][0]["content"]
    assert cur["documents"][1]["revision"] in body and prev["documents"][1]["revision"] not in body
    assert "reading_key" not in body and '"reference"' not in body
    assert all(e["phase"] == "analysis" for e in calls if e["type"] == "request_sent")
    packet = study.read(out / "notes-to-read.json")
    assert len(packet) == 16 and all("key" not in card and "arm" not in card for card in packet)
    path = out / "reading.json"
    ratings = {"packet_sha256": stable_hash(packet), "notes": {p["blind_id"]: {"rating": "usable"} for p in packet}}
    path.write_text(json.dumps(ratings))
    assert study.summarize(out, path)["current"]["controller"]["prose_usable"] == 5
    ratings["notes"].pop(packet[0]["blind_id"])
    path.write_text(json.dumps(ratings))
    with pytest.raises(ValueError, match="Incomplete"):
        study.summarize(out, path)
    (wheel / "checkpoint.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        study.restore_checkpoint(wheel / "checkpoint.json", cur["checkpoint_sha256"])


def test_reader_cannot_cite_later_documents_and_unknowns_remain_observable(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_document_handoffs as study

    items = [{"id": i, "value": "U", "sources": []} for i in study.ITEMS]
    valid = study.canonical_brief({"rows": list(reversed(items))}, {"D1"})
    assert valid == items
    for patch in ({"id": "P2"}, {"sources": ["D2"]}, {"value": "N"}, {"sources": ["D1", "D1"]}):
        assert study.canonical_brief({"rows": [{**items[0], **patch}, *items[1:]]}, {"D1"}) is None
    assert study.answer_labels("P1=Y P2=U P3=N\n交接正文") == ["Y", "U", "N"]
    assert study.answer_labels("P1=Y P2=U P1=N") is None
    assert study.grade_reader(None, [["U", []]] * 3) == {"judgments": 0, "with_required_sources": 0}


def test_release_note_keeps_model_reading_and_validates_only_the_requested_revision(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_document_handoffs as study

    scope = study.read(study.SCOPES)["wheel"]
    source = {"key": scope["path"], "revision": "old", "evidence_ids": ["D2"]}
    rows = [{"id": item, "value": "N", "sources": ["D2"]} for item in study.ITEMS]
    old = study.apply_file_scope({"rows": rows}, [source], scope)
    assert old["model_rows"] == rows
    assert [r["value"] for r in old["rows"]] == ["N", "U", "U"]
    assert all(check["supplied_file_revisions"] == ["old"] for check in old["file_scope_checks"])
    current = {**source, "revision": scope["revision"]}
    assert study.apply_file_scope({"rows": rows}, [current], scope)["rows"] == rows
    wrong_file = {**current, "key": "README.md"}
    assert study.apply_file_scope({"rows": rows}, [wrong_file], scope)["rows"] == old["rows"]
    assert study.apply_file_scope({"rows": None}, [source], scope)["rows"] is None


def test_document_note_preserves_text_when_reading_a_labelled_gap(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_document_handoffs as study

    writer = study.RepositoryBriefAssembler(None, None)
    pack = study.EvidencePack("pack", "dependencies", [{"value": "pytest>=8.0", "evidence_ids": ["D1"]}])
    claim = {"claim_id": "c", "text": "pytest>=8.0", "evidence_ids": ["D1"]}
    gap = {"gap_id": "P3", "text": "当次实际安装的版本待查。"}
    data = {"answer": "pytest>=8.0", "claims": [claim], "gaps": ["运行日期待查。", gap]}
    recovered, errors = writer._validate(data, pack)
    assert not errors and recovered["answer"] == data["answer"]
    assert recovered["gaps"] == ["运行日期待查。", gap["text"]]
    assert recovered["claims"][0].evidence_ids == ["D1"] and data["gaps"][1] == gap
    invalid = {**data, "gaps": [{**gap, "condition": "不要丢失这个条件"}]}
    assert "gaps_not_list_of_string" in writer._validate(invalid, pack)[1]
    invalid = {**data, "claims": [{**claim, "evidence_ids": ["absent"]}]}
    assert writer._validate(invalid, pack)[0] is None
