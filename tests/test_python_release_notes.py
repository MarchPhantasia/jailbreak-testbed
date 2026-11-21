import json
from argparse import Namespace
from pathlib import Path

import pytest

from framework.clients import CallResult, MockGateway
from framework.contracts import Status, stable_hash
from framework.python_release_notes import (
    ANCHORS, ITEMS, api_entries, apply_notice, chapter_window, checkpoint, required_anchor, select_documentation,
)


def test_full_chapter_lookup_retains_directive_examples_and_handles_ambiguity():
    source = ".. function:: a()\n\n   first\n\n.. function:: f(x)\n\n   body\n\n   .. versionchanged:: 3.11\n      more\n\nHeader\n======\n.. function:: z()\n   last\n"
    items = api_entries(source, "mod", "edition", "hash")
    window = chapter_window(items, "mod.f")
    found, method = select_documentation("查阅 `mod.f`", [items], window)
    assert method == "named_api" and found == [items[1]]
    assert found[0]["value"] == "\n".join(source.splitlines()[4:10])
    both = items + [items[1]]
    assert select_documentation("查阅 `mod.f`", [both], window) == (window, "full_window_unresolved_symbol")
    assert select_documentation("没有指定接口", [items], window) == (window, "full_window_no_symbol")


def test_restore_executes_chosen_prefix_even_when_the_choice_is_wrong(tmp_path):
    def rows(answers, src):
        return [{"id": i, "value": v, "sources": [src]} for i, v in zip(ITEMS, answers, strict=True)]

    old, new = rows("NNY", "old"), rows("NYY", "current")
    receipts = {name: checkpoint(tmp_path, name, old[:n]) for n, name in enumerate(ANCHORS)}
    assert required_anchor("NNY", "NYY") == "after_P1"
    saved, edits = apply_notice(tmp_path, receipts, "revise", "after_P2", new)
    assert saved == [new[0], old[1], new[2]] and edits["retained_disagreements"] == ["P2"]
    assert apply_notice(tmp_path, receipts, "revise", "after_P1", new)[0] == new
    with pytest.raises(ValueError, match="Action"):
        apply_notice(tmp_path, receipts, "keep", "start", new)
    (tmp_path / "after_P1.json").write_text("[]")
    with pytest.raises(ValueError, match="changed"):
        apply_notice(tmp_path, receipts, "revise", "after_P1", new)


def test_closed_document_run_binds_each_notice_and_keeps_gold_offline(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_python_releases as study

    fixture = study.read(study.FIXTURE)
    fixture["cases"] = [fixture["cases"][0], fixture["cases"][3]]
    cases = {c["id"]: c for c in fixture["cases"]}
    sources = {}
    for key, case in cases.items():
        name = case["baseline_topic"].split(".", 1)[1]
        for tag in fixture["releases"]:
            sources[tag, case["module"]] = f".. function:: a()\n   unrelated\n.. function:: {name}(v)\n   source {tag}\n.. function:: z()\n   other\n"

    def route(routing, role, guard, store):
        def reply(messages):
            content = messages[0]["content"]
            data = json.loads(content[content.index("{"):])
            assert '"reference"' not in content and '"prose_requirements"' not in content
            case = cases[data["task"]["task_id"]]
            if role == "controller":
                tag = data["running_python"]
                readings = case["reference"][tag]
                doc = next(m for m in data["documents"] if m["release"] == tag and m["symbol"] == case["baseline_topic"])
                result = {"rows": [{"id": i, "value": readings[ITEMS.index(i)], "sources": doc["evidence_ids"]}
                                   for i in data["assigned"]], "reason": "按文档核对。"}
                if data["mode"] == "draft":
                    assert {m["release"] for m in data["documents"]} == {"3.10.0"}
                else:
                    anchor = required_anchor(study.values(data["committed"]), readings)
                    result.update(action="keep" if anchor == "after_P3" else "revise", restore_to=anchor)
            else:
                header = " ".join(i + "=" + v for i, v in zip(ITEMS, case["reference"]["3.11.0"], strict=True))
                eid = data["pack"]["materials"][-1]["evidence_ids"]
                result = {"answer": header + "\n维护说明。", "claims": [{"claim_id": "c1", "text": "维护说明。", "evidence_ids": eid}], "gaps": []}
            return CallResult("", Status.OK, text=json.dumps(result, ensure_ascii=False))
        return MockGateway(guard, store, scripts={"*": [reply]}), role, "mock", {}

    monkeypatch.setattr(study, "_load_config", lambda args: {})
    monkeypatch.setattr(study, "resolve_routing", lambda cfg: {})
    monkeypatch.setattr(study, "local_analysis_gateway", route)
    out = tmp_path / "batch"
    study.run(Namespace(out=out, writer_style="repository"), fixture, sources)
    result = study.summarize(out)
    assert result["transitions"] == {"action_correct": 3, "anchor_correct": 3, "applied_three_correct": 3}
    assert result["usage"]["controller"]["calls"] == 9 and result["usage"]["assembler"]["calls"] == 4
    q = study.read(out / "weighted-mean" / "episode.json")
    assert [n["restore_to"] for n in q["notices"]] == ["after_P3", "start"]
    assert q["products"]["full"]["handoff_sha256"] == q["products"]["automatic"]["handoff_sha256"]
    assert len(q["products"]["full"]["included"]) == 6 and len(q["products"]["automatic"]["included"]) == 2
    cards = study.read(out / "notes-to-read.json")
    assert all("arm" not in card for card in cards)
    review = tmp_path / "review.json"
    study.save(review, {"packet_sha256": stable_hash(cards), "notes": {c["blind_id"]: {"rating": "usable"} for c in cards}})
    assert study.summarize(out, review)["products"]["automatic"]["usable_product"] == 2
    follow = tmp_path / "old-handoff"
    study.compare_products(Namespace(out=follow, compare_old_handoff=out, compare_writer=None), fixture, sources)
    pair = study.read(follow / "weighted-mean.old.json")
    current = study.read(out / "weighted-mean" / "automatic.json")
    assert pair["pack"]["materials"] == current["pack"]["materials"]
    assert pair["handoff_sha256"] != current["handoff_sha256"]
    assert study.summarize(follow)["new_usage"]["assembler"]["calls"] == 1
    edited = tmp_path / "writer"
    study.compare_products(Namespace(out=edited, compare_writer=out, compare_old_handoff=None), fixture, sources)
    examples = study.read(edited / "weighted-mean.examples.json")
    assert examples["pack"] == current["pack"] and examples["messages_sha256"] != current["messages_sha256"]
    assert study.summarize(edited)["new_usage"]["assembler"]["calls"] == 2
