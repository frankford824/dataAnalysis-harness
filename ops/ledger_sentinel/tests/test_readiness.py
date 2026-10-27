"""All fixtures in this file are synthetic contracts, never real cycle observations."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import pytest

from ops.ledger_sentinel import readiness as module
from ops.ledger_sentinel.readiness import Readiness, REQUIRED_OBSERVATIONS
from ops.ledger_sentinel.state import State, digest
from ops.ledger_sentinel.probes import file_sha, Probes


class Clock:
    def __init__(self):self.value=1793100000.0
    def advance(self):self.value+=90
    def stamp(self):return datetime.fromtimestamp(self.value,timezone.utc).isoformat()


@pytest.fixture
def rig(tmp_path,monkeypatch):
    clock=Clock()
    monkeypatch.setattr(module.time,'time',lambda:clock.value)
    monkeypatch.setattr(module,'now',clock.stamp)
    engine=Readiness(State(tmp_path/'state'),SimpleNamespace(plans={}),minimum_seconds=60)
    return engine,clock,tmp_path


def sample(rig,period='2026-01',phase='shadow',*,index=0,input_sha=None,source_id=None,mutate=None):
    engine,clock,root=rig;folder=root/(period+'-'+phase+'-'+str(index));folder.mkdir(exist_ok=True)
    identity={'phase':phase,'period':period,'code_sha':'a'*40,'source_code_sha':'b'*64,
        'model_sha':'c'*64,'input_sha':input_sha or digest(period),'acceptance_receipt_sha':'d'*64}
    body={'schema':2,**identity,'measurement_kind':'synthetic','checks':[],
        'semantic_model_sha':'e'*64}
    for name in sorted(REQUIRED_OBSERVATIONS):
        evidence={'schema':2,**identity,'name':name,'measurement_kind':'synthetic','observed_at':clock.stamp(),
            'source_observation_id':source_id or period+phase+str(index)+name,
            'result':{'status':'passed','checked':1,'failures':0,'unknown':0,'errors':0,'skipped':0,'difference':'0'}}
        if mutate:mutate(evidence)
        path=folder/(name+'.json');path.write_text(json.dumps(evidence))
        body['checks'].append({'name':name,'artifact':path.name,'sha256':file_sha(path)})
    path=folder/'sample.json';path.write_text(json.dumps(body));return path


def observed(rig,period='2026-01',phase='shadow'):
    engine,clock,root=rig
    first=engine.capture(sample(rig,period,phase,index=0));clock.advance()
    second=engine.capture(sample(rig,period,phase,index=1))
    path=root/'observation.json';path.write_text(json.dumps({'schema':2,'sample_ids':[first,second]}))
    return engine.record(path)


def test_two_adjacent_cycles_include_shadow_then_canary_not_four(rig):
    engine,clock,_=rig
    observed(rig,'2026-01','shadow')
    assert engine.assess('shadow','a'*40,'c'*64,include_synthetic=True)['status']=='simulated_ready'
    assert engine.assess('shadow','a'*40,'c'*64)['status']=='blocked'
    clock.advance();observed(rig,'2026-02','canary')
    gate=engine.assess('canary','a'*40,'c'*64,include_synthetic=True)
    assert gate['status']=='simulated_ready' and gate['observed_periods']==['2026-01','2026-02']
    assert gate['required_cycles']==2 and gate['latest_period_has_canary']
    assert engine.assess('canary','a'*40,'c'*64)['observed_periods']==[]
    assert engine.assess('canary','a'*40,'c'*64)['synthetic_observation_count']==2


def test_nonadjacent_or_latest_shadow_cannot_complete_final_gate(rig):
    engine,clock,_=rig;observed(rig,'2026-01','shadow');clock.advance()
    observed(rig,'2026-03','canary')
    assert engine.assess('canary','a'*40,'c'*64,include_synthetic=True)['status']=='blocked'
    clock.advance();observed(rig,'2026-04','shadow')
    gate=engine.assess('canary','a'*40,'c'*64,include_synthetic=True)
    assert gate['status']=='blocked' and gate['latest_period_has_canary'] is False


@pytest.mark.parametrize('field,value',[('period','2026-02'),('code_sha','f'*40),
    ('source_code_sha','f'*64),('model_sha','f'*64),('input_sha','f'*64),('acceptance_receipt_sha','f'*64)])
def test_internal_check_identity_cannot_be_relabelled(rig,field,value):
    engine,_,_=rig
    with pytest.raises(ValueError,match='identity'):
        engine.capture(sample(rig,mutate=lambda e:e.update({field:value})))


@pytest.mark.parametrize('same_input,same_source',[(True,False),(False,True)])
def test_same_input_or_source_evidence_cannot_be_reused_for_another_month(rig,same_input,same_source):
    engine,clock,_=rig
    engine.capture(sample(rig,input_sha='1'*64,source_id='original-observation'))
    clock.advance()
    with pytest.raises(ValueError,match='another period'):
        engine.capture(sample(rig,'2026-02',input_sha='1'*64 if same_input else '2'*64,
            source_id='original-observation' if same_source else 'independent-source'))


@pytest.mark.parametrize('change',[
    lambda e:e['result'].update(unknown=1),
    lambda e:e['result'].update(checked=0),
    lambda e:e['result'].update(failures=False),
    lambda e:e['result'].update(difference='0.01') if e['name']=='amount_conservation' else None,
    lambda e:e.update(observed_at='2020-01-01T00:00:00+00:00')])
def test_failed_empty_unknown_nonzero_or_stale_is_not_passed(rig,change):
    with pytest.raises(ValueError):rig[0].capture(sample(rig,mutate=change))


def test_caller_duration_cannot_replace_collector_clock_or_repeated_events(rig):
    engine,clock,root=rig
    a=engine.capture(sample(rig,index=0))
    b=engine.capture(sample(rig,index=1))
    path=root/'observe.json';path.write_text(json.dumps({'schema':2,'sample_ids':[a,b],
        'started_at':'2000-01-01T00:00:00Z','finished_at':'2026-01-01T00:00:00Z','sample_count':999}))
    with pytest.raises(ValueError,match='duration'):engine.record(path)
    clock.advance()
    repeated=engine.capture(sample(rig,index=2,source_id='shared-source'))
    clock.advance()
    repeated2=engine.capture(sample(rig,index=3,source_id='shared-source'))
    path.write_text(json.dumps({'schema':2,'sample_ids':[repeated,repeated2]}))
    with pytest.raises(ValueError,match='independent'):engine.record(path)


def test_retained_sample_is_immutable_and_later_corruption_invalidates_gate(rig):
    engine,_,_=rig;observed(rig)
    with engine.state.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE readiness_sample SET at='changed'")
    retained=next((engine.state.home/'rollout-samples').rglob('*.json'));retained.write_text('{}')
    gate=engine.assess('shadow','a'*40,'c'*64,include_synthetic=True)
    assert gate['status']=='blocked' and gate['invalid_observation_count']==1


def close_fixture(root,identity,clock,*,kind='human',at=None,corrupt=False,semantic_model_sha='e'*64):
    """Synthetic read-only database contract, not a service-generated finance receipt."""
    from ledger.automation_store import SCHEMA
    from ledger.storage_integrity import seal
    root.mkdir()
    original=b'synthetic original; no business rows'
    import hashlib
    sha=hashlib.sha256(original).hexdigest();source=root/'files'/sha[:2]/sha
    source.parent.mkdir(parents=True);source.write_bytes(original)
    facts=root/'facts.parquet';facts.write_bytes(b'synthetic sealed artifact');seal(facts)
    manifest={'period':identity['period'],'store_id':'synthetic-store','engine':identity['code_sha'],
        'source_code_sha':identity['source_code_sha'],'model_sha':semantic_model_sha,
        'source_refs':[sha],'slots':[['synthetic-store','synthetic-original.txt',sha]],
        'artifacts':[{'path':facts.name,'sha':file_sha(facts)}]}
    stamp=at or clock.stamp()
    result={'receipt_id':'close-r','batch_id':'proposal','period':identity['period'],'status':'completed',
        'completed':[{'store_id':'synthetic-store','run_id':1}],'unfinished':[]}
    with sqlite3.connect(root/'workspace.db') as db:
        db.executescript(SCHEMA)
        db.execute('CREATE TABLE period(store_id TEXT,period TEXT,state TEXT,run_id INTEGER)')
        db.execute("INSERT INTO period VALUES(?,?,'closed',1)",('synthetic-store',identity['period']))
        preview={'overhead':{'basis':[{'store_id':'synthetic-store'}]},'blocked':[]}
        db.execute('INSERT INTO automation_proposal VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('proposal','close',identity['period'],'',identity['input_sha'],'f'*64,json.dumps(preview),'synthetic-human',0,stamp))
        db.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            ('auth','close-request','synthetic-human',kind,'close.confirm',identity['input_sha'],'f'*64,
             json.dumps({'period':identity['period'],'store_ids':['synthetic-store']}),stamp))
        db.execute('INSERT INTO automation_receipt VALUES(?,?,?,?,?,?,?)',
            ('close-r','close-request','close.confirm','9'*64,'auth',json.dumps(result),stamp))
        db.execute('INSERT INTO automation_run_proof VALUES(?,?,?,?,?,?)',
            (1,'1'*64,'2'*64,'3'*64,json.dumps(manifest),stamp))
    if corrupt:source.write_bytes(b'changed')
    return root


def test_human_close_requires_originals_current_state_and_fresh_window(rig):
    engine,clock,root=rig;body=json.loads(sample(rig).read_text());identity=module._identity(body)
    workspace=close_fixture(root/'workspace',identity,clock)
    engine.workspace_root=workspace
    before=file_sha(workspace/'workspace.db')
    proof=engine._close('close-r',identity,clock.value-1,clock.value+1,'e'*64)
    assert proof['source_refs'] and proof['authorization_sha256'] and file_sha(workspace/'workspace.db')==before
    with pytest.raises(ValueError,match='window'):engine._close('close-r',identity,clock.value+1,clock.value+100,'e'*64)
    with sqlite3.connect(workspace/'workspace.db') as db:db.execute("UPDATE period SET state='open'")
    with pytest.raises(ValueError,match='closed state'):engine._close('close-r',identity,clock.value-1,clock.value+1,'e'*64)


@pytest.mark.parametrize('kind,corrupt',[('service',False),('human',True)])
def test_service_identity_or_missing_original_cannot_satisfy_manual_close(rig,kind,corrupt):
    engine,clock,root=rig;identity=module._identity(json.loads(sample(rig).read_text()))
    engine.workspace_root=close_fixture(root/'workspace',identity,clock,kind=kind,corrupt=corrupt)
    with pytest.raises(ValueError):engine._close('close-r',identity,clock.value-1,clock.value+1,'e'*64)


def test_actual_capture_requires_accepted_context_and_does_not_trust_manual_close_boolean(rig):
    engine,_,_=rig;path=sample(rig);body=json.loads(path.read_text())
    body.update(measurement_kind='actual',manual_close=True,close_receipt_id='invented')
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError,match='acceptance context'):engine.capture(path)


def test_missing_candidate_source_code_identity_is_unknown_not_null_equal():
    probes=Probes([{'id':'version','kind':'http_json','url':'unused',
        'assertions':[{'field':'source_code_sha','value':'$candidate_source_code_sha'}]}],
        fetch=lambda spec:{'source_code_sha':None})
    assert probes.read('version',{'candidate_source_code_sha':None})['status']=='unknown'
    probes.fetch=lambda spec:{'source_code_sha':'a'*64}
    assert probes.read('version',{'candidate_source_code_sha':'a'*64})['status']=='ok'


def test_actual_protocol_round_trip_uses_collected_human_receipt_and_same_release(rig,monkeypatch):
    """Stub acceptance/model only inside this synthetic unit; no real cycle claimed."""
    engine,clock,root=rig
    from ledger.model import loader
    semantic=digest({'synthetic_model_contract':True})
    monkeypatch.setattr(loader,'load_model',lambda path:SimpleNamespace(model_dump=lambda **kw:{'synthetic_model_contract':True}))
    engine.executor.plans={'release':{'kind':'deployment','model_root':str(root/'models')}}
    engine.executor._accept=lambda *args:None
    acceptance=root/'acceptance.json'
    acceptance.write_text(json.dumps({'receipt_sha':'d'*64,'model_sha':'c'*64,'source_code_sha':'b'*64}))
    flag={**module._identity(json.loads(sample(rig,index=9).read_text())),
        'shadow_enabled':True,'changed_at':clock.stamp()}
    (engine.state.home/'rollout.json').write_text(json.dumps(flag))
    def actual(index,receipt=None):
        path=sample(rig,index=index);body=json.loads(path.read_text())
        body.update(measurement_kind='actual',action_id='release',acceptance_receipt=str(acceptance),
            semantic_model_sha=semantic,close_receipt_id=receipt)
        for check in body['checks']:
            file=path.parent/check['artifact'];value=json.loads(file.read_text());value['measurement_kind']='actual'
            file.write_text(json.dumps(value));check['sha256']=file_sha(file)
        path.write_text(json.dumps(body));return engine.capture(path)
    first=actual(0);clock.value+=45
    engine.workspace_root=close_fixture(root/'workspace',flag,clock,semantic_model_sha=semantic)
    clock.value+=45;last=actual(1,'close-r')
    record=root/'cycle.json';record.write_text(json.dumps({'schema':2,'sample_ids':[first,last]}))
    engine.record(record)
    assert engine.assess('shadow','a'*40,'c'*64,acceptance_receipt_sha='d'*64)['status']=='ready'
    assert engine.assess('shadow','a'*40,'c'*64,acceptance_receipt_sha='f'*64)['status']=='blocked'
    source=next((engine.workspace_root/'files').rglob('?'*64));source.write_bytes(b'changed')
    assert engine.assess('shadow','a'*40,'c'*64)['status']=='blocked'


def test_virtual_feed_source_is_not_treated_as_a_missing_cas_original(rig):
    engine,clock,root=rig;identity=module._identity(json.loads(sample(rig).read_text()))
    workspace=close_fixture(root/'workspace',identity,clock);engine.workspace_root=workspace
    with sqlite3.connect(workspace/'workspace.db') as db:
        manifest=json.loads(db.execute('SELECT manifest_json FROM automation_run_proof').fetchone()[0])
        manifest['source_refs'].append('0'*64)
        manifest['feed']={'snapshot_id':'synthetic-H0','consumed_seq':10,'source_latest_seq':10}
        db.execute('UPDATE automation_run_proof SET manifest_json=?',(json.dumps(manifest),))
    with pytest.raises(ValueError,match='captured original input bundle'):
        engine._close('close-r',identity,clock.value-1,clock.value+1,'e'*64)


def test_captured_upstream_bundle_requires_original_hashes_model_and_watermarks(rig):
    """Real frozen verifier over tiny synthetic parquet, not actual ERP/financial data."""
    import polars as pl
    from ledger import freight_experiments_inputs as frozen
    engine,_,root=rig;engine.input_bundle_root=root/'captures'
    bundle=engine.input_bundle_root/'synthetic';(bundle/'feed').mkdir(parents=True)
    objects={}
    for name in frozen.REQUIRED_OBJECTS:
        path=bundle/'feed'/name;pl.DataFrame({'synthetic':[1]}).write_parquet(path)
        objects[name]={'path':name,'sha256':file_sha(path),'bytes':path.stat().st_size,'rows':1}
    model={'synthetic_model_contract':True};semantic=digest(model)
    inputs={'period':'2026-01','model_hash':frozen.digest(model),'snapshot_id':'synthetic-H0','consumed_seq':10,'source_latest_seq':10,
        'feed_manifest':{'snapshot_id':'synthetic-H0','through_seq':10,'objects':objects},'delta_hash':digest([]),'slots':[]}
    (bundle/'model.json').write_text(json.dumps(model));(bundle/'deltas.json').write_text('[]')
    (bundle/'inputs.json').write_text(json.dumps(inputs));frozen.seal(bundle)
    reference={'ref':'synthetic','fingerprint':frozen.verify(bundle)[1]}
    identity=module._identity(json.loads(sample(rig).read_text()))
    kept,proof=engine._inputs(reference,identity,semantic)
    assert kept['snapshot_id']=='synthetic-H0' and proof['bundle_manifest_sha256']==file_sha(bundle/'bundle.json')
    with pytest.raises(ValueError,match='semantic identity'):
        engine._inputs(reference,identity,'f'*64)
    next((bundle/'feed').glob('*.parquet')).write_bytes(b'changed original')
    with pytest.raises(ValueError):engine._inputs(reference,identity,semantic)
