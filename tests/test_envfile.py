"""env 文件加载：系统环境优先、只报告键名、绝不回显值。"""

from __future__ import annotations

import json
import os

import pytest

from framework import cli
from framework.envfile import EnvFileError, load_env_file, load_env_if_configured, parse_env_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "synthetic_library", "tasks.jsonl")


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_parse_supports_export_quotes_and_comments(tmp_path):
    path = write_env(
        tmp_path,
        "# comment\n\nA=1\nexport B='two words'\nC=\"three\"\nD=\n",
    )
    assert parse_env_file(path) == {"A": "1", "B": "two words", "C": "three", "D": ""}


def test_parse_rejects_malformed_lines_with_line_number(tmp_path):
    path = write_env(tmp_path, "A=1\nnot-a-pair\n")
    with pytest.raises(EnvFileError) as exc:
        parse_env_file(path)
    assert ":2" in str(exc.value)


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(EnvFileError):
        parse_env_file(str(tmp_path / "nope.env"))


def test_system_environment_wins_by_default(tmp_path):
    path = write_env(tmp_path, "JBT_KEY=from-file\n")
    env = {"JBT_KEY": "from-system"}
    info = load_env_file(path, environ=env)
    assert env["JBT_KEY"] == "from-system"
    assert info["loaded_keys"] == []
    assert info["skipped_existing_keys"] == ["JBT_KEY"]
    assert "from-file" not in json.dumps(info), "返回值不能包含密钥值"


def test_override_is_explicit(tmp_path):
    path = write_env(tmp_path, "JBT_KEY=from-file\n")
    env = {"JBT_KEY": "from-system"}
    load_env_file(path, override=True, environ=env)
    assert env["JBT_KEY"] == "from-file"


def test_jbt_env_file_fallback(tmp_path):
    path = write_env(tmp_path, "JBT_OTHER=1\n")
    env = {"JBT_ENV_FILE": path}
    info = load_env_if_configured(None, environ=env)
    assert info["loaded_keys"] == ["JBT_OTHER"] and env["JBT_OTHER"] == "1"
    assert load_env_if_configured(None, environ={}) is None


def test_cli_env_file_sets_missing_key_without_echoing_value(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JBT_CLI_KEY", raising=False)
    env_path = write_env(tmp_path, "JBT_CLI_KEY=super-secret-value\n")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        json.dumps(
            {
                "experiment": {
                    "id": "env_cli",
                    "seed": 1,
                    "session_mode": "stateless",
                    "results_dir": str(tmp_path / "results"),
                    "max_steps": 2,
                    "budget": {"target_calls": 1, "non_target_calls": 4},
                },
                "datasets": {
                    "adapter": "synthetic_library",
                    "path": FIXTURE,
                    "split": "dev",
                    "dev_ratio": 1,
                    "split_seed": 1,
                    "manifests_dir": str(tmp_path / "manifests"),
                },
                "method": {
                    "name": "independent@v0.1",
                    "controller": {"type": "element"},
                    "state": {"backend": "fox", "max_chars": 200},
                    "assembly": {"k": 1, "selector": "coverage"},
                    "judge": {"rubric_id": "inline", "version": "v1"},
                },
                "target": {"type": "mock"},
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["prepare-data", "--config", str(cfg_path), "--env-file", env_path]) == 0
    assert os.environ["JBT_CLI_KEY"] == "super-secret-value"
    captured = capsys.readouterr()
    assert "JBT_CLI_KEY" in captured.err
    assert "super-secret-value" not in captured.err + captured.out
