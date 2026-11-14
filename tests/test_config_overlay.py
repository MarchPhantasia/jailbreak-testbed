"""配置便利入口的离线验收（CONFIG_AND_EXPERIMENT_PRIORITY）。

三条要求，全部用假服务验证，不连接真实模型：
1. `--services` 覆盖确实进入最终网关与冻结计划，且按 service ID 整项替换；
2. 不同角色不串服务：只迁移一个服务不会顺带改另一个角色；
3. 旧实验重评的接口迁移留下实际连接记录、不改旧 plan，改变实验条件时明确报错。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from framework import runner
from framework.cli import main as cli_main
from test_execution1_r2 import fake, routed_cfg, run_once  # noqa: F401  fake 是 fixture


def write_overlay(tmp_path: Path, services: dict, name: str = "services.local.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump({"services": services}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def migrated_service(base_url: str = "http://migrated.invalid/v1", **extra) -> dict:
    return {
        "type": "openai",
        "base_url": base_url,
        "model": "unit-judge",
        "api_key_env": "JBT_UNIT_KEY",
        "auth_required": True,
        "timeout_s": 3,
        "parameter_profile": "deepseek_thinking_proxy",
        **extra,
    }


def test_overlay_replaces_service_whole_and_is_frozen(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    cfg_path = tmp_path / "experiment.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    cfg = runner.load_config(str(cfg_path))
    overlay = write_overlay(tmp_path, {"general": migrated_service()})
    info = runner.apply_service_overlay(cfg, str(overlay))
    assert info["replaced_service_ids"] == ["general"]
    assert info["sha256"]
    # 整项替换：旧服务的字段不会残留
    assert set(cfg["services"]["general"]) == set(migrated_service())
    root, ep, plan, _ = run_once(cfg)
    assert plan["routing"]["services"]["general"]["base_url"] == "http://migrated.invalid/v1"
    assert plan["evaluation_spec"]["service"]["base_url"] == "http://migrated.invalid/v1"
    assert plan["config_sources"]["services_overlay"]["replaced_service_ids"] == ["general"]
    assert plan["config_sources"]["experiment"]["path"] == str(cfg_path)
    # 数据/模板路径仍按实验 YAML 的 path_base 解析（覆盖文件不改变基准目录）
    assert plan["config_sources"]["experiment"]["sha256"] == runner._file_sha256(str(cfg_path))


def test_overlay_rejects_non_services_keys(tmp_path):
    cfg = routed_cfg(tmp_path)
    path = tmp_path / "bad.yaml"
    path.write_text("experiment:\n  id: nope\nservices: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        runner.apply_service_overlay(cfg, str(path))
    path2 = tmp_path / "empty.yaml"
    path2.write_text("services: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        runner.apply_service_overlay(cfg, str(path2))


def test_overlay_does_not_cross_roles(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    overlay = write_overlay(tmp_path, {"general": migrated_service()})
    runner.apply_service_overlay(cfg, str(overlay))
    root, ep, plan, _ = run_once(cfg)
    services = plan["routing"]["services"]
    # 只迁移裁判服务：Target 仍指原服务与地址
    assert services["general"]["base_url"] == "http://migrated.invalid/v1"
    assert services["target"]["base_url"] == "http://fixture.invalid/v1"
    assert plan["evaluation_spec"]["service"]["service_id"] == "general"
    roles = {r: s for r, s in plan["routing"]["components"].items()}
    assert roles["target"]["service_ref"] == "target"
    assert roles["judge_eval"]["service_ref"] == "general"


def test_evaluate_migration_records_actual_connection(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    frozen_plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    cfg2 = copy.deepcopy(cfg)
    cfg2["services"]["judge_api"] = migrated_service()
    out = runner.evaluate_experiment(cfg2, root=str(root), eval_service="judge_api")
    eval_dir = Path(out["dir"])
    config = json.loads((eval_dir / "config.json").read_text(encoding="utf-8"))
    migration = config["service_migration"]
    assert migration["actual"]["base_url"] == "http://migrated.invalid/v1"
    assert migration["frozen"]["base_url"] == "http://fixture.invalid/v1"
    assert "base_url" in migration["changed_fields"]
    events = [json.loads(l) for l in (eval_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    started = next(e for e in events if e["type"] == "evaluation_started")
    assert started["payload"]["base_url"] == "http://migrated.invalid/v1", "网关必须实际使用迁移后的地址"
    # 旧实验的 plan 不被重写
    assert json.loads((root / "plan.json").read_text(encoding="utf-8")) == frozen_plan
    assert out["N"] == 1 and out["S"] == 1


def test_evaluate_migration_rejects_identity_changes(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    cfg2 = copy.deepcopy(cfg)
    cfg2["services"]["judge_api"] = migrated_service(model="another-judge")
    with pytest.raises(ValueError, match="实验条件"):
        runner.evaluate_experiment(cfg2, root=str(root), eval_service="judge_api")
    with pytest.raises(ValueError, match="不存在"):
        runner.evaluate_experiment(cfg2, root=str(root), eval_service="missing_service")


def test_evaluate_without_flag_keeps_frozen_service(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    cfg2 = copy.deepcopy(cfg)
    cfg2["services"]["judge_api"] = migrated_service()
    out = runner.evaluate_experiment(cfg2, root=str(root))
    config = json.loads((Path(out["dir"]) / "config.json").read_text(encoding="utf-8"))
    assert "service_migration" not in config
    assert config["evaluation_spec"]["service"]["base_url"] == "http://fixture.invalid/v1"


def test_show_config_cli_is_offline_and_hides_secrets(tmp_path, fake, capsys):
    cfg = routed_cfg(tmp_path)
    cfg_path = tmp_path / "experiment.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    code = cli_main(["show-config", "--config", str(cfg_path)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["calls_model"] is False
    assert out["roles"]["target"]["model"] == "unit-target"
    assert out["roles"]["judge_eval"]["service_id"] == "general"
    assert out["roles"]["target"]["api_key_env"] == "JBT_UNIT_KEY"
    assert out["roles"]["target"]["api_key_set"] is True
    assert "unit-secret-value" not in json.dumps(out), "show-config 不得回显密钥值"
    assert out["shared_services"] == [] or all(s["roles"] for s in out["shared_services"])


def test_show_config_accepts_services_overlay(tmp_path, fake, capsys):
    cfg = routed_cfg(tmp_path)
    cfg_path = tmp_path / "experiment.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    overlay = write_overlay(tmp_path, {"general": migrated_service()})
    code = cli_main(["show-config", "--config", str(cfg_path), "--services", str(overlay)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"]["judge_eval"]["base_url"] == "http://migrated.invalid/v1"
    assert out["roles"]["target"]["base_url"] == "http://fixture.invalid/v1"
    assert out["services_overlay"]["replaced_service_ids"] == ["general"]
