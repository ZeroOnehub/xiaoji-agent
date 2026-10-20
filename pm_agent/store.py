"""Transactional local storage. Model tools cannot mutate this database."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS items (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
              content TEXT NOT NULL, version INTEGER DEFAULT 1, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, target TEXT, version INTEGER, action TEXT,
              content TEXT, status TEXT DEFAULT 'pending', created REAL);
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, event TEXT, detail TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, reserved REAL DEFAULT 0);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, kind, title, content):
        item = dict(id=uuid.uuid4().hex, kind=kind, title=title, content=content,
                    version=1, created=time.time())
        with self.db() as db:
            db.execute('INSERT INTO items VALUES (:id,:kind,:title,:content,:version,:created)', item)
            self._audit(db, 'created', {'id': item['id'], 'kind': kind})
        return item

    def get(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('记录不存在或已删除')
        return dict(row)

    def list(self, kind=None):
        with self.db() as db:
            rows = db.execute('SELECT * FROM items WHERE kind=? ORDER BY created DESC', (kind,)) if kind else db.execute('SELECT * FROM items ORDER BY created DESC')
            return [dict(r) for r in rows]

    def propose(self, ident, action, content=''):
        if action not in ('delete', 'replace'):
            raise ValueError('不支持的操作')
        with self.db() as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('记录不存在')
            if row['kind'] not in ('memory', 'opportunity', 'prd', 'source'):
                raise ValueError('不允许修改此记录')
            aid = uuid.uuid4().hex
            db.execute('INSERT INTO approvals(id,target,version,action,content,created) VALUES (?,?,?,?,?,?)',
                       (aid, ident, row['version'], action, content, time.time()))
            self._audit(db, 'approval.requested', {'id': aid, 'target': ident, 'action': action})
        return aid

    def decide(self, aid, approve):
        with self.db() as db:
            a = db.execute('SELECT * FROM approvals WHERE id=?', (aid,)).fetchone()
            if not a or a['status'] != 'pending':
                raise ValueError('审批不存在或已经处理')
            row = db.execute('SELECT * FROM items WHERE id=?', (a['target'],)).fetchone()
            status = 'rejected'
            if approve:
                if time.time() - a['created'] > 900:
                    status = 'expired'
                elif not row or row['version'] != a['version']:
                    status = 'stale'
                else:
                    if a['action'] == 'delete':
                        db.execute('DELETE FROM items WHERE id=?', (a['target'],))
                    else:
                        db.execute('UPDATE items SET content=?,version=version+1 WHERE id=?', (a['content'], a['target']))
                    status = 'succeeded'
            db.execute('UPDATE approvals SET status=? WHERE id=?', (status, aid))
            self._audit(db, 'approval.' + status, {'id': aid, 'target': a['target']})
        return status

    def approvals(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT a.*,i.title,i.content AS original FROM approvals a LEFT JOIN items i ON a.target=i.id WHERE status="pending" ORDER BY a.created DESC')]

    @staticmethod
    def _audit(db, event, detail):
        db.execute('INSERT INTO audit(event,detail,created) VALUES (?,?,?)', (event, json.dumps(detail, ensure_ascii=False), time.time()))

    def audit(self, event, detail):
        with self.db() as db:
            self._audit(db, event, detail)

    def logs(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]

    def reserve(self, day, amount, limit):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO budget(day) VALUES (?)', (day,))
            used = db.execute('SELECT reserved FROM budget WHERE day=?', (day,)).fetchone()[0]
            if used + amount > limit:
                raise ValueError('今日预算不足，已停止模型请求')
            db.execute('UPDATE budget SET reserved=reserved+? WHERE day=?', (amount, day))

    def settle(self, day, reserved, actual):
        with self.db() as db:
            db.execute('UPDATE budget SET reserved=MAX(0,reserved+?) WHERE day=?', (actual-reserved, day))

    def spent(self, day):
        with self.db() as db:
            r = db.execute('SELECT reserved FROM budget WHERE day=?', (day,)).fetchone()
            return r[0] if r else 0
