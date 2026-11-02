"""Real production mapping DTOs and SQLite exports preserve the same scope."""
import json
from pathlib import Path
import pytest
from ledger import freight_experiments_inputs as frozen
from test_replay_service import complete_bundle


def test_production_dto_materializes_entire_scope_without_rewriting_frozen_bundle(complete_bundle,tmp_path):
    bundle,_=complete_bundle
    inputs=json.loads((bundle/'inputs.json').read_text())
    original=inputs['mapping']
    inputs['mapping']=[json.loads(row['payload_json']) for row in original]
    # A missing mapping stays missing rather than being auto-confirmed.
    inputs['mapping'].append({'order_store_id':'unknown-shop','ledger_store_id':None,'mapping_status':'missing'})
    (bundle/'inputs.json').write_text(json.dumps(inputs));frozen.seal(bundle)
    before={str(p.relative_to(bundle)):p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    feed=frozen.materialize_feed(bundle,tmp_path/'scratch')
    with feed._connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM feed_store ORDER BY order_store_id')]
    assert len(rows)==len(inputs['mapping'])
    assert {row['order_store_id'] for row in rows}=={row['order_store_id'] for row in inputs['mapping']}
    pending=next(row for row in rows if row['order_store_id']=='unknown-shop')
    assert pending['ledger_store_id']=='' and pending['mapping_status']=='missing'
    assert json.loads(pending['payload_json'])==inputs['mapping'][-1]
    assert before=={str(p.relative_to(bundle)):p.read_bytes() for p in bundle.rglob('*') if p.is_file()}


def test_original_sqlite_payload_is_preserved_byte_for_byte():
    raw=' { "order_store_id": 42, "ledger_store_id": "s", "mapping_status": "confirmed", "extra": true } '
    row={'order_store_id':'42','ledger_store_id':'s','mapping_status':'confirmed','payload_json':raw}
    assert frozen._feed_store_record(row)==row


@pytest.mark.parametrize('fault',['conflicting_id','conflicting_store','conflicting_status','null_payload','invalid_payload','absent_status'])
def test_mapping_conflict_cannot_be_hidden_by_dto_adapter(fault):
    source={'order_store_id':'42','ledger_store_id':'s','mapping_status':'confirmed'}
    row={**source,'payload_json':json.dumps(source)}
    if fault=='conflicting_id':row['order_store_id']='43'
    elif fault=='conflicting_store':row['ledger_store_id']='different'
    elif fault=='conflicting_status':row['mapping_status']='missing'
    elif fault=='null_payload':row['payload_json']=None
    elif fault=='invalid_payload':row['payload_json']='[]'
    else:row.pop('mapping_status')
    with pytest.raises(frozen.InputError):frozen._feed_store_record(row)
