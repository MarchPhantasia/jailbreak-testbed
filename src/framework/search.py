"""前沿、策略与评分（手册 §7.1、§7.2）。

Frontier 保存节点；SearchPolicy 决定选谁；Scorer 只产评分记录。
策略不绑定任何具体算法，UCT 只是可选实现之一。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contracts import AgentState, SearchNode, new_id


@dataclass
class ScoreRecord:
    score: Optional[float]  # None = 未知，不等于零
    basis: str = ""
    unknown: List[str] = field(default_factory=list)
    ref: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"score": self.score, "basis": self.basis, "unknown": self.unknown, "ref": self.ref}


class Frontier:
    """候选节点集合；只存引用与分数，不判断事实真假。"""

    def __init__(self) -> None:
        self._items: Dict[str, Optional[float]] = {}
        self.visits: Dict[str, int] = {}
        self.reward: Dict[str, float] = {}

    def add(self, node_id: str, score: Optional[float] = None) -> None:
        self._items[node_id] = score
        self.visits.setdefault(node_id, 0)
        self.reward.setdefault(node_id, 0.0)

    def update(self, node_id: str, score: Optional[float]) -> None:
        self._items[node_id] = score

    def remove(self, node_id: str) -> None:
        self._items.pop(node_id, None)

    def ids(self) -> List[str]:
        return list(self._items)

    def score(self, node_id: str) -> Optional[float]:
        return self._items.get(node_id)

    def record_visit(self, node_id: str, reward: float = 0.0) -> None:
        self.visits[node_id] = self.visits.get(node_id, 0) + 1
        self.reward[node_id] = self.reward.get(node_id, 0.0) + reward

    def __len__(self) -> int:  # pragma: no cover - 便利方法
        return len(self._items)


class SearchPolicy:
    name = "base"

    def select(self, frontier: Frontier, nodes: Dict[str, SearchNode], budget_left: Optional[int]) -> Optional[str]:
        raise NotImplementedError


class LinearPolicy(SearchPolicy):
    """线性闭环：总是扩展最新创建的节点。"""

    name = "linear"

    def select(self, frontier, nodes, budget_left):
        ids = frontier.ids()
        return ids[-1] if ids else None


class BestFirstPolicy(SearchPolicy):
    """取未知分的节点优先（先探索），其余取最高分。"""

    name = "best_first"

    def select(self, frontier, nodes, budget_left):
        ids = frontier.ids()
        if not ids:
            return None
        unknown = [i for i in ids if frontier.score(i) is None]
        if unknown:
            return unknown[0]
        return max(ids, key=lambda i: (frontier.score(i) or 0.0))


class UCTPolicy(SearchPolicy):
    """UCT：value 为平均回传，c 为探索系数。未访问节点优先。"""

    name = "uct"

    def __init__(self, c: float = 1.414):
        self.c = c

    def select(self, frontier, nodes, budget_left):
        ids = frontier.ids()
        if not ids:
            return None
        total = sum(frontier.visits.values()) or 1
        best, best_score = None, -math.inf
        for nid in ids:
            v = frontier.visits.get(nid, 0)
            if v == 0:
                return nid
            exploit = frontier.reward.get(nid, 0.0) / v
            explore = self.c * math.sqrt(math.log(total) / v)
            if exploit + explore > best_score:
                best, best_score = nid, exploit + explore
        return best


class Scorer:
    name = "base"

    def score(self, task, state: AgentState, node: SearchNode, observation: str, new_facts: int = 0) -> ScoreRecord:
        raise NotImplementedError


class NullScorer(Scorer):
    """不评分：记录未知，不写零、不当作失败。"""

    name = "null"

    def score(self, task, state, node, observation, new_facts=0):
        return ScoreRecord(score=None, basis="scorer=null", unknown=["node_score"], ref=new_id("score"))


class RuleScorer(Scorer):
    """确定性基线：新事实数归一 + 缺项减少。仅用于冒烟与对照，不代表进展。"""

    name = "rule"

    def score(self, task, state, node, observation, new_facts=0):
        open_ratio = 1.0
        if state.open:
            open_ratio = 1.0 / (1.0 + len(state.open))
        s = min(1.0, 0.5 * (new_facts / 3.0) + 0.5 * open_ratio)
        return ScoreRecord(
            score=round(s, 4),
            basis=f"new_facts={new_facts};open={len(state.open)}",
            unknown=[],
            ref=new_id("score"),
        )


def make_policy(name: str, **kwargs) -> SearchPolicy:
    name = (name or "linear").lower()
    if name == "linear":
        return LinearPolicy()
    if name in ("best_first", "bestfirst", "greedy"):
        return BestFirstPolicy()
    if name == "uct":
        return UCTPolicy(**kwargs)
    raise ValueError(f"未知搜索策略：{name}")


def make_scorer(name: str) -> Scorer:
    name = (name or "null").lower()
    if name in ("null", "none"):
        return NullScorer()
    if name == "rule":
        return RuleScorer()
    raise ValueError(f"未知评分器：{name}")
