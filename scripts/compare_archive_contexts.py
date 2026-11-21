"""Compare three presentations of the same archived dispatch ledger on the local Assembler model."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from calibrate_review_roles import parameters
from framework.archive_replay import replay_run
from framework.assembly import LLMAssembler
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import build_gateway, resolve_routing

ROLES = ("controller", "target", "assembler")
ARMS = ("chronological", "grouped", "subtotals")
INSTRUCTION = """根据这份实验的调用账单，给研究同事写一条中文交接。每笔派发都计一次。只结算 controller、target、assembler 三个角色。
输出一个 JSON 对象。counts 是三个角色各自的整数次数，total 是三者之和，leaders 是并列第一的全部角色名数组，margin 是排序后第一名次数减第二名次数（并列时为零），note 是一句中文交接。分别填写各字段。角色名使用账单中的小写英文。
账单
"""


def render(dispatches, arm):
    if arm == "subtotals":
        counts = Counter(role for _, role in dispatches)
        lines = [f"{role} 合计 {counts[role]}" for role in ROLES]
    else:
        ordered = dispatches if arm == "chronological" else sorted(dispatches, key=lambda e: ROLES.index(e[1]))
        lines = [f"{seq} {role}" for seq, role in ordered]
    return [{"role": "user", "content": INSTRUCTION + "\n".join(lines)}]


def grade(output, counts):
    ranked = sorted(counts.values(), reverse=True)
    gold = {"counts": counts, "total": sum(ranked), "margin": ranked[0] - ranked[1],
            "leaders": sorted(role for role in ROLES if counts[role] == ranked[0])}
    out = output if isinstance(output, dict) else {}
    got = out.get("counts")
    leaders = out.get("leaders")
    checks = {
        "counts": isinstance(got, dict) and got == counts and all(type(i) is int for i in got.values()),
        "total": type(out.get("total")) is int and out["total"] == gold["total"],
        "margin": type(out.get("margin")) is int and out["margin"] == gold["margin"],
        "leaders": isinstance(leaders, list) and all(isinstance(r, str) for r in leaders) and sorted(leaders) == gold["leaders"],
    }
    return {**checks, "all_four": all(checks.values())}


def load_cases(source):
    archive = replay_run(source, at=["end"])
    cases = []
    for ep in archive["episodes"]:
        end = ep["checkpoints"]["end"]
        case = {k: ep[k] for k in ("episode_id", "exact_task_id", "duplicate_group_id", "archive_status")}
        if end:
            dispatches = []
            with (source / "episodes" / ep["episode_id"] / "events.jsonl").open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event["seq"] > end["through_seq"]:
                        break
                    if event["type"] == "request_sent" and event["role"] in ROLES:
                        dispatches.append((event["seq"], event["role"]))
            counts = {role: end["calls_by_role"].get(role, 0) for role in ROLES}
            if Counter(r for _, r in dispatches) != Counter({r: n for r, n in counts.items() if n}):
                raise ValueError(f"Source receipts disagree for {ep['episode_id']}")
            case.update(dispatches=dispatches, counts=counts,
                        message_hashes={a: stable_hash(render(dispatches, a)) for a in ARMS})
        cases.append(case)
    return cases


def run(args):
    source = Path(args.root)
    cases = load_cases(source)
    routing = resolve_routing(_load_config(args))
    route = routing.route("assembler")
    if not route or not route.enabled:
        raise ValueError("Assembler route unavailable")
    service = routing.service(route)
    if (service.type == "mock" or service.parameter_profile != "qwen38_vllm"
            or urlsplit(service.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("Expected the local Qwen vLLM service")
    root = Path(args.out or f"results/archive-context-comparison-{uuid4().hex[:10]}")
    root.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(root), root.name, path=str(root / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=len(ARMS) * len(cases)), store, root.name)
    gateway = build_gateway(routing, "assembler", guard, store)
    gateway.default_params = {}
    params = parameters(service.parameter_profile)
    protocol = {"source": str(source), "model": service.model, "prompt": INSTRUCTION,
                "sent_params": gateway._merge_params(params), "arms": ARMS, "cases": cases,
                "order": "start at cohort position modulo 3 and cycle through arms",
                "endpoints": ["counts", "total", "leaders", "margin"], "planned_per_arm": len(cases)}
    write_json(protocol, str(root / "protocol.json"))
    print(root, flush=True)
    rows = []
    for i, case in enumerate(cases):
        for arm in ARMS[i % len(ARMS):] + ARMS[:i % len(ARMS)]:
            row = {"episode_id": case["episode_id"], "arm": arm, "status": case["archive_status"]}
            if "dispatches" in case:
                packet = render(case["dispatches"], arm)
                result = gateway.call("assembler", route.stage_id, packet, params, phase=Phase.ANALYSIS,
                                      attempt_id=f"{case['episode_id']}.{arm}", cache_bypass=True)
                events = [e for e in store.events if e.call_id == result.call_id]
                sent = next((e for e in events if e.type == "request_sent"), None)
                if sent and (stable_hash(sent.payload["messages"]) != case["message_hashes"][arm]
                             or sent.payload["sent_params"] != protocol["sent_params"]):
                    raise ValueError("Dispatched input differs from frozen protocol")
                cost = next((e.cost for e in events if e.type == "budget_settled"), {})
                output = LLMAssembler._extract_json(result.text) if sent and result.status == Status.OK else None
                row.update(status=result.status.value, dispatched=bool(sent), call_id=result.call_id,
                           cost=cost, output=output, checks=grade(output, case["counts"]))
            rows.append(row)
            write_json(row, str(root / f"{case['episode_id']}.{arm}.json"))
    summary = {"planned_per_arm": len(cases), "arms": {}}
    for arm in ARMS:
        group = [r for r in rows if r["arm"] == arm]
        usage = [r.get("cost", {}) for r in group if r.get("dispatched")]
        result = {k: sum(bool(r.get("checks", {}).get(k)) for r in group) for k in (*protocol["endpoints"], "all_four")}
        for key in ("tokens_in", "tokens_out"):
            result[key] = sum(c[key] for c in usage) if usage and all(type(c.get(key)) is int for c in usage) else None
        result["dispatched"] = len(usage)
        summary["arms"][arm] = result
    write_json(summary, str(root / "summary.json"))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Original run containing plan.json and episodes/")
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", help="New experiment directory")
    run(parser.parse_args())
