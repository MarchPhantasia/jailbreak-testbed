/* 越狱研究工作台 —— 前端逻辑（vanilla，无构建） */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

/* ---------- 视图切换 ---------- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  ["runs", "compare", "interactive", "workbench"].forEach(v => $(`#view-${v}`).hidden = v !== b.dataset.view);
  if (b.dataset.view === "runs") loadRuns();
  if (b.dataset.view === "compare") loadComparePicker();
  if (b.dataset.view === "workbench") wbInit();
});

/* ---------- 回放 ---------- */
let RUNS = [];
async function loadRuns() {
  RUNS = await api("/api/runs");
  const box = $("#run-list .body");
  box.innerHTML = RUNS.map((r, i) =>
    `<div class="run-item" data-i="${i}"><b>${esc(r.label)}</b><br>
     <span class="hint">${esc(r.experiment_id || r.root)} · ${r.episodes.length} 任务</span></div>`).join("");
  box.querySelectorAll(".run-item").forEach(el => el.onclick = () => {
    box.querySelectorAll(".run-item").forEach(x => x.classList.remove("sel"));
    el.classList.add("sel");
    showEpisodes(RUNS[el.dataset.i]);
  });
}
function showEpisodes(run) {
  const box = $("#episode-list .body");
  box.innerHTML = run.episodes.map((e, i) =>
    `<div class="ep-item" data-i="${i}"><b>${esc(e.task_id)}</b> ${e.has_final ? "" : '<span class="no-final">事故 U</span>'}<br>
     <span class="hint">${esc(e.prompt)}</span></div>`).join("");
  box.querySelectorAll(".ep-item").forEach(el => el.onclick = () => {
    box.querySelectorAll(".ep-item").forEach(x => x.classList.remove("sel"));
    el.classList.add("sel");
    showTrace(run, run.episodes[el.dataset.i]);
  });
}
async function showTrace(run, ep) {
  const t = await api(`/api/episode/${encodeURIComponent(run.root)}/${encodeURIComponent(ep.episode_id)}`);
  const box = $("#trace .body");
  if (t.error) { box.innerHTML = `<p class="hint">${esc(t.error)}</p>`; return; }
  let html = `<p><b>任务</b> ${esc(t.task.task_id)}<br>${esc(t.task.prompt || "")}</p>`;
  for (const s of t.steps) html += stepHtml(s);
  html += `<h3>判定</h3>` + verdictHtml(t.verdicts, t.final_candidate);
  if (t.final_candidate) html += `<p><button onclick="showCand('${esc(run.root)}','${esc(ep.episode_id)}','${esc(t.final_candidate)}')">查看最终候选（主张→证据）</button></p>`;
  html += `<p><button onclick="wbOpenFromReplay('${esc(run.root)}','${esc(ep.episode_id)}')">打开工作台（人工回退记录）</button></p>`;
  box.innerHTML = html;
}
function stepHtml(s) {
  const m = (x) => `<div class="meta">${x}</div>`;
  switch (s.kind) {
    case "start": return `<div class="step k-start">${m("episode 开始 · " + esc(s.task_id))}<div class="content">${esc(s.prompt)}</div></div>`;
    case "propose": return `<div class="step k-propose">${m("控制器提议 · " + esc(s.type) + (s.node ? " · " + esc(s.node) : ""))}<div class="content">${esc(s.query || s.rationale)}</div></div>`;
    case "paused": return `<div class="step k-paused">${m("挂起等待人工（node " + esc(s.node) + "）")}<div class="content">${esc(s.query)}</div></div>`;
    case "edited": return `<div class="step k-edited">${m("人工编辑（node " + esc(s.node) + "）· 理由：" + esc(s.reason))}<div class="content">原：${esc(s.original)}<br>改：${esc(s.edited)}</div></div>`;
    case "branch": return `<div class="step k-branch">${m("人工开分支 · 从 " + esc(s.from_node))}<div class="content">${esc(s.note)}</div></div>`;
    case "view": {
      const msgs = (s.messages || []).map(x => `<div class="ev-text"><b>${esc(x.role)}</b>：${esc(x.content)}</div>`).join("");
      return `<details class="step k-view"><summary>实际发送消息（${s.messages.length} 条 · ${esc(s.session_mode)}）</summary>${msgs}</details>`;
    }
    case "request": {
      const view = s.view ? `<details class="step k-view"><summary>消息准备快照（${s.view.messages.length} 条 · ${esc(s.view.session_mode)}）</summary>${s.view.messages.map(x => `<div class="ev-text"><b>${esc(x.role)}</b>：${esc(x.content)}</div>`).join("")}</details>` : "";
      return view + `<div class="step k-request">${m(`${esc(s.role || "target")} 请求 · call ${esc(s.call_id)} · ${s.chars} 字符（真实派发）`)}${s.messages ? `<details><summary>实际发送消息</summary>${s.messages.map(x => `<div class="ev-text"><b>${esc(x.role)}</b>：${esc(x.content)}</div>`).join("")}</details>` : ""}</div>`;
    }
    case "response": return `<div class="step k-response">${m(`${esc(s.role || "target")} 回复 · call ${esc(s.call_id)} · ${esc(s.status)}`)}<div class="content">${esc(s.excerpt)}…</div></div>`;
    case "cache_hit": return `<div class="step k-paused">${m("缓存命中 · " + esc(s.role) + " · " + esc(s.key))}</div>`;
    case "refusal": return `<div class="step k-refusal">${m("拒答")}<div class="content">${esc(s.label)}</div></div>`;
    case "pack": return `<div class="step k-pack">${m("冻结包 " + esc(s.pack_id) + " · " + s.materials + " 材料 · hash " + esc(s.input_hash) + "…")}</div>`;
    case "assembled": return `<div class="step k-assembled">${m("装配候选 " + esc(s.candidate_id))}<div class="content">${s.answer_chars} 字符</div></div>`;
    case "selected": return `<div class="step k-selected">${m("选中最终候选 " + esc(s.candidate_id))}<div class="content">${esc(s.basis)}</div></div>`;
    case "terminate": return `<div class="step k-terminate">${m("终止")}<div class="content">${esc(s.reason)}</div></div>`;
    default: return "";
  }
}
function verdictHtml(v, finalCand) {
  if (!v) return "";
  let h = "";
  if (v.human) {
    const h2 = v.human;
    h += `<div class="verdict">人工（已锁定）：${h2["tf/src/ev"]} · ` +
      (h2.full_success ? "<b>完整成功</b>" : (h2.partial_success_rule_v2 ? "<b>仅部分成功</b>" : "其余")) +
      ` <span class="hint">${esc(h2.reason || "")}</span>`;
    if (h2.sub_results?.length) h += `<br><span class="hint">子结果：${h2.sub_results.map(x => esc(x.name + "（" + x.support + "）")).join("；")}</span>`;
    h += `</div>`;
  }
  if (v.historical?.length) h += `<div class="verdict">历史原判：` + v.historical.map(x =>
    `<span class="cls-${esc(x.cls)}">${esc(x.cls)}</span> <span class="hint">${esc(x.eval_run_id)} ${esc(x.basis)}</span>`).join("<br>") + `</div>`;
  if (v.model_v4?.length) h += `<div class="verdict">独立 Judge 评价：` + v.model_v4.map(x =>
    `<span class="cls-${esc(x.cls)}">${esc(x.cls)}</span> sub=${esc(x.sub_result)} cit=${esc(x.citation_status)} partial=${esc(x.partial_success)} <span class="hint">${esc(x.eval_run_id || x.run)} ${esc(x.judge_ref || "")} ${esc(x.sub_result_statement)}</span>`).join("<br>") + `</div>`;
  return h || `<p class="hint">无评价记录${finalCand ? "" : "（事故位置无候选）"}</p>`;
}
window.showCand = async (root, ep, cand) => {
  const c = await api(`/api/candidate/${encodeURIComponent(root)}/${encodeURIComponent(ep)}/${encodeURIComponent(cand)}`);
  const box = $("#trace .body");
  let h = `<p><b>候选</b> ${esc(c.candidate_id)} · 包 ${esc((c.packs[0] || {}).pack_id || "?")}（hash ${esc((c.packs[0] || {}).input_hash || "?")}…）</p>
    <div class="content" style="white-space:pre-wrap">${esc(c.answer)}</div><h3>主张 → 证据（episode 有 / 包允许 / Judge 看到）</h3>`;
  for (const cl of c.claims) {
    const evs = cl.evidence.map(e => `<details><summary>${esc(e.evidence_id)} ${evTags(e)}</summary><div class="ev-text">${esc(e.text ?? "（episode 无此条原文）")}</div></details>`).join("");
    h += `<div class="claim"><div class="claim-text">${esc(cl.text)}</div>${evs}</div>`;
  }
  h += verdictHtml(c.verdicts, c.candidate_id);
  box.innerHTML = h;
};

/* ---------- 对照 ---------- */
async function loadComparePicker() {
  if (!RUNS.length) RUNS = await api("/api/runs");
  const opts = [];
  for (const r of RUNS) for (const e of r.episodes) {
    // R4：按 candidates.jsonl 真实枚举（去重 + 最终标记），不再依赖事件步推断
    const cands = await api(`/api/candidates/${encodeURIComponent(r.root)}/${encodeURIComponent(e.episode_id)}`);
    for (const c of (cands || [])) {
      opts.push({root: r.root, ep: e.episode_id, task: e.task_id, cand: c.candidate_id, final: c.is_final});
    }
  }
  const fill = (sel) => { sel.innerHTML = opts.map((o, i) =>
    `<option value="${i}">${esc(o.task)} · ${esc(o.cand.slice(0, 14))}… ${o.final ? "（最终）" : ""} · ${esc(o.root.split("/").pop())}</option>`).join(""); };
  fill($("#cmp-side-a")); fill($("#cmp-side-b"));
  $("#cmp-go").onclick = async () => {
    const a = opts[$("#cmp-side-a").value], b = opts[$("#cmp-side-b").value];
    const [ca, cb] = await Promise.all([
      api(`/api/candidate/${encodeURIComponent(a.root)}/${encodeURIComponent(a.ep)}/${encodeURIComponent(a.cand)}`),
      api(`/api/candidate/${encodeURIComponent(b.root)}/${encodeURIComponent(b.ep)}/${encodeURIComponent(b.cand)}`)]);
    $("#compare-result").innerHTML = candPanel(ca, "A") + candPanel(cb, "B");
  };
}
function evTags(e) {
  const t = [];
  t.push(e.in_episode ? `<span class="tag ok">episode 有</span>` : `<span class="tag bad">episode 无</span>`);
  t.push(e.in_pack ? `<span class="tag ok">包允许</span>` : `<span class="tag bad">包外</span>`);
  if (!e.judge_inputs?.length) t.push(`<span class="tag">无 Judge 评价记录</span>`);
  for (const view of e.judge_inputs || []) {
    const label = view.included === null ? "送评范围未知" : (view.included ? "送评含此条" : "送评未含此条");
    t.push(`<span class="tag">${esc(view.evaluation)} · ${label}</span>`);
  }
  return t.join("");
}
function fallbackBadge(ac) {
  if (!ac?.fallback_render) return "";
  const reason = ac.reason || ac.assembly_config?.reason || "";
  const why = reason || ac.relocated || "";
  return `<span class="tag bad" title="原文回退：不调用模型的材料模板渲染">原文回退</span>` +
    (reason ? ` <span class="tag">${esc(reason)}</span>` : "");
}
function citeFixBadge(ac) {
  const r = ac?.citation_relocation;
  if (!r || r.version === "off") return "";
  const n = (r.relocated || []).length;
  return n ? `<span class="tag ok" title="${esc(r.version)}">引用定位修复 ${n} 处</span>` : "";
}
function candPanel(c, tag) {
  const ac = c.assembly_config || {};
  let h = `<div class="panel wide"><h2>候选 ${tag}：${esc(c.candidate_id)}</h2><div class="body">
    <p class="hint">任务 ${esc(c.task_id)} · 包 ${esc((c.packs[0] || {}).pack_id || "?")}（${esc((c.packs[0] || {}).input_hash || "?")}…）· 采样 ${esc(ac.sample_index ?? "-")} ${fallbackBadge(ac)} ${citeFixBadge(ac)}</p>
    <div class="content" style="white-space:pre-wrap">${esc(c.answer)}</div>
    <h3>主张 → 证据（episode 有 / 包允许 / Judge 看到 三层）</h3>`;
  for (const cl of c.claims) {
    const evs = cl.evidence.map(e => `<details><summary>${esc(e.evidence_id)} ${evTags(e)}</summary><div class="ev-text">${esc(e.text ?? "（episode 无此条原文）")}</div></details>`).join("");
    h += `<div class="claim"><div class="claim-text">${esc(cl.text)}</div>${evs}</div>`;
  }
  h += verdictHtml(c.verdicts, c.candidate_id) + `</div></div>`;
  return h;
}

/* ---------- 交互 ---------- */
let SID = null;
$("#int-start").onclick = async () => {
  $("#int-status").textContent = "启动会话（真实本地服务）…";
  const out = await api("/api/interactive/start", {});
  if (out.error) { $("#int-status").textContent = "失败：" + out.error; return; }
  SID = out.session_id;
  $("#int-controls").hidden = false;
  renderInt(out.state);
};
$("#int-step").onclick = async () => { await api(`/api/interactive/${SID}/step`, {}); renderInt(await intState()); };
$("#int-approve").onclick = async () => {
  const out = await api(`/api/interactive/${SID}/approve`, {edit: $("#int-edit").value || null, reason: $("#int-reason").value});
  if (out.error) return alert(out.error);
  $("#int-edit").value = ""; $("#int-reason").value = "";
  renderInt(await intState());
};
$("#int-skip").onclick = async () => { await api(`/api/interactive/${SID}/skip`, {}); renderInt(await intState()); };
$("#int-branch").onclick = async () => {
  const out = await api(`/api/interactive/${SID}/branch`, {node: $("#int-branch-node").value, note: $("#int-branch-note").value});
  if (out.error) return alert(out.error);
  renderInt(await intState());
};
$("#int-assemble").onclick = async () => { await api(`/api/interactive/${SID}/assemble`, {}); renderInt(await intState()); };
$("#int-finalize").onclick = async () => { await api(`/api/interactive/${SID}/finalize`, {}); renderInt(await intState()); };
async function intState() { return api(`/api/interactive/${SID}`); }
function renderInt(s) {
  if (!s || s.error) { $("#int-status").textContent = s?.error || "会话结束"; return; }
  const b = s.budget || {};
  $("#int-status").innerHTML = `任务 ${esc(s.task_id)} · 步 ${s.steps} · Target ${b.target_calls_used}/${b.target_calls_used + b.target_calls_left} · 本地 ${b.non_target_calls_used} · ${s.terminated ? "已终止（" + esc(s.termination_reason) + "）" : "进行中"}`;
  const p = s.pending;
  $("#int-pending").innerHTML = p
    ? `<div class="step k-paused"><div class="meta">待批准查询（node ${esc(p.node_id)}）</div><div class="content">${esc(p.query)}</div>
       ${p.components?.length ? `<div class="hint">组件：${p.components.map(c => esc(c.id + ":" + c.status)).join(" · ")}</div>` : ""}</div>`
    : `<p class="hint">无挂起动作。点「下一步」让控制器提议。</p>`;
  let tree = (s.nodes || []).map(n =>
    `<span class="node ${n.node_id === s.current_node ? "cur" : ""}">${esc(n.node_id)}<br><span class="hint">${n.materials}材料</span><br><button onclick="document.querySelector('#int-branch-node').value='${esc(n.node_id)}'">选为分支点</button></span>`).join("");
  tree += `<h3>包与候选</h3>` + (s.packs || []).map(p2 =>
    `<div class="tag">${esc(p2.pack_id)} · ${p2.materials}材料 · ${esc((p2.input_hash || "").slice(0, 10))}…</div>`).join("")
    + (s.candidates || []).map(c => `<div class="tag ok">${esc(c.candidate_id)} · ${c.claims}主张 · 采样${esc(c.sample_index)}</div>`).join("");
  $("#int-tree .body").innerHTML = tree || '<p class="hint">暂无节点</p>';
}

loadRuns();

/* ---------- Controller 工作台（人工回退记录，Issue #1） ---------- */
let WB = null;            // {root, episode_id, task, interactions}
let WB_SEL = { discovery: null, fallback: null };

function wbInit() {
  const btn = $("#wb-open-r22");
  if (!btn.onclick) btn.onclick = async () => {
    const s = await api("/api/workbench/sample");
    if (s.error) { $("#wb-anno-count").textContent = "失败：" + s.error; return; }
    wbOpen(s.run_root, s.episode_id);
  };
  wbRefreshCount();
}
async function wbRefreshCount() {
  const rows = await api("/api/workbench/annotations");
  $("#wb-anno-count").innerHTML = rows.length
    ? `<span class="hint">工作台共 ${rows.length} 份草稿（data/workbench/annotations.jsonl）</span>` : "";
}
async function wbOpen(root, ep) {
  WB = await api(`/api/workbench/${encodeURIComponent(root)}/${encodeURIComponent(ep)}`);
  if (WB.error) { $("#wb-body").innerHTML = `<p class="hint">${esc(WB.error)}</p>`; return; }
  $("#wb-main").hidden = false;
  $("#wb-title").textContent = `交互轨迹 · ${root.split("/").pop()} · ${ep}`;
  // 默认选第一条有实际请求和回复的交互
  const first = WB.interactions.find(x => x.call_id && x.reply_text != null) || WB.interactions[0];
  WB_SEL = { discovery: first, fallback: first };
  wbRenderList();
  wbRenderDraft();
  wbRenderSaved();
}
function wbCollapsible(title, content, open) {
  return `<details ${open ? "open" : ""}><summary>${title}</summary><div class="content" style="white-space:pre-wrap">${content}</div></details>`;
}
function wbMsgs(msgs) {
  if (!Array.isArray(msgs) || !msgs.length) return `<span class="hint">该次运行未记录</span>`;
  return msgs.map(m => `<div class="meta">${esc(m.role)}</div><div class="content" style="white-space:pre-wrap">${esc(m.content)}</div>`).join("<hr>");
}
function wbRenderList() {
  const t = WB.task || {};
  let h = wbCollapsible(`<b>原始任务</b> ${esc(t.task_id || "")}`, esc(t.prompt || ""), false) + "<hr>";
  WB.interactions.forEach(it => {
    const retry = it.node_total > 1 ? ` · 同节点第 ${it.node_nth}/${it.node_total} 次` : "";
    const isD = WB_SEL.discovery && WB_SEL.discovery.call_id === it.call_id;
    const isF = WB_SEL.fallback && WB_SEL.fallback.call_id === it.call_id;
    h += `<div class="step k-propose" id="wb-it-${esc(it.call_id)}">
      <div class="meta">第 ${it.seq} 次提问 · ${esc(it.node_id)}${retry} · ${esc(it.call_id)}
        ${isD ? '<span class="tag ok">发现点</span>' : ""}${isF ? '<span class="tag">回退点</span>' : ""}
        ${it.reply_status ? `<span class="hint">回复 ${esc(it.reply_status)}</span>` : '<span class="tag bad">无回复记录</span>'}
      </div>
      ${wbCollapsible("① Controller 规划输入（该次规划调用的输入消息，不含后来材料）",
        it.controller_input_messages
          ? wbMsgs(it.controller_input_messages)
          : '<span class="tag bad">关联不确定</span> <span class="hint">规划调用无法唯一对应（无 request_id 且同视图多次请求）</span>')}
      ${wbCollapsible(`② 原提议${it.generation_call_id ? "（规划调用 " + esc(it.generation_call_id) + "）" : ""}`,
        it.proposal_ambiguous
          ? '<span class="tag bad">关联不确定</span> <span class="hint">同一视图下有多次请求，且该次运行未记录 request_id，无法唯一对应提议</span>'
          : `<div class="content">${esc(it.proposal ?? "该次运行未记录")}</div>
         ${it.rationale ? `<div class="meta">理由</div><div class="content">${esc(it.rationale)}</div>` : ""}
         ${it.strategy ? `<div class="hint">策略 ${esc(it.strategy)}</div>` : ""}
         ${it.planning_call ? wbCollapsible("规划调用响应原文",
            `<div class="content">${esc(it.planning_call.response_text ?? "未记录")}</div>`) : '<div class="hint">规划调用关联不确定</div>'}`)}
      ${wbCollapsible("③ 人工修改记录", it.human_edit
        ? `<div class="content">${esc(JSON.stringify(it.human_edit))}</div>`
        : '<span class="hint">该次运行未记录（批次运行无人工编辑）</span>')}
      ${wbCollapsible("④ Target 实际收到的消息（完整）", wbMsgs(it.sent_messages) +
        (it.target_view_snapshot ? wbCollapsible("发送前消息准备快照（该次 view_snapshot，按事件序配对）", wbMsgs(it.target_view_snapshot)) : ""))}
      ${wbCollapsible("⑤ Target 回复", it.reply_text != null
        ? `<div class="content">${esc(it.reply_text)}</div>`
        : '<span class="hint">该次运行未记录</span>')}
      <p><button onclick="wbSetDiscovery('${esc(it.call_id)}')">设为发现点</button>
         <button onclick="wbSetFallback('${esc(it.call_id)}')">设为回退点</button></p>
    </div>`;
  });
  $("#wb-body").innerHTML = h;
}
window.wbSetDiscovery = (callId) => {
  WB_SEL.discovery = WB.interactions.find(x => x.call_id === callId);
  if (WB_SEL.fallback && WB_SEL.fallback.seq > WB_SEL.discovery.seq) WB_SEL.fallback = WB_SEL.discovery;
  wbRenderList(); wbRenderDraft();
};
window.wbSetFallback = (callId) => {
  WB_SEL.fallback = WB.interactions.find(x => x.call_id === callId);
  wbRenderList(); wbRenderDraft();
};
function wbRenderDraft() {
  const d = WB_SEL.discovery, f = WB_SEL.fallback;
  if (!d || !f) { $("#wb-draft").innerHTML = '<p class="hint">先从上方选择一次交互。</p>'; return; }
  const opts = WB.interactions.filter(x => x.seq <= d.seq).map(x =>
    `<option value="${esc(x.call_id)}" ${x.call_id === f.call_id ? "selected" : ""}>第 ${x.seq} 次（${esc((x.sent_text || "").slice(0, 40))}…）</option>`).join("");
  $("#wb-draft").innerHTML = `
    <p><b>发现问题的位置：</b>第 ${d.seq} 次回复 · ${esc(d.call_id)}
       <button onclick="document.getElementById('wb-it-${esc(d.call_id)}').scrollIntoView()">回到该步</button></p>
    <p><b>希望退回的位置：</b><select id="wb-fb-sel" onchange="wbFallbackChanged(this.value)">${opts}</select>
       <span class="hint">＝从第 ${f.seq} 次发送前开始改写</span></p>
    <div id="wb-fb-context"></div>
    <p><b>我的批注（为什么第 ${d.seq} 次回复改变了判断）：</b><br>
       <textarea id="wb-reason" style="width:100%;height:56px"></textarea></p>
    <p><b>替代问题（草稿 · 未发送，预填第 ${f.seq} 次实际发送）：</b><br>
       <textarea id="wb-draft-text" style="width:100%;height:90px">${esc(f.sent_text || "")}</textarea></p>
    <p><button onclick="wbSaveDraft()">保存草稿（只写工作台数据，模型调用 0）</button>
       <span class="hint">可在同一位置保存多份草稿，刷新后均可找回</span></p>`;
  wbLoadContext(f.call_id);
}
window.wbFallbackChanged = (callId) => {
  WB_SEL.fallback = WB.interactions.find(x => x.call_id === callId);
  $("#wb-draft-text").value = WB_SEL.fallback.sent_text || "";
  wbLoadContext(callId);
};
async function wbLoadContext(callId) {
  const c = await api(`/api/workbench/context/${encodeURIComponent(WB.root)}/${encodeURIComponent(WB.episode_id)}/${encodeURIComponent(callId)}`);
  const el = $("#wb-fb-context");
  if (c.error) { el.innerHTML = `<p class="hint">${esc(c.error)}</p>`; return; }
  el.innerHTML = wbCollapsible(`回退点发送前可见的上下文（Controller 当时已知；第 ${c.seq} 次之后的回复不在此列）`,
    wbMsgs(c.view_messages) + (c.prior_turns.length
      ? `<div class="meta">此前各轮</div>` + c.prior_turns.map(p =>
        `<div class="hint">第 ${p.seq} 次问：${esc((p.question || "").slice(0, 60))}… → 回复 ${esc(p.reply_status ?? "?")}</div>`).join("")
      : '<div class="hint">（这是第一次提问，无此前轮次）</div>'), false);
}
async function wbSaveDraft() {
  const d = WB_SEL.discovery, f = WB_SEL.fallback;
  const body = {
    root: WB.root, episode_id: WB.episode_id,
    discovery: { seq: d.seq, call_id: d.call_id, text_sha: d.text_sha },
    fallback: { seq: f.seq, call_id: f.call_id, text_sha: f.text_sha },
    reason: $("#wb-reason").value, draft_text: $("#wb-draft-text").value,
  };
  const out = await api("/api/workbench/annotations", body);
  if (out.error) { alert("保存失败：" + out.error); return; }
  alert(`已保存：${out.saved.draft_id}`);
  wbRenderSaved(); wbRefreshCount();
}
async function wbRenderSaved() {
  const rows = await api(`/api/workbench/annotations?root=${encodeURIComponent(WB.root)}&ep=${encodeURIComponent(WB.episode_id)}`);
  if (!rows.length) { $("#wb-saved").innerHTML = '<p class="hint">尚无草稿。</p>'; return; }
  $("#wb-saved").innerHTML = rows.map(r => {
    const f = WB.interactions.find(x => x.call_id === r.fallback.call_id) || {};
    const d = WB.interactions.find(x => x.call_id === r.discovery.call_id) || {};
    return `<div class="cols">
      <div class="panel"><h3>原（第 ${r.fallback.seq} 次实际发送 · ${esc(r.fallback.call_id)}）</h3><div class="body">
        <div class="content" style="white-space:pre-wrap">${esc(f.sent_text ?? r.fallback.text_head ?? "")}</div>
        <div class="meta">Target 回复（归属原调用）</div>
        <div class="content" style="white-space:pre-wrap">${esc((f.reply_text ?? "").slice(0, 600))}</div>
      </div></div>
      <div class="panel"><h3>草稿 ${esc(r.draft_id)}（发现点：第 ${r.discovery.seq} 次回复）</h3><div class="body">
        <span class="tag bad">草稿 · 未发送</span>
        <div class="content" style="white-space:pre-wrap">${esc(r.draft_text)}</div>
        <div class="meta">修改理由</div>
        <div class="content">${esc(r.reason)}</div>
        <div class="hint">${esc(r.ts)} · 展示序号 #${r.display_index}</div>
        <p><button onclick="wbSetDiscovery('${esc(r.discovery.call_id)}');wbSetFallback('${esc(r.fallback.call_id)}');document.getElementById('wb-it-${esc(r.discovery.call_id)}').scrollIntoView()">回到发现点那一轮</button></p>
      </div></div>
    </div>`;
  }).join("<hr>");
}

window.wbOpenFromReplay = (root, ep) => {
  document.querySelector('nav button[data-view="workbench"]').click();
  wbOpen(root, ep);
};
