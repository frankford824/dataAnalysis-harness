"""Verify an owner attestation that no closed store-month has an engine baseline.

Usage::

    ledger/.venv/bin/python tools/verify-baseline-attestation.py \
      --attestation /evidence/attestation.json --target-period 2026-07 \
      --junit /attempt/baseline_attestation.xml

The attestation has schema_version=1, kind="no_engine_baseline_attestation", a
nonblank approval_reference, approved_by, approved_at, target_period and
evidence={"path":"selection.json","sha256":...} relative to the attestation.
The evidence is the selection report of tools/build-historical-golden.py run
read-only on production. It is accepted only when that report selected nothing
and every store-month of the target month and its predecessor was excluded
because it was closed by a manual finance decision or was not closed at all.
Any store-month that could have been replayed fails the check: the release must
then use approved service cases instead. This tool never issues a receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from xml.sax.saxutils import quoteattr

NO_ENGINE_BASELINE = {'manual_finance', 'not_closed'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def previous(period):
    year, month = map(int, period.split('-'))
    return f'{year-1:04d}-12' if month == 1 else f'{year:04d}-{month-1:02d}'


def evaluate(attestation_path, target_period):
    """Return [(case name, failure message or None)] in a stable order."""
    results = []
    def check(name, ok, message):
        results.append((name, None if ok else message));return ok
    path = Path(attestation_path).resolve()
    try:body = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return [('attestation_readable', f'attestation unreadable: {error}')]
    check('attestation_contract', body.get('schema_version') == 1 and body.get('kind') == 'no_engine_baseline_attestation'
          and all(str(body.get(k) or '').strip() for k in ('approval_reference', 'approved_by', 'approved_at')),
          'attestation schema, kind or approval fields are missing')
    check('attestation_period', body.get('target_period') == target_period, 'attestation target period differs')
    evidence = body.get('evidence') or {}
    relative = evidence.get('path')
    if (not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute()
            or '..' in PurePosixPath(relative).parts or '\\' in relative):
        results.append(('evidence_path', 'evidence path must be relative to the attestation'));return results
    report = path.parent / relative
    if not check('evidence_hash', report.is_file() and not report.is_symlink() and sha(report) == evidence.get('sha256'),
                 'evidence report is missing or changed'):return results
    selection = json.loads(report.read_text(encoding='utf-8'))
    check('evidence_contract', selection.get('schema_version') == 1 and selection.get('kind') == 'historical_golden_selection'
          and selection.get('target_period') == target_period, 'evidence is not a selection report for this period')
    check('nothing_replayable', not selection.get('selected') and not selection.get('clean_not_selected')
          and selection.get('complete') is False, 'the selection found replayable store-months; use approved service cases')
    periods = {target_period, previous(target_period)}
    excluded = [row for row in selection.get('excluded') or [] if row.get('period') in periods]
    check('both_months_present', {row['period'] for row in excluded} == periods, 'a month has no excluded store-months')
    for row in sorted(excluded, key=lambda r: (r['period'], r['store_id'])):
        reasons = set(row.get('reasons') or [])
        check(f"{row['period']}/{row['store_id']}", bool(reasons & NO_ENGINE_BASELINE),
              'closed without a manual finance decision: ' + ','.join(sorted(reasons)))
    return results


def junit(results, path):
    failures = sum(message is not None for _, message in results)
    cases = ''.join(f'<testcase classname="baseline_attestation" name={quoteattr(name)}>'
                    + (f'<failure message={quoteattr(message)}/>' if message else '') + '</testcase>' for name, message in results)
    Path(path).write_text(f'<testsuites><testsuite name="baseline_attestation" tests="{len(results)}" failures="{failures}" '
                          f'errors="0" skipped="0">{cases}</testsuite></testsuites>', encoding='utf-8')
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--attestation', type=Path, required=True)
    parser.add_argument('--target-period', required=True)
    parser.add_argument('--junit', type=Path, required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', args.target_period):parser.error('target period must be YYYY-MM')
    results = evaluate(args.attestation, args.target_period)
    failures = junit(results, args.junit)
    print(json.dumps({'tests': len(results), 'failures': failures,
                      'failed': [name for name, message in results if message][:20]}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == '__main__':raise SystemExit(main())
