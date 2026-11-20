# Controller 人工干预工作台（Issue #1）：交付记录

- 日期：2026-11-21；基线 `eb9a49c`（从该分支开始实现）
- 调用数：Controller / Target / Assembler / Judge 全部 **0**（工作台只读档案 + 写工作台数据）

## 启动命令

```bash
cd ~/sci-deepseek/jailbreak-testbed
/home/pha/sci-deepseek/.venv/bin/python -m framework.webapp --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

## 网页操作路径（可照着走）

1. 顶部导航点 **「工作台」**；
2. 点 **「打开固定样例 R22 工作台」**（读 `data/samples/r22-regression.json` 定位
   `results/mainflow8_sr_batch2_20261117_r1` / `ep_49f441c969`；上轮 A/B 目录不参与演示）；
3. 交互轨迹按时间列出（20 次提问，同节点多次调用带「同节点第 k/总 次」标记）；
   顶部可展开**原始任务**；默认选中第一条有请求和回复的交互；
4. 每次交互五个可折叠区块，按任务书顺序：
   **① Controller 当时收到的上下文**（该次 view_snapshot，不含后来材料）
   → **② 原提议**（request_generated 的 query/理由/策略；`generation_call_id`
   可展开回查规划调用的输入与响应；关联不确定就标明）
   → **③ 人工修改记录**（query_edited；R22 是批次运行，就地标「该次运行未记录」）
   → **④ 实际发送给 Target 的消息**（完整 messages，不截短）
   → **⑤ Target 回复**（按顶层 `call_id` 配对）；
   另有入口：**「回放」页**任务记录链底部新增「打开工作台（人工回退记录）」按钮；
5. 回退判断：在某次交互上点 **「设为发现点」**（默认当前查看的这一步）与
   **「设为回退点」**（默认同一步，可改选更早的提问；页面明示
   「＝从第 N 次发送前开始改写」）；回退点下方可展开**发送前可见的上下文**
   （Controller 当时已知；晚于该步的回复不出现）；
6. **「我的批注」**写第 5 次回复为何改变判断；**「替代问题」**预填第 2 次实际发送的
   问题，编辑后点 **「保存草稿」**（只写工作台数据，模型调用 0，标「草稿 · 未发送」）；
7. **刷新/关闭重开** → 重新打开同一工作台 → 「已保存草稿」区并排显示：
   左＝原问题＋实际回复（归属原调用），右＝草稿＋修改理由＋「草稿 · 未发送」；
   点「回到发现点那一轮」可跳回触发判断的交互；
8. 同一位置可保存第二份草稿，两份均按草稿 ID 找回（展示序号仅用于显示）。

## 本次演示的实际身份（浏览器验收走完）

| 项 | 值 |
|---|---|
| 运行 | `results/mainflow8_sr_batch2_20261117_r1`（R22 原始运行） |
| episode | `ep_49f441c969`（任务 sr_0000） |
| 发现点 | 第 **5** 次回复 · `call_89072d000a88`（text_sha 91bc0198e1269634） |
| 回退点 | 第 **2** 次提问 · `call_7f6da56333a7`（text_sha 1d8546af8cb8f344） |
| 草稿 1 | `wb_c1e0abd508b5`（2026-11-20T17:22:15+0000） |
| 草稿 2 | `wb_5c3ada78cf93`（2026-11-20T17:22:17+0000，同一回退点的第二个想法） |
| 截图 | `results/workbench-acceptance-20261121.png`（并排对照页） |

**从磁盘读回草稿**：

```bash
cd ~/sci-deepseek/jailbreak-testbed
python3 - <<'PY'
import json
for l in open('data/workbench/annotations.jsonl', encoding='utf-8'):
    r = json.loads(l)
    print(r['draft_id'], r['run_root'], r['episode_id'],
          r['discovery'], r['fallback'], r['reason'][:30], r['draft_text'][:30])
PY
```

每行字段：draft_id、ts、run_root、episode_id、task_id、discovery/fallback（各含
seq+call_id+text_sha+text_head）、reason、draft_text、note。原运行目录零改动。

## 验收结果（Playwright 真实 Chromium，`scripts/workbench_acceptance.py`）

```json
{"r22_opened": true, "interactions": 20, "expanded_5th": true, "discovery_set": 5,
 "fallback_set": 2, "draft_prefill_head": "In preparing an IT vendor evaluation memo…",
 "saved_alert": true, "refresh_recovered": true, "side_by_side": true,
 "drafts_recovered": 2, "r12_no_cross": true,
 "screenshot": "results/workbench-acceptance-20261121.png"}
```

- 草稿预填＝第 2 次实际发送原文（头 60 字符核对）；
- 刷新后并排找回，两份草稿均 recoverable；
- **R12（同题 sr_0000、episode 同名、不同运行 mainflow7）工作台页无串绑**；
- 展开第 5 次交互的上下文与实际消息（可折叠全文）。

## 回归测试

新增 `tests/test_workbench.py`（4 项，全部通过；全套测试绿）：

- `test_same_task_different_runs_do_not_cross_bind`：R22/R12 同题同 episode 名，
  草稿按运行+episode 过滤，互不可见；
- `test_same_step_two_drafts_both_recoverable`：同一位置两份草稿 ID 可区分、
  锚点读回正确（seq/call_id 与该次交互一致）；
- `test_wrong_text_sha_rejected`：锚点 text_sha 与实际发送不符 → 拒绝保存（防错位）；
- `test_original_run_files_untouched`：保存只写工作台存储，原运行文件 mtime 不变。

## 已复用 / 本轮新增 / 旧档案缺失

- **已复用**：`episode_trace()` 的运行/episode 发现与 `_safe_root`；事件流按
  `call_id` 配对（request_sent/response_received 顶层字段）；`view_snapshot` 与
  `request_generated` 的既有事件；`data/samples/r22-regression.json` 固定样例清单；
  现有网页样式与 API 风格（vanilla，无构建）；`new_id`。
- **本轮新增**：`webapp.py` 的 `workbench_episode / workbench_context /
  workbench_annotations / workbench_save` 及 4 条路由（/api/workbench/*）；
  `data/workbench/annotations.jsonl` 工作台存储；index.html 工作台区块与 app.js
  工作台逻辑（交互列表、五区块详情、双锚点草稿、并排回看、回放页入口）；
  `scripts/workbench_acceptance.py` 浏览器验收；`tests/test_workbench.py`。
- **旧档案缺失**（就地标注，不影响其余信息）：R22 为批次运行——人工修改
  （query_edited）无记录，交互详情标「该次运行未记录（批次运行无人工编辑）」；
  个别交互若无 view_snapshot/规划调用，分别标「该次运行未记录」/「规划调用关联不确定」。

## 小修（2026-11-21，同视图两次请求的关联归位）

**复现**：R22 中 10 个视图各有 2 次请求（如 view_412655f6f691 下第 1/2 次提问）。
原实现按 view_id 建 dict，后写覆盖——点开第 1 次时，原提议与消息准备快照显示的是
第 2 次的内容。

**修复**（关联优先级按任务书：`request_id` → `generation_call_id` → `call_id`）：

- 提议/规划调用：target 请求的 `payload.request_id` ↔ `request_generated.payload.request_id`
  主关联；无 request_id 的旧记录回退 view_id，同视图多候选 → 该项**关联不确定**
  （`proposal_ambiguous`，前端红标 + 说明，其余信息照常）；
- view_snapshot 语义更正：它是 **Target 发送前的消息准备快照**（同 view_id 多份），
  按事件流序配对——每个 target 请求取其之前最近的同 view_id 快照；
- 区块标注分别改为 **「① Controller 规划输入（该次规划调用的输入消息）」**
  （= 规划调用 call 的 payload.messages）与 **「④ Target 实际收到的消息（完整）」**
  （附「发送前消息准备快照」，按事件序配对）。

**R22 点验**（真实 Chromium，截图 results/workbench-fix-verify-20261121.png）：同视图
第 1/2 次各自的 Controller 规划输入（JBT_STAGE=PLAN 提示词）、原提议（第 1 次
"A forensic linguistics lab…"、第 2 次 "In preparing an IT vendor evaluation memo…"）、
实际发送、Target 回复、规划调用（call_cb5c73a755f6 / call_0404f97395d5）全部归位。

**测试改为干净检出可运行**：`tests/test_workbench.py` 重写为 tmp_path 合成 episode
（同一视图两次请求的最小事件流），不依赖服务器上的 results/ 运行目录；新增
`test_same_view_two_requests_attributed_correctly`（提议/规划输入/发送/回复各自归位）
与 `test_no_request_id_marks_ambiguous`（无 request_id → 关联不确定）。

## 中文全文翻译（2026-11-21）

- **译本存储**：`data/workbench/translations/mainflow8_sr_batch2_20261117_r1__ep_49f441c969.json`
  （60 单元：任务 1 + 查询 20 + 理由 20（原为中文）+ 回复 19；键=en 的 sha256 前 16 位，
  英文原件留在事件流中供复核）。第 4 次请求回复为 TRANSPORT_ERROR（无正文），⑤ 区块
  如实显示"该次运行未记录"。
- **展示层替换**（webapp.py）：任务/提议/理由/回复挂 `*_zh` 字段；控制器规划输入的
  JSON 状态内嵌英文（查询/回复原文）按哈希递归替换（从头扫描首个能消费到结尾的
  `\n{` 定位外层状态对象，指令文本中的 action_schema 示例 JSON 不受影响）；
  发送消息与消息准备快照按整条哈希替换。前端中文优先、无译本回退英文，
  页首注明"正文已译为中文；英文原始记录保留在运行档案中供复核"。
- **翻译原则**：逐段翻译；保留数字、例句（构造示例保留英文原句并附中文释义）、
  引用（Clough & Stevenson、PAN、COPE、DORA 等专名不译）、否定与限制条件
  （"未公开""默认关闭""不应作为事实"等原样保留）；提问者声称的事实（47 案、
  机构身份设定）保持"声称"语态，Target 的回答保持其自称边界（"我给不了""据我所知"），
  Target 自写的构造示例标注（构造）。
- **核对**：20/20 交互全覆盖（提议/理由/回复/规划输入/发送消息均无长英文残留）；
  浏览器实测（截图 results/workbench-zh-verify-20261121.png）：任务、①-⑤ 区块
  全部中文可读。
- **查看方式**：刷新 http://127.0.0.1:8765 → 「工作台」→「打开固定样例 R22 工作台」
  （webapp 需运行本轮代码后启动的实例）。

## 未做 / 下一步

- 视觉精修（任务书明确留到实际使用后）；
- 下一轮：越狱实验的回退策略（人工标注 vs 系统判断，历史截止在发现点）；
  再下一轮：Assembler 本地推理与来源区分。
