"""M3 研究网页：回放真实运行、并排对照候选/证据/裁判分歧、交互式普通任务。

只读回放与对照读取 results/ 下的运行档案（真实 StrongREJECT 批次 + 交互演示）；
交互路径通过 framework.interactive.InteractiveSession 驱动同一事件/预算/网关链路。

启动：
  python -m framework.webapp --port 8765 [--demo-config configs/demo.interactive-normal-task.yaml]

仅绑定 127.0.0.1（本地研究工具）；无鉴权、无写权限之外的副作用。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import runner
from .interactive import InteractiveSession

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "static")

# 显式可回放的运行目录（真实档案 + 演示），避免把 results/ 下杂项全部暴露
KNOWN_RUNS = [
    ("results/mainflow7_sr_batch1_20261117_r1", "StrongREJECT 批次1（12 任务）"),
    ("results/mainflow8_sr_batch2_20261117_r1", "StrongREJECT 批次2·r1（12 计划位，含事故 U）"),
    ("results/mainflow8_sr_batch2_20261117_r2", "StrongREJECT 批次2·r2（9 任务）"),
    ("results/demo-same-pack-20261120", "演示·普通资料任务（同包双候选）"),
]
KNOWN_JUDGE_DIRS = "results/judge-standalone-v2*"

_SESSIONS: Dict[str, InteractiveSession] = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 数据装配（全部只读磁盘产物）
# --------------------------------------------------------------------------

def _safe_root(root: str) -> Optional[str]:
    for known, _ in KNOWN_RUNS:
        if os.path.abspath(root) == os.path.abspath(known) and os.path.isdir(known):
            return known
    return None


def list_runs() -> List[Dict[str, Any]]:
    out = []
    for root, label in KNOWN_RUNS:
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


def _verdicts_for(root: str, task_id: str, candidate_id: Optional[str]) -> Dict[str, Any]:
    """历史评价（root/evaluations）+ 模型 v4（judge-standalone 目录）。"""
    out: Dict[str, Any] = {"historical": [], "model_v4": []}
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        for line in open(vp, encoding="utf-8"):
            v = json.loads(line)
            if v.get("task_id") == task_id and (candidate_id is None or v.get("answer_id") == candidate_id):
                out["historical"].append({
                    "eval_run_id": vp.split(os.sep)[-2], "cls": v.get("cls"),
                    "basis": (v.get("basis") or "")[:200],
                })
    for path in sorted(glob.glob(KNOWN_JUDGE_DIRS + os.sep + "verdicts.jsonl")):
        if "interrupted" in path:
            continue
        for line in open(path, encoding="utf-8"):
            v = json.loads(line)
            if v.get("root", "").endswith(os.path.basename(root)) and v.get("task_id") == task_id \
                    and (candidate_id is None or v.get("candidate_id") == candidate_id):
                out["model_v4"].append({
                    "run": os.path.basename(os.path.dirname(path)),
                    "cls": v.get("cls"), "usable": v.get("usable"),
                    "full_success": v.get("full_success"), "partial_success": v.get("partial_success"),
                    "sub_result": v.get("sub_result"),
                    "sub_result_statement": (v.get("sub_result_statement") or "")[:300],
                    "citation_status": v.get("citation_status"),
                })
    return out


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
    for ev in events:
        t, p = ev.get("type"), ev.get("payload") or {}
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
        elif t == "view_snapshot":
            steps.append({"kind": "view", "node": ev.get("node_id"),
                          "session_mode": p.get("session_mode"),
                          "messages": [{"role": m.get("role"), "content": (m.get("content") or "")}
                                        for m in (p.get("messages") or [])]})
        elif t == "request_sent" and ev.get("role") == "target":
            steps.append({"kind": "request", "node": ev.get("node_id"), "call_id": p.get("call_id"),
                          "chars": sum(len(m.get("content") or "") for m in (p.get("messages") or []))})
        elif t == "response_received":
            steps.append({"kind": "response", "node": ev.get("node_id"), "status": p.get("status"),
                          "excerpt": (p.get("text") or "")[:600]})
        elif t == "target_refusal":
            steps.append({"kind": "refusal", "node": ev.get("node_id"), "label": p.get("label")})
        elif t == "pack_frozen":
            steps.append({"kind": "pack", "node": ev.get("node_id"),
                          "pack_id": p.get("pack_id"), "input_hash": (p.get("input_hash") or "")[:12],
                          "materials": len(p.get("materials") or [])})
        elif t in ("assembled",):
            steps.append({"kind": "assembled", "node": ev.get("node_id"),
                          "candidate_id": p.get("candidate_id"),
                          "answer_chars": len(p.get("answer") or "")})
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
        "verdicts": _verdicts_for(root, entry["task"]["task_id"], cand_id),
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
    pack_ids = set(cand.evidence_pack_ids or [])
    packs = []
    if os.path.exists(os.path.join(ep_dir, "packs.jsonl")):
        for line in open(os.path.join(ep_dir, "packs.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            if row.get("pack_id") in pack_ids:
                packs.append({"pack_id": row["pack_id"],
                              "input_hash": (row.get("input_hash") or "")[:16],
                              "materials": len(row.get("materials") or [])})
    claims = []
    for c in cand.claims:
        evs = []
        for eid in c.evidence_ids:
            row = ev_rows.get(eid)
            evs.append({"evidence_id": eid,
                        "text": (row.get("text") if row else None),
                        "in_pack": row is not None,
                        "call_id": (row or {}).get("call_id")})
        claims.append({"claim_id": c.claim_id, "text": c.text, "evidence": evs})
    return {
        "root": root, "episode_id": episode_id, "candidate_id": candidate_id,
        "task_id": task.get("task_id"), "task_prompt": task.get("prompt"),
        "answer": cand.answer, "claims": claims, "packs": packs,
        "assembly_config": cand.assembly_config, "gaps": list(cand.gaps),
        "verdicts": _verdicts_for(root, task.get("task_id", ""), candidate_id),
    }


# --------------------------------------------------------------------------
# 交互会话
# --------------------------------------------------------------------------

def start_session(cfg_path: str, services: str, env_file: str, out_root: str,
                  prompt: Optional[str] = None) -> Dict[str, Any]:
    from framework.cli import _load_config
    import argparse as _ap
    cfg = _load_config(_ap.Namespace(config=cfg_path, services=services, env_file=env_file))
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
    sess = InteractiveSession(cfg, task, out_root)
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
            if path.startswith("/api/candidate/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                root = _safe_root(parts[0])
                if root is None:
                    return self._json({"error": "未知运行目录"}, 404)
                return self._json(candidate_detail(root, parts[1], parts[2]))
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
