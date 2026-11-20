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
    return {**card, "status": "complete" if n else "empty_plan", "scored": scored,
            "percent": {"lower": _percent(success, n), "upper": _percent(success + unknown, n),
                        "scored_only": _percent(success, scored)}}
