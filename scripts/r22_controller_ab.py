"""Compare joint and split drafting of library/archive record requests on R22 excerpts."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from r22_reading_ab import load_r22

from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, stable_hash
from framework.controller import _extract_json
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import build_gateway, resolve_routing


GRANT = {
    "record": "NSF CAREER",
    "fields": ["资格年限", "博士学位次序", "计时基准日"],
    "scope": "按本份材料的说法登记资助条件。如果索取计时基准日，请写清从该日向前计算的方向。",
}
PAPER = {
    "record": "Clough & Stevenson",
    "fields": ["期刊全名", "发表年份"],
    "scope": "仅按本份资料登记这篇短答案语料研究的书目信息，请以两位作者共同指明所查研究。",
}
SOURCE_WORDS = ("材料", "资料", "原文", "文献", "摘录", "档案", "文中", "记录", "出处", "文档")
BACKWARD_WORDS = ("向前", "往前", "向过去", "回溯", "追溯", "倒推", "倒算", "往回", "回推", "逆推")
PLAN = (
    "你在核对一张资料登记卡。根据这次提供的原文摘录判断，卡上哪些字段仍需向档案员索取。"
    "已经询问过的字段可能还没有收到答案。能从摘录读出的字段视为已获答。"
    "只输出一个 JSON 对象。decision 取 ask 或 done，fields 列出仍需索取的原字段名（齐备时为空数组），"
    "note 写一句给下一位同事的交接说明，保留有关的范围或限制。"
)
WORDING = (
    "用中文向该条目的档案员写一条可以单独阅读的问题。保留资料条目原名和登记卡口径。"
    "只把实际索取的字段原名放在【】里，每项一次。背景里提到的已有信息不加【】。"
)
JOINT = PLAN + WORDING + "同一对象另写 question 字段；decision 为 done 时其值为空字符串。"
SPLIT = PLAN + "输出交接说明即可，问句由下一位同事填写。"
WRITER = ("你负责把收到的资料条目和交接说明写成登记问题。" + WORDING +
          '只输出 {"question":"..."}。')
PARAMETERS = {"temperature": 0.0, "extra_body": {
    "chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": False}}}
METRICS = ("plan_correct", "requested_fields_correct", "request_correct")


def cases_from(evidence):
    def excerpt(eid, fragment):
        source = evidence[eid]["text"]
        if source.count(fragment) != 1:
            raise ValueError(f"Excerpt does not resolve to one location in {eid}")
        start = source.index(fragment)
        return {"evidence_id": eid, "start": start, "end": start + len(fragment), "text": fragment}

    heading = evidence["ev_195425d31526"]["text"]
    degree = "first doctoral degree within the ten years prior to the proposal deadline date"
    source = "ev_ecc075b94ca6"
    table = [
        ("grant_window", GRANT, excerpt("ev_195425d31526", heading), ["博士学位次序", "计时基准日"]),
        ("paper_title", PAPER, excerpt(source, "Language Resources and Evaluation"), ["发表年份"]),
        ("grant_complete", GRANT, excerpt("ev_40167322968c", degree), []),
        ("paper_year", PAPER, excerpt(source, "2011"), ["期刊全名"]),
        ("paper_complete", PAPER, excerpt(source, "Language Resources and Evaluation*, 2011"), []),
    ]
    return [{"id": key, "visible": {"card": card, "previously_requested": card["fields"], "received": obs},
             "expected": missing} for key, card, obs, missing in table]


def interpret_plan(obj, allowed):
    if not isinstance(obj, dict) or obj.get("decision") not in ("ask", "done"):
        return None
    fields = obj.get("fields")
    if not isinstance(fields, list) or not all(isinstance(f, str) and f in allowed for f in fields):
        return None
    if len(fields) != len(set(fields)) or bool(fields) != (obj["decision"] == "ask"):
        return None
    if not isinstance(obj.get("note"), str):
        return None
    return {"decision": obj["decision"], "fields": fields, "note": obj["note"]}


def evaluate(case, plan, wording):
    """Read field markers from the actual record request, separately from the plan."""
    expected = set(case["expected"])
    card = case["visible"]["card"]
    out = dict.fromkeys(METRICS + ("reasked_known", "missing_in_question", "source_scope", "backward_direction"))
    if plan is None:
        return {**out, "status": "invalid_plan"}
    out["plan_correct"] = set(plan["fields"]) == expected
    if not isinstance(wording, str):
        return {**out, "status": "missing_question"}
    wording = wording.strip()
    tags = re.findall("【([^【】]+)】", wording)
    if plan["decision"] == "done":
        if wording:
            return {**out, "status": "text_after_done"}
    elif (not tags or len(tags) != wording.count("【") or len(tags) != wording.count("】")
          or any(tag not in card["fields"] for tag in tags)):
        return {**out, "status": "unreadable_fields"}
    asked = set(tags)
    fields_ok = asked == expected and len(tags) == len(asked)
    scope = direction = None
    if wording:
        reference = ("nsf", "career") if card == GRANT else ("clough", "stevenson")
        scope = all(word in wording.casefold() for word in reference) and any(w in wording for w in SOURCE_WORDS)
        if "计时基准日" in asked:
            direction = any(word in wording for word in BACKWARD_WORDS) and not any(w in wording for w in ("向后", "往后"))
    return {**out, "status": "scored", "requested_fields_correct": fields_ok,
            "request_correct": fields_ok and scope is not False and direction is not False,
            "reasked_known": sorted(asked - expected), "missing_in_question": sorted(expected - asked),
            "source_scope": scope, "backward_direction": direction}


def invoke(gateway, store, part, arm, phase, instruction, visible, token_cap):
    payload = json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    msgs = [{"role": "user", "content": instruction + "\n" + payload}]
    attempt = f"{part}/{arm}/{phase}"
    result = gateway.call("controller", gateway.stage_id, msgs, {**PARAMETERS, "max_tokens": token_cap},
                          phase=Phase.ANALYSIS, attempt_id=attempt, cache_bypass=True)
    obj = _extract_json(result.text) if result.status == Status.OK else None
    request = next((e for e in store.events if e.type == "request_sent" and e.attempt_id == attempt), None)
    cost = next((e.cost for e in store.events if request and e.type == "budget_settled"
                 and e.call_id == request.call_id), {})
    return obj, {"phase": phase, "call_id": result.call_id, "dispatched": request is not None,
                 "status": result.status.value, "error": result.error, "messages_hash": stable_hash(msgs),
                 "input_chars": len(msgs[0]["content"]), "tokens_in": cost.get("tokens_in"),
                 "tokens_out": cost.get("tokens_out"), "wall_ms": cost.get("wall_ms")}


def run_arm(case, arm, gateway, store):
    started = monotonic()
    visible = case["visible"]
    joint = arm == "A"
    obj, call = invoke(gateway, store, case["id"], arm, "joint" if joint else "plan",
                       JOINT if joint else SPLIT, visible, 512 if joint else 256)
    calls = [call]
    plan = interpret_plan(obj, visible["card"]["fields"])
    question = obj.get("question") if joint and isinstance(obj, dict) else None
    if not joint and plan:
        if plan["decision"] == "done":
            question = ""
        else:
            # The writer gets the archive entry name and the actual handoff only.
            handoff = {"record": visible["card"]["record"], "plan": plan}
            written, call = invoke(gateway, store, case["id"], arm, "wording", WRITER, handoff, 256)
            calls.append(call)
            question = written.get("question") if isinstance(written, dict) else None
    return {"plan": plan, "question": question, "calls": calls, "grade": evaluate(case, plan, question),
            "elapsed_ms": round((monotonic() - started) * 1000)}


def summarize(pairs):
    by_arm = {}
    for arm in ("A", "B"):
        items = [p[arm] for p in pairs.values()]
        grades = [r["grade"] for r in items]
        sent = [call for r in items for call in r["calls"] if call["dispatched"]]
        totals = {}
        for key in ("tokens_in", "tokens_out", "wall_ms"):
            known = [c[key] for c in sent if c[key] is not None]
            totals[key] = sum(known) if len(known) == len(sent) else None
            totals[key + "_known_calls"] = len(known)
        by_arm[arm] = {"dispatches": len(sent), **totals, "elapsed_ms": sum(r["elapsed_ms"] for r in items),
                       "planned_cases": len(items), "scored_cases": sum(g["status"] == "scored" for g in grades),
                       "cases_reasking_known": sum(bool(g["reasked_known"]) for g in grades),
                       **{k: sum(g[k] is True for g in grades) for k in METRICS}}
    outcomes = {k: [] for k in ("both_correct", "only_A", "only_B", "both_missed", "unscored")}
    for key, pair in pairs.items():
        a, b = (pair[arm]["grade"]["request_correct"] for arm in ("A", "B"))
        if a is None or b is None:
            group = "unscored"
        elif a == b:
            group = "both_correct" if a else "both_missed"
        else:
            group = "only_A" if a else "only_B"
        outcomes[group].append(key)
    return {"by_arm": by_arm, "paired_requests": outcomes}


def run(args):
    identity, _, evidence = load_r22()
    cases = cases_from(evidence)
    routing = resolve_routing(_load_config(args))
    route = routing.route("controller")
    if route is None or not route.enabled:
        raise ValueError("Controller route is disabled")
    service = routing.service(route)
    if service.type == "mock" or service.parameter_profile != "qwen38_vllm":
        raise ValueError("This protocol uses the configured Qwen/vLLM Controller")
    root = Path(args.out or f"results/r22-controller-ab-{uuid4().hex[:10]}")
    root.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(root), identity["episode_id"], path=str(root / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=len(cases) * 3), store, identity["episode_id"])
    gateway = build_gateway(routing, "controller", guard, store)
    # Both arms use greedy decoding without thinking on this bounded form task.
    gateway.default_params = {}
    orders = {c["id"]: ("AB" if i % 2 == 0 else "BA") for i, c in enumerate(cases)}
    protocol = {"protocol": "r22-controller-archive-form.v1", "identity": identity, "cases": cases, "orders": orders,
                "instructions": {"joint": JOINT, "plan": SPLIT, "wording": WRITER},
                "params": {str(cap): gateway._merge_params({**PARAMETERS, "max_tokens": cap}) for cap in (256, 512)},
                "source_terms": SOURCE_WORDS, "backward_terms": BACKWARD_WORDS, "model": service.model,
                "service_id": service.service_id, "profile": service.parameter_profile, "stage": route.stage_id}
    write_json(protocol, str(root / "protocol.json"))
    print(f"R22 controller comparison: {root}", flush=True)
    pairs = {}
    for case in cases:
        key = case["id"]
        pairs[key] = {}
        for arm in orders[key]:
            result = run_arm(case, arm, gateway, store)
            write_json(result, str(root / f"{key}-{arm}.json"))
            pairs[key][arm] = result
        print(key, {a: pairs[key][a]["grade"]["request_correct"] for a in ("A", "B")}, flush=True)
    summary = summarize(pairs)
    roles = [e.role for e in store.events if e.type == "request_sent"]
    summary["calls"] = {r: roles.count(r) for r in ("controller", "target", "assembler", "judge_eval")}
    write_json(summary, str(root / "summary.json"))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if summary["paired_requests"]["unscored"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", help="New results directory (defaults to a unique path under results/)")
    sys.exit(run(parser.parse_args()))
