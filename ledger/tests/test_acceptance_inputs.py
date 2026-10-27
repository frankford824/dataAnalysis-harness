"""Approval inventories bind CSV, opaque CAS and feed bytes, not only XLSX."""
import importlib.util
import json
from pathlib import Path

import pytest

REPO=Path(__file__).resolve().parents[2]


def module():
    spec=importlib.util.spec_from_file_location('acceptance_inputs_test',REPO/'tools/ledger_acceptance_inputs.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


@pytest.fixture
def approved(tmp_path):
    tool=module();corpus=tmp_path/'corpus';corpus.mkdir();(corpus/'legacy.xlsx').write_bytes(b'synthetic legacy corpus')
    bundle=tmp_path/'frozen';(bundle/'feed').mkdir(parents=True);(bundle/'files/ab').mkdir(parents=True)
    for name,body in [('original.csv',b'day,sku,qty\n2030-01-01,SYNTHETIC,2\n'),
                      ('files/ab/abcdef',b'synthetic original CAS bytes'),('feed/orders.parquet',b'synthetic input identity, not a real dataframe'),
                      ('deltas.json',b'[]')]:
        (bundle/name).write_bytes(body)
    records=[{'path':p.relative_to(bundle).as_posix(),'sha256':tool.sha(p)} for p in sorted(bundle.rglob('*')) if p.is_file()]
    (bundle/'bundle.json').write_text(json.dumps({'schema_version':1,'files':records}))
    controls=tmp_path/'approved';controls.mkdir()
    (controls/'case.json').write_text(json.dumps({'bundle':'../frozen'}))
    (controls/'manifest.json').write_text(json.dumps({'schema_version':1,'kind':'historical_golden',
        'cases':[{'path':'case.json','sha256':tool.sha(controls/'case.json')}]}))
    roots,inputs=tool.capture(corpus,controls/'manifest.json')
    return tool,corpus,bundle,controls,{'input_roots':roots,'inputs':inputs,'corpus_root':str(corpus)}


def test_all_case_original_types_and_control_references_are_bound(approved):
    tool,_,_,_,receipt=approved
    rows=tool.verify(receipt)
    assert len(rows)==8
    assert {'original.csv','files/ab/abcdef','feed/orders.parquet','deltas.json','bundle.json','case.json','manifest.json','legacy.xlsx'}=={r['relative_path'] for r in rows}
    assert len({row['path'] for row in rows})==len(rows)


@pytest.mark.parametrize('mutation',['csv','cas','feed','control','extra_bundle_file','new_corpus_file'])
def test_changed_or_unlisted_case_inputs_cannot_keep_a_qualified_receipt(approved,mutation):
    tool,corpus,bundle,controls,receipt=approved
    target={'csv':bundle/'original.csv','cas':bundle/'files/ab/abcdef','feed':bundle/'feed/orders.parquet',
            'control':controls/'case.json','extra_bundle_file':bundle/'unapproved.csv','new_corpus_file':corpus/'new.xlsx'}[mutation]
    target.write_bytes(b'changed after acceptance')
    with pytest.raises(ValueError):tool.verify(receipt)


def test_unapproved_adjacent_notes_are_not_promoted_to_originals(approved):
    tool,corpus,_,controls,receipt=approved
    (controls/'private-notes.txt').write_text('not an approved reference')
    (corpus/'unapproved.csv').write_text('not used by the legacy XLSX corpus gate')
    assert tool.verify(receipt)==receipt['inputs']


def test_resealing_bundle_does_not_replace_the_original_approved_manifest(approved):
    tool,_,bundle,_,receipt=approved
    (bundle/'original.csv').write_text('different original')
    body=json.loads((bundle/'bundle.json').read_text())
    next(row for row in body['files'] if row['path']=='original.csv')['sha256']=tool.sha(bundle/'original.csv')
    (bundle/'bundle.json').write_text(json.dumps(body))
    with pytest.raises(ValueError,match='manifest changed'):tool.verify(receipt)
