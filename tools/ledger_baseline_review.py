"""Prepare private baseline adjudication material; never approve or record money.

Observed engine output is a comparison subject. Independent partial source
controls remain partial, and neither is an approved financial expected scope.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ledger'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite material')))


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def scope(stores):
    return {store: sorted(periods) for store, periods in sorted(stores.items())}


def baseline_identity(value, file_sha):
    manifest = value.get('input_manifest')
    issues = []
    if not re.fullmatch(r'[0-9a-f]{7,40}', str(value.get('engine_version', ''))):
        issues.append('baseline_code_not_reproducible')
    if value.get('schema') != 2:
        issues.append('baseline_schema_not_hash_bound_v2')
    if (not isinstance(manifest, dict) or not manifest.get('inputs')
            or not manifest.get('model') or manifest.get('scope') != scope(value.get('stores', {}))):
        issues.append('baseline_input_model_or_scope_identity_missing')
    return {'file_sha256': file_sha, 'engine_version': value.get('engine_version'),
            'schema': value.get('schema'), 'scope': scope(value.get('stores', {})),
            'issues': issues, 'financial_truth_established': False}


def safe_change(change):
    # Keyed list labels can contain people, orders, filenames, or amounts.
    family = change.path.split('.', 1)[0].split('[', 1)[0].split(' ', 1)[0]
    return {'change_id': identity([change.store, change.period, change.path]),
            'store_id': change.store, 'period': change.period, 'field_family': family,
            'kind': change.kind, 'money_field': change.money,
            'independent_expected_status': 'not_established_for_legacy_field'}


def verified_source_candidate(path):
    path = Path(path)
    value = read(path)
    if (value.get('kind') != 'bounded_real_source_service_case_proposal.v2'
            or value.get('engine_amounts_used_as_expected') is not False
            or value.get('financial_golden') is not False):
        raise ValueError('Source proposal must remain independent, partial, and unapproved')
    oracle = path.with_name('posting-oracle.frozen.private.json')
    if sha(oracle) != value['source_oracle_sha256']:
        raise ValueError('Independent source oracle identity changed')
    oracle_value = read(oracle)
    if oracle_value.get('engine_outputs_used_as_expected') is not False:
        raise ValueError('Engine observations cannot supply independent expected values')
    row_evidence = path.with_name('joined-posting-rows.private.json')
    row_evidence_sha = sha(row_evidence)
    if row_evidence_sha != oracle_value.get('row_evidence_sha256'):
        raise ValueError('Independent original-row evidence identity changed')
    bundle = Path(value['source_bundle']).resolve()
    manifest = bundle / 'bundle.json'
    if sha(manifest) != value['source_bundle_manifest_sha256']:
        raise ValueError('Original bundle manifest identity changed')
    files = read(manifest)['files']
    inputs_path = bundle / 'inputs.json'
    names = {row.get('sha'): row.get('name') for row in read(inputs_path).get('files', [])} if inputs_path.exists() else {}
    originals = {}
    for row in files:
        unresolved = bundle / row['path']
        material = unresolved.resolve()
        if unresolved.is_symlink() or not material.is_relative_to(bundle) or sha(material) != row['sha256']:
            raise ValueError('Original bundle material identity changed')
        originals[row['sha256']] = str(names.get(row['sha256']) or '原件') + ' (' + str(material) + ')'
    fields = []
    for declared in value['scopes']:
        if declared.get('final_financial_ready') is not False:
            raise ValueError('Partial source proposal cannot assert financial completion')
        for node, evidence in declared['statement'].items():
            supported = evidence.get('status') == 'independent_source_sum'
            if supported and (evidence.get('value') is None or evidence.get('comparison_required') is not True):
                raise ValueError('Source-supported node has no explicit value or comparison')
            fields.append({'store_id': declared['store_id'], 'period': declared['period'],
                'field': 'statement.' + node, 'expected': evidence.get('value') if supported else None,
                'source_status': evidence.get('status'), 'formula': evidence.get('formula'),
                'review_control_available': supported, 'financial_approved': False,
                'original_row_evidence_sha256': row_evidence_sha,
                'source_candidate_sha256': sha(path), 'source_oracle_sha256': sha(oracle)})
    return fields, {'path': str(path), 'sha256': sha(path),
                    'original_row_evidence_path': str(row_evidence), 'original_row_evidence_sha256': row_evidence_sha,
                    'original_bundle_manifest_sha256': sha(manifest), 'verified_bundle_files': len(files)}, originals


def _reference_lines(references, originals, maximum=3):
    lines = []
    for ref in references[:maximum]:
        # Cumulative-version groups contain their physical row references.
        rows = ref.get('row_references') if isinstance(ref, dict) else None
        if rows:
            lines.extend(_reference_lines(rows, originals, maximum=2))
            continue
        if not isinstance(ref, dict):
            continue
        source_sha = ref.get('source_sha256', ref.get('source_sha', ref.get('sha256')))
        original = originals.get(source_sha)
        lines.append('- 原件 ' + (original or '原件定位待补') + '; SHA=' + str(source_sha)
                     + '; sheet=' + str(ref.get('sheet')) + '; row=' + str(ref.get('row'))
                     + '; column=' + str(ref.get('subject_column', ref.get('column'))))
    return lines


def audit_unmapped_subjects(source_review, model):
    """Probe existing dictionary, overlays and real matcher; infer no new policy."""
    import difflib
    import unicodedata
    import polars as pl
    from ledger.engine.classify import classify, COL_CLASSIFIED, COL_EXCLUDED, COL_MAJOR
    from ledger.fees import known_fees
    from ledger.model.schema import normalize_header
    fees = [fee for fee in known_fees(model) if fee.platform in {'douyin', '*'}]
    templates = [template for template in model.templates if template.id.startswith('douyin')
                 and template.source == 'settlement' and any(binding.role == 'subject' for binding in template.bindings)]
    output = []
    for group in source_review['source_physical_controls']['unmapped_subject_groups']:
        if group['platform'] != 'douyin':
            continue
        subject = group['subject_value_private']
        variants = {'original': subject, 'unicode_trimmed': unicodedata.normalize('NFKC', subject).strip(),
                    'header_normalized': normalize_header(subject)}
        probes = []
        for variant, text in variants.items():
            frame = pl.DataFrame({'subject': [text], 'douyin_scene': [text], 'income': [1.]})
            for template in templates:
                classified, _ = classify(frame, model, 'douyin', amount_column='income', template=template)
                row = classified.row(0, named=True)
                probes.append({'variant': variant, 'template_id': template.id,
                    'classified': bool(row[COL_CLASSIFIED]), 'excluded': bool(row[COL_EXCLUDED]),
                    'major': row[COL_MAJOR]})
        aliases = sorted(({'raw_private': fee.key, 'major': fee.major, 'origin': fee.origin,
                    'similarity': difflib.SequenceMatcher(None, normalize_header(subject), fee.norm).ratio()}
                    for fee in fees if fee.field in {'subject', 'douyin_scene'}),
                    key=lambda item: item['similarity'], reverse=True)[:3]
        output.append({'subject_sha256': group['subject_sha256'], 'subject_private': subject,
            'probe_scope': 'subject_only_scalar_and_normalization_not_all_original_context_columns',
            'normalized_exact_known_fee_matches': sum(fee.norm == normalize_header(subject) for fee in fees),
            'matcher_probes': probes, 'lexical_candidates_not_approved_aliases': aliases,
            'simple_whitespace_or_unicode_alias_failure_reproduced': (
                not any(row['classified'] or row['excluded'] for row in probes if row['variant'] == 'original')
                and any(row['classified'] or row['excluded'] for row in probes if row['variant'] != 'original')),
            'semantic_alias_or_economic_nature_established': False,
            'no_new_policy_or_dictionary_written': True})
    return output


def human_review(source_review, refund_review, originals):
    """Private business wording is rendered locally; callers print only safe IDs."""
    lines = ['# 本机候选审查：未签认财务预期', '',
        '这是原件与规则的待核清单。引擎观察值不是预期，不要求业务逐项解释旧基线632处技术差异。', '']
    safe = []
    groups = source_review['source_physical_controls']['unmapped_subject_groups']
    for item in source_review['remaining_business']:
        if item['platform'] == 'douyin':
            original = next(group for group in groups if group['subject_sha256'] == item['subject_sha256'])
            count = item.get('canonical_original_events', original.get('occurrences'))
            lines += ['## ' + item['id'], '原动账场景：' + str(original['subject_value_private']),
                '影响范围：' + ', '.join(item['periods']) + '；独立事件数=' + str(count),
                '原件可采集：平台场景说明、对应订单/账单或费用协议，先查清实际经济性质。',
                '工程可推导：原件身份、物理行、发生日期及数值；不能仅靠类型未知就归零。',
                '候选规则：合同明确是损益收费时归已有对应费用；明确是资产往来时按往来处理；证据不足继续待核。',
                '先查已有字典/模板/当前界面规则及matcher别名；没有证实遗漏规则前，不把问题直接交财务。',
                '业务决定：仅在平台说明与已有规则仍无法确定时，确认已有凭据对应的经济性质及适用期间，不要求猜金额。']
            lines += _reference_lines(item['source_references'], originals)
        else:
            count = len(item['source_references'])
            lines += ['## ' + item['id'], '原件问题：同一累计账单存在多个来源版本。',
                '影响范围：' + ', '.join(item['periods']) + '；累计组数=' + str(count),
                '原件可采集：导出批次、账单覆盖期、更新时点及权威截止版本依据。',
                '工程可推导：逐行来源与不同字段，识别累计容器；禁止简单取first/max或按金额去重。',
                '候选规则：若证实为同一累计账单，使用已证权威截止版本；若为独立经济事件，须先补唯一事件键再合并。',
                '业务决定：确认已存在的账单截止/期间政策；版本权威缺证应补原件，不让业务猜哪一版金额正确。']
            lines += _reference_lines(item['source_references'], originals)
        lines += ['']
        safe.append({'id': item['id'], 'count': count})
    pending = refund_review['source_pending']
    lines += ['## TB-refund-policy-and-original-date',
        '影响范围：退款原单/原日待证行数=' + str(len(pending)),
        '当前配置（不是批准替代品）：', '```json',
        json.dumps(refund_review['effective_configuration'], ensure_ascii=False, indent=2), '```',
        '原件可采集：对应原销售订单、原日及历史配置变更/批准依据。',
        '工程可推导：按已证原单原日执行明确配置；缺原日不推断为零，也不临时改按发生月制造一致。',
        '业务决定：确认历史适用的退款归期配置；当前order_number与旧文档差异不能自动视为已批准改规则。']
    lines += _reference_lines(pending, originals)
    lines += ['', '## 营业范围、公司总额、人员与实际影子',
        '既有7/8月目标不能替代首批新月批准。首批新月未指定，保持待定。',
        '可自动采集：各店营业起止/暂停记录、兼职总额凭据（零元也须明确）、现有人员/方案有效期、核定/实发原记录。',
        '工程可推导：固定来源SHA、有效店月、公司分摊及人员守恒、关闭记录不变；不能从今日归档标志推历史停业。',
        '业务批准：基于已收凭据确认首批月份、营业范围、适用规则与已核定总额。不是签认未知引擎数。',
        '实际影子：当前没有两期真实自动化观察或人工耗时基线；历史回放和两个月原件不计作两期生产观察。',
        '完整财务expected、批准manifest及实际影子仍缺，保持blocked。', '']
    platforms = sorted({item['platform'] for item in source_review.get('scopes', [])}
                       | set(source_review.get('unverified_operating_platforms', [])))
    lines += ['### 既有7/8月资格范围逐平台材料',
        '这是既有资格目标清单，不是首批新月已批准营业范围。没有原件不能证明停业，也不能据此伪造零元数据。']
    for platform in platforms:
        lines += ['- ' + platform + '：采集7/8月原始业务/结算材料及营业起止凭据；工程核来源、期间与映射；业务仅依凭据批准该平台各店月是否营业。']
    lines += ['- 公司兼职总额：采集两期原始总额与核定凭据，明确零也需要证据；工程核公司分摊守恒；业务批准原核定总额及有效月。',
        '- 人员/方案：先自动采集生产已存在的人员、店铺成员、商品提成方案和政策的相关生效版本，以及核定/实发记录，避免全库复制或要求业务重新提供已有资料。工程核候选店月/商品的配置版本与按人守恒；只有未配置、冲突或有效期不明才交业务确认。空seed不能证明没有人员。',
        '- 缺少批准workspace_decisions、完整expected.scopes和案例manifest时，以上各项继续待核，不能签资格收据。', '']
    safe.append({'id': 'TB-refund-policy-and-original-date', 'count': len(pending)})
    return '\n'.join(lines), safe


def package(baseline, baseline_sha, observed, source_fields, source_proof, metadata):
    from ledger.replay import compare
    replay = compare(observed, baseline)
    changes = [asdict(change) | {'change_id': identity([change.store, change.period, change.path])}
               for change in replay.changes]
    safe_changes = [safe_change(change) for change in replay.changes]
    tracks = {'historical_regression': {'scope': scope(baseline['stores']),
            'original_baseline_preserved': True, 'independent_full_expected_established': False},
        'approved_new_month': {'first_cycle': None, 'operating_scope_approved': False,
            'company_totals_approved': False, 'personnel_snapshot_complete': False},
        'actual_shadow': {'observed_production_cycles': 0, 'human_time_baseline_measured': False,
            'historical_replay_counted_as_production_cycle': False}}
    private = {'schema': 1, 'kind': 'candidate_baseline_and_source_review',
        'baseline': baseline_identity(baseline, baseline_sha), 'observation': metadata,
        'observed_scope': scope(observed), 'differences': changes, 'appeared': replay.appeared,
        'vanished': replay.vanished, 'partial_source_field_matrix': source_fields,
        'partial_source_proof': source_proof, 'tracks': tracks,
        'financial_golden_issued': False, 'approval_reference': None,
        'status': 'blocked_pending_independent_financial_expected_and_approval'}
    safe = {key: private[key] for key in ('schema', 'kind', 'baseline', 'observed_scope', 'appeared',
                                        'vanished', 'tracks', 'financial_golden_issued', 'approval_reference', 'status')}
    safe.update(observation={key: metadata[key] for key in metadata if key.endswith('_sha256') or key in {'engine_version', 'code_sha'}},
        difference_count=len(changes), money_field_change_count=len(replay.money_changes),
        numeric_nonmoney_change_count=len(replay.count_changes),
        difference_kind_counts=dict(Counter(change.kind for change in replay.changes)),
        field_family_counts=dict(Counter(row['field_family'] for row in safe_changes)),
        differences=safe_changes,
        partial_source_fields=[{key: row[key] for key in ('store_id', 'period', 'field', 'source_status',
            'review_control_available', 'financial_approved')} for row in source_fields])
    return private, safe


def write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--classification-model', type=Path, required=True,
                        help='Explicit frozen applicable classification model, distinct from legacy replay model')
    parser.add_argument('--source-candidate', type=Path, required=True)
    parser.add_argument('--source-review', type=Path, required=True)
    parser.add_argument('--refund-review', type=Path, required=True)
    parser.add_argument('--cost-controls', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from ledger.model.loader import load_model
    from ledger.replay import input_manifest, snapshot
    from ledger.version import engine_version, source_code_sha
    baseline_sha = sha(args.baseline)
    tool_sha = sha(__file__)
    baseline = read(args.baseline)
    fields, proof, originals = verified_source_candidate(args.source_candidate)
    # No service, Registry, provider, authority, baseline writer, or release tool.
    os.environ['LEDGER_ORDER_FEED_ENABLED'] = '0'
    before_inventory = input_manifest(args.model, args.corpus, {})
    before_source = source_code_sha()
    observed = snapshot(load_model(args.model), args.corpus)
    inventory = input_manifest(args.model, args.corpus, observed)
    if (before_inventory['inputs'] != inventory['inputs'] or before_inventory['model'] != inventory['model']
            or source_code_sha() != before_source):
        raise ValueError('Observed source/model/input identities changed during review')
    metadata = {'engine_version': engine_version(),
        'code_sha': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'source_code_sha256': source_code_sha(), 'input_manifest_sha256': identity(inventory),
        'review_tool_sha256': tool_sha,
        'kind': 'engine_observation_not_independent_expected'}
    private, safe = package(baseline, baseline_sha, observed, fields, proof, metadata)
    private['input_manifest'] = inventory
    costs = read(args.cost_controls)
    for item in costs['stores']:
        if sha(item['private_path']) != item['sha256']:
            raise ValueError('Independent component control identity changed')
    private['component_math_controls'] = {'path': str(args.cost_controls), 'sha256': sha(args.cost_controls), 'controls': costs,
        'whole_store_cogs_or_financial_completion_established': False}
    private['unmapped_subject_matcher_review'] = {
        'classification_model_path': str(args.classification_model),
        'classification_model_file_manifest': input_manifest(args.classification_model, args.corpus, {})['model'],
        'probes': audit_unmapped_subjects(read(args.source_review), load_model(args.classification_model))}
    human, business = human_review(read(args.source_review), read(args.refund_review), originals)
    safe['business_items'] = business
    if sha(args.baseline) != baseline_sha or sha(args.source_candidate) != proof['sha256'] or sha(__file__) != tool_sha:
        raise ValueError('Original baseline, source proposal, or review tool changed during review')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private(args.output / 'observed.private.json', json.dumps(observed, ensure_ascii=False, indent=2, allow_nan=False))
    write_private(args.output / 'review.private.json', json.dumps(private, ensure_ascii=False, indent=2, allow_nan=False))
    write_private(args.output / 'review.safe.json', json.dumps(safe, ensure_ascii=False, indent=2, allow_nan=False))
    write_private(args.output / 'human-review.private.md', human)
    write_private(args.output / 'review-tool.frozen.py', Path(__file__).read_text(encoding='utf-8'))
    print(json.dumps({'output': str(args.output), 'status': safe['status'],
        'difference_count': safe['difference_count'], 'source_supported_fields': sum(row['review_control_available'] for row in fields),
        'business_items': business, 'financial_golden_issued': False}))


if __name__ == '__main__':
    main()
