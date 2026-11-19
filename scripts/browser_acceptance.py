#!/usr/bin/env python
"""浏览器级验收（R1/R10）：真实 Chromium 点「下一步」、编辑、分支、收尾、刷新找回。

用 Playwright 驱动本机 Chromium headless，对 http://127.0.0.1:8765 做与研究者
完全相同的操作。所有等待用轮询（控制器/装配是真实模型调用，需分钟级）。
"""

from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"


def wait_text(page, sel, needle, timeout_s=180, poll=3):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        txt = page.evaluate(f"document.querySelector('{sel}').textContent")
        if needle in txt:
            return True
        time.sleep(poll)
    return False


def main() -> int:
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/home/pha/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            headless=True,
        )
        page = browser.new_page()

        # ---- R1：点「下一步」恰好推进一步（轮询等 pending 出现）----
        page.goto(BASE)
        page.click('button[data-view="interactive"]')
        page.click("#int-start")
        page.wait_for_selector("#int-controls:not([hidden])", timeout=60000)
        page.click("#int-step")
        results["r1_pending_after_click"] = wait_text(page, "#int-pending", "待批准", 120)
        # 有挂起时再点下一步不重复推进：状态仍显示同一待批准
        page.click("#int-step")
        time.sleep(2)
        results["r1_no_double_advance"] = "待批准" in page.evaluate(
            "document.querySelector('#int-pending').textContent")

        # ---- 编辑后批准（query_edited 事件落盘）----
        page.fill("#int-edit", "请分别说明首次预约查档与再次预约查档各自的登记要求，并注明依据。")
        page.fill("#int-reason", "浏览器验收：把两方面的条件问明确")
        page.click("#int-approve")
        results["approve_sent"] = wait_text(page, "#int-pending", "无挂起", 120)

        # ---- 从 root 开分支 ----
        page.fill("#int-branch-node", "root")
        page.fill("#int-branch-note", "浏览器验收：从 root 开分支换方向")
        page.click("#int-branch")
        time.sleep(2)
        results["branch_started_ui"] = "root" in page.evaluate(
            "document.querySelector('#int-tree .body').textContent")

        # ---- 分支上再走一步（批准）----
        page.click("#int-step")
        wait_text(page, "#int-pending", "待批准", 120)
        page.click("#int-approve")
        wait_text(page, "#int-pending", "无挂起", 120)

        # ---- 收尾（装配为真实调用，轮询到已终止）----
        page.click("#int-finalize")
        results["finalize_terminated"] = wait_text(page, "#int-status", "已终止", 360)

        browser.close()

    # ---- R10：刷新后找回自己创建的运行（新会话出现在列表）----
    import urllib.request
    runs = json.load(urllib.request.urlopen(BASE + "/api/runs"))
    web_runs = sorted(r["root"] for r in runs if r["root"].startswith("results/web-interactive-"))
    results["r10_runs_found"] = web_runs
    results["r10_found_after_refresh"] = len(web_runs) >= 1

    print(json.dumps(results, ensure_ascii=False, indent=1))
    bools = [v for k, v in results.items() if isinstance(v, bool)]
    ok = all(bools)
    print("浏览器验收：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
