"""From one saved CPython note, compare immediate writing with optional draft revision."""
import argparse
import json
from pathlib import Path

from compare_archive_contexts import local_analysis_gateway
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, stable_hash
from framework.events import EventStore
from framework.python_release_notes import ITEMS, InvalidDocumentDecision, api_entries, apply_notice, chapter_sources, restore
from framework.routing import resolve_routing
from study_document_handoffs import REPO, RepositoryBriefAssembler, load_template, read, role_usage, write_product
from study_python_releases import FIXTURE, ask, new_prefixes, review_map, save, task_at, write_blind_packet


CASE = REPO / "data/samples/python-sample-note-v1.json"


def journal(folder, limits):
    folder.mkdir(exist_ok=False)
    store = EventStore(str(folder), folder.name, path=str(folder / "events.jsonl"))
    return store, BudgetGuard(BudgetLimits(**limits), store, folder.name)


def local_role(routing, role, store, guard):
    gateway, stage, model, params = local_analysis_gateway(routing, role, guard, store)
    gateway.default_params = {**params, "max_tokens": 1024 if role == "controller" else 1536}
    name = "plan.python-release-note.zh.v1" if role == "controller" else "assemble.repository-brief.zh.v1"
    template = load_template(stage, role, name)
    return gateway, template, {"model": model, "params": gateway._merge_params({}), "template": template.ref()}


def run_pair(out, case, doc, routing):
    out.mkdir(parents=True, exist_ok=False)
    docs = [doc]
    release = case["release"]
    shared = out / "shared"
    store, guard = journal(shared, {"target_calls": 0, "non_target_calls": 1})
    reader, template, reader_ref = local_role(routing, "controller", store, guard)
    exchange = {"question": case["request"], "reply": case["fixed_reply"], "origin": case["reply_origin"]}
    receipts = {}
    roles = {}
    # Freeze both sets of budgets, model settings and templates before the first dispatch.
    for arm in case["arms"]:
        arm_store, arm_guard = journal(out / arm, case["per_arm_limits"])
        arm_reader = local_role(routing, "controller", arm_store, arm_guard)
        arm_writer = local_role(routing, "assembler", arm_store, arm_guard)
        roles[arm] = (arm_reader, arm_writer)
        receipts[arm] = {"controller": arm_reader[2], "assembler": arm_writer[2]}
    if receipts["direct"] != receipts["optional_review"]:
        raise ValueError("The paired model settings differ")
    save(out / "protocol.json", {"fixture": case, "source": doc, "source_sha256": stable_hash(docs),
                                 "exchange": exchange, "exchange_sha256": stable_hash(exchange),
                                 "shared_reader": reader_ref, "arm_roles": receipts,
                                 "initial_assignment": ["P1", "P2"], "initial_pending": "P3"})
    initial = ask(reader, template, case, release, docs, ITEMS[:2], [], "draft", "shared")
    save(shared / "reading.json", initial)
    if initial["rows"] is None:
        save(out / "result.json", {"status": "unavailable_checkpoint", "arms": {}})
        write_blind_packet(out, [], 31457)
        return
    draft = [*initial["rows"], {"id": "P3", "value": "U", "sources": []}]
    checkpoints = new_prefixes(shared, draft)
    save(shared / "checkpoints.json", checkpoints)
    # The question and the replayed reply are the same in both arms and add no API facts.
    after = {**case, "request": case["request"] + "\n以上问题收到的固定回复为「" + case["fixed_reply"] + "」。"}
    blind, results = [], {}
    for arm in case["arms"]:
        folder = out / arm
        original = restore(shared, checkpoints["after_P3"])
        rows = original
        result = {"start_sha256": stable_hash(original), "source_sha256": stable_hash(docs),
                  "exchange_sha256": stable_hash(exchange), "status": "started", "rolled_back": False}
        results[arm] = result
        (controller, rt, _), (gateway, wt, _) = roles[arm]
        if arm == "optional_review":
            decision = ask(controller, rt, after, release, docs, ITEMS, original, "review", "reread")
            save(folder / "decision.json", decision)
            if decision["rows"] is None:
                result["status"] = "invalid_reading"
                continue
            try:
                applied, edits = apply_notice(shared, checkpoints, decision["action"], decision["restore_to"], decision["rows"])
            except InvalidDocumentDecision:
                result["status"] = "invalid_decision"
                continue
            result.update(action=decision["action"], anchor=decision["restore_to"], edits=edits,
                          rolled_back=decision["action"] == "revise")
            # A keep must give the writer the byte-identical original handoff.
            if result["rolled_back"]:
                prefix = restore(shared, checkpoints[decision["restore_to"]])
                rows = [*prefix, *applied[len(prefix):]]
        handoff = {"current_release": release, "rows": rows}
        writer = RepositoryBriefAssembler(gateway, wt, stage_id=wt.stage_id, phase=Phase.ANALYSIS,
                                          preserve_gap_details=True, max_format_retries=0)
        product = write_product(writer, task_at(after, release), docs, handoff, arm, "python-sample.v1")
        save(folder / "product.json", product)
        result.update(status="fallback" if product["fallback"] else "written",
                      labels=product["labels"], handoff_sha256=product["handoff_sha256"],
                      final_rows=[r["value"] for r in rows], candidate_id=product["candidate"]["candidate_id"])
        if not product["fallback"]:
            answer = product["candidate"]["answer"]
            body = "\n".join(answer.strip().splitlines()[1:]).strip() if product["labels"] else answer
            blind.append((case["id"], arm, {"request": case["request"], "requirements": case["primary"]["requirements"],
                                            "source": docs, "body": body}))
    save(out / "result.json", {"status": "finished", "arms": results})
    write_blind_packet(out, blind, 31457)


def summary(out, review=None):
    p, results = read(out / "protocol.json"), read(out / "result.json")
    case = p["fixture"]
    scores = review_map(out, review) if review else {}
    result = {}
    for arm in case["arms"]:
        row = results["arms"].get(arm, {"status": results["status"]})
        labels_ok = row.get("labels") == case["primary"]["reference"]
        prose = scores.get((case["id"], arm), False) if review else None
        result[arm] = {**row, "judgments_correct": labels_ok, "prose_usable": prose,
                       "usable_product": (row["status"] == "written" and labels_ok and prose) if review else None,
                       "usage": role_usage(out / arm) if (out / arm / "events.jsonl").is_file() else {}}
    return {"planned": len(case["arms"]), "shared_usage": role_usage(out / "shared"), "arms": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=Path, metavar="NEW_DIRECTORY")
    mode.add_argument("--summarize", type=Path, metavar="RUN_DIRECTORY")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--cache", type=Path, default=Path("results/cpython-docs-310-311"))
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summary(args.summarize, args.review), ensure_ascii=False, indent=2))
        return
    if args.review:
        parser.error("--review requires --summarize")
    case, upstream = read(CASE), read(FIXTURE)
    tag, module = case["release"], case["module"]
    sources = chapter_sources(upstream, args.cache)
    entries = api_entries(sources[tag, module], module, tag, upstream["releases"][tag]["revision"])
    [doc] = [e for e in entries if e["symbol"] == case["symbol"]]
    run_pair(args.run, case, doc, resolve_routing(_load_config(args)))
    print(args.run)


if __name__ == "__main__":
    main()
