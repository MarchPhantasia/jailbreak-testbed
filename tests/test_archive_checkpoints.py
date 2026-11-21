import hashlib
import json
from pathlib import Path

import pytest

from framework import cli
from framework.archive_replay import replay_run
from framework.assembly import Assembler, EvidencePack, LLMAssembler, raw_citation_lines, record_model_claim
from framework.contracts import Phase, Task, stable_hash
from framework.events import EventStore
from framework.prompts import PromptRegistry, PromptTemplate
from test_execution1_r2 import _assembler


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_normal_assembler_discriminates_authorship_literal_wording_and_support(tmp_path):
    material = {"kind": "raw_response", "value": "The album appeared in May.\nThe reissue came in June.",
                "evidence_ids": ["may", "june"]}
    pack = EvidencePack("P", "T", [material])
    task = Task("T", "Write an archive note.", "catalogue")
    payload = {"answer": "The first release preceded the reissue.", "claims": [
        {"claim_id": "quote", "text": "The album appeared in May.", "evidence_ids": ["may"]},
        {"claim_id": "edit", "text": "May was the first release.", "evidence_ids": ["may"]},
        {"claim_id": "other", "text": "The album appeared in May.", "evidence_ids": ["june"]},
    ], "gaps": []}
    live = _assembler(json.dumps(payload), tmp_path=tmp_path)
    offline = LLMAssembler(live.gateway, live.template, max_format_retries=0, phase=Phase.ANALYSIS)
    candidate = offline.assemble_from_pack(task, pack)[0]
    assert live.phase == Phase.ONLINE
    assert {e.phase for e in live.gateway.store.events if e.type in {"request_sent", "assembly_samples"}} == {Phase.ANALYSIS}
    assert [c.text_producer for c in candidate.claims] == ["llm_assembler"] * 3
    assert [(c.origin, c.fidelity) for c in candidate.claims] == [
        ("target_response", "verbatim"), ("unknown", "unknown"), ("unknown", "unknown")]
    assert all(c.support_status == "unknown" for c in candidate.claims)
    copied = Assembler().render(task, pack)[1][0]
    assert (copied.text_producer, copied.fidelity) == ("material_template", "verbatim")
    # No row alignment for a truncated block, conflicting IDs, or a derived summary.
    for rows in ([dict(material, evidence_ids=["may"])], [material, dict(material, value="Changed.\nTwo.")],
                 [dict(material, kind="summary")]):
        claim = record_model_claim("c", payload["claims"][0]["text"], ["may"], raw_citation_lines(rows))
        assert claim.fidelity == "unknown"


def test_real_prefixes_and_legacy_candidate_preserved_at_normal_cli(tmp_path, capsys):
    ep = tmp_path / "episodes/ep"
    store = EventStore(str(tmp_path), "ep")
    tpl = PromptTemplate("plan", "archive", "1", "controller", "Plan\n{{input_json}}\nEnd", "")
    manifest = PromptRegistry({"plan": tpl}).freeze(str(tmp_path))
    save(tmp_path / "plan.json", {"episodes": [{"episode_id": "ep", "task_id": "T"},
                                               {"episode_id": "missing", "task_id": "X"}],
                                  "prompt_manifest": manifest})
    raw = "Only the archive has this sentence."
    mat = {"kind": "raw_response", "value": raw, "evidence_ids": ["E1"]}
    def plan_call(cid, materials):
        msgs = [{"role": "user", "content": tpl.render({"state": {"materials": materials}})}]
        store.append("request_sent", role="controller", call_id=cid, payload={"messages": msgs, "template_ref": tpl.ref()})
        return msgs

    store.append("episode_started")
    plan_call("p1", [])
    target = store.append("request_sent", role="target", call_id="t1", payload={"messages": []})
    reply = store.append("response_received", role="target", call_id="t1", payload={"text": raw})
    save(ep / "evidence.jsonl", {"evidence_id": "E1", "source_event_id": reply.event_id, "text": raw,
                                "span_start": 0, "span_end": len(raw), "checksum": hashlib.sha256(raw.encode()).hexdigest()})
    store.append("state_write", node_id="n1", payload={"snapshot": {"materials": [mat], "source_event_seq": reply.seq}})
    store.append("node_selected", node_id="n1")
    msgs = plan_call("p2", [mat])
    store.append("request_sent", role="target", call_id="t2", payload={"messages": []})
    store.append("pack_frozen", payload={"pack_id": "P", "input_hash": stable_hash([mat]), "materials": 1})
    store.append("request_sent", role="assembler", call_id="a", payload={"messages": []})
    store.append("candidate_selected", payload={"candidate_id": "C"})
    store.append("terminate", payload={"final_candidate_id": "C"})
    save(ep / "packs.jsonl", {"pack_id": "P", "materials": [mat], "input_hash": stable_hash([mat])})
    save(ep / "candidates.jsonl", {"candidate_id": "C", "answer": raw, "evidence_pack_ids": ["P"],
                                  "assembly_config": {"assembler": "llm_assembler"},
                                  "claims": [{"claim_id": "c", "evidence_ids": ["E1"], "text": "A paraphrase.",
                                              "origin": "target_response", "fidelity": "verbatim"}]})
    old = {f: f.read_bytes() for f in ep.iterdir()}
    args = ["replay", "--root", str(tmp_path), "--evidence-id", "E1"]
    for point in ("before-send:1", "after-reply:1", "after-state:1", "before-send:2", "after-reply:2", "before-assembly", "end"):
        args.extend(["--at", point])
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["calls_model"] and report["planned_episodes"] == 2
    assert report["episodes"][1]["archive_status"] == "missing_archive"
    assert report["available_by_point"]["after-reply:2"] == 0
    p = report["episodes"][0]["checkpoints"]
    assert p["before-send:1"]["through_seq"] == target.seq - 1
    assert p["before-send:1"]["watched_received_ids"] == []
    assert p["after-reply:1"]["watched_received_ids"] == ["E1"] and p["after-reply:1"]["last_state_write"] is None
    assert p["after-state:1"]["last_state_write"]["materials"]["watched_ids"] == ["E1"]
    assert p["after-state:1"]["last_dispatched_input"]["controller"]["materials"]["watched_ids"] == []
    assert p["before-send:2"]["last_dispatched_input"]["controller"]["messages_sha256"] == stable_hash(msgs)
    assert p["before-send:2"]["selected_node_state"]["node_id"] == "n1"
    assert p["before-assembly:1"]["last_pack"]["pack_id"] == "P" and "assembler" not in p["before-assembly:1"]["last_dispatched_input"]
    final = p["end"]["final_wording"]
    assert final["archived_verbatim"] == 1 and final["without_exact_cited_raw_line"] == 1
    assert final["recorded_text_producers"] == {"unrecorded": 1}
    assert "final_wording" not in p["after-state:1"] and raw not in json.dumps(report)
    assert cli.main(["replay", "--root", str(tmp_path), "--at", "end", "--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert "root" not in summary and "episodes" not in summary and raw not in json.dumps(summary)
    assert summary["planned_episodes"] == 2 and summary["checkpoints"]["end"]["available_episodes"] == 1
    assert summary["checkpoints"]["end"]["calls_by_role"] == {"controller": 2, "target": 2, "assembler": 1}
    assert summary["final_wording"] == {"status": {"checked": 1}, "verified_llm_claims": 1,
                                        "archived_verbatim": 1, "exact_cited_raw_line": 0}
    assert all(f.read_bytes() == b for f, b in old.items())
    assert not (tmp_path / "episodes/missing").exists()


def test_concurrent_receipts_use_dispatch_identity_and_incomplete_tail_stays_visible(tmp_path):
    save(tmp_path / "plan.json", {"episodes": [{"episode_id": "ep", "task_id": "T"}]})
    store = EventStore(str(tmp_path), "ep")
    store.append("request_sent", role="target", call_id="first")
    store.append("request_sent", role="target", call_id="second")
    store.append("response_received", role="target", call_id="second")
    with open(store.path, "a", encoding="utf-8") as f:
        f.write('{"seq":')
    result = replay_run(tmp_path, at=["after-reply:1", "after-reply:2", "end"])
    episode = result["episodes"][0]
    assert episode["archive_status"] == "unreadable_prefix" and episode["issue_line"] == 4
    assert episode["checkpoints"]["after-reply:1"] is None
    assert episode["checkpoints"]["after-reply:2"]["through_seq"] == 3
    assert episode["checkpoints"]["end"] is None
    with pytest.raises(ValueError, match="Unknown replay point"):
        replay_run(tmp_path, at=["after-reply:0"])
    with pytest.raises(ValueError, match="Unknown episode"):
        replay_run(tmp_path, episodes=["wrong"])


def test_end_requires_a_unique_termination_and_a_readable_archive(tmp_path):
    save(tmp_path / "plan.json", {"episodes": [{"episode_id": "ep", "task": {"task_id": "T"}}]})
    store = EventStore(str(tmp_path), "ep")
    store.append("terminate", payload={"final_candidate_id": None})
    store.append("terminate", payload={"final_candidate_id": None})
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("{")
    row = replay_run(tmp_path, at=["end"])["episodes"][0]
    assert (row["task_id"], row["archive_status"], row["checkpoints"]["end"]) == ("T", "multiple_terminations", None)


def test_archive_comparison_only_passes_the_same_dispatches_and_grades_ties(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import compare_archive_contexts as study

    save(tmp_path / "plan.json", {"episodes": [{"episode_id": "ep", "task_id": "t"},
                                               {"episode_id": "missing", "task_id": "x"}]})
    store = EventStore(str(tmp_path), "ep")
    for role in ("target", "controller", "reviewer", "assembler", "target", "controller"):
        store.append("request_sent", role=role, call_id=f"c{len(store.events)}",
                     payload={"messages": [{"role": "user", "content": "original body stays on disk"}]})
    store.append("terminate", payload={"final_candidate_id": None})
    complete, missing = study.load_cases(tmp_path)
    assert "dispatches" not in missing and missing["archive_status"] == "missing_archive"
    assert complete["counts"] == {"target": 2, "controller": 2, "assembler": 1}
    packets = [study.render(complete["dispatches"], arm)[0]["content"] for arm in study.ARMS]
    ledgers = [p[len(study.INSTRUCTION):].splitlines() for p in packets]
    assert ledgers[0] != ledgers[1] and sorted(ledgers[0]) == sorted(ledgers[1])
    assert len(ledgers[2]) == 3 and all("original body" not in p for p in packets)
    correct = {"counts": complete["counts"], "total": 5, "leaders": ["target", "controller"], "margin": 0}
    assert study.grade(correct, complete["counts"])["all_four"]
    assert not study.grade({**correct, "margin": False}, complete["counts"])["all_four"]
