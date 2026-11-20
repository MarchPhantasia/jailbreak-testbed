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
