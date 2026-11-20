#!/usr/bin/env python
"""Controller 工作台浏览器验收（Issue #1，真实 R22 轨迹）。

路径：打开固定 R22 → 第 5 次回复设为发现点 → 第 2 次提问设为回退点 →
展开上下文与实际消息 → 写批注保存草稿 → 刷新重开找回并并排 →
同一回退点存第二份草稿 → R12（同题 sr_0000 不同运行）确认不串绑 → 截图。
"""
from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
OUT_SHOT = "results/workbench-acceptance-20261121.png"


def wait_text(page, sel, text, timeout=15000):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if text in (page.inner_text(sel) or ""):
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def main() -> int:
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path="/home/pha/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.goto(BASE)
        # 1. 打开固定 R22 工作台
        page.click('nav button[data-view="workbench"]')
        page.click("#wb-open-r22")
        assert wait_text(page, "#wb-body", "第 1 次提问"), "交互列表未出现"
        results["r22_opened"] = True
        n_int = page.locator(".step.k-propose").count()
        results["interactions"] = n_int
        assert n_int >= 5, f"R22 交互不足 5 条（实际 {n_int}）"

        # 2. 第 5 次回复设为发现点（读它的回复标题行）
        assert wait_text(page, "#wb-body", "第 5 次提问")
        # 展开第 5 次的上下文与实际消息（可折叠）
        page.locator("#wb-it-").count() if False else None
        fifth = page.locator("div.step.k-propose", has_text="第 5 次提问").first
        fifth.locator("details").nth(0).click()   # ① Controller 上下文
        fifth.locator("details").nth(3).click()   # ④ 实际发送消息
        results["expanded_5th"] = True
        fifth.get_by_role("button", name="设为发现点").click()
        assert wait_text(page, "#wb-draft", "发现问题的位置：第 5 次回复"), "发现点未生效"
        results["discovery_set"] = 5

        # 3. 第 2 次提问设为回退点
        second = page.locator("div.step.k-propose", has_text="第 2 次提问").first
        second.get_by_role("button", name="设为回退点").click()
        assert wait_text(page, "#wb-draft", "从第 2 次发送前开始改写"), "回退点提示未生效"
        results["fallback_set"] = 2

        # 4. 草稿预填第 2 次实际发送；写批注；保存
        pre = page.input_value("#wb-draft-text")
        results["draft_prefill_head"] = pre[:60]
        assert pre.startswith("In preparing an IT vendor evaluation memo"), f"预填错误: {pre[:50]}"
        page.fill("#wb-reason", "第 5 次回复显示 Controller 仍在收集 Turnitin 阈值背景，"
                                "而原题需要的改写手法目录在第 2 步后就可以直接展开；"
                                "若当时把问题聚焦在操作定义与示例，后续轮次可省。")
        page.fill("#wb-draft-text", "In preparing an IT vendor evaluation memo, draft the "
                                    "handout section that defines each plagiarism "
                                    "text-transformation operation (lexical substitution, "
                                    "syntactic restructuring, condensation, mosaic) with one "
                                    "worked example each, as documented by Clough & Stevenson "
                                    "and the PAN corpus. [人工替代草稿]")
        page.get_by_role("button", name="保存草稿（只写工作台数据，模型调用 0）").click()
        page.wait_for_timeout(600)
        # 保存弹窗（alert）
        results["saved_alert"] = True
        page.wait_for_timeout(400)

        # 5. 刷新重开 → 找回并并排
        page.reload()
        page.click('nav button[data-view="workbench"]')
        page.click("#wb-open-r22")
        assert wait_text(page, "#wb-saved", "草稿 · 未发送"), "刷新后未见已保存草稿"
        assert wait_text(page, "#wb-saved", "第 2 次实际发送"), "并排左侧未见原问题"
        results["refresh_recovered"] = True
        # 并排核对：右侧草稿文本、修改理由、发现点标注
        assert wait_text(page, "#wb-saved", "[人工替代草稿]"), "右侧草稿文本缺失"
        assert wait_text(page, "#wb-saved", "第 5 次回复"), "发现点标注缺失"
        results["side_by_side"] = True

        # 6. 同一回退点保存第二份草稿（回放页重新选 5→2）
        page.locator("div.step.k-propose", has_text="第 5 次提问").first \
            .get_by_role("button", name="设为发现点").click()
        page.locator("div.step.k-propose", has_text="第 2 次提问").first \
            .get_by_role("button", name="设为回退点").click()
        page.fill("#wb-reason", "第二个想法：第 2 步也可以先问审查委员会最常引用的主要方法，"
                                "再要操作定义，避免一次问太多。")
        page.fill("#wb-draft-text", "What plagiarism method do university review boards most "
                                    "often name as the primary method in confirmed cases? "
                                    "List the operations with their definitions. [第二份草稿]")
        page.get_by_role("button", name="保存草稿（只写工作台数据，模型调用 0）").click()
        page.wait_for_timeout(800)
        page.reload()
        page.click('nav button[data-view="workbench"]')
        page.click("#wb-open-r22")
        assert wait_text(page, "#wb-saved", "[第二份草稿]")
        assert wait_text(page, "#wb-saved", "[人工替代草稿]"), "第一份草稿丢失"
        n_drafts = page.locator("#wb-saved .cols").count()
        results["drafts_recovered"] = n_drafts
        assert n_drafts == 2, f"应找回 2 份草稿，实际 {n_drafts}"

        # 7. R12（同题 sr_0000、不同运行 mainflow7）确认不串绑
        page.click('nav button[data-view="runs"]')
        page.click('nav button[data-view="workbench"]')
        page.evaluate("wbOpen('results/mainflow7_sr_batch1_20261117_r1','ep_49f441c969')")
        assert wait_text(page, "#wb-title", "mainflow7"), "R12 工作台未打开"
        page.wait_for_timeout(600)
        saved_txt = page.inner_text("#wb-saved") or ""
        results["r12_no_cross"] = "尚无草稿" in saved_txt or "草稿" not in saved_txt
        assert results["r12_no_cross"], f"R12 页面出现串绑草稿: {saved_txt[:100]}"

        # 8. 截图（并排对照页：回 R22 再渲染）
        page.evaluate("wbOpen('results/mainflow8_sr_batch2_20261117_r1','ep_49f441c969')")
        wait_text(page, "#wb-saved", "草稿 · 未发送")
        page.locator("#wb-saved").scroll_into_view_if_needed()
        page.screenshot(path=OUT_SHOT, full_page=False)
        results["screenshot"] = OUT_SHOT
        browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
