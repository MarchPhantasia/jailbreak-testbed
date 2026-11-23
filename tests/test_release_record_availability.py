from pathlib import Path

import pytest

from framework.contracts import stable_hash


def test_release_note_handoff_follows_the_received_records(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_release_record_availability as study

    fixture = study.read(study.FIXTURE)
    case = next(c for c in fixture["cases"] if c["id"] == "wheel")
    docs = {"D1": study.document(fixture["documents"], case["prefix"], "D1"),
            "D2": study.document(fixture["documents"], case["current"], "D2")}
    inputs = {}
    for arm, expected in [("available", ["D1", "D2"]), ("refused", ["D2"])]:
        events, retrieval = study.collect_records(tmp_path, arm, case, docs)
        visible, decision = study.release_handoff(events, docs)
        assert decision["visible_sources"] == expected
        assert [s["evidence_ids"][0] for s in visible] == expected
        assert retrieval["fixture_target_dispatches"] == 2
        assert decision["trigger_response_event_id"] == decision["receipts"]["D1"]["response_event_id"]
        inputs[arm] = decision
        if arm == "refused":
            receipt = next(e for e in events if e["event_id"] == decision["trigger_response_event_id"])
            assert receipt["payload"]["status"] == "OK" and decision["receipts"]["D1"]["label"] == "refusal"
            assert decision["action"] == "write_with_public_config"
            receipt["payload"]["status"] = "TIMEOUT"
            with pytest.raises(ValueError, match="Unexpected reply"):
                study.release_handoff(events, docs)
    assert inputs["available"]["action"] == "write_with_both_records"
    assert inputs["available"]["receipts"]["D2"]["reply_sha256"] == stable_hash(docs["D2"]["value"])
    assert inputs["refused"]["receipts"]["D2"]["reply_sha256"] == inputs["available"]["receipts"]["D2"]["reply_sha256"]
