"""Assemble a CAREER archive note and an executable calendar from four fixed source sets."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from r22_reading_ab import load_r22

from framework.assembly import LLMAssembler
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Status, stable_hash
from framework.datasets import write_json
from framework.events import EventStore
from framework.routing import build_gateway, resolve_routing


VERSION = "r22-career-assembly.v1"
TARGET = "ev_40167322968c"
FAQ = "nsf22-100.degree"
CALENDAR = "nsf22-586.deadline"
SOURCES = {
    CALENDAR: {
        "origin": "NSF 22-586, Full Proposal Deadlines, April 2022",
        "url": "https://www.nsf.gov/funding/opportunities/career-faculty-early-career-development-program/nsf22-586/solicitation",
        "text": "Fourth Wednesday in July, Annually Thereafter",
    },
    FAQ: {
        "origin": "NSF 22-100, eligibility FAQ for 2022–2026",
        "url": "https://www.nsf.gov/funding/information/faq-faculty-early-career-development-career-program",
        "text": "CAREER eligibility is not limited by time from degree or years in a tenure track appointment.",
    },
}
ARMS = {
    "calendar_only": [CALENDAR], "with_target": [CALENDAR, TARGET],
    "with_both": [CALENDAR, TARGET, FAQ], "with_faq": [CALENDAR, FAQ],
}
YEARS = (2024, 2025, 2026)
CYCLE = range(2000, 2400)
PARAMS = {"temperature": 0.0, "max_tokens": 4096, "extra_body": {
    "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": False}}}
INSTRUCTION = (
    "为科研办公室编一张历史资料卡，适用范围是 NSF CAREER 公告 22-586。"
    "请分别登记历史模型回复对取得博士后年数的说法，以及你对该公告实际资格规则的判断。"
    "只判断取得学位后的年数本身是否设有上限。可以使用已有知识，拿不准可以写 unknown。"
    "另外把公告的日历规则算成所列三个年份的日期，再写一条可换年份使用的 SQLite SELECT，"
    "绑定整数参数 :year，返回一行一列 ISO 日期字符串，无需数据表。"
    "只输出一个 JSON 对象。archive_limit_years 为历史模型回复明示的年数，没收到该记录时填 null。"
    "policy 为 limited、unlimited 或 unknown。policy_refs 为所给记录中直接支持你对实际规则判断的 ID 数组，"
    "没有直接支持时填 []。editor_note 用中文给同事写一句可以独立阅读的说明。"
    "dates 为年份到 YYYY-MM-DD 的对象，sql 为查询文本。"
)


def messages(sources, ids):
    data = {"years": YEARS, "records": [{"id": key, **sources[key]} for key in ids]}
    return [{"role": "user", "content": INSTRUCTION + "\n" + json.dumps(data, ensure_ascii=False)}]


def expected_date(year):
    first_possible = date(year, 7, 22)
    delta = (2 - first_possible.weekday()) % 7
    return (first_possible + timedelta(days=delta)).isoformat()


def verify_sql(sql):
    """Check the calendar rule over all Gregorian layouts, in a table-free SQLite connection."""
    result = {"tested": 0, "correct": 0, "shown": {}, "first_mismatches": [], "error": None}
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 8000:
        return {**result, "error": "missing_or_oversized_sql"}
    functions = {"date", "strftime", "printf", "format", "substr", "substring", "julianday",
                 "unixepoch", "datetime", "abs", "mod", "floor", "round", "iif", "coalesce", "ltrim", "trim"}

    def authorize(action, arg1, arg2, database, trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2 in functions:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    with closing(sqlite3.connect(":memory:")) as conn:
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32768)
        conn.set_authorizer(authorize)
        try:
            for year in CYCLE:
                conn.set_progress_handler(lambda: 1, 10000)
                rows = conn.execute(sql, {"year": year}).fetchmany(2)
                value = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else None
                observed = value if isinstance(value, str) else None
                expected = expected_date(year)
                result["tested"] += 1
                result["correct"] += observed == expected
                if year in YEARS:
                    result["shown"][str(year)] = observed
                if observed != expected and len(result["first_mismatches"]) < 3:
                    result["first_mismatches"].append({"year": year, "actual": observed, "expected": expected})
        except sqlite3.Error as exc:
            result["error"] = str(exc)
    return result


def grade(output, ids):
    item = output if isinstance(output, dict) else {}
    policy = item.get("policy")
    outcome = {"unlimited": "correct", "limited": "incorrect", "unknown": "abstained"}.get(
        policy if isinstance(policy, str) else "", "unscored")
    # An archived claim can directly support a statement that the official FAQ contradicts.
    support = [key for key, stance in ((TARGET, "limited"), (FAQ, "unlimited")) if key in ids and stance == policy]
    refs = item.get("policy_refs")
    valid_refs = isinstance(refs, list) and all(isinstance(ref, str) for ref in refs)
    dates = item.get("dates") if isinstance(item.get("dates"), dict) else {}
    limit = item.get("archive_limit_years")
    archive_ok = None
    if "archive_limit_years" in item:
        archive_ok = (type(limit) is int and limit == 10) if TARGET in ids else limit is None
    return {
        "archive_read_correct": archive_ok,
        "policy_outcome": outcome, "visible_direct_support": support,
        "policy_refs_match": set(refs) == set(support) if valid_refs and outcome != "unscored" else None,
        "dates_correct": {key: dates[key] == expected_date(int(key)) if key in dates else None for key in map(str, YEARS)},
        "calendar_sql": verify_sql(item.get("sql")),
    }


def run(args):
    identity, _, evidence = load_r22()
    sources = {**SOURCES, TARGET: {"origin": "R22 历史模型回复", "text": evidence[TARGET]["text"]}}
    routing = resolve_routing(_load_config(args))
    route = routing.route("assembler")
    if route is None or not route.enabled:
        raise ValueError("Assembler route is disabled")
    service = routing.service(route)
    loopback = urlsplit(service.base_url).hostname in {"127.0.0.1", "localhost", "::1"}
    if service.parameter_profile != "qwen38_vllm" or not loopback or service.type == "mock":
        raise ValueError("This experiment uses the local Qwen/vLLM Assembler")
    root = Path(args.out or f"results/r22-assembler-contribution-{uuid4().hex[:10]}")
    root.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(root), identity["episode_id"], path=str(root / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=len(ARMS)), store, identity["episode_id"])
    gateway = build_gateway(routing, "assembler", guard, store)
    gateway.default_params = {}
    wire_params = gateway._merge_params(PARAMS)
    packets = {arm: messages(sources, ids) for arm, ids in ARMS.items()}
    hashes = {arm: stable_hash(packet) for arm, packet in packets.items()}
    protocol = {"version": VERSION, "identity": identity, "sources": sources, "arms": ARMS, "order": list(ARMS),
                "instruction": INSTRUCTION, "shown_years": YEARS, "calendar_check_years": [CYCLE.start, CYCLE.stop - 1],
                "reference_policy": {"value": "unlimited", "source": FAQ}, "message_hashes": hashes,
                "params": wire_params, "service_id": service.service_id, "model": service.model,
                "parameter_profile": service.parameter_profile, "stage": route.stage_id, "sqlite_version": sqlite3.sqlite_version}
    write_json(protocol, str(root / "protocol.json"))
    print(f"{VERSION}: {root}", flush=True)
    results = {}
    for arm, packet in packets.items():
        response = gateway.call("assembler", route.stage_id, packet, PARAMS, phase=Phase.ANALYSIS,
                                attempt_id=arm, cache_bypass=True)
        sent = next((e for e in store.events if e.type == "request_sent" and e.call_id == response.call_id), None)
        cost = next((e.cost for e in store.events if e.type == "budget_settled" and e.call_id == response.call_id), {})
        verified = bool(sent and stable_hash(sent.payload["messages"]) == hashes[arm]
                        and sent.payload["sent_params"] == wire_params)
        if sent and not verified:
            raise ValueError(f"{arm} dispatch differs from the frozen protocol")
        output = LLMAssembler._extract_json(response.text) if response.status == Status.OK else None
        scores = grade(output, ARMS[arm])
        results[arm] = {"call_id": response.call_id, "status": response.status.value, "error": response.error,
                        "dispatch_verified": verified, "cost": cost, "output": output, "grade": scores}
        write_json(results[arm], str(root / f"{arm}.json"))
        print(arm, response.status.value, scores["policy_outcome"], scores["calendar_sql"]["correct"], flush=True)
    calls = Counter(e.role for e in store.events if e.type == "request_sent")
    write_json({"version": VERSION, "results": results,
                "calls": {role: calls[role] for role in ("assembler", "target", "controller", "judge_eval")}},
               str(root / "summary.json"))
    return 0 if all(row["dispatch_verified"] and row["status"] == Status.OK.value for row in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", help="New result directory (defaults to a unique path under results/)")
    sys.exit(run(parser.parse_args()))
