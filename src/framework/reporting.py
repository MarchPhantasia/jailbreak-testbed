"""Episode-level accounting for a completed evaluation."""
from decimal import Decimal, ROUND_HALF_UP


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    value = Decimal(numerator) * 100 / denominator
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def episode_accounting(planned: int, metrics: dict) -> dict:
    """Keep the planned and scored denominators distinct from task-weighted ASR."""
    counts = {key: metrics.get(key) for key in ("N", "S", "F", "U")}
    card = {"unit": "episode", "planned": planned, "counts": counts, "scored": None, "percent": None}
    if all(value is None for value in counts.values()):
        return {**card, "status": "not_available"}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return {**card, "status": "incomplete_counts"}
    n, success, failure, unknown = (counts[key] for key in ("N", "S", "F", "U"))
    if n != planned or n != success + failure + unknown:
        return {**card, "status": "counts_disagree"}
    scored = success + failure
    return {**card, "status": "reconciled" if n else "empty_plan", "scored": scored,
            "percent": {"lower": _percent(success, n), "upper": _percent(success + unknown, n),
                        "scored_only": _percent(success, scored)}}


def overlap_bounds(n: int, left: int, right: int, *, left_only: int | None = None,
                   neither: int | None = None) -> dict:
    """Feasible count in both sets on a common, frozen list of n positions."""
    if any(type(count) is not int or count < 0 for count in (n, left, right)) or max(left, right) > n:
        raise ValueError("Invalid population or marginal totals")
    for count in (left_only, neither):
        if count is not None and (type(count) is not int or not 0 <= count <= n):
            raise ValueError("Paired counts must be within the common population")
    lower, upper = max(0, left + right - n), min(left, right)
    for value in (left - left_only if left_only is not None else None,
                  left + right - n + neither if neither is not None else None):
        if value is not None:
            lower, upper = max(lower, value), min(upper, value)
    if lower > upper:
        raise ValueError("Paired counts conflict with the marginal totals")
    return {"lower": lower, "upper": upper}


# ---------------------------------------------------------------------------
# 三题对账导出（2026-11-22 归因修复）：直问 vs 完整流程 vs 独立 Judge

import json as _json
import os as _os
import re as _re
from pathlib import Path
# 只读聚合：不改运行产物；旧档案缺关联字段时按事件序/父子关系重建并标注。
# ---------------------------------------------------------------------------

def _stable_hash_messages(messages) -> str:
    from .contracts import stable_hash
    return stable_hash([{"role": m.get("role", ""), "content": m.get("content", "")}
                        for m in (messages or [])])


def reconstruct_episode_attribution(events) -> dict:
    """从事件流重建 Target 交互的分支归属（新旧档案通用）。

    新档案：node_created.payload 带 call_id/response_event_id（直接绑定）；
    旧档案：按事件序重建——每次 target 调用的观察子节点 =
    该响应事件之后首条 parent=发送节点 的 node_created。
    """
    reqs = [(i, e) for i, e in enumerate(events)
            if e.get("type") == "request_sent" and (e.get("role") or "") == "target"]
    resp_idx = {e.get("call_id"): i for i, e in enumerate(events)
                if e.get("type") == "response_received"}
    nodes = [(i, e) for i, e in enumerate(events) if e.get("type") == "node_created"]
    child2parent = {}
    for _, e in nodes:
        child2parent[e.get("node_id") or ""] = (e.get("payload") or {}).get("parent")
    call2child = {}
    for _, e in nodes:
        cid = (e.get("payload") or {}).get("call_id")
        if cid:
            call2child[cid] = e.get("node_id") or ""
    rounds = []
    for order, (i, r) in enumerate(reqs, 1):
        call = r.get("call_id") or ""
        node = r.get("node_id") or ""
        child = call2child.get(call)
        rebuilt = False
        if not child:
            ri = resp_idx.get(call, -1)
            for j, e in nodes:
                if j > ri and (e.get("payload") or {}).get("parent") == node:
                    child = e.get("node_id") or ""
                    break
            rebuilt = True
        rounds.append({"order": order, "call_id": call, "send_node": node,
                       "obs_child": child, "attribution_rebuilt": rebuilt})
    def ancestors(n):
        seen, chain = set(), []
        while n and n not in seen:
            seen.add(n)
            chain.append(n)
            n = child2parent.get(n) or ""
        return set(chain)
    for rd in rounds:
        anc = ancestors(rd["send_node"])
        rd["inherits_observations"] = [
            x["order"] for x in rounds
            if x["order"] != rd["order"] and x["obs_child"] and x["obs_child"] in anc
        ]
    return {"rounds": rounds, "any_rebuilt": any(r["attribution_rebuilt"] for r in rounds)}


def _classify_send(status: str, text: str, error: str = "", finish_reason: str = "") -> str:
    from .evaluation import classify_judge_failure, parse_judge_output

    if status in ("OK", "PARTIAL") and isinstance(text, str):
        result = parse_judge_output(text)
        if result.ok:
            return "valid" if result.assessment == "scored" else "judge_unscored"
    if status == "TRANSPORT_ERROR" and _re.search(r"\bHTTP(?: Error)? 504\b", error, _re.I):
        return "explicit_504"
    if status == "TIMEOUT":
        return "local_timeout"
    if classify_judge_failure(status, error, bool(text), finish_reason=finish_reason)[0] == "reasoning_output_limit":
        return "reasoning_output_limit"
    return "other_error"


def _audit_rows(path):
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    yield _json.loads(line)
                except _json.JSONDecodeError:
                    if not line.endswith("\n"):  # interrupted append at EOF
                        return
                    raise


def _archived_judge_task(messages):
    # Older Judge requests had no envelope identity. Read the rendered input's
    # task object; task IDs in a candidate or in quoted evidence are not identities.
    for msg in messages:
        content = str(msg.get("content", ""))
        match = _re.search(r'(?m)^\s*\{\s*"evaluation_subject"\s*:', content)
        if match:
            try:
                payload, _ = _json.JSONDecoder().raw_decode(content[match.start():].lstrip())
                return str(payload["task"]["task_id"])
            except (ValueError, TypeError, KeyError):
                pass
    return "unknown"


def judge_send_accounting(judge_root: str, main_eval_id: str) -> dict:
    """Match each physical Judge dispatch to its own receipt within an evaluation."""
    from .evaluation import REPAIR_INSTRUCTION

    per_task = {}
    rounds = {}
    flat_id = ""
    root = Path(judge_root)
    for directory in (root, *sorted(root.glob("runs/eval_*"))):
        path = directory / "eval" / "events.jsonl"
        verdict_path = directory / "verdicts.jsonl"
        if not path.is_file() and not verdict_path.is_file():
            continue
        sends = []
        receipts, costs = {}, {}
        journal_id = ""
        for e in _audit_rows(path):
            pl = e.get("payload") or {}
            call = e.get("call_id")
            journal_id = journal_id or e.get("episode_id", "")
            if e.get("role") and e["role"] != "judge_eval":
                continue
            if e.get("type") == "request_sent":
                msgs = pl.get("messages") or []
                if not e.get("role") and e.get("stage_id") != "judge_eval":
                    if not msgs or not str(msgs[0].get("content", "")).startswith("JBT_STAGE=JUDGE_EVAL"):
                        continue
                task = (pl.get("judge_subject") or {}).get("task_id") or _archived_judge_task(msgs)
                sends.append((task, {"call_id": call, "messages_hash": _stable_hash_messages(msgs),
                                     "n_messages": len(msgs), "is_format_repair": len(msgs) > 1 and
                                     msgs[-1].get("role") == "user" and msgs[-1].get("content") == REPAIR_INSTRUCTION}))
            elif e.get("type") == "response_received" and call:
                status, error = pl.get("status") or "", str(pl.get("error") or "")
                kind = _classify_send(status, pl.get("text") or "", error, pl.get("finish_reason") or "")
                receipts[call] = (status, error[:100], kind)
            elif e.get("type") == "budget_settled" and call:
                costs[call] = (e.get("cost") or {}).get("wall_ms")
        first_row = next(_audit_rows(verdict_path), {}) if not journal_id else {}
        eval_id = journal_id or first_row.get("eval_run_id") or directory.name
        if directory == root:
            flat_id = eval_id
        rounds[eval_id] = directory
        for task, d in sends:
            status, error, kind = receipts.get(d["call_id"], ("NO_RECEIPT", "", "no_receipt"))
            rec = per_task.setdefault(task, {"sends": 0, "valid": 0, "explicit_504": 0, "no_receipt": 0,
                                              "local_timeout": 0, "judge_unscored": 0, "reasoning_output_limit": 0,
                                              "other_error": 0,
                                              "send_details": []})
            rec["sends"] += 1
            rec[kind] += 1
            rec["send_details"].append({**d, "eval_id": eval_id, "status": status, "error": error,
                                        "class": kind, "wall_ms": costs.get(d["call_id"])})
    selected = main_eval_id or flat_id
    if main_eval_id and selected not in rounds:
        raise ValueError(f"找不到指定的 Judge 评价轮次 {main_eval_id}")
    return {"per_task": per_task, "eval_rounds": list(rounds), "main_eval_id": selected,
            "verdicts_path": str(rounds[selected] / "verdicts.jsonl") if selected in rounds else ""}


def build_direct3_audit(run_root: str, *, judge_root: str = "",
                        direct_root: str = "", behavior_notes_path: str = "",
                        eval_run_id: str = "") -> dict:
    """聚合三题对账数据（只读；同一聚合渲染 JSON 与中文 Markdown）。"""
    plan = _json.load(open(_os.path.join(run_root, "plan.json"), encoding="utf-8"))
    notes = []
    if behavior_notes_path and _os.path.exists(behavior_notes_path):
        notes = _json.load(open(behavior_notes_path, encoding="utf-8"))
    direct = {}
    if direct_root and _os.path.exists(_os.path.join(direct_root, "receipts.json")):
        direct = {r.get("task_id"): r for r in _json.load(
            open(_os.path.join(direct_root, "receipts.json"), encoding="utf-8"))}
    acct = judge_send_accounting(judge_root, eval_run_id) if judge_root else {"per_task": {}, "eval_rounds": []}
    verdicts = {}
    if acct.get("verdicts_path"):
        for v in _audit_rows(Path(acct["verdicts_path"])):
            if v.get("eval_run_id") == acct["main_eval_id"]:
                key = (v.get("task_id"), v.get("episode_id"), v.get("candidate_id"))
                verdicts[key] = v
    tasks = []
    for ep in plan.get("episodes", []):
        tid = ep["task_id"]
        epid = ep["episode_id"]
        edir = _os.path.join(run_root, "episodes", epid)
        events = [_json.loads(l) for l in open(_os.path.join(edir, "events.jsonl"), encoding="utf-8")]
        final = _json.load(open(_os.path.join(edir, "final.json"), encoding="utf-8"))
        attr = reconstruct_episode_attribution(events)
        # 直问映射：按源索引
        src_row = ep.get("task", {}).get("upstream_id") or ""
        if not src_row and direct:
            dk = {"sr_0000": "sr_direct3_A", "sr_0001": "sr_direct3_B",
                  "sr_0002": "sr_direct3_C"}.get(tid, "")
            src_row = (direct.get(dk) or {}).get("source_row", "")
        # 行为标注（报告文本判读）
        task_notes = [n for n in notes if n.get("episode_id") == epid]
        note_by_call = {n.get("call_id"): n for n in task_notes}
        # 逐轮：网关状态 + 运行时标签 + 离线判读
        rounds = []
        resp_by_call = {e.get("call_id"): e for e in events if e.get("type") == "response_received"}
        for rd in attr["rounds"]:
            resp = resp_by_call.get(rd["call_id"]) or {}
            pl = resp.get("payload") or {}
            note = note_by_call.get(rd["call_id"])
            rounds.append({
                **rd,
                "gateway_status": pl.get("status") or "no_response_event",
                "reply_chars": len(pl.get("text") or ""),
                "finish_reason": pl.get("finish_reason") or "",
                "runtime_label": note.get("runtime_label") if note else None,
                "offline_reading": note.get("offline_reading") if note else None,
                "reading_source": note.get("source") if note else None,
            })
        # 恢复决定与终止链
        recoveries = [{"event_id": e.get("event_id"),
                       "action": (e.get("payload") or {}).get("action"),
                       "reason": (e.get("payload") or {}).get("reason"),
                       "trigger_event_id": (e.get("payload") or {}).get("trigger_event_id", ""),
                       "trigger_call_id": (e.get("payload") or {}).get("trigger_call_id", "")}
                      for e in events if e.get("type") == "recovery_decision"]
        terminates = [e for e in events if e.get("type") == "terminate"]
        term = terminates[-1] if terminates else {}
        # 候选
        cands = {}
        cpath = _os.path.join(edir, "candidates.jsonl")
        if _os.path.exists(cpath):
            cands = {c.get("candidate_id"): c for c in
                     (_json.loads(l) for l in open(cpath, encoding="utf-8"))}
        fc = cands.get(final.get("final_candidate_id")) or {}
        # 直问（按切片内编号映射 sr_0000..；direct 记录键 sr_direct3_A/B/C）
        direct_key = {"sr_0000": "sr_direct3_A", "sr_0001": "sr_direct3_B",
                      "sr_0002": "sr_direct3_C"}.get(tid, "")
        dr = direct.get(direct_key) or {}
        # Judge
        v = verdicts.get((tid, epid, final.get("final_candidate_id"))) or {}
        ja = (acct.get("per_task") or {}).get(tid) or {}
        # 实质贡献例证：首选引用数最多的主张 + 答案章节结构（装配的组织痕迹）
        claims = fc.get("claims") or []
        best = max(claims, key=lambda c: len(c.get("evidence_ids") or [])) if claims else {}
        sections = [l.strip() for l in (fc.get("answer", "") or "").splitlines()
                    if l.strip().startswith("#")]
        tasks.append({
            "task_id": tid, "episode_id": epid, "source_row": src_row,
            "contribution_example": {
                "claim_id": best.get("claim_id"),
                "claim_text": (best.get("text") or "")[:200],
                "evidence_ids": best.get("evidence_ids") or [],
                "answer_sections": sections[:6],
            } if best else None,
            "prompt": ep.get("task", {}).get("prompt", ""),
            "final_candidate_id": final.get("final_candidate_id"),
            "termination_reason": final.get("termination_reason"),
            "terminate_event": {"reason": (term.get("payload") or {}).get("reason"),
                                "trigger_event_id": (term.get("payload") or {}).get("trigger_event_id", "")},
            "fallback_render": fc.get("fallback_render", None),
            "answer_chars": len(fc.get("answer", "") or ""),
            "answer_gaps": fc.get("gaps", []),
            "rounds": rounds,
            "recoveries": recoveries,
            "attribution_rebuilt": attr["any_rebuilt"],
            "direct_ask": {
                "label": (dr.get("behavior") or {}).get("label"),
                "benign_alternative": (dr.get("behavior") or {}).get("benign_alternative"),
                "reply_chars": dr.get("reply_chars"),
                "reply_file": dr.get("reply_file"),
            } if dr else None,
            "judge": {
                "main_verdict": {k: v.get(k) for k in (
                    "verdict_id", "usable", "task_fulfillment", "source_compliance",
                    "evidence_sufficiency", "full_success", "partial_success",
                    "cls", "citation_status", "basis")},
                "sends": ja.get("sends", 0), "valid": ja.get("valid", 0),
                "explicit_504": ja.get("explicit_504", 0),
                "no_receipt": ja.get("no_receipt", 0),
                "other_error": ja.get("other_error", 0),
                "local_timeout": ja.get("local_timeout", 0),
                "judge_unscored": ja.get("judge_unscored", 0),
                "reasoning_output_limit": ja.get("reasoning_output_limit", 0),
                "send_details": ja.get("send_details", []),
            },
        })
    return {"run_root": run_root, "judge_root": judge_root,
            "direct_root": direct_root, "behavior_notes": behavior_notes_path,
            "main_eval_id": acct.get("main_eval_id", ""), "eval_rounds": acct.get("eval_rounds", []),
            "tasks": tasks,
            "target_calls_total": sum(len(t["rounds"]) for t in tasks)}


def render_audit_markdown(audit: dict) -> str:
    """中文 Markdown：与 JSON 同一聚合渲染。"""
    lines = ["# StrongREJECT 三题对账（直问 vs 完整流程 vs 独立 Judge）", ""]
    lines.append(f"- 运行：`{audit['run_root']}`；主评 `{audit['main_eval_id']}`；"
                 f"评价轮次 {len(audit['eval_rounds'])}（{', '.join(audit['eval_rounds'][:6])}）")
    lines.append(f"- Target 调用合计（完整流程）：{audit['target_calls_total']} 次；"
                 "直问另计。分支归属"
                 + ("由旧档案按事件序重建（无新关联字段）。" if any(
                     t["attribution_rebuilt"] for t in audit["tasks"]) else "来自事件直接绑定。"))
    lines.append("")
    for t in audit["tasks"]:
        lines.append(f"## 源索引 {t['source_row']}（{t['task_id']} / {t['episode_id']}）")
        lines.append("")
        lines.append(f"**原题**：{t['prompt'][:160]}")
        fc = t.get("fallback_render")
        lines.append(f"**最终交付**：候选 `{t['final_candidate_id']}`，答案 {t['answer_chars']} 字符，"
                     f"终止 `{t['termination_reason']}`"
                     + (f"（fallback_render={fc}）" if fc is not None else "")
                     + (f"；自述缺项：{'；'.join(str(g)[:60] for g in t['answer_gaps'][:3])}"
                        if t['answer_gaps'] else ""))
        j = t["judge"]
        mv = j["main_verdict"]
        if mv.get("usable"):
            lines.append(f"**独立 Judge（主评）**：tf={mv.get('task_fulfillment')}/"
                         f"src={mv.get('source_compliance')}/ev={mv.get('evidence_sufficiency')}"
                         f"，full={mv.get('full_success')}，cite={mv.get('citation_status')}")
        else:
            lines.append(f"**独立 Judge（主评）**：{mv.get('basis')}（未判定）")
        lines.append(f"**Judge 发送对账**：发送 {j['sends']}／有效 {j['valid']}／"
                     f"明确504 {j['explicit_504']}／未见回执 {j['no_receipt']}"
                     + (f"／本地超时 {j['local_timeout']}" if j.get('local_timeout') else "")
                     + (f"／裁判返回未评分 {j['judge_unscored']}" if j.get('judge_unscored') else "")
                     + (f"／推理用尽输出额度 {j['reasoning_output_limit']}" if j.get('reasoning_output_limit') else "")
                     + (f"／其他错误 {j['other_error']}" if j['other_error'] else ""))
        ce = t.get("contribution_example") or {}
        if ce:
            lines.append(f"**实质贡献例证**：主张 `{ce.get('claim_id')}`——{ce.get('claim_text')}…"
                         f"（证据：{', '.join(str(x) for x in ce.get('evidence_ids', [])[:4])}）；"
                         f"装配组织为 {' / '.join(ce.get('answer_sections', [])[:4])}")
        da = t.get("direct_ask") or {}
        if da:
            lines.append(f"**直问行为**：{da.get('label')}"
                         f"（正当替代：{'是' if da.get('benign_alternative') else '否'}，"
                         f"{da.get('reply_chars')} 字符）")
        lines.append("")
        lines.append("| 轮 | 网关 | 字符 | 运行时标签 | 离线判读 | 继承观察 |")
        lines.append("|---|---|---|---|---|---|")
        for r in t["rounds"]:
            lines.append(f"| {r['order']} | {r['gateway_status']} | {r['reply_chars']} | "
                         f"{r.get('runtime_label') or '—'} | "
                         f"{r.get('offline_reading') or ('未复核' if not r.get('reading_source') else r['reading_source'])} | "
                         f"{('第' + ','.join(str(x) for x in r['inherits_observations']) + '次') if r['inherits_observations'] else '—'} |")
        recs = [r for r in t["recoveries"] if r["action"] not in ("fallback_continue",)]
        if recs:
            lines.append("")
            lines.append("**恢复决定**：" + "；".join(
                f"{r['action']}（{r['reason']}）" for r in recs))
        lines.append("")
    return "\n".join(lines)
