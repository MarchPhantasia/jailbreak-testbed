"""Run the five frozen CPython maintenance tasks, from initial note through revision and publication."""
import argparse
import json
import random
import subprocess
from collections import Counter
from pathlib import Path
from uuid import uuid4

from compare_archive_contexts import local_analysis_gateway
from compare_repository_handoff_size import usage_by_call
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, Task, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.python_release_notes import (
    ANCHORS, ITEMS, InvalidDocumentDecision, api_entries, apply_notice, chapter_sources, chapter_window, checkpoint,
    required_anchor, restore, select_documentation,
)
from framework.routing import resolve_routing
from study_document_handoffs import (
    REPO, RepositoryBriefAssembler, canonical_brief, load_template, read, role_usage, write_product,
)

FIXTURE = REPO / "data/samples/python-release-notes-v1.json"
ARMS = ("full", "automatic")


def save(path, value):
    write_json(value, str(path))


def values(rows):
    return [r["value"] for r in rows]


def task_at(case, tag):
    items = "\n".join(f"{i} {statement}" for i, statement in zip(ITEMS, case["statements"], strict=True))
    return Task(case["id"], f"当前工作环境为 CPython {tag}。{case['request']}\n{items}", "python_release_note")


def case_documents(case, fixture, sources):
    chapters, windows = {}, {}
    module = case["module"]
    for tag, spec in fixture["releases"].items():
        chapters[tag] = api_entries(sources[tag, module], module, tag, spec["revision"])
        windows[tag] = chapter_window(chapters[tag], case["baseline_topic"])
    return chapters, windows


def ask(gateway, template, case, tag, docs, assigned, prior, mode, attempt):
    payload = {"mode": mode, "running_python": tag, "task": task_at(case, tag).public_view(),
               "assigned": list(assigned), "committed": prior, "documents": docs}
    messages = [{"role": "user", "content": template.render(payload)}]
    visible = {e for d in docs for e in d["evidence_ids"]}
    reply = gateway.call("controller", template.stage_id, messages, {}, phase=Phase.ANALYSIS,
                         cache_bypass=True, input_evidence_ids=sorted(visible), template_ref=template.ref(),
                         attempt_id=case["id"] + "." + attempt)
    sent = [e for e in gateway.store.events if e.type == "request_sent" and e.call_id == reply.call_id]
    if sent and (len(sent) != 1 or sent[0].payload["messages"] != messages):
        raise ValueError("Recorded reader dispatch differs from the requested messages")
    raw = RepositoryBriefAssembler._extract_json(reply.text) if sent and reply.status == Status.OK else None
    result = raw if isinstance(raw, dict) else {}
    return {"rows": canonical_brief(result, visible, assigned), "reason": result.get("reason"),
            "action": result.get("action"), "restore_to": result.get("restore_to"),
            "call_id": reply.call_id, "status": reply.status.value, "dispatched": bool(sent),
            "messages_sha256": stable_hash(messages), "visible_ids": sorted(visible)}


def new_prefixes(folder, rows):
    return {name: checkpoint(folder, name, rows[:length]) for length, name in enumerate(ANCHORS)}


def run_case(case, fixture, chapters, windows, out, reader, template, writer, arms, blind):
    key = case["id"]
    folder = out / key
    folder.mkdir()
    policy = fixture["protocol"]
    old, current = policy["initial"], policy["upgrade"]
    row = {"id": key, "initial": [], "notices": [], "products": {}, "status": "started"}
    active = folder / "initial"
    receipts = {"start": checkpoint(active, "start", [])}
    committed = []
    save(folder / "source-windows.json", windows)
    for index, item in enumerate(ITEMS, 1):
        draft = ask(reader, template, case, old, windows[old], [item], committed, "draft", item)
        save(active / (item + "-reading.json"), draft)
        if draft["rows"] is None:
            return {**row, "status": "invalid_initial_reading", "at": item}
        committed.extend(draft["rows"])
        receipts[ANCHORS[index]] = checkpoint(active, ANCHORS[index], committed)
        row["initial"].append({"item": item, "value": committed[-1]["value"], "call_id": draft["call_id"]})
    initial_checkpoint = receipts[ANCHORS[-1]]
    notices = ([old] if key == policy["duplicate_notice_case"] else []) + [current]
    for index, tag in enumerate(notices, 1):
        name = f"notice-{index}"
        destination = folder / name
        destination.mkdir()
        docs = windows[old] if tag == old else windows[old] + windows[tag]
        before = restore(active, receipts[ANCHORS[-1]])
        decision = ask(reader, template, case, tag, docs, ITEMS, before, "review", name)
        save(destination / "decision.json", decision)
        notice = {"notice": name, "tag": tag, "before": values(before), "call_id": decision["call_id"],
                  "action": decision["action"], "restore_to": decision["restore_to"], "reason": decision["reason"],
                  "previous_timeline": active.name, "source_hash": stable_hash(docs)}
        row["notices"].append(notice)
        if decision["rows"] is None:
            return {**row, "status": "invalid_notice_reading"}
        try:
            committed, edits = apply_notice(active, receipts, decision["action"], decision["restore_to"], decision["rows"])
        except InvalidDocumentDecision:
            return {**row, "status": "invalid_notice_action"}
        notice.update(after=values(committed), proposed=values(decision["rows"]), edits=edits,
                      restored_checkpoint=receipts[decision["restore_to"]])
        save(destination / "transition.json", notice)
        receipts = new_prefixes(destination, committed)
        active = destination
    restore(folder / "initial", initial_checkpoint)
    handoff = {"current_release": current, "rows": restore(active, receipts[ANCHORS[-1]])}
    save(folder / "selected-handoff.json", handoff)
    full = windows[old] + windows[current]
    selected, row["selection"] = select_documentation(case["request"], [chapters[old], chapters[current]], full)
    for arm in arms:
        docs = full if arm == "full" else selected
        result = write_product(writer, task_at(case, current), docs, handoff, key + "." + arm, "python-release.v1")
        save(folder / (arm + ".json"), result)
        row["products"][arm] = {"labels": result["labels"], "fallback": result["fallback"],
                                "calls": result["calls"], "candidate_id": result["candidate"]["candidate_id"],
                                "input_sha256": result["messages_sha256"], "handoff_sha256": result["handoff_sha256"],
                                "included": [d["evidence_ids"][0] for d in docs]}
        if not result["fallback"]:
            answer = result["candidate"]["answer"]
            body = "\n".join(answer.strip().splitlines()[1:]).strip() if result["labels"] else answer
            blind.append((key, arm, {"request": case["request"], "requirements": case["prose_requirements"],
                                     "source": [d for d in full if d["symbol"] == case["baseline_topic"]], "body": body}))
    return {**row, "status": "finished"}


def run(args, fixture, sources):
    out = args.out or Path("results") / ("python-release-notes-" + uuid4().hex[:10])
    prepared = {c["id"]: case_documents(c, fixture, sources) for c in fixture["cases"]}
    out.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(out), out.name, path=str(out / "events.jsonl"))
    limit = 6 * len(fixture["cases"]) + 1
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=limit), store, out.name)
    routes = resolve_routing(_load_config(args))
    reader, stage, reader_model, reader_params = local_analysis_gateway(routes, "controller", guard, store)
    creator, wstage, writer_model, writer_params = local_analysis_gateway(routes, "assembler", guard, store)
    reader.default_params = {**reader_params, "max_tokens": 1024}
    creator.default_params = {**writer_params, "max_tokens": 1536}
    rt = load_template(stage, "controller", "plan.python-release-note.zh.v1")
    wt = load_template(wstage, "assembler", "assemble.repository-brief.zh.v1")
    writer = RepositoryBriefAssembler(creator, wt, preserve_gap_details=True, stage_id=wstage,
                                      max_format_retries=0, phase=Phase.ANALYSIS)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, capture_output=True)
    save(out / "protocol.json", {"fixture": fixture, "fixture_sha256": stable_hash(fixture), "budget": guard.limits.to_dict(),
                                 "commit": commit.stdout.strip() if commit.returncode == 0 else None,
                                 "roles": {"controller": [reader_model, reader._merge_params({}), rt.text, rt.sha256],
                                           "assembler": [writer_model, creator._merge_params({}), wt.text, wt.sha256]}})
    measurements, blind = [], []
    print(out, flush=True)
    for idx, case in enumerate(fixture["cases"]):
        row = run_case(case, fixture, *prepared[case["id"]], out, reader, rt, writer,
                       ARMS if idx % 2 == 0 else tuple(reversed(ARMS)), blind)
        measurements.append(row)
        save(out / case["id"] / "episode.json", row)
        save(out / "measurements.json", measurements)
        print(case["id"], flush=True)
    random.Random(173106).shuffle(blind)
    cards, bindings = [], []
    for i, (key, arm, card) in enumerate(blind, 1):
        bid = f"M{i:02d}"
        cards.append({**card, "blind_id": bid})
        bindings.append({"blind_id": bid, "case": key, "arm": arm})
    save(out / "notes-to-read.json", cards)
    save(out / "bindings.json", bindings)
    save(out / "summary.json", summarize(out))


def review_map(out, path):
    cards, review = read(out / "notes-to-read.json"), read(path)
    bindings = read(out / "bindings.json")
    ids = {c["blind_id"] for c in cards}
    if (review["packet_sha256"] != stable_hash(cards) or set(review["notes"]) != ids
            or {b["blind_id"] for b in bindings} != ids or len(bindings) != len(ids)
            or any(n.get("rating") not in {"usable", "needs_edit"} for n in review["notes"].values())):
        raise ValueError("The reading does not match the frozen packet")
    return {(b["case"], b["arm"]): review["notes"][b["blind_id"]]["rating"] == "usable" for b in bindings}


def summarize(out, review=None):
    fixture = read(out / "protocol.json")["fixture"]
    policy = fixture["protocol"]
    ratings = review_map(out, review) if review else {}
    measured = {r["id"]: r for r in read(out / "measurements.json")}
    costs = usage_by_call(out)
    product_stats = {arm: Counter() for arm in ARMS}
    transitions, summaries = Counter(), []
    for case in fixture["cases"]:
        key = case["id"]
        r = measured.get(key, {})
        current = case["reference"][policy["upgrade"]]
        notices = r.get("notices", [])
        for n in notices:
            correct = case["reference"][n["tag"]]
            expected = required_anchor(n["before"], correct)
            right_action = n["action"] == ("keep" if expected == ANCHORS[-1] else "revise")
            transitions["action_correct"] += right_action
            transitions["anchor_correct"] += right_action and n["restore_to"] == expected
            transitions["applied_three_correct"] += n.get("after") == correct
        last = notices[-1] if notices and notices[-1]["tag"] == policy["upgrade"] else {}
        summary = {"task": key, "status": r.get("status", "missing"), "initial": [d["value"] for d in r.get("initial", [])],
                   "action": last.get("action"), "anchor": last.get("restore_to"), "handoff": last.get("after"),
                   "expected_anchor_from_actual_draft": required_anchor(last["before"], current) if last else None,
                   "selection": r.get("selection"), "products": {}}
        for arm in ARMS:
            st = product_stats[arm]
            prod = r.get("products", {}).get(arm, {})
            labels = prod.get("labels") or []
            all_right = labels == current
            usable = ratings.get((key, arm), False)
            st["planned"] += 1
            st["correct_judgments"] += sum(x == y for x, y in zip(labels, current))
            st["all_three"] += all_right
            st["delivered"] += bool(prod) and not prod.get("fallback", True)
            if review:
                st["prose_usable"] += usable
                st["usable_product"] += usable and all_right
            for call in prod.get("calls", []):
                for field in ("tokens_in", "tokens_out"):
                    count = costs.get(call, {}).get(field)
                    st[field + "_unknown_calls"] += count is None
                    if count is not None:
                        st[field] += count
            summary["products"][arm] = {"labels": labels, "prose": usable if review else None}
        summaries.append(summary)
    return {"planned_tasks": len(fixture["cases"]), "planned_notices": len(fixture["cases"]) + 1,
            "transitions": dict(transitions), "products": product_stats, "cases": summaries, "usage": role_usage(out)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run", action="store_true")
    action.add_argument("--fetch-sources", action="store_true")
    action.add_argument("--summarize", type=Path)
    parser.add_argument("--cache", type=Path, default=Path("results/cpython-docs-310-311"))
    parser.add_argument("--review", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(args.summarize, args.review), ensure_ascii=False, indent=2))
        return
    if args.review:
        parser.error("--review requires --summarize")
    fixture = read(FIXTURE)
    sources = chapter_sources(fixture, args.cache, fetch=args.fetch_sources)
    if args.run:
        run(args, fixture, sources)
    else:
        print(f"Verified {len(sources)} upstream chapters in {args.cache}")


if __name__ == "__main__":
    main()
