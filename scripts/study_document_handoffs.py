"""Measure how a local reader and writer hand off versioned repository documentation."""
import argparse
import json
import random
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from compare_archive_contexts import local_analysis_gateway
from framework.assembly import EvidencePack, LLMAssembler
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, Task, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.prompts import PromptTemplate
from framework.routing import resolve_routing

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "data/samples/repository-handoff-v1.json"
SCOPES = REPO / "data/samples/repository-handoff-scopes.json"
ARMS = ("documents", "controller", "checked")
ITEMS = ("P1", "P2", "P3")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_template(stage, role, name):
    path = REPO / "prompts" / (name + ".txt")
    return PromptTemplate(stage, name, "1", role, path.read_bytes().decode("utf-8"), str(path))


def document(sources, name, ident):
    source = sources[name]
    return {"kind": "repository_excerpt", "key": source["path"], "value": source["text"],
            "revision": source["revision"], "first_line": source["first_line"], "last_line": source["last_line"],
            "evidence_ids": [ident], "origin": "input", "fidelity": "verbatim"}


def canonical_brief(output, visible_ids):
    rows = output.get("rows") if isinstance(output, dict) else None
    if not isinstance(rows, list) or len(rows) != len(ITEMS):
        return None
    decisions = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        item, value, refs = row.get("id"), row.get("value"), row.get("sources")
        if item not in ITEMS or item in decisions or value not in ("Y", "N", "U"):
            return None
        if not isinstance(refs, list) or any(not isinstance(r, str) or r not in visible_ids for r in refs):
            return None
        if len(set(refs)) != len(refs) or (value != "U" and not refs):
            return None
        decisions[item] = {"id": item, "value": value, "sources": sorted(refs)}
    return [decisions[item] for item in ITEMS]


def reference_brief(reference):
    return [{"id": k, "value": val, "sources": sorted(ids)} for k, (val, ids) in zip(ITEMS, reference)]


def apply_file_scope(observation, documents, scope):
    """For questions about a particular file revision, keep model and verified readings."""
    if not scope or observation["rows"] is None:
        return observation
    file_versions = [d for d in documents if d["key"] == scope["path"]]
    eligible = {eid for d in file_versions if d["revision"] == scope["revision"] for eid in d["evidence_ids"]}
    rows, checks = [], []
    for row in observation["rows"]:
        missing_reference = row["value"] != "U" and eligible.isdisjoint(row["sources"])
        if row["id"] in scope["statements"] and (not eligible or missing_reference):
            checks.append({"statement": row["id"], "file": scope["path"], "requested_revision": scope["revision"],
                           "supplied_file_revisions": sorted({d["revision"] for d in file_versions}),
                           "finding": "缺少对指定修订文件的可核对引用，此项待查。"})
            rows.append({"id": row["id"], "value": "U", "sources": []})
        else:
            rows.append(row)
    return {**observation, "model_rows": observation["rows"], "rows": rows, "file_scope_checks": checks}


class RepositoryBriefAssembler(LLMAssembler):
    """Read both observed forms of a maintenance note's list of outstanding checks."""

    def __init__(self, *args, preserve_gap_details=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.preserve_gap_details = preserve_gap_details

    def _validate(self, data, pack):
        if isinstance(data, dict) and isinstance(data.get("gaps"), list):
            gaps = []
            for gap in data["gaps"]:
                if (isinstance(gap, dict) and isinstance(gap.get("gap_id"), str) and gap["gap_id"].strip()
                        and isinstance(gap.get("text"), str) and gap["text"].strip()):
                    if set(gap) == {"gap_id", "text"}:
                        gap = gap["text"]
                    elif self.preserve_gap_details:
                        metadata = {k: v for k, v in gap.items() if k != "text"}
                        gap = gap["text"] + "\n" + json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                gaps.append(gap)
            data = {**data, "gaps": gaps}
        return super()._validate(data, pack)


def grade_reader(rows, reference):
    gold = reference_brief(reference)
    return {"judgments": sum(a["value"] == b["value"] for a, b in zip(rows or [], gold)),
            "with_required_sources": sum(a["value"] == b["value"] and set(b["sources"]) <= set(a["sources"])
                                         for a, b in zip(rows or [], gold))}


def answer_labels(answer):
    first = answer.strip().splitlines()[0] if answer.strip() else ""
    pairs = re.findall(r"\b(P[1-3])\s*[=:：]\s*([YNU])\b", first.upper().replace("*", "").replace("`", ""))
    values = dict(pairs)
    return [values[k] for k in ITEMS] if len(pairs) == 3 and set(values) == set(ITEMS) else None


def task_for(case):
    items = "\n".join(f"{k} {s}" for k, s in zip(ITEMS, case["statements"]))
    return Task(case["id"], case["request"] + "\n" + items, "repository_documentation")


def read_documents(gateway, stage, template, params, store, task, docs, prior, name):
    payload = {"task": task.public_view(), "documents": docs, "earlier_handoff": prior}
    messages = [{"role": "user", "content": template.render(payload)}]
    visible = {i for d in docs for i in d["evidence_ids"]}
    result = gateway.call("controller", stage, messages, params, phase=Phase.ANALYSIS, cache_bypass=True,
                          input_evidence_ids=sorted(visible), template_ref=template.ref(), attempt_id=name)
    sent = [e for e in store.events if e.type == "request_sent" and e.call_id == result.call_id]
    if sent and (len(sent) != 1 or sent[0].payload["messages"] != messages):
        raise ValueError("Document reader dispatch differs from requested input")
    output = LLMAssembler._extract_json(result.text) if result.status == Status.OK and sent else None
    rows = canonical_brief(output, visible)
    return {"rows": rows, "status": result.status.value, "call_id": result.call_id, "dispatched": bool(sent),
            "messages_sha256": stable_hash(messages), "document_sha256": stable_hash(docs),
            "prior_sha256": stable_hash(prior)}


def restore_checkpoint(path, expected):
    snapshot = read(path)
    if stable_hash(snapshot) != expected:
        raise ValueError("Saved document checkpoint changed")
    return snapshot


def write_product(assembler, task, docs, brief, stage_key, protocol_version):
    notes = [json.dumps(brief, ensure_ascii=False)] if brief is not None else []
    key = stable_hash({"documents": docs, "notes": notes, "task_id": task.task_id})
    pack = EvidencePack("pack_" + key[:16], task.task_id, docs, notes=notes)
    candidate = assembler.assemble_from_pack(task, pack, stage_policy_ref="document-handoff." + protocol_version)[0]
    call_ids = list(assembler.last_call_ids)
    sent = [e for e in assembler.gateway.store.events if e.type == "request_sent" and e.call_id in call_ids]
    if len(sent) > 1:
        raise ValueError("Unexpected repeated document assembly")
    receipts = {"messages_sha256": stable_hash(sent[0].payload["messages"]) if sent else None,
                "document_sha256": stable_hash(docs), "handoff_sha256": stable_hash(brief),
                "calls": call_ids, "pack": pack.to_dict(), "candidate": asdict(candidate)}
    labels = None if candidate.fallback_render else answer_labels(candidate.answer)
    return {**receipts, "key": stage_key, "labels": labels, "fallback": candidate.fallback_render}


def reader_scores(out, cases):
    by_stage = {}
    for case in cases:
        for stage, reference in case["reference"].items():
            stats = by_stage.setdefault(stage, {"planned_decisions": 0, "model_correct": 0,
                                                "handoff_correct": 0, "handoff_with_required_sources": 0})
            stats["planned_decisions"] += len(reference)
            name = "checkpoint" if stage == "prefix" else stage
            path = out / case["id"] / (name + ".json")
            observation = read(path)["reader"] if path.is_file() else {}
            rows = observation.get("rows")
            raw = observation.get("model_rows", rows)
            stats["model_correct"] += grade_reader(raw, reference)["judgments"]
            handoff = grade_reader(rows, reference)
            stats["handoff_correct"] += handoff["judgments"]
            stats["handoff_with_required_sources"] += handoff["with_required_sources"]
    return by_stage


def role_usage(out):
    sent, settled = {}, {}
    with (out / "events.jsonl").open(encoding="utf-8") as log:
        for line in log:
            e = json.loads(line)
            if e["type"] == "request_sent":
                sent[e["call_id"]] = e["role"]
            elif e["type"] == "budget_settled" and e.get("call_id"):
                settled[e["call_id"]] = e.get("cost") or {}
    usage = {}
    for call, role in sent.items():
        stats = usage.setdefault(role, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                        "tokens_in_unknown_calls": 0, "tokens_out_unknown_calls": 0})
        stats["calls"] += 1
        for field in ("tokens_in", "tokens_out"):
            count = settled.get(call, {}).get(field)
            if count is None:
                stats[field + "_unknown_calls"] += 1
            else:
                stats[field] += count
    return usage


def summarize(out, review=None):
    rows = read(out / "measurements.json")
    cases = read(out / "protocol.json")["fixture"]["cases"]
    current = {r["case"]: r for r in rows if r["version"] == "current"}
    passed = {}
    if review:
        packet = read(out / "notes-to-read.json")
        ratings = read(review)
        if ratings["packet_sha256"] != stable_hash(packet):
            raise ValueError("Reading does not refer to these exact texts")
        bindings = {b["blind_id"]: b["key"] for b in read(out / "bindings.json")}
        if (set(ratings["notes"]) != {p["blind_id"] for p in packet} or set(bindings) != set(ratings["notes"])
                or any(v.get("rating") not in {"usable", "needs_edit"} for v in ratings["notes"].values())):
            raise ValueError("Incomplete or ambiguous reading")
        passed = {bindings[bid]: note["rating"] == "usable" for bid, note in ratings["notes"].items()}
    comparison = {}
    for arm in ARMS:
        statuses = Counter()
        judgments = triplets = texts = usable = 0
        for case in cases:
            item = case["id"]
            prod = current.get(item, {}).get("products", {}).get(arm, {})
            statuses[prod.get("status", "missing")] += 1
            right = prod.get("judgments", 0)
            judgments += right
            triplets += right == len(ITEMS)
            text_ok = passed.get(f"{item}.current.{arm}", False)
            texts += text_ok
            usable += text_ok and right == len(ITEMS)
        comparison[arm] = {"planned": len(cases), "statuses": dict(statuses), "correct_decisions": judgments,
                           "all_three": triplets, "prose_usable": texts if review else None,
                           "usable_product": usable if review else None}
    usage = role_usage(out)
    return {"current": comparison, "planned_reader_judgments_per_stage": len(cases) * len(ITEMS),
            "prefix_reader_correct": sum(r["prefix"]["judgments"] for r in current.values()),
            "current_reader_correct": sum(r["reader"]["judgments"] for r in current.values()),
            "reader_by_stage": reader_scores(out, cases), "usage_including_version_fork": usage,
            "actual_dispatches_including_version_fork": {role: stats["calls"] for role, stats in usage.items()}}


def run(args):
    fixture = read(FIXTURE)
    protocol_version = args.protocol
    scopes = read(SCOPES) if protocol_version in {"v2", "v3"} else {}
    cases = fixture["cases"]
    out = Path(args.out or "results/document-handoffs-" + uuid4().hex[:10])
    out.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(out), out.name, path=str(out / "events.jsonl"))
    extra = sum("previous" in c for c in cases)
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=len(cases) * 5 + 2 * extra), store, out.name)
    routing = resolve_routing(_load_config(args))
    reader, rs, reader_model, rp = local_analysis_gateway(routing, "controller", guard, store)
    writer, ws, writer_model, wp = local_analysis_gateway(routing, "assembler", guard, store)
    writer.default_params = {**wp, "max_tokens": 1536}
    rt = load_template(rs, "controller", "plan.repository-reading.zh.v1")
    wt = load_template(ws, "assembler", "assemble.repository-brief.zh.v1")
    options = {"max_format_retries": 0, "stage_id": ws, "phase": Phase.ANALYSIS}
    if protocol_version == "v1":
        assembler = LLMAssembler(writer, wt, **options)
    else:
        assembler = RepositoryBriefAssembler(writer, wt, preserve_gap_details=protocol_version == "v3", **options)
    write_json({"version": fixture["version"], "protocol_version": protocol_version, "file_scopes": scopes,
                "fixture": fixture, "fixture_sha256": stable_hash(fixture),
                "roles": {"controller": [reader_model, reader._merge_params(rp), rt.ref(), rt.text],
                          "assembler": [writer_model, writer._merge_params({}), wt.ref(), wt.text]},
                "arms": ARMS, "rotation": "case position modulo three", "budget": guard.limits.to_dict(),
                "primary": "All three decisions and usable Chinese instructions on blind reading",
                "blind_read": "Grade the prose against the recorded use case, flag contradictions, unsupported additions and omitted material conditions. A missing model product cannot pass."},
               str(out / "protocol.json"))
    print(out, flush=True)
    blind, keys, results = [], [], []
    for index, case in enumerate(cases):
        folder = out / case["id"]
        folder.mkdir()
        task = task_for(case)
        first_doc = document(fixture["documents"], case["prefix"], "D1")
        first = read_documents(reader, rs, rt, rp, store, task, [first_doc], None, case["id"] + ".prefix")
        first = apply_file_scope(first, [first_doc], scopes.get(case["id"]))
        snapshot = {"case": case["id"], "documents": [first_doc], "reader": first}
        checkpoint = folder / "checkpoint.json"
        write_json(snapshot, str(checkpoint))
        checkpoint_hash = stable_hash(snapshot)
        for version in (("previous", "current") if "previous" in case else ("current",)):
            restored = restore_checkpoint(checkpoint, checkpoint_hash)
            docs = restored["documents"] + [document(fixture["documents"], case[version], "D2")]
            observation = read_documents(reader, rs, rt, rp, store, task, docs, restored["reader"]["rows"],
                                         case["id"] + "." + version)
            observation = apply_file_scope(observation, docs, scopes.get(case["id"]))
            reference = case["reference"][version]
            write_json({"checkpoint_sha256": checkpoint_hash, "documents": docs, "reader": observation},
                       str(folder / f"{version}.json"))
            row = {"case": case["id"], "version": version, "reader_valid": observation["rows"] is not None,
                   "reader": grade_reader(observation["rows"], reference),
                   "prefix": grade_reader(first["rows"], case["reference"]["prefix"]), "products": {}}
            arms = ARMS[index % len(ARMS):] + ARMS[:index % len(ARMS)] if version == "current" else ("controller",)
            for arm in arms:
                key = f"{case['id']}.{version}.{arm}"
                brief = {"controller": observation["rows"], "checked": reference_brief(reference), "documents": None}[arm]
                if arm == "controller" and brief is None:
                    row["products"][arm] = {"status": "unavailable_handoff", "judgments": 0}
                    continue
                if arm == "controller" and observation.get("file_scope_checks"):
                    brief = {"rows": brief, "file_scope_checks": observation["file_scope_checks"]}
                product = write_product(assembler, task, docs, brief, key, protocol_version)
                write_json(product, str(folder / f"{version}.{arm}.json"))
                labels = product["labels"]
                correct = sum(a == b[0] for a, b in zip(labels or [], reference))
                status = "fallback" if product["fallback"] else "scored" if labels else "labels_unreadable"
                row["products"][arm] = {"status": status, "judgments": correct, "input_sha256": product["messages_sha256"]}
                if not product["fallback"]:
                    answer = product["candidate"]["answer"]
                    body = "\n".join(answer.strip().splitlines()[1:]).strip() if labels else answer
                    blind.append((key, {"task": case["request"], "statements": case["statements"], "documents": docs, "body": body}))
            results.append(row)
        if stable_hash(read(checkpoint)) != checkpoint_hash:
            raise ValueError("A branch modified the original checkpoint")
        write_json(results, str(out / "measurements.json"))
        print(case["id"], flush=True)
    random.Random(531991).shuffle(blind)
    for i, (key, item) in enumerate(blind, 1):
        bid = f"H{i:02d}"
        item["blind_id"] = bid
        keys.append({"blind_id": bid, "key": key})
    write_json([item for _, item in blind], str(out / "notes-to-read.json"))
    write_json(keys, str(out / "bindings.json"))
    write_json(summarize(out), str(out / "summary.json"))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", help="New result directory")
    parser.add_argument("--protocol", choices=("v1", "v2", "v3"), default="v3",
                        help="Public docs. v2 reads labelled gaps and checks revisions; v3 retains extra gap fields.")
    parser.add_argument("--summarize", type=Path, help="Read an existing experiment without calling models")
    parser.add_argument("--review", type=Path, help="Completed blind reading for --summarize")
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(args.summarize, args.review), ensure_ascii=False, indent=2))
    else:
        if args.review:
            parser.error("--review requires --summarize")
        raise SystemExit(run(args))
