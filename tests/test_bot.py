"""小纪（飞书机器人）的离线测试：不连飞书、不调真实模型。"""
import asyncio
from types import SimpleNamespace as NS

import pytest
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse

from pm_agent.bot import ALLOWED_TOOLS, Budgeted, MinutesTool, RecallTool, RememberTool, build, media_file
from pm_agent.engine import day
from pm_agent.store import Store


class Fake(LLMProvider):
    def __init__(self, content='# 周会\n纪要正文', usage=None):
        super().__init__(provider_name='fake')
        self.content, self.usage, self.calls = content, usage, []

    def get_default_model(self):
        return 'fake'

    async def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return LLMResponse(content=self.content, usage=self.usage)


def test_only_xiaoji_tools_survive(tmp_path):
    loop = build(Store(tmp_path / 'db'), MessageBus(), Fake(), tmp_path / 'ws', tmp_path / 'media', tmp_path / 'out')
    assert set(loop.tools.tool_names) == ALLOWED_TOOLS
    for dangerous in ('exec', 'write_file', 'read_file', 'web_fetch', 'message', 'apply_patch'):
        assert not loop.tools.has(dangerous)


def test_media_file_rejects_paths_outside_media(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'a.txt').write_text('x')
    (tmp_path / 'secret.txt').write_text('x')
    (media / 'link.txt').symlink_to(tmp_path / 'secret.txt')
    assert media_file(media / 'a.txt', media) == (media / 'a.txt').resolve()
    assert media_file('a.txt', media).name == 'a.txt'
    for bad in (tmp_path / 'secret.txt', '../secret.txt', media / 'link.txt', '/etc/hosts', media / 'missing.txt'):
        with pytest.raises(ValueError):
            media_file(bad, media)


def test_minutes_saved_and_transcript_kept_out_of_history(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    (media / '周会.txt').write_text('王敏：下周五提测。', encoding='utf-8')
    s, p = Store(tmp_path / 'db'), Fake()
    out = asyncio.run(MinutesTool(s, p, media, tmp_path / 'out').execute(path=str(media / '周会.txt')))
    assert '纪要正文' in out
    assert s.list('minutes')[0]['title'] == '周会'
    assert len(list((tmp_path / 'out').glob('*.md'))) == 1
    # 转写稿只发给纪要那一次独立调用
    assert '下周五提测' in p.calls[0][0][1]['content']


def test_minutes_refuses_outside_file_and_empty_text(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'empty.txt').write_text('  ', encoding='utf-8')
    t = MinutesTool(Store(tmp_path / 'db'), Fake(), media, tmp_path / 'out')
    assert '只能整理' in asyncio.run(t.execute(path='/etc/hosts'))
    assert '读不出文字' in asyncio.run(t.execute(path=str(media / 'empty.txt')))


def test_remember_and_recall(tmp_path):
    s = Store(tmp_path / 'db')
    asyncio.run(RememberTool(s).execute(content='XX 项目周五前交'))
    s.add('minutes', '周会', '赵磊明天发接口文档')
    r = RecallTool(s)
    assert 'XX 项目' in asyncio.run(r.execute(query='XX'))
    assert '接口文档' in asyncio.run(r.execute(query='接口'))
    assert '没有找到' in asyncio.run(r.execute(query='不存在的词'))
    assert s.list('source') == []


def test_budget_settles_actual_usage_and_blocks_when_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv('PM_INPUT_PRICE', '.30')
    monkeypatch.setenv('PM_OUTPUT_PRICE', '1.20')
    monkeypatch.setenv('PM_DAILY_BUDGET_USD', '1')
    s = Store(tmp_path / 'db')
    b = Budgeted(s, Fake('ok', usage=NS(input_tokens=1000, output_tokens=200)))
    r = asyncio.run(b.chat([{'role': 'user', 'content': 'hi'}]))
    assert r.content == 'ok'
    assert s.spent(day()) == pytest.approx(.00054)
    monkeypatch.setenv('PM_DAILY_BUDGET_USD', '0.0001')
    r = asyncio.run(b.chat([{'role': 'user', 'content': 'hi'}]))
    assert '预算' in r.content
    assert len(b.inner.calls) == 1
