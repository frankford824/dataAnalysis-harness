"""Verify approved historical cases through the offline accounting service.

Usage (there is deliberately no record/update-baseline command)::

    ledger/.venv/bin/python tools/verify-ledger-service-cases.py \
      --candidate-model models/cn-ecommerce --target-period 2026-07 \
      --manifest /evidence/approved.json \
      --junit /attempt/service_cases.xml --output /attempt/service_cases.json

The approved manifest has schema_version=1, kind="historical_golden", a nonblank
approval_reference, latest_period="YYYY-MM", target_scopes=["store_id/YYYY-MM"],
and cases=[{"path":"case.json", "sha256":"<case file SHA256>"}]. Paths are
relative to the manifest. latest_period must equal the required --target-period:
it is the approved target data month, not an inference from the wall clock.
Enabled platforms are those used by unarchived candidate stores; a catalog-only
platform with no operating store does not require invented historical data.
Targets must cover the target month and its predecessor overall, every enabled
platform in at least one complete store-period, and at least one same store in
both months. A platform that operated in only one month need not fabricate zero
data for the other month. Platforms cannot be hidden by selecting fewer targets.

Each case follows ledger.replay_service.execute's contract, including approved
expected.scopes, and additionally declares source_evidence={"kind":
"historical_export", "reference":"<source/approval evidence>", "input_sha256":
["<every original slot SHA256>"]}. The evidence inventory must exactly match the
verified bundle's original files. It is an approval attestation bound to material
hashes, not an independent proof that a human supplied truthful provenance.
Known fixture/synthetic declarations are rejected even when kind is relabeled.

When historical operating scope/overhead decisions are required, the manifest
and every case must reference the same workspace_decisions={path,sha256,
approval_reference}; case.source_evidence.workspace_decisions_sha256 must agree.
The file is schema_version=1, kind="approved_workspace_decisions", with that
approval_reference and decisions=[{period,store_id,kind,values,reason}]. Only
store_scope and overhead_total are admitted, through real decision preview/apply
in a temporary Workspace. No missing total becomes zero. Required platforms then
come from that same approved per-month operating scope; targets alone never
shrink scope. Actual service output must attest restoration of the same SHA.

Case steps support recompute/supplement/confirm/replace_scheme plus the existing
Manager drain_pending and explicit close_approved. Close steps require the same
approval_reference as their case, use the real offline human preview/confirm,
and cannot ignore Checker blockers. scopes.can_close is final Checker readiness
(always false once closed); proofs.engine_can_close is diagnostics only.

The candidate semantic model_digest is freight_trial_inputs.digest applied to
json.loads(Model.model_dump_json()). It must match both the validated model.json
and inputs.model_hash in EVERY bundle. No model merging or baseline generation
occurs. Missing materials/approval produce a blocked attempt and failed JUnit;
an amount/coverage mismatch fails. This tool never issues a release receipt.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'ledger'))
from ledger import freight_experiments_inputs as frozen
from ledger import replay_service
from ledger.freight_trial_inputs import digest
from ledger.model.loader import load_model
from ledger.model.schema import Model

SHA = re.compile(r'[0-9a-f]{64}')
PERIOD = re.compile(r'\d{4}-(0[1-9]|1[0-2])')
FIXTURE = re.compile(r'fixture|synthetic|test.only|合成|夹具', re.IGNORECASE)


class CaseBlocked(ValueError):
    pass


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _read(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise CaseBlocked('材料顶层必须是对象：' + str(path))
    return data


def _model(path):
    return load_model(path) if path.is_dir() else Model.model_validate_json(path.read_text(encoding='utf-8'))


def model_digest(model):
    return digest(json.loads(model.model_dump_json()))


def _previous(period):
    if not isinstance(period, str) or not PERIOD.fullmatch(period) or period[:4] == '0000':
        raise CaseBlocked('目标月份必须明确指定有效 YYYY-MM')
    year, month = map(int, period.split('-'))
    if year == 1 and month == 1:
        raise CaseBlocked('目标月份缺有效前一月')
    return f'{year:04d}-{month - 1:02d}' if month > 1 else f'{year - 1:04d}-12'


def _issue(code, message, status='blocked'):
    return {'code': code, 'message': str(message), 'status': status}


def _scope(key, stores):
    if not isinstance(key, str) or key.count('/') != 1:
        raise CaseBlocked('店月范围必须是 store_id/YYYY-MM')
    store_id, period = key.split('/')
    if store_id not in stores or not PERIOD.fullmatch(period) or period[:4] == '0000':
        raise CaseBlocked('范围含未知店铺或无效月份：' + key)
    return store_id, period


def _workspace_reference(reference,base):
    if not isinstance(reference,dict):raise CaseBlocked('工作区决定引用必须是绑定SHA的对象')
    normalized=dict(reference)
    path=Path(str(reference.get('path','')))
    normalized['path']=str(path.resolve() if path.is_absolute() else (base/path).resolve())
    if FIXTURE.search(str(reference.get('approval_reference',''))):raise CaseBlocked('fixture工作区决定不能冒充历史批准')
    replay_service.load_workspace_decisions(normalized)
    return normalized


def _approved_operating_scope(candidate,reference,periods):
    """Use exactly the real approval resolver; no hand-written scope arithmetic."""
    from ledger.workspace import Workspace
    from ledger.commission_registry import Registry
    from ledger import automation_checker
    with tempfile.TemporaryDirectory(prefix='ledger-service-scope-') as temporary:
        ws=Workspace(Path(temporary))
        try:
            receipt=replay_service.restore_workspace_decisions(ws,Registry(ws.root),candidate,reference)
            scope={period:sorted(store.id for store in automation_checker.operating_stores(ws,candidate,period))
                   for period in sorted(periods)}
            return scope,receipt
        finally:ws.close()


def _case(path, row, candidate, candidate_digest, expected_periods,workspace_reference=None):
    """Preflight, execute the real service, then reverify all frozen materials."""
    attempt = {'path': str(path), 'declared_sha256': row.get('sha256'),
               'case_sha256': None, 'case_id': None, 'bundle_fingerprint': None,
               'model_digest': None, 'declared_scopes': [], 'checked_scopes': [],
               'output_scopes': [], 'issues': [], 'status': 'blocked'}
    try:
        attempt['case_sha256'] = _sha(path)
        if not SHA.fullmatch(str(row.get('sha256', ''))) or attempt['case_sha256'] != row['sha256']:
            raise CaseBlocked('案例文件 SHA256 不符或未批准')
        case = _read(path)
        attempt['case_id'] = case.get('case_id')
        if case.get('schema_version') != 1 or not _text(case.get('case_id')):
            raise CaseBlocked('案例合同缺 schema_version=1 或 case_id')
        if case.get('kind') != 'historical_golden':
            raise CaseBlocked('真实历史门禁拒绝 service_fixture 或未分类案例')
        if not _text(case.get('approval_reference')):
            raise CaseBlocked('真实历史案例缺 approval_reference')
        if any(FIXTURE.search(str(case.get(key, ''))) for key in ('case_id', 'approval_reference')):
            raise CaseBlocked('案例保留 fixture/synthetic 声明，禁止冒充真实历史')
        expected = case.get('expected')
        if not isinstance(expected, dict) or not isinstance(expected.get('scopes'), dict) or not expected['scopes']:
            raise CaseBlocked('缺少批准的 expected.scopes；禁止自动录制基线')
        attempt['declared_scopes'] = sorted(expected['scopes'])
        stores = {store.id: store for store in candidate.stores}
        for key in expected['scopes']:
            _store_id, period = _scope(key, stores)
            if period > max(expected_periods):
                raise CaseBlocked('案例包含晚于 latest_period 的月份，最近月份声明不成立：' + key)
        if not _text(case.get('bundle')):
            raise CaseBlocked('案例缺固定 bundle 路径')
        bundle = Path(case['bundle'])
        bundle = bundle.resolve() if bundle.is_absolute() else (path.parent / bundle).resolve()
        case['bundle'] = str(bundle)
        attempt['bundle'] = str(bundle)
        inputs, fingerprint = frozen.verify(bundle)
        attempt['bundle_fingerprint'] = fingerprint
        attempt['bundle_manifest_sha256'] = _sha(bundle / 'bundle.json')
        bundle_model = _model(bundle / 'model.json')
        attempt['model_digest'] = model_digest(bundle_model)
        if attempt['model_digest'] != candidate_digest or inputs.get('model_hash') != candidate_digest:
            raise CaseBlocked('候选语义 model_digest 与 bundle model.json / inputs.model_hash 不一致')
        catalog = _read(bundle / 'classification.json')
        if any(FIXTURE.search(str(value)) for value in
               (bundle_model.id, bundle_model.name, catalog.get('version', ''), inputs.get('data_kind', ''))):
            raise CaseBlocked('输入包保留 fixture/synthetic 声明，禁止冒充真实历史')
        evidence = case.get('source_evidence')
        if (not isinstance(evidence, dict) or evidence.get('kind') != 'historical_export'
                or not _text(evidence.get('reference')) or FIXTURE.search(evidence['reference'])):
            raise CaseBlocked('案例缺可追溯的 historical_export 来源批准证据')
        evidence_shas = evidence.get('input_sha256')
        slot_shas = {slot['sha'] for slot in inputs.get('slots', [])}
        file_shas = {file['sha'] for file in inputs.get('files', [])}
        if (not isinstance(evidence_shas, list) or not evidence_shas or not slot_shas
                or not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in evidence_shas)
                or len(set(evidence_shas)) != len(evidence_shas)
                or set(evidence_shas) != slot_shas or slot_shas != file_shas):
            raise CaseBlocked('批准来源 SHA 清单必须完整对应输入包全部原始文件')
        attempt['source_evidence'] = evidence
        reference=case.get('workspace_decisions')
        if workspace_reference is not None:
            reference=_workspace_reference(reference,path.parent)
            if (reference['sha256']!=workspace_reference['sha256']
                or reference['approval_reference']!=workspace_reference['approval_reference']
                or evidence.get('workspace_decisions_sha256')!=reference['sha256']):
                raise CaseBlocked('案例工作区决定与批准manifest/source_evidence的SHA不一致')
            case['workspace_decisions']=reference
            attempt['workspace_decisions']=reference
        elif reference is not None or evidence.get('workspace_decisions_sha256'):
            raise CaseBlocked('案例工作区决定没有manifest一致批准，不能单独改变营业范围')
        # Do not replace this with snapshot normalization or a fixture evaluator.
        result = replay_service.execute(case)
        attempt['result'] = result
        attempt['checked_scopes'] = sorted(result.get('checked_scopes') or [])
        actual = result.get('actual') or {}
        if workspace_reference is not None:
            restored=actual.get('workspace_decisions') or {}
            if (restored.get('source_sha256')!=workspace_reference['sha256']
                or restored.get('approval_reference')!=workspace_reference['approval_reference']
                or restored.get('kind')!='approved_workspace_decisions' or restored.get('offline_only') is not True):
                raise CaseBlocked('真实服务没有恢复同一批准工作区决定，不能按声明缩减范围')
            replay_service.load_workspace_decisions(reference)
        attempt['output_scopes'] = sorted(actual.get('scopes') or {})
        if (result.get('case_id') != case['case_id'] or result.get('kind') != 'historical_golden'
                or actual.get('input_bundle_fingerprint') != fingerprint):
            raise CaseBlocked('服务输出案例身份或输入 fingerprint 不符')
        if (set(attempt['checked_scopes']) != set(attempt['output_scopes'])
                or set(attempt['declared_scopes']) != set(attempt['output_scopes'])):
            attempt['issues'].append(_issue('scope_mismatch', '批准范围、服务已检查范围和输出范围不一致', 'failed'))
        if result.get('status') != 'passed' or result.get('differences'):
            attempt['issues'].append(_issue('service_differences', '真实服务回放未通过批准期望', 'failed'))
        after_inputs, after_fingerprint = frozen.verify(bundle)
        if (after_fingerprint != fingerprint or after_inputs != inputs
                or _sha(bundle / 'bundle.json') != attempt['bundle_manifest_sha256']
                or _sha(path) != attempt['case_sha256']):
            raise CaseBlocked('案例或固定输入包在服务执行期间发生变化')
        attempt['status'] = 'failed' if attempt['issues'] else 'passed'
    except Exception as exc:
        # An exception from a malformed/missing bundle must still leave an attempt.
        attempt['issues'].append(_issue('case_material_or_execution', f'{type(exc).__name__}: {exc}'))
        attempt['status'] = 'blocked'
    return attempt


def _coverage(scopes, stores, periods):
    """Coverage of complete store-period scopes, never assumed zero months."""
    parsed = [key.split('/') for key in scopes]
    store_months = {}
    for store_id, period in parsed:
        store_months.setdefault(store_id, set()).add(period)
    return {'platforms': sorted({stores[store_id].platform for store_id, _period in parsed}),
            'periods': sorted({period for _store_id, period in parsed}),
            'cross_period_stores': sorted(store_id for store_id, months in store_months.items()
                                         if periods <= months)}


def evaluate(candidate_model, manifest_path=None, *, target_period):
    """Return inspectable evidence; missing real material can never become green."""
    candidate_model = Path(candidate_model).resolve()
    manifest_path = Path(manifest_path).resolve() if manifest_path else None
    attempt = {'schema_version': 1, 'gate': 'approved_historical_service_cases',
               'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'blocked',
               'candidate_model': str(candidate_model), 'model_digest': None,
               'model_digest_contract': 'freight_trial_inputs.digest(json.loads(Model.model_dump_json()))',
               'manifest': str(manifest_path) if manifest_path else None, 'manifest_sha256': None,
               'provenance_validation': 'declared_approval_and_hash_bound_materials',
               'period_anchor': 'required_target_period_equals_approved_manifest.latest_period',
               'target_period': target_period, 'manifest_latest_period': None,
               'periods': [], 'required_platforms': [], 'target_scopes': [],
               'catalog_only_platforms': [], 'target_platforms': [], 'target_periods': [],
               'target_cross_period_stores': [], 'verified_periods': [], 'verified_cross_period_stores': [],
               'verified_scopes': [], 'missing_scopes': [], 'verified_platforms': [],
               'unverified_platforms': [], 'untargeted_stores': [], 'cases': [], 'issues': [],
               'workspace_decisions':None,'approved_operating_stores':None}
    try:
        candidate = _model(candidate_model)
        candidate_digest = attempt['model_digest'] = model_digest(candidate)
        stores = {store.id: store for store in candidate.stores}
        platforms = {store.platform for store in candidate.stores if not store.archived}
        attempt['required_platforms'] = sorted(platforms)
        attempt['catalog_only_platforms'] = sorted({platform.id for platform in candidate.platforms} - platforms)
        attempt['unverified_platforms'] = sorted(platforms)
        if not platforms:
            raise CaseBlocked('候选模型没有未归档营业店及在用平台，空范围不能通过')
        periods = {_previous(target_period), target_period}
        attempt['periods'] = sorted(periods)
        if manifest_path is None:
            raise CaseBlocked('缺少明确批准的真实历史案例 manifest')
        attempt['manifest_sha256'] = _sha(manifest_path)
        manifest = _read(manifest_path)
        if (manifest.get('schema_version') != 1 or manifest.get('kind') != 'historical_golden'
                or not _text(manifest.get('approval_reference'))
                or FIXTURE.search(manifest['approval_reference'])):
            raise CaseBlocked('manifest 必须为已批准的 historical_golden 合同')
        attempt['approval_reference'] = manifest['approval_reference']
        attempt['manifest_latest_period'] = manifest.get('latest_period')
        if manifest.get('latest_period') != target_period:
            raise CaseBlocked('manifest.latest_period 必须等于显式 --target-period；禁止静默回退目标月份')
        workspace_reference=None
        operating={period:sorted(store.id for store in candidate.stores if not store.archived) for period in periods}
        if manifest.get('workspace_decisions') is not None:
            workspace_reference=_workspace_reference(manifest['workspace_decisions'],manifest_path.parent)
            operating,receipt=_approved_operating_scope(candidate,workspace_reference,periods)
            attempt['workspace_decisions']=workspace_reference
            attempt['workspace_decision_receipt']=receipt
            platforms={stores[sid].platform for ids in operating.values() for sid in ids}
            if not platforms:raise CaseBlocked('批准营业范围为空，不能用空范围通过真实验收')
            attempt['required_platforms']=sorted(platforms)
            attempt['unverified_platforms']=sorted(platforms)
            attempt['catalog_only_platforms']=sorted({platform.id for platform in candidate.platforms}-platforms)
        attempt['approved_operating_stores']=operating
        targets = manifest.get('target_scopes')
        if not isinstance(targets, list) or not targets or not all(isinstance(key, str) for key in targets):
            raise CaseBlocked('manifest target_scopes 必须显式指定非空店月范围')
        if len(set(targets)) != len(targets):
            raise CaseBlocked('target_scopes 含重复范围')
        attempt['target_scopes'] = sorted(targets)
        for key in targets:
            store_id, period = _scope(key, stores)
            store = stores[store_id]
            if period not in periods or store_id not in operating[period] or store.platform not in platforms:
                raise CaseBlocked('target_scopes 必须是启用店铺和最近相邻两月：' + key)
        attempt['untargeted_stores'] = sorted(store.id for store in candidate.stores
            if any(store.id in ids for ids in operating.values()) and store.platform in platforms
            and not any(key.startswith(store.id + '/') for key in targets))
        target_coverage = _coverage(targets, stores, periods)
        attempt['target_platforms'] = target_coverage['platforms']
        attempt['target_periods'] = target_coverage['periods']
        attempt['target_cross_period_stores'] = target_coverage['cross_period_stores']
        missing_platforms = platforms - set(target_coverage['platforms'])
        if missing_platforms:
            attempt['issues'].append(_issue('target_platform_coverage',
                'target_scopes 漏掉在用平台：' + ', '.join(sorted(missing_platforms))))
        if periods != set(target_coverage['periods']):
            attempt['issues'].append(_issue('target_month_coverage', 'target_scopes 总体必须覆盖目标月及相邻前一月'))
        if not target_coverage['cross_period_stores']:
            attempt['issues'].append(_issue('target_same_store_coverage', 'target_scopes 至少需要同一家营业店跨相邻两月覆盖'))
        rows = manifest.get('cases')
        if not isinstance(rows, list) or not rows:
            raise CaseBlocked('缺少批准的真实历史案例；空案例列表不能通过')
        paths, case_ids = set(), set()
        for row in rows:
            if not isinstance(row, dict) or not _text(row.get('path')):
                attempt['issues'].append(_issue('invalid_case_entry', '案例条目必须包含 path 和 sha256'))
                continue
            path = Path(row['path'])
            path = path.resolve() if path.is_absolute() else (manifest_path.parent / path).resolve()
            if path in paths:
                attempt['issues'].append(_issue('duplicate_case_path', '重复案例路径：' + str(path)))
                continue
            paths.add(path)
            case_attempt = _case(path, row, candidate, candidate_digest, periods,workspace_reference)
            attempt['cases'].append(case_attempt)
            case_id = case_attempt.get('case_id')
            if isinstance(case_id, str) and case_id in case_ids:
                attempt['issues'].append(_issue('duplicate_case_id', '重复 case_id：' + case_id))
            if isinstance(case_id, str):
                case_ids.add(case_id)
        verified = {key for row in attempt['cases'] if row['status'] == 'passed' for key in row['output_scopes']}
        attempt['verified_scopes'] = sorted(verified)
        attempt['missing_scopes'] = sorted(set(targets) - verified)
        verified_coverage = _coverage(verified & set(targets), stores, periods)
        attempt['verified_platforms'] = verified_coverage['platforms']
        attempt['verified_periods'] = verified_coverage['periods']
        attempt['verified_cross_period_stores'] = verified_coverage['cross_period_stores']
        attempt['unverified_platforms'] = sorted(platforms - set(attempt['verified_platforms']))
        if attempt['missing_scopes']:
            attempt['issues'].append(_issue('missing_verified_scopes',
                '未通过真实服务验证的目标店月：' + ', '.join(attempt['missing_scopes'])))
        if model_digest(_model(candidate_model)) != candidate_digest or _sha(manifest_path) != attempt['manifest_sha256']:
            raise CaseBlocked('候选语义模型或批准 manifest 在验证期间发生变化')
        if workspace_reference is not None:replay_service.load_workspace_decisions(workspace_reference)
        statuses = {row['status'] for row in attempt['cases']} | {row['status'] for row in attempt['issues']}
        attempt['status'] = 'blocked' if 'blocked' in statuses else 'failed' if 'failed' in statuses else 'passed'
    except Exception as exc:
        attempt['issues'].append(_issue('manifest_or_candidate', f'{type(exc).__name__}: {exc}'))
        attempt['status'] = 'blocked'
    return attempt


def write_reports(attempt, output, junit):
    output, junit = Path(output).resolve(), Path(junit).resolve()
    if output == junit:
        raise ValueError('attempt JSON 和 JUnit 必须使用不同文件')
    protected_files = {Path(row['path']).resolve() for row in attempt['cases']}
    if attempt.get('workspace_decisions'):protected_files.add(Path(attempt['workspace_decisions']['path']).resolve())
    protected_files.update(Path(row['workspace_decisions']['path']).resolve() for row in attempt['cases'] if row.get('workspace_decisions'))
    if attempt.get('manifest'):
        protected_files.add(Path(attempt['manifest']).resolve())
    candidate = Path(attempt['candidate_model']).resolve()
    protected_roots = [Path(row['bundle']).resolve() for row in attempt['cases'] if row.get('bundle')]
    if candidate.is_dir():
        protected_roots.append(candidate)
    else:
        protected_files.add(candidate)
    for destination in (output, junit):
        if destination in protected_files or any(destination.is_relative_to(root) for root in protected_roots):
            raise ValueError('报告路径不能覆盖批准材料、候选模型或固定输入包：' + str(destination))
    output.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)
    rows = [(row.get('case_id') or row['path'], row['status'], row['issues']) for row in attempt['cases']]
    rows.append(('historical_scope_and_candidate_binding', 'passed' if not attempt['issues'] else attempt['status'], attempt['issues']))
    suite = ET.Element('testsuite', name='approved_historical_service_cases', tests=str(len(rows)),
                       failures=str(sum(status != 'passed' for _name, status, _issues in rows)), errors='0', skipped='0')
    properties = ET.SubElement(suite, 'properties')
    for name in ('status', 'model_digest', 'manifest_sha256', 'target_period', 'manifest_latest_period'):
        ET.SubElement(properties, 'property', name=name, value=str(attempt.get(name)))
    for name, status, issues in rows:
        case = ET.SubElement(suite, 'testcase', classname='ledger.historical_service', name=str(name))
        if status != 'passed':
            failure = ET.SubElement(case, 'failure', type=status,
                                    message='; '.join(row['message'] for row in issues) or status)
            failure.text = json.dumps(issues, ensure_ascii=False)
    ET.indent(suite)
    ET.ElementTree(suite).write(junit, encoding='utf-8', xml_declaration=True)
    attempt['junit'] = {'path': str(junit.resolve()), 'sha256': _sha(junit), 'tests': len(rows),
                        'failures': sum(status != 'passed' for _name, status, _issues in rows),
                        'errors': 0, 'skipped': 0}
    output.write_text(json.dumps(attempt, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--candidate-model', type=Path, required=True)
    parser.add_argument('--target-period', required=True, help='Approved target data month YYYY-MM; must equal manifest.latest_period')
    parser.add_argument('--manifest', type=Path, help='Absent materials produce a blocked attempt, never a skip')
    parser.add_argument('--junit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Structured attempt JSON; never a release receipt')
    args = parser.parse_args(argv)
    attempt = evaluate(args.candidate_model, args.manifest, target_period=args.target_period)
    write_reports(attempt, args.output, args.junit)
    print(json.dumps({key: attempt[key] for key in ('status', 'model_digest', 'periods',
        'required_platforms', 'verified_platforms', 'unverified_platforms', 'missing_scopes')}, ensure_ascii=False))
    return 0 if attempt['status'] == 'passed' else 2 if attempt['status'] == 'blocked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
