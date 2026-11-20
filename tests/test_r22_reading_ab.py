import copy
import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "r22_reading_ab", Path(__file__).resolve().parents[1] / "scripts" / "r22_reading_ab.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_layout_preserves_different_origins_fields_and_order():
    materials = [{"text": "record", "value": "record", "evidence_ids": ["e1"]},
                 {"text": "record", "value": "record", "evidence_ids": ["e2"]},
                 {"text": "exception", "value": "rule", "evidence_ids": ["e3"]},
                 {"value": "no text field", "evidence_ids": ["e4"]}]
    original = copy.deepcopy(materials)
    a, b = probe.layouts(materials).values()
    assert a == original == materials
    assert b == [{**a[0], "text": probe.ALIAS}, {**a[1], "text": probe.ALIAS}, *a[2:]]
    wire = probe.messages(b)[0]["content"]
    assert all(p["question"] in wire for p in probe.PROBES.values())
    assert all(eid not in wire for p in probe.PROBES.values() for eid in p["rows"])


def test_answer_value_quotation_and_cited_row_are_independent():
    line = "The first doctoral degree must be within ten years prior to the proposal deadline."
    rows = {"ev_40167322968c": {"text": line}, "unrelated": {"text": line}}
    output = {"qualification": {"values": ["first", "proposal deadline"], "quote": line, "evidence_id": "unrelated"}}
    result = probe.grade(output, rows)
    assert result["window"] == {"scored": False}
    q = result["qualification"]
    assert q["values_correct"] and q["quote_supports_values"] and not q["cited_line_correct"]
    output["qualification"].update(quote="first doctoral degree", evidence_id="ev_40167322968c")
    q = probe.grade(output, rows)["qualification"]
    assert q["values_correct"] and q["quote_in_pack"] and not q["quote_supports_values"]
    output["qualification"].update(values=["latest", "proposal deadline"], quote=line)
    q = probe.grade(output, rows)["qualification"]
    assert not q["values_correct"] and q["quote_supports_values"] and q["cited_line_correct"]
