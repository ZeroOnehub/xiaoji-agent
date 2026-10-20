"""每日摘要：美国时间一天结束后（默认太平洋时间 00:05），把这一天的记录和新冒头的 AI 开源项目发到飞书。

只读：查本地记录、查 GitHub 公开搜索；模型只负责写点评。发送对象固定是 allowFrom 里的用户本人。
"""
import asyncio
import json
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage

QUERY = 'llm OR agent OR ai created:>={since} stars:>30'
REFUSALS = ('今日预算不足', '上下文过长')
PROMPT = '''你在给一位正在求职的 AI 产品经理写每日 AI 开源摘要。用户消息是 GitHub 上近 7 天新建、星标涨得最快的项目（JSON）。
对每个项目输出：
### 项目名
- 是什么：一句话，只根据描述，不要编造功能
- 为什么值得看：结合星标和创建时间，说它踩中了什么趋势
- 面试可能怎么问：一个产品经理视角的问题
只用给出的信息，拿不准就直说。不要开场白和结尾。'''


def zone():
    return ZoneInfo(os.getenv('XIAOJI_DIGEST_TZ', 'America/Los_Angeles'))


def send_at():
    h, m = os.getenv('XIAOJI_DIGEST_AT', '00:05').split(':')
    return dtime(int(h), int(m))


def ended_day(now, at):
    """now 之前最近一个已经结束、并且已过发送时刻的日期。"""
    return now.date() - timedelta(days=1 if now.time() >= at else 2)


class Digest:
    def __init__(self, store, provider, state_dir):
        self.store, self.provider = store, provider
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / 'state.json'

    def load(self):
        try:
            return json.loads(self.state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def save(self, state):
        self.state_file.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

    def due(self, state, now, at):
        """该补发哪一天；第一次部署只记下基准、不补发。返回 (日期或 None, 新状态)。"""
        day = ended_day(now, at)
        if 'last' not in state:
            return None, {**state, 'last': day.isoformat()}
        return (day if date.fromisoformat(state['last']) < day else None), state

    async def fetch(self, day, limit=10):
        since = (day - timedelta(days=6)).isoformat()
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            r = await client.get('https://api.github.com/search/repositories',
                                 params={'q': QUERY.format(since=since), 'sort': 'stars', 'order': 'desc', 'per_page': limit},
                                 headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'xiaoji-digest'})
            r.raise_for_status()
            items = r.json()['items']
        return [{'name': x['full_name'], 'url': x['html_url'], 'description': (x['description'] or '')[:200],
                 'stars': x['stargazers_count'], 'created': x['created_at'][:10], 'language': x['language']} for x in items]

    def records(self, day):
        start = datetime.combine(day, dtime(0), zone()).timestamp()
        return [r for r in reversed(self.store.list())
                if r['kind'] in ('memory', 'minutes') and start <= r['created'] < start + 86400]

    async def build(self, day, seen=()):
        lines = [f'# 每日摘要 · {day:%m-%d}（美国太平洋时间）', '', '## 今天的记录']
        recs = self.records(day)
        lines += [f"- {'纪要' if r['kind'] == 'minutes' else '记事'}：{r['title']}" for r in recs] or ['- 没有新的纪要和记事']
        lines += ['', '## AI 开源新动向（近 7 天新建，按星标）']
        try:
            repos = [x for x in await self.fetch(day) if x['name'] not in seen][:3]
        except (httpx.HTTPError, KeyError, ValueError):
            lines.append('GitHub 搜索失败（可能被限流），下次再看。')
            return '\n'.join(lines), []
        if not repos:
            lines.append('没有新冒头的项目。')
            return '\n'.join(lines), []
        r = await self.provider.chat(messages=[{'role': 'system', 'content': PROMPT},
                                               {'role': 'user', 'content': json.dumps(repos, ensure_ascii=False)}],
                                     max_tokens=1500, temperature=.3)
        body = (r.content or '').strip()
        if not body or r.finish_reason == 'error' or body.startswith(REFUSALS):
            body = '\n'.join(f"- {x['name']}：{x['description'] or '（无描述）'}（{x['stars']}⭐）" for x in repos)
        lines += [body, '', '链接：' + ' · '.join(f"[{x['name']}]({x['url']})" for x in repos)]
        return '\n'.join(lines), [x['name'] for x in repos]

    async def run(self, bus, target):
        at = send_at()
        while True:
            try:
                day, state = self.due(self.load(), datetime.now(zone()), at)
                self.save(state)
                if day:
                    text, picked = await self.build(day, set(state.get('seen', [])))
                    await bus.publish_outbound(OutboundMessage(channel='feishu', chat_id=target, content=text))
                    (self.dir / f'{day}.md').write_text(text, encoding='utf-8')
                    self.save({**state, 'last': day.isoformat(), 'seen': (state.get('seen', []) + picked)[-200:]})
                    logger.info('每日摘要已发送：{}', day)
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('每日摘要失败，10 分钟后重试')
                await asyncio.sleep(600)


class DigestTool(Tool):
    name = 'daily_digest'
    description = '用户要「摘要」「今天的摘要」时调用：生成到目前为止的今日摘要（今天的记录 + 近 7 天新冒头的 AI 开源项目）。'
    parameters = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    read_only = True

    def __init__(self, digest):
        self.digest = digest

    async def execute(self, **kwargs):
        text, _ = await self.digest.build(datetime.now(zone()).date(), set(self.digest.load().get('seen', [])))
        return text
