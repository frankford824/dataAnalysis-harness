"""助手可调用的工具。每个工具都带 purpose：给用户看的一句话进度。"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config, llm, store

LIMIT = 6000
SKIP_DIRS = {'node_modules', '.git', '__pycache__', 'venv', '.venv', 'static', 'dist'}
_openapi = {}


def _cut(text, limit=LIMIT):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f'\n…（已截断，共 {len(text)} 字符）'


def _http(method, path, body=None, timeout=180, base=None):
    url = (base or config.LEDGER_URL) + (path if path.startswith('/') else '/' + path)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        if exc.code == 503 and base is None and config.LEDGER_URL != config.LEDGER_DIRECT:
            return _http(method, path, body, timeout, config.LEDGER_DIRECT)
        return exc.code, exc.read().decode('utf-8', 'replace')
    except urllib.error.URLError as exc:
        if base is None and config.LEDGER_URL != config.LEDGER_DIRECT and isinstance(exc.reason, ConnectionError):
            return _http(method, path, body, timeout, config.LEDGER_DIRECT)
        raise


def ledger_get(path, query=None, **_):
    if query:
        path += ('&' if '?' in path else '?') + urllib.parse.urlencode(query, doseq=True)
    try:
        status, text = _http('GET', path, timeout=90)
    except (TimeoutError, OSError) as exc:
        if 'timed out' not in str(exc):
            raise
        return '这个接口 90 秒内没有返回（数据量大）。改用 sql_query 直接查对应的表，或换更具体的接口。'
    return _cut(f'HTTP {status}\n{text}')


def ledger_preview(path, body, **_):
    if 'preview' not in path:
        return '只能直接调用预览接口；会改变数据的接口请用 propose_change 交给用户确认。'
    status, text = _http('POST', path, body, timeout=1800)
    return _cut(f'HTTP {status}\n{text}')


def api_index(keyword='', **_):
    if not _openapi:
        _openapi.update(json.loads(_http('GET', '/openapi.json')[1]))
    rows = []
    for path, methods in _openapi.get('paths', {}).items():
        for method, spec in methods.items():
            line = f"{method.upper()} {path}  {spec.get('summary', '')}"
            if not keyword or keyword.lower() in line.lower():
                rows.append(line)
    return _cut('\n'.join(rows[:150]) or '没有匹配的接口')


def sql_query(sql, db='workspace', **_):
    if not re.match(r'\s*(select|with|pragma|explain)\b', sql, re.I):
        return '只允许只读查询（SELECT/WITH/PRAGMA/EXPLAIN）。'
    path = config.DATABASES.get(db)
    if not path:
        return f'未知数据库 {db}，可选：{", ".join(config.DATABASES)}'
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
    deadline = time.time() + 60
    connection.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    try:
        cursor = connection.execute(sql)
        columns = [c[0] for c in cursor.description or []]
        rows = cursor.fetchmany(200)
        return _cut({'columns': columns, 'rows': [list(r) for r in rows], 'truncated': len(rows) == 200})
    except sqlite3.OperationalError as exc:
        return f'查询失败：{exc}（超过 60 秒会被中止；大表必须按店铺/账期过滤并 LIMIT）'
    finally:
        connection.close()


def read_file(path, offset=1, limit=300, **_):
    file = Path(path)
    if not file.is_file():
        return f'文件不存在：{path}'
    if file.stat().st_size > 20_000_000:
        return '文件太大，请用 powershell 按需读取片段。'
    lines = file.read_text(encoding='utf-8', errors='replace').splitlines()
    start = max(1, int(offset))
    chunk = lines[start - 1:start - 1 + int(limit)]
    return _cut('\n'.join(f'{start + i:>6}|{line}' for i, line in enumerate(chunk)) + f'\n（共 {len(lines)} 行）')


def search_files(pattern, path=None, glob='*', **_):
    root = Path(path or config.APP_DIR)
    regex = re.compile(pattern)
    hits = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            file = Path(directory) / name
            if not file.match(glob) or file.stat().st_size > 2_000_000:
                continue
            try:
                for number, line in enumerate(file.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
                    if regex.search(line):
                        hits.append(f'{file}:{number}: {line.strip()[:200]}')
                        if len(hits) >= 200:
                            return _cut('\n'.join(hits))
            except OSError:
                continue
    return _cut('\n'.join(hits) or '没有找到')


def _backup(file):
    batch = config.ROOT / 'code-history' / _batch()
    target = batch / re.sub(r'[:\\/]+', '_', str(file.resolve()))
    batch.mkdir(parents=True, exist_ok=True)
    manifest = batch / 'manifest.json'
    entries = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
    if not any(e['path'] == str(file.resolve()) for e in entries):
        existed = file.exists()
        if existed:
            shutil.copy2(file, target)
        entries.append({'path': str(file.resolve()), 'backup': str(target) if existed else None})
        manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding='utf-8')


_current_batch = []


def _batch():
    if not _current_batch:
        _current_batch.append(datetime.now().strftime('%Y%m%d-%H%M%S'))
    return _current_batch[0]


def write_file(path, content, **_):
    file = Path(path)
    _backup(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding='utf-8')
    return f'已写入 {path}（{len(content)} 字符）。程序文件改动需要 restart_ledger 才生效。'


def edit_file(path, old, new, **_):
    file = Path(path)
    text = file.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        return f'原文出现 {count} 次，必须恰好 1 次；请提供更长的上下文。'
    _backup(file)
    file.write_text(text.replace(old, new), encoding='utf-8')
    return '已修改。程序文件改动需要 restart_ledger 才生效。'


def powershell(command, timeout=60, **_):
    timeout = min(int(timeout or 60), 120)
    script = '[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ProgressPreference="SilentlyContinue";' + command
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=int(timeout))
    except subprocess.TimeoutExpired:
        return f'命令超过 {timeout} 秒未结束，已中止。'
    out = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')
    return _cut(f'exit={result.returncode}\n{out}')


PRELUDE = '''import json, sqlite3, urllib.request, urllib.parse
LEDGER = {ledger!r}
DATABASES = {databases!r}
def get(path, **query):
    if query: path += ('&' if '?' in path else '?') + urllib.parse.urlencode(query, doseq=True)
    with urllib.request.urlopen(LEDGER + path, timeout=60) as r: return json.loads(r.read().decode('utf-8'))
def order_get(path, **query):
    if query: path += ('&' if '?' in path else '?') + urllib.parse.urlencode(query, doseq=True)
    with urllib.request.urlopen('http://127.0.0.1:8001' + path, timeout=30) as r: return json.loads(r.read().decode('utf-8'))
def sql(query, db='workspace', params=()):
    c = sqlite3.connect('file:' + DATABASES[db] + '?mode=ro', uri=True, timeout=10); c.row_factory = sqlite3.Row
    try: return [dict(r) for r in c.execute(query, params).fetchall()]
    finally: c.close()
'''


def python(code, timeout=60, **_):
    timeout = min(int(timeout or 60), 60)
    import sys
    import tempfile
    prelude = PRELUDE.format(ledger=config.LEDGER_URL, databases={k: str(v) for k, v in config.DATABASES.items()})
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8') as handle:
        handle.write(prelude + '\n' + code)
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
    try:
        result = subprocess.run([sys.executable, handle.name], capture_output=True, timeout=int(timeout), env=env)
    except subprocess.TimeoutExpired:
        return f'运行超过 {timeout} 秒，已中止。这个取法太慢，换更直接的接口或表，或者缩小范围（一家店、一个月）再算。'
    finally:
        os.unlink(handle.name)
    out = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')[-3000:]
    return _cut(f'exit={result.returncode}\n{out}')


def _closed_samples():
    try:
        connection = sqlite3.connect(f"file:{config.DATABASES['workspace']}?mode=ro", uri=True, timeout=10)
        rows = connection.execute("SELECT store_id, period FROM period WHERE state='closed' ORDER BY changed_at DESC LIMIT 3").fetchall()
        connection.close()
    except sqlite3.Error:
        return {}
    samples = {}
    for store_id, period in rows:
        status, text = _http('GET', f'/api/stores/{urllib.parse.quote(store_id)}/periods/{period}', timeout=120)
        if status == 200:
            samples[(store_id, period)] = _money(json.loads(text))
    return samples


def _money(value, prefix=''):
    out = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'stale', 'shown_at', 'changed_at', 'at', 'generated_at', 'elapsed_ms'}:
                continue
            out.update(_money(item, f'{prefix}.{key}'))
    elif isinstance(value, list):
        for index, item in enumerate(value[:200]):
            out.update(_money(item, f'{prefix}[{index}]'))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix] = round(float(value), 2)
    return out


def _restart_service():
    powershell("Get-NetTCPConnection -LocalAddress 127.0.0.1,0.0.0.0 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue"
               " | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }", 60)
    deadline = time.time() + 240
    time.sleep(5)
    while time.time() < deadline:
        try:
            status, text = _http('GET', '/api/version', timeout=10)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(3)
    return False


def _rollback(batch):
    entries = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    for entry in entries:
        file = Path(entry['path'])
        if entry['backup']:
            shutil.copy2(entry['backup'], file)
        elif file.exists():
            file.unlink()


def restart_ledger(**_):
    batch = config.ROOT / 'code-history' / _current_batch[0] if _current_batch else None
    before = _closed_samples()
    healthy = _restart_service()
    after = _closed_samples() if healthy else {}
    changed = [f'{s} {p}' for (s, p), money in before.items() if (s, p) in after and after[(s, p)] != money]
    if healthy and not changed:
        _current_batch.clear()
        store.log_event('code_change', {'batch': batch.name if batch else '', 'result': 'ok'})
        from . import events
        events.publish('data_updated', {})
        return '台账服务已重启并恢复正常；已结账月份抽查结果一致。'
    reason = '服务没有正常启动' if not healthy else '已结账结果出现变化：' + '、'.join(changed)
    if batch and batch.exists():
        _rollback(batch)
        _current_batch.clear()
        restored = _restart_service()
        store.log_event('code_change', {'batch': batch.name, 'result': 'rolled_back', 'reason': reason})
        return f'{reason}，已自动恢复到修改前的程序（恢复后{"正常" if restored else "仍异常，需要继续排查"}）。'
    return reason


def build_web(summary, **_):
    from . import webui
    outcome = webui.build(summary, _current_batch[0] if _current_batch else None)
    if outcome['ok']:
        _current_batch.clear()
    return outcome['message']


def web_search(query, **_):
    answer, _, _ = llm.complete(config.models()['quick']['id'], [
        {'role': 'system', 'content': '根据联网搜索结果回答，300 字以内，只写查到的事实，注明信息来自哪个网站和日期；查不到就直说。'},
        {'role': 'user', 'content': query}], max_tokens=600, timeout=90,
        extra={'enable_search': True, 'search_options': {'forced_search': True, 'search_strategy': 'turbo'}})
    return _cut(answer)


def machine_map(section='', **_):
    from . import machine
    return machine.machine_map(section)


def find_files(keywords, limit=30, **_):
    from . import machine
    return machine.find_files(keywords, limit)


KNOWLEDGE = config.SELF_DIR / 'knowledge'


def knowledge_index():
    index = KNOWLEDGE / '00-index.md'
    return index.read_text(encoding='utf-8') if index.exists() else ''


def read_knowledge(section, **_):
    matches = sorted(KNOWLEDGE.glob(f'*{section}*.md')) if section else []
    if not matches:
        return '没有这个章节。可选：' + '、'.join(p.name for p in sorted(KNOWLEDGE.glob('*.md')))
    return _cut(matches[0].read_text(encoding='utf-8'), 8000)


def restart_assistant(**_):
    _pending_self_restart.append(True)
    return '助手会在本次回答结束后自动重启以加载新代码。'


_pending_self_restart = []


def remember(note, **_):
    current = store.memory()
    stamp = datetime.now().strftime('%Y-%m-%d')
    store.write_memory((current.rstrip() + f'\n- [{stamp}] {note.strip()}\n').lstrip())
    if len(store.memory()) > store.MEMORY_LIMIT:
        from .agent import compact_memory
        compact_memory()
    return '已记住。'


def record_correction(problem, cause, fix, scope='', thread_id='', **_):
    store.add_correction(thread_id, problem, cause, fix, scope)
    return '已记录本次纠错。'


def search_memory(query, **_):
    rows = store.search_corrections(query, 8)
    return _cut(rows) if rows else '没有找到相关的历史纠错记录。'


def auto_close_status(**_):
    return _cut(store.events('auto_close', 30) or '自动结账还没有运行记录。')


def run_auto_close(periods=None, **_):
    from . import autoclose
    return _cut(autoclose.run(periods or None))


ORDER_URL = 'http://127.0.0.1:8001'
METRICS = {
    'net_income': '销售收入（到账）', 'receipts': '对账销售收入（未扣退款）', 'refunds': '销售退款', 'refund_rate': '退款率',
    'goods_cost': '商品成本', 'goods_rate': '货本率', 'gross_profit': '商品毛利', 'gross_margin': '毛利率',
    'promotion_cost': '推广费用', 'ad_cost': '平台推广', 'brushing_cost': '补单成本', 'promotion_rate': '推广费用占比',
    'platform_cost': '平台费用', 'freight_cost': '发货运费', 'freight_rate': '发货运费占比', 'aftersales_cost': '客服售后费用',
    'profit': '利润（扣兼职前）', 'profit_margin': '利润率', 'labor_cost': '兼职人工费用', 'labor_rate': '兼职人工费用占比',
    'profit_after_labor': '利润额（扣兼职后）', 'after_labor_margin': '利润额率',
}
RATE_METRICS = {'refund_rate', 'goods_rate', 'gross_margin', 'promotion_rate', 'freight_rate', 'profit_margin', 'labor_rate',
                'after_labor_margin'}
_METRIC_WORDS = {'退款': 'refunds', '收入': 'net_income', '销售额': 'net_income', '毛利': 'gross_profit', '推广': 'promotion_cost',
                 '运费': 'freight_cost', '成本': 'goods_cost', '利润': 'profit_after_labor', '兼职': 'labor_cost',
                 '售后': 'aftersales_cost', '平台费': 'platform_cost', '补单': 'brushing_cost'}


def _metric_key(metric):
    metric = (metric or '').strip()
    if metric in METRICS:
        return metric
    for key, label in METRICS.items():
        if metric and (metric == label or metric in label.split('（')[0]):
            return key
    return next((key for word, key in _METRIC_WORDS.items() if word in metric), None)


def _json_get(url, timeout=90):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def _fmt(value, key):
    if value is None:
        return '—（还没算出）'
    return f'{float(value) * 100:.2f}%' if key in RATE_METRICS else str(value)


def store_metrics(month, metric='', store='', top=10, ascending=False, platform='', compare=False, **_):
    key = _metric_key(metric)
    if not store and not key:
        return '请给 metric（可选：' + '、'.join(METRICS.values()) + '）或 store。'
    query = {'period': month, 'level': 'store', 'compare': 'true' if compare else 'false', 'limit': 200}
    if platform:
        query['platform'] = platform
    try:
        data = _json_get(config.LEDGER_URL + '/api/operations/month?' + urllib.parse.urlencode(query), timeout=120)
    except urllib.error.HTTPError as exc:
        return f'经营看板接口报错：{_friendly_error(exc.read().decode("utf-8", "replace"))}'
    rows = data.get('rows') or []
    head = f'{month} 经营看板（口径 {data.get("version")}，{"已结账终版" if data.get("is_final") else "阶段结果，未结账的店还会变"}），共 {len(rows)} 家店。'
    if store:
        words = store.split()
        hits = [r for r in rows if all(w in (r.get('name') or '') + ' ' + (r.get('store_id') or '') for w in words)]
        if not hits:
            return head + f'\n没有名称或编号包含「{store}」的店铺。'
        lines = [head]
        for r in hits[:5]:
            m = r.get('metrics') or {}
            lines.append(f'\n【{r["name"]}】store_id={r["store_id"]} 平台={r.get("platform")}')
            lines += [f'  {label}：{_fmt(m.get(k), k)}' + (_change(r, k) if compare else '') for k, label in METRICS.items()]
        return _cut('\n'.join(lines), 12000)
    label = METRICS[key]
    known = [r for r in rows if (r.get('metrics') or {}).get(key) is not None]
    missing = [r['name'] for r in rows if (r.get('metrics') or {}).get(key) is None]
    known.sort(key=lambda r: float(r['metrics'][key]), reverse=not ascending)
    lines = [head, f'按「{label}」{"从低到高" if ascending else "从高到低"}：']
    for index, r in enumerate(known[:max(1, min(int(top or 10), 60))], 1):
        lines.append(f'{index}. {r["name"]}（{r["store_id"]}）：{_fmt(r["metrics"][key], key)}' + (_change(r, key) if compare else ''))
    if key not in RATE_METRICS and known:
        total = sum(float(r['metrics'][key]) for r in known)
        lines.append(f'已算出的 {len(known)} 家合计：{total:.2f}')
    if missing:
        lines.append(f'还没算出这一项的 {len(missing)} 家（不在排名里，合计不含它们）：' + '、'.join(missing[:15]) + ('…' if len(missing) > 15 else ''))
    return _cut('\n'.join(lines), 12000)


def _change(row, key):
    out = []
    for name, word in (('mom', '环比'), ('yoy', '同比')):
        change = (row.get(name) or {}).get(key) or {}
        if change.get('state') != 'comparable':
            continue
        if key in RATE_METRICS:
            out.append(f'{word} {float(change["delta"]) * 100:+.2f} 个百分点')
        else:
            out.append(f'{word} {change.get("delta")}' + (f'（{float(change["rate"]) * 100:+.1f}%）' if change.get('rate') is not None else ''))
    return ('；' + '，'.join(out)) if out else ''


def sku_cost(sku, **_):
    try:
        found = _json_get(ORDER_URL + '/api/sku-cost/search?' + urllib.parse.urlencode({'q': sku}), timeout=30).get('rows') or []
    except (OSError, ValueError) as exc:
        return f'订单系统没有响应：{exc}'
    if not found:
        return f'订单系统的成本库里没有「{sku}」，也就是没有成本价记录。'
    lines = []
    for row in found[:8]:
        lines.append(f'{row.get("skuId")} {row.get("name")}：当前成本 {row.get("cost") if row.get("cost") is not None else "无"}'
                     + (f'（{row["costNote"]}）' if row.get('costNote') else '') + f'，调价 {row.get("changes") or 0} 次')
    if len(found) == 1 and found[0].get('changes'):
        try:
            history = _json_get(ORDER_URL + f'/api/sku-cost/{urllib.parse.quote(found[0]["skuId"])}/history', timeout=30)
            lines.append('调价历史：' + _cut(history, 3000))
        except (OSError, ValueError):
            pass
    return '\n'.join(lines)


FEED_ROOT = Path(os.environ.get('LEDGER_ORDER_FEED_ROOT', r'D:\order\exchange\ledger-feed'))
FEED_KEYS = {'orders': ('order_id', 'online_order_no'),
             'order_items': ('order_id', 'sub_order_id', 'online_order_no', 'outer_sku'),
             'after_sales': ('after_sale_id', 'internal_after_sale_id', 'platform_after_sale_id', 'order_id', 'online_order_no')}
FEED_SHOW = {'orders': ('order_id', 'online_order_no', 'shop_name', 'order_status_text', 'order_time', 'pay_time', 'ship_time',
                        'paid_amount', 'refund_amount', 'order_type', 'is_split', 'is_merge', 'order_remark'),
             'order_items': ('order_id', 'outer_sku', 'sku_id', 'product_name', 'quantity', 'paid_amount', 'refund_amount', 'item_status_raw'),
             'order_costs': ('order_id', 'sku_id', 'quantity', 'unit_cost', 'cost_amount', 'cost_status', 'cost_as_of'),
             'after_sales': ('after_sale_id', 'platform_after_sale_id', 'order_id', 'online_order_no', 'after_sale_type_raw',
                             'goods_status_raw', 'refund_status_raw', 'refund_amount', 'created_at_source', 'order_side_decision_reason'),
             'after_sale_items': ('after_sale_id', 'order_id', 'sub_order_id', 'outer_sku', 'sku_id', 'merchant_sku', 'quantity',
                                  'refund_amount')}


def _after_items(after, items):
    """Each after-sale's returned lines matched to the order lines, plus refund vs paid."""
    rows = _feed('after_sale_items', ('after_sale_id',), {str(r['after_sale_id']) for r in after if r.get('after_sale_id')})
    by_line = {(str(i.get('order_id')), str(i.get('sub_order_id'))): i for i in items}
    for row in rows:
        line = by_line.get((str(row.get('order_id')), str(row.get('sub_order_id'))))
        if line:
            row.setdefault('outer_sku', line.get('outer_sku'))
            if not row.get('sku_id'):
                row['sku_id'] = line.get('sku_id')
    notes = []
    for sale in after:
        paid = [float(i['paid_amount']) for i in items
                if str(i.get('order_id')) == str(sale.get('order_id')) and i.get('paid_amount') not in (None, '')]
        try:
            refund = float(sale.get('refund_amount'))
        except (TypeError, ValueError):
            continue
        if paid:
            whole = '按金额看是整单退' if refund >= sum(paid) - 0.005 else '按金额看只退了一部分'
            notes.append(f'售后单 {sale.get("after_sale_id")}：退款 {refund:.2f}，内部单 {sale.get("order_id")} '
                         f'商品实付合计 {sum(paid):.2f}（{len(paid)} 行），{whole}')
    return rows, notes


def _feed(name, keys, values):
    import polars as pl
    manifest = json.loads((FEED_ROOT / 'current' / 'manifest.json').read_text(encoding='utf-8'))
    scan = pl.scan_parquet(FEED_ROOT / manifest['objects'][f'{name}.parquet']['path'])
    cols = scan.collect_schema().names()
    keys = [c for c in keys if c in cols]
    if not keys or not values:
        return []
    found = scan.filter(pl.any_horizontal([pl.col(c).cast(pl.Utf8).is_in(sorted(values)) for c in keys])).head(200).collect()
    return [{k: v for k, v in row.items() if v not in (None, '')} for row in found.iter_rows(named=True)]


def _rows(title, rows, name):
    if not rows:
        return [f'{title}：没有']
    show = FEED_SHOW.get(name)
    return [f'{title}（{len(rows)} 行）：'] + ['- ' + '，'.join(f'{k}={str(row[k])[:80]}' for k in (show or row) if k in row) for row in rows[:30]]


def order_lookup(order_id, **_):
    key = str(order_id).strip()
    lines = []
    try:
        after = _feed('after_sales', FEED_KEYS['after_sales'], {key})
        items = _feed('order_items', FEED_KEYS['order_items'], {key})
        orders = _feed('orders', FEED_KEYS['orders'], {key})
        internal = {str(r['order_id']) for r in [*after, *items, *orders] if r.get('order_id')}
        online = {str(r['online_order_no']).rpartition(':')[2] for r in [*after, *items, *orders] if r.get('online_order_no')}
        orders = _feed('orders', ('order_id', 'online_order_no'), internal | online)
        internal |= {str(r['order_id']) for r in orders}
        online |= {str(r['online_order_no']) for r in orders if r.get('online_order_no')}
        items = _feed('order_items', ('order_id',), internal)
        costs = _feed('order_costs', ('order_id',), internal)
        after = _feed('after_sales', ('order_id', 'online_order_no'), internal | online) or after
        stores = {str(r['order_store_id']): r for r in _feed('stores', ('order_store_id',),
                                                             {str(r.get('order_store_id')) for r in [*orders, *after]})}
        if not (orders or after):
            lines.append(f'订单台快照里没有找到「{key}」（可填线上单号、平台子订单号、内部单号、售后单号、平台售后单号）。')
        else:
            lines.append(f'订单台快照：线上单号 {"、".join(sorted(online)) or "—"}，拆成 {len(orders)} 张内部单 {"、".join(sorted(internal))}。')
            for sid, st in stores.items():
                lines.append(f'店铺：{st.get("shop_name")} → 台账店铺 {st.get("ledger_store_id") or "未对上"}（{st.get("mapping_status")}）')
            lines += _rows('内部单', orders, 'orders')
            lines += _rows('商品行（outer_sku 就是台账挂账用的平台子订单号）', items, 'order_items')
            lines += _rows('订单台成本', costs, 'order_costs')
            lines += _rows('售后单', after, 'after_sales')
            if after:
                returned, notes = _after_items(after, items)
                lines += _rows('售后退的商品（按内部子单号对到商品行；sku_id 空表示售后单没写编码）', returned, 'after_sale_items')
                lines += notes
        live = []
        for oid in sorted(internal)[:6]:
            try:
                got = _json_get(ORDER_URL + f'/api/orders/{urllib.parse.quote(oid)}/after-sales', timeout=10)
                live += got.get('after_sales') or got.get('items') or (got if isinstance(got, list) else [])
            except (OSError, ValueError):
                pass
        known = {str(r.get('after_sale_id')) for r in after}
        fresh = [r for r in live if isinstance(r, dict) and str(r.get('after_sale_id')) not in known]
        if fresh:
            lines += _rows('快照之后订单台新增的售后单', fresh, 'after_sales')
    except (OSError, ValueError, KeyError, ImportError) as exc:
        lines.append(f'订单台快照读不到：{exc}')
        online = set()
    for q in sorted(online or {key})[:3]:
        try:
            data = _json_get(config.LEDGER_URL + '/api/search?' + urllib.parse.urlencode({'q': q}), timeout=60)
            hits = [h for h in data.get('hits') or [] if h.get('subject') != '订单台售后单']
            lines.append(f'台账原始资料里找到 {len(hits)} 处「{q}」：')
            for hit in hits[:15]:
                lines.append(f'- [{hit.get("subject")}] {hit.get("store")}（{hit.get("store_id")}） 文件 {hit.get("file")} 第 {hit.get("row_no")} 行：'
                             + (hit.get('snippet') or '')[:200].replace('\t', ' | '))
        except (OSError, ValueError) as exc:
            lines.append(f'台账搜索没有响应：{exc}')
    lines.append('（台账按线上单号、平台子订单号挂账，台账里没有售后单这种单据：售后让商品成本不计时，'
                 '那条商品成本记 0、备注里写着售后单号。订单台的退款金额只作参考，台账的退款以平台对账流水为准。）')
    return _cut('\n'.join(lines), 12000)


def schema(name, description, properties, required=()):
    props = {'purpose': {'type': 'string', 'description': '用用户能看懂的业务语言，一句话说明这一步在做什么，例如“查看 8 月这家店的退款明细”。不要出现技术词。'},
             **properties}
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': props, 'required': ['purpose', *required]}}}


S = {'type': 'string'}
SPECS = [
    schema('store_metrics', '【查经营数字首选，一步出结果】某月各店铺经营指标：按指标排名（谁最高/最低、前几名、合计）或查某家店的全部指标。'
           '数据就是左侧经营看板的结果。metric 填指标名（销售收入、销售退款、退款率、商品成本、商品毛利、毛利率、推广费用、平台推广、补单成本、'
           '平台费用、发货运费、客服售后费用、利润、兼职人工费用、利润额等）；store 填店铺名关键字（空格分隔多个关键字）；'
           'compare=true 时附环比/同比。', {'month': S, 'metric': S, 'store': S, 'top': {'type': 'integer'},
                                     'ascending': {'type': 'boolean'}, 'platform': S, 'compare': {'type': 'boolean'}}, ['month']),
    schema('sku_cost', '【查商品成本价首选】按 SKU 编码或商品名查订单系统（聚水潭同步）的当前成本价和调价历史，秒回。', {'sku': S}, ['sku']),
    schema('order_lookup', '【查某个订单/售后单首选】填任意一个号：线上单号、平台子订单号、聚水潭内部单号、售后单号、平台售后单号。'
           '秒回订单台里这笔的拆单、商品行、成本、售后单（含退款参考金额）和对应台账店铺，并附台账原始资料里按线上单号找到的行；'
           '用来回答订单在哪个店、有没有售后/退款、成本怎么算。', {'order_id': S}, ['order_id']),
    schema('ledger_get', '调用台账服务的只读接口（GET），返回 JSON。', {'path': S, 'query': {'type': 'object'}}, ['path']),
    schema('ledger_preview', '调用台账的预览接口（路径含 preview，POST），只生成预览、不改数据，返回 proposal_id/proposal_sha/input_sha 等。',
           {'path': S, 'body': {'type': 'object'}}, ['path', 'body']),
    schema('api_index', '按关键字列出台账服务的全部接口。', {'keyword': S}),
    schema('sql_query', '只读 SQL 查询。db=workspace（台账主库，体积很大，必须带店铺/账期过滤和 LIMIT）或 commission（提成库）。',
           {'sql': S, 'db': {'type': 'string', 'enum': list(config.DATABASES)}}, ['sql']),
    schema('read_file', '读取文本文件（带行号）。', {'path': S, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['path']),
    schema('search_files', '在目录中按正则搜索文本，默认程序目录。', {'pattern': S, 'path': S, 'glob': S}, ['pattern']),
    schema('write_file', '写入整个文件（会自动留存修改前版本）。', {'path': S, 'content': S}, ['path', 'content']),
    schema('edit_file', '把文件中唯一出现的一段原文替换为新文本（会自动留存修改前版本）。', {'path': S, 'old': S, 'new': S}, ['path', 'old', 'new']),
    schema('python', '运行一段 Python 处理数据（排序、汇总、对比、核对），print 输出结果。已内置 get(path, **query) 调台账只读接口返回 JSON，'
           'sql(query, db="workspace", params=()) 只读查库返回字典列表。取数和计算优先用这个工具。',
           {'code': S, 'timeout': {'type': 'integer'}}, ['code']),
    schema('powershell', '在生产机上执行 PowerShell 命令（完整权限），用于系统运维；取数和计算请用 python。', {'command': S, 'timeout': {'type': 'integer'}}, ['command']),
    schema('restart_ledger', '重启台账服务使程序修改生效；自动检查服务和已结账结果，异常时自动恢复修改前的程序。', {}),
    schema('restart_assistant', '修改了助手自身代码后调用，回答结束后助手自动重启。', {}),
    schema('build_web', '修改左侧界面源码（D:\\ledger\\app\\ledger\\web\\src）后调用：构建并立即推送给所有人，失败不影响线上；用户可一键撤销。'
           'summary 用一句业务话描述改了什么。', {'summary': S}, ['summary']),
    schema('web_search', '联网搜索（平台规则、费率政策、物流/税务公告、软件报错等外部信息）。按次计费，台账内部数据不要用它查。',
           {'query': S}, ['query']),
    schema('machine_map', '生产机地图：各端口/服务/计划任务/共享盘/目录/日志位置。section 可填 监听端口、计划任务、服务、共享盘、磁盘、目录、日志。',
           {'section': S}),
    schema('find_files', '在 NAS 共享盘（\\\\192.168.0.125\\dataAnalysis）按文件名/路径关键字找文件，空格分隔多个关键字（都要匹配），按修改时间从新到旧。',
           {'keywords': S, 'limit': {'type': 'integer'}}, ['keywords']),
    schema('read_knowledge', '读取系统地图的某个章节（文件名或关键字，如 api、pages、glossary、rules、known-issues、code、data-model）。',
           {'section': S}, ['section']),
    schema('propose_change', '提出一项会影响账务数据的修改，生成确认卡片给用户，用户点确认后才执行。'
           'actions 为按顺序执行的步骤：{"type":"api","method":"POST","path":"...","body":{...}} 或 '
           '{"type":"file","path":"...","old":"...","new":"..."} 或 {"type":"powershell","command":"..."}。',
           {'title': S, 'summary': S,
            'items': {'type': 'array', 'description': '给用户看的变化清单', 'items': {'type': 'object', 'properties': {
                'label': S, 'before': S, 'after': S}}},
            'actions': {'type': 'array', 'items': {'type': 'object'}}}, ['title', 'summary', 'items', 'actions']),
    schema('remember', '把一条稳定的业务口径、偏好或结论写入长期记忆（简短一句）。', {'note': S}, ['note']),
    schema('record_correction', '确认一个问题的原因并处理后，记录这次纠错。', {'problem': S, 'cause': S, 'fix': S, 'scope': S},
           ['problem', 'cause', 'fix']),
    schema('search_memory', '搜索以往的纠错记录。', {'query': S}, ['query']),
    schema('auto_close_status', '查看最近的自动结账运行结果。', {}),
    schema('run_auto_close', '立即对指定月份执行自动结账（检查全部通过的店铺直接结账，耗时可能几十分钟）。不填则为最近三个已过去的月份。',
           {'periods': {'type': 'array', 'items': S}}),
]

HANDLERS = {
    'ledger_get': ledger_get, 'ledger_preview': ledger_preview, 'api_index': api_index, 'sql_query': sql_query,
    'read_file': read_file, 'search_files': search_files, 'write_file': write_file, 'edit_file': edit_file,
    'powershell': powershell, 'python': python, 'restart_ledger': restart_ledger, 'restart_assistant': restart_assistant,
    'build_web': build_web, 'read_knowledge': read_knowledge, 'web_search': web_search, 'machine_map': machine_map,
    'find_files': find_files,
    'remember': remember, 'record_correction': record_correction, 'search_memory': search_memory,
    'auto_close_status': auto_close_status, 'run_auto_close': run_auto_close,
    'store_metrics': store_metrics, 'sku_cost': sku_cost, 'order_lookup': order_lookup,
}
DEFAULT_LABEL = {
    'ledger_get': '查看台账数据', 'ledger_preview': '生成预览', 'api_index': '查找可用功能', 'sql_query': '查询明细数据',
    'read_file': '查看规则与配置', 'search_files': '查找相关规则', 'write_file': '修复程序', 'edit_file': '修复程序',
    'powershell': '处理系统问题', 'python': '计算核对数据', 'restart_ledger': '让修复生效', 'restart_assistant': '更新助手',
    'build_web': '更新界面', 'read_knowledge': '查阅系统说明', 'web_search': '上网查一下', 'machine_map': '看一下机器上的服务',
    'find_files': '在共享盘找文件',
    'propose_change': '整理修改方案', 'remember': '记住这条信息', 'record_correction': '记录本次纠错',
    'search_memory': '查找以往类似问题', 'auto_close_status': '查看自动结账情况', 'run_auto_close': '执行自动结账',
    'store_metrics': '查经营看板数字', 'sku_cost': '查商品成本价', 'order_lookup': '查这个订单',
}


BUSY_WAITS = (10, 20, 30)
_executing = set()
_executing_lock = threading.Lock()


class LedgerBusy(Exception):
    pass


def _ledger_busy(status, text):
    # 台账锁库时只回一句笼统的服务器错误；业务上的拒绝都带 detail，不在此列。
    return status in (500, 502, 503, 504) and '"detail"' not in text


def _card_api(action):
    for wait in BUSY_WAITS + (None,):
        status, text = _http(action.get('method', 'POST').upper(), action['path'], action.get('body'), timeout=1800)
        if not _ledger_busy(status, text):
            break
        if wait is None:
            raise LedgerBusy()
        time.sleep(wait)
    if status >= 400:
        raise RuntimeError(_friendly_error(text))
    return text


def execute_card(card_id, by):
    with _executing_lock:
        if card_id in _executing:
            return {'status': 'pending', 'message': '正在执行，请稍候'}
        _executing.add(card_id)
    try:
        return _execute_card(card_id, by)
    finally:
        with _executing_lock:
            _executing.discard(card_id)


def _execute_card(card_id, by):
    row = store.card(card_id)
    if not row or row['status'] != 'pending':
        return {'status': row['status'] if row else 'missing', 'message': '这张卡片已经处理过了'}
    actions = json.loads(row['actions'])
    results = []
    try:
        for index, action in enumerate(actions):
            kind = action.get('type', 'api')
            if kind == 'api':
                try:
                    results.append(_card_api(action)[:2000])
                except LedgerBusy:
                    from . import events
                    done = f'已完成 {index} 步，' if index else ''
                    message = f'台账正忙，{done}稍后再点确认会接着做剩下的'
                    store.card_remaining(card_id, actions[index:], message)
                    events.publish('card_update', {'card_id': card_id, 'status': 'pending', 'message': message})
                    if index:
                        store.add_message(row['thread_id'], 'system', f'「{row["title"]}」执行到一半台账正忙，已完成 {index} 步，'
                                          f'剩下 {len(actions) - index} 步等再次确认。结果摘要：' + _cut(results, 2000))
                    return {'status': 'pending', 'message': message}
            elif kind == 'file':
                outcome = edit_file(action['path'], action['old'], action['new'])
                if not outcome.startswith('已修改'):
                    raise RuntimeError(outcome)
                results.append(outcome)
            elif kind == 'powershell':
                outcome = powershell(action['command'])
                if not outcome.startswith('exit=0'):
                    raise RuntimeError('执行没有成功')
                results.append(outcome[:2000])
        from . import events
        message = '已按确认完成修改' + (f'（{by} 确认）' if by else '')
        store.decide_card(card_id, 'applied', message, by)
        store.add_message(row['thread_id'], 'system', f'{by or "用户"}已确认「{row["title"]}」，已执行。结果摘要：' + _cut(results, 3000))
        events.publish('card_update', {'card_id': card_id, 'status': 'applied', 'message': message, 'by': by})
        events.publish('data_updated', {})
        return {'status': 'applied', 'message': message}
    except Exception as exc:
        from . import events
        message = f'修改没有完成：{exc}'
        store.decide_card(card_id, 'failed', message, by)
        events.publish('card_update', {'card_id': card_id, 'status': 'failed', 'message': message, 'by': by})
        store.add_message(row['thread_id'], 'system', f'用户确认「{row["title"]}」后执行失败：{exc}；已完成步骤：' + _cut(results, 2000))
        return {'status': 'failed', 'message': message}


def _friendly_error(text):
    try:
        detail = json.loads(text).get('detail')
        return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:300]
    except (ValueError, AttributeError):
        return text[:300]
