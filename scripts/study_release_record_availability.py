"""Generate the same release note with and without access to its historical install record."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from framework.archive_attribution import reconstruct_episode_attribution
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import resolve_routing
from framework.runner import load_config, run_experiment
from study_document_handoffs import (
    FIXTURE, REPO, RepositoryBriefAssembler, document, load_template, local_analysis_gateway,
    read, read_documents, role_usage, task_for, write_product,
)


QUERIES = {"D2": "读取 c7f3c35 的公开打包配置。", "D1": "读取早期安装验收记录。"}
REFUSAL = "无法提供该次安装验收记录。"


def collect_records(out, arm, case, documents):
    """Use the normal batch runner. The only changed reply concerns D1 availability."""
    cfg = load_config(str(REPO / "configs/experiment.yaml"))
    cfg["experiment"].update(id=arm, results_dir=str(out))
    cfg["method"].update(controller={"type": "scripted", "queries": list(QUERIES.values())},
                         state={"backend": "raw_history"})
    dataset = out / (arm + ".jsonl")
    scripts = {}
    for ident, query in QUERIES.items():
        value = REFUSAL if arm == "refused" and ident == "D1" else documents[ident]["value"]
        scripts[query] = [{"status": "OK", "text": value}]
    record = {"task_id": case["id"], "prompt": task_for(case).prompt, "target_scripts": scripts}
    dataset.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    cfg["datasets"].update(path=str(dataset), manifests_dir=str(out / "manifests"))
    ledger = run_experiment(cfg)
    run_root = out / arm
    ep, = read(run_root / "plan.json")["episodes"]
    events_file = run_root / "episodes" / ep["episode_id"] / "events.jsonl"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    return events, {"episode_id": ep["episode_id"], "events": str(events_file.relative_to(out)),
                    "fixture_target_dispatches": ledger["calls_by_role"].get("target", 0)}


def release_handoff(events, documents):
    """Choose the release-note inputs from actual dispatches, receipts and observations."""
    rounds = reconstruct_episode_attribution(events)["rounds"]
    by_id = {event["event_id"]: event for event in events if event.get("event_id")}
    requests = {e["call_id"]: e for e in events if e["type"] == "request_sent" and e["role"] == "target"}
    source_by_query = {q: ident for ident, q in QUERIES.items()}
    visible, receipts = {}, {}
    for rd in rounds:
        if rd["observation_binding"] == "unresolved" or not rd["response_event_id"]:
            raise ValueError("Unresolved release-record receipt")
        messages = requests[rd["call_id"]]["payload"]["messages"]
        ident = source_by_query[messages[-1]["content"]]
        if ident in receipts:
            raise ValueError("Repeated release-record dispatch")
        response = by_id[rd["response_event_id"]]["payload"]
        body = response.get("text") or ""
        if rd["gateway_status"] == "OK" and body == documents[ident]["value"]:
            visible[ident] = documents[ident]
            availability = "present"
        elif (ident == "D1" and rd["gateway_status"] == "OK" and body == REFUSAL
              and rd["runtime_label"] == "refusal"):
            availability = "refused"
        else:
            raise ValueError(f"Unexpected reply for release record {ident}")
        receipts[ident] = {"availability": availability, "call_id": rd["call_id"],
                           "response_event_id": rd["response_event_id"],
                           "observation_event_id": rd["observation_event_id"], "status": rd["gateway_status"],
                           "label": rd["runtime_label"], "reply_sha256": stable_hash(body)}
    if set(receipts) != set(QUERIES) or "D2" not in visible:
        raise ValueError("Incomplete release-record collection")
    action = "write_with_public_config" if receipts["D1"]["availability"] == "refused" else "write_with_both_records"
    return [visible[k] for k in sorted(visible)], {
        "action": action, "trigger_response_event_id": receipts["D1"]["response_event_id"],
        "receipts": receipts, "visible_sources": sorted(visible),
    }


def run(args):
    fixture = read(FIXTURE)
    case, = [c for c in fixture["cases"] if c["id"] == "wheel"]
    task = task_for(case)
    documents = {k: document(fixture["documents"], case[name], k) for k, name in
                 (("D1", "prefix"), ("D2", "current"))}
    out = Path(args.out or "results/release-record-" + uuid4().hex[:10]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    routing = resolve_routing(_load_config(args))
    store = EventStore(str(out), out.name, path=str(out / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=4), store, out.name)
    reader, rs, rm, rp = local_analysis_gateway(routing, "controller", guard, store)
    writer, ws, wm, wp = local_analysis_gateway(routing, "assembler", guard, store)
    writer.default_params = {**wp, "max_tokens": 1536}
    rt = load_template(rs, "controller", "plan.repository-reading.zh.v1")
    wt = load_template(ws, "assembler", "assemble.repository-brief.zh.v1")
    assembler = RepositoryBriefAssembler(writer, wt, preserve_gap_details=True, max_format_retries=0,
                                         stage_id=ws, phase=Phase.ANALYSIS)
    references = {"available": ["Y", "N", "Y"], "refused": ["U", "N", "Y"]}
    write_json({"task": task.public_view(), "fixture_sha256": stable_hash(fixture), "queries": QUERIES,
                "expected_labels": references, "target": "scripted repository records",
                "reader": [rm, rp, rt.ref()], "assembler": [wm, writer.default_params, wt.ref()]},
               str(out / "protocol.json"))
    results = {}
    for arm, expected in references.items():
        events, retrieval = collect_records(out, arm, case, documents)
        sources, handoff = release_handoff(events, documents)
        write_json({**retrieval, **handoff}, str(out / arm / "release-handoff.json"))
        reading = read_documents(reader, rs, rt, rp, store, task, sources, None, arm)
        product = write_product(assembler, task, sources, reading["rows"], arm, "release-record-v1")
        write_json(reading, str(out / arm / "reading.json"))
        write_json(product, str(out / arm / "release-note.json"))
        results[arm] = {"action": handoff["action"], "sources": handoff["visible_sources"],
                        "fixture_target_dispatches": retrieval["fixture_target_dispatches"],
                        "reader_valid": reading["rows"] is not None, "labels": product["labels"],
                        "matches_reference": product["labels"] == expected, "fallback": product["fallback"],
                        "candidate_id": product["candidate"]["candidate_id"]}
    write_json({"arms": results, "usage": role_usage(out)}, str(out / "summary.json"))
    print(out, flush=True)
    return 0 if all(r["matches_reference"] and r["reader_valid"] for r in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "configs/demo.interactive-normal-task.yaml"))
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file")
    parser.add_argument("--out")
    raise SystemExit(run(parser.parse_args()))
