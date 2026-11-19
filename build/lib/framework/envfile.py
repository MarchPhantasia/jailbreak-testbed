"""可选的 env 文件加载（凭证入口）。

框架本身只从 ``os.environ`` 读取密钥；本模块提供一个**显式**加载器，方便在项目目录
本地运行时用 `.env`，不改变"系统环境优先"的语义：

- 只有显式传 ``--env-file`` 或设置 ``JBT_ENV_FILE`` 才会读取，不自动搜索；
- 已存在的环境变量默认不被覆盖（override=False），系统/服务配置优先；
- 返回值只含键名与统计，不回显值；不把密钥写入日志、异常正文或产物。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """env 文件缺失、格式错误或键名非法。"""


def parse_env_file(path: str) -> Dict[str, str]:
    """解析 KEY=VALUE；支持注释、空行、`export ` 前缀与成对引号。"""
    if not os.path.exists(path):
        raise EnvFileError(f"env 文件不存在：{path}")
    try:
        text = open(path, "rb").read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvFileError(f"env 文件不是 UTF-8：{path}") from exc
    values: Dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise EnvFileError(f"{path}:{lineno} 不是 KEY=VALUE 形式")
        key, _, value = line.partition("=")
        key = key.strip()
        if not KEY_RE.match(key):
            raise EnvFileError(f"{path}:{lineno} 键名非法：{key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: str, *, override: bool = False, environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把文件里的键写进 ``os.environ``；默认不覆盖已存在的变量。"""
    env = os.environ if environ is None else environ
    values = parse_env_file(path)
    loaded, skipped = [], []
    for key, value in values.items():
        if key in env and not override:
            skipped.append(key)
            continue
        env[key] = value
        loaded.append(key)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0
    return {
        "path": os.path.abspath(path),
        "loaded_keys": loaded,
        "skipped_existing_keys": skipped,
        "count": len(loaded),
        "permissions": oct(mode),
        # 只提示权限，不读取、不回显任何值
        "warning": "文件对其他用户可读，建议 chmod 600" if mode & 0o077 else "",
    }


def load_env_if_configured(explicit: Optional[str], *, override: bool = False, environ: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """显式路径优先，其次 JBT_ENV_FILE；都没有则什么都不做。"""
    env = os.environ if environ is None else environ
    path = (explicit or env.get("JBT_ENV_FILE") or "").strip()
    if not path:
        return None
    return load_env_file(path, override=override, environ=env)
