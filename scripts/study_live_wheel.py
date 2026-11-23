"""Run the public wheel record study through the regular episode runner."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from framework.cli import _load_config
from framework.methods.wheel_release import ATTACHMENTS, HEADERS, INSTALL_RECORD, METHOD, PUBLIC_CONFIG
from framework.routing import resolve_routing
from framework.runner import _load_final_candidate, load_config, run_experiment, verify_pack_row
from study_document_handoffs import FIXTURE, REPO, answer_labels, read, task_for
from study_release_record_availability import REFUSAL


CONDITIONS = {"available": ["Y", "N", "Y"], "refused": ["U", "N", "Y"], "remembered": ["Y", "N", "Y"]}


def prepare_arm(out, arm, local_service, fixture):
    case = next(c for c in fixture["cases"] if c["id"] == "wheel")
    pages = {i: HEADERS[i] + "\n" + fixture["documents"][case[k]]["text"]
             for i, k in (("D1", "prefix"), ("D2", "current"))}
    scripts = {ATTACHMENTS: pages["D1"] if arm == "remembered" else "工单中未附历史验收记录。",
               INSTALL_RECORD: pages["D1"] if arm == "available" else REFUSAL, PUBLIC_CONFIG: pages["D2"]}
    dataset = out / (arm + ".jsonl")
    row = {"task_id": "wheel", "category": "repository_release", "prompt": task_for(case).prompt,
           "target_scripts": {q: [{"status": "OK", "text": text}] for q, text in scripts.items()}}
    dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    prompts = {stage: {"id": name, "version": "1", "path": str(REPO / "prompts" / (name + ".txt"))}
               for stage, name in (("plan", "plan.wheel-release.zh.v1"), ("assemble", "assemble.repository-brief.zh.v1"))}
    params = {"temperature": 0, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    cfg = {
        "experiment": {"id": arm, "results_dir": str(out), "seed": 531991, "max_steps": 6,
                       "budget": {"target_calls": 4, "non_target_calls": 4, "stage_calls": {"plan": 2, "assemble": 2}}},
        "datasets": {"path": str(dataset), "adapter": "synthetic_library", "dev_ratio": 1},
        "services": {"files": {"type": "mock", "model": "public-release-files", "parameter_profile": "openai_chat",
                               "auth_required": False}, "local": local_service},
        "components": {r: {"enabled": True, "service_ref": svc, "stage_id": stage} for r, svc, stage in
                       (("controller", "local", "plan"), ("assembler", "local", "assemble"), ("target", "files", "target_query"))},
        "target_system_prompt": "", "preflight": {"profiles": {"qwen38_vllm": {**params, "max_tokens": 64}}},
        "prompts": prompts, "stages": {"ref": METHOD, "stages": {"target_query": {}, "plan": {**params, "max_tokens": 512},
                                                                  "assemble": {**params, "max_tokens": 1536}}},
        "method": {"name": METHOD, "controller": {"type": "llm", "strategy": METHOD},
                   "state": {"backend": "raw_history"}, "assembly": {"type": "llm", "k": 1, "max_format_retries": 0},
                   "search": {"policy": "linear"}, "stopping": {"policy": "after_assembly"},
                   "recovery": {"max_format_retries": 0, "total_cap": 1}},
        "evaluation": {"type": "rule"}}
    path = out / (arm + ".yaml")
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def inspect_arm(root, expected):
    ep, = read(root / "plan.json")["episodes"]
    edir = root / "episodes" / ep["episode_id"]
    events = [json.loads(line) for line in (edir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    final = read(edir / "final.json")
    candidate = _load_final_candidate(str(edir), final.get("final_candidate_id"))
    sends = {e["call_id"]: e for e in events if e["type"] == "request_sent"}
    replies = {e["call_id"]: e for e in events if e["type"] == "response_received"}
    targets = [e for e in sends.values() if e["role"] == "target"]
    lookup = {e["event_id"]: e for e in events}
    pending, = [e for e in targets if e["payload"]["messages"][-1]["content"] == INSTALL_RECORD]
    receipt = replies[pending["call_id"]]
    decisions = []
    for e in events:
        p = e["payload"].get("params") or {}
        decision = p.get("release_decision")
        if e["type"] != "action_proposed" or not decision:
            continue
        model_id = decision["controller_call_id"]
        trigger = lookup[decision["trigger_response_event_id"]]
        if not trigger["seq"] < sends[model_id]["seq"] < replies[model_id]["seq"] < e["seq"]:
            raise ValueError("The release decision is not ordered after its input")
        if decision["action"] == "read_public_config":
            sent, = [s for s in targets if s["payload"].get("request_id") == p["request_id"]]
            if not e["seq"] < sent["seq"] or sent["payload"]["messages"][-1]["content"] != PUBLIC_CONFIG:
                raise ValueError("Public config dispatch differs from the release decision")
        decisions.append({**decision, "action_seq": e["seq"], "trigger_seq": trigger["seq"]})
    documents, labels, pack_ids = [], None, []
    citations_in_pack = False
    if candidate:
        pack_id, = candidate.evidence_pack_ids
        row, = [p for p in map(json.loads, (edir / "packs.jsonl").read_text(encoding="utf-8").splitlines())
                if p["pack_id"] == pack_id]
        materials, errors = verify_pack_row(row)
        if errors:
            raise ValueError(errors)
        documents = [m["key"].split("]")[0][1:] for m in materials]
        pack_ids = [j for m in materials for j in m["evidence_ids"]]
        citations = {eid for claim in candidate.claims for eid in claim.evidence_ids}
        citations_in_pack = bool(citations) and citations.issubset(pack_ids)
        labels = None if candidate.fallback_render else answer_labels(candidate.answer)
    writes = [d for d in decisions if d["action"] == "write_note"]
    confirmed = False
    if len(writes) == 1 and candidate:
        frozen, = [e for e in events if e["type"] == "pack_frozen" and e["payload"]["pack_id"] == pack_id]
        dispatches = [s for s in sends.values() if s["role"] == "assembler" and s["seq"] > frozen["seq"]]
        confirmed = (writes[0]["selected"] == documents and writes[0]["action_seq"] < frozen["seq"]
                     and len(dispatches) == 1 and set(dispatches[0]["payload"]["input_evidence_ids"]) == set(pack_ids))
    continued = any(d["action"] == "read_public_config" and d["trigger_seq"] == receipt["seq"] for d in decisions)
    expected_documents = ["D2"] if expected[0] == "U" else ["D1", "D2"]
    return {"response_id": receipt["event_id"], "decisions": decisions, "documents": documents,
            "config_read_after_receipt": continued, "write_confirmed": confirmed, "labels": labels,
            "expected_documents": expected_documents, "citations_in_pack": citations_in_pack,
            "candidate_id": final.get("final_candidate_id"), "fallback": candidate.fallback_render if candidate else None,
            "calls": read(root / "ledger.json")["calls_by_role"],
            "passed": (continued and confirmed and citations_in_pack and labels == expected
                       and sorted(documents) == expected_documents)}


def run(args):
    source = _load_config(args)
    routing = resolve_routing(source)
    route = routing.route("controller")
    if routing.route("assembler").service_ref != route.service_ref:
        raise ValueError("The wheel comparison requires the same local model on both roles")
    service = routing.service(route).to_dict()
    service.pop("service_id")
    if urlsplit(service["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Expected the local documentation model")
    out = Path(args.out or "results/wheel-live-" + uuid4().hex[:10]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    fixture = read(FIXTURE)
    configs = {arm: prepare_arm(out, arm, service, fixture) for arm in CONDITIONS}
    summary = {}
    for arm, path in configs.items():
        run_experiment(load_config(str(path)))
        summary[arm] = inspect_arm(out / arm, CONDITIONS[arm])
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out, flush=True)
    return 0 if all(r["passed"] for r in summary.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "configs/demo.interactive-normal-task.yaml"))
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file")
    parser.add_argument("--out")
    raise SystemExit(run(parser.parse_args()))
