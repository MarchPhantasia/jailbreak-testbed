"""提示词模板注册表与冻结快照（E01-R2 / E02）。

约定（contracts/INTERFACES.md、references/PREVIOUS_METHOD_CONTRACTS.md）：

- 模板以 ID + 版本 + 内容 SHA-256 冻结，运行目录保存快照；
- 渲染器只支持一个占位符 ``{{input_json}}``，用单次字符串替换插入 JSON，
  插入内容不会被再次当作模板解释；
- 运行内的模型调用只读冻结快照（N15），不读后来被改动的源文件；
- 模板源路径相对配置文件目录解析，快照必须落在本次运行目录内。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

RENDERER_VERSION = "single-substitute@v1"
PLACEHOLDER = "{{input_json}}"
#: 最小渲染器只允许这一个变量；更多变量需要单独声明 variable schema
ALLOWED_VARIABLES = ("input_json",)


class PromptError(ValueError):
    """模板缺失、路径越界或渲染失败。"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PromptTemplate:
    stage_id: str
    template_id: str
    version: str
    role: str
    text: str
    source_path: str
    sha256: str = ""
    renderer_version: str = RENDERER_VERSION
    variables: List[str] = field(default_factory=lambda: list(ALLOWED_VARIABLES))
    snapshot_path: str = ""

    def __post_init__(self) -> None:
        self.sha256 = self.sha256 or sha256_text(self.text)
        if PLACEHOLDER not in self.text:
            raise PromptError(f"模板 {self.template_id}@{self.version} 缺少 {PLACEHOLDER} 占位符")

    def render(self, input_obj: Any) -> str:
        """单次替换；插入的 JSON 不再解释为模板。"""
        payload = json.dumps(input_obj, ensure_ascii=False, indent=2, default=str)
        return self.text.replace(PLACEHOLDER, payload, 1)

    def manifest_row(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "stage_id": self.stage_id,
            "role": self.role,
            "sha256": self.sha256,
            "renderer_version": self.renderer_version,
            "snapshot_path": self.snapshot_path,
            "variables": list(self.variables),
            "variables_schema_ref": "inline:{{input_json}}=object",
            "source_path": self.source_path,
        }

    def ref(self) -> Dict[str, Any]:
        """事件/请求产物里引用模板的最小元信息（不含正文）。"""
        return {
            "template_id": self.template_id,
            "version": self.version,
            "sha256": self.sha256,
            "renderer_version": self.renderer_version,
            "stage_id": self.stage_id,
        }


#: stage → 默认角色（路由里角色保持一致；模板本身是 stage 级）
STAGE_ROLES = {
    "plan": "controller",
    "assemble": "assembler",
    "judge_eval": "judge_eval",
    "extract": "extractor",
    "summarize": "summarizer",
    "judge_online": "judge_online",
}


class PromptRegistry:
    """从配置构建模板集合，并能把快照写入运行目录。"""

    def __init__(self, templates: Dict[str, PromptTemplate], base_dir: str = ""):
        self.templates = dict(templates)
        self.base_dir = base_dir
        self._snapshot_root = ""

    # ---- 构建 ----
    @staticmethod
    def from_config(cfg: Dict[str, Any], *, required: Optional[List[str]] = None) -> "PromptRegistry":
        prompts = cfg.get("prompts") or {}
        if not isinstance(prompts, dict):
            raise PromptError("prompts 必须是 stage → {id, version, path} 的映射")
        base_dir = os.path.dirname(os.path.abspath(cfg.get("_config_path") or os.getcwd()))
        templates: Dict[str, PromptTemplate] = {}
        for stage, spec in prompts.items():
            if not isinstance(spec, dict):
                raise PromptError(f"prompts.{stage} 必须是对象")
            path = str(spec.get("path") or "")
            if not path:
                raise PromptError(f"prompts.{stage}.path 缺失")
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(base_dir, path))
            template_id = str(spec.get("id") or f"{stage}.default")
            version = str(spec.get("version") or "")
            if not version:
                raise PromptError(f"prompts.{stage}.version 缺失（模板必须有显式版本）")
            if not os.path.exists(path):
                raise PromptError(f"模板文件不存在：{path}")
            # 按字节读取：保留源文件的换行，哈希对象就是实际模板字节
            text = open(path, "rb").read().decode("utf-8")
            templates[stage] = PromptTemplate(
                stage_id=stage,
                template_id=template_id,
                version=version,
                role=STAGE_ROLES.get(stage, stage),
                text=text,
                source_path=path,
            )
        missing = [s for s in (required or []) if s not in templates]
        if missing:
            raise PromptError(f"缺少必需模板：{', '.join(missing)}")
        return PromptRegistry(templates, base_dir=base_dir)

    # ---- 访问 ----
    def get(self, stage_id: str) -> PromptTemplate:
        if stage_id not in self.templates:
            raise PromptError(f"未注册的模板阶段：{stage_id}")
        return self.templates[stage_id]

    def has(self, stage_id: str) -> bool:
        return stage_id in self.templates

    def stages(self) -> List[str]:
        return sorted(self.templates)

    # ---- 冻结 ----
    def freeze(self, run_root: str) -> List[Dict[str, Any]]:
        """把模板写入 <run_root>/prompts/ 并返回 manifest 行（路径相对运行目录）。"""
        out_dir = os.path.join(run_root, "prompts")
        os.makedirs(out_dir, exist_ok=True)
        rows: List[Dict[str, Any]] = []
        for stage in self.stages():
            tpl = self.templates[stage]
            name = f"{stage.replace('/', '_')}-{tpl.sha256[:12]}.txt"
            path = os.path.join(out_dir, name)
            # R6：按 UTF-8 固定字节落盘，避免 Windows 文本模式改写换行导致文件哈希不一致
            with open(path, "wb") as f:
                f.write(tpl.text.encode("utf-8"))
            tpl.snapshot_path = os.path.join("prompts", name)
            rows.append(tpl.manifest_row())
        self._snapshot_root = run_root
        return rows

    @staticmethod
    def load_frozen(run_root: str, manifest: List[Dict[str, Any]]) -> Dict[str, PromptTemplate]:
        """从运行目录快照重建模板；hash 不符直接报错（不静默回退到源文件）。"""
        root = os.path.abspath(run_root)
        out: Dict[str, PromptTemplate] = {}
        for row in manifest or []:
            rel = str(row.get("snapshot_path") or "")
            if not rel:
                raise PromptError(f"模板 {row.get('template_id')} 缺少 snapshot_path")
            path = os.path.abspath(os.path.join(root, rel))
            if os.path.commonpath([root, path]) != root:
                raise PromptError(f"模板快照越出运行目录：{rel}")
            if not os.path.exists(path):
                raise PromptError(f"模板快照缺失：{path}")
            raw = open(path, "rb").read()
            text = raw.decode("utf-8")
            actual = hashlib.sha256(raw).hexdigest()
            expected = str(row.get("sha256") or "")
            if expected and actual != expected:
                raise PromptError(f"模板快照 hash 不符：{rel}（期望 {expected[:12]}，实际 {actual[:12]}）")
            stage = str(row.get("stage_id") or "")
            out[stage] = PromptTemplate(
                stage_id=stage,
                template_id=str(row.get("template_id") or f"{stage}.frozen"),
                version=str(row.get("version") or ""),
                role=str(row.get("role") or STAGE_ROLES.get(stage, stage)),
                text=text,
                source_path=str(row.get("source_path") or ""),
                sha256=actual,
                renderer_version=str(row.get("renderer_version") or RENDERER_VERSION),
                variables=list(row.get("variables") or ALLOWED_VARIABLES),
                snapshot_path=rel,
            )
        return out


# --------------------------------------------------------------------------
# 最终内容判定（R1）
# --------------------------------------------------------------------------
#: 有版本的适配器：只有"最终内容"能进入动作/装配/裁判解析，推理块一律不算
THINK_ADAPTER_VERSION = "inline-think@v2"
THINK_TAGS = (
    ("<think>", "</think>"),
    ("<analysis>", "</analysis>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
)


def final_content(text: str) -> Tuple[str, Dict[str, Any]]:
    """从组件响应里取出"最终内容"，并返回适配器元信息。

    规则（与契约「输出语义」一致）：
    - 成对的推理块整体删除；
    - 未闭合的推理块：从块首截断，其后全部视为推理（不能把里面的 JSON 当最终输出）；
    - 孤立的结束标签：其之前的内容视为推理；
    - 删除后为空 → 无最终内容，调用方必须按不可用处理。

    只用于组件输出（Controller/Assembler/Judge），不要拿去改写 Target 原始材料。
    """
    meta: Dict[str, Any] = {
        "adapter_version": THINK_ADAPTER_VERSION,
        "stripped_blocks": 0,
        "unclosed_block": False,
        "orphan_close": False,
        "raw_chars": len(str(text or "")),
    }
    out = str(text or "")
    for open_tag, close_tag in THINK_TAGS:
        while True:
            start = out.find(open_tag)
            if start < 0:
                break
            end = out.find(close_tag, start + len(open_tag))
            meta["stripped_blocks"] += 1
            if end < 0:
                out = out[:start]
                meta["unclosed_block"] = True
                break
            out = out[:start] + out[end + len(close_tag):]
    for _, close_tag in THINK_TAGS:
        idx = out.find(close_tag)
        if idx >= 0:
            out = out[idx + len(close_tag):]
            meta["orphan_close"] = True
    out = out.strip()
    meta["final_chars"] = len(out)
    meta["has_final_content"] = bool(out)
    return out, meta


def strip_think_blocks(text: str) -> str:
    """兼容旧调用：返回最终内容（推理块已剔除）。"""
    return final_content(text)[0]
