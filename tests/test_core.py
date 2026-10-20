import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from pm_agent.store import Store
from pm_agent.engine import generate, Model


def test_approval_reject_replay_and_stale(tmp_path):
    s = Store(tmp_path/'db')
    item = s.add('prd','草稿','v1')
    rejected = s.propose(item['id'],'delete')
    assert s.decide(rejected,False) == 'rejected'
    assert s.get(item['id'])['content'] == 'v1'
    with pytest.raises(ValueError): s.decide(rejected,True)
    first = s.propose(item['id'],'replace','v2')
    stale = s.propose(item['id'],'replace','bad')
    assert s.decide(first,True) == 'succeeded'
    assert s.decide(stale,True) == 'stale'
    assert s.get(item['id'])['content'] == 'v2'


def test_memory_restart_and_delete(tmp_path):
    p=tmp_path/'db'; s=Store(p)
    item=s.add('memory','背景','只做小团队')
    assert Store(p).list('memory')[0]['content']=='只做小团队'
    aid=s.propose(item['id'],'delete');s.decide(aid,True)
    assert Store(p).list('memory')==[]


def test_budget_concurrent_and_restart(tmp_path):
    p=tmp_path/'db';s=Store(p)
    def reserve(_):
        try: s.reserve('2026-10-19',.4,1);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=5) as ex: assert sum(ex.map(reserve,range(5)))==2
    assert Store(p).spent('2026-10-19')==pytest.approx(.8)
    s.settle('2026-10-19',.4,.1)
    assert s.spent('2026-10-19')==pytest.approx(.5)


@pytest.mark.parametrize('workflow',['opportunity','prd'])
def test_real_nanobot_loop_demo(tmp_path,workflow):
    s=Store(tmp_path/'db');s.add('memory','背景','中文写作，服务小团队')
    s.add('source','访谈A','测试用户说整理反馈费时间')
    events=[]
    output=asyncio.run(generate(s,tmp_path/'runtime','demo',workflow,'用户反馈助手','',events.append))
    assert '小团队' in output and '访谈A' in output and '演示' in output
    assert any(e['type']=='tool.completed' and e['name']=='read_context' for e in events)
    assert all(e['name']=='read_context' for e in events)
    assert not (tmp_path/'runtime/agent/memory/MEMORY.md').exists()


def test_auth_csrf_export_and_validation(tmp_path,monkeypatch):
    from pm_agent import app as module
    monkeypatch.setattr(module,'STORE',Store(tmp_path/'db'))
    with TestClient(module.app) as c:
        assert c.get('/api/state').status_code==401
        assert c.get('/').status_code==200
        token=c.get('/api/state').json()['csrf']
        payload={'kind':'memory','title':'test','content':'<script>alert(1)</script>'}
        assert c.post('/api/items',json=payload).status_code==403
        headers={'X-PM-CSRF':token}
        assert c.post('/api/items',json=payload,headers={**headers,'Origin':'https://evil.invalid'}).status_code==403
        item=c.post('/api/items',json=payload,headers=headers).json()
        assert c.get('/api/export/'+item['id']).text==payload['content']
        assert c.post('/api/items',json={**payload,'kind':'shell'},headers=headers).status_code==422
        assert c.post('/api/approvals',json={'target':'../../etc/passwd','action':'delete'},headers=headers).status_code==400


def test_expired_approval(tmp_path):
    s=Store(tmp_path/'db');i=s.add('memory','t','keep');a=s.propose(i['id'],'delete')
    with s.db() as db:db.execute('UPDATE approvals SET created=0 WHERE id=?',(a,))
    assert s.decide(a,True)=='expired'
    assert s.get(i['id'])['content']=='keep'


def test_live_missing_key_does_not_charge(tmp_path,monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    s=Store(tmp_path/'db');m=Model(s,'live','prd','test')
    with pytest.raises(ValueError,match='DEEPSEEK_API_KEY'):
        asyncio.run(m.chat([{'role':'user','content':'test'}]))


def test_live_adapter_payload_tools_and_cost(tmp_path,monkeypatch):
    from types import SimpleNamespace as NS
    import openai
    from pm_agent.engine import day
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only-placeholder')
    monkeypatch.setenv('PM_INPUT_PRICE','.30');monkeypatch.setenv('PM_OUTPUT_PRICE','1.20')
    monkeypatch.setenv('PM_DAILY_BUDGET_USD','1')
    captured={}
    class FakeClient:
        def __init__(self,**kw): self.chat=NS(completions=self);captured['config']=kw
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def create(self,**kw):
            captured['request']=kw
            return NS(usage=NS(prompt_tokens=1000,completion_tokens=200), choices=[NS(message=NS(content=None,tool_calls=[NS(id='1',function=NS(name='read_context',arguments='{}'))]))])
    monkeypatch.setattr(openai,'AsyncOpenAI',FakeClient)
    s=Store(tmp_path/'db'); m=Model(s,'live','prd','test')
    r=asyncio.run(m.chat([{'role':'user','content':'hello'}],tools=[{'type':'function','function':{'name':'read_context'}}]))
    assert r.tool_calls[0].name=='read_context'
    assert captured['request']['max_tokens']==4000
    assert captured['config']['max_retries']==0
    assert s.spent(day())==pytest.approx(.00054)


def test_model_cannot_use_shell(tmp_path,monkeypatch):
    import pm_agent.engine as engine
    from nanobot.providers.base import LLMResponse,ToolCallRequest
    observed=[]
    async def malicious(self,messages,**kwargs):
        self.calls+=1
        if self.calls==1:
            return LLMResponse(content='',tool_calls=[ToolCallRequest(id='attack',name='exec',arguments={'command':'touch /tmp/should-never-run'})])
        observed.extend(messages)
        return LLMResponse(content='危险工具不可用。')
    monkeypatch.setattr(engine.Model,'chat',malicious)
    s=Store(tmp_path/'db')
    result=asyncio.run(generate(s,tmp_path/'runtime','demo','prd','test','',lambda e:None))
    assert '危险工具不可用' in result
    tool_messages=[str(m.get('content','')) for m in observed if m.get('role')=='tool']
    assert any('not found' in m.lower() or 'unknown' in m.lower() for m in tool_messages)
