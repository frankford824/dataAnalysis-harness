"""对话、确认卡片、纠错记录、长期记忆和用量，全部存在生产机本地。"""
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from . import config

_local = threading.local()
_lock = threading.Lock()
MEMORY_LIMIT = 8000

SCHEMA = '''
CREATE TABLE IF NOT EXISTS thread(id TEXT PRIMARY KEY, title TEXT, user TEXT, created_at REAL, updated_at REAL,
    summary TEXT DEFAULT '', summarized_upto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS message(id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, role TEXT, text TEXT,
    images TEXT DEFAULT '[]', steps TEXT DEFAULT '[]', cards TEXT DEFAULT '[]', receipt TEXT, page TEXT, user TEXT, at REAL);
CREATE INDEX IF NOT EXISTS message_thread ON message(thread_id, id);
CREATE TABLE IF NOT EXISTS card(id TEXT PRIMARY KEY, thread_id TEXT, title TEXT, summary TEXT, items TEXT, actions TEXT,
    status TEXT, message TEXT, created_at REAL, decided_at REAL, decided_by TEXT);
CREATE TABLE IF NOT EXISTS correction(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, thread_id TEXT,
    problem TEXT, cause TEXT, fix TEXT, scope TEXT);
CREATE TABLE IF NOT EXISTS usage(at REAL, month TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL);
CREATE INDEX IF NOT EXISTS usage_month ON usage(month);
CREATE TABLE IF NOT EXISTS event(at REAL, kind TEXT, body TEXT);
'''


def conn():
    db = getattr(_local, 'db', None)
    if db is None:
        config.ROOT.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.ROOT / 'agent.db', timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        message_columns = {r[1] for r in db.execute('PRAGMA table_info(message)')}
        for column, kind in (('files', "TEXT DEFAULT '[]'"), ('prompt', 'TEXT')):
            if column not in message_columns:
                db.execute(f'ALTER TABLE message ADD COLUMN {column} {kind}')
        columns = {r[1] for r in db.execute('PRAGMA table_info(usage)')}
        for column in ('cached', 'written', 'explicit'):
            if column not in columns:
                db.execute(f'ALTER TABLE usage ADD COLUMN {column} INTEGER DEFAULT 0')
        _local.db = db
    return db


def _j(value):
    return json.dumps(value, ensure_ascii=False)


def month():
    return datetime.now().strftime('%Y-%m')


def add_usage(model, tokens_in, tokens_out, cost, cached=0, written=0, explicit=False):
    with _lock:
        conn().execute('INSERT INTO usage(at,month,model,tokens_in,tokens_out,cost,cached,written,explicit) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (time.time(), month(), model, tokens_in, tokens_out, cost, cached, written, int(bool(explicit))))
        conn().commit()


def log_event(kind, body):
    with _lock:
        conn().execute('INSERT INTO event VALUES(?,?,?)', (time.time(), kind, _j(body)))
        conn().commit()


def events(kind, limit=20):
    rows = conn().execute('SELECT at, body FROM event WHERE kind=? ORDER BY rowid DESC LIMIT ?', (kind, limit)).fetchall()
    return [{'at': datetime.fromtimestamp(r['at']).strftime('%Y-%m-%d %H:%M'), **json.loads(r['body'])} for r in rows]


GROUP = 'group'


def ensure_thread(thread_id, title, user):
    now = time.time()
    with _lock:
        if thread_id and conn().execute('SELECT 1 FROM thread WHERE id=?', (thread_id,)).fetchone():
            conn().execute('UPDATE thread SET updated_at=? WHERE id=?', (now, thread_id))
            conn().commit()
            return thread_id, False
        if thread_id == GROUP:
            title = '公共群聊'
        else:
            thread_id = uuid.uuid4().hex[:12]
        conn().execute('INSERT INTO thread(id,title,user,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (thread_id, title[:40] or '新话题', user, now, now))
        conn().commit()
        return thread_id, True


def thread(thread_id):
    return conn().execute('SELECT * FROM thread WHERE id=?', (thread_id,)).fetchone()


def unanswered(since, until):
    """最后一条是用户提问、还没有回答的话题（服务重启打断的那一轮）。"""
    rows = conn().execute(
        'SELECT m.* FROM message m JOIN (SELECT thread_id, max(id) id FROM message GROUP BY thread_id) last '
        "ON m.id = last.id WHERE m.role = 'user' AND m.at >= ? AND m.at < ?", (since, until)).fetchall()
    return [{'id': r['id'], 'thread_id': r['thread_id'], 'text': r['text'], 'user': r['user'],
             'page': json.loads(r['page']) if r['page'] else None, 'files': json.loads(r['files'] or '[]')} for r in rows]


def threads(limit=50):
    ensure_thread(GROUP, '', '') if not thread(GROUP) else None
    rows = conn().execute('SELECT id,title,user,updated_at FROM thread ORDER BY id=? DESC, updated_at DESC LIMIT ?',
                          (GROUP, limit)).fetchall()
    return [{'id': r['id'], 'title': r['title'], 'user': r['user'],
             'updated_at': datetime.fromtimestamp(r['updated_at']).astimezone().isoformat(timespec='seconds')} for r in rows]


def add_message(thread_id, role, text, *, images=(), steps=(), cards=(), receipt=None, page=None, user='', files=(),
                prompt=None):
    with _lock:
        cur = conn().execute(
            'INSERT INTO message(thread_id,role,text,images,steps,cards,receipt,page,user,at,files,prompt) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (thread_id, role, text, _j(list(images)), _j(list(steps)), _j(list(cards)),
             _j(receipt) if receipt else None, _j(page) if page else None, user, time.time(), _j(list(files)), prompt))
        conn().commit()
        return cur.lastrowid


def set_prompt(message_id, prompt):
    with _lock:
        conn().execute('UPDATE message SET prompt=? WHERE id=?', (prompt, message_id))
        conn().commit()


def messages(thread_id, after=0, last=None, prompts=False):
    rows = conn().execute('SELECT * FROM message WHERE thread_id=? AND id>? ORDER BY id', (thread_id, after)).fetchall()
    if last:
        rows = rows[-last:]
    out = []
    for r in rows:
        cards = [card_view(c) for c in json.loads(r['cards'])]
        out.append({'id': r['id'], 'role': r['role'], 'text': r['text'], 'images': json.loads(r['images']),
                    'steps': json.loads(r['steps']), 'cards': [c for c in cards if c],
                    'receipt': json.loads(r['receipt']) if r['receipt'] else None,
                    'at': datetime.fromtimestamp(r['at']).astimezone().isoformat(timespec='seconds'), 'user': r['user'],
                    'files': json.loads(r['files'] or '[]'), **({'prompt': r['prompt']} if prompts else {})})
    return out


def set_summary(thread_id, summary, upto):
    with _lock:
        conn().execute('UPDATE thread SET summary=?, summarized_upto=? WHERE id=?', (summary, upto, thread_id))
        conn().commit()


def add_card(thread_id, title, summary, items, actions):
    card_id = uuid.uuid4().hex[:12]
    with _lock:
        conn().execute('INSERT INTO card VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (card_id, thread_id, title, summary, _j(items), _j(actions), 'pending', '', time.time(), None, None))
        conn().commit()
    return card_id


def card(card_id):
    return conn().execute('SELECT * FROM card WHERE id=?', (card_id,)).fetchone()


def card_view(card_id):
    row = card(card_id)
    if not row:
        return None
    return {'card_id': row['id'], 'title': row['title'], 'summary': row['summary'], 'items': json.loads(row['items']),
            'status': row['status'], 'message': row['message']}


def decide_card(card_id, status, message, by):
    with _lock:
        conn().execute('UPDATE card SET status=?, message=?, decided_at=?, decided_by=? WHERE id=?',
                       (status, message, time.time(), by, card_id))
        conn().commit()


def add_correction(thread_id, problem, cause, fix, scope):
    with _lock:
        conn().execute('INSERT INTO correction(at,thread_id,problem,cause,fix,scope) VALUES(?,?,?,?,?,?)',
                       (time.time(), thread_id, problem, cause, fix, scope))
        conn().commit()


def _terms(text):
    words = re.findall(r'[A-Za-z0-9_\-]{2,}', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        words += [run[i:i + 2] for i in range(max(1, len(run) - 1))]
    return {w.lower() for w in words}


def search_corrections(query, limit=5):
    terms = _terms(query)
    if not terms:
        return []
    rows = conn().execute('SELECT * FROM correction ORDER BY id DESC LIMIT 5000').fetchall()
    scored = []
    for r in rows:
        body = f"{r['problem']} {r['cause']} {r['fix']} {r['scope']}".lower()
        score = sum(1 for t in terms if t in body)
        if score:
            scored.append((score, r['id'], r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [{'时间': datetime.fromtimestamp(r['at']).strftime('%Y-%m-%d'), '问题': r['problem'], '原因': r['cause'],
             '处理': r['fix'], '范围': r['scope']} for _, _, r in scored[:limit]]


def memory_path():
    return config.ROOT / 'memory.md'


def memory():
    path = memory_path()
    return path.read_text(encoding='utf-8') if path.exists() else ''


def write_memory(text):
    config.ROOT.mkdir(parents=True, exist_ok=True)
    path = memory_path()
    if path.exists():
        history = config.ROOT / 'memory-history'
        history.mkdir(exist_ok=True)
        (history / f'{datetime.now():%Y%m%d-%H%M%S}.md').write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    path.write_text(text, encoding='utf-8')
