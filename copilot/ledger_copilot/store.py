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
CREATE TABLE IF NOT EXISTS arrival(store_id TEXT, period TEXT, kind TEXT, note TEXT, at REAL, seen_at REAL);
CREATE INDEX IF NOT EXISTS arrival_period ON arrival(period, seen_at);
'''


def conn():
    db = getattr(_local, 'db', None)
    if db is None:
        config.ROOT.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.ROOT / 'agent.db', timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        if 'deleted_at' not in {r[1] for r in db.execute('PRAGMA table_info(thread)')}:
            db.execute('ALTER TABLE thread ADD COLUMN deleted_at REAL')
        message_columns = {r[1] for r in db.execute('PRAGMA table_info(message)')}
        for column, kind in (('files', "TEXT DEFAULT '[]'"), ('prompt', 'TEXT'), ('bot', "TEXT DEFAULT ''")):
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


def is_room(thread_id):
    text = thread_id or ''
    return text == GROUP or text.startswith('store:') or text.startswith('month:')


# 页面新开会话时自己生成 12 位十六进制 id，这样第一句还没回来就能切走再切回。
CLIENT_ID = re.compile(r'[0-9a-f]{12}')


def ensure_thread(thread_id, title, user):
    now = time.time()
    with _lock:
        if thread_id and conn().execute('SELECT 1 FROM thread WHERE id=?', (thread_id,)).fetchone():
            # 别人删掉了、这边还开着接着说的，会话回到列表里。
            conn().execute('UPDATE thread SET updated_at=?, deleted_at=NULL WHERE id=?', (now, thread_id))
            conn().commit()
            return thread_id, False
        if thread_id == GROUP:
            title = '公共群聊'
        elif not is_room(thread_id) and not CLIENT_ID.fullmatch(thread_id or ''):
            thread_id = uuid.uuid4().hex[:12]
        conn().execute('INSERT INTO thread(id,title,user,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (thread_id, (title or '新会话')[:80], user, now, now))
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


def threads(limit=200, query=''):
    """所有人的会话，删掉的不列。query 按标题和对话内容找。"""
    sql = 'SELECT id,title,user,updated_at FROM thread WHERE deleted_at IS NULL'
    args = []
    words = [w for w in (query or '').split() if w][:5]
    for word in words:
        like = f'%{word}%'
        sql += ' AND (title LIKE ? OR id IN (SELECT thread_id FROM message WHERE text LIKE ?))'
        args += [like, like]
    rows = conn().execute(sql + ' ORDER BY updated_at DESC LIMIT ?', (*args, limit)).fetchall()
    return [{'id': r['id'], 'title': r['title'], 'user': r['user'],
             'updated_at': datetime.fromtimestamp(r['updated_at']).astimezone().isoformat(timespec='seconds')} for r in rows]


def rename_thread(thread_id, title):
    with _lock:
        conn().execute('UPDATE thread SET title=? WHERE id=?', (title[:80], thread_id))
        conn().commit()


def delete_thread(thread_id, deleted):
    """只从列表里拿掉，对话和确认卡都留着，随时能恢复。"""
    with _lock:
        conn().execute('UPDATE thread SET deleted_at=? WHERE id=?', (time.time() if deleted else None, thread_id))
        conn().commit()


def thread_of_question(question_id):
    rows = conn().execute("SELECT thread_id, receipt FROM message WHERE role='assistant' AND receipt LIKE ? ORDER BY id DESC "
                          'LIMIT 20', (f'%{question_id}%',)).fetchall()
    for r in rows:
        if str(json.loads(r['receipt']).get('question_id') or '') == str(question_id):
            return r['thread_id']
    return ''


def add_arrival(store_id, period, kind, note):
    with _lock:
        conn().execute('INSERT INTO arrival VALUES(?,?,?,?,?,NULL)', (store_id, period, kind, note[:200], time.time()))
        conn().commit()


def arrivals(period):
    """还没人看过的到达，按店汇总。"""
    rows = conn().execute('SELECT store_id, kind, count(*) n, max(at) last FROM arrival WHERE period=? AND seen_at IS NULL '
                          'GROUP BY store_id, kind ORDER BY last DESC', (period,)).fetchall()
    out = {}
    for r in rows:
        item = out.setdefault(r['store_id'], {'store_id': r['store_id'], 'period': period, 'materials': 0, 'orders': 0,
                                              'last': datetime.fromtimestamp(r['last']).astimezone().isoformat(timespec='seconds')})
        item['orders' if r['kind'] == 'orders' else 'materials'] += r['n']
    return list(out.values())


def see_arrivals(store_id, period):
    with _lock:
        conn().execute('UPDATE arrival SET seen_at=? WHERE store_id=? AND period=? AND seen_at IS NULL',
                       (time.time(), store_id, period))
        conn().commit()


def add_message(thread_id, role, text, *, images=(), steps=(), cards=(), receipt=None, page=None, user='', files=(),
                prompt=None, bot=''):
    with _lock:
        cur = conn().execute(
            'INSERT INTO message(thread_id,role,text,images,steps,cards,receipt,page,user,at,files,prompt,bot) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (thread_id, role, text, _j(list(images)), _j(list(steps)), _j(list(cards)),
             _j(receipt) if receipt else None, _j(page) if page else None, user, time.time(), _j(list(files)), prompt,
             bot or ''))
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
                    'bot': r['bot'] or '', 'files': json.loads(r['files'] or '[]'),
                    **({'prompt': r['prompt']} if prompts else {})})
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


def card_remaining(card_id, actions, message):
    with _lock:
        conn().execute('UPDATE card SET actions=?, message=? WHERE id=?', (_j(actions), message, card_id))
        conn().commit()


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
