"""Episode-level accounting for a completed evaluation."""
from decimal import Decimal, ROUND_HALF_UP

from .archive_attribution import reconstruct_episode_attribution


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

import glob as _glob
import json as _json
import os as _os
import re as _re
# 只读聚合：不改运行产物；旧档案缺关联字段时按事件序/父子关系重建并标注。
# ---------------------------------------------------------------------------

def _stable_hash_messages(messages) -> str:
    from .contracts import stable_hash
    return stable_hash([{"role": m.get("role", ""), "content": m.get("content", "")}
                        for m in (messages or [])])




def _classify_send(status: str, text: str, error: str = "") -> str:
    """单次 Judge 发送的四分类：valid / explicit_504 / no_receipt / other_error。"""
    if status == "OK" and text:
        return "valid"
    if "504" in str(status) or "504" in str(error):
        return "explicit_504"
    if status in ("", None, "PENDING"):
        return "no_receipt"
    return "other_error"


def judge_send_accounting(judge_root: str, main_eval_id: str) -> dict:
    """逐题统计各评价轮次的 Judge 发送（发送/有效/明确504/未见回执）。

    覆盖主评（平铺或 runs/<id>/）与 runs/ 下所有补充轮；每次发送记录
    stable_hash(messages) 供"同输入"核对。
    """
    per_task = {}
    eval_rounds = []
    roots = []
    flat = _os.path.join(judge_root, "verdicts.jsonl")
    if _os.path.exists(flat):
        roots.append({"eval_id": main_eval_id, "dir": judge_root, "is_main": True})
    for d in sorted(_glob.glob(_os.path.join(judge_root, "runs", "eval_*"))):
        roots.append({"eval_id": _os.path.basename(d), "dir": d, "is_main": False})
    for meta in roots:
        ev_path = _os.path.join(meta["dir"], "eval", "events.jsonl")
        if not _os.path.exists(ev_path):
            continue
        evs = [_json.loads(l) for l in open(ev_path, encoding="utf-8")]
        # 按题分组：request_sent(JUDGE_EVAL) 内提取 task/episode 标识
        cur_task, cur_ep = None, None
        for e in evs:
            t = e.get("type")
            pl = e.get("payload") or {}
            if t == "request_sent":
                msgs = pl.get("messages") or [{}]
                all_c = "\n".join(str(m.get("content", "")) for m in msgs)
                if "JBT_STAGE=JUDGE_EVAL" in all_c:
                    m = _re.search(r"task_id[\"'\s:]+(sr_\d{4})", all_c)
                    m2 = _re.search(r"(ep_[0-9a-f]{10})", all_c)
                    cur_task = m.group(1) if m else (cur_task or "unknown")
                    cur_ep = m2.group(1) if m2 else cur_ep
                    is_repair = len(msgs) > 1
                    rec = per_task.setdefault(cur_task, {
                        "sends": 0, "valid": 0, "explicit_504": 0,
                        "no_receipt": 0, "other_error": 0, "send_details": []})
                    rec["sends"] += 1
                    rec["send_details"].append({
                        "eval_id": meta["eval_id"], "call_id": e.get("call_id", ""),
                        "messages_hash": _stable_hash_messages(msgs),
                        "n_messages": len(msgs), "status": "PENDING",
                        "is_format_repair": is_repair,
                    })
            elif t == "response_received" and cur_task:
                rec = per_task.get(cur_task)
                if rec and rec["send_details"] and rec["send_details"][-1]["status"] == "PENDING":
                    d = rec["send_details"][-1]
                    d["status"] = pl.get("status") or ""
                    d["error"] = str(pl.get("error") or "")[:60]
                    cls = _classify_send(pl.get("status") or "", pl.get("text") or "",
                                         str(pl.get("error") or ""))
                    d["class"] = cls
                    rec[cls if cls in ("valid", "explicit_504", "no_receipt") else "other_error"] += 1
        # 本轮结束时仍无回执的发送 → 未见回执（如超时中断的轮次）。
        # 状态改写为 NO_RECEIPT 防止后续轮次的清扫重复计数。
        for rec in per_task.values():
            for d in rec["send_details"]:
                if d["status"] in ("PENDING", "", None):
                    d["class"] = "no_receipt"
                    d["status"] = "NO_RECEIPT"
                    rec["no_receipt"] += 1
        eval_rounds.append(meta["eval_id"])
    return {"per_task": per_task, "eval_rounds": eval_rounds}


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
    vpath = _os.path.join(judge_root, "verdicts.jsonl")
    if judge_root and _os.path.exists(vpath):
        for l in open(vpath, encoding="utf-8"):
            v = _json.loads(l)
            verdicts[v.get("task_id")] = v
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
        for rd in attr["rounds"]:
            note = note_by_call.get(rd["call_id"])
            rounds.append({
                **rd,
                "annotated_runtime_label": note.get("runtime_label") if note else None,
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
        v = verdicts.get(tid) or {}
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
            "target_refusal_events": sum(e.get("type") == "target_refusal" for e in events),
            "recoveries": recoveries,
            "attribution_rebuilt": attr["any_rebuilt"],
            "attribution_unresolved": attr["any_unresolved"],
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
                "send_details": ja.get("send_details", []),
            },
        })
    return {"run_root": run_root, "judge_root": judge_root,
            "direct_root": direct_root, "behavior_notes": behavior_notes_path,
            "main_eval_id": eval_run_id, "eval_rounds": acct.get("eval_rounds", []),
            "tasks": tasks,
            "target_calls_total": sum(len(t["rounds"]) for t in tasks)}


def render_audit_markdown(audit: dict) -> str:
    """中文 Markdown：与 JSON 同一聚合渲染。"""
    lines = ["# StrongREJECT 三题对账（直问 vs 完整流程 vs 独立 Judge）", ""]
    lines.append(f"- 运行：`{audit['run_root']}`；主评 `{audit['main_eval_id']}`；"
                 f"评价轮次 {len(audit['eval_rounds'])}（{', '.join(audit['eval_rounds'][:6])}）")
    bindings = [r.get("observation_binding", "legacy_report") for t in audit["tasks"] for r in t["rounds"]]
    lines.append(f"- Target 调用合计 {audit['target_calls_total']} 次，直问另计。"
                 f"观察归属共直接关联 {bindings.count('explicit')} 次，"
                 f"按事件序重建 {bindings.count('chronology')} 次，待确认 {bindings.count('unresolved')} 次。"
                 + (f"旧报告另有 {bindings.count('legacy_report')} 次尚未按现口径核对。"
                    if "legacy_report" in bindings else ""))
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
        if "target_refusal_events" in t:
            lines.append(f"运行时共记录 {t['target_refusal_events']} 条 Target 拒答事件。"
                         "下表的运行时标签来自观察节点，离线判读来自批注。")
        else:
            lines.append("旧报告未记录 Target 拒答事件总数及标签出处。")
        lines.append("")
        lines.append("| 轮 | 网关 | 字符 | 观察归属 | 运行时标签 | 离线判读 | 继承观察 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in t["rounds"]:
            binding = {"explicit": "直接关联", "chronology": "按事件序重建",
                       "unresolved": "待确认"}.get(r.get("observation_binding"), "旧表待核")
            label = r.get("runtime_label") or "—"
            if label != "—" and "observation_binding" not in r:
                label += "（旧表，出处待核）"
            lines.append(f"| {r['order']} | {r['gateway_status']} | {r['reply_chars']} | "
                         f"{binding} | {label} | "
                         f"{r.get('offline_reading') or ('未复核' if not r.get('reading_source') else r['reading_source'])} | "
                         f"{('第' + ','.join(str(x) for x in r['inherits_observations']) + '次') if r['inherits_observations'] else '—'} |")
        recs = [r for r in t["recoveries"] if r["action"] not in ("fallback_continue",)]
        if recs:
            lines.append("")
            lines.append("**恢复决定**：" + "；".join(
                f"{r['action']}（{r['reason']}）" for r in recs))
        lines.append("")
    return "\n".join(lines)
