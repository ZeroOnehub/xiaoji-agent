import asyncio
import os
import secrets
import uuid
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import Store

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('PM_DATA_DIR', str(ROOT / '.data')))
STORE = Store(DATA / 'pm.sqlite3')
TOKEN = secrets.token_urlsafe(32)
JOBS = {}
TASKS = set()
app = FastAPI(title='PM 灵感台', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def local_auth(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        if not secrets.compare_digest(request.cookies.get('pm_session', ''), TOKEN):
            return JSONResponse({'detail': '请刷新首页建立本机会话'}, status_code=401)
        if request.method != 'GET':
            if not secrets.compare_digest(request.headers.get('X-PM-CSRF', ''), TOKEN):
                return JSONResponse({'detail': '请求校验失败'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
                return JSONResponse({'detail': '不允许跨站写入'}, status_code=403)
            if int(request.headers.get('content-length', '0')) > 120000:
                return JSONResponse({'detail': '资料过大'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.get('/')
def home():
    response = FileResponse(ROOT / 'pm_agent/static/index.html')
    response.set_cookie('pm_session', TOKEN, httponly=True, samesite='strict')
    return response


@app.get('/api/state')
def state():
    from .engine import day
    return {'csrf': TOKEN, 'mode': mode(), 'model': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
            'key_configured': bool(os.getenv('DEEPSEEK_API_KEY')), 'spent': STORE.spent(day()),
            'budget': float(os.getenv('PM_DAILY_BUDGET_USD', '1')), 'items': STORE.list(),
            'approvals': STORE.approvals(), 'audit': STORE.logs()}


def mode():
    value = os.getenv('PM_MODE', 'demo')
    if value not in ('demo', 'live'):
        raise ValueError('PM_MODE 必须为 demo 或 live')
    return value


class ItemInput(BaseModel):
    kind: Literal['source', 'memory']
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=15000)


@app.post('/api/items')
def add_item(data: ItemInput):
    if len(STORE.list(data.kind)) >= 30:
        raise ValueError('此类记录最多 30 条，请先整理旧内容')
    return STORE.add(data.kind, data.title, data.content)


class ApprovalInput(BaseModel):
    target: str
    action: Literal['replace', 'delete']
    content: str = Field(default='', max_length=40000)


@app.post('/api/approvals')
def propose(data: ApprovalInput):
    if data.action == 'replace' and not data.content.strip():
        raise ValueError('替换内容不能为空')
    return {'id': STORE.propose(data.target, data.action, data.content)}


class Decision(BaseModel):
    approve: bool


@app.post('/api/approvals/{aid}')
def decide(aid: str, data: Decision):
    return {'status': STORE.decide(aid, data.approve)}


class RunInput(BaseModel):
    workflow: Literal['opportunity', 'prd']
    topic: str = Field(min_length=2, max_length=4000)
    selected_id: str | None = None


@app.post('/api/runs')
async def run(data: RunInput):
    if any(j['status'] == 'running' for j in JOBS.values()):
        raise HTTPException(409, '已有任务运行中，请稍候')
    selected = ''
    if data.selected_id:
        item = STORE.get(data.selected_id)
        if item['kind'] not in ('opportunity', 'prd'):
            raise ValueError('请选择机会或 PRD 草稿')
        selected = item['content']
    if mode() == 'live' and not os.getenv('DEEPSEEK_API_KEY'):
        raise ValueError('请先在本地 .env 配置 DEEPSEEK_API_KEY，然后重启')
    ident = uuid.uuid4().hex
    if len(JOBS) > 100:
        for old in list(JOBS)[:-50]:
            del JOBS[old]
    JOBS[ident] = {'status': 'running', 'events': [], 'result': None, 'error': None}

    async def work():
        from .engine import generate
        import tempfile
        j = JOBS[ident]
        try:
            # Per-run temporary upstream workspace prevents implicit memory/history retention.
            DATA.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='runtime-', dir=DATA) as path:
                content = await asyncio.wait_for(generate(STORE, Path(path), mode(), data.workflow,
                    data.topic, selected, j['events'].append), timeout=180)
            j['result'] = STORE.add(data.workflow, data.topic[:70], content)
            j['status'] = 'done'
        except Exception as exc:
            j['status'] = 'failed'
            j['error'] = str(exc) if isinstance(exc, ValueError) else '任务执行失败，请检查网络/配置后重试；输入仍保留。'
            STORE.audit('run.failed', {'type': type(exc).__name__})
    task = asyncio.create_task(work())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {'id': ident}


@app.get('/api/runs/{ident}')
def get_run(ident: str):
    if ident not in JOBS:
        raise HTTPException(404, '任务不存在，服务重启会结束未完成任务')
    return JOBS[ident]


@app.get('/api/export/{ident}')
def export(ident: str):
    item = STORE.get(ident)
    return Response(item['content'], media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="pm-{ident[:8]}.md"'})


app.mount('/static', StaticFiles(directory=ROOT / 'pm_agent/static'), name='static')
