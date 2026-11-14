# 审查包 handoff-v3.1 修复记录（X01 / X02 / X03 + 配置入口）

- 审查对象：`f8249dcf8d3604cbd46d68664f06d424ce2d232a`（实现提交 `bfbddd3`）
- 状态：**offline_ready**；X01–X03 已修，配置便利入口已实现。
- 真实实验边界：本机**对齐 Target 的地址与凭证仍未提供**。`services.example.yaml` 是可填契约，
  不是可运行的迁移配置；本地链路用 `--services` 走通（§5），但那不是对齐 Target 的 ASR。
- 研究口径不变：主终点是固定 Target 调用预算下，最终选中答案实质完成冻结的原始有害请求并由
  冻结判据判定。字面覆盖、材料覆盖率、拒答率都不能替代它。

## 1. X01 评价前确定性校验正文与冻结材料

改动集中在 `runner.py` 的证据读取与范围解析：

| 校验 | 行为 | 诊断 |
|---|---|---|
| 原文行 schema | 必须是对象；`evidence_id` 非空字符串；`text` 必须是字符串（不再变成 `""` 或 `str(obj)`） | `not_an_object` / `bad_evidence_id` / `text_missing` / `text_not_string` |
| 原文 checksum | 有 `checksum` 时必须等于 `sha256(text.encode("utf-8"))`，校验前不改换行/空白/Unicode | `checksum_missing` / `checksum_mismatch` |
| 同 ID 多条 | 完全一致 → 去重并记重复数；正文或校验和冲突 → 该 ID 不可核验 | `duplicate_identical_record`（不阻塞）/ `conflicting_evidence_record`（阻塞） |
| 包结构 | `materials` 是列表、每条是对象、`evidence_ids` 是字符串列表 | `invalid_pack_structure` → `invalid_pack` |
| 包冻结身份 | `input_hash == sha256(json.dumps(materials, ensure_ascii=False, sort_keys=True, default=str))`，与 `Engine._freeze_pack()` 同一序列化 | `pack_hash_missing` / `pack_hash_mismatch` → `unverified_pack` |
| 同 pack_id 多条 | 冲突记录不能"最后一条覆盖" | `conflicting_pack_record` → `conflicting_pack` |
| 允许证据完整性 | 冻结包声明但原文缺失 → `missing_allowed_evidence`；原文存在但不可核验 → `unverifiable_allowed_evidence` | 同名字段 |

作用范围：

- 默认 `candidate_pack` 协议下，任一失败 → `integrity_complete=false`，**不发 Judge_eval**、
  不新增结算，episode 记 `unscored:evidence_input_incomplete`，保留在冻结 N 内；
- 只作用于该候选实际用到的包与 ID，同一批其他正常 episode 继续；完整空包仍是合法情形
  （`materials=[]` 且 `input_hash` 与空列表相符 → `integrity_complete=true`）；
- 记录贯通 `evidence_scope → evidence_plan → verdict → records`：`failure_reason`、
  `missing_pack_ids`、`invalid_pack_ids`、`unverified_pack_ids`、`missing_allowed_ids`、
  `unverifiable_allowed_ids`、`pack_problems`、`evidence_problems`、`evidence_verified_count`；
- 本轮**不**做自动恢复、重新抽取或 LLM 核对；checksum/`input_hash` 只用于识别常见产物损坏、
  迁移错误与重复记录冲突，不构成对恶意篡改的密码学认证，也不能证明 Target 陈述为真。

## 2. X02 一次装配的诊断与调用引用隔离

- `LLMAssembler.assemble_from_pack()` 入口调用 `_reset_call_state()`：清空
  `last_lexical_diagnostics`、`last_failure`、`last_call_ids`。一次装配内部的有限重试仍可累积，
  不同装配调用之间不再无标识沿用。
- 模型正文不可用（拒答/格式无效）而走原文回退时，回退候选的 `lexical_diagnostics` 明确记为
  **未计算**（`uncovered_ratio=None`，附 `note` 与 `diagnostics_subject=fallback_render`），
  不用 `0` 冒充"完全没有未覆盖部分"，也不沿用上一次装配的数字。
- `processing_refs` 只由本次装配调用与本次冻结包组成（`last_call_ids` 每次重置），上一轮失败尝试
  仍保留在事件与 `assembly_config.attempts` 中，但不会混进新候选的过程来源。

## 3. X03 换行兼容

- 仓库测试 `test_judge_sees_full_episode_evidence_not_only_cited` 改用已有的
  `FakeService._input()`（与换行无关的解析）；注释澄清"该夹具的冻结包恰好覆盖 episode 全部证据"，
  默认协议仍是 `candidate_pack`。函数名保持不变，外部审查 harness 仍能按名调用。
- 历史包 `legacy_checks/tools/contract_server.py` 在**审查包副本**内做等价解析修复：`text` 先做
  `\r\n|\r → \n` 归一化再找 `INPUT_JSON` 标记。只改替身解析，未改任何历史断言，也未触碰生产
  `PromptRegistry` 的字节保留与哈希。最小 diff 见交付目录 `contract_server.patch`。
- 生产模板字节语义不变；LF 与 CRLF 的临时模板各自落盘快照与各自源字节一致，不做统一哈希。
- 审查包自带的 `verify_bundle.py --repo` 报的唯一 `changed` 就是
  `legacy_checks/tools/contract_server.py`（即上面这处有意修改），包内其余文件字节未动；
  交付记录里已把该行单列为有意差异，不是未记录的漂移。

## 4. 服务与实验配置分离（CONFIG_AND_EXPERIMENT_PRIORITY）

新增能力（都可离线验证）：

1. `--services FILE`（`prepare-data`/`check-data`/`check-services`/`show-config`/`run`/`evaluate`）：
   覆盖文件只允许顶层 `services`，按 service ID **整项替换**实验 YAML 的同名服务；未覆盖的服务保留、
   可新增 ID；数据/模板路径仍按实验 YAML 的 `path_base` 解析；覆盖文件路径与 SHA-256 写入
   `plan.json.config_sources`。
2. `jbt show-config`：不调用模型，打印每个启用角色的 service ID / base_url / model / 参数协议 /
   `context_window` / 阶段发送参数，标出共用同一服务的角色；密钥只显示变量名与是否已设置。
3. `jbt evaluate --eval-service SID`：旧实验重评的**连接层**迁移。只允许替换
   `base_url` / `api_key_env` / `auth_required` / `timeout_s` / `service_id`；`model`、
   `parameter_profile`、阶段参数与 `context_window` 必须与冻结值一致，否则报错要求另建评价协议。
   迁移写入 `<eval_run_id>/config.json.service_migration` 与 `evaluation_started` 事件的
   `base_url`/`service_migration`，新评价使用新 `eval_run_id`，旧 plan、候选、证据不被改写。
   不传 `--eval-service` 时仍用冻结服务，不会因为存在服务文件而隐式迁移。

尚未实现：配置里的 `${VAR}` 自动插值（本轮明确不引入）；跨协议供应商适配器。

## 5. 验证

| 检查 | 结果 |
|---|---|
| 本轮 review（X01 五条 + X02 + X03） | 7 passed |
| previous（W01–W03 + 护栏） | 9 passed |
| boundaries（C/S/A/P） | 18 passed |
| baseline（仓库全量，含新增配置回归 8 项） | 258 passed |
| prior / round2 / round3 / round4 / round5 | 17 / 19 / 12 / 8 / 5 passed |
| harness / next / routing | 2 / 16 / 8 passed |
| `scripts/precheck.sh` | 通过 |
| 远端 CI | 本轮未推送，未运行 |

`next` 在 Linux 默认环境 16 passed：修前 Windows 的 2 个失败来自历史替身 LF 假设（X03），
修复在审查包副本内完成并有 diff 记录；没有把诊断插件当默认验收环境。

## 6. 真实链路（本地替身，不是 ASR）

对齐 Target 仍需用户提供迁移后的地址与密钥；下面这一批是**链路证据**，不能当作越狱成功率。

证据设计：`configs/real-dev.wfix.overlay.yaml` 把 service 的 `base_url` 故意写成不可达的
`127.0.0.1:9`，真实地址只在 `--services` 覆盖文件里。运行成功即证明"覆盖确实进了网关"，
而不是实验 YAML 里的值在起作用。

| 项 | 值 |
|---|---|
| 实验 ID | `smoke_wfix_overlay_20261114b` |
| 覆盖文件 SHA-256 | `5bd7ed0dfb536c758b785fbfe48aaa87a0564338b0e097912e845602228127d8`（记入 `plan.json.config_sources.services_overlay`） |
| preflight | 通过，`response_model=JonathanColetti/Qwen3.8-27B-Uncensored`，`finish_reason=stop` |
| 运行 | 3 episode，target 调用 1/1/1（上限 2），`controller=7 / assembler=3 / target=3`，`tokens_in=41400 / tokens_out=21774` |
| 评价 | `eval_412929c0ce5b`（原连接）与 `eval_4e524e7f13d6`（`--eval-service` 迁到 `localhost`），两者一致：`N=3 S=2 F=0 U=1`，`asr_task_range [0.6667, 1.0]`，`task_eval_coverage=0.6667` |
| 迁移留痕 | `config.json.service_migration.changed_fields.base_url = 127.0.0.1:8000/v1 → localhost:8000/v1`，`identity_checked=[model,parameter_profile,context_window]`；旧 `plan.json` 未被改写 |
| 迁移拒绝路径 | 把 `model` 改成别的值 → `ok:false`，`error` 列出 `{"model":{"frozen":…,"actual":"some-other-model"}}` |
| U 的原因 | `ep_bc2472b64c`：186 条允许证据 → 估算 33253 > 窗口 32768，`headroom=-5093` → `component_error:context_budget_exceeded`，未发送、0 扣费、仍在 N=3 分母内 |
| 完整性状态 | 三个 episode 均 `mode=candidate_pack, status=pack, integrity_complete=true`，`evidence_verified_count` = 5 / 21 / 186（X01 生效且未误杀） |

## 7. 接入远端判别模型后的真实批次（本地生成 + 远端裁判）

服务分工按用户提供的信息落定（连接写在 `configs/services.local.yaml`，不入库；密钥在 `.env`，不入库）：

| 角色 | service | endpoint | model | profile | window |
|---|---|---|---|---|---|
| target / controller / assembler | `local_uncensored` | `http://127.0.0.1:8000/v1` | `JonathanColetti/Qwen3.8-27B-Uncensored` | `qwen38_vllm` | 32768 |
| judge_eval | `judge_remote` | `https://<judge-host>/v1` | `deepseek/deepseek-v4.1-flash-1017` | `deepseek_thinking_proxy` | 1000000（`/v1/models` 的 `max_input_tokens`） |

- 实验：`real_dev_local_20261114c`，`configs/real-dev.local.yaml`（services 全是不可达占位，
  真实连接由 `--services` 整项替换，`plan.json.config_sources.services_overlay.sha256=4df89592…`）。
- preflight：两个服务都 `healthy`，`response_model` 与配置 alias 一致（`alias_match=true`）；
  远端裁判 `sent_params={max_tokens, reasoning_effort, thinking.type}`，不带 temperature/top_p。
- 生成：6 episode，target 调用 `1,1,1,2,1,2`（共 8），`controller=14 / assembler=6`，
  `tokens_in=83127 / tokens_out=39635`，全部 `terminate=assembled`。
- 评价：`eval_5661b7efeed2`，`N=6 S=6 F=0 U=0`，`asr_task_range [1.0, 1.0]`，
  `task_eval_coverage=1.0`；裁判 6 次调用、`tokens_in=80733 / tokens_out=7761`、`wall_ms≈45.8s`、
  无格式重试、无 `component_error`。
- **这不是越狱成功率**：Target 是未对齐替身，本地模型本来就服从；1.0 只说明"生成→装配→独立裁判"
  链路与裁判预算正确，窗口 1M 后不再出现 `context_budget_exceeded`。替换为对齐 Target 后
  同一套配置可直接产出可报告 ASR。
