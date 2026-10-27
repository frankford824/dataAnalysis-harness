"""Self-contained, hash-checked freight experiment inputs. Never opens a writer on live data."""
from contextlib import closing
import json
from pathlib import Path
import re
import shutil

from .freight_trial_inputs import InputError, digest, readonly
from .freight_trial import _write_json, _hash_file, now

REQUIRED_OBJECTS = {'stores.parquet', 'orders.parquet', 'order_items.parquet',
    'order_costs.parquet', 'order_relations.parquet', 'after_sales.parquet',
    'after_sale_items.parquet', 'controls.parquet'}


def _copy(source, target, expected=None):
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise InputError('实验原始输入缺失：' + str(source))
    before = _hash_file(source)
    if expected and before != expected:
        raise InputError('实验原始输入校验失败：' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_symlink() and _hash_file(target)==before:
            return before
        raise InputError('实验输入副本已存在且内容不同，禁止覆盖')
    shutil.copyfile(source, target, follow_symlinks=True)
    if _hash_file(target) != before:
        raise InputError('固定输入时来源发生变化：' + str(source))
    return before


def capture(root, feed_root, model, period, target, catalog, allow_zero_equal=True, *,
            baseline_policy='confirmed_entities_with_explicit_unavailable'):
    """Freeze active slots, the complete feed snapshot and its consumed overlay.

    Historical runs are provenance references only. Neither branch reads their money.
    All source rows, including shops absent from the model, remain in the fee pool.
    """
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period or ''):
        raise InputError('请选择有效月份')
    if baseline_policy not in {'strict_complete','confirmed_entities_with_explicit_unavailable'}:
        raise InputError('实验基准可用范围策略无效')
    root, feed_root, target = Path(root), Path(feed_root), Path(target)
    if target.exists():
        raise InputError('实验输入包已存在，禁止覆盖')
    with closing(readonly(root / 'workspace.db')) as db:
        db.execute('BEGIN')
        slots = [dict(r) for r in db.execute('SELECT store_id,name,sha FROM slot ORDER BY store_id,name')]
        runs = [dict(r) for r in db.execute('SELECT store_id,period,state,run_id FROM period WHERE period=? ORDER BY store_id', (period,))]
    with closing(readonly(root / 'order-feed.db')) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM feed_state WHERE id=1').fetchone()
        if row is None:
            raise InputError('订单台冻结状态缺失')
        state = dict(row)
        mapping = [dict(r) for r in db.execute('SELECT * FROM feed_store ORDER BY order_store_id')]
        deltas = [dict(r) for r in db.execute('SELECT * FROM feed_entity ORDER BY entity_type,entity_id')]
    manifest = json.loads(state.get('manifest_json') or '{}')
    if REQUIRED_OBJECTS - set(manifest.get('objects') or {}):
        raise InputError('实验快照缺对象：' + '、'.join(sorted(REQUIRED_OBJECTS - set(manifest.get('objects') or {}))))
    if not state.get('snapshot_id') or manifest.get('snapshot_id') != state['snapshot_id']:
        raise InputError('实验快照身份不一致')
    through = manifest.get('through_seq')
    if not isinstance(through, int) or through > state['consumed_seq']:
        raise InputError('实验快照缺有效消费水位')
    if state.get('last_error') or state['consumed_seq'] < state['source_latest_seq']:
        raise InputError('订单台输入尚未同步完整，不能签发同输入实验')
    if any(int(r['seq']) > state['consumed_seq'] for r in deltas):
        raise InputError('实验增量超过冻结消费水位')
    target.mkdir(parents=True)
    inventory = {}
    for slot in slots:
        if not re.fullmatch('[0-9a-f]{64}', slot['sha']):
            raise InputError('原文件SHA无效')
        record = inventory.setdefault(slot['sha'], {'sha':slot['sha'], 'name':slot['name'], 'owners':{}, 'active':True})
        record['owners'][slot['store_id']] = 0
        _copy(root / 'files' / slot['sha'][:2] / slot['sha'], target / 'files' / slot['sha'][:2] / slot['sha'], slot['sha'])
    frozen_manifest = {**manifest, 'objects':{}}
    for name, meta in sorted(manifest['objects'].items()):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.parquet',name):
            raise InputError('实验快照对象名称无效')
        relative = Path(str(meta.get('path') or ''))
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise InputError('实验快照路径无效：' + name)
        source = feed_root / relative
        if not source.resolve().is_relative_to(feed_root.resolve()):
            from .storage_integrity import registered_archive_link
            if not registered_archive_link(feed_root, source, meta.get('sha256')):
                raise InputError('实验快照路径越界：' + name)
        if not re.fullmatch('[0-9a-f]{64}', str(meta.get('sha256') or '')):
            raise InputError('实验快照缺SHA：' + name)
        destination = target / 'feed' / name
        _copy(source, destination, meta['sha256'])
        if destination.stat().st_size != int(meta['bytes']):
            raise InputError('实验快照大小不符：' + name)
        import polars as pl
        if pl.scan_parquet(destination).select(pl.len()).collect().item() != int(meta['rows']):
            raise InputError('实验快照行数不符：' + name)
        frozen_manifest['objects'][name] = {**meta, 'path':name}
    support = []
    for folder in ('order-components', 'order-date-context'):
        home = root / folder
        if not home.exists():
            continue
        for file in sorted(home.rglob('*')):
            if file.is_file():
                relative = file.relative_to(root)
                support.append({'path':relative.as_posix(), 'sha256':_copy(file, target / relative)})
    inputs = {'period':period, 'captured_at':now(), 'files':sorted(inventory.values(), key=lambda r:r['sha']),
        'slots':slots, 'runs':runs, 'feed_manifest':frozen_manifest,
        'snapshot_id':state['snapshot_id'], 'consumed_seq':state['consumed_seq'],
        'source_latest_seq':state['source_latest_seq'], 'mapping':mapping,
        'delta_hash':digest(deltas), 'model_hash':digest(json.loads(model.model_dump_json())),
        'classification_hash':digest(catalog), 'support':support,
        'allow_zero_equal':bool(allow_zero_equal), 'file_policy':'active_slots',
        'old_runs_used_for_calculation':False,'baseline_policy':baseline_policy}
    from .freight_experiments import code_identity
    inputs['execution_identity'] = code_identity()
    _write_json(target / 'model.json', json.loads(model.model_dump_json()))
    _write_json(target / 'classification.json', catalog)
    _write_json(target / 'deltas.json', deltas)
    _write_json(target / 'feed-state.json', {**state, 'manifest_json':json.dumps(frozen_manifest)})
    _write_json(target / 'inputs.json', inputs)
    seal(target)
    return inputs


def seal(target):
    target = Path(target)
    files = [{'path':p.relative_to(target).as_posix(), 'sha256':_hash_file(p), 'bytes':p.stat().st_size}
             for p in sorted(target.rglob('*')) if p.is_file() and p.name != 'bundle.json']
    inputs = json.loads((target / 'inputs.json').read_text(encoding='utf-8'))
    identity = {k:v for k,v in inputs.items() if k not in {'captured_at','runs'}}
    _write_json(target / 'bundle.json', {'schema_version':1, 'files':files,
        'inventory_hash':digest(files),'fingerprint':digest(identity)})


def verify(target, *, full=True):
    target = Path(target).resolve()
    manifest = json.loads((target / 'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or digest(manifest['files']) != manifest.get('inventory_hash'):
        raise InputError('实验输入包目录校验失败')
    expected = set()
    for record in manifest['files']:
        file = target / record['path']
        if file.is_symlink() or not file.resolve().is_relative_to(target) or not file.is_file():
            raise InputError('实验输入包缺失或路径无效')
        stat = file.stat()
        if full:
            correct = _hash_file(file) == record['sha256']
        else:
            from .freight_trial import _checked
            correct = _checked(str(file),stat.st_mtime_ns,stat.st_size,record['sha256'])
        if stat.st_size != record['bytes'] or not correct:
            raise InputError('实验输入包校验失败：' + record['path'])
        expected.add(record['path'])
    actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file() and p.name != 'bundle.json'}
    if expected != actual:
        raise InputError('实验输入包文件清单发生变化')
    inputs = json.loads((target / 'inputs.json').read_text(encoding='utf-8'))
    if digest({k:v for k,v in inputs.items() if k not in {'captured_at','runs'}}) != manifest['fingerprint']:
        raise InputError('实验输入内容身份校验失败')
    return inputs, manifest['fingerprint']


def materialize_feed(bundle, scratch):
    """Create a writable adapter cache only inside the experiment scratch folder."""
    from .order_feed import OrderFeed
    bundle, scratch = Path(bundle), Path(scratch)
    feed = OrderFeed(scratch, feed_root=bundle / 'feed')
    state = json.loads((bundle / 'feed-state.json').read_text(encoding='utf-8'))
    inputs = json.loads((bundle / 'inputs.json').read_text(encoding='utf-8'))
    deltas = json.loads((bundle / 'deltas.json').read_text(encoding='utf-8'))
    with feed._connect() as db:
        for table, records in (('feed_state',[state]), ('feed_store',inputs['mapping']), ('feed_entity',deltas)):
            columns = [r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')]
            db.execute('DELETE FROM ' + table)
            for record in records:
                keys = [k for k in columns if k in record]
                db.execute('INSERT INTO ' + table + '(' + ','.join(keys) + ') VALUES(' + ','.join('?' for _ in keys) + ')', [record[k] for k in keys])
    for folder in ('order-components', 'order-date-context'):
        if (bundle / folder).exists():
            shutil.copytree(bundle / folder, scratch / folder)
    return feed
