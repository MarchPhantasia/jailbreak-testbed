from pathlib import Path

import pytest


def test_target_boundary_is_the_first_declined_reply_to_this_candidate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from audit_refusal_provenance import target_history

    def event(seq, role, kind, call="", **payload):
        return {"seq": seq, "role": role, "type": kind, "call_id": call, "payload": payload}

    events = [
        event(1, "target", "request_sent", "a"), event(2, "target", "response_received", "a", status="OK"),
        event(3, "target", "target_refusal", "a", label="soft_refusal"),
        event(4, "controller", "response_received", "b", status="REFUSED"),
        event(5, "target", "request_sent", "c"), event(6, "target", "response_received", "c", status="OK"),
        event(7, "target", "target_refusal", "c", label="refusal"),
        event(8, "assembler", "assembled", candidates=[{"id": "selected"}]),
        event(9, "target", "request_sent", "d"), event(10, "target", "response_received", "d", status="REFUSED"),
    ]
    replies, boundary = target_history(events, "selected")
    assert set(replies) == {"a", "c"} and boundary == 6
    with pytest.raises(ValueError, match="Unpaired"):
        target_history(events[1:], "selected")


def test_human_reference_time_and_missing_alias_stay_distinct(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from audit_refusal_provenance import grouped, review_refs

    aliases = {"E1": "r1", "E2": "r2", "E3": "r3"}
    phases = {"r1": "before", "r2": "at", "r3": "after"}
    assert grouped(review_refs("资料E1–E3", aliases), phases) == "before+at+after"
    assert grouped(review_refs("E1/E4", aliases), phases) == "unresolved"
    assert grouped(review_refs("既有评语未写编号", aliases), phases) == "unresolved"


def test_same_public_detail_requires_the_number_and_its_subject(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from audit_refusal_provenance import DETAILS, mentions_detail

    _, number, qualifier = DETAILS["R13"]
    assert mentions_detail("14–16 billion cumulative views by 2024", number, qualifier)
    assert mentions_detail("累计观看量为 140–160 亿", number, qualifier)
    assert not mentions_detail("14–16 billion dollars", number, qualifier)
    _, number, qualifier = DETAILS["R22"]
    assert mentions_detail("The eligibility window: 10 years.", number, qualifier)
    assert not mentions_detail("The equipment was purchased ten years ago.", number, qualifier)
