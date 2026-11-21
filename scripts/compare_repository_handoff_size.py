"""Compare full sources, file locations, and pinned line ranges for public repository notes."""
import argparse
import json
import random
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from compare_archive_contexts import local_analysis_gateway
from framework.budget import BudgetGuard
from framework.assembly import EvidencePack
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import resolve_routing
from study_document_handoffs import (
    FIXTURE, ITEMS, RepositoryBriefAssembler, canonical_brief, document, load_template, read, task_for, write_product,
)


def file_locations(documents):
    return [{**d, "kind": "repository_file_location", "value": "", "fidelity": "unknown"} for d in documents]


def file_excerpts(documents, source_keys, ranges):
    excerpts = []
    for doc, key in zip(documents, source_keys, strict=True):
        first, last = ranges[key]
        if not doc["first_line"] <= first <= last <= doc["last_line"]:
            raise ValueError(f"Excerpt outside the frozen source for {key}")
        lines = doc["value"].splitlines()[first - doc["first_line"]:last - doc["first_line"] + 1]
        excerpts.append({**doc, "first_line": first, "last_line": last, "value": "\n".join(lines)})
    return excerpts


def usage_by_call(root):
    with (root / "events.jsonl").open(encoding="utf-8") as f:
        return {e["call_id"]: e["cost"] for e in map(json.loads, f)
                if e["type"] == "budget_settled" and e.get("call_id")}


def generate(args):
    parent, fixture = args.parent, read(FIXTURE)
    protocol = read(parent / "protocol.json")
    version = protocol.get("protocol_version")
    if version not in {"v2", "v3"} or protocol["fixture_sha256"] != stable_hash(fixture):
        raise ValueError("Supply a v2/v3 repository-document study with the same five tasks")
    variant = args.representation
    ranges = read(FIXTURE.with_name("repository-handoff-excerpts.json")) if variant == "excerpts" else {}
    out = args.out or Path("results") / (f"repository-file-{variant}-" + uuid4().hex[:12])
    out.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(out), out.name, path=str(out / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=len(fixture["cases"])), store, out.name)
    gateway, stage, model, params = local_analysis_gateway(resolve_routing(_load_config(args)), "assembler", guard, store)
    gateway.default_params = {**params, "max_tokens": 1536}
    template = load_template(stage, "assembler", "assemble.repository-brief.zh.v1")
    frozen = protocol["roles"]["assembler"]
    if [model, gateway._merge_params({}), template.sha256] != [frozen[0], frozen[1], frozen[2]["sha256"]]:
        raise ValueError("Assembler model, parameters or maintenance-note template differ from the parent")
    writer = RepositoryBriefAssembler(gateway, template, preserve_gap_details=version == "v3",
                                      max_format_retries=0, stage_id=stage, phase=Phase.ANALYSIS)
    costs = usage_by_call(parent)
    rows, blind = [], []
    write_json({"parent": str(parent.resolve()), "fixture_sha256": stable_hash(fixture),
                "model": model, "params": frozen[1], "prompt_sha256": template.sha256,
                "planned": len(fixture["cases"]), "primary": protocol["primary"],
                "variant": variant, "git_line_ranges": ranges, "parent_protocol_version": version},
               str(out / "protocol.json"))
    for case in fixture["cases"]:
        key = case["id"]
        folder = parent / key
        docs = [document(fixture["documents"], case["prefix"], "D1"),
                document(fixture["documents"], case["current"], "D2")]
        reading = read(folder / "current.json")
        full = read(folder / "current.controller.json")
        handoff = canonical_brief({"rows": reading["reader"]["rows"]}, {"D1", "D2"})
        if (reading["documents"] != docs or full["pack"]["materials"] != docs or handoff is None
                or stable_hash(handoff) != full["handoff_sha256"]):
            raise ValueError(f"Could not bind original documents and the plain reader handoff for {key}")
        small_docs = (file_excerpts(docs, (case["prefix"], case["current"]), ranges)
                      if variant == "excerpts" else file_locations(docs))
        short = write_product(writer, task_for(case), small_docs, handoff, key, version)
        if short["pack"]["notes"] != full["pack"]["notes"]:
            raise ValueError(f"Handoff changed for {key}")
        write_json(short, str(out / (key + ".json")))
        for condition, product, ledger in (("full", full, costs), (variant, short, usage_by_call(out))):
            judgments = sum(a == b[0] for a, b in zip(product["labels"] or [], case["reference"]["current"]))
            call = product["calls"][0] if product["calls"] else None
            rows.append({"task": key, "condition": condition, "call_id": call, "cost": ledger.get(call, {}),
                         "fallback": product["fallback"], "decisions": judgments,
                         "candidate_id": product["candidate"]["candidate_id"]})
            if not product["fallback"]:
                answer = product["candidate"]["answer"]
                body = "\n".join(answer.strip().splitlines()[1:]).strip() if product["labels"] else answer
                blind.append((key, condition, {"task": case["request"], "statements": case["statements"],
                                               "documents": docs, "body": body}))
    random.Random(845113 if variant == "excerpts" else 792367).shuffle(blind)
    packet, bindings = [], []
    for i, (key, condition, item) in enumerate(blind, 1):
        bid = f"T{i:02d}"
        packet.append({**item, "blind_id": bid})
        bindings.append({"blind_id": bid, "task": key, "condition": condition})
    for name, value in (("measurements", rows), ("notes-to-read", packet), ("bindings", bindings)):
        write_json(value, str(out / (name + ".json")))
    print(out)


def summary(out, review):
    packet, ratings = read(out / "notes-to-read.json"), read(review)
    if (ratings["packet_sha256"] != stable_hash(packet) or
            set(ratings["notes"]) != {r["blind_id"] for r in packet} or
            any(n.get("rating") not in {"usable", "needs_edit"} for n in ratings["notes"].values())):
        raise ValueError("Reading does not match this blind packet")
    usable = {(r["task"], r["condition"]) for r in read(out / "bindings.json")
              if ratings["notes"][r["blind_id"]]["rating"] == "usable"}
    result = {}
    rows = read(out / "measurements.json")
    variant = read(out / "protocol.json").get("variant", "locations")
    for condition in ("full", variant):
        group = [r for r in rows if r["condition"] == condition]
        good = [r for r in group if (r["task"], condition) in usable]
        data = {"planned": len(group), "correct_decisions": sum(r["decisions"] for r in group),
                "prose_usable": len(good), "usable_product": sum(r["decisions"] == len(ITEMS) for r in good)}
        for field in ("tokens_in", "tokens_out"):
            observed = [r["cost"][field] for r in group if r["cost"].get(field) is not None]
            data[field] = sum(observed)
            data[field + "_unknown"] = len(group) - len(observed)
        result[condition] = data
    return result


def recover_gap_drafts(root, dest):
    """Apply the document v3 reader to previously paid responses, without changing the original run."""
    if not dest or dest.exists():
        raise ValueError("Supply a new --out directory for the recovered drafts")
    if read(root / "protocol.json")["fixture_sha256"] != stable_hash(read(FIXTURE)):
        raise ValueError("Not the frozen public repository-document study")
    with (root / "events.jsonl").open(encoding="utf-8") as f:
        events = [json.loads(line) for line in f]
    requests = {e["call_id"]: e["payload"]["messages"] for e in events if e["type"] == "request_sent"}
    responses = {e["call_id"]: e["payload"]["text"] for e in events if e["type"] == "response_received"}
    writer = RepositoryBriefAssembler(None, None, preserve_gap_details=True)
    drafts = []
    for row in read(root / "measurements.json"):
        if row["condition"] == "full" or not row["fallback"]:
            continue
        item = read(root / (row["task"] + ".json"))
        call = row["call_id"]
        attempts = item["candidate"]["assembly_config"].get("attempts", [])
        if not any(a.get("call_id") == call and a.get("status") == "OK"
                   and a.get("problems") == ["gaps_not_list_of_string"] for a in attempts):
            continue
        if not call or call not in responses:
            continue
        if stable_hash(requests[call]) != item["messages_sha256"] or item["calls"] != [call]:
            raise ValueError("Archived response belongs to a different request")
        raw = writer._extract_json(responses[call])
        received, errors = writer._validate(raw, EvidencePack(**item["pack"]))
        draft = {"task": row["task"], "condition": row["condition"], "call_id": call,
                 "response_sha256": stable_hash(responses[call]), "problems": errors}
        if received is not None:
            draft.update(answer=received["answer"], claims=[asdict(c) for c in received["claims"]],
                         gaps=received["gaps"], original_gaps=raw["gaps"])
        drafts.append(draft)
    dest.mkdir(parents=True)
    write_json(drafts, str(dest / "drafts.json"))
    return {"reviewed": len(drafts), "recoverable": sum(not d["problems"] for d in drafts), "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parent", type=Path)
    mode.add_argument("--summarize", type=Path)
    mode.add_argument("--recover-gaps", type=Path, help="Read archived model outputs using document protocol v3")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--representation", choices=("locations", "excerpts"), default="locations",
                        help="Keep file locations only or include the specified Git line ranges.")
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    if args.recover_gaps:
        print(json.dumps(recover_gap_drafts(args.recover_gaps, args.out), ensure_ascii=False, indent=2))
    elif args.summarize and args.review:
        print(json.dumps(summary(args.summarize, args.review), ensure_ascii=False, indent=2))
    elif args.parent and not args.review:
        generate(args)
    else:
        parser.error("--summarize requires --review and --parent starts a new comparison")
