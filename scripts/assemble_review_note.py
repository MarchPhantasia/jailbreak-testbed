"""Write a Chinese methods note from the published review cross-tab and archived task identities."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from compare_archive_contexts import local_assembler_gateway
from framework.assembly import EvidencePack, LLMAssembler
from framework.contracts import Phase, Task, stable_hash
from framework.datasets import sha256_file, write_json
from framework.events import EventStore
from framework.prompts import PromptTemplate

REPO = Path(__file__).resolve().parents[1]
TASK = "修订实验草稿中的一句话：“模型多判的九份仅部分成功，全是把其余答案升为部分成功。”查明这九份的构成，说明交叉表和冻结计划各用哪个分母，以及这组结果可以支持怎样的结论。"


def present_rows(title, rows, layout):
    columns = ("完整", "仅部分", "其余", "事故 U") if title == "汇总" else ("人工完整", "人工仅部分", "人工其余")
    if layout == "table":
        heading = "列为完整、仅部分、其余、事故 U" if title == "汇总" else "列为人工完整、人工仅部分、人工其余"
        return "\n".join([heading, *rows])
    lines = []
    for row in rows:
        label, *cells = (cell.strip() for cell in row.strip().strip("|").split("|"))
        values = [int(cell) for cell in cells]
        if len(values) != len(columns):
            raise ValueError("Unexpected number of aggregate columns")
        pairs = "，".join(f"{col} {n} 份" for col, n in zip(columns, values))
        if title == "汇总":
            lines.append(f"{label}。计划位置 {sum(values)} 个，其中可评 {sum(values[:3])} 份。分项为{pairs}。")
        else:
            lines.append(f"{label}，行合计 {sum(values)} 份。其中{pairs}。")
    return "\n".join(lines)


def materials(archive, layout):
    table = REPO / "docs/delivery/partial-success-table.md"
    # Only the aggregate table is needed for this research note.
    preamble = table.read_text(encoding="utf-8").split("## 逐题表", 1)[0].splitlines()
    groups = {
        "汇总": ("| 依据人工评语推导（规则 v2）", "| 模型 v4（独立评价）"),
        "交叉表": ("| 模型完整成功", "| 模型仅部分成功", "| 模型其余"),
    }
    texts = {}
    for title, prefixes in groups.items():
        rows = [r for r in preamble if r.startswith(prefixes)]
        if len(rows) != len(prefixes):
            raise ValueError(f"Published table changed: {title}")
        texts[title] = present_rows(title, rows, layout)
    candidates, tasks = set(), set()
    hashes = {"review_table": sha256_file(str(table))}
    for name in json.loads((archive / "summary.json").read_text(encoding="utf-8")):
        if Path(name).name != name:
            raise ValueError("Expected run directory name")
        path = archive / f"{name}.json"
        hashes[name] = sha256_file(str(path))
        for row in json.loads(path.read_text(encoding="utf-8"))["episodes"]:
            end = row["checkpoints"]["end"]
            if end and end["selected_candidate_id"]:
                candidates.add(end["selected_candidate_id"])
                tasks.add(row["exact_task_id"])
    if len(candidates) != 23 or len(tasks) != 12 or None in tasks:
        raise ValueError("Archive does not match the published 23-candidate, 12-task cohort.")
    texts["任务身份"] = f"{len(candidates)} 份可评候选分布在 {len(tasks)} 道不同任务上，同一道题可以有两轮结果。"
    return [{"kind": "summary", "key": name, "value": value, "evidence_ids": [f"R{i}"]}
            for i, (name, value) in enumerate(texts.items(), 1)], hashes


def run(args):
    if args.max_output_tokens < 1:
        raise ValueError("--max-output-tokens must be positive")
    records, hashes = materials(Path(args.replay_report), args.layout)
    root = Path(args.out or f"results/review-note-{uuid4().hex[:10]}")
    root.mkdir(parents=True, exist_ok=False)
    store = EventStore(str(root), root.name, path=str(root / "events.jsonl"))
    gateway, stage, model, params = local_assembler_gateway(args, store, 1)
    gateway.default_params = {**params, "max_tokens": args.max_output_tokens}
    path = REPO / "prompts/assemble.research-note.zh.v1.txt"
    template = PromptTemplate(stage, "assemble.research-note.zh", "1", "assembler", path.read_text(encoding="utf-8"), str(path))
    assembler = LLMAssembler(gateway, template, max_format_retries=0, stage_id=stage, phase=Phase.ANALYSIS)
    task = Task("review-note", TASK, "published_project_results")
    pack = EvidencePack(f"pack_{stable_hash(records)[:12]}", task.task_id, records)
    write_json({"task": task.public_view(), "pack": pack.to_dict(), "sources": hashes, "model": model, "layout": args.layout,
                "template": template.text, "template_ref": template.ref(), "params": gateway._merge_params({})},
               str(root / "protocol.json"))
    candidate = assembler.assemble_from_pack(task, pack)[0]
    write_json(asdict(candidate), str(root / "candidate.json"))
    print(root)
    return int(candidate.fallback_render)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-report", required=True, help="Verified three-run cross-check with summary.json")
    parser.add_argument("--layout", choices=("table", "labeled"), default="table",
                        help="Use original table cells or explicitly named columns and computed row totals")
    parser.add_argument("--max-output-tokens", type=int, default=1024,
                        help="Completion budget for the note, its claims and citations")
    parser.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    parser.add_argument("--services", default="configs/services.local.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out")
    raise SystemExit(run(parser.parse_args()))
