import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / 'tools' / 'verify-baseline-attestation.py'
spec = importlib.util.spec_from_file_location('verify_baseline_attestation', TOOL)
tool = importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)


def write(tmp_path, excluded, **selection):
    report = {'schema_version': 1, 'kind': 'historical_golden_selection', 'target_period': '2026-07',
              'selected': [], 'clean_not_selected': [], 'complete': False, 'excluded': excluded, **selection}
    (tmp_path / 'selection.json').write_text(json.dumps(report))
    body = {'schema_version': 1, 'kind': 'no_engine_baseline_attestation', 'approval_reference': '业务负责人确认',
            'approved_by': 'owner', 'approved_at': '2026-11-02', 'target_period': '2026-07',
            'evidence': {'path': 'selection.json', 'sha256': hashlib.sha256((tmp_path / 'selection.json').read_bytes()).hexdigest()}}
    (tmp_path / 'attestation.json').write_text(json.dumps(body))
    return tmp_path / 'attestation.json'


MANUAL = [{'store_id': 's', 'period': '2026-06', 'reasons': ['manual_finance', 'cost_line_total_unknown']},
          {'store_id': 's', 'period': '2026-07', 'reasons': ['manual_close_ignored_blockers', 'manual_finance']},
          {'store_id': 't', 'period': '2026-07', 'reasons': ['not_closed']}]


def test_accepts_when_every_month_was_closed_by_a_manual_decision(tmp_path):
    path = write(tmp_path, MANUAL)
    results = tool.evaluate(path, '2026-07')
    assert all(message is None for _, message in results) and len(results) == 9
    assert tool.main(['--attestation', str(path), '--target-period', '2026-07', '--junit', str(tmp_path / 'r.xml')]) == 0
    assert 'failures="0"' in (tmp_path / 'r.xml').read_text()


@pytest.mark.parametrize('excluded,selection', [
    (MANUAL + [{'store_id': 'u', 'period': '2026-06', 'reasons': ['feed_changes_after_close']}], {}),
    (MANUAL, {'selected': [{'store_id': 'u', 'period': '2026-07'}]}),
    (MANUAL, {'clean_not_selected': [{'store_id': 'u', 'period': '2026-07', 'reasons': ['per_platform_limit']}]}),
    (MANUAL[1:], {}),
    (MANUAL, {'target_period': '2026-06'}),
])
def test_rejects_when_any_store_month_could_have_been_replayed(tmp_path, excluded, selection):
    path = write(tmp_path, excluded, **selection)
    assert any(message for _, message in tool.evaluate(path, '2026-07'))
    assert tool.main(['--attestation', str(path), '--target-period', '2026-07', '--junit', str(tmp_path / 'r.xml')]) == 1


def test_changed_evidence_is_rejected(tmp_path):
    path = write(tmp_path, MANUAL)
    (tmp_path / 'selection.json').write_text('{}')
    assert dict(tool.evaluate(path, '2026-07'))['evidence_hash']
