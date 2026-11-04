"""生产机地图和 NAS 文件名索引，每天自动刷新一次。

地图让助手一眼知道这台机器上跑着什么、端口归谁、日志在哪；NAS 索引让按文件名、
月份、店铺找文件只需一次查询，不用每次在 1TB 以上的共享盘上遍历。
"""
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, store

MAP = config.ROOT / 'machine-map.md'
INDEX = config.ROOT / 'nas-index.db'
NAS_ROOT = os.environ.get('COPILOT_NAS_ROOT', r'\\192.168.0.125\dataAnalysis')
SKIP_DIRS = {'$RECYCLE.BIN', 'System Volume Information', '#recycle', '@eaDir', '.snapshot', 'node_modules', '.git'}
SECRET = re.compile(r'(?i)((?:key|token|secret|password|passwd|pwd)[=: ]+)\S+')
INVENTORY = r'''
$ErrorActionPreference = 'SilentlyContinue'
"## 监听端口"
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalAddress -notin '::','::1' } | Sort-Object LocalPort -Unique | ForEach-Object {
  $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.OwningProcess)
  "- {0}:{1} {2} :: {3}" -f $_.LocalAddress, $_.LocalPort, $p.Name, ($p.CommandLine -replace '\s+', ' ')
}
"## 计划任务（非系统）"
Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\' } | ForEach-Object {
  $a = ($_.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' ; '
  "- {0} [{1}] {2}" -f $_.TaskName, $_.State, $a
}
"## 第三方服务（运行中）"
Get-CimInstance Win32_Service | Where-Object { $_.State -eq 'Running' -and $_.PathName -notmatch '(?i)\\windows\\' } | ForEach-Object {
  "- {0} ({1}) {2}" -f $_.Name, $_.DisplayName, $_.PathName
}
"## 共享盘"
Get-SmbGlobalMapping | ForEach-Object { "- 全局 {0} -> {1} {2}" -f $_.LocalPath, $_.RemotePath, $_.Status }
Get-SmbMapping | ForEach-Object { "- 用户 {0} -> {1} {2}" -f $_.LocalPath, $_.RemotePath, $_.Status }
"## 磁盘"
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used } | ForEach-Object { "- {0}: 已用 {1} GB，剩余 {2} GB" -f $_.Name, [int]($_.Used/1GB), [int]($_.Free/1GB) }
"## 主要目录"
foreach ($root in 'D:\', 'D:\ledger', 'D:\order') {
  Get-ChildItem $root -Directory | ForEach-Object { "- " + $_.FullName }
}
"## 日志目录"
Get-ChildItem 'D:\' -Directory -Recurse -Depth 2 -Filter 'logs' | ForEach-Object { "- " + $_.FullName }
'''


def refresh_map():
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', INVENTORY],
                            capture_output=True, timeout=300)
    text = result.stdout.decode('utf-8', 'replace') if result.stdout else ''
    if '\ufffd' in text[:2000]:
        text = result.stdout.decode('gbk', 'replace')
    lines = [SECRET.sub(r'\1***', line[:400]) for line in text.splitlines() if line.strip()]
    body = f'# 生产机地图（{datetime.now():%Y-%m-%d %H:%M} 自动生成）\n\n' + '\n'.join(
        ('\n' + line) if line.startswith('## ') else line for line in lines)
    MAP.write_text(body, encoding='utf-8')
    return body


def machine_map(section='', **_):
    if not MAP.exists() or time.time() - MAP.stat().st_mtime > 86400:
        refresh_map()
    text = MAP.read_text(encoding='utf-8')
    if section:
        parts = [p for p in text.split('\n## ') if section in p.splitlines()[0]]
        if parts:
            return '## ' + parts[0][:7000]
    return text[:7000]


def refresh_index():
    temp = INDEX.with_suffix('.new')
    temp.unlink(missing_ok=True)
    db = sqlite3.connect(temp)
    db.execute('CREATE TABLE file(path TEXT, name TEXT, size INTEGER, mtime REAL)')
    started, count, batch = time.time(), 0, []
    stack = [NAS_ROOT]
    while stack and time.time() - started < 3 * 3600:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRS:
                                stack.append(entry.path)
                        else:
                            info = entry.stat(follow_symlinks=False)
                            batch.append((entry.path, entry.name.lower(), info.st_size, info.st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
        if len(batch) >= 5000:
            db.executemany('INSERT INTO file VALUES(?,?,?,?)', batch)
            count += len(batch)
            batch = []
    db.executemany('INSERT INTO file VALUES(?,?,?,?)', batch)
    count += len(batch)
    db.execute('CREATE INDEX file_mtime ON file(mtime)')
    db.commit()
    db.close()
    os.replace(temp, INDEX)
    store.log_event('nas_index', {'files': count, 'seconds': int(time.time() - started), 'complete': not stack})
    return count


def find_files(keywords, limit=30, **_):
    if not INDEX.exists():
        return 'NAS 文件索引还在建立中，稍后再试；急用可以用 powershell 在共享盘上按名字查找。'
    terms = [t.lower() for t in re.split(r'\s+', keywords.strip()) if t][:6]
    if not terms:
        return '请给出要找的文件名关键字'
    db = sqlite3.connect(f'file:{INDEX}?mode=ro', uri=True)
    where = ' AND '.join('path LIKE ?' for _ in terms)
    rows = db.execute(f'SELECT path, size, mtime FROM file WHERE {where} ORDER BY mtime DESC LIMIT ?',
                      [f'%{t}%' for t in terms] + [min(int(limit), 100)]).fetchall()
    total = db.execute(f'SELECT count(*) FROM file WHERE {where}', [f'%{t}%' for t in terms]).fetchone()[0]
    built = datetime.fromtimestamp(INDEX.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
    db.close()
    lines = [f'{datetime.fromtimestamp(m):%Y-%m-%d %H:%M}  {s / 1e6:.1f}MB  {p}' for p, s, m in rows]
    return f'共 {total} 个匹配（索引更新于 {built}），按修改时间从新到旧：\n' + '\n'.join(lines)


def start():
    def loop():
        time.sleep(60)
        while True:
            for job in (refresh_map, refresh_index):
                try:
                    target = MAP if job is refresh_map else INDEX
                    if not target.exists() or time.time() - target.stat().st_mtime > 20 * 3600:
                        job()
                except Exception as exc:
                    store.log_event('machine_refresh_error', {'job': job.__name__, 'error': str(exc)[:500]})
            time.sleep(3600)

    threading.Thread(target=loop, name='machine-refresh', daemon=True).start()
