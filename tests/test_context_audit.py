import json

from framework import cli, webapp
from framework.context_audit import context_profile
from framework.prompts import PromptRegistry, PromptTemplate


def dispatch(role, cid, messages, **payload):
    stage = {"controller": "plan", "assembler": "assemble"}.get(role, role)
    return {"type": "request_sent", "role": role, "stage_id": stage, "call_id": cid,
            "payload": {"messages": messages, **payload}}


def settled(cid, tokens):
    return {"type": "budget_settled", "call_id": cid, "cost": {"tokens_in": tokens, "wall_ms": 11}}


def test_recorded_inputs_agree_on_cli_and_replay(tmp_path, capsys):
    episode = tmp_path / "episodes" / "ep"
    episode.mkdir(parents=True)
    template = PromptTemplate("assemble", "catalogue", "1", "assembler", "前\r\n{{input_json}}\n后", "")
    manifest = PromptRegistry({"assemble": template}).freeze(str(tmp_path))
    (tmp_path / "plan.json").write_text(json.dumps({
        "prompt_manifest": manifest, "episodes": [{"episode_id": "ep", "task": {"task_id": "book"}}],
    }), encoding="utf-8")
    value = "周二闭馆📚"
    body = {"pack": {"materials": [{"value": value}, {"text": value}, {"value": "修订稿"}]}}
    rendered = template.render(body)
    events = [
        dispatch("controller", "p", [{"role": "user", "content": "核对版本"}]),
        dispatch("target", "other", [{"role": "user", "content": "background"}]),
        dispatch("assembler", "a", [{"role": "user", "content": rendered}], template_ref=template.ref()),
        {"type": "cache_hit", "role": "assembler", "stage_id": "assemble",
         "payload": {"served": False, "original_call_id": "a"}},
        settled("other", 99), settled("a", 0), settled("p", None),
    ]
    path = episode / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    original = path.read_bytes()
    assert cli.main(["audit", "--episode-dir", str(episode), "--context"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["calls_model"] is False
    profile = report["context_profile"]
    assert profile == webapp.episode_trace(str(tmp_path), "ep")["context_profile"]
    assert path.read_bytes() == original
    assert [r["call_id"] for r in profile["requests"]] == ["p", "a"]
    p, a = profile["requests"]
    assert p["tokens_in"] is None and p["sections"] is None
    assert a["message_chars"] == len(rendered) and a["tokens_in"] == 0
    s = a["sections"]
    assert s["template_chars"] + s["payload_json_chars"] == len(rendered)
    assert s["template_chars"] == 5 and s["material_text_chars"] == 2 * len(value) + 3
    assert s["repeated_material_text_chars"] == len(value) and s["material_count"] == 3
    assembler, controller = profile["stages"]
    assert (assembler["dispatched"], assembler["cache_hits"], assembler["tokens_in_known_sum"]) == (1, 1, 0)
    assert controller["tokens_in_known_calls"] == 0 and controller["tokens_in_known_sum"] is None
    assert value not in json.dumps(profile, ensure_ascii=False)

    # A changed on-disk template cannot be used to reconstruct old sections.
    (tmp_path / manifest[0]["snapshot_path"]).write_bytes(b"modified")
    a_later = context_profile(events, tmp_path, manifest)["requests"][1]
    assert a_later["sections"] is None and a_later["message_chars"] == len(rendered)
    assert cli.main(["audit", "--episode-dir", str(episode)]) == 0
    assert "context_profile" not in json.loads(capsys.readouterr().out)


def test_incomplete_records_and_nonunique_joins(tmp_path):
    events = [
        dispatch("controller", "shared", None), dispatch("target", "shared", []), settled("shared", 9),
        dispatch("assembler", "dup", []), settled("dup", 21), settled("dup", 21),
        dispatch("assembler", "", [{"role": "user", "content": [{"type": "image"}]}]),
        {"type": "cache_hit", "role": "controller", "stage_id": "legacy"},
    ]
    profile = context_profile(events, tmp_path)
    assert [r["message_chars"] for r in profile["requests"]] == [None, 0, None]
    assert all(r["tokens_in"] is None for r in profile["requests"])
    cached = next(s for s in profile["stages"] if s["stage_id"] == "legacy")
    assert cached["dispatched"] == 0 and cached["cache_hits"] == 1 and cached["tokens_in_peak"] is None


def test_citation_locator_keeps_uncertainty():
    from hashlib import sha256

    source = {"evidence_id": "a", "source_event_id": "reply1", "span_start": 0, "span_end": 3,
              "text": "旧版本", "checksum": sha256("旧版本".encode()).hexdigest()}
    rows = [source, dict(source), {**source, "evidence_id": "b", "checksum": None},
            {**source, "evidence_id": "c", "checksum": "broken"}]
    resolved = webapp._citation_sources(rows)
    assert resolved["a"] == (source, "verified")
    assert resolved["b"][1] == "checksum_missing" and resolved["c"] == ({}, "invalid")
    for change in ({"source_event_id": "reply2"}, {"span_start": 1}, {"text": "新版本"}):
        conflict = webapp._citation_sources([source, {**source, **change}, source])
        assert conflict["a"] == ({}, "conflict")
