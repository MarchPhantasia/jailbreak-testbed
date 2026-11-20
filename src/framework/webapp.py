"""M3 研究网页：回放真实运行、并排对照候选/证据/裁判分歧、交互式普通任务。

只读回放与对照读取 results/ 下的运行档案（真实 StrongREJECT 批次 + 交互演示）；
交互路径通过 framework.interactive.InteractiveSession 驱动同一事件/预算/网关链路。

启动：
  python -m framework.webapp --port 8765 [--demo-config configs/demo.interactive-normal-task.yaml]

仅绑定 127.0.0.1（本地研究工具）；无鉴权、无写权限之外的副作用。
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional

from . import runner
from .interactive import InteractiveSession
from .standalone import recorded_evidence_ids

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "static")

# 固定档案（真实 StrongREJECT 批次）；交互/演示运行从 results/ 动态发现（R10：
# 收尾后刷新仍能找回自己创建的运行）
BATCH2ROOT = {
    "b1": "results/mainflow7_sr_batch1_20261117_r1",
    "b2r1": "results/mainflow8_sr_batch2_20261117_r1",
    "b2r2": "results/mainflow8_sr_batch2_20261117_r2",
}
ARCHIVE_RUNS = [
    ("results/mainflow7_sr_batch1_20261117_r1", "StrongREJECT 批次1（12 任务）"),
    ("results/mainflow8_sr_batch2_20261117_r1", "StrongREJECT 批次2·r1（12 计划位，含事故 U）"),
    ("results/mainflow8_sr_batch2_20261117_r2", "StrongREJECT 批次2·r2（9 任务）"),
]


def _discover_runs() -> List[str]:
    """发现交互/演示运行目录：results/ 下含 plan.json 且非固定档案、非评价目录。"""
    out = []
    if not os.path.isdir("results"):
        return out
    for name in sorted(os.listdir("results")):
        root = os.path.join("results", name)
        if not os.path.isdir(root) or not os.path.exists(os.path.join(root, "plan.json")):
            continue
        if any(root == k for k, _ in ARCHIVE_RUNS):
            continue
        if name.startswith(("judge-standalone", "m1-history", "human-", "candidate-handoff")):
            continue
        out.append(root)
    return out

_SESSIONS: Dict[str, InteractiveSession] = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 数据装配（全部只读磁盘产物）
# --------------------------------------------------------------------------

def _safe_root(root: str) -> Optional[str]:
    candidates = [k for k, _ in ARCHIVE_RUNS] + _discover_runs()
    for known in candidates:
        if os.path.abspath(root) == os.path.abspath(known) and os.path.isdir(known):
            return known
    return None


def list_runs() -> List[Dict[str, Any]]:
    out = []
    runs = list(ARCHIVE_RUNS)
    for root in _discover_runs():
        label = os.path.basename(root)
        try:
            plan = json.load(open(os.path.join(root, "plan.json"), encoding="utf-8"))
            note = str(plan.get("note") or "")
            if "interactive" in note:
                label = f"交互会话 · {label}"
        except Exception:  # noqa: BLE001
            pass
        runs.append((root, label))
    for root, label in runs:
        if not os.path.isdir(root):
            continue
        info: Dict[str, Any] = {"root": root, "label": label}
        mp = os.path.join(root, "manifest.json")
        if os.path.exists(mp):
            m = json.load(open(mp, encoding="utf-8"))
            info.update({"experiment_id": m.get("experiment_id"), "n_episodes": m.get("n_episodes")})
        eps = []
        plan_fp = os.path.join(root, "plan.json")
        if os.path.exists(plan_fp):
            plan = json.load(open(plan_fp, encoding="utf-8"))
            for e in plan["episodes"]:
                ep_dir = os.path.join(root, "episodes", e["episode_id"])
                eps.append({
                    "episode_id": e["episode_id"],
                    "task_id": e["task"]["task_id"],
                    "prompt": (e["task"].get("prompt") or "")[:110],
                    "has_final": os.path.exists(os.path.join(ep_dir, "final.json")),
                })
        info["episodes"] = eps
        out.append(info)
    return out


def _human_for(task_id: str, root: str, episode_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
    """R11：已锁定人工评语与规则 v2 推导等级（模型结果旁直接可见）。"""
    try:
        ident = json.load(open("results/human-blind-review-20261119/internal/identity_map.json", encoding="utf-8"))
        rec = next((r for r in ident if r["task_id"] == task_id
                    and r.get("candidate_id") == candidate_id and r.get("episode_id") == episode_id
                    and r["batch"] in BATCH2ROOT
                    and os.path.abspath(BATCH2ROOT[r["batch"]]) == os.path.abspath(root)), None)
        if rec is None:
            return None
        human = {r["盲审编号"].strip(): r for r in csv.DictReader(
            open("results/human-review-returns/20261119_评审表_returned.csv", encoding="utf-8"))}
        labels = json.load(open("results/human-review-returns/partial_success_labels_v2.json", encoding="utf-8"))["labels"]
        hm = human.get(rec["盲审编号"])
        if hm is None:
            return None
        lb = labels.get(rec["盲审编号"]) or {}
        full = hm["tf"] == "complete" and hm["src"] == "met" and hm["ev"] == "sufficient"
        partial = bool(lb.get("partial")) and not full
        return {
            "盲审编号": rec["盲审编号"], "tf/src/ev": f"{hm['tf']}/{hm['src']}/{hm['ev']}",
            "full_success": full, "partial_success_rule_v2": partial,
            "sub_results": [{"name": x["name"], "support": x["support"]}
                            for x in (lb.get("sub_results") or [])],
            "reason": hm.get("关键理由（一句）", ""),
        }
    except Exception:  # noqa: BLE001 - 人工数据缺失不阻塞展示
        return None


def _verdict_rows(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _verdicts_for(root: str, task_id: str, episode_id: str, candidate_id: Optional[str]) -> Dict[str, Any]:
    """历史评价（root/evaluations）+ 人工（锁定）+ 模型 v4（judge-standalone 目录）。"""
    out: Dict[str, Any] = {"historical": [], "model_v4": []}
    if not candidate_id:
        return out
    human = _human_for(task_id, root, episode_id, candidate_id)
    if human is not None:
        out["human"] = human
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        for v in _verdict_rows(vp):
            if v.get("task_id") == task_id and v.get("answer_id") == candidate_id:
                out["historical"].append({
                    "eval_run_id": vp.split(os.sep)[-2], "cls": v.get("cls"),
                    "basis": (v.get("basis") or "")[:200],
                    "judge_evidence_ids": recorded_evidence_ids(v.get("payload") or {}),
                })
    paths = glob.glob("results/*/verdicts.jsonl") + glob.glob("results/*/runs/eval_*/verdicts.jsonl")
    for path in sorted(set(paths)):
        if "interrupted" in path:
            continue
        for v in _verdict_rows(path):
            if (v.get("root") and os.path.abspath(v["root"]) == os.path.abspath(root)
                    and v.get("task_id") == task_id and v.get("candidate_id") == candidate_id
                    and v.get("episode_id", episode_id) == episode_id):
                out["model_v4"].append({
                    "run": os.path.basename(os.path.dirname(path)),
                    "eval_run_id": v.get("eval_run_id"), "judge_ref": v.get("judge_ref"),
                    "judge_evidence_ids": v.get("judge_evidence_ids"),
                    "cls": v.get("cls"), "usable": v.get("usable"),
                    "full_success": v.get("full_success"), "partial_success": v.get("partial_success"),
                    "sub_result": v.get("sub_result"),
                    "sub_result_statement": (v.get("sub_result_statement") or "")[:300],
                    "citation_status": v.get("citation_status"),
                })
    return out


WORKBENCH_STORE = os.path.join("data", "workbench", "annotations.jsonl")


def _sha256_short(t: str) -> str:
    import hashlib
    return hashlib.sha256((t or "").encode("utf-8")).hexdigest()[:16]


def workbench_episode(root: str, episode_id: str) -> Dict[str, Any]:
    """Controller 工作台：按时间列出该 episode 的 Target 交互（Issue #1）。

    每次交互：Controller 提议（request_generated，按 view_id 关联）、人工修改
    （query_edited；旧档案缺项就地标"该次运行未记录"）、实际发送消息（request_sent
    payload.messages）、回复（response_received 按 call_id 配对）、Controller 当时
    上下文（view_snapshot 按 view_id）。同节点多次请求可分辨（重试标记）。
    锚点稳定标识 = {seq, call_id, text_sha}；列表序号只用于显示。
    """
    ep_dir = os.path.join(root, "episodes", episode_id)
    if not os.path.isdir(ep_dir):
        return {"error": "episode 不存在"}
    evs = []
    with open(os.path.join(ep_dir, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    plan = runner.load_plan_from_root(root)
    entry = next((e for e in plan["episodes"] if e["episode_id"] == episode_id), None)
    task = (entry or {}).get("task") or {}

    reqs = [e for e in evs if e.get("type") == "request_sent" and e.get("role") == "target"]
    resp_by_call = {e.get("call_id"): e for e in evs
                    if e.get("type") == "response_received"}
    gens = [e for e in evs if e.get("type") == "request_generated"]
    # 关联优先级（Issue #1 小修）：request_id（一次请求）→ generation_call_id（规划调用）
    # → call_id（具体发送与回复）。同一视图下的多次请求必须各自归位；
    # 旧记录无法唯一对应时该项置 None 并标 ambiguous，前端显示"关联不确定"。
    gen_by_rid = {}
    for g in gens:
        rid = (g.get("payload") or {}).get("request_id")
        if rid and rid not in gen_by_rid:
            gen_by_rid[rid] = g
    gens_by_view: Dict[str, list] = {}
    for g in gens:
        gens_by_view.setdefault(g.get("view_id"), []).append(g)
    # view_snapshot 是 Target 发送前的消息准备快照（同 view_id 可能有多份——
    # 同视图多次请求各有一份）。按事件流序配对：每个 target 请求取其**之前最近**的
    # 同 view_id 快照；找不到则未记录。
    snap_before: Dict[int, Any] = {}
    last_snap_by_view: Dict[str, Any] = {}
    for idx, e in enumerate(evs):
        if e.get("type") == "view_snapshot":
            last_snap_by_view[e.get("view_id")] = (idx, e)
        elif e.get("type") == "request_sent" and e.get("role") == "target":
            snap_before[idx] = last_snap_by_view.get(e.get("view_id"), (None, None))[1]
    edited_by_view = {}
    for e in evs:
        if e.get("type") == "query_edited":
            edited_by_view[e.get("view_id") or e.get("payload", {}).get("view_id")] = e
    ctrl_by_call = {e.get("call_id"): e for e in evs
                    if e.get("type") == "request_sent" and e.get("role") == "controller"}
    node_counts: Dict[str, int] = {}
    seen_nodes: Dict[str, int] = {}

    interactions = []
    req_idxs = [i for i, e in enumerate(evs)
                if e.get("type") == "request_sent" and e.get("role") == "target"]
    for i, r in enumerate(reqs, 1):
        node = r.get("node_id") or ""
        node_counts[node] = node_counts.get(node, 0) + 1
        nth = seen_nodes.get(node, 0) + 1
        seen_nodes[node] = nth
        msgs = (r.get("payload") or {}).get("messages") or []
        sent_text = msgs[-1].get("content", "") if msgs else ""
        rid = (r.get("payload") or {}).get("request_id")
        gen, gen_ambiguous = gen_by_rid.get(rid), False
        if gen is None:
            cands = gens_by_view.get(r.get("view_id")) or []
            if len(cands) == 1:
                gen = cands[0]
            elif len(cands) > 1:
                gen_ambiguous = True  # 同视图多次请求且无 request_id 可区分
        resp = resp_by_call.get(r.get("call_id"))
        view = snap_before.get(req_idxs[i - 1])
        edit = edited_by_view.get(r.get("view_id"))
        # 规划调用（generation_call_id → controller 角色 request/response 配对；
        # 关联不确定时置 null，前端标『该次运行未记录』）
        ctrl_req = ctrl_by_call.get((gen or {}).get("payload", {}).get("generation_call_id"))
        ctrl_resp = (resp_by_call.get((gen or {}).get("payload", {}).get("generation_call_id")
                                    if gen else None) or {})
        interactions.append({
            "seq": i,
            "call_id": r.get("call_id") or "",
            "node_id": node,
            "node_nth": nth,
            "node_total": None,  # 二次扫描补
            "view_id": r.get("view_id") or "",
            "text_sha": _sha256_short(sent_text),
            "proposal": (gen or {}).get("payload", {}).get("query") or None,
            "rationale": (gen or {}).get("payload", {}).get("rationale") or None,
            "strategy": (gen or {}).get("payload", {}).get("strategy") or None,
            "human_edit": (edit or {}).get("payload") if edit else None,
            "sent_messages": msgs,
            "sent_text": sent_text,
            "reply_status": (resp or {}).get("payload", {}).get("status") if resp else None,
            "reply_text": (resp or {}).get("payload", {}).get("text") if resp else None,
            "controller_input_messages": ((ctrl_req or {}).get("payload") or {}).get("messages")
                if ctrl_req and gen and not gen_ambiguous else None,
            "target_view_snapshot": (view or {}).get("payload", {}).get("messages") if view else None,
            "generation_call_id": ((gen or {}).get("payload") or {}).get("generation_call_id"),
            "request_id": rid,
            "proposal_ambiguous": gen_ambiguous,
            "planning_call": ({
                "call_id": ctrl_req.get("call_id"),
                "input_messages": (ctrl_req.get("payload") or {}).get("messages"),
                "status": ctrl_resp.get("payload", {}).get("status"),
                "response_text": ctrl_resp.get("payload", {}).get("text"),
            } if ctrl_req and gen and not gen_ambiguous else None),
        })
    for it in interactions:
        it["node_total"] = node_counts.get(it["node_id"], 1)
    return {"root": root, "episode_id": episode_id, "task": task,
            "interactions": interactions,
            "note": "锚点=seq+call_id+text_sha；同节点第 2 次起标重试；"
                    "人工修改无记录的批次运行按项标『该次运行未记录』"}


def workbench_context(root: str, episode_id: str, call_id: str) -> Dict[str, Any]:
    """回退点发送前可见的上下文：该次 view_snapshot + 此前各轮的问答。"""
    wb = workbench_episode(root, episode_id)
    if wb.get("error"):
        return wb
    it = next((x for x in wb["interactions"] if x["call_id"] == call_id), None)
    if it is None:
        return {"error": "call_id 不在该 episode"}
    prior = wb["interactions"][:it["seq"] - 1]
    return {
        "root": root, "episode_id": episode_id, "call_id": call_id, "seq": it["seq"],
        "task": wb["task"],
        # Controller 当时收到的上下文（该次快照）
        "controller_input_messages": it["controller_input_messages"],
        "target_view_snapshot": it["target_view_snapshot"],
        # 此前各轮（晚于该步的回复不出现——只列到 seq-1）
        "prior_turns": [{"seq": p["seq"], "call_id": p["call_id"],
                         "question": p["sent_text"], "reply": p["reply_text"],
                         "reply_status": p["reply_status"]} for p in prior],
        "sent_text": it["sent_text"], "text_sha": it["text_sha"],
    }


def workbench_annotations(*, root: str = "", episode_id: str = "") -> List[Dict[str, Any]]:
    """读取工作台草稿；按运行+episode 过滤（同题不同运行不串绑）。"""
    out = []
    if not os.path.exists(WORKBENCH_STORE):
        return out
    with open(WORKBENCH_STORE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if root and row.get("run_root") != root:
                continue
            if episode_id and row.get("episode_id") != episode_id:
                continue
            out.append(row)
    out.sort(key=lambda r: r.get("ts") or "")
    for i, row in enumerate(out, 1):
        row["display_index"] = i  # 序号只用于显示
    return out


def workbench_save(body: Dict[str, Any]) -> Dict[str, Any]:
    """保存一份人工回退草稿：两个位置 + 理由 + 替代文本。

    只写 data/workbench/annotations.jsonl；原运行文件不动；
    Controller/Target/Assembler/Judge 调用均为 0。
    """
    root = str(body.get("root") or "")
    episode_id = str(body.get("episode_id") or "")
    if _safe_root(root) is None or not root:
        return {"error": "未知运行目录"}
    ep_dir = os.path.join(root, "episodes", episode_id)
    if not os.path.isdir(ep_dir):
        return {"error": "episode 不存在"}
    discovery = body.get("discovery") or {}
    fallback = body.get("fallback") or {}
    for anchor, name in ((discovery, "discovery"), (fallback, "fallback")):
        if not anchor.get("call_id") or not anchor.get("seq"):
            return {"error": f"{name} 锚点不完整（需要 seq+call_id）"}
    wb = workbench_episode(root, episode_id)
    by_call = {x["call_id"]: x for x in wb["interactions"]}
    for anchor, name in ((discovery, "发现问题的位置"), (fallback, "希望退回的位置")):
        it = by_call.get(anchor.get("call_id"))
        if it is None:
            return {"error": f"{name} 的 call_id 不在该 episode"}
        if str(anchor.get("text_sha")) != it["text_sha"]:
            return {"error": f"{name} 锚点 text_sha 与该次发送不符（防错位）"}
        anchor["seq"] = it["seq"]
        anchor["text_head"] = (it["sent_text"] or "")[:80]
    from .contracts import new_id
    import time as _t
    row = {
        "draft_id": new_id("wb"),
        "ts": _t.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_root": root, "episode_id": episode_id,
        "task_id": (wb.get("task") or {}).get("task_id", ""),
        "discovery": discovery, "fallback": fallback,
        "reason": str(body.get("reason") or ""),
        "draft_text": str(body.get("draft_text") or ""),
        "note": "草稿 · 未发送（只写工作台数据，未调用任何模型角色）",
    }
    os.makedirs(os.path.dirname(WORKBENCH_STORE), exist_ok=True)
    with open(WORKBENCH_STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"saved": row}


def episode_trace(root: str, episode_id: str) -> Dict[str, Any]:
    ep_dir = os.path.join(root, "episodes", episode_id)
    if not os.path.isdir(ep_dir):
        return {"error": "episode 不存在"}
    plan = json.load(open(os.path.join(root, "plan.json"), encoding="utf-8"))
    entry = next((e for e in plan["episodes"] if e["episode_id"] == episode_id), None)
    if entry is None:
        return {"error": "plan 无此 episode"}

    steps: List[Dict[str, Any]] = []
    events = [json.loads(l) for l in open(os.path.join(ep_dir, "events.jsonl"), encoding="utf-8")]
    # R2：按顶层 call_id 关联请求与回复；view_snapshot（消息准备快照）挂到其后
    # 首个 target 请求上；缓存命中单独呈现
    pending_view: Optional[Dict[str, Any]] = None
    for ev in events:
        t, p = ev.get("type"), ev.get("payload") or {}
        if t == "view_snapshot":
            pending_view = {
                "messages": [{"role": m.get("role"), "content": (m.get("content") or "")}
                             for m in (p.get("messages") or [])],
                "session_mode": p.get("session_mode"),
            }
            continue
        if t == "cache_hit":
            steps.append({"kind": "cache_hit", "node": ev.get("node_id"),
                          "role": ev.get("role"), "key": str(p.get("key", ""))[:60]})
            continue
        if t == "request_sent":
            role = ev.get("role")
            view = pending_view if role == "target" else None
            if role == "target":
                pending_view = None
            steps.append({"kind": "request", "node": ev.get("node_id"),
                          "role": role, "call_id": ev.get("call_id"),
                          "chars": sum(len(m.get("content") or "") for m in (p.get("messages") or [])),
                          "messages": ([{"role": m.get("role"), "content": (m.get("content") or "")}
                                        for m in (p.get("messages") or [])] if role == "target" else None),
                          "view": view})
            continue
        if t == "response_received":
            steps.append({"kind": "response", "node": ev.get("node_id"),
                          "role": ev.get("role"), "call_id": ev.get("call_id"),
                          "status": p.get("status"), "excerpt": (p.get("text") or "")[:600]})
            continue
        if t == "episode_started":
            steps.append({"kind": "start", "task_id": p.get("task_id"),
                          "prompt": p.get("prompt", ""), "category": p.get("category", "")})
        elif t in ("action_proposed",):
            steps.append({"kind": "propose", "node": ev.get("node_id"),
                          "type": p.get("type"), "rationale": p.get("rationale", ""),
                          "query": str(p.get("query", ""))[:300] if p.get("type") == "ASK_TARGET" else ""})
        elif t == "interactive_paused":
            steps.append({"kind": "paused", "node": ev.get("node_id"), "query": p.get("query", "")})
        elif t == "query_edited":
            steps.append({"kind": "edited", "node": ev.get("node_id"),
                          "original": p.get("original", ""), "edited": p.get("edited", ""),
                          "reason": p.get("reason", "")})
        elif t == "branch_started":
            steps.append({"kind": "branch", "node": ev.get("node_id"),
                          "from_node": p.get("from_node"), "note": p.get("note", "")})
        elif t == "target_refusal":
            steps.append({"kind": "refusal", "node": ev.get("node_id"), "label": p.get("label")})
        elif t == "pack_frozen":
            mats = p.get("materials")
            steps.append({"kind": "pack", "node": ev.get("node_id"),
                          "pack_id": p.get("pack_id"), "input_hash": (p.get("input_hash") or "")[:12],
                          "materials": mats if isinstance(mats, int) else len(mats or [])})
        elif t == "assembled":
            # R4：按真实 assembled.payload.candidates[] 枚举（id 字段；claims 可能是计数）
            for c in (p.get("candidates") or []):
                cid = c.get("id") if isinstance(c, dict) else None
                if cid:
                    cl = c.get("claims")
                    steps.append({"kind": "assembled", "node": ev.get("node_id"),
                                  "candidate_id": cid,
                                  "answer_chars": c.get("chars") or 0,
                                  "claims": cl if isinstance(cl, int) else len(cl or [])})
        elif t == "candidate_selected":
            steps.append({"kind": "selected", "node": ev.get("node_id"),
                          "candidate_id": p.get("candidate_id"), "basis": p.get("basis", "")})
        elif t == "terminate":
            steps.append({"kind": "terminate", "reason": p.get("reason", "")})

    cand_id = None
    final_fp = os.path.join(ep_dir, "final.json")
    if os.path.exists(final_fp):
        cand_id = json.load(open(final_fp, encoding="utf-8")).get("final_candidate_id")
    return {
        "root": root, "episode_id": episode_id,
        "task": entry["task"],
        "steps": steps,
        "final_candidate": cand_id,
        "verdicts": _verdicts_for(root, entry["task"]["task_id"], episode_id, cand_id),
    }


def candidate_detail(root: str, episode_id: str, candidate_id: str) -> Dict[str, Any]:
    ep_dir = os.path.join(root, "episodes", episode_id)
    cand = runner._load_final_candidate(ep_dir, candidate_id)
    if cand is None:
        return {"error": "候选不存在"}
    plan = json.load(open(os.path.join(root, "plan.json"), encoding="utf-8"))
    entry = next((e for e in plan["episodes"] if e["episode_id"] == episode_id), {})
    task = entry.get("task", {})
    ev_all = runner._read_evidence(ep_dir, None)
    ev_rows = {e["evidence_id"]: e for e in ev_all}
    episode_ids = set(ev_rows)
    pack_ids = set(cand.evidence_pack_ids or [])
    packs = []
    pack_allowed: set = set()
    if os.path.exists(os.path.join(ep_dir, "packs.jsonl")):
        for line in open(os.path.join(ep_dir, "packs.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            if row.get("pack_id") in pack_ids:
                packs.append({"pack_id": row["pack_id"],
                              "input_hash": (row.get("input_hash") or "")[:16],
                              "materials": len(row.get("materials") or [])})
                for m in row.get("materials") or []:
                    pack_allowed.update(m.get("evidence_ids") or [])
    verdicts = _verdicts_for(root, task.get("task_id", ""), episode_id, candidate_id)
    views = [(v.get("eval_run_id") or v["run"],
              set(v["judge_evidence_ids"]) if isinstance(v["judge_evidence_ids"], list) else None)
             for v in verdicts["historical"] + verdicts["model_v4"]]
    claims = []
    for c in cand.claims:
        evs = []
        for eid in c.evidence_ids:
            row = ev_rows.get(eid)
            receipts = [{"evaluation": ref, "included": eid in ids if ids is not None else None}
                        for ref, ids in views]
            seen = [r["included"] for r in receipts]
            evs.append({"evidence_id": eid,
                        "text": (row.get("text") if row else None),
                        "in_episode": eid in episode_ids,
                        "in_pack": eid in pack_allowed,
                        "judge_seen": True if True in seen else (False if seen and None not in seen else None),
                        "judge_inputs": receipts,
                        "call_id": (row or {}).get("call_id")})
        claims.append({"claim_id": c.claim_id, "text": c.text, "evidence": evs})
    return {
        "root": root, "episode_id": episode_id, "candidate_id": candidate_id,
        "task_id": task.get("task_id"), "task_prompt": task.get("prompt"),
        "answer": cand.answer, "claims": claims, "packs": packs,
        "assembly_config": cand.assembly_config, "gaps": list(cand.gaps),
        "verdicts": verdicts,
    }


def episode_candidates(root: str, episode_id: str) -> List[Dict[str, Any]]:
    """按 candidates.jsonl 枚举全部候选（去重），标记最终选择（R4）。"""
    ep_dir = os.path.join(root, "episodes", episode_id)
    cand_fp = os.path.join(ep_dir, "candidates.jsonl")
    final_fp = os.path.join(ep_dir, "final.json")
    final_id = None
    if os.path.exists(final_fp):
        final_id = json.load(open(final_fp, encoding="utf-8")).get("final_candidate_id")
    out, seen = [], set()
    if os.path.exists(cand_fp):
        for line in open(cand_fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("candidate_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({"candidate_id": cid, "is_final": cid == final_id,
                        "answer_chars": len(row.get("answer") or ""),
                        "claims": len(row.get("claims") or [])})
    return out


# --------------------------------------------------------------------------
# 交互会话
# --------------------------------------------------------------------------

def start_session(cfg_path: str, services: str, env_file: str, out_root: str,
                  prompt: Optional[str] = None) -> Dict[str, Any]:
    from framework.cli import _load_config
    import argparse as _ap
    cfg = _load_config(_ap.Namespace(config=cfg_path, services=services, env_file=env_file))
    ctx = runner.build_run_context(cfg)  # R7：完整解析（含阶段预算/模板/停止策略）
    from framework.datasets import load_split
    from framework.runner import resolve_path
    ds = cfg.get("datasets", {}) or {}
    path = resolve_path(cfg, str(ds.get("path")))
    result, dev, held, manifest = load_split(
        ds.get("adapter", "synthetic_library"), path,
        seed=int(ds.get("split_seed", 20261110)), dev_ratio=int(ds.get("dev_ratio", 2)),
        revision=ds.get("revision"))
    task = (dev if ds.get("split", "dev") == "dev" else held)[0]
    if prompt:
        task.prompt = prompt
    sess = InteractiveSession(cfg, task, out_root, ctx=ctx)
    sid = f"s_{int(time.time())}_{len(_SESSIONS)}"
    with _LOCK:
        _SESSIONS[sid] = sess
    return {"session_id": sid, "state": sess.state()}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str):
        fp = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(fp):
            self.send_error(404)
            return
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8"}.get(name.rsplit(".", 1)[-1], "text/plain")
        body = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.endswith(".js") or path.endswith(".css"):
                return self._static(path.lstrip("/"))
            if path == "/api/runs":
                return self._json(list_runs())
            if path.startswith("/api/episode/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(episode_trace(root, parts[1]))
            if path.startswith("/api/candidates/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(episode_candidates(root, parts[1]))
            if path.startswith("/api/candidate/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(candidate_detail(root, parts[1], parts[2]))
            if path == "/api/workbench/annotations":
                return self._json(workbench_annotations(
                    root=q.get("root", [""])[0], episode_id=q.get("ep", [""])[0]))
            if path == "/api/workbench/sample":
                m = json.load(open("data/samples/r22-regression.json", encoding="utf-8"))
                ident = m["identity"]
                return self._json({"run_root": ident["source_run_root"],
                                   "episode_id": ident["episode_id"],
                                   "candidate_id": ident["candidate_id"],
                                   "task_id": ident["task_id"], "card": m.get("identity", {}).get("blind_review_card")})
            if path.startswith("/api/workbench/context/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[4:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(workbench_context(root, parts[1], parts[2]))
            if path.startswith("/api/workbench/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(workbench_episode(root, parts[1]))
            if path.startswith("/api/interactive/"):
                sid = path.split("/")[3]
                with _LOCK:
                    sess = _SESSIONS.get(sid)
                if sess is None:
                    return self._json({"error": "会话不存在"}, 404)
                return self._json(sess.state())
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        try:
            if path == "/api/workbench/annotations":
                return self._json(workbench_save(body))
            if path == "/api/interactive/start":
                out = start_session(
                    body.get("config", "configs/demo.interactive-normal-task.yaml"),
                    body.get("services", "configs/services.local.yaml"),
                    body.get("env_file", ".env"),
                    body.get("root", f"results/web-interactive-{time.strftime('%Y%m%d-%H%M%S')}"),
                    body.get("prompt"),
                )
                return self._json(out)
            sid = path.split("/")[3] if path.startswith("/api/interactive/") else ""
            with _LOCK:
                sess = _SESSIONS.get(sid)
            if sess is None:
                return self._json({"error": "会话不存在"}, 404)
            op = path.rsplit("/", 1)[-1]
            if op == "step":
                return self._json(sess.step())
            if op == "approve":
                return self._json(sess.approve(body.get("edit"), body.get("reason", "")))
            if op == "skip":
                return self._json(sess.skip_pending(body.get("reason", "")))
            if op == "branch":
                return self._json(sess.branch_from(body.get("node", ""), body.get("note", "")))
            if op == "assemble":
                return self._json(sess.assemble_now(body.get("reason", "web_requested")))
            if op == "finalize":
                return self._json(sess.finalize(body.get("reason", "web_finished")))
            return self._json({"error": "unknown op"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"研究网页：http://127.0.0.1:{args.port}（仅本机）", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
