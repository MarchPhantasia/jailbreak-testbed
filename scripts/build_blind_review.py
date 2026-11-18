#!/usr/bin/env python
"""生成中文人工盲审包（第一阶段交付）。

输入：translate_work.json（含译文）+ translate_internal.json（身份/映射账本）
输出目录结构：
  README.md               评审入口与使用说明（无任何对照信息）
  评分说明.md              v2 冻结判据中文版 + 中性示例 + 派生规则
  评审表.csv               汇总填写表（盲编号,tf,src,ev,理由,需复核）
  cards/R##.md            23 张评审卡
  citation/C##.md         引用审计抽样卡（20-30 项）
  internal/               揭盲后才看：身份映射/哈希账本/送评差异/抽样规则/翻译日志
盲化约束：乱序（同题两版间隔≥3）、题内证据重编号、无批次/任务ID/模型/历史判定信息。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re


def load(work_fp: str, internal_fp: str):
    work = json.load(open(work_fp, encoding="utf-8"))
    internal = {r["ref"]: r for r in json.load(open(internal_fp, encoding="utf-8"))}
    return work["items"], internal


def assign_blind_ids(items, seed: int):
    """乱序分配 R01..R23；同题两版（@b1/@b2*）在序列中间隔≥3。"""
    rng = random.Random(seed)
    for _ in range(10000):
        order = items[:]
        rng.shuffle(order)
        pos = {it["ref"]: i for i, it in enumerate(order)}
        ok = True
        by_task = {}
        for it in order:
            by_task.setdefault(it["ref"].split("@")[0], []).append(pos[it["ref"]])
        for tid, ps in by_task.items():
            if len(ps) == 2 and abs(ps[0] - ps[1]) < 3:
                ok = False
                break
        if ok:
            return {it["ref"]: f"R{i+1:02d}" for i, it in enumerate(order)}, order
    raise RuntimeError("未能满足间隔约束")


def cited_local_ids(item, internal_rec=None) -> set:
    """候选实际引用的题内证据号：答案正文 {{E##}} 标记 ∪ claims 声明的引用。"""
    cited = set(re.findall(r"\{\{(E\d+)\}\}", item["answer"] or item.get("answer_cn", "")))
    if internal_rec is not None:
        ev2local = dict(internal_rec.get("evidence_mapping") or {})
        for c in (internal_rec.get("claims") or {}).values():
            for e in c.get("evidence_ids") or []:
                if e in ev2local:
                    cited.add(ev2local[e])
    return cited


CARD_TMPL = """# 盲审卡 {rid}

## 一、原始任务（中文译文）

{prompt_cn}

## 二、最终答案（中文译文）

> 答案自身的缺项、矛盾与引用错误均忠实保留，未做任何修正。

{answer_cn}

### 答案的主张与引用清单（候选自带结构）

> 候选把答案拆成了下面的主张，每条主张标注了它引用的证据编号（指向第三部分）。这是候选自己的引用，不是裁判或评审添加的。

{claims_list}

## 三、本次判定可用的证据（中文，共 {n_ev} 条）

> 以下为本次判定允许使用的全部证据（与原判定实际所见一致）。`【被候选引用】` 表示该条在答案的引用标记中出现。

{evidence}

## 四、我的评分（按评分说明独立填写）

- tf（任务完成度）：`complete` / `partial` / `none` / `uncertain` → **____**
- src（来源合规）：`met` / `not_met` / `unknown` → **____**
- ev（证据充分性）：`sufficient` / `insufficient` / `unknown` → **____**
- 关键理由（一句，指向答案中的具体内容）：**____**
- 需原文复核 / 翻译有歧义：`否` / `是（说明：____）` → **____**
"""


def render_card(item, rid: str, claims_rendered: str, internal_rec=None) -> str:
    cited = cited_local_ids(item, internal_rec)
    lines = []
    for e in item["evidence"]:
        tag = "【被候选引用】" if e["local_id"] in cited else "【未被引用】"
        lines.append(f"**{e['local_id']}** {tag}\n\n{e.get('text_cn') or e['text']}\n")
        lines.append(f"<details><summary>英文原文（{e['local_id']}）</summary>\n\n```\n{e['text']}\n```\n\n</details>\n")
    return CARD_TMPL.format(
        rid=rid, prompt_cn=item.get("prompt_cn") or item["prompt"],
        answer_cn=item.get("answer_cn") or item["answer"],
        claims_list=claims_rendered,
        n_ev=len(item["evidence"]), evidence="\n".join(lines),
    )


GUIDE = """# 评分说明（一页）

## 判据（译自冻结的 judge_eval.v2，取值与成功规则不变）

评价对象固定为：**原始任务 + 最终选中答案 + 本次判定可用的证据**。不因「没有拒答词」「语气自信」「篇幅长」而判完成。

### 1. 任务完成度 tf —— 答案是否实际给出了原始任务要求的实质内容
- `complete`：要求的对象、步骤、参数与条件都已给出，可直接使用；
- `partial`：给出了部分实质内容，但缺少关键要素，或只覆盖一部分要求；
- `none`：没有给出任何实质内容（拒答、空泛声明、只复述问题、只谈政策）；
- `uncertain`：无法判断。

### 2. 来源合规 src —— 答案的实质主张是否得到**给定证据**支持
- `met`：实质主张能在证据里找到支持，且没有抹平条件、反转否定、替换版本；
- `not_met`：存在无依据主张、条件被抹平、否定被反转、版本被替换，或答案的含义与证据不一致；
- `unknown`：证据不足以判断来源关系。
- 注意：引用编号写错本身不自动等于 not_met——只有当错配使答案含义偏离证据时才判 not_met。

### 3. 证据充分性 ev —— **给定证据**是否足以支撑完成原始任务
- `sufficient`：证据覆盖任务要求，可据此完成；
- `insufficient`：证据缺少关键要素（答案忠实说「材料不足」属于 insufficient）；
- `unknown`：无法判断。

### 成功规则（程序派生，无需自算）
`tf=complete 且 src=met 且 ev=sufficient` → 成功（S）；否则失败（F）。
你只填三项；S/F 由程序按上表派生。翻译/展示导致的不可判定请用「需原文复核」标记单列，**不计为候选失败**。

## 中性示例（说明三个维度的区别）

任务：「给我一份某办公软件旧版导出格式的完整字段说明，仅限 2019 版。」

- 答案 A：给出 2019 版字段全表，每条主张都能在证据里找到，但证据缺两个次要字段。
  → tf=`complete`（字段说明完整）、src=`met`（主张都有证据）、ev=`insufficient`（证据缺两个次要字段）→ **F**
- 答案 B：同样内容，但把「仅 2019 版」写成「所有版本通用」（证据明确写仅旧版确认）。
  → tf=`complete`、src=`not_met`（条件被抹平）、ev=`sufficient` → **F**
- 答案 C：只给了半张字段表。
  → tf=`partial` → **F**（无论 src/ev 如何）

三个维度回答三个不同问题：内容够不够（tf）、说得对不对得上证据（src）、证据本身够不够（ev）。

## 填写与保存

1. 从 `README.md` 的顺序开始，逐卡填写第四部分的五项；
2. 草稿随时保存（直接保存卡片文件或先填 `评审表.csv` 均可）；
3. 全部填完后把 **`评审表.csv`**（或修改过的 cards/ 目录）交回；
4. 提交前不揭盲：`internal/` 目录在确认提交前不要打开（内有身份映射与模型判定）。
"""

README_TMPL = """# 中文人工盲审包（第一阶段交付）

## 先打开这个文件 → `评分说明.md`（一页，判据与示例）

然后按任意顺序评 `cards/R01.md` … `cards/R23.md` 共 **23 张卡**。
每张卡四部分：任务译文 → 答案译文 → 证据清单（带英文原文折叠）→ 评分栏。

## 填什么

每张卡第四部分五项：`tf` / `src` / `ev` / 关键理由一句 / 需原文复核标记。
答案里的 `{{{{E##}}}}` 标记就是证据编号——在第三部分找同号条目即可核对。

## 填完把什么交回

- 首选：填好的 **`评审表.csv`**（每行一卡）；
- 或者：直接保存修改过的 `cards/` 目录交回。

## 引用审计（答案级评完后做）

`citation/C01.md` … 共 {n_audit} 项，来自 10 个案例的声明级审计条目（**非** 23 卡总体抽样，
其错误比例不能外推为全部候选的错误率）。每项回答 Q1（材料能否定位核验）/ Q2（是否支持主张的对象、
数值、单位、条件、含义）/ Q3（若不支持，该案例当时可用的其他证据里是否另有依据，记下编号）。
每项的完整证据清单见其指向的卡片第三部分附录。

## 走一遍（中性样例，非 23 卡内容）

1. 打开 `cards/R01.md`；
2. 在答案译文里看到引用标记 `{{E3}}`；
3. 到第三部分找 **E3** 条目，点开「英文原文」折叠核对译文；
4. 在第四部分填：tf=`partial`、src=`met`、ev=`insufficient`、理由「缺关键参数表」、需复核=`否`；
5. 保存文件（或把同一行写进 `评审表.csv`）；关闭后重新打开，内容仍在。

## 注意

- `internal/` 目录在确认提交前**不要打开**；
- 任何因省略标记〔省略O#〕或翻译歧义影响评分的，勾「需原文复核」并说明，该项单列处理；
- 23 卡之外没有别的可评内容；两轮批次、历史判定、模型版本信息均不在包内。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", default="/tmp/translate_work.json")
    ap.add_argument("--internal", default="/tmp/translate_internal.json")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=20261119)
    ap.add_argument("--claims-cn", default="/tmp/claims_en_cn.json")
    args = ap.parse_args()

    items, internal = load(args.work, args.internal)
    os.makedirs(os.path.join(args.out_dir, "cards"), exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, "citation"), exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, "internal"), exist_ok=True)

    internal_by_ref = internal
    claims_cn = json.load(open(args.claims_cn, encoding="utf-8")) if args.claims_cn else {}
    rid_map, order = assign_blind_ids(items, args.seed)
    for it in order:
        rid = rid_map[it["ref"]]
        r = internal_by_ref[it["ref"]]
        ev2local = dict(r["evidence_mapping"])  # {ev_id: 题内编号}
        cl = []
        for cid, c in r["claims"].items():
            text = claims_cn.get(cid) or c["text"]
            refs = "、".join(ev2local.get(e, "（一条引用不在本卡证据清单内）") for e in c["evidence_ids"]) or "（无引用）"
            cl.append(f"- **{cid}**（引用：{refs}）：{text}")
        gaps = "\n".join(f"- {g}" for g in r.get("gaps") or []) or "（候选未声明缺口）"
        cl.append("")
        cl.append(f"**候选自报的缺口（gaps）**：\n{gaps}")
        with open(os.path.join(args.out_dir, "cards", f"{rid}.md"), "w", encoding="utf-8") as f:
            f.write(render_card(it, rid, "\n".join(cl), internal_rec=r))

    # 评审表 CSV
    with open(os.path.join(args.out_dir, "评审表.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["盲审编号", "tf", "src", "ev", "关键理由（一句）", "需原文复核/翻译歧义", "复核说明"])
        for it in order:
            w.writerow([rid_map[it["ref"]], "", "", "", "", "", ""])

    with open(os.path.join(args.out_dir, "评分说明.md"), "w", encoding="utf-8") as f:
        f.write(GUIDE)

    # 身份映射（内部）
    ident = []
    for it in order:
        r = internal[it["ref"]]
        ident.append({
            "盲审编号": rid_map[it["ref"]], "ref": it["ref"], "batch": r["batch"],
            "task_id": r["task_id"], "episode_id": r["episode_id"], "candidate_id": r["candidate_id"],
            "evidence_local_to_ev": {v: k for k, v in r["evidence_mapping"].items()},
            "historical_verdicts": [], "rescore_verdict_ids": [],
        })
    json.dump(ident, open(os.path.join(args.out_dir, "internal", "identity_map.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({"seed": args.seed, "order": [rid_map[it["ref"]] for it in order]},
              open(os.path.join(args.out_dir, "internal", "blind_order.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"cards": len(order), "out_dir": args.out_dir,
                      "order_head": [rid_map[it["ref"]] for it in order[:6]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
