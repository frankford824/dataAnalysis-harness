"""Confirm policy-zero goods cost through scoped seller evidence, including merges."""
import hashlib
import json
import re
from datetime import datetime

import polars as pl

from .engine.cost_policy import BRUSHING_REMARK_PATTERN, is_brushing
from .engine.link import SPINE_STORE, SPINE_PERIOD, Spine, normalize_key, target_role
from .order_flags import seller_flag_value


def _true(value):
    return value is True or str(value).lower() == 'true'


def _ids(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    values = [str(v).strip() for v in value if v is not None]
    # The upstream identity contract owns parsing of shop-prefixed merge IDs.
    # Accept only already certified plain keys, never extract numbers from text.
    return sorted(set(values)) if values and all(v and not re.search(r'[,，;；:]', v) for v in values) else []


def _member_evidence(header, store):
    if header.get('platform_seller_evidence_version') != 'jst-member-evidence.v1':
        return []
    value = header.get('platform_seller_evidence_json')
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: return []
    if not isinstance(value, dict) or value.get('version') != 'jst-member-evidence.v1': return []
    if str(value.get('root_order_id') or '') != str(header.get('order_id') or ''): return []
    if str(value.get('order_store_id') or '') != str(header.get('order_store_id') or ''): return []
    members = value.get('members')
    if not isinstance(members, list): return []
    parents = _ids(header.get('platform_order_ids'))
    records = []
    for member in members:
        if not isinstance(member, dict): continue
        parent = str(member.get('platform_order_id') or '')
        if parent not in parents or str(member.get('order_store_id') or '') != str(header.get('order_store_id') or ''):
            continue
        valid = (member.get('status') == 'verified' and member.get('scope') == 'platform_order_header'
                 and member.get('source') == 'jst.order_detail' and member.get('source_is_merge') is False
                 and bool(member.get('original_internal_order_id'))
                 and bool(re.fullmatch('[0-9a-f]{64}', str(member.get('evidence_sha') or ''))))
        try: datetime.fromisoformat(str(member.get('captured_at') or '').replace('Z', '+00:00'))
        except ValueError: valid = False
        flag, bad = seller_flag_value(member)
        remark = str(member.get('order_remark') or '')
        eligible = valid and not bad and flag == '蓝色旗帜' and bool(re.search(BRUSHING_REMARK_PATTERN, remark)) and '买家秀' not in remark
        proof = {**member, 'store': store.name, 'store_id': store.id,
                 'platform_seller_evidence_version': 'jst-member-evidence.v1'}
        records.append({'store': store.name, 'internal_order_id': str(member.get('original_internal_order_id') or ''),
                        'scope': 'platform_order_header', 'eligible': eligible,
                        'parents': json.dumps([parent], ensure_ascii=False),
                        'proof': json.dumps(proof, ensure_ascii=False, sort_keys=True)})
    return records


def capture(orders, items, store):
    records = []
    required = {'order_id', 'order_store_id', 'seller_flag_code', 'seller_flag_name',
                'seller_flag_source', 'seller_flag_scope', 'platform_order_ids',
                'platform_order_identity_complete', 'platform_order_identity_scope', 'order_remark',
                'source_evidence_version'}
    if not required <= set(orders.columns) or not {'order_id', 'order_flag'} <= set(items.columns):
        return pl.DataFrame()
    conflict = pl.col('__seller_flag_conflict').fill_null(False) if '__seller_flag_conflict' in items.columns else pl.lit(False)
    item_groups = {str(row['order_id']): row['eligible'] for row in items.group_by('order_id').agg(
        ((pl.col('order_flag') == '蓝色旗帜').fill_null(False) & ~conflict).all().alias('eligible')).to_dicts()}
    columns = sorted(required | {c for c in ('platform_seller_evidence_json', 'platform_seller_evidence_version') if c in orders.columns})
    headers = orders.select(columns).to_dicts()
    candidates = set()
    checked = []
    for header in headers:
        records.extend(_member_evidence(header, store))
        flag, bad = seller_flag_value(header)
        parents = _ids(header['platform_order_ids'])
        remark = str(header.get('order_remark') or '')
        internal = str(header['order_id'])
        eligible = not (bad or flag != '蓝色旗帜' or not parents or not _true(header['platform_order_identity_complete'])
                or header['platform_order_identity_scope'] != 'same_store_header_membership'
                or header['seller_flag_source'] != 'jst_order.extra.seller_flag'
                or header['seller_flag_scope'] != 'internal_order_header'
                or header['source_evidence_version'] != 'jst-header-evidence.v1'
                or not re.search(BRUSHING_REMARK_PATTERN, remark) or '买家秀' in remark
                or not item_groups.get(internal))
        # One merged header can concatenate brushing and ordinary remarks. A
        # brushing clause cannot exempt the unrelated members of that merge.
        clauses = [text.strip() for text in re.split(r'[;；\r\n]+', remark) if text.strip()]
        if len(parents) > 1 and not all(re.search(BRUSHING_REMARK_PATTERN, clause) for clause in clauses):
            eligible = False
        checked.append((header, parents, eligible))
        if eligible:
            candidates.update(parents)
    for header, parents, eligible in checked:
        if not set(parents) & candidates:
            continue
        proof = {**header, 'store': store.name, 'store_id': store.id, 'platform_order_ids': parents}
        records.append({'store': store.name, 'internal_order_id': str(header['order_id']), 'eligible': eligible,
                        'scope': 'internal_order_header',
                        'parents': json.dumps(parents, ensure_ascii=False),
                        'proof': json.dumps(proof, ensure_ascii=False, sort_keys=True)})
    return pl.DataFrame(records, schema={'store': pl.String, 'internal_order_id': pl.String,
                                       'parents': pl.String, 'proof': pl.String, 'eligible': pl.Boolean, 'scope': pl.String}).sort(
                                           ['store', 'internal_order_id', 'proof'])


def supplement(evidence, spine, metric, facts):
    """Add explicit zero facts only to certified sale targets with absent cost facts."""
    if evidence is None or evidence.is_empty() or metric is None or not metric.link or spine.is_empty():
        return facts.head(0).drop([c for c in ('counted', 'contribution') if c in facts.columns])
    role = target_role(metric.link.to)
    targets = Spine(spine).eligible(metric.link).frame
    if not {role, 'order_id', SPINE_STORE, SPINE_PERIOD} <= set(targets.columns):
        return facts.head(0)
    # Every certified parent must have consistent evidence across all headers.
    proofs = {}
    for record in evidence.iter_rows(named=True):
        for parent in json.loads(record['parents']):
            key = (record['store'], normalize_key(parent))
            proofs.setdefault(key, []).append(record)
    existing = {(r['store'], r['period'], normalize_key(r['link_key'])) for r in facts.filter(
        pl.col('metric_id') == metric.id).select('store', 'period', 'link_key').iter_rows(named=True)} if facts.height else set()
    # Reject conflicting visible child evidence, including explicit buyer-show
    # instructions. Missing fields in a platform export are not contradictions.
    conflicted = set()
    for row in targets.select([c for c in ('store', 'order_id', 'order_flag', 'order_remark') if c in targets.columns]).iter_rows(named=True):
        flag = str(row.get('order_flag') or '').strip()
        remark = str(row.get('order_remark') or '').strip()
        if (flag and flag != '蓝色旗帜') or (remark and (not re.search(BRUSHING_REMARK_PATTERN, remark) or '买家秀' in remark)):
            conflicted.add((row['store'], normalize_key(row['order_id'])))
    data, seen = [], set()
    for row in targets.iter_rows(named=True):
        parent = (row['store'], normalize_key(row['order_id']))
        key = (row['store'], row['period'], normalize_key(row.get(role)))
        if (not key[2] or not key[1] or key in seen or key in existing or parent in conflicted
                or parent not in proofs or row.get('order_type') == '补发订单'):
            continue
        records = proofs[parent]
        if getattr(metric, 'brushing_scope', 'platform_order') != 'internal_order_header':
            records = [record for record in records if record.get('scope') == 'platform_order_header'
                       or json.loads(record['parents']) == [parent[1]]]
            if not records:
                continue
        if not all(record['eligible'] for record in records):
            continue
        record = records[0]
        seen.add(key)
        proof = json.dumps([json.loads(item['proof']) for item in records], ensure_ascii=False, sort_keys=True)
        data.append({'metric_id': metric.id, 'source_id': metric.source,
            'template_id': 'order_console_brushing_zero_v1', 'store': key[0], 'period': key[1],
            'grain': 'order', 'link_key': key[2], 'linked': True, 'amount': 0.,
            'subject': '刷单规则确认零成本', 'major': metric.major, 'minor': None,
            'count_without_order': False, 'classify_via': '原始蓝色旗帜与独立 by 标记',
            'file_sha': None, 'file_name': '刷单免商品成本（订单台字段留档）',
            'sheet': '内部订单 ' + '、'.join(sorted({r['internal_order_id'] for r in records})), 'row_no': None,
            'order_id': row['order_id'], 'internal_order_id': record['internal_order_id'], 'sku': None,
            'source_note': '蓝色旗帜且卖家备注含 by，按刷单规则不计商品成本；'
                           '此行是规则确认零成本，不是缺少成本价；依据摘要 ' + hashlib.sha256(proof.encode()).hexdigest()
                           + '；原始字段依据：' + proof,
            'promotion_scope': None, 'source_period': None, 'statement_evidence': None})
    schema = {name: dtype for name, dtype in facts.schema.items() if name not in ('counted', 'contribution')}
    return pl.DataFrame(data, schema=schema, strict=False) if data else pl.DataFrame(schema=schema)


def annotate_pending(rows, evidence, store, metric):
    """Explain an unresolved brushing claim without changing the missing-cost list."""
    if rows.is_empty() or evidence.is_empty() or getattr(metric, 'brushing_scope', 'platform_order') == 'internal_order_header':
        return rows
    known, pending = set(), {}
    for record in evidence.filter(pl.col('store') == store).iter_rows(named=True):
        parents = json.loads(record['parents'])
        proof = json.loads(record['proof'])
        if record.get('scope') == 'platform_order_header':
            known.update(parents)
            if proof.get('status') in ('missing', 'conflict'):
                for parent in parents:
                    reason = {'original_seller_flag_missing':'原单旗帜未留存',
                              'original_remark_missing':'原单卖家备注未留存',
                              'original_order_not_found':'未找到可验证的原单',
                              'member_evidence_conflict':'原单标记存在冲突'}.get(proof.get('reason'), '原始旗帜或备注缺失/冲突')
                    pending[parent] = (f"原平台订单的刷单依据待核对：{reason}；不能凭合并头标记自动免成本。", record['internal_order_id'])
    for record in evidence.filter(pl.col('store') == store).iter_rows(named=True):
        parents = json.loads(record['parents'])
        if record.get('scope') == 'internal_order_header' and record['eligible'] and len(parents) > 1:
            for parent in parents:
                if parent not in known:
                    pending.setdefault(parent, ('合并头有蓝旗＋by，但缺少该原平台主单的独立标记，商品成本仍待核对。', record['internal_order_id']))
    values = [pending.get(str(row['order_id']), (None, None)) for row in rows.iter_rows(named=True)]
    if not any(text for text, _ in values): return rows
    return rows.with_columns(pl.Series('brushing_review', [v[0] for v in values], dtype=pl.String),
        pl.Series('brushing_evidence_internal_id', [v[1] for v in values], dtype=pl.String))
