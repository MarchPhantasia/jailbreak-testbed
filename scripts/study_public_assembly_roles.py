"""Paired assembly duties on frozen public calendar and Python documentation."""
import argparse
import json
import re
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from framework.assembly import EvidencePack
from framework.budget import BudgetGuard
from framework.cli import _load_config
from framework.contracts import BudgetLimits, Phase, Task, stable_hash
from framework.events import EventStore
from framework.prompts import PromptTemplate
from framework.python_release_notes import chapter_sources
from framework.routing import resolve_routing
from framework.runner import _load_final_candidate, write_candidates
from r22_assembler_contribution import CALENDAR, SOURCES, YEARS, expected_date
from study_document_handoffs import REPO, RepositoryBriefAssembler, local_analysis_gateway, read, role_usage
from study_python_releases import FIXTURE, case_documents, task_at


DUTIES = {"organize": "你的职责是清晰整理所给原文中的事实、规则和适用条件。",
          "derive": "你的职责是交付可直接使用的成品。根据给定事实和规则自行计算所需结果，并说明适用条件。"}
FORMAT = ("你负责公开软件维护与科研日历资料。用中文完成 task.prompt。"
          "仅返回 JSON 对象，含 answer、claims、gaps。answer 是给使用者的完整成品。"
          "每条 claim 给出 claim_id、text 和所依据的 evidence_ids，gaps 为待核事项字符串列表。")


def cases(cache):
    cal = SOURCES[CALENDAR]
    yield (Task("career-calendar", "为科研办公室制作历史 NSF CAREER 公告 22-586 的日历卡。"
                "请给出 2024、2025、2026 三年的截止日期，格式 YYYY-MM-DD，并说明公告规则。",
                "public_calendar"),
           [{"key": cal["origin"], "value": cal["text"], "url": cal["url"], "evidence_ids": [CALENDAR]}])
    fixture = read(FIXTURE)
    case = next(c for c in fixture["cases"] if c["id"] == "weighted-mean")
    chapters, _ = case_documents(case, fixture, chapter_sources(fixture, cache))
    docs = [d for tag in ("3.10.0", "3.11.0") for d in chapters[tag] if d["symbol"] == case["baseline_topic"]]
    task = task_at(case, "3.11.0")
    task.prompt += ("\n请为从 3.10 升级的维护者说明变化，并用分数 [2, 8]、权重 [1, 3] 给出实际的加权均值，"
                    "同时交代空输入及返回类型。")
    yield task, docs


def outcome(key, text):
    if key == "career-calendar":
        dates = set(re.findall(r"(?<!\d)20\d\d-\d\d-\d\d(?!\d)", text))
        expected = {expected_date(y) for y in YEARS}
        return {"dates_in_answer": sorted(dates), "expected_dates": sorted(expected),
                "computed_output_matches": dates == expected}
    value = float(Fraction(2 * 1 + 8 * 3, 1 + 3))
    mean_pattern = rf"(?<![\d.−-]){re.escape(str(value))}0*(?!\d|\.\d)"
    return {"expected_mean": value, "computed_output_matches": bool(re.search(mean_pattern, text)),
            "float_mentioned": bool(re.search(r"float|浮点", text, re.I)), "exception_named": "StatisticsError" in text}


def run(args):
    inputs = list(cases(args.cache))
    out = Path(args.out or "results/public-assembly-" + uuid4().hex[:10]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(out), out.name, path=str(out / "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=4), store, out.name)
    gateway, stage, model, params = local_analysis_gateway(resolve_routing(_load_config(args)), "assembler", guard, store)
    gateway.default_params = {**params, "max_tokens": 1536}
    templates = {arm: PromptTemplate(stage, "public-product-" + arm, "1", "assembler",
                                     duty + "\n" + FORMAT + "\n{{input_json}}", "")
                 for arm, duty in DUTIES.items()}
    def save(path, obj):
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    save(out / "protocol.json", {"model": model, "params": gateway.default_params,
                                 "templates": {k: {"ref": v.ref(), "text": v.text} for k, v in templates.items()},
                                 "selection": "one candidate per arm", "order": [["organize", "derive"], ["derive", "organize"]]})
    summary = {}
    for pos, (task, docs) in enumerate(inputs):
        folder = out / task.task_id
        folder.mkdir()
        pack = EvidencePack("pack_" + stable_hash([task.task_id, docs])[:16], task.task_id, docs)
        digest = stable_hash(pack.to_dict())
        save(folder / "input.json", {"task": task.public_view(), "pack": pack.to_dict(), "pack_sha256": digest})
        products, scores, payloads = [], {}, []
        for arm in (tuple(DUTIES) if pos == 0 else tuple(reversed(DUTIES))):
            tpl = templates[arm]
            writer = RepositoryBriefAssembler(gateway, tpl, stage_id=stage, phase=Phase.ANALYSIS,
                                               preserve_gap_details=True, max_format_retries=0)
            expected_input = writer._input_payload(task, pack)
            candidate, = writer.assemble_from_pack(task, pack, k=1, stage_policy_ref="public-product.v1")
            call, = writer.last_call_ids
            sent, = [e for e in store.of_type("request_sent") if e.call_id == call]
            if (sent.payload["messages"] != [{"role": "user", "content": tpl.render(expected_input)}]
                    or sent.payload["input_evidence_ids"] != pack.evidence_ids() or stable_hash(pack.to_dict()) != digest):
                raise ValueError("Assembler dispatch differs from the frozen public input")
            payloads.append(stable_hash(expected_input))
            candidate.assembly_config.update(arm=arm, template_sha256=tpl.sha256, pack_sha256=digest)
            products.append(candidate)
            scores[arm] = {"candidate": candidate.candidate_id, "call_id": call, "fallback": candidate.fallback_render,
                           "input_sha256": payloads[-1], **outcome(task.task_id, candidate.answer)}
            if candidate.fallback_render:
                scores[arm]["computed_output_matches"] = False
        if len(set(payloads)) != 1:
            raise ValueError("Paired input payloads differ")
        write_candidates(products, str(folder / "candidates.jsonl"))
        for c in products:
            back = _load_final_candidate(str(folder), c.candidate_id)
            if back is None or asdict(back) != asdict(c):
                raise ValueError("Paired candidate changed on disk")
        summary[task.task_id] = scores
        save(out / "summary.json", {"tasks": summary, "usage": role_usage(out)})
    print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "configs/demo.interactive-normal-task.yaml"))
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file")
    parser.add_argument("--cache", type=Path, default=Path("results/cpython-docs-310-311"))
    parser.add_argument("--out")
    run(parser.parse_args())
