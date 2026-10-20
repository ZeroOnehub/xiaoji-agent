"""每日摘要的离线测试：不联网、不调真实模型。"""
import asyncio
from datetime import date, datetime, time as dtime

import httpx
from nanobot.providers.base import LLMProvider, LLMResponse

from pm_agent.digest import Digest, ended_day, zone
from pm_agent.store import Store

AT = dtime(0, 5)
REPOS = [{'name': f'o/r{i}', 'url': f'https://github.com/o/r{i}', 'description': f'项目{i}', 'stars': 100 - i,
          'created': '2026-10-19', 'language': 'Python'} for i in range(5)]


class Fake(LLMProvider):
    def __init__(self, content):
        super().__init__(provider_name='fake')
        self.content, self.calls = content, []

    def get_default_model(self):
        return 'fake'

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        return LLMResponse(content=self.content)


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=zone())


def test_schedule_first_deploy_then_daily(tmp_path):
    dg = Digest(Store(tmp_path / 'db'), Fake('x'), tmp_path / 'dg')
    assert ended_day(at(2026, 10, 21, 0, 4), AT) == date(2026, 10, 19)
    assert ended_day(at(2026, 10, 21, 0, 5), AT) == date(2026, 10, 20)
    # 第一次部署只记基准，不补发
    day, state = dg.due({}, at(2026, 10, 20, 6, 0), AT)
    assert day is None and state['last'] == '2026-10-19'
    assert dg.due(state, at(2026, 10, 20, 23, 59), AT)[0] is None
    assert dg.due(state, at(2026, 10, 21, 0, 5), AT)[0] == date(2026, 10, 20)
    # 离线两天回来，只补最近结束的那一天
    assert dg.due(state, at(2026, 10, 23, 9, 0), AT)[0] == date(2026, 10, 22)


def test_build_records_seen_filter_and_model_text(tmp_path, monkeypatch):
    s = Store(tmp_path / 'db')
    s.add('memory', '周五交项目', '周五交项目')
    s.add('minutes', '周会纪要', '...')
    s.add('prd', '旧 PRD', '...')
    dg = Digest(s, Fake('### o/r1\n- 是什么：……'), tmp_path / 'dg')

    async def fetch(day, limit=10):
        return REPOS
    monkeypatch.setattr(dg, 'fetch', fetch)
    text, picked = asyncio.run(dg.build(datetime.now(zone()).date(), seen={'o/r0'}))
    assert '记事：周五交项目' in text and '纪要：周会纪要' in text and '旧 PRD' not in text
    assert picked == ['o/r1', 'o/r2', 'o/r3']
    assert '### o/r1' in text and 'https://github.com/o/r3' in text
    assert 'o/r0' not in dg.provider.calls[0][1]['content']


def test_build_falls_back_when_budget_refused_or_github_fails(tmp_path, monkeypatch):
    dg = Digest(Store(tmp_path / 'db'), Fake('今日预算不足，已停止模型请求'), tmp_path / 'dg')

    async def fetch(day, limit=10):
        return REPOS[:2]
    monkeypatch.setattr(dg, 'fetch', fetch)
    text, picked = asyncio.run(dg.build(date(2026, 10, 20)))
    assert '- o/r0：项目0（100⭐）' in text and '预算不足' not in text
    assert '没有新的纪要和记事' in text

    async def broken(day, limit=10):
        raise httpx.ConnectError('down')
    monkeypatch.setattr(dg, 'fetch', broken)
    text, picked = asyncio.run(dg.build(date(2026, 10, 20)))
    assert 'GitHub 搜索失败' in text and picked == []
