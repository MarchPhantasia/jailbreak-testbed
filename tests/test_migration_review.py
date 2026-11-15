"""Small offline review checks for service migration and actual transport routing."""
import copy
import json
from pathlib import Path

from framework import runner
from test_execution1_r2 import FakeService,routed_cfg,run_once,rows
from test_config_overlay import migrated_service,write_overlay


class SpyService(FakeService):
    def __init__(self):
        super().__init__()
        self.destinations=[]

    def __call__(self,request,*args,**kwargs):
        url=request if isinstance(request,str) else request.full_url
        body=json.loads(request.data.decode('utf-8')) if not isinstance(request,str) and request.data else {}
        self.destinations.append((url,body.get('model')))
        return super().__call__(request,*args,**kwargs)


def bind_service(monkeypatch):
    service=SpyService()
    monkeypatch.setattr('urllib.request.urlopen',service)
    monkeypatch.setenv('JBT_UNIT_KEY','unit-placeholder')
    return service


def test_Y01_eval_migration_uses_selected_service_id_everywhere(tmp_path,monkeypatch):
    fake=bind_service(monkeypatch)
    cfg=routed_cfg(tmp_path)
    root,ep,plan,_=run_once(cfg)
    old_plan=(root/'plan.json').read_bytes()
    cfg2=copy.deepcopy(cfg)
    cfg2['services']['judge_api']=migrated_service()
    offset=len(fake.destinations)
    result=runner.evaluate_experiment(cfg2,root=str(root),eval_service='judge_api')
    assert [u for u,m in fake.destinations[offset:]]==['http://migrated.invalid/v1/chat/completions']
    assert (root/'plan.json').read_bytes()==old_plan
    evaldir=Path(result['dir'])
    config=json.loads((evaldir/'config.json').read_text(encoding='utf-8'))
    migration=config['service_migration']
    assert migration['requested_service_id']=='judge_api'
    assert migration['actual']['service_id']=='judge_api', migration
    assert migration['changed_fields']['service_id']=={'frozen':'general','actual':'judge_api'}
    events=rows(evaldir/'events.jsonl')
    started=next(e for e in events if e['type']=='evaluation_started')
    assert started['payload']['service_id']=='judge_api'
    sent=[e for e in events if e['type']=='request_sent']
    assert sent and all(e['payload']['service_id']=='judge_api' for e in sent)
    assert all(e['payload']['resolved_ref']['service_id']=='judge_api' for e in sent)


def test_G01_overlay_changes_target_transport_without_changing_controller(tmp_path,monkeypatch):
    fake=bind_service(monkeypatch)
    cfg=routed_cfg(tmp_path)
    replacement=dict(cfg['services']['target'],base_url='http://target-moved.invalid/v1')
    runner.apply_service_overlay(cfg,str(write_overlay(tmp_path,{'target':replacement})))
    run_once(cfg)
    target=[u for u,m in fake.destinations if m=='unit-target']
    controller=[u for u,m in fake.destinations if m=='unit-local']
    assert target and set(target)=={'http://target-moved.invalid/v1/chat/completions'}
    assert controller and set(controller)=={'http://fixture.invalid/v1/chat/completions'}
