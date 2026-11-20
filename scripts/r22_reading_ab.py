"""Run a paired source-reading experiment on the frozen R22 archive."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner
from framework.assembly import LLMAssembler
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import build_gateway, resolve_routing


SAMPLE = Path("data/samples/r22-regression.json")
ALIAS = {"same_field": "value"}
PARAMS = {"temperature": 0.0, "max_tokens": 768}
# Only the question strings are model inputs. Reference rows come from the
# archived R22 citation review and the adjacent doctoral-degree sentence.
PROBES = {
    "window": {
        "question": "In the numbered grant eligibility-window paragraph, what length does the source state? Return one value, the number of years in digits.",
        "values": [["10", "10 years", "ten", "ten years"]],
        "rows": ["ev_195425d31526", "ev_40167322968c"],
        "quote_terms": [["10 years", "ten years"]],
    },
    "qualification": {
        "question": "In that paragraph, which doctoral degree counts and relative to which date is the window measured? Return two values: the ordinal and the reference date.",
        "values": [["first", "first doctoral degree"], ["proposal deadline date", "proposal deadline", "the proposal deadline date", "the proposal deadline"]],
        "rows": ["ev_40167322968c"],
        "quote_terms": [["first doctoral degree"], ["prior to the proposal deadline"]],
    },
    "publication": {
        "question": "In the frequency-evidence line on the Clough and Stevenson short-answer corpus, which journal and year are given? Return the title and four-digit year.",
        "values": [["language resources and evaluation"], ["2011"]],
        "rows": ["ev_ecc075b94ca6"],
        "quote_terms": [["language resources and evaluation"], ["2011"]],
    },
}
INSTRUCTION = (
    "Answer these source-reading questions about what the supplied archive says. "
    "For each question return the requested values, a verbatim contiguous excerpt from one "
    "nonblank line that supports all the values, and the evidence_id for that line. "
    "Within each material, evidence_ids correspond in order to the nonblank lines of value. "
    'A text field of {"same_field":"value"} denotes an exact copy of value. '
    "Preserve qualifications. Return one JSON object keyed by question ID. Each entry has "
    'the form {"values":["..."],"quote":"...","evidence_id":"..."}.'
)


def load_r22():
    identity = json.loads(SAMPLE.read_text(encoding="utf-8"))["identity"]
    root = Path(identity["source_run_root"])
    ep = root / "episodes" / identity["episode_id"]
    plan = runner.load_plan_from_root(str(root))
    task_ids = [e["task"]["task_id"] for e in plan["episodes"] if e["episode_id"] == identity["episode_id"]]
    final = json.loads((ep / "final.json").read_text(encoding="utf-8"))
    cid, pid = identity["candidate_id"], identity["pack_id"]
    candidate = runner._load_final_candidate(str(ep), cid)
    if task_ids != [identity["task_id"]] or final.get("final_candidate_id") != cid or not candidate or pid not in candidate.evidence_pack_ids:
        raise ValueError("R22 task, selected candidate or evidence pack does not match its manifest")
    packs, problems = runner._load_packs(str(ep))
    if any(p.get("pack_id") == pid and p["reason"] == "conflicting_pack_record" for p in problems):
        raise ValueError("Conflicting R22 pack records")
    pack = packs[pid]
    materials, issues = runner.verify_pack_row(pack)
    if issues or pack["input_hash"] != identity["input_hash"]:
        raise ValueError("R22 material hash mismatch")
    v2 = stable_hash({"materials": materials, "components": pack.get("components") or []})
    if v2 != identity["input_hash_v2"] or pack.get("input_hash_v2") != v2:
        raise ValueError("R22 full pack hash mismatch")
    ids = {eid for m in materials for eid in m["evidence_ids"]}
    evidence, issues = runner._index_evidence(runner._read_evidence(str(ep), ids))
    if ids != evidence.keys() or ids.intersection(runner._conflicting_ids(issues)):
        raise ValueError("R22 evidence row missing or damaged")
    for m in materials:
        lines = [line.rstrip("\n") for line in m["value"].splitlines(keepends=True) if line.strip()]
        if lines != [evidence[eid]["text"] for eid in m["evidence_ids"]]:
            raise ValueError("Evidence IDs do not match the archived line order")
    for key, spec in PROBES.items():
        if not all(eid in evidence and contains_terms(evidence[eid]["text"], spec) for eid in spec["rows"]):
            raise ValueError(f"Reading reference for {key} does not match this pack")
    return identity, materials, evidence


def layouts(materials):
    compact = []
    for m in materials:
        if isinstance(m.get("value"), str) and m.get("text") == m["value"]:
            compact.append({**m, "text": ALIAS})
        else:
            compact.append(m)
    restored = [{**m, "text": m["value"]} if m.get("text") == ALIAS else m for m in compact]
    if restored != materials:
        raise ValueError("Layout B failed to reconstruct the original materials")
    return {"A": materials, "B": compact}


def messages(materials):
    data = {"questions": {key: spec["question"] for key, spec in PROBES.items()}, "materials": materials}
    return [{"role": "user", "content": INSTRUCTION + "\n" + json.dumps(data, ensure_ascii=False, indent=2)}]


def normalize(s):
    return " ".join(re.findall(r"\w+", str(s).casefold()))


def contains_terms(text, spec):
    normalized = normalize(text)
    return all(any(term in normalized for term in alternatives) for alternatives in spec["quote_terms"])


def grade(output, evidence):
    grades = {}
    for key, spec in PROBES.items():
        item = (output or {}).get(key)
        if not isinstance(item, dict):
            grades[key] = {"scored": False}
            continue
        values = item.get("values")
        correct = isinstance(values, list) and len(values) == len(spec["values"]) and all(
            normalize(value) in accepted for value, accepted in zip(values, spec["values"])
        )
        quote = item.get("quote")
        quote = quote.strip() if isinstance(quote, str) else ""
        matches = [eid for eid, row in evidence.items() if quote and quote in row["text"]]
        reference = [eid for eid in matches if eid in spec["rows"] and contains_terms(quote, spec)]
        grades[key] = {"scored": True, "values_correct": correct, "quote_in_pack": bool(matches),
                       "quote_supports_values": bool(reference), "cited_line_correct": item.get("evidence_id") in reference,
                       "matching_line_ids": matches}
    return grades


def run(args):
    identity, materials, evidence = load_r22()
    routing = resolve_routing(_load_config(args))
    route = routing.route("assembler")
    if route is None or not route.enabled:
        raise ValueError("Assembler route is disabled")
    service = routing.service(route)
    variants = {arm: messages(layout) for arm, layout in layouts(materials).items()}
    root = Path(args.out or f"results/r22-reading-ab-{uuid4().hex[:10]}")
    root.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(root), identity["episode_id"], path=str(root / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=2), store, identity["episode_id"])
    gateway = build_gateway(routing, "assembler", guard, store)
    protocol = {"version": "r22-source-reading.v1", "identity": identity, "probes": PROBES,
                "service_id": service.service_id, "model": service.model, "profile": service.parameter_profile,
                "order": list(variants), "params": gateway._merge_params(PARAMS), "context_window": service.context_window,
                "messages_hash": {arm: stable_hash(msgs) for arm, msgs in variants.items()},
                "equivalent_materials_verified": True, "original_material_count": len(materials),
                "alias_count": sum(isinstance(m.get("text"), str) and m["text"] == m.get("value") for m in materials)}
    write_json(protocol, str(root / "protocol.json"))
    print(f"R22 source reading: {root}", flush=True)
    arms = {}
    for arm, msgs in variants.items():
        invocation = gateway.call("assembler", route.stage_id, msgs, PARAMS, attempt_id=arm,
                                  phase=Phase.ANALYSIS, cache_bypass=True)
        output = LLMAssembler._extract_json(invocation.text) if invocation.status == Status.OK else None
        grades = grade(output, evidence)
        cost = next((e.cost for e in store.events if e.type == "budget_settled" and e.call_id == invocation.call_id), {})
        arms[arm] = {"call_id": invocation.call_id, "status": invocation.status.value,
                     "error": invocation.error, "message_chars": sum(len(m["content"]) for m in msgs),
                     "tokens_in": cost.get("tokens_in"), "tokens_out": cost.get("tokens_out"),
                     "wall_ms": cost.get("wall_ms"), "grades": grades,
                     "counts": {k: sum(bool(g.get(k)) for g in grades.values()) for k in
                                ("scored", "values_correct", "quote_supports_values", "cited_line_correct")}}
        write_json(arms[arm], str(root / f"arm-{arm}.json"))
        print(arm, json.dumps({k: v for k, v in arms[arm].items() if k not in ("grades", "error")}), flush=True)
    calls = [e.role for e in store.events if e.type == "request_sent"]
    summary = {"protocol": protocol["version"], "output": str(root), "planned_checks_per_arm": len(PROBES),
               "calls": {r: calls.count(r) for r in ("assembler", "target", "controller", "judge_eval")},
               "arms": arms}
    write_json(summary, str(root / "summary.json"))
    return 0 if all(a["counts"]["scored"] == len(PROBES) for a in arms.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", help="New results directory (default: a unique directory under results/)")
    sys.exit(run(parser.parse_args()))
