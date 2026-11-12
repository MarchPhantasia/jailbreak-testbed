# 提示词模板（版本化）

模板是实验条件的一部分，必须显式版本化并冻结到运行目录。

| 文件 | stage | 角色 | 版本 | 说明 |
|---|---|---|---|---|
| `plan.v1.txt` | plan | controller | v1 | 规划并生成自包含子查询（`{{input_json}}` 单次替换） |
| `assemble.v1.txt` | assemble | assembler | v1 | 证据约束装配，输出 answer/claims/gaps |
| `judge_eval.v1.txt` | judge_eval | judge_eval | v1 | 独立语义评价，输出三维度或 unscored |

约定：

- 每个模板必须包含 `JBT_STAGE=<STAGE>` 首行标记与 `INPUT_JSON\n{{input_json}}` 输入段；
- 渲染器只有一个占位符 `{{input_json}}`，使用单次字符串替换（`single-substitute@v1`），
  插入的 JSON 不会被再次当作模板解释；
- 运行开始时，模板内容会复制到 `<results>/<experiment>/prompts/`，`plan.json` 的
  `prompt_manifest` 记录 `template_id/version/sha256/renderer_version/snapshot_path`；
- 评价（`judge_eval`）只读运行目录里的冻结快照，不读工作区中被改动过的源文件；
- 修改模板必须新建版本号（`v2`）并新建实验，不要原地改 v1 后继续沿用旧运行。

研究用途说明：本仓库的模板用于在受控环境中测量对齐 Target 的越狱 ASR。模板内容是
研究条件的一部分，不是对第三方系统的使用建议；真实目标、密钥与数据由研究者在本机提供。
