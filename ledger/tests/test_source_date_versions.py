"""Raw date observations do not manufacture another financial generation."""
import hashlib,json
from types import SimpleNamespace
import pytest
from ledger import source_dates,source_recovery,automation_actions as actions,automation_checker as checker,automation_store as journal
from ledger.engine.runtime import Ingestion
from ledger.engine.refund_dates import lookup
from ledger.publication_scope import load_automatic
from test_source_recovery import source_case
from test_publication_scope import configure
from test_jst_source_members import order,item,html


def publish(c,day,nonce):
    actions.sync_month(c.ws,c.model,c.period)
    case=dict(c.ws.conn.execute('SELECT * FROM automation_case').fetchone())
    tid='source-'+hashlib.sha256(nonce.encode()).hexdigest()[:40];jid=hashlib.sha256((nonce+'job').encode()).hexdigest()[:32]
    folder=c.ws.root/'source-recovery'/tid/jid;folder.mkdir(parents=True)
    raw=html(order('100',[item()],order_date=day+' 10:00:00'))
    sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.raw')).write_bytes(raw)
    receipt={'nodes':[{'order_id':'100','sha256':sha}]}
    receipt_sha=source_recovery.durable(folder/'receipt.json',receipt)
    return source_dates.publish_from_task(c.ws,c.model,case,{'platform_order_id':'202600001','shop_ids':['77']},tid,jid,receipt_sha,c.actor)


@pytest.mark.parametrize('source_case',[{'date_only':True,'with_dates':True}],indirect=True)
def test_same_date_new_raw_observation_reuses_fact_and_correction_versions_without_editing_history(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),))
    first=publish(c,'2026-05-15','initial-source-date')
    assert first['status']=='source_input_queued'
    before=source_dates.fingerprint(c.ws,'s1',c.period)
    old=[dict(r) for r in c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='date_source_fact'")]
    dispatch=c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]
    second=publish(c,'2026-05-15','equivalent-source-date')
    assert second['status']=='source_input_reused' and not second['recompute_required']
    assert source_dates.fingerprint(c.ws,'s1',c.period)==before
    assert [dict(r) for r in c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='date_source_fact'")]==old
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]==dispatch
    observation=c.ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='date_source_observation'").fetchone()
    refs=json.loads(observation[0])['source_refs'];assert len(refs)==2
    for ref in refs:
        path=__import__('pathlib').Path(ref['path'])
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==ref['sha256']
    old_input=checker.check(c.ws,'s1',c.period,model=c.model)['input_sha']
    correction=publish(c,'2026-06-16','corrected-source-date')
    assert correction['status']=='source_input_queued'
    assert source_dates.fingerprint(c.ws,'s1',c.period)!=before
    assert checker.check(c.ws,'s1',c.period,model=c.model)['input_sha']!=old_input
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='date_source_fact'").fetchone()[0]==2
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]==dispatch+1
    assert dict(c.ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(old[0]['id'],)).fetchone())==old[0]
    ing=Ingestion(c.model,[])
    source_dates.attach_saved(c.ws,c.model,c.model.store('s1'),ing,load_automatic(c.ws.root,c.model))
    resolved=lookup(ing.items,{'测试店':'测试店'})
    assert resolved['测试店','202600001'][0]=='2026-06'
    assert not c.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
    assert not c.ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()
