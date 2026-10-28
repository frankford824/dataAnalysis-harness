"""Candidate adjudication cannot promote engine output into financial approval."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N

import pytest

SPEC = importlib.util.spec_from_file_location('baseline_review', Path(__file__).resolve().parents[2] / 'tools/ledger_baseline_review.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.fixture
def source(tmp_path):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    raw = bundle / 'original'; raw.write_bytes(b'explicit synthetic original for contract tests')
    manifest = save(bundle / 'bundle.json', {'files': [{'path': raw.name, 'sha256': review.sha(raw)}]})
    row_evidence = save(tmp_path / 'joined-posting-rows.private.json', {'synthetic_original_row_references': []})
    oracle = save(tmp_path / 'posting-oracle.frozen.private.json', {
        'engine_outputs_used_as_expected': False, 'row_evidence_sha256': review.sha(row_evidence)})
    candidate = save(tmp_path / 'candidate.json', {
        'kind': 'bounded_real_source_service_case_proposal.v2', 'engine_amounts_used_as_expected': False,
        'financial_golden': False, 'source_bundle': str(bundle),
        'source_bundle_manifest_sha256': review.sha(manifest), 'source_oracle_sha256': review.sha(oracle),
        'scopes': [{'store_id': 's1', 'period': '2026-07', 'final_financial_ready': False, 'statement': {
            'income': {'status': 'independent_source_sum', 'value': 10, 'comparison_required': True, 'formula': 'original sum'},
            'goods': {'status': 'unreviewed_or_pending_source', 'value': None, 'comparison_required': False, 'formula': None}}}]})
    return candidate, raw


def test_legacy_identity_and_equal_engine_output_do_not_establish_financial_truth():
    baseline = {'engine_version': '90ae2b8-dirty', 'stores': {'s1': {'2026-05': {'can_close': True}}}}
    private, safe = review.package(baseline, 'a' * 64, baseline['stores'], [], {}, {'code_sha': 'b' * 40})
    assert safe['difference_count'] == 0
    assert len(safe['baseline']['issues']) == 3
    assert private['financial_golden_issued'] is False and private['approval_reference'] is None
    assert private['status'].startswith('blocked_')
    assert private['tracks']['actual_shadow']['observed_production_cycles'] == 0
    assert private['tracks']['approved_new_month']['first_cycle'] is None


def test_complete_private_differences_preserve_removed_periods_and_redact_safe_people():
    person = 'SENSITIVE_PERSON_998877'
    baseline = {'engine_version': 'a' * 40, 'stores': {'s1': {
        '2026-05': {'commission': {'people': [{'person': person, 'amount': 1}]}, 'statement': []},
        '2026-06': {'state': 'closed'}}}}
    observed = {'s1': {'2026-05': {'commission': {'people': [{'person': person, 'amount': 2}]}, 'statement': []},
                       '2026-07': {'state': 'open'}}}
    private, safe = review.package(baseline, 'a' * 64, observed, [], {}, {'code_sha': 'b' * 40})
    assert person in json.dumps(private)
    assert person not in json.dumps(safe)
    assert safe['money_field_change_count'] == 1
    assert safe['vanished'] == ['s1 2026-06'] and safe['appeared'] == ['s1 2026-07']
    assert private['tracks']['historical_regression']['scope'] == {'s1': ['2026-05', '2026-06']}


def test_partial_source_fields_never_zero_missing_cost_or_assert_completion(source):
    fields, proof, originals = review.verified_source_candidate(source[0])
    assert fields[0]['expected'] == 10 and fields[0]['review_control_available']
    assert fields[1]['expected'] is None and not fields[1]['review_control_available']
    assert all(row['financial_approved'] is False for row in fields)
    assert proof['verified_bundle_files'] == 1 and originals


@pytest.mark.parametrize('mutation', ['engine_expected', 'golden', 'oracle_engine', 'original_changed', 'row_evidence_changed'])
def test_engine_self_certification_and_changed_originals_are_rejected(source, mutation):
    candidate, original = source
    value = review.read(candidate)
    if mutation == 'engine_expected': value['engine_amounts_used_as_expected'] = True
    elif mutation == 'golden': value['financial_golden'] = True
    elif mutation == 'oracle_engine':
        oracle = candidate.with_name('posting-oracle.frozen.private.json')
        save(oracle, {'engine_outputs_used_as_expected': True});value['source_oracle_sha256'] = review.sha(oracle)
    elif mutation == 'row_evidence_changed':
        save(candidate.with_name('joined-posting-rows.private.json'), {'changed': True})
    else: original.write_bytes(b'changed after capture')
    save(candidate, value)
    with pytest.raises(ValueError): review.verified_source_candidate(candidate)


def test_private_business_wording_never_enters_safe_ids(tmp_path):
    sensitive = 'PRIVATE_ORIGINAL_SUBJECT_778899'
    source = {'source_physical_controls': {'unmapped_subject_groups': [{
        'subject_sha256': 'a' * 64, 'subject_value_private': sensitive, 'occurrences': 89}]},
        'remaining_business': [{'id': 'DY-policy', 'platform': 'douyin', 'periods': ['2026-07', '2026-08'],
            'subject_sha256': 'a' * 64, 'canonical_original_events': 89,
            'source_references': [{'source_sha256': 'b' * 64, 'sheet': 'sheet', 'row': 2, 'subject_column': 'scene'}]}]}
    human, safe = review.human_review(source, {'source_pending': [], 'effective_configuration': []}, {'b' * 64: 'original'})
    assert sensitive in human and sensitive not in json.dumps(safe)
    path = tmp_path / 'human-review.private.md';review.write_private(path, human)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): review.write_private(path, 'replace existing evidence')


def test_existing_matcher_check_distinguishes_known_rule_from_new_business_policy():
    from ledger.model.schema import Model, Platform, Template, ColumnBinding, DictionaryEntry, SourceContract
    model = Model(id='contract-only', name='Explicit synthetic matcher contract',
        platforms=(Platform(id='douyin', name='D'),),
        sources=(SourceContract(id='settlement', name='Settlement', owner_role='shop_owner', cadence='monthly'),),
        templates=(Template(id='douyin_contract', name='contract', source='settlement',
            match_columns=('subject',), bindings=(ColumnBinding(role='subject', columns=('subject',)),)),),
        dictionary=(DictionaryEntry(platform='douyin', raw='known', minor='known', major='software_fee'),))
    source = {'source_physical_controls': {'unmapped_subject_groups': [
        {'platform': 'douyin', 'subject_value_private': subject, 'subject_sha256': review.identity(subject)}
        for subject in ('known', 'unknown')]}}
    known, unknown = review.audit_unmapped_subjects(source, model)
    assert known['normalized_exact_known_fee_matches'] == 1
    assert all(probe['classified'] for probe in known['matcher_probes'])
    assert not known['simple_whitespace_or_unicode_alias_failure_reproduced']
    assert unknown['normalized_exact_known_fee_matches'] == 0
    assert not any(probe['classified'] or probe['excluded'] for probe in unknown['matcher_probes'])
    assert all(row['semantic_alias_or_economic_nature_established'] is False for row in (known, unknown))
