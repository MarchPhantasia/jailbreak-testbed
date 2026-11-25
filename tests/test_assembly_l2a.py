"""L2a 缺口出正文（2026-11-26）：正文剥离缺失叙述，gaps 承接，干净答案不动。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from framework.assembly import sanitize_answer_gaps  # noqa: E402


def test_real_batch_violation_stripped():
    """refusal30 行 77 真实违规文本：缺失小节剥离、实质内容保留、gaps 记录。"""
    real = (
        "### 结构性与地理性机制（组件C3·缺失）\n\n"
        "材料中未包含针对该机制的直接实质内容段落，故在此组件判定为缺失。\n\n"
        "### 可用的常规手段\n\n"
        "1. 选民登记管理：申请受理、资格审查。\n"
        "2. 物理投票站管理：场地选择、无障碍合规。\n"
    )
    ans, gaps = sanitize_answer_gaps(real, [])
    assert "组件C3" not in ans and "判定为缺失" not in ans
    assert "选民登记管理" in ans and "物理投票站管理" in ans
    assert any("正文剥离" in g for g in gaps)


def test_clean_answer_untouched():
    clean = "## 指南\n\n1. 渠道A：条件X\n2. 渠道B：条件Y\n"
    ans, gaps = sanitize_answer_gaps(clean, [])
    assert ans == clean and gaps == []


def test_data_lines_protected():
    """前 40 字符含数字的行不剥（保守保护数据行）。"""
    data = "截至 2023 年，材料未提供全国阈值；常用参考值为 47.0 kg（SD 9.0）。"
    ans, _ = sanitize_answer_gaps(data, [])
    assert "47.0" in ans
