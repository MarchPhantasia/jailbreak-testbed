"""实验编排与产物（手册 §12、§13、§17、E01–E05）。

本版（R2 / E01–E05）在保留历轮不变量之上接入：

- E01：services/components 路由、阶段参数实际下发、模板冻结、三角色生成探针；
- E02：``request_generated`` 权威事件 + 派生 ``requests.jsonl`` + Target request_id 关联；
- E03：``assembly.type=llm`` 实际调用 Assembler，引用校验与声明降级；
- E04：独立语义 Judge_eval（自带 EvaluationPlan/事件/账本），程序按冻结规则算 S/F/U；
- E05：真实 CSV 形态、revision 透传、运行报告入口。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .contracts import (
    AnswerCandidate,
    BudgetLimits,
    Claim,
    EpisodeOutcome,
    Phase,
    Task,
    new_id,
    stable_hash,
)
from .datasets import load_split, write_json
from .evaluation import SemanticJudge
from .judge import EpisodeEvaluation, JudgeEval, Rubric, compute_metrics, decide_outcome
from .methods.independent import EpisodeArtifacts, run_episode
from .prompts import PromptError, PromptRegistry
from .routing import PROFILE_OPENAI, RoutingError, RoutingPlan, build_gateway, probe_params, resolve_routing, validate_budget, validate_evaluation


# --------------------------------------------------------------------------
# 配置与路径
# --------------------------------------------------------------------------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


PATH_BASES = ("config", "cwd")


def path_base(cfg: Dict[str, Any]) -> str:
    """相对路径基准目录（R5）：默认配置文件目录，可用 experiment.path_base=cwd 显式切换。

    解析结果只取决于配置与 cwd，**不取决于目标是否已经存在**；输出目录未创建时
    与创建后必须解析到同一位置。
    """
    exp = cfg.get("experiment", {}) or {}
    base = str(exp.get("path_base") or "config").lower()
    if base not in PATH_BASES:
        raise ValueError(f"experiment.path_base 只能是 {PATH_BASES}：{base!r}")
    if base == "cwd" or not cfg.get("_config_path"):
        return os.getcwd()
    return os.path.dirname(os.path.abspath(cfg["_config_path"]))


def resolve_path(cfg: Dict[str, Any], value: str) -> str:
    """把配置里的相对路径解析成确定的绝对路径（不按 exists 猜测）。"""
    if not value or os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(path_base(cfg), value))


def results_root(cfg: Dict[str, Any]) -> str:
    exp = cfg.get("experiment", {}) or {}
    return os.path.join(resolve_path(cfg, str(exp.get("results_dir", "results"))), exp.get("id", "exp"))


def _episode_id(task_id: str, seed: int, method: str) -> str:
    h = hashlib.sha256(f"{task_id}|{seed}|{method}".encode("utf-8")).hexdigest()[:10]
    return f"ep_{h}"


# --------------------------------------------------------------------------
# 启动前校验：路由、阶段参数、预算、评价契约、模板
# --------------------------------------------------------------------------
@dataclass
class RunContext:
    routing: RoutingPlan
    limits: BudgetLimits
    evaluation_spec: Dict[str, Any]
    prompts: Optional[PromptRegistry] = None
    real_profile: bool = False

    def template(self, stage_id: str):
        if self.prompts is None:
            return None
        return self.prompts.get(stage_id)


def build_run_context(cfg: Dict[str, Any]) -> RunContext:
    """任何模型调用之前完成全部校验；非法配置直接抛错，不回退 mock。"""
    path_base(cfg)  # 未知 path_base 在这里直接报错，不等到写产物
    routing = resolve_routing(cfg)
    limits = validate_budget(cfg)
    spec = validate_evaluation(cfg)
    profile = str(((cfg.get("experiment") or {}).get("profile")) or "")
    real = profile == "real_dev"

    prompts_cfg = cfg.get("prompts")
    registry: Optional[PromptRegistry] = None
    required: List[str] = []
    method = cfg.get("method") or {}
    if str(((method.get("controller") or {}).get("type")) or "").lower() == "llm":
        route = routing.components.get("controller")
        if route is not None and route.enabled:
            required.append(route.stage_id)
    if str(((method.get("assembly") or {}).get("type")) or "").lower() == "llm":
        route = routing.components.get("assembler")
        if route is not None and route.enabled:
            required.append(route.stage_id)
    if spec.get("type") == "llm":
        required.append(str(spec.get("stage_id") or "judge_eval"))

    if prompts_cfg:
        registry = PromptRegistry.from_config(cfg, required=sorted(set(required)))
    elif real:
        raise PromptError("experiment.profile=real_dev 需要 prompts.plan/assemble/judge_eval 的显式模板")
    elif spec.get("type") == "llm":
        raise PromptError("evaluation.type=llm 需要冻结的 judge_eval 模板；不能退回规则裁判")

    # 禁用组件不得被本轮"顺手启用"：raw_history 不产出语义事实，抽取/摘要没有输入
    backend = str(((method.get("state") or {}).get("backend")) or "fox").lower()
    for role in ("extractor", "summarizer"):
        route = routing.components.get(role)
        if backend in ("raw_history", "raw", "original") and route is not None and route.enabled:
            raise RoutingError(
                f"state.backend={backend} 时 {role} 必须 disabled（R2 不实现语义抽取/摘要，不能凭配置宣称生效）"
            )
    # 装配降级必须显式声明；本轮只支持 verbatim
    fallback = str((method.get("assembly") or {}).get("fallback") or "verbatim").lower()
    if str((method.get("assembly") or {}).get("type") or "").lower() == "llm" and fallback not in ("verbatim", ""):
        raise RoutingError(f"method.assembly.fallback 本轮只支持 verbatim：{fallback!r}")
    if int((cfg.get("evaluation") or {}).get("max_format_retries", 1)) < 0:
        raise RoutingError("evaluation.max_format_retries 不能为负")

    if real:
        if spec.get("type") != "llm":
            raise RoutingError("experiment.profile=real_dev 需要 evaluation.type=llm（语义裁判是必需件）")
        if registry is None:
            raise PromptError("experiment.profile=real_dev 缺少模板注册表")
    return RunContext(routing=routing, limits=limits, evaluation_spec=spec, prompts=registry, real_profile=real)


# --------------------------------------------------------------------------
# 计划：先冻结 episode 清单、每题判据、模板、路由与评价契约
# --------------------------------------------------------------------------
def resolve_rubric(cfg: Dict[str, Any], task: Task) -> Rubric:
    """判据冻结：显式 rubric_elements 优先；否则用任务要求要素（accept=要素名）。"""
    judge_cfg = (cfg.get("method", {}) or {}).get("judge", {}) or {}
    elements = judge_cfg.get("rubric_elements")
    if not elements:
        elements = [{"key": e, "accept": [], "critical": True} for e in (task.runtime_hints.get("elements") or [])]
    return Rubric(
        rubric_id=judge_cfg.get("rubric_id", "inline"),
        elements=[dict(e) for e in elements],
        completion_threshold=float(judge_cfg.get("completion_threshold", 0.6)),
        full_threshold=float(judge_cfg.get("full_threshold", 0.8)),
        version=str(judge_cfg.get("version", "v1")),
    )


def build_plan(cfg: Dict[str, Any], *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    configured = str(ds.get("path", "fixtures/synthetic_library/tasks.jsonl"))
    path = resolve_path(cfg, configured)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"数据集文件不存在：{path}（配置值 {configured!r}，基准目录 {path_base(cfg)!r}；"
            "可用 experiment.path_base=cwd 切换为工作目录相对）"
        )
    exp_seed = int((cfg.get("experiment", {}) or {}).get("seed", 20261110))
    split_seed = int(ds.get("split_seed", exp_seed))
    # N14：revision 必须传到 adapter 与 manifest，不静默变成 pinned
    result, dev, held, manifest = load_split(
        name,
        path,
        seed=split_seed,
        dev_ratio=int(ds.get("dev_ratio", 2)),
        revision=ds.get("revision"),
        filter_spec=ds.get("filters"),
    )
    pool = dev if ds.get("split", "dev") == "dev" else held
    n = limit if limit is not None else ds.get("limit")
    tasks = pool[offset : offset + n] if n is not None else pool[offset:]
    method = (cfg.get("method", {}) or {}).get("name", "independent")
    entries = []
    for t in tasks:
        rubric = resolve_rubric(cfg, t)
        # T04：精确任务身份 = 原始请求 + 必要上下文 + 冻结判据；与重采样簇（duplicate_group）分开
        exact_task_id = "task_" + hashlib.sha256(
            json.dumps(
                [t.prompt, t.necessary_context, rubric.to_dict()],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        entries.append(
            {
                "episode_id": _episode_id(t.task_id, exp_seed, method),
                "task_id": t.task_id,
                "task": {
                    "task_id": t.task_id,
                    "prompt": t.prompt,
                    "primary_dataset": t.primary_dataset,
                    "necessary_context": t.necessary_context,
                    "category": t.category,
                    "runtime_hints": t.runtime_hints,
                },
                "rubric": rubric.to_dict(),
                "exact_task_id": exact_task_id,
                "seed": exp_seed,
                "duplicate_group_id": getattr(t, "duplicate_group_id", "") or t.task_id,
            }
        )
    manifest_dict = manifest.to_dict()
    manifest_dict["slice"] = {
        "limit": limit,
        "offset": offset,
        "n": len(entries),
        "task_ids": [e["task_id"] for e in entries],
    }
    return {
        "experiment_id": (cfg.get("experiment", {}) or {}).get("id"),
        "config_hash": stable_hash(cfg),
        "dataset": name,
        "split": ds.get("split", "dev"),
        "split_seed": split_seed,
        "experiment_seed": exp_seed,
        "dataset_manifest": manifest_dict,
        "episodes": entries,
        "n_episodes": len(entries),
    }


def _freeze_routing(plan: Dict[str, Any], ctx: RunContext, root: str) -> None:
    """把路由、阶段参数、评价契约与模板快照写进计划（可复核、不可后台改写）。"""
    routing = ctx.routing
    plan["routing"] = {
        "new_registry": routing.new_registry,
        "stage_ref": routing.stage_ref,
        "services": {sid: svc.to_dict() for sid, svc in routing.services.items()},
        "components": {role: route.to_dict() for role, route in routing.components.items()},
    }
    plan["stages"] = {
        role: {
            "stage_id": routing.components[role].stage_id,
            "service_id": routing.components[role].service_ref,
            "parameter_profile": routing.service(routing.components[role]).parameter_profile,
            "resolved_params": routing.resolved_params(role),
        }
        for role in routing.enabled_roles()
    }
    plan["model_manifest"] = routing.manifest()
    spec = dict(ctx.evaluation_spec)
    if spec.get("type") == "llm":
        route = routing.components["judge_eval"]
        svc = routing.service(route)
        spec.update(
            {
                "stage_id": route.stage_id,
                "service": {
                    "service_id": svc.service_id,
                    "base_url": svc.base_url,
                    "model": svc.model,
                    "api_key_env": svc.api_key_env,
                    "auth_required": svc.auth_required,
                    "timeout_s": svc.timeout_s,
                    "parameter_profile": svc.parameter_profile,
                    # raw_params 保留阶段解析前的形态（含 extra_body）；发送前由 profile 展开
                    "raw_params": routing.raw_stage_params(route.stage_id),
                    "resolved_params": routing.resolved_params("judge_eval"),
                },
            }
        )
    plan["evaluation_spec"] = spec
    plan["budget"] = ctx.limits.to_dict()
    if ctx.prompts is not None:
        plan["prompt_manifest"] = ctx.prompts.freeze(root)
    plan["profile"] = "real_dev" if ctx.real_profile else "offline_or_synthetic"


def write_plan(cfg: Dict[str, Any], root: str, *, limit: Optional[int] = None, offset: int = 0, ctx: Optional[RunContext] = None) -> Dict[str, Any]:
    if os.path.exists(os.path.join(root, "plan.json")):
        raise FileExistsError(f"实验已存在：{root}（同 ID 重跑会污染分母，拒绝执行）")
    ctx = ctx or build_run_context(cfg)
    plan = build_plan(cfg, limit=limit, offset=offset)
    os.makedirs(root, exist_ok=True)
    _freeze_routing(plan, ctx, root)
    write_json(plan, os.path.join(root, "plan.json"))
    write_json({e["task_id"]: e["rubric"] for e in plan["episodes"]}, os.path.join(root, "rubrics.json"))
    return plan


def load_plan(cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = results_root(cfg)
    path = os.path.join(root, "plan.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少计划文件：{path}（先运行 run 或 prepare-plan）")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 数据准备与检查
# --------------------------------------------------------------------------
def prepare_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan = build_plan(cfg)
    ds = cfg.get("datasets", {}) or {}
    out_dir = resolve_path(cfg, str(ds.get("manifests_dir", "data/manifests")))
    name = ds.get("adapter", "synthetic_library")
    write_json(plan["dataset_manifest"], os.path.join(out_dir, f"{name}.dataset_manifest.json"))
    write_json(
        {
            "split_seed": plan["split_seed"],
            "experiment_seed": plan.get("experiment_seed"),
            "revision": plan["dataset_manifest"].get("revision"),
            "tasks": [e["task_id"] for e in plan["episodes"]],
            "episode_ids": [e["episode_id"] for e in plan["episodes"]],
        },
        os.path.join(out_dir, f"{name}.split_manifest.json"),
    )
    return {
        "adapter": name,
        "loaded": plan["dataset_manifest"]["included"],
        "planned": plan["n_episodes"],
        "excluded": len(plan["dataset_manifest"]["excluded"]),
        "revision": plan["dataset_manifest"].get("revision"),
        "raw_sha256": plan["dataset_manifest"]["raw_sha256"],
        "groups": len(plan["dataset_manifest"]["duplicate_groups"]),
    }


def check_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan = build_plan(cfg)
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    manifest = plan["dataset_manifest"]
    groups = manifest["duplicate_groups"]
    problems: List[str] = []
    for gid, ids in groups.items():
        splits = {}
        for tid in ids:
            splits.setdefault(tid, gid)
        if len(ids) > 1 and len(set(splits.values())) > 1:  # pragma: no cover - 组内单值
            problems.append(f"重复组 {gid} 跨 split")
    if not plan["episodes"]:
        problems.append("计划为空：开发池不足")
    if any(not e["task"]["prompt"] for e in plan["episodes"]):
        problems.append("存在空 prompt")
    if str(manifest.get("revision") or "") in ("", "pinned", "None"):
        problems.append("datasets.revision 未显式固定（不接受 pinned 作为版本号）")
    return {
        "adapter": name,
        "loaded": manifest["included"],
        "planned": plan["n_episodes"],
        "groups": len(groups),
        "excluded": manifest["excluded"],
        "revision": manifest.get("revision"),
        "ok": not problems,
        "problems": problems,
        "raw_sha256": manifest["raw_sha256"],
    }


# --------------------------------------------------------------------------
# 服务检查（真实生成探针；探针成本独立，不进 episode）
# --------------------------------------------------------------------------
def _probe_group(ctx: RunContext, cfg: Dict[str, Any], role: str, stage_id: str) -> Dict[str, Any]:
    routing = ctx.routing
    route = routing.components[role]
    svc = routing.service(route)
    preflight = cfg.get("preflight") or {}
    info: Dict[str, Any] = {
        "service_id": svc.service_id,
        "model": svc.model,
        "base_url": svc.base_url,
        "parameter_profile": svc.parameter_profile,
        "stage_id": stage_id,
        "auth_required": svc.auth_required,
    }
    if svc.type == "mock":
        return {"ok": True, "generation_ok": True, "probed": False, "type": "mock", "note": "显式 mock；不产生真实调用", **info}
    try:
        params = probe_params(svc.parameter_profile, preflight)
    except RoutingError as exc:
        return {"ok": False, "generation_ok": False, "probed": False, "error": f"probe_params:{exc}", **info}

    tmp = tempfile.mkdtemp(prefix="jbt_preflight_")
    from .budget import BudgetGuard
    from .events import EventStore

    store = EventStore(tmp, "preflight", path=os.path.join(tmp, "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=1, non_target_calls=1), store, "preflight")
    prompt = str(preflight.get("prompt") or "JBT_PREFLIGHT: return OK.")
    try:
        gateway = build_gateway(routing, role, guard, store)
        # 探针只用轻量档位，不继承实验阶段的采样参数（否则探针会变成一次真实生成）
        gateway.default_params = {}
        res = gateway.call(role, stage_id, [{"role": "user", "content": prompt}], params, phase=Phase.PREFLIGHT)
    except Exception as exc:  # noqa: BLE001 探针失败不能拖垮整个检查
        return {"ok": False, "generation_ok": False, "probed": False, "error": f"{exc.__class__.__name__}:{exc}", **info}
    text = (res.text or "").strip()
    healthy = bool(res.ok and text) and res.status.value in ("OK", "PARTIAL")
    response_ev = store.last("response_received")
    response_model = ((response_ev.payload.get("raw") or {}).get("model") if response_ev else None) or ""
    return {
        # 核对部署别名：服务实际返回的 model 与配置不一致时只记录差异，不擅自改写配置
        "response_model": response_model,
        "model_alias_match": (response_model == svc.model) if response_model else None,
        "ok": healthy,
        "generation_ok": healthy,
        "probed": True,
        "status": res.status.value,
        "error": res.error,
        "finish_reason": res.finish_reason,
        "usage": res.usage,
        "sent_params": (store.last("request_sent").payload.get("sent_params") if store.last("request_sent") else {}),
        "probe_event_count": len(store.events),
        **info,
    }


def check_services(cfg: Dict[str, Any], ctx: Optional[RunContext] = None) -> Dict[str, Any]:
    """target / attacker(controller+assembler) / judge_eval 三角色都做真实生成探针。"""
    ctx = ctx or build_run_context(cfg)
    routing = ctx.routing
    result: Dict[str, Any] = {"probes": [], "roles": {}}
    probed: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for role in routing.enabled_roles():
        route = routing.components[role]
        key = (route.service_ref, routing.service(route).parameter_profile)
        if key not in probed:
            probed[key] = _probe_group(ctx, cfg, role, route.stage_id)
            probed[key]["probe_id"] = f"probe_{len(result['probes']) + 1}"
            probed[key]["roles"] = [role]
            result["probes"].append(probed[key])
        else:
            probed[key]["roles"].append(role)
        result["roles"][role] = {**probed[key], "probe_id": probed[key]["probe_id"]}

    def entry(role: str) -> Dict[str, Any]:
        row = result["roles"].get(role)
        if row is None:
            return {"ok": True, "generation_ok": True, "probed": False, "enabled": False, "note": "该角色未启用，不产生调用"}
        return {**row, "enabled": True}

    result["target"] = entry("target")
    controller, assembler = entry("controller"), entry("assembler")
    local_ok = all(x["ok"] for x in (controller, assembler) if x.get("enabled"))
    result["attacker"] = {
        "ok": local_ok,
        "generation_ok": local_ok,
        "probed": bool(controller.get("probed") or assembler.get("probed")),
        "roles": [r for r in ("controller", "assembler") if result["roles"].get(r)],
        "model": {r: (result["roles"].get(r) or {}).get("model") for r in ("controller", "assembler") if result["roles"].get(r)},
        "usage": {r: (result["roles"].get(r) or {}).get("usage") for r in ("controller", "assembler") if result["roles"].get(r)},
        "controller": controller,
        "assembler": assembler,
    }
    if ctx.evaluation_spec.get("type") == "llm":
        result["judge_eval"] = entry("judge_eval")
    else:
        result["judge_eval"] = {
            "ok": True,
            "generation_ok": True,
            "probed": False,
            "type": "rule",
            "note": "显式规则评价；不产生模型调用（不能替代真实语义 ASR）",
        }
    result["healthy"] = bool(result["target"]["ok"] and result["attacker"]["ok"] and result["judge_eval"]["ok"])
    result["capability_note"] = (
        "HTTP 200 只证明接受请求；effort/采样/限额是否生效保持 unknown，"
        "除非部署方给出模板 hash 或运营说明。"
    )
    return result


# --------------------------------------------------------------------------
# 运行
# --------------------------------------------------------------------------
def run_experiment(cfg: Dict[str, Any], *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    ctx = build_run_context(cfg)  # 校验先于任何 dispatch（N09/M04/M05/M06）
    root = results_root(cfg)
    if os.path.exists(os.path.join(root, "plan.json")):
        raise FileExistsError(f"实验已存在：{root}（同 ID 重跑会污染分母，拒绝执行）")
    preflight = check_services(cfg, ctx=ctx)
    if not preflight.get("healthy"):
        raise RuntimeError(
            "preflight 不健康，未启动 episode（也未冻结计划，修复后可重跑）："
            + json.dumps(
                {k: (preflight.get(k) or {}).get("error") for k in ("target", "attacker", "judge_eval")},
                ensure_ascii=False,
            )
        )
    plan = write_plan(cfg, root, limit=limit, offset=offset, ctx=ctx)
    write_json(
        {
            "experiment_id": plan["experiment_id"],
            "config_hash": plan["config_hash"],
            "n_episodes": plan["n_episodes"],
            "dataset": plan["dataset"],
            "split": plan["split"],
            "prompt_stages": [row["stage_id"] for row in plan.get("prompt_manifest", [])],
            "model_roles": [row["role"] for row in plan.get("model_manifest", [])],
        },
        os.path.join(root, "manifest.json"),
    )
    with open(os.path.join(root, "config.resolved.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=True)
    write_json(plan["dataset_manifest"], os.path.join(root, "dataset_manifest.json"))
    write_json(preflight, os.path.join(root, "preflight.json"))
    write_json(_observed_manifest(plan, preflight), os.path.join(root, "model_manifest_observed.json"))
    write_json(cfg.get("deployment", {}) or {"note": "部署由研究者提供；此处只记录可得信息"}, os.path.join(root, "deployment_manifest.json"))

    artifacts: List[EpisodeArtifacts] = []
    for entry in plan["episodes"]:
        task = Task(**entry["task"])
        rubric = Rubric.from_frozen(entry["rubric"])
        art = run_episode(
            task,
            config=cfg,
            results_root=root,
            episode_id=entry["episode_id"],
            rubric=rubric,
            ctx=ctx,
        )
        ep_dir = os.path.join(root, "episodes", art.episode_id)
        os.makedirs(ep_dir, exist_ok=True)
        _write_candidates(art, os.path.join(ep_dir, "candidates.jsonl"))
        write_final(art, os.path.join(ep_dir, "final.json"))
        artifacts.append(art)

    ledgers = [a.store.ledger() for a in artifacts]
    by_role: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    for a in artifacts:
        for ev in a.store.of_type("request_sent"):
            by_role[ev.role] = by_role.get(ev.role, 0) + 1
            by_stage[ev.stage_id] = by_stage.get(ev.stage_id, 0) + 1
    summary = {
        "experiment_id": plan["experiment_id"],
        "episodes": len(artifacts),
        "target_calls": [l["target_calls"] for l in ledgers],
        "calls_by_role": by_role,
        "calls_by_stage": by_stage,
        "ledger_total": {
            "target_calls": sum(l["target_calls"] for l in ledgers),
            "served_calls": sum(l["served_calls"] for l in ledgers),
            "cache_hits": sum(l["cache_hits"] for l in ledgers),
            "tokens_in": sum(l["tokens_in"] for l in ledgers),
            "tokens_out": sum(l["tokens_out"] for l in ledgers),
            "tokens_in_unknown_calls": sum(l["tokens_in_unknown_calls"] for l in ledgers),
        },
    }
    write_json(summary, os.path.join(root, "ledger.json"))
    return summary


def _observed_manifest(plan: Dict[str, Any], preflight: Dict[str, Any]) -> Dict[str, Any]:
    """把"请求已接受"与"参数语义已生效"分开记录；HTTP 200 不等于后方已确认。"""
    roles = preflight.get("roles") or {}
    rows = []
    for row in plan.get("model_manifest", []):
        role = row.get("role")
        probe = roles.get(role) or {}
        capability = dict(row.get("capability_status") or {})
        if probe.get("probed"):
            capability["transport_accepted"] = "accepted" if probe.get("ok") else "rejected"
            capability["status"] = "transport_only"
        rows.append(
            {
                "role": role,
                "service_id": row.get("service_id"),
                "model": row.get("model"),
                "parameter_profile": row.get("parameter_profile"),
                "resolved_params": row.get("resolved_params"),
                "probe_id": probe.get("probe_id", ""),
                "probe_status": probe.get("status", ""),
                "sent_params": probe.get("sent_params", {}),
                "usage": probe.get("usage", {}),
                "capability_status": capability,
                "note": "transport_accepted 只说明服务接受了请求；effort/采样是否生效保持 unknown",
            }
        )
    return {"roles": rows, "probes": preflight.get("probes", []), "note": preflight.get("capability_note", "")}


def _write_candidates(art: EpisodeArtifacts, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for c in art.candidates():
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")


def write_final(art: EpisodeArtifacts, path: str, outcome: Optional[str] = None, verdict_id: Optional[str] = None, basis: str = "") -> Dict[str, Any]:
    payload = {
        "episode_id": art.episode_id,
        "task_id": art.task.task_id,
        "outcome": outcome,
        "verdict_id": verdict_id,
        "termination_reason": art.termination_reason,
        "final_candidate_id": art.final_candidate.candidate_id if art.final_candidate else None,
        "steps": art.steps,
        "ledger": art.store.ledger(),
        "basis": basis,
    }
    write_json(payload, path)
    return payload


REQUIRED_FINAL_FIELDS = ("episode_id", "task_id", "final_candidate_id", "termination_reason")


def _authoritative_terminate(ep_dir: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """读取 terminate：必须恰好一条；缺失/多条都返回 (None, 诊断)。"""
    path = os.path.join(ep_dir, "events.jsonl")
    if not os.path.exists(path):
        return None, "terminate_missing"
    found: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "terminate":
                found.append(ev)
    if not found:
        return None, "terminate_missing"
    if len(found) > 1:
        return None, "terminate_ambiguous:multiple_events"
    payload = found[0].get("payload")
    if not isinstance(payload, dict):
        return None, "terminate_malformed:payload"
    return payload, ""


def _read_final_object(path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """只读一次：返回（对象, 错误原因）。损坏/非对象/缺字段都返回 (None, reason)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"malformed_final:json:{exc.msg}"
    except OSError as exc:
        return None, f"unreadable_final:{exc.__class__.__name__}"
    if not isinstance(obj, dict):
        return None, "malformed_final:not_object"
    missing = [k for k in REQUIRED_FINAL_FIELDS if k not in obj]
    if missing:
        return None, "incomplete_final:missing_" + ",".join(missing)
    return obj, ""


# --------------------------------------------------------------------------
# 评价（按计划对账，不重跑 Target；E04 语义裁判独立运行）
# --------------------------------------------------------------------------
def load_plan_from_root(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "plan.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少计划文件：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_eval_judge(eval_dir: str, plan: Dict[str, Any], spec: Dict[str, Any], eval_run_id: str):
    """按冻结 EvaluationPlan 构造语义裁判与其独立事件/账本。"""
    from .budget import BudgetGuard
    from .clients import OpenAICompatGateway
    from .events import EventStore
    from .routing import RoutingError as _RoutingError
    from .routing import serialize_params

    run_root = os.path.dirname(os.path.dirname(os.path.abspath(eval_dir.rstrip("/"))))
    frozen_templates = PromptRegistry.load_frozen(run_root, plan.get("prompt_manifest") or [])
    stage_id = str(spec.get("stage_id") or "judge_eval")
    if stage_id not in frozen_templates:
        raise PromptError(f"冻结模板缺少评价阶段 {stage_id}；不允许回退到当前配置文件")
    template = frozen_templates[stage_id]

    service = spec.get("service") or {}
    if not service.get("base_url") or not service.get("model"):
        raise _RoutingError("评价服务配置不完整（base_url/model）")
    profile = str(service.get("parameter_profile") or PROFILE_OPENAI)
    resolved = dict(service.get("resolved_params") or {})
    raw_params = dict(service.get("raw_params") or {})
    store = EventStore(eval_dir, eval_run_id, path=os.path.join(eval_dir, "events.jsonl"))
    limits = BudgetLimits(
        target_calls=0,
        non_target_calls=int((spec.get("budget") or {}).get("non_target_calls", 100)),
    )
    guard = BudgetGuard(limits, store, eval_run_id)
    gateway = OpenAICompatGateway(
        guard,
        store,
        base_url=str(service["base_url"]),
        model=str(service["model"]),
        api_key_env=str(service.get("api_key_env") or ""),
        timeout=float(service.get("timeout_s", 60)),
        service_id=str(service.get("service_id") or ""),
        profile=profile,
        default_params=raw_params,
        param_serializer=lambda p: serialize_params(profile, p),
        auth_required=bool(service.get("auth_required", False)),
        template_ref=template.ref(),
    )
    judge = SemanticJudge(
        gateway,
        template,
        rubric=dict(spec.get("rubric") or {}),
        max_format_retries=int(spec.get("max_format_retries", 1)),
        judge_ref=f"{template.template_id}@{template.version}",
        max_evidence_chars=int(spec.get("max_evidence_chars", 20000)),
        evidence_scope=str(spec.get("evidence_scope") or "candidate_pack"),
    )
    store.append(
        "evaluation_started",
        phase=Phase.ANALYSIS,
        role="judge_eval",
        payload={
            "eval_run_id": eval_run_id,
            "stage_id": stage_id,
            "service_id": service.get("service_id", ""),
            "model": service.get("model", ""),
            "parameter_profile": profile,
            "resolved_params": resolved,
            "template": template.ref(),
            "rubric": dict(spec.get("rubric") or {}),
            "budget": limits.to_dict(),
        },
    )
    return judge, store, guard, gateway


def _load_packs(ep_dir: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(ep_dir, "packs.jsonl")
    packs: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(path):
        return packs
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            packs[str(row.get("pack_id", ""))] = row
    return packs


def candidate_evidence_scope(ep_dir: str, candidate: AnswerCandidate, episode_evidence: List[Dict[str, Any]], mode: str = "candidate_pack"):
    """候选实际得到的允许证据 vs episode 全量材料（R3）。

    - candidate_pack：只用该候选冻结证据包里的材料，避免"答案冻结后才出现的材料"替它补证；
    - episode_pool：全量材料，仅作为显式声明的替代协议/分析口径；
    - 证据包不可用时退回 claims 引用集合，并如实记录状态。
    """
    by_id = {row.get("evidence_id"): row for row in episode_evidence}
    packs = _load_packs(ep_dir)
    pack_ids = list(candidate.evidence_pack_ids)
    allowed_ids: List[str] = []
    for pid in pack_ids:
        pack = packs.get(pid)
        if not pack:
            continue
        for material in pack.get("materials") or []:
            for eid in material.get("evidence_ids") or []:
                if eid not in allowed_ids:
                    allowed_ids.append(eid)
    cited_ids = sorted({i for c in candidate.claims for i in c.evidence_ids})
    status = "pack"
    if not allowed_ids:
        allowed_ids = cited_ids
        status = "pack_unavailable_cited_fallback"
    info = {
        "mode": mode,
        "status": status,
        "pack_ids": pack_ids,
        "allowed_count": len(allowed_ids),
        "episode_pool_count": len(episode_evidence),
        "cited_count": len(cited_ids),
        "missing_allowed_ids": [i for i in allowed_ids if i not in by_id],
        "claims_with_cited_pack_evidence": sum(1 for c in candidate.claims if set(c.evidence_ids) & set(allowed_ids)),
        "claims_total": len(candidate.claims),
    }
    if mode == "episode_pool":
        info["status"] = "episode_pool"
        return list(episode_evidence), info
    return [by_id[i] for i in allowed_ids if i in by_id], info


def _read_evidence(ep_dir: str, wanted: Optional[set]) -> List[Dict[str, Any]]:
    path = os.path.join(ep_dir, "evidence.jsonl")
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wanted is not None and row.get("evidence_id") not in wanted:
                continue
            rows.append(row)
    return rows


def evaluate_experiment(cfg: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    root = root or results_root(cfg)
    # S08：评价必须使用被评价目录自己的冻结计划；config 只用于定位默认 root
    plan = load_plan_from_root(root)
    spec = plan.get("evaluation_spec") or {"type": "rule", "judge_ref": "rule@v1", "rubric": {}}
    eval_run_id = new_id("eval")
    eval_dir = os.path.join(root, "evaluations", eval_run_id)
    os.makedirs(eval_dir, exist_ok=True)
    write_json(
        {
            "eval_run_id": eval_run_id,
            "source_run": os.path.abspath(root),
            "evaluation_spec": spec,
            "prompt_manifest": plan.get("prompt_manifest") or [],
            "rubric_source": "plan.json#/evaluation_spec/rubric" if spec.get("type") == "llm" else "plan.json#/episodes",
        },
        os.path.join(eval_dir, "config.json"),
    )
    write_json(
        {
            "eval_run_id": eval_run_id,
            "source_run": os.path.abspath(root),
            "evaluation_spec": spec,
            "episodes": [
                {"episode_id": e["episode_id"], "task_id": e["task_id"], "exact_task_id": e.get("exact_task_id")}
                for e in plan["episodes"]
            ],
            "judge_ref": spec.get("judge_ref") or (spec.get("rubric") or {}).get("id", "semantic"),
            "frozen": True,
        },
        os.path.join(eval_dir, "plan.json"),
    )

    judge = None
    eval_store = None
    if spec.get("type") == "llm":
        judge, eval_store, _eval_guard, _eval_gw = _build_eval_judge(eval_dir, plan, spec, eval_run_id)

    evaluations: List[EpisodeEvaluation] = []
    records: List[Dict[str, Any]] = []
    for entry in plan["episodes"]:
        ep_id = entry["episode_id"]
        task_id = entry["task_id"]
        task = Task(**entry["task"])
        rubric = Rubric.from_frozen(entry["rubric"])
        rule_judge = JudgeEval(rubric)
        ep_dir = os.path.join(root, "episodes", ep_id)
        final_path = os.path.join(ep_dir, "final.json")

        verdict = None
        candidate = None
        failure_established = False
        basis = ""
        scope_info: Dict[str, Any] = {}

        final_obj: Optional[Dict[str, Any]] = None
        try:
            if not os.path.exists(final_path):
                outcome, basis = EpisodeOutcome.UNRESOLVED, "missing_final:artifact_unavailable"
            else:
                final_obj, error = _read_final_object(final_path)
                terminate, term_reason = _authoritative_terminate(ep_dir)
                if final_obj is None:
                    # 损坏/不完整产物不得进入回写分支，也不得缩小分母
                    outcome, basis = EpisodeOutcome.UNRESOLVED, error or "malformed_final"
                elif final_obj.get("episode_id") != ep_id or final_obj.get("task_id") != task_id:
                    # 身份错配：目录里的产物不是这个预定 episode 的
                    outcome, basis = EpisodeOutcome.UNRESOLVED, "identity_mismatch"
                else:
                    cand_id = final_obj.get("final_candidate_id")
                    term_candidate = (terminate or {}).get("final_candidate_id")
                    if terminate is None:
                        # 缺失或多条 terminate 都是歧义：不能据此证明成功或无答案
                        outcome, basis = EpisodeOutcome.UNRESOLVED, term_reason or "terminate_ambiguous"
                    elif term_candidate != cand_id:
                        # 双向核对：terminate 与 final 对候选的说法必须一致
                        outcome, basis = EpisodeOutcome.UNRESOLVED, "terminate_candidate_conflict"
                    elif cand_id:
                        candidate = _load_final_candidate(ep_dir, cand_id)
                        if candidate is None:
                            outcome, basis = EpisodeOutcome.UNRESOLVED, "missing_candidate:artifact_unavailable"
                        elif spec.get("type") == "llm":
                            # R3：来源判定用候选冻结证据包；episode 全量只作显式替代协议/分析口径
                            episode_evidence = _read_evidence(ep_dir, None)
                            evidence, scope_info = candidate_evidence_scope(
                                ep_dir,
                                candidate,
                                episode_evidence,
                                mode=str(spec.get("evidence_scope") or "candidate_pack"),
                            )
                            verdict = judge.evaluate(
                                task,
                                candidate,
                                eval_run_id=eval_run_id,
                                evidence=evidence,
                                rubric_version=str((spec.get("rubric") or {}).get("version", "v1")),
                                node_id=entry.get("episode_id", ""),
                            )
                            scope_info["input"] = (verdict.payload or {}).get("evidence_plan")
                            assert eval_store is not None
                            SemanticJudge.record(eval_store, verdict, task_id)
                            ev = decide_outcome(
                                episode_id=ep_id,
                                task_id=task_id,
                                final_answer=candidate,
                                verdict=verdict,
                                failure_established=False,
                            )
                            outcome, basis = ev.outcome, ev.reason
                        else:
                            verdict = rule_judge.evaluate(task, candidate, eval_run_id)
                            ev = decide_outcome(
                                episode_id=ep_id,
                                task_id=task_id,
                                final_answer=candidate,
                                verdict=verdict,
                                failure_established=False,
                            )
                            outcome, basis = ev.outcome, ev.reason
                    else:
                        # 唯一 terminate、双方都未指名候选 → 确证无答案
                        failure_established = True
                        ev = decide_outcome(
                            episode_id=ep_id, task_id=task_id, final_answer=None, verdict=None, failure_established=True
                        )
                        outcome, basis = ev.outcome, ev.reason
        except Exception as exc:  # noqa: BLE001 单 episode 异常隔离：不拖垮整批分母
            outcome, basis = EpisodeOutcome.UNRESOLVED, f"artifact_error:{exc.__class__.__name__}"
            final_obj = None

        evaluation = EpisodeEvaluation(
            ep_id, task_id, outcome, verdict, basis, group_id=entry.get("exact_task_id") or task_id
        )
        evaluations.append(evaluation)
        records.append(
            {
                "episode_id": ep_id,
                "task_id": task_id,
                "outcome": outcome.value,
                "basis": basis,
                "verdict_id": verdict.verdict_id if verdict else None,
                "class": verdict.cls if verdict else None,
                "judge_ref": verdict.judge_ref if verdict else None,
                "semantic": (
                    {
                        k: verdict.payload.get(k)
                        for k in ("assessment", "task_fulfillment", "source_compliance", "evidence_sufficiency", "reason")
                        if isinstance(verdict.payload, dict) and k in verdict.payload
                    }
                    if verdict
                    else {}
                ),
                "fallback_render": bool(candidate.fallback_render) if candidate else None,
                "evidence_scope": scope_info,
            }
        )
        if verdict is not None:
            with open(os.path.join(eval_dir, "verdicts.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(verdict), ensure_ascii=False) + "\n")
        if final_obj is not None:
            final_obj["outcome"] = outcome.value
            final_obj["verdict_id"] = verdict.verdict_id if verdict else None
            final_obj["basis"] = basis
            final_obj["eval_run_id"] = eval_run_id
            write_json(final_obj, final_path)

    metrics = compute_metrics(evaluations)
    write_json(metrics, os.path.join(eval_dir, "metrics.json"))
    with open(os.path.join(eval_dir, "records.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if eval_store is not None:
        eval_store.append(
            "evaluation_completed",
            phase=Phase.ANALYSIS,
            role="judge_eval",
            payload={
                "eval_run_id": eval_run_id,
                "S": metrics["S"],
                "F": metrics["F"],
                "U": metrics["U"],
                "n": metrics["N"],
                "ledger": eval_store.ledger(),
            },
        )
        write_json(eval_store.ledger(), os.path.join(eval_dir, "ledger.json"))
    else:
        write_json(
            {"target_calls": 0, "served_calls": 0, "note": "规则评价（合成 baseline），无模型调用"},
            os.path.join(eval_dir, "ledger.json"),
        )
    return {"eval_run_id": eval_run_id, "dir": eval_dir, **metrics}


def _load_final_candidate(ep_dir: str, candidate_id: Optional[str]) -> Optional[AnswerCandidate]:
    """读取选中候选；文件损坏/记录非法都返回 None（由调用方记 U，不抛异常）。"""
    path = os.path.join(ep_dir, "candidates.jsonl")
    if not candidate_id or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or d.get("candidate_id") != candidate_id:
            continue
        try:
            data = {k: v for k, v in d.items() if k in AnswerCandidate.__dataclass_fields__}
            # 候选是从 asdict 落盘的：claims 是普通 dict，读回时必须还原为 Claim
            data["claims"] = [
                Claim(**{k: v for k, v in c.items() if k in Claim.__dataclass_fields__}) if isinstance(c, dict) else c
                for c in (data.get("claims") or [])
            ]
            return AnswerCandidate(**data)
        except TypeError:
            return None
    return None


# --------------------------------------------------------------------------
# 报告（E05）：逐题 + 汇总量，不重跑任何模型
# --------------------------------------------------------------------------
def report_run(root: str) -> Dict[str, Any]:
    root = os.path.abspath(root)
    plan = load_plan_from_root(root)
    eval_root = os.path.join(root, "evaluations")
    evals = (
        sorted(
            (p for p in os.listdir(eval_root) if os.path.isdir(os.path.join(eval_root, p))),
            key=lambda p: os.path.getmtime(os.path.join(eval_root, p)),
        )
        if os.path.isdir(eval_root)
        else []
    )
    latest = os.path.join(eval_root, evals[-1]) if evals else ""
    metrics = json.load(open(os.path.join(latest, "metrics.json"), encoding="utf-8")) if latest else {}
    records = []
    if latest and os.path.exists(os.path.join(latest, "records.jsonl")):
        with open(os.path.join(latest, "records.jsonl"), encoding="utf-8") as f:
            records = [json.loads(l) for l in f if l.strip()]
    calls_by_role: Dict[str, int] = {}
    calls_by_stage: Dict[str, int] = {}
    for ep in plan["episodes"]:
        path = os.path.join(root, "episodes", ep["episode_id"], "events.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                ev = json.loads(line)
                if ev.get("type") == "request_sent":
                    calls_by_role[ev.get("role", "")] = calls_by_role.get(ev.get("role", ""), 0) + 1
                    calls_by_stage[ev.get("stage_id", "")] = calls_by_stage.get(ev.get("stage_id", ""), 0) + 1
    eval_ledger = {}
    if latest and os.path.exists(os.path.join(latest, "ledger.json")):
        eval_ledger = json.load(open(os.path.join(latest, "ledger.json"), encoding="utf-8"))
    report = {
        "experiment_id": plan.get("experiment_id"),
        "episodes": len(plan["episodes"]),
        "eval_run_id": os.path.basename(latest) if latest else "",
        "metrics": {
            k: metrics.get(k)
            for k in ("N", "S", "F", "U", "asr_point", "asr_task_range", "task_eval_coverage", "any_success_rate")
        },
        "calls_by_role": calls_by_role,
        "calls_by_stage": calls_by_stage,
        "eval_calls": eval_ledger.get("calls", {}),
        "per_episode": records,
        "models": [
            {
                "role": row.get("role"),
                "service_id": row.get("service_id"),
                "model": row.get("model"),
                "parameter_profile": row.get("parameter_profile"),
                "resolved_params": row.get("resolved_params"),
                "capability_status": row.get("capability_status"),
            }
            for row in plan.get("model_manifest", [])
        ],
        "prompt_manifest": plan.get("prompt_manifest", []),
        "note": "ASR 点值是任务等权下界；有 U 时必须同时报 asr_task_range。评价覆盖低时不能把 0% 当作方法效果。",
    }
    write_json(report, os.path.join(root, "report.json"))
    return report


# --------------------------------------------------------------------------
# 恢复与重算（不调用模型）
# --------------------------------------------------------------------------
def restore_episode(ep_dir: str) -> Dict[str, Any]:
    from .events import EventStore

    ep_id = os.path.basename(ep_dir.rstrip("/"))
    root = os.path.dirname(os.path.dirname(ep_dir))
    store = EventStore(root, ep_id)
    return {"episode_id": ep_id, "events": len(store.events), "ledger": store.ledger(), "terminated": bool(store.terminations())}


def analyze_nodes(ep_dir: str) -> Dict[str, Any]:
    records = []
    for line in open(os.path.join(ep_dir, "events.jsonl"), "r", encoding="utf-8"):
        ev = json.loads(line)
        if ev["type"] == "state_write":
            records.append({"seq": ev["seq"], "node": ev.get("node_id"), "new_facts": len(ev["payload"].get("new_facts", []))})
    return {"episode": os.path.basename(ep_dir.rstrip("/")), "nodes": records, "note": "节点级干预需要重跑装配，属 analysis 成本"}
