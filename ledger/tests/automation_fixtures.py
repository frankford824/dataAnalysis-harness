"""Explicit synthetic summary evidence for isolated snapshot/close unit tests.

This does not claim a service replay or a production acceptance receipt. Real
raw-file/service/Registry integration is separately tested by test_replay_service.
The helper never changes readiness, findings, income, business operating scope
or overhead policy. It explicitly enrolls this synthetic period for isolated
automatic-action tests; production has no such default enrollment.
"""
import io
import json
from types import SimpleNamespace

import polars as pl

from ledger import automation_period_proof
from ledger.storage_integrity import seal,verified


def capture_synthetic_summary_fixture(ws,model,run_id):
    row=ws.conn.execute('SELECT * FROM run WHERE id=?',(run_id,)).fetchone()
    from conftest import fixture_publication_scope
    fixture_publication_scope(ws,model,[row['period']])
    body=json.loads(row['result'])
    source=ws.keep(f'synthetic-summary-{run_id}.json',io.BytesIO(row['result'].encode()),row['store_id'])
    with ws.conn:
        ws.conn.execute('UPDATE run SET shas=? WHERE id=?',
            (json.dumps(sorted(set(json.loads(row['shas']))|{source.sha})),run_id))
    path=ws.facts_path(run_id)
    if path.exists():
        if not verified(path):raise ValueError('Synthetic fixture must not bless corrupt existing evidence')
        facts=pl.read_parquet(path)
    else:
        totals=(body.get('calculation_inputs') or {}).get('metric_totals') or {}
        rows=[]
        for i,(metric,amount) in enumerate(sorted(totals.items())):
            try:source_id=model.metric(metric).source
            except KeyError:source_id='synthetic_summary'
            rows.append({'metric_id':metric,'source_id':source_id,'store':model.store(row['store_id']).name,
                'period':row['period'],'amount':amount,'contribution':amount,'counted':True,'linked':True,
                'file_sha':source.sha,'file_name':source.name,'sheet':'declared_summary','row_no':i+1,
                'record_type':'synthetic_aggregate_fixture'})
        facts=pl.from_dicts(rows) if rows else pl.DataFrame({'metric_id':pl.Series([],dtype=pl.String),'amount':pl.Series([],dtype=pl.Float64)})
        facts.write_parquet(path);seal(path)
    def archived(suffix):
        p=path.with_suffix(suffix)
        if p.exists():
            if not verified(p):raise ValueError('Synthetic fixture evidence checksum failed')
            return pl.read_parquet(p)
        return pl.DataFrame()
    sl=SimpleNamespace(period=row['period'],facts=facts,pricing_gaps=archived('.pricing.parquet'),
        coverage_gap_rows=archived('.coverage.parquet'),allocation_evidence=archived('.allocation.parquet'),
        operations_evidence=archived('.operations.parquet'))
    ing=SimpleNamespace(items=[SimpleNamespace(ref=SimpleNamespace(sha256=source.sha))])
    proof=automation_period_proof.prepare(ws,model,model.store(row['store_id']),ing,sl,body,None)
    automation_period_proof.save(ws,run_id,proof)
    return proof
