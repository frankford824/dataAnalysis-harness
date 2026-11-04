"""助手改左侧界面：构建到临时目录，成功后换上线；每次留快照，可一键撤销。

资源文件名带哈希，旧资源一直留着，所以切换和撤销都只是换 index.html。
"""
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config, events, store

WEB = config.APP_DIR / 'ledger' / 'web'
STATIC = config.APP_DIR / 'ledger' / 'ledger' / 'static'
HISTORY = config.ROOT / 'ui-history'


def _entries():
    path = HISTORY / 'history.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def _save(entries):
    HISTORY.mkdir(parents=True, exist_ok=True)
    (HISTORY / 'history.json').write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding='utf-8')


def build(summary, batch=None):
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    out = HISTORY / 'builds' / stamp
    command = f'pnpm exec vite build --outDir "{out}" --emptyOutDir'
    started = time.time()
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                            cwd=WEB, capture_output=True, timeout=600)
    if result.returncode != 0 or not (out / 'index.html').exists():
        log = (result.stdout + result.stderr).decode('utf-8', 'replace')
        return {'ok': False, 'message': '界面构建失败，线上界面没有变化。构建输出：\n' + log[-3000:]}
    snapshot = HISTORY / 'snapshots' / stamp
    snapshot.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATIC / 'index.html', snapshot / 'index.html')
    for item in out.iterdir():
        if item.name == 'index.html':
            continue
        target = STATIC / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    shutil.copy2(out / 'index.html', STATIC / 'index.html')
    shutil.rmtree(out, ignore_errors=True)
    entries = _entries()
    entries.append({'id': stamp, 'summary': summary, 'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                    'snapshot': str(snapshot), 'batch': batch, 'undone': False})
    _save(entries[-50:])
    store.log_event('ui_build', {'id': stamp, 'summary': summary, 'seconds': round(time.time() - started, 1)})
    events.publish('ui_updated', {'summary': summary, 'can_undo': True})
    return {'ok': True, 'message': f'界面已更新并推送给所有人（构建 {time.time() - started:.0f} 秒）。'}


def history():
    entries = [e for e in _entries() if not e['undone']]
    return {'items': [{'id': e['id'], 'summary': e['summary'], 'at': e['at']} for e in reversed(entries)][:10],
            'can_undo': bool(entries)}


def undo(by=''):
    entries = _entries()
    target = next((e for e in reversed(entries) if not e['undone']), None)
    if not target:
        return {'status': 'nothing', 'message': '没有可以撤销的界面修改'}
    shutil.copy2(Path(target['snapshot']) / 'index.html', STATIC / 'index.html')
    manifest = config.ROOT / 'code-history' / (target.get('batch') or '-') / 'manifest.json'
    if manifest.exists():
        for entry in json.loads(manifest.read_text(encoding='utf-8')):
            path = Path(entry['path'])
            if WEB.resolve() not in path.resolve().parents:
                continue
            if entry['backup']:
                shutil.copy2(entry['backup'], path)
            elif path.exists():
                path.unlink()
    target['undone'] = True
    _save(entries)
    store.log_event('ui_undo', {'id': target['id'], 'summary': target['summary'], 'by': by})
    events.publish('ui_updated', {'summary': '已撤销：' + target['summary'], 'can_undo': history()['can_undo']})
    return {'status': 'undone', 'message': '已撤销：' + target['summary']}
