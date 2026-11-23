import json

from framework.reporting import build_direct3_audit, reconstruct_episode_attribution, render_audit_markdown


def send(call, parent="root"):
    return {"type": "request_sent", "role": "target", "node_id": parent, "call_id": call}


def reply(call, role="target"):
    return {"type": "response_received", "role": role, "call_id": call, "event_id": "r-" + call,
            "payload": {"status": "OK", "text": "sample"}}


def node(child, parent="root", **extra):
    return {"type": "node_created", "event_id": "n-" + child, "node_id": child,
            "payload": {"parent": parent, "label": "refusal", **extra}}


def test_missing_receipt_does_not_steal_the_next_calls_observation():
    events = [send("a"), send("b"), reply("b"), node("second"), send("c", "second")]
    a, b, c = reconstruct_episode_attribution(events)["rounds"]
    assert (a["obs_child"], a["binding_reason"]) == (None, "missing_or_ambiguous_receipt")
    assert (b["obs_child"], b["observation_binding"]) == ("second", "chronology")
    assert c["inherits_observations"] == [2]


def test_concurrent_legacy_calls_and_multiple_children_stay_unresolved():
    events = [send("a"), send("b"), reply("a"), node("one"), reply("b"), node("two")]
    rows = reconstruct_episode_attribution(events)["rounds"]
    assert [r["binding_reason"] for r in rows] == ["overlapping_requests"] * 2
    rows = reconstruct_episode_attribution([send("a"), reply("a"), node("one"), node("two")])["rounds"]
    assert rows[0]["binding_reason"] == "missing_or_ambiguous_observation"


def test_explicit_links_take_priority_and_cannot_be_borrowed_by_a_sibling():
    events = [send("a"), send("b"), reply("b"), reply("a"), node("b", response_event_id="r-b"),
              {**node("a"), "call_id": "a"}, send("c", "a"), reply("c"), node("c", "a")]
    a, b, c = reconstruct_episode_attribution(events)["rounds"]
    assert [(r["obs_child"], r["observation_binding"]) for r in (a, b, c)] == [
        ("a", "explicit"), ("b", "explicit"), ("c", "chronology")]
    assert c["inherits_observations"] == [1]
    conflict = node("branch", call_id="a", response_event_id="r-b")
    events = [send("a"), reply("a"), send("b"), reply("b"), conflict]
    assert all(r["binding_reason"] == "conflicting_explicit_link"
               for r in reconstruct_episode_attribution(events)["rounds"])


def test_report_reads_runtime_label_from_node_with_or_without_offline_notes(tmp_path):
    ep = tmp_path / "episodes" / "episode"
    ep.mkdir(parents=True)
    (tmp_path / "plan.json").write_text(json.dumps({"episodes": [{"task_id": "t", "episode_id": "episode"}]}))
    (ep / "final.json").write_text("{}")
    refused = {"type": "target_refusal", "role": "target", "call_id": "a", "event_id": "ref-a"}
    events = [send("a"), reply("a"), refused, node("one"), reply("a", "assembler"), send("b")]
    (ep / "events.jsonl").write_text("\n".join(map(json.dumps, events)))
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps([{"episode_id": "episode", "call_id": "a", "runtime_label": "other",
                                  "offline_reading": "human interpretation", "source": "audit"}]))
    audit = build_direct3_audit(str(tmp_path), behavior_notes_path=str(notes))
    first, second = audit["tasks"][0]["rounds"]
    assert (first["runtime_label"], first["annotated_runtime_label"], first["offline_reading"]) == (
        "refusal", "other", "human interpretation")
    assert second["runtime_label"] is None and second["observation_binding"] == "unresolved"
    assert first["refusal_event_ids"] == ["ref-a"] and audit["tasks"][0]["target_refusal_events"] == 1
    assert "待确认 1 次" in render_audit_markdown(audit)
    first = build_direct3_audit(str(tmp_path))["tasks"][0]["rounds"][0]
    assert first["runtime_label"] == "refusal" and first["offline_reading"] is None
    archived = audit["tasks"][0]
    del archived["target_refusal_events"]
    for row in archived["rounds"]:
        del row["observation_binding"]
    rendered = render_audit_markdown(audit)
    assert "拒答事件总数及标签出处" in rendered and "refusal（旧表，出处待核）" in rendered


def test_ambiguous_receipts_are_distinct_from_no_receipt():
    rows = reconstruct_episode_attribution([send("a"), reply("a"), reply("a"), send("b")])["rounds"]
    assert [r["gateway_status"] for r in rows] == ["unresolved_response", "no_response_event"]
    assert all(r["obs_child"] is None for r in rows)
